"""Model routing around the existing prompt; never rewrite model answer fields."""

import json
import os
import re
from dataclasses import dataclass

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from image_input import attach_images


MODEL_CALL_ERRORS = (OpenAIError, TimeoutError, ConnectionError)


def rejects_temperature(exc):
    """Retry only an explicit 400 temperature compatibility rejection."""
    if not isinstance(exc, APIStatusError) or exc.status_code != 400:
        return False
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error", body)
    else:
        error = body
    if isinstance(error, dict):
        if error.get("param") not in (None, "temperature"):
            return False
        message = str(error.get("message", "")).lower()
    else:
        message = str(error or "").lower()
    return "temperature" in message and any(term in message for term in (
        "unsupportedparams", "unsupported parameter", "unsupported value",
        "doesn't support", "does not support", "not supported",
    ))


def model_error_details(exc):
    """Describe the failure without exposing arbitrary provider exception text."""
    status = getattr(exc, "status_code", None)
    if rejects_temperature(exc):
        reason = "接口不支持当前 temperature 参数（HTTP 400）"
    elif isinstance(status, int):
        reason = {
            400: "接口拒绝请求参数", 401: "API Key 无效或无权访问",
            402: "账户余额不足", 403: "接口拒绝访问",
            404: "模型或接口地址不存在", 429: "请求限流或额度不足",
        }.get(status, "上游模型接口异常") + f"（HTTP {status}）"
    elif isinstance(exc, (APITimeoutError, TimeoutError)):
        reason = "模型请求超时"
    elif isinstance(exc, (APIConnectionError, ConnectionError)):
        reason = "无法连接模型接口"
    else:
        reason = "模型调用失败，未取得有效输出"
    details = {"error_type": type(exc).__name__, "reason": reason}
    if isinstance(status, int):
        details["http_status"] = status
    return details


class InvalidAnswer(ValueError):
    """The provider did not return the required complete answer JSON."""

    def __init__(self, message):
        super().__init__(message)
        self.diagnostics = {}


