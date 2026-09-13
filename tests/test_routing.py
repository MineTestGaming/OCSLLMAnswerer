import json
import unittest
from types import SimpleNamespace

from model_router import ModelRouter, RoutingConfig, InvalidAnswer


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

    def test_invalid_verifier_escalates(self):
        result, calls = self.solve(A, "invalid", B)
        self.assertEqual(result["answer"], "B")
        self.assertEqual(len(calls), 3)

    def test_invalid_strong_model_fails_without_returning_risky_l1(self):
        with self.assertRaises(InvalidAnswer):
            self.solve(A, B, "invalid")

    def test_infrastructure_errors_do_not_trigger_extra_paid_calls(self):
        client = FakeClient(TimeoutError("timeout"), A)
        with self.assertRaises(TimeoutError):
            ModelRouter(client, RoutingConfig("cheap", "strong")).solve(MESSAGES, "题目", "single")
        self.assertEqual(len(client.calls), 1)

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


if __name__ == "__main__":
    unittest.main()
