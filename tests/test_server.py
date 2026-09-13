import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from test_routing import A, B, FakeClient

# Prevent loading the developer's .env or using a real API key in tests.
with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True), \
        patch("dotenv.load_dotenv"), patch("openai.OpenAI", return_value=FakeClient()):
    import main


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "OPENAI_MODEL_L1": "cheap", "OPENAI_MODEL_L2": "strong"
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_original_messages_are_preserved_for_every_type_and_stage(self):
        fixtures = json.loads(Path(__file__).with_name("original_messages.json").read_text(encoding="utf-8"))
        for fixture in fixtures:
            with self.subTest(kind=fixture["type"]):
                client = (FakeClient({"answer": "A#B", "analysis": "说明"},
                                     {"answer": "B#A", "analysis": "复核"})
                          if fixture["type"] == "multiple" else FakeClient(A, B, B))
                with patch.object(main, "client", client):
                    result = main.get_chatgpt_answer("测试题目", "A. 甲\nB. 乙", fixture["type"])
                self.assertIn("_meta", result)
                for call in client.calls:
                    self.assertEqual(call["messages"], fixture["messages"])

    def test_search_keeps_json_contract_and_exact_answer(self):
        answer = {"answer": " B#D ", "analysis": "原解析"}
        with patch.object(main, "client", FakeClient(answer, answer)):
            response = main.app.test_client().post("/search", json={
                "title": "题目", "options": "甲\n乙\n丙\n丁", "type": "multiple"
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"code": 1, "question": "题目", **answer})

    def test_provider_failure_is_not_reported_as_success(self):
        with patch.object(main, "client", FakeClient(TimeoutError("private-provider-detail"))):
            response = main.app.test_client().post("/search", json={"title": "题目", "type": "single"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["code"], 0)
        self.assertNotIn("private-provider-detail", response.get_data(as_text=True))

    def test_malformed_request_returns_400_without_model_call(self):
        for payload in ([], {"title": 1}, {"title": "题", "options": []},
                        {"title": "题", "type": []}, {"title": "   "}):
            with self.subTest(payload=payload), patch.object(main, "client", FakeClient()):
                response = main.app.test_client().post("/search", json=payload)
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
