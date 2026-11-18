"""Alibaba Cloud Qwen provider: mocked HTTP only. No test can reach the network or needs a key."""
import base64
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from services.extraction.ai import providers
from services.extraction.ai.cloud_qwen_provider import AlibabaCloudQwenProvider, CloudProviderError
from services.extraction.ai.qwen_provider import OllamaQwenProvider, prepare_request, validate_response_fields, parse_json

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
FAKE_KEY = "sk-test-not-a-real-key-000000"
ENV = {"QWEN_CLOUD_ENABLED": "true", "QWEN_CLOUD_ALLOW_DOCUMENT_UPLOAD": "true",
       "QWEN_CLOUD_BASE_URL": "https://example-workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
       "QWEN_CLOUD_MODEL": "qwen3.7-plus", "QWEN_CLOUD_API_KEY": FAKE_KEY}


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def completion(content, model="qwen3.7-plus", finish="stop"):
    return FakeResponse(json.dumps({"model": model, "choices": [{"finish_reason": finish,
        "message": {"role": "assistant", "content": content}}], "usage": {"prompt_tokens": 900, "completion_tokens": 40}}).encode())


def http_error(code, body=b'{"error":{"code":"Throttling"}}'):
    return HTTPError("https://x.aliyuncs.com", code, "error", {}, io.BytesIO(body))


def provider(opener, **kwargs):
    return AlibabaCloudQwenProvider(opener, environ=ENV, sleep=lambda _s: None, **kwargs)


class Disabled(unittest.TestCase):
    def test_default_selection_is_disabled_without_an_explicit_cloud_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(providers.provider_name(), "disabled")
            with self.assertRaises(providers.ProviderDisabled):
                providers.select_qwen_provider()

    def test_cloud_needs_explicit_enable_and_upload_acknowledgement(self):
        for missing in ("QWEN_CLOUD_ENABLED", "QWEN_CLOUD_ALLOW_DOCUMENT_UPLOAD"):
            env = {k: v for k, v in ENV.items() if k != missing}
            with self.assertRaisesRegex(CloudProviderError, "DISABLED"):
                AlibabaCloudQwenProvider(MagicMock(), environ=env)

    def test_disabled_provider_never_constructs_a_client(self):
        with patch.dict(os.environ, {"QWEN_PROVIDER": "disabled"}, clear=True):
            with self.assertRaises(providers.ProviderDisabled):
                providers.select_qwen_provider()

    def test_unknown_provider_name_fails_closed(self):
        with patch.dict(os.environ, {"QWEN_PROVIDER": "somewhere-else"}, clear=True):
            with self.assertRaises(providers.ProviderDisabled):
                providers.select_qwen_provider()


class Configuration(unittest.TestCase):
    def test_rejects_non_https_foreign_or_credentialed_endpoints(self):
        for base in ("http://x.aliyuncs.com/v1", "https://evil.example.com/v1", "https://u:p@x.aliyuncs.com/v1",
                     "https://x.aliyuncs.com.evil.com/v1"):
            with self.assertRaisesRegex(CloudProviderError, "BASE_URL"):
                provider(MagicMock(), base=base)

    def test_missing_key_or_model_is_reported_without_echoing_values(self):
        with self.assertRaisesRegex(CloudProviderError, "API_KEY_MISSING"):
            AlibabaCloudQwenProvider(MagicMock(), environ={**ENV, "QWEN_CLOUD_API_KEY": ""})
        with self.assertRaisesRegex(CloudProviderError, "MODEL_INVALID"):
            AlibabaCloudQwenProvider(MagicMock(), environ={**ENV, "QWEN_CLOUD_MODEL": "bad model"})

    def test_key_never_appears_in_repr_or_metadata(self):
        p = provider(MagicMock())
        self.assertNotIn(FAKE_KEY, repr(p))
        self.assertNotIn(FAKE_KEY, json.dumps([p.metadata("/api/version"), p.metadata("/api/tags"), p.base]))

    def test_bounds_on_timeout_and_retries(self):
        with self.assertRaises(CloudProviderError):
            provider(MagicMock(), timeout=0)
        with self.assertRaises(CloudProviderError):
            provider(MagicMock(), max_retries=9)