@dataclass(frozen=True)
class RoutingConfig:
    l1_model: str
    l2_model: str
    vision_models: frozenset[str] = frozenset()
    backup_model: str = ""
    backup_vision_models: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        main = (env.get("OPENAI_MODEL_MAIN") or env.get("OPENAI_MODEL")
                or "gpt-5.6-terra")
        l1 = env.get("OPENAI_MODEL_L1") or main
        # No assumed provider model names: existing deployments remain usable.
        vision = frozenset(name.strip() for name in
                           env.get("OPENAI_VISION_MODELS", "").split(",") if name.strip())
        backup_vision = frozenset(name.strip() for name in
                                 env.get("OPENAI_BACKUP_VISION_MODELS", "").split(",")
                                 if name.strip())
        return cls(l1, env.get("OPENAI_MODEL_L2") or main, vision,
                   env.get("OPENAI_BACKUP_MODEL", "").strip(), backup_vision)


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
        # Some compatible providers append a bare ellipsis even when
        # json_object mode is requested. Accept only that exact benign suffix;
        # explanatory prose and additional values remain invalid.
        try:
            result, end = json.JSONDecoder().raw_decode(content)
        except (ValueError, TypeError):
            raise InvalidAnswer("模型输出不是合法 JSON") from exc
        if content[end:].strip() not in ("...", "…"):
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
    def __init__(self, client, config, jev_router=None, backup_client=None):
        self.client = client
        self.config = config
        self.jev_router = jev_router
        self.backup_client = backup_client

    def solve(self, messages, title, question_type, options="", images=None, image_options=False):
        attempts = []
        reasons = static_risk(title, question_type, options)

        def record_failure(exc, model, attempt):
            exc.diagnostics = {
                **getattr(exc, "diagnostics", {}),
                "model": model,
                "provider": attempt["provider"],
                "reason": attempt["reason"],
                "attempts": attempts,
                "reasons": reasons,
            }
            if "http_status" in attempt:
                exc.diagnostics["http_status"] = attempt["http_status"]

        def call(model, *, backup=False):
            active_client = self.backup_client if backup else self.client
            vision_models = (self.config.backup_vision_models if backup
                             else self.config.vision_models)
            # Each stage chooses its own input modality. Text-only models see
            # the same Image IDs, without image_url parts or OCR.
            call_messages = (attach_images(messages, images)
                             if images and model in vision_models else messages)
            if images and model not in vision_models:
                call_messages = [*messages[:-1], {
                    **messages[-1], "content": messages[-1]["content"] +
                    "\n本次未附图片，Image ID 仅为占位符。请依据剩余文字与知识尝试作答，"
                    "不要假称看到了图片或编造图中细节；analysis 简要说明缺少图片导致的不确定性。"
                    "仍须遵守上述 answer 格式和 JSON 要求。",
                }]
            for omit_temperature in (False, True):
                attempt = {"model": model, "status": "started",
                           "provider": "backup" if backup else "primary"}
                attempts.append(attempt)
                kwargs = {"model": model, "messages": call_messages,
                          "response_format": {"type": "json_object"}}
                if omit_temperature:
                    attempt["temperature_omitted"] = True
                else:
                    kwargs["temperature"] = 0.3
                try:
                    response = active_client.chat.completions.create(**kwargs)
                except Exception as exc:
                    attempt.update(status="failed", **model_error_details(exc))
                    record_failure(exc, model, attempt)
                    if not omit_temperature and rejects_temperature(exc):
                        if "unsupported_temperature" not in reasons:
                            reasons.append("unsupported_temperature")
                        continue
                    raise
                break
            try:
                if not response.choices:
                    raise InvalidAnswer("模型未返回候选答案")
                result = parse_answer(response.choices[0])
                if image_options:
                    pattern = r"[A-Z](?:#[A-Z])+" if question_type == "multiple" else r"[A-Z]"
                    answer = result["answer"]
                    labels = [row.split(".", 1)[0] for row in options.splitlines()]
                    selected = answer.split("#")
                    if (not re.fullmatch(pattern, answer)
                            or any(label not in labels for label in selected)
                            or selected != sorted(set(selected))):
                        raise InvalidAnswer("纯图片选项须按顺序返回大写字母，多选用 # 分隔")
                if question_type == "multiple":
                    multiple_answer_set(result["answer"], options)
            except InvalidAnswer as exc:
                attempt.update(status="invalid", error_type=type(exc).__name__, reason=str(exc))
                choice = response.choices[0] if response.choices else None
                message = getattr(choice, "message", None)
                # Preserve the original output, including whitespace, fences,
                # think tags and partial JSON. Never include the request payload.
                exc.diagnostics = {
                    "model": model,
                    "raw_output": getattr(message, "content", None),
                    "finish_reason": getattr(choice, "finish_reason", None),
                    "refusal": getattr(message, "refusal", None),
                }
                record_failure(exc, model, attempt)
                raise
            attempt.update(status="valid", answer=result["answer"])
            return result

        def finish(result, model, route, confidence=None,
                   confidence_method="independent_answer_agreement"):
            return {**result, "_meta": {
                "model": model,
                "route": route,
                "upgraded": route == "escalated_l2" and self.config.l1_model != model,
                "reasons": reasons,
                "attempts": attempts,
                "confidence": confidence,
                "confidence_method": confidence_method,
            }}

        def finish_l2(route):
            try:
                result = call(self.config.l2_model)
            except (*MODEL_CALL_ERRORS, InvalidAnswer):
                if self.backup_client is None or not self.config.backup_model:
                    raise
                reasons.append("primary_l2_failed")
                return finish(call(self.config.backup_model, backup=True),
                              self.config.backup_model, "backup")
            return finish(result, self.config.l2_model, route)

        if reasons:
            return finish_l2("direct_l2")

        if (self.jev_router is not None and not images and not image_options
                and question_type in ("single", "judgement")):
            l0 = self.jev_router.solve(title, options, question_type)
            if l0 is not None:
                attempts.append({
                    "model": l0.model,
                    "status": "valid",
                    "route": l0.route,
                    "answer_probability": l0.answer_probability,
                    "route_probability": l0.route_probability,
                })
                if l0.route == "direct_l2":
                    reasons.append("jev_direct_l2")
                    return finish_l2("direct_l2")
                if l0.can_answer_directly:
                    reasons.append("jev_high_confidence")
                    return finish(
                        {
                            "answer": l0.answer,
                            "analysis": "Jev L0 高置信度直答（99.0%+）",
                        },
                        l0.model,
                        "jev_l0_direct",
                        min(l0.answer_probability, l0.route_probability),
                        "jev_system_one",
                    )
                reasons.append("jev_verify_l1")

        try:
            first = call(self.config.l1_model)
        except InvalidAnswer:
            reasons.append("invalid_l1_output")
        except MODEL_CALL_ERRORS:
            reasons.append("l1_call_failed")
        else:
            try:
                second = call(self.config.l1_model)
            except InvalidAnswer:
                reasons.append("invalid_verifier_output")
            except MODEL_CALL_ERRORS:
                reasons.append("verifier_call_failed")
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
        return finish_l2("escalated_l2")
