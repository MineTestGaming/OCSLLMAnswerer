import io
import os
import unittest
from email.message import Message
from unittest.mock import patch

from test_server import main
from test_routing import A, B, FakeClient


URL = "http://p.ananas.chaoxing.com/star3/origin/circuit.PNG"
OPTION = "https://example.com/option.png?token=abc&size=2"


def image_response(body=b"\x89PNG\r\n\x1a\nimage", content_type="image/png"):
    response = io.BytesIO(body)
    response.headers = Message()
    response.headers["Content-Type"] = content_type
    response.status = 200
    return response


class ImageTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "OPENAI_MODEL_L1": "cheap", "OPENAI_MODEL_L2": "strong",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def post(self, client, title=URL, options=OPTION + "\n" + URL, kind="single"):
        with patch.object(main, "client", client):
            return main.app.test_client().post("/search", json={
                "title": title, "options": options, "type": kind,
            })

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_text_model_gets_ids_without_urls_and_preserves_response_question(self, fetch):
        client = FakeClient(A, A)
        response = self.post(client, title="电路 " + URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["question"], "电路 " + URL)
        for call in client.calls:
            content = call["messages"][1]["content"]
            self.assertIsInstance(content, str)
            self.assertIn("[Image 1]", content)
            self.assertIn("[Image 2]", content)
            self.assertNotIn("http", content)

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_vision_is_selected_per_model_during_escalation(self, fetch):
        with patch.dict(os.environ, {"OPENAI_VISION_MODELS": " strong, another "}):
            client = FakeClient(A, B, B)
            response = self.post(client)
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(client.calls[0]["messages"][1]["content"], str)
        self.assertIsInstance(client.calls[1]["messages"][1]["content"], str)
        parts = client.calls[2]["messages"][1]["content"]
        self.assertIsInstance(parts, list)
        self.assertEqual(parts[1:], [
            {"type": "text", "text": "[Image 1]"},
            {"type": "image_url", "image_url": {"url": URL}},
            {"type": "text", "text": "[Image 2]"},
            {"type": "image_url", "image_url": {"url": OPTION}},
        ])
        self.assertEqual(fetch.call_count, 2)

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_duplicate_images_reuse_id_and_are_fetched_once(self, fetch):
        client = FakeClient(A, A)
        response = self.post(client, options=URL)
        self.assertEqual(response.status_code, 200)
        content = client.calls[0]["messages"][1]["content"]
        self.assertIn("[Image 1]", content)
        self.assertNotIn("[Image 2]", content)
        self.assertEqual(fetch.call_count, 1)

    @patch("urllib.request.urlopen", side_effect=TimeoutError("private detail"))
    def test_inaccessible_image_returns_501_without_model_calls(self, fetch):
        client = FakeClient(A, A)
        response = self.post(client)
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json["code"], 0)
        self.assertNotIn("private detail", response.get_data(as_text=True))
        self.assertEqual(client.calls, [])

    @patch("urllib.request.urlopen", side_effect=TimeoutError("private detail"))
    def test_501_includes_original_question_options_and_type(self, fetch):
        title = "电路题 " + URL
        options = "  " + OPTION + "\n\n" + URL + "  "
        response = self.post(FakeClient(), title=title, options=options, kind="multiple")
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json["question"], title)
        self.assertEqual(response.json["options"], options)
        self.assertEqual(response.json["type"], "multiple")

    def test_empty_or_html_response_returns_501(self):
        for body, mime in ((b"", "image/png"), (b"login", "text/html")):
            with self.subTest(mime=mime), patch("urllib.request.urlopen",
                    side_effect=lambda *a, **k: image_response(body, mime)):
                client = FakeClient(A, A)
                self.assertEqual(self.post(client).status_code, 501)
                self.assertEqual(client.calls, [])

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_image_ids_are_valid_multiple_option_answers(self, fetch):
        answer = {"answer": "[Image 1]#[Image 2]", "analysis": "说明"}
        client = FakeClient(answer, answer)
        response = self.post(client, title="选择", options=URL + "\n" + OPTION + "\n其他", kind="multiple")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["answer"], URL + "#" + OPTION)

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_all_image_options_are_lettered_independently_of_image_ids(self, fetch):
        for options in (OPTION + "\n" + URL, "1. " + OPTION + "\n2. " + URL):
            with self.subTest(options=options):
                client = FakeClient(B, B)
                response = self.post(client, options=options)
                self.assertEqual(response.json["answer"], "B")
                prompt = client.calls[0]["messages"][1]["content"]
                self.assertIn("A. [Image 2]\nB. [Image 1]", prompt)

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_numeric_or_image_id_single_answers_escalate_to_letter_answer(self, fetch):
        for invalid in ("1", "[Image 2]", "Z"):
            with self.subTest(answer=invalid):
                client = FakeClient({"answer": invalid, "analysis": "说明"}, B)
                response = self.post(client, options=OPTION + "\n" + URL)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["answer"], "B")
                self.assertEqual([call["model"] for call in client.calls], ["cheap", "strong"])

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_all_image_multiple_answers_require_letters_and_hash_separator(self, fetch):
        valid = {"answer": "A#B", "analysis": "说明"}
        for invalid in ("1#2", "[Image 1]#[Image 2]", "A,B"):
            with self.subTest(answer=invalid):
                client = FakeClient({"answer": invalid, "analysis": "说明"}, valid)
                response = self.post(client, title="选择", options=URL + "\n" + OPTION, kind="multiple")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["answer"], "A#B")

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_invalid_image_answer_from_l2_returns_failure(self, fetch):
        client = FakeClient({"answer": "1", "analysis": "说明"})
        response = self.post(client, title="不正确的是 " + URL, options=OPTION)
        self.assertEqual(response.status_code, 502)

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_completion_with_image_options_still_accepts_free_text(self, fetch):
        answer = {"answer": "逻辑与", "analysis": "说明"}
        client = FakeClient(answer, answer)
        self.assertEqual(self.post(client, kind="completion").json["answer"], "逻辑与")

    @patch("urllib.request.urlopen", side_effect=lambda *a, **k: image_response())
    def test_vision_l1_and_text_l2_do_not_share_image_parts(self, fetch):
        with patch.dict(os.environ, {"OPENAI_VISION_MODELS": "cheap"}):
            client = FakeClient(A, B, B)
            response = self.post(client)
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(client.calls[0]["messages"][1]["content"], list)
        self.assertIsInstance(client.calls[1]["messages"][1]["content"], list)
        self.assertIsInstance(client.calls[2]["messages"][1]["content"], str)

    @patch("urllib.request.urlopen", side_effect=[image_response(), OSError("unavailable")])
    def test_inaccessible_option_also_stops_request(self, fetch):
        client = FakeClient(A, A)
        self.assertEqual(self.post(client).status_code, 501)
        self.assertEqual(client.calls, [])

    @patch("urllib.request.urlopen")
    def test_normal_web_link_is_not_treated_as_image(self, fetch):
        client = FakeClient(A, A)
        response = self.post(client, title="参考 https://example.com/article", options="甲\n乙")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_count, 0)


if __name__ == "__main__":
    unittest.main()