class RequestAndResponse(unittest.TestCase):
    fields = ["invoice_number", "total_amount"]

    def payload(self, p):
        return prepare_request(PNG, self.fields, model=p.model, profile="invoice", source_context={})

    def test_request_uses_documented_image_thinking_and_json_settings(self):
        opener = MagicMock()
        opener.open.return_value = completion('{"invoice_number": "INV-1", "total_amount": "10.00"}')
        p = provider(opener)
        p.chat(self.payload(p))
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertTrue(request.full_url.endswith("/compatible-mode/v1/chat/completions"))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 60.0)
        image = body["messages"][0]["content"][0]
        self.assertEqual(image["type"], "image_url")
        self.assertTrue(image["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(image["image_url"]["url"].split(",", 1)[1]), PNG)
        self.assertIs(body["enable_thinking"], False)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("JSON", body["messages"][0]["content"][1]["text"])
        self.assertEqual((body["temperature"], body["stream"]), (0, False))

    def test_valid_answer_passes_the_unchanged_strict_validation(self):
        opener = MagicMock()
        opener.open.return_value = completion('{"invoice_number": "INV-1", "total_amount": null}')
        p = provider(opener)
        usable, rejected = validate_response_fields(parse_json(p.chat(self.payload(p))), self.fields,
                                                    model=p.model, profile="invoice")
        self.assertEqual(usable, {"invoice_number": "INV-1", "total_amount": None})  # null is preserved
        self.assertEqual(rejected, {})
        self.assertEqual(p.last_call["attempts"], 1)
        self.assertNotIn("INV-1", json.dumps(p.last_call))

    def test_invalid_amount_and_truncated_or_other_model_answers_are_rejected(self):
        opener = MagicMock()
        opener.open.return_value = completion('{"invoice_number": "INV-1", "total_amount": "RM 10"}')
        p = provider(opener)
        usable, rejected = validate_response_fields(parse_json(p.chat(self.payload(p))), self.fields, model=p.model, profile="invoice")
        self.assertIsNone(usable["total_amount"])
        self.assertIn("total_amount", rejected)
        for reply in (completion("{}", finish="length"), completion('{"invoice_number": null, "total_amount": null}', model="other")):
            opener.open.return_value = reply
            with self.assertRaises(ValueError):
                validate_response_fields(parse_json(p.chat(self.payload(p))), self.fields, model=p.model, profile="invoice")

    def test_transient_errors_are_retried_a_bounded_number_of_times(self):
        opener = MagicMock()
        opener.open.side_effect = [http_error(503), completion('{"invoice_number": "A", "total_amount": "1.00"}')]
        p = provider(opener, max_retries=1)
        p.chat(self.payload(p))
        self.assertEqual((opener.open.call_count, p.last_call["attempts"]), (2, 2))
        opener.open.side_effect = [URLError("down"), URLError("down")]
        with self.assertRaisesRegex(CloudProviderError, "UNREACHABLE"):
            p.chat(self.payload(p))

    def test_authentication_errors_are_not_retried_and_hide_the_key(self):
        opener = MagicMock()
        opener.open.side_effect = [http_error(401, b'{"error":{"code":"InvalidApiKey","message":"' + FAKE_KEY.encode() + b'"}}')]
        p = provider(opener)
        with self.assertRaises(CloudProviderError) as caught:
            p.chat(self.payload(p))
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(str(caught.exception), "CLOUD_HTTP_401:InvalidApiKey")
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_timeout_uses_the_capped_value_and_is_not_retried_by_default(self):
        # The extraction service lowers provider.timeout to what is left of the 20 s target.
        opener = MagicMock()
        opener.open.side_effect = TimeoutError("timed out")
        p = provider(opener)
        p.timeout = 6.5
        with self.assertRaises(CloudProviderError) as caught:
            p.chat(self.payload(p))
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 6.5)
        self.assertEqual(str(caught.exception), "CLOUD_UNREACHABLE: TimeoutError")
        self.assertEqual((p.last_call["http_status"], p.last_call["attempts"]), (None, 1))
        self.assertNotIn(FAKE_KEY, str(caught.exception) + json.dumps(p.last_call))

    def test_a_slow_connection_is_abandoned_at_the_wall_clock_deadline(self):
        # urllib applies its timeout per socket operation; live, a 15 s setting took 30.6 s.
        import threading
        import time
        release = threading.Event()
        opener = MagicMock()
        opener.open.side_effect = lambda *a, **k: (release.wait(5), completion('{"total_amount": "1.00"}'))[1]
        p = provider(opener)
        p.timeout = 0.2
        started = time.perf_counter()
        try:
            with self.assertRaises(CloudProviderError) as caught:
                p.chat(self.payload(p))
        finally:
            release.set()
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(str(caught.exception), "CLOUD_UNREACHABLE: TimeoutError")
        self.assertEqual(p.last_call["attempts"], 1)

    def test_malformed_body_is_a_provider_error(self):
        opener = MagicMock()
        opener.open.return_value = FakeResponse(b"<html>gateway</html>")
        p = provider(opener)
        with self.assertRaisesRegex(CloudProviderError, "MALFORMED"):
            p.chat(self.payload(p))


if __name__ == "__main__":
    unittest.main()
