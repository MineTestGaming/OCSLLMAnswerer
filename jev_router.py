"""Optional TypeSafe Jev System One routing for low-risk choice questions."""

from __future__ import annotations

import math
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from typesafe_sdk import Choice, TypeSafeClient


ROUTES = frozenset(("l0_direct", "verify_l1", "direct_l2"))


@dataclass(frozen=True)
class JevConfig:
    enabled: bool = False
    api_key: str = ""
    model: str = "jev-latest"
    direct_confidence: float = 0.990

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        switch = env.get("JEV_ENABLED", "false").strip().lower()
        if switch not in ("1", "true", "yes", "on"):
            return cls()
        api_key = env.get("TYPESAFE_API_KEY", "").strip()
        model = env.get("JEV_MODEL", "jev-latest").strip() or "jev-latest"
        try:
            threshold = float(env.get("JEV_DIRECT_CONFIDENCE", "0.990"))
        except (TypeError, ValueError):
            return cls()
        if not api_key or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            return cls()
        return cls(True, api_key, model, threshold)


@dataclass(frozen=True)
class L0Result:
    answer: str
    route: str
    answer_probability: float
    route_probability: float
    model: str
    threshold: float

    @property
    def can_answer_directly(self):
        return (
            self.route == "l0_direct"
            and self.answer_probability >= self.threshold
            and self.route_probability >= self.threshold
        )


def _read(value: Any, name: str):
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _selected_probability(answer: Any, choice: str):
    probabilities = _read(answer, "probabilities")
    if not isinstance(probabilities, Mapping) or choice not in probabilities:
        raise ValueError("Jev response is missing selected probability")
    normalized = {}
    for key, value in probabilities.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Jev response contains an invalid probability")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("Jev response contains an invalid probability")
        normalized[key] = number
    total = sum(normalized.values())
    if total <= 0:
        raise ValueError("Jev response probabilities have no mass")
    return normalized[choice] / total


def _option_identity(option: str):
    value = unicodedata.normalize("NFKC", option).strip()
    value = re.sub(r"^(?:[A-Z]|\d{1,2})[.、:：)）]\s*", "", value, flags=re.I)
    return re.sub(r"\s+", "", value).casefold()


def parse_options(options: str, question_type: str):
    lines = [line.strip() for line in options.splitlines() if line.strip()]
    expected = 2 if question_type == "judgement" else None
    if expected is not None and len(lines) != expected:
        return None
    if question_type == "single" and not 2 <= len(lines) <= 26:
        return None
    identities = [_option_identity(line) for line in lines]
    if any(not item for item in identities) or len(set(identities)) != len(identities):
        return None
    return {f"option_{index + 1}": line for index, line in enumerate(lines)}


class JevRouter:
    def __init__(self, config: JevConfig, client=None):
        self.config = config
        self.client = client

    @classmethod
    def from_env(cls, env=None, client_factory=TypeSafeClient):
        config = JevConfig.from_env(env)
        if not config.enabled:
            return cls(config)
        try:
            client = client_factory(api_key=config.api_key, model=config.model)
        except Exception:
            return cls(JevConfig())
        return cls(config, client)

    @property
    def available(self):
        return self.config.enabled and self.client is not None

    def solve(self, title: str, options: str, question_type: str):
        if not self.available or question_type not in ("single", "judgement"):
            return None
        option_map = parse_options(options, question_type)
        if option_map is None:
            return None

        try:
            response = self.client.system_one(
                state={
                    "question": title,
                    "question_type": question_type,
                    "options": option_map,
                },
                questions={
                    "candidate_answer": Choice(
                        instructions=(
                            "Choose the single best answer using only one of the provided "
                            "option keys."
                        ),
                        criteria={key: value for key, value in option_map.items()},
                    ),
                    "route": Choice(
                        instructions=(
                            "Assess answer risk. Choose l0_direct only when the answer is "
                            "unambiguous and safe to return without another model."
                        ),
                        criteria={
                            "l0_direct": "Clear answer with at least 99% confidence.",
                            "verify_l1": "Some uncertainty remains; use independent verification.",
                            "direct_l2": "High semantic or reasoning risk; use the strongest model.",
                        },
                    ),
                },
            )
            answers = _read(response, "answers")
            if not isinstance(answers, Mapping):
                raise ValueError("Jev response has no answers mapping")
            candidate = answers.get("candidate_answer")
            routing = answers.get("route")
            candidate_key = _read(candidate, "choice")
            route = _read(routing, "choice")
            if candidate_key not in option_map or route not in ROUTES:
                raise ValueError("Jev response contains an unknown choice")
            answer_probability = _selected_probability(candidate, candidate_key)
            route_probability = _selected_probability(routing, route)
            model = _read(response, "model")
            if not isinstance(model, str) or not model.strip():
                model = self.config.model
            return L0Result(
                answer=option_map[candidate_key],
                route=route,
                answer_probability=answer_probability,
                route_probability=route_probability,
                model=model,
                threshold=self.config.direct_confidence,
            )
        except Exception:
            # L0 is optional. Authentication, network, timeout, SDK and schema
            # failures all preserve the established L1/L2 behavior.
            return None
