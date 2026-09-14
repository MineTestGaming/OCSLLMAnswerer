import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
import ssl

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

    def test_ssl_disabled_by_default(self):
        self.assertIsNone(main.get_ssl_context({}))
        self.assertIsNone(main.get_ssl_context({"SSL_ENABLED": "false"}))

    def test_ssl_loads_configured_certificate_and_key(self):
        with patch("ssl.SSLContext") as context_class:
            context = main.get_ssl_context({
                "SSL_ENABLED": "True", "SSL_CERT_FILE": "/certs/server.crt",
                "SSL_KEY_FILE": "/certs/server.key",
            })
        self.assertIs(context, context_class.return_value)
        context_class.assert_called_once_with(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain.assert_called_once_with("/certs/server.crt", "/certs/server.key")

    def test_ssl_missing_settings_or_invalid_switch_fail(self):
        for env in ({"SSL_ENABLED": "true"},
                    {"SSL_ENABLED": "true", "SSL_CERT_FILE": "server.crt"},
                    {"SSL_ENABLED": "typo"}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                main.get_ssl_context(env)

    def test_ssl_certificate_failure_does_not_fall_back_to_http(self):
        with patch("ssl.SSLContext") as context_class:
            context_class.return_value.load_cert_chain.side_effect = ssl.SSLError("bad certificate")
            with self.assertRaises(ssl.SSLError):
                main.get_ssl_context({"SSL_ENABLED": "1", "SSL_CERT_FILE": "bad.crt",
                                      "SSL_KEY_FILE": "bad.key"})

    def test_server_uses_ssl_context_and_logs_https(self):
        context = object()
        with patch.object(main, "get_ssl_context", return_value=context), \
                patch.object(main, "patch_connect"), patch.object(main, "log_info") as log, \
                patch.object(main.app, "run") as run:
            main.run_server()
        self.assertIs(run.call_args.kwargs["ssl_context"], context)
        self.assertIn("https://", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
