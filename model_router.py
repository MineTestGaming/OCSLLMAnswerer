"""Model routing around the existing prompt; never rewrite model answer fields."""

import json
import os
import re
from dataclasses import dataclass

from image_input import attach_images


class InvalidAnswer(ValueError):
    """The provider did not return the required complete answer JSON."""


@dataclass(frozen=True)
class RoutingConfig:
    l1_model: str
    l2_model: str
    vision_models: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        main = (env.get("OPENAI_MODEL_MAIN") or env.get("OPENAI_MODEL")
                or "gpt-5.6-terra")
        l1 = env.get("OPENAI_MODEL_L1") or main
        # No assumed provider model names: existing deployments remain usable.
        vision = frozenset(name.strip() for name in
                           env.get("OPENAI_VISION_MODELS", "").split(",") if name.strip())
        return cls(l1, env.get("OPENAI_MODEL_L2") or main, vision)


def static_risk(title, question_type, options=""):
    reasons = []
    if question_type not in ("single", "judgement", "completion", "multiple"):
        reasons.append("unknown_question_type")
    if question_type == "multiple" and len([s for s in options.splitlines() if s.strip()]) >= 6:
        reasons.append("many_options")
    if len(title) > 150:
        reasons.append("long_question")
    if any(term in title for term in (
        "错误的是", "不正确", "不属于", "不包括", "最符合", "最恰当",
        "最佳", "根本原因", "主要原因", "根据上述材料", "结合案例",
    )):
        reasons.append("semantic_risk")
    return reasons


def parse_answer(choice):
    message = choice.message
    if getattr(message, "refusal", None):
        raise InvalidAnswer("模型未提供答案")
    if getattr(choice, "finish_reason", None) != "stop":
        raise InvalidAnswer("模型输出未正常完成")
    content = message.content
    if not isinstance(content, str) or not content.strip():
        raise InvalidAnswer("模型输出为空")
    # Preserve support for legacy compatible providers, without extracting a
    # possibly unrelated JSON object from arbitrary explanatory prose.
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if fence:
        content = fence.group(1)
    try:
        result = json.loads(content)
    except (ValueError, TypeError) as exc:
        raise InvalidAnswer("模型输出不是合法 JSON") from exc
    if not isinstance(result, dict) or set(result) != {"answer", "analysis"}:
        raise InvalidAnswer("模型输出必须且只能包含 answer 和 analysis")
    if not isinstance(result["answer"], str) or not result["answer"].strip():
        raise InvalidAnswer("answer 必须为非空字符串")
    if not isinstance(result["analysis"], str):
        raise InvalidAnswer("analysis 必须为字符串")
    return result


def multiple_answer_set(answer, options):
    """Resolve selections for comparison only; ambiguous text is not accepted."""
    lines = [line.strip() for line in options.splitlines() if line.strip()]
    if not 2 <= len(lines) <= 26:
        raise InvalidAnswer("多选题选项缺失或无法识别")
    aliases = {}
    for index, line in enumerate(lines):
        label = re.match(r"^([A-Z]|\d{1,2})[.．、:：)）]\s*(.+)$", line)
        names = {line}
        if label:
            names.update((label.group(1), label.group(2).strip()))
        else:
            names.add(chr(ord("A") + index))
        for name in names:
            aliases.setdefault(name, set()).add(index)

    text = answer.strip().replace("＃", "#")
    # Alternative separators are unambiguous only for isolated letter labels;
    # commas inside full option text must remain part of the option text.
    if re.fullmatch(r"[A-Z](?:\s*[,，、;；]\s*[A-Z])+", text):
        parts = re.split(r"[,，、;；]", text)
    else:
        parts = text.split("#")
    selected = []
    for part in parts:
        candidates = aliases.get(part.strip(), set())
        if len(candidates) != 1:
            raise InvalidAnswer("多选答案包含空项、未知选项或歧义选项")
        selected.append(next(iter(candidates)))
    if len(selected) < 2 or len(set(selected)) != len(selected):
        raise InvalidAnswer("多选答案须包含至少两个不同选项")
    return frozenset(selected)


class ModelRouter:
    def __init__(self, client, config):
        self.client = client
        self.config = config

    def solve(self, messages, title, question_type, options="", images=None):
        attempts = []
        reasons = static_risk(title, question_type, options)

        def call(model):
            attempt = {"model": model, "status": "started"}
            attempts.append(attempt)
            # Each stage chooses its own input modality. Text-only models see
            # the same Image IDs, without image_url parts or OCR.
            call_messages = (attach_images(messages, images)
                             if images and model in self.config.vision_models else messages)
            response = self.client.chat.completions.create(
                model=model, messages=call_messages, temperature=0.3,
            )
            try:
                if not response.choices:
                    raise InvalidAnswer("模型未返回候选答案")
                result = parse_answer(response.choices[0])
                if question_type == "multiple":
                    multiple_answer_set(result["answer"], options)
            except InvalidAnswer:
                attempt["status"] = "invalid"
                raise
            attempt.update(status="valid", answer=result["answer"])
            return result

        def finish(result, model, route):
            return {**result, "_meta": {
                "model": model,
                "route": route,
                "upgraded": route == "escalated_l2" and self.config.l1_model != model,
                "reasons": reasons,
                "attempts": attempts,
                "confidence": None,
                "confidence_method": "independent_answer_agreement",
            }}

        if reasons:
            return finish(call(self.config.l2_model), self.config.l2_model, "direct_l2")

        try:
            first = call(self.config.l1_model)
        except InvalidAnswer:
            reasons.append("invalid_l1_output")
        else:
            try:
                second = call(self.config.l1_model)
            except InvalidAnswer:
                reasons.append("invalid_verifier_output")
            else:
                if question_type == "multiple":
                    agrees = (multiple_answer_set(first["answer"], options)
                              == multiple_answer_set(second["answer"], options))
                else:
                    agrees = first["answer"].strip() == second["answer"].strip()
                # Comparison never rewrites the chosen answer or analysis.
                if agrees:
                    reasons.append("answers_agree")
                    return finish(first, self.config.l1_model, "verified_l1")
                reasons.append("answer_disagreement")

        if self.config.l1_model == self.config.l2_model:
            reasons.append("same_model_fallback")
        return finish(call(self.config.l2_model), self.config.l2_model, "escalated_l2")
