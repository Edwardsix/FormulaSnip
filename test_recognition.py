"""Offline transport/error tests. Only a loopback mock server is contacted."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

import recognition as ocr


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6"
    "G3sAAAAASUVORK5CYII="
)


@contextmanager
def mock_api(result, status=200, raw=None, extra_headers=None):
    records = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            records.append({"path": self.path, "headers": self.headers, "body": json.loads(data)})
            payload = raw if raw is not None else json.dumps(result).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def response(content=r"\frac{a}{b}", finish_reason="stop", refusal=None):
    return {"choices": [{"message": {"content": content, "refusal": refusal}, "finish_reason": finish_reason}]}


class CleaningTests(unittest.TestCase):
    def test_complete_wrappers(self):
        for text in (" $$ x^2 $$ ", r"\[x^2\]", r"\(x^2\)", "$x^2$", "```latex\nx^2\n```", "```tex\n$x^2$\n```", "```\nx^2\n```"):
            with self.subTest(text=text):
                self.assertEqual(ocr.clean_latex(text), "x^2")

    def test_inner_delimiters_and_escaped_dollars_preserved(self):
        for text in ("$a$ + $b$", r"\[a\] \[b\]", r"\text{cost }\$2", r"\begin{cases}a&x<0\\b&x>0\end{cases}"):
            self.assertEqual(ocr.clean_latex(text), text)
        self.assertEqual(ocr.clean_latex(r"$\text{cost }\$2$"), r"\text{cost }\$2")

    def test_empty_and_invalid_output(self):
        for value in ("", "  ", "$$ $$", "NO_FORMULA", None, 12, "x\x00y"):
            with self.subTest(value=value), self.assertRaises(ocr.RecognitionError):
                ocr.clean_latex(value)

    def test_too_long(self):
        with self.assertRaises(ocr.RecognitionError):
            ocr.clean_latex("x" * (ocr.MAX_LATEX_CHARS + 1))


class EndpointTests(unittest.TestCase):
    def test_remote_and_loopback_addresses(self):
        self.assertEqual(ocr.endpoint_url("https://example.com/v1/"), "https://example.com/v1/chat/completions")
        self.assertEqual(ocr.endpoint_url("https://example.com/v1/chat/completions"), "https://example.com/v1/chat/completions")
        for value in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1"):
            self.assertEqual(ocr.endpoint_url(value), value + "/chat/completions")

    def test_rejects_insecure_or_credential_urls(self):
        for value in ("", "example.com/v1", "http://example.com/v1", "https://u:p@example.com/v1", "https://example.com/v1?key=x", "https://example.com/v1#x", "https://example.com/v1?", "https://example.com/a b", "https://example.com\\bad", "https://localhost:99999/v1", "file:///tmp/x", None):
            with self.subTest(value=value), self.assertRaises(ocr.RecognitionError):
                ocr.endpoint_url(value)


class TransportTests(unittest.TestCase):
    def test_openai_wire_and_formula_result(self):
        with mock_api(response("```latex\n\\frac{a}{b}\n```")) as (base, requests):
            actual = ocr.recognize(PNG, {"provider": "openai", "api_base": base, "api_key": "test-only", "model": "visual-test"})
        self.assertEqual(actual, r"\frac{a}{b}")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer test-only")
        body = requests[0]["body"]
        self.assertEqual(body["model"], "visual-test")
        self.assertEqual(body["max_completion_tokens"], 4096)
        image = body["messages"][1]["content"][1]["image_url"]
        self.assertEqual(base64.b64decode(image["url"].split(",", 1)[1]), PNG)
        self.assertEqual(image["detail"], "high")
        self.assertIn("Do not solve", body["messages"][0]["content"])

    def test_loopback_can_omit_api_key(self):
        with mock_api(response("x^2")) as (base, requests):
            self.assertEqual(ocr.recognize(PNG, {"api_base": base}), "x^2")
            self.assertIsNone(requests[0]["headers"].get("Authorization"))

    def test_content_part_array(self):
        with mock_api(response([{"type": "text", "text": "x^2"}])) as (base, _):
            self.assertEqual(ocr.recognize(PNG, {"api_base": base}), "x^2")

    def test_remote_missing_key_never_contacts_network(self):
        with patch.object(ocr, "_post_json") as post:
            with self.assertRaises(ocr.RecognitionError):
                ocr.recognize(PNG, {"api_base": "https://example.com/v1"})
            post.assert_not_called()

    def test_bad_settings_and_input_never_contacts_network(self):
        tests = [(b"bad", {}), (PNG, None), (PNG, {"provider": "bad"}), (PNG, {"api_key": "x\r\nsecret"}), (PNG, {"model": "", "api_base": "http://127.0.0.1/v1"}), (PNG, {"provider": "mathpix", "api_key": "only-key"})]
        with patch.object(ocr, "_post_json") as post:
            for image, config in tests:
                with self.subTest(config=config), self.assertRaises(ocr.RecognitionError):
                    ocr.recognize(image, config)
            post.assert_not_called()

    def test_image_limit(self):
        with patch.object(ocr, "MAX_IMAGE_BYTES", len(PNG) - 1), self.assertRaises(ocr.RecognitionError):
            ocr.recognize(PNG, {})

    def test_mathpix_wire_and_latex(self):
        with mock_api({"latex_styled": r"\sqrt{x}"}) as (base, requests):
            with patch.object(ocr, "MATHPIX_URL", base + "/text"):
                actual = ocr.recognize(PNG, {"provider": "mathpix", "api_key": "test-only", "mathpix_app_id": "test-id"})
        self.assertEqual(actual, r"\sqrt{x}")
        self.assertEqual(requests[0]["headers"]["app_id"], "test-id")
        self.assertEqual(requests[0]["headers"]["app_key"], "test-only")
        self.assertEqual(requests[0]["body"]["metadata"], {"improve_mathpix": False})

    def test_mathpix_multiple_equations_and_mixed_prose(self):
        config = {"provider": "mathpix", "api_key": "test-only", "mathpix_app_id": "test-id"}
        with mock_api({"data": [{"type": "latex", "value": "a=b"}, {"type": "latex", "value": "c=d"}], "text": "Do something"}) as (base, _):
            with patch.object(ocr, "MATHPIX_URL", base + "/text"):
                self.assertEqual(ocr.recognize(PNG, config), "\\begin{gathered}\na=b \\\\\nc=d\n\\end{gathered}")
        with mock_api({"text": "some prose without a formula"}) as (base, _):
            with patch.object(ocr, "MATHPIX_URL", base + "/text"), self.assertRaises(ocr.RecognitionError):
                ocr.recognize(PNG, config)

    def test_mathpix_text_fallback(self):
        with mock_api({"text": r"\[a=b\]"}) as (base, _):
            with patch.object(ocr, "MATHPIX_URL", base + "/text"):
                self.assertEqual(ocr.recognize(PNG, {"provider": "mathpix", "api_key": "test-only", "mathpix_app_id": "test-id"}), "a=b")

    def test_http_errors_are_friendly_and_never_retry(self):
        for status in (400, 401, 403, 404, 413, 429, 500):
            with self.subTest(status=status), mock_api({"error": "do not expose secret"}, status=status) as (base, requests):
                with self.assertRaises(ocr.RecognitionError) as caught:
                    ocr.recognize(PNG, {"api_base": base})
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(requests), 1)

    def test_redirect_is_not_followed(self):
        with mock_api({}, status=307, extra_headers={"Location": "http://127.0.0.1:9/credential-sink"}) as (base, requests):
            with self.assertRaises(ocr.RecognitionError) as caught:
                ocr.recognize(PNG, {"api_base": base, "api_key": "test-only"})
            self.assertIn("跳转", str(caught.exception))
            self.assertEqual(len(requests), 1)

    def test_bad_or_missing_json_content(self):
        for result in ({}, [], {"choices": []}, {"choices": [None]}, {"choices": [{"message": None}]}, response(None), response(""), response("x", "length"), response("x", "content_filter"), response("x", refusal="refused"), {"error": {"message": "secret"}}):
            with self.subTest(result=result), mock_api(result) as (base, _):
                with self.assertRaises(ocr.RecognitionError):
                    ocr.recognize(PNG, {"api_base": base})
        with mock_api({}, raw=b"<html>bad endpoint</html>") as (base, _):
            with self.assertRaises(ocr.RecognitionError):
                ocr.recognize(PNG, {"api_base": base})

    def test_response_limit(self):
        with mock_api(response("x" * 1024)) as (base, _):
            with patch.object(ocr, "MAX_RESPONSE_BYTES", 100), self.assertRaises(ocr.RecognitionError):
                ocr.recognize(PNG, {"api_base": base})

    def test_connection_error(self):
        with patch.object(ocr.request.OpenerDirector, "open", side_effect=ocr.error.URLError("hidden details")):
            with self.assertRaises(ocr.RecognitionError) as caught:
                ocr.recognize(PNG, {"api_base": "http://127.0.0.1:9/v1"})
            self.assertIn("无法连接", str(caught.exception))
            self.assertNotIn("hidden details", str(caught.exception))

    def test_timeout(self):
        with patch.object(ocr.request.OpenerDirector, "open", side_effect=TimeoutError):
            with self.assertRaises(ocr.RecognitionError) as caught:
                ocr.recognize(PNG, {"api_base": "http://127.0.0.1:9/v1"})
            self.assertIn("超时", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
