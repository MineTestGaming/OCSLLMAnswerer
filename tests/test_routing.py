import json
import unittest
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from model_router import ModelRouter, RoutingConfig, InvalidAnswer
from jev_router import L0Result
from image_input import ImageReference


class FakeClient:
    """Only replaces the paid network boundary; routing and parsing remain real."""

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        content = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, refusal=None), finish_reason="stop"
        )])


MESSAGES = [{"role": "system", "content": "原 system"},
            {"role": "user", "content": "原 Prompt，含 JSON 输出要求"}]
A = {"answer": "A", "analysis": "原解析"}
B = {"answer": "B", "analysis": "复核后的解析"}


def provider_error(status, message, param=None):
    response = SimpleNamespace(request=SimpleNamespace(), status_code=status, headers={})
    return APIStatusError("private-provider-detail", response=response,
                          body={"error": {"message": message, "param": param}})


class FakeJevRouter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def solve(self, title, options, question_type):
        self.calls.append((title, options, question_type))
        return self.result


class BackupRoutingTests(unittest.TestCase):
    def test_final_primary_failure_uses_separate_backup_client(self):
        for title, responses in (
            ("普通题", (ConnectionError("l1 down"), TimeoutError("l2 down"))),
            ("以下不正确的是", (TimeoutError("l2 down"),)),
            ("普通题", (A, B, "invalid JSON")),
        ):
            with self.subTest(title=title, responses=responses):
                primary = FakeClient(*responses)
                backup = FakeClient(B)
                config = RoutingConfig("cheap", "strong", backup_model="backup")
                result = ModelRouter(primary, config, backup_client=backup).solve(
                    MESSAGES, title, "single",
                )
                self.assertEqual(result["answer"], B["answer"])
                self.assertEqual(result["_meta"]["route"], "backup")
                self.assertEqual(result["_meta"]["model"], "backup")
                self.assertIn("primary_l2_failed", result["_meta"]["reasons"])
                self.assertEqual(len(backup.calls), 1)
                self.assertEqual(backup.calls[0]["model"], "backup")
                self.assertEqual(backup.calls[0]["messages"], MESSAGES)

    def test_healthy_primary_does_not_call_backup(self):
        backup = FakeClient()
        config = RoutingConfig("cheap", "strong", backup_model="backup")
        result = ModelRouter(FakeClient(A, A), config, backup_client=backup).solve(
            MESSAGES, "普通题", "single",
        )
        self.assertEqual(result["_meta"]["route"], "verified_l1")
        self.assertEqual(backup.calls, [])

    def test_failed_backup_does_not_loop_or_accept_invalid_answer(self):
        for failure in (TimeoutError("backup down"), "invalid JSON"):
            with self.subTest(failure=failure):
                backup = FakeClient(failure)
                config = RoutingConfig("cheap", "strong", backup_model="backup")
                with self.assertRaises((TimeoutError, InvalidAnswer)):
                    ModelRouter(FakeClient(TimeoutError()), config, backup_client=backup).solve(
                        MESSAGES, "以下不正确的是", "single",
                    )
                self.assertEqual(len(backup.calls), 1)

    def test_backup_settings_are_read_from_environment(self):
        config = RoutingConfig.from_env({
            "OPENAI_BACKUP_MODEL": " backup ",
            "OPENAI_BACKUP_VISION_MODELS": " backup, other ",
        })
        self.assertEqual(config.backup_model, "backup")
        self.assertEqual(config.backup_vision_models, frozenset({"backup", "other"}))

    def test_backup_uses_its_own_vision_capabilities(self):
        for supports_vision in (False, True):
            with self.subTest(supports_vision=supports_vision):
                backup = FakeClient(A)
                config = RoutingConfig(
                    "cheap", "shared", vision_models=frozenset({"shared"}),
                    backup_model="shared",
                    backup_vision_models=frozenset({"shared"}) if supports_vision else frozenset(),
                )
                ModelRouter(FakeClient(TimeoutError()), config, backup_client=backup).solve(
                    MESSAGES, "以下不正确的是", "single",
                    images={"[Image 1]": ImageReference("https://example.com/a.png",
                                                      "data:image/png;base64,AA==")},
                )
                content = backup.calls[0]["messages"][-1]["content"]
                self.assertEqual(isinstance(content, list), supports_vision)

    def test_jev_direct_l2_failure_also_uses_backup(self):
        jev = FakeJevRouter(L0Result("A", "direct_l2", 0.5, 1.0, "jev-test", 0.99))
        config = RoutingConfig("cheap", "strong", backup_model="backup")
        result = ModelRouter(FakeClient(TimeoutError()), config,
                             jev_router=jev, backup_client=FakeClient(B)).solve(
            MESSAGES, "普通题", "single", options="A. 甲\nB. 乙",
        )
        self.assertEqual(result["_meta"]["route"], "backup")


