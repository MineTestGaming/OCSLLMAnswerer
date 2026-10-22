import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from test_server import main
from test_routing import A, FakeClient


class ConsoleLoggingTests(unittest.TestCase):
    def test_debug_flag_defaults_to_release_and_accepts_boolean_values(self):
        self.assertFalse(main.get_debug_flag({}))
        for value in ("true", "1", "YES", " on "):
            self.assertTrue(main.get_debug_flag({"DEBUG": value}))
        for value in ("false", "0", "NO", " off ", ""):
            self.assertFalse(main.get_debug_flag({"DEBUG": value}))
        with self.assertRaisesRegex(ValueError, "DEBUG"):
            main.get_debug_flag({"DEBUG": "typo"})

    def test_debug_prints_full_request_answer_and_errors(self):
        output = io.StringIO()
        with patch.object(main, "DEBUG", True), redirect_stdout(output):
            main.log_request("题干内容", "选项内容", "单选")
            main.log_response("答案内容", "解析内容")
            main.log_error("错误详情")
        for value in ("题干内容", "选项内容", "答案内容", "解析内容", "错误详情"):
            self.assertIn(value, output.getvalue())

    def test_release_only_prints_model_names_and_results_for_success_and_failure(self):
        for replies, status, result_word in (((A, A), 200, "成功"),
                                             ((TimeoutError(), TimeoutError()), 502, "失败")):
            with self.subTest(status=status):
                output = io.StringIO()
                with patch.object(main, "DEBUG", False), \
                        patch.object(main, "CONSOLE_STREAM", output), \
                        patch.dict(os.environ, {"OPENAI_MODEL_L1": "cheap", "OPENAI_MODEL_L2": "strong"}, clear=True), \
                        patch.object(main, "client", FakeClient(*replies)), \
                        patch.object(main, "jev_router", None), \
                        patch.object(main, "backup_client", None), \
                        redirect_stdout(output), redirect_stderr(output):
                    response = main.app.test_client().post("/search", json={
                        "title": "不能显示的题目", "options": "不能显示的选项", "type": "single",
                    })
                self.assertEqual(response.status_code, status)
                models = ("cheap", "cheap") if status == 200 else ("cheap", "strong")
                self.assertEqual(output.getvalue().splitlines(),
                                 [f"{model}: {result_word}" for model in models])

    def test_release_hides_framework_output_but_preserves_model_summary(self):
        output = io.StringIO()

        def fake_run(**kwargs):
            print("framework banner")
            print("framework access log", file=sys.stderr)
            main.log_model_calls([{"model": "my-model", "status": "valid"}])

        with patch.object(main, "DEBUG", False), patch.object(main, "CONSOLE_STREAM", output), \
                patch.object(main, "get_ssl_context", return_value=None), \
                patch.object(main, "patch_connect"), patch.object(main.app, "run", side_effect=fake_run), \
                redirect_stdout(output), redirect_stderr(output):
            main.run_server()
        self.assertEqual(output.getvalue(), "my-model: 成功\n")

    def test_debug_keeps_foreground_output_without_enabling_flask_debugger(self):
        output = io.StringIO()
        with patch.object(main, "DEBUG", True), \
                patch.object(main, "get_ssl_context", return_value=None), \
                patch.object(main, "patch_connect"), \
                patch.object(main.app, "run", side_effect=lambda **kwargs: print("foreground log")) as run, \
                redirect_stdout(output):
            main.run_server()
        self.assertIn("foreground log", output.getvalue())
        self.assertFalse(run.call_args.kwargs["debug"])


if __name__ == "__main__":
    unittest.main()
