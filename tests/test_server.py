import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import ssl

from test_routing import A, B, FakeClient, provider_error
from jev_router import L0Result
from test_routing import FakeJevRouter

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

    def test_backup_client_uses_independent_credentials_and_timeout(self):
        with patch.object(main, "OpenAI") as factory:
            main.create_backup_client({
                "OPENAI_BACKUP_API_KEY": "backup-key",
                "OPENAI_BACKUP_BASE_URL": "https://backup.example/v1",
                "OPENAI_BACKUP_MODEL": "backup-model",
                "OPENAI_TIMEOUT_SECONDS": "30",
            })
        factory.assert_called_once_with(api_key="backup-key",
                                        base_url="https://backup.example/v1", timeout=30.0)
        self.assertIsNone(main.create_backup_client({}))
        with self.assertRaisesRegex(ValueError, "OPENAI_BACKUP_BASE_URL"):
            main.create_backup_client({"OPENAI_BACKUP_MODEL": "backup-model"})

    def test_search_uses_backup_after_primary_failure(self):
        for backup_result, expected_status in ((B, 200), ("invalid JSON", 502)):
            with self.subTest(backup_result=backup_result), \
                    patch.dict(os.environ, {"OPENAI_BACKUP_MODEL": "backup"}), \
                    patch.object(main, "client", FakeClient(TimeoutError())), \
                    patch.object(main, "backup_client", FakeClient(backup_result)):
                response = main.app.test_client().post("/search", json={
                    "title": "以下不正确的是", "type": "single",
                })
            self.assertEqual(response.status_code, expected_status)
            self.assertEqual(response.json["code"], int(expected_status == 200))
            if expected_status == 200:
                self.assertEqual(response.json["answer"], B["answer"])
            else:
                self.assertEqual(response.json["model"], "backup")

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

    def test_search_jev_direct_keeps_json_contract(self):
        jev = FakeJevRouter(L0Result(
            "B. 乙", "l0_direct", 1.0, 1.0, "jev-test", 0.990,
        ))
        with patch.object(main, "jev_router", jev), \
                patch.object(main, "client", FakeClient()):
            response = main.app.test_client().post("/search", json={
                "title": "1 + 1 = ?", "options": "A. 1\nB. 乙", "type": "single",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {
            "code": 1,
            "question": "1 + 1 = ?",
            "answer": "B. 乙",
            "analysis": "Jev L0 高置信度直答（99.0%+）",
        })

    def test_search_returns_l2_answer_after_either_l1_stage_fails(self):
        for prefix in ((), (A,)):
            with self.subTest(verifier=bool(prefix)):
                client = FakeClient(*prefix, TimeoutError("private-provider-detail"), B)
                with patch.object(main, "client", client):
                    response = main.app.test_client().post("/search", json={
                        "title": "题目", "type": "single",
                    })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json, {"code": 1, "question": "题目", **B})
                self.assertEqual([c["model"] for c in client.calls],
                                 ["cheap"] * (len(prefix) + 1) + ["strong"])

    def test_provider_failure_is_not_reported_as_success(self):
        client = FakeClient(TimeoutError("private-l1-detail"),
                            TimeoutError("private-provider-detail"))
        with patch.object(main, "client", client):
            response = main.app.test_client().post("/search", json={"title": "题目", "type": "single"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["code"], 0)
        self.assertNotIn("private-provider-detail", response.get_data(as_text=True))
        self.assertIsNone(response.json["raw_output"])
        self.assertEqual(response.json["error_type"], "TimeoutError")
        self.assertEqual([c["model"] for c in client.calls], ["cheap", "strong"])
        self.assertEqual(response.json["model"], "strong")
        self.assertEqual([a["model"] for a in response.json["attempts"]], ["cheap", "strong"])

    def test_failure_reports_jev_primary_and_backup_attempts_without_secrets(self):
        jev = FakeJevRouter(L0Result("A", "verify_l1", 0.5, 1.0, "jev-test", 0.99))
        with patch.dict(os.environ, {"OPENAI_BACKUP_MODEL": "backup"}), \
                patch.object(main, "jev_router", jev), \
                patch.object(main, "client", FakeClient(TimeoutError(), TimeoutError())), \
                patch.object(main, "backup_client", FakeClient(provider_error(402, "private-secret"))), \
                patch.object(main, "log_error") as log:
            response = main.app.test_client().post("/search", json={
                "title": "普通题", "options": "A. 甲\nB. 乙", "type": "single",
            })
        self.assertEqual(response.status_code, 502)
        data = response.json
        self.assertEqual([a["model"] for a in data["attempts"]],
                         ["jev-test", "cheap", "strong", "backup"])
        self.assertEqual(data["http_status"], 402)
        self.assertEqual(data["provider"], "backup")
        self.assertIn("余额不足", data["reason"])
        logged = " ".join(str(call) for call in log.call_args_list)
        for model in ("jev-test", "cheap", "strong", "backup"):
            self.assertIn(model, logged)
        self.assertNotIn("private-", logged + response.get_data(as_text=True))

    def test_502_returns_final_model_output_before_any_cleanup(self):
        raw = '  <think>原始思考</think>\n```json\n{"answer":"A"}\n```  '
        with patch.object(main, "client", FakeClient("first invalid", raw)):
            response = main.app.test_client().post("/search", json={
                "title": "题目", "options": "甲\n乙", "type": "single",
            })
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["code"], 0)
        self.assertEqual(response.json["raw_output"], raw)
        self.assertEqual(response.json["model"], "strong")
        self.assertEqual(response.json["finish_reason"], "stop")
        self.assertEqual(response.json["reason"], "模型输出必须且只能包含 answer 和 analysis")
        self.assertEqual([a["model"] for a in response.json["attempts"]], ["cheap", "strong"])

    def test_502_preserves_truncated_or_refused_output(self):
        for content, finish, refusal in (("{\"answer\":", "length", None),
                                         (None, "stop", "拒绝内容")):
            with self.subTest(finish=finish):
                client = FakeClient()
                reply = SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=content, refusal=refusal), finish_reason=finish,
                )])
                with patch.object(main, "client", client), patch.object(client, "create", return_value=reply):
                    response = main.app.test_client().post("/search", json={
                        "title": "不正确的是", "type": "single",
                    })
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json["raw_output"], content)
                self.assertEqual(response.json["finish_reason"], finish)
                self.assertEqual(response.json["refusal"], refusal)

    def test_502_without_choices_has_no_raw_output(self):
        client = FakeClient()
        with patch.object(main, "client", client), \
                patch.object(client, "create", return_value=SimpleNamespace(choices=[])):
            response = main.app.test_client().post("/search", json={
                "title": "不正确的是", "type": "single",
            })
        self.assertEqual(response.status_code, 502)
        self.assertIsNone(response.json["raw_output"])
        self.assertEqual(response.json["reason"], "模型未返回候选答案")

    def test_malformed_request_returns_400_without_model_call(self):
        for payload in ([], {"title": 1}, {"title": "题", "options": []},
                        {"title": "题", "type": []}, {"title": "   "}):
            with self.subTest(payload=payload), patch.object(main, "client", FakeClient()):
                response = main.app.test_client().post("/search", json=payload)
                self.assertEqual(response.status_code, 400)

    def test_ssl_disabled_by_default(self):
        self.assertIsNone(main.get_ssl_context({}))
        self.assertIsNone(main.get_ssl_context({"SSL_ENABLED": "false"}))

    def test_missing_userscript_returns_404_and_server_remains_available(self):
        with patch.object(main, "send_file", side_effect=FileNotFoundError()):
            client = main.app.test_client()
            response = client.get("/ocs.user.js")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json, {"code": 0, "msg": "OCS 脚本不存在"})
            self.assertEqual(client.get("/").status_code, 200)

    def test_missing_userscript_does_not_prevent_server_startup(self):
        with patch.object(main, "get_ssl_context", return_value=None), \
                patch.object(main.Path, "read_text", side_effect=FileNotFoundError()), \
                patch.object(main, "log_info"), patch.object(main.app, "run") as run:
            main.run_server()
        run.assert_called_once()

    def test_ssl_loads_configured_certificate_and_key(self):
        with patch("ssl.SSLContext") as context_class:
            context = main.get_ssl_context({
                "SSL_ENABLED": "True",
                "SSL_SERVER_CERT_FILE": "/certs/server.crt",
                "SSL_SERVER_KEY_FILE": "/certs/server.key",
            })
        self.assertIs(context, context_class.return_value)
        context_class.assert_called_once_with(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain.assert_called_once_with("/certs/server.crt", "/certs/server.key")

    def test_ssl_missing_settings_or_invalid_switch_fail(self):
        for env in ({"SSL_ENABLED": "true"},
                    {"SSL_ENABLED": "true", "SSL_SERVER_CERT_FILE": "server.crt"},
                    {"SSL_ENABLED": "typo"}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                main.get_ssl_context(env)

    def test_ssl_certificate_failure_does_not_fall_back_to_http(self):
        with patch("ssl.SSLContext") as context_class:
            context_class.return_value.load_cert_chain.side_effect = ssl.SSLError("bad certificate")
            with self.assertRaises(ssl.SSLError):
                main.get_ssl_context({
                    "SSL_ENABLED": "1",
                    "SSL_SERVER_CERT_FILE": "bad.crt",
                    "SSL_SERVER_KEY_FILE": "bad.key",
                })

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