class RoutingTests(unittest.TestCase):
    def solve(self, *responses, kind="single", title="普通题", config=None,
              options="甲\n乙\n丙\n丁"):
        client = FakeClient(*responses)
        router = ModelRouter(client, config or RoutingConfig("cheap", "strong"))
        result = router.solve(MESSAGES, title, kind, options=options)
        return result, client.calls

    def test_agreement_keeps_first_answer_and_analysis(self):
        result, calls = self.solve(A, {"answer": "A", "analysis": "另一解析"})
        self.assertEqual(result["answer"], "A")
        self.assertEqual(result["analysis"], "原解析")
        self.assertEqual([c["model"] for c in calls], ["cheap", "cheap"])
        self.assertFalse(result["_meta"]["upgraded"])

    def test_explicit_temperature_rejection_retries_without_temperature(self):
        failure = provider_error(400, "UnsupportedParamsError: model doesn't support temperature=0.3 while reasoning is active")
        client = FakeClient(failure, B)
        result = ModelRouter(client, RoutingConfig("cheap", "strong")).solve(
            MESSAGES, "以下不正确的是", "single",
        )
        self.assertEqual(result["answer"], "B")
        self.assertEqual([c["model"] for c in client.calls], ["strong", "strong"])
        self.assertEqual(client.calls[0]["temperature"], 0.3)
        self.assertNotIn("temperature", client.calls[1])
        self.assertEqual([a["status"] for a in result["_meta"]["attempts"]], ["failed", "valid"])

    def test_unrelated_errors_do_not_trigger_parameter_retry(self):
        for failure in (provider_error(400, "response_format is unsupported", "response_format"),
                        provider_error(401, "temperature unsupported")):
            with self.subTest(failure=failure):
                client = FakeClient(failure)
                with self.assertRaises(APIStatusError):
                    ModelRouter(client, RoutingConfig("cheap", "strong")).solve(
                        MESSAGES, "以下不正确的是", "single",
                    )
                self.assertEqual(len(client.calls), 1)

    def test_parameter_retry_is_bounded_before_backup(self):
        failure = provider_error(400, "Unsupported parameter: temperature")
        client = FakeClient(failure, failure)
        backup = FakeClient(B)
        config = RoutingConfig("cheap", "strong", backup_model="backup")
        result = ModelRouter(client, config, backup_client=backup).solve(
            MESSAGES, "以下不正确的是", "single",
        )
        self.assertEqual(result["_meta"]["route"], "backup")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(backup.calls), 1)

    def test_disagreement_uses_strong_model_without_rewriting_payload(self):
        final = {"answer": "  B#D  ", "analysis": "保留原始输出"}
        result, calls = self.solve(A, B, final)
        self.assertEqual(result["answer"], final["answer"])
        self.assertEqual(result["analysis"], final["analysis"])
        self.assertEqual([c["model"] for c in calls], ["cheap", "cheap", "strong"])
        self.assertTrue(result["_meta"]["upgraded"])
        self.assertIn("answer_disagreement", result["_meta"]["reasons"])
        for call in calls:
            self.assertEqual(call["messages"], MESSAGES)
            self.assertEqual(call["temperature"], 0.3)
            self.assertEqual(call["response_format"], {"type": "json_object"})
            self.assertNotIn("logprobs", call)

    def test_high_risk_types_go_directly_to_strong_model(self):
        for kind in ("unknown",):
            with self.subTest(kind=kind):
                result, calls = self.solve(B, kind=kind)
                self.assertEqual(result["answer"], "B")
                self.assertEqual([c["model"] for c in calls], ["strong"])
                self.assertEqual(result["_meta"]["route"], "direct_l2")
                self.assertFalse(result["_meta"]["upgraded"])

    def test_static_risk_goes_directly_to_strong_model(self):
        for title in ("以下不正确的是", "选择最恰当的表述", "根据上述材料回答", "题" * 151):
            with self.subTest(title=title):
                _, calls = self.solve(B, title=title)
                self.assertEqual([c["model"] for c in calls], ["strong"])

    def test_completion_and_judgement_are_verified(self):
        for kind, answer in (("completion", "标准术语"), ("judgement", "正确")):
            with self.subTest(kind=kind):
                item = {"answer": answer, "analysis": "说明"}
                result, calls = self.solve(item, item, kind=kind)
                self.assertEqual(result["answer"], answer)
                self.assertEqual(len(calls), 2)

    def test_bad_l1_json_escalates(self):
        for bad in ("invalid", "[]", '{"answer": []}', '{"answer":"","analysis":"x"}',
                    '{"answer":"A","analysis":"x","confidence":0.99}'):
            with self.subTest(bad=bad):
                result, calls = self.solve(bad, B)
                self.assertEqual(result["answer"], "B")
                self.assertEqual([c["model"] for c in calls], ["cheap", "strong"])

    def test_provider_trailing_ellipsis_is_supported(self):
        payload = json.dumps(A, ensure_ascii=False)
        for suffix in ("...", "…"):
            with self.subTest(suffix=suffix):
                result, calls = self.solve(payload + suffix, payload + suffix)
                self.assertEqual(result["answer"], "A")
                self.assertEqual([c["model"] for c in calls], ["cheap", "cheap"])

    def test_arbitrary_text_after_json_is_rejected(self):
        payload = json.dumps(A, ensure_ascii=False)
        result, calls = self.solve(payload + " explanation", B)
        self.assertEqual(result["answer"], "B")
        self.assertEqual([c["model"] for c in calls], ["cheap", "strong"])

    def test_invalid_verifier_escalates(self):
        result, calls = self.solve(A, "invalid", B)
        self.assertEqual(result["answer"], "B")
        self.assertEqual(len(calls), 3)

    def test_invalid_strong_model_fails_without_returning_risky_l1(self):
        with self.assertRaises(InvalidAnswer):
            self.solve(A, B, "invalid")

    def test_provider_failures_in_either_l1_stage_escalate(self):
        request = SimpleNamespace()
        response = SimpleNamespace(request=request, status_code=503, headers={})
        failures = (
            TimeoutError("private-timeout-detail"),
            ConnectionError("private-connection-detail"),
            APITimeoutError(request),
            APIConnectionError(request=request),
            APIStatusError("private-provider-detail", response=response, body=None),
            OpenAIError("private-sdk-detail"),
        )
        for prefix, reason in (((), "l1_call_failed"), ((A,), "verifier_call_failed")):
            for failure in failures:
                with self.subTest(stage=reason, error=type(failure).__name__):
                    result, calls = self.solve(*prefix, failure, B)
                    self.assertEqual(result["answer"], B["answer"])
                    self.assertEqual(result["analysis"], B["analysis"])
                    self.assertEqual([c["model"] for c in calls],
                                     ["cheap"] * (len(prefix) + 1) + ["strong"])
                    self.assertTrue(all(c["messages"] == MESSAGES for c in calls))
                    self.assertEqual(result["_meta"]["route"], "escalated_l2")
                    self.assertTrue(result["_meta"]["upgraded"])
                    self.assertIn(reason, result["_meta"]["reasons"])
                    attempt = result["_meta"]["attempts"][-2]
                    self.assertEqual({key: attempt[key] for key in ("model", "status", "error_type")}, {
                        "model": "cheap", "status": "failed",
                        "error_type": type(failure).__name__,
                    })
                    self.assertNotIn("private-", json.dumps(result))

    def test_l2_failure_after_l1_failure_propagates_without_more_calls(self):
        failure = TimeoutError("strong model unavailable")
        client = FakeClient(ConnectionError("cheap model unavailable"), failure)
        with self.assertRaises(TimeoutError) as raised:
            ModelRouter(client, RoutingConfig("cheap", "strong")).solve(MESSAGES, "题目", "single")
        self.assertIs(raised.exception, failure)
        self.assertEqual([c["model"] for c in client.calls], ["cheap", "strong"])

    def test_direct_l2_failure_does_not_call_l1(self):
        failure = TimeoutError("strong model unavailable")
        client = FakeClient(failure)
        with self.assertRaises(TimeoutError) as raised:
            ModelRouter(client, RoutingConfig("cheap", "strong")).solve(
                MESSAGES, "以下不正确的是", "single",
            )
        self.assertIs(raised.exception, failure)
        self.assertEqual([c["model"] for c in client.calls], ["strong"])

    def test_programming_errors_are_not_treated_as_provider_failures(self):
        client = FakeClient(TypeError("incorrect client usage"), B)
        with self.assertRaises(TypeError):
            ModelRouter(client, RoutingConfig("cheap", "strong")).solve(MESSAGES, "题目", "single")
        self.assertEqual([c["model"] for c in client.calls], ["cheap"])

    def test_legacy_think_and_fence_wrapping_is_supported(self):
        wrapped = '<think>内部文本</think>\n```json\n{"answer":"A","analysis":"解析"}\n```'
        result, _ = self.solve(wrapped, A)
        self.assertEqual(result["answer"], "A")

    def test_same_model_cannot_claim_upgrade(self):
        result, _ = self.solve(A, B, B, config=RoutingConfig("same", "same"))
        self.assertFalse(result["_meta"]["upgraded"])
        self.assertIn("same_model_fallback", result["_meta"]["reasons"])

    def test_environment_aliases_and_explicit_l2(self):
        config = RoutingConfig.from_env({"OPENAI_MODEL_MAIN": "existing", "OPENAI_MODEL_L2": "strong"})
        self.assertEqual((config.l1_model, config.l2_model), ("existing", "strong"))
        config = RoutingConfig.from_env({"OPENAI_MODEL": "legacy"})
        self.assertEqual((config.l1_model, config.l2_model), ("legacy", "legacy"))

    def test_multiple_agreement_ignores_order_but_preserves_original(self):
        first = {"answer": " C # A ", "analysis": "原解析"}
        second = {"answer": "A#C", "analysis": "复核解析"}
        result, calls = self.solve(first, second, kind="multiple")
        self.assertEqual(result["answer"], first["answer"])
        self.assertEqual(result["analysis"], first["analysis"])
        self.assertEqual([c["model"] for c in calls], ["cheap", "cheap"])

    def test_multiple_disagreement_escalates_with_original_prompt(self):
        first = {"answer": "A#C", "analysis": "初答"}
        second = {"answer": "A#D", "analysis": "复核"}
        final = {"answer": "A#C#D", "analysis": "终答"}
        result, calls = self.solve(first, second, final, kind="multiple")
        self.assertEqual(result["answer"], "A#C#D")
        self.assertTrue(result["_meta"]["upgraded"])
        self.assertEqual([c["model"] for c in calls], ["cheap", "cheap", "strong"])
        self.assertTrue(all(c["messages"] == MESSAGES for c in calls))

    def test_multiple_invalid_selection_escalates(self):
        final = {"answer": "A#C", "analysis": "终答"}
        for answer in ("A#A", "A##C", "A#Z", "A", "答案是A和C", "甲#不存在"):
            with self.subTest(answer=answer):
                result, calls = self.solve({"answer": answer, "analysis": "说明"},
                                           final, kind="multiple")
                self.assertEqual(result["answer"], "A#C")
                self.assertEqual([c["model"] for c in calls], ["cheap", "strong"])

    def test_multiple_invalid_verifier_escalates(self):
        valid = {"answer": "A#C", "analysis": "说明"}
        result, calls = self.solve(valid, {"answer": "Z#C", "analysis": "说明"},
                                   valid, kind="multiple")
        self.assertEqual(result["_meta"]["route"], "escalated_l2")
        self.assertEqual(len(calls), 3)

    def test_multiple_text_answers_match_by_option_identity(self):
        first = {"answer": "甲#丙", "analysis": "原解析"}
        result, calls = self.solve(first, {"answer": "C#A", "analysis": "说明"},
                                   kind="multiple", options="A. 甲\nB. 乙\nC. 丙")
        self.assertEqual(result["answer"], "甲#丙")
        self.assertEqual(len(calls), 2)

    def test_multiple_static_risk_and_many_options_skip_l1(self):
        valid = {"answer": "A#C", "analysis": "说明"}
        for title, options in (("以下不正确的是", "甲\n乙\n丙"),
                               ("普通题", "甲\n乙\n丙\n丁\n戊\n己")):
            result, calls = self.solve(valid, kind="multiple", title=title, options=options)
            self.assertEqual(result["_meta"]["route"], "direct_l2")
            self.assertEqual([c["model"] for c in calls], ["strong"])

    def test_multiple_bad_l2_selection_is_not_accepted(self):
        with self.assertRaises(InvalidAnswer):
            self.solve({"answer": "A#Z", "analysis": "说明"}, kind="multiple",
                       title="以下不正确的是")

    def test_jev_high_confidence_returns_original_option_without_openai_call(self):
        client = FakeClient()
        jev = FakeJevRouter(L0Result(
            "B. 乙", "l0_direct", 0.990, 0.990, "jev-test", 0.990,
        ))
        result = ModelRouter(
            client, RoutingConfig("cheap", "strong"), jev_router=jev,
        ).solve(MESSAGES, "普通题", "single", options="A. 甲\nB. 乙")
        self.assertEqual(result["answer"], "B. 乙")
        self.assertEqual(result["analysis"], "Jev L0 高置信度直答（99.0%+）")
        self.assertEqual(result["_meta"]["route"], "jev_l0_direct")
        self.assertEqual(result["_meta"]["confidence"], 0.990)
        self.assertEqual(client.calls, [])

    def test_jev_low_confidence_or_verify_route_uses_existing_l1_flow(self):
        for route, answer_probability, route_probability in (
            ("l0_direct", 0.989, 1.0),
            ("l0_direct", 1.0, 0.989),
            ("verify_l1", 1.0, 1.0),
        ):
            with self.subTest(route=route, ap=answer_probability, rp=route_probability):
                client = FakeClient(A, A)
                jev = FakeJevRouter(L0Result(
                    "A. 甲", route, answer_probability, route_probability,
                    "jev-test", 0.990,
                ))
                result = ModelRouter(
                    client, RoutingConfig("cheap", "strong"), jev_router=jev,
                ).solve(MESSAGES, "普通题", "single", options="A. 甲\nB. 乙")
                self.assertEqual(result["_meta"]["route"], "verified_l1")
                self.assertEqual([call["model"] for call in client.calls], ["cheap", "cheap"])

    def test_jev_direct_l2_skips_l1(self):
        client = FakeClient(B)
        jev = FakeJevRouter(L0Result(
            "A. 甲", "direct_l2", 0.6, 0.6, "jev-test", 0.990,
        ))
        result = ModelRouter(
            client, RoutingConfig("cheap", "strong"), jev_router=jev,
        ).solve(MESSAGES, "普通题", "single", options="A. 甲\nB. 乙")
        self.assertEqual(result["_meta"]["route"], "direct_l2")
        self.assertEqual([call["model"] for call in client.calls], ["strong"])

    def test_static_risk_images_and_other_types_skip_jev(self):
        cases = (
            {"title": "以下不正确的是", "kind": "single", "images": None},
            {"title": "普通题", "kind": "single", "images": {"[Image 1]": object()}},
            {"title": "普通题", "kind": "multiple", "images": None},
            {"title": "普通题", "kind": "completion", "images": None},
        )
        for case in cases:
            with self.subTest(case=case):
                answer = ({"answer": "A#B", "analysis": "说明"}
                          if case["kind"] == "multiple" else B)
                client = FakeClient(answer, answer)
                jev = FakeJevRouter(None)
                ModelRouter(
                    client, RoutingConfig("cheap", "strong"), jev_router=jev,
                ).solve(MESSAGES, case["title"], case["kind"],
                        options="A. 甲\nB. 乙", images=case["images"])
                self.assertEqual(jev.calls, [])


if __name__ == "__main__":
    unittest.main()
