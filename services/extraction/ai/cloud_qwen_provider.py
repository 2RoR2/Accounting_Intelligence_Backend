"""Optional Alibaba Cloud Model Studio (OpenAI-compatible) Qwen provider. DISABLED BY DEFAULT.

It plugs into the same seam as OllamaQwenProvider (base/model/timeout, metadata(),
chat()), translating the existing Ollama-shaped request into an OpenAI-compatible
Chat Completions request and the reply back into the Ollama envelope. All existing
strict response validation, source verification, Module 3 validation and human
review therefore apply unchanged: a cloud answer is only ever an unverified candidate.

Verified against the public Model Studio documentation (November 2026):
  * images: content item {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  * thinking: top-level "enable_thinking"; it defaults to true for the qwen3.7 series,
    so this provider always sends it explicitly (false unless configured).
  * structured output: response_format {"type": "json_object"} (prompt must contain
    "JSON", which the shared prompt does) or {"type": "json_schema", ...}.
Not verified (no account): the exact workspace base URL, the model ID string that the
API reports back, pricing and rate limits. They are configuration, never guessed here.

Nothing in this module runs unless QWEN_PROVIDER=alibaba_cloud AND QWEN_CLOUD_ENABLED=true
AND QWEN_CLOUD_ALLOW_DOCUMENT_UPLOAD=true are set. The API key is never logged, returned
in errors, stored in run records or shown by repr().
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROVIDER_NAME = "alibaba_cloud"
SINGAPORE_WORKSPACE = re.compile(r"[a-z0-9][a-z0-9-]*\.ap-southeast-1\.maas\.aliyuncs\.com")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class CloudProviderError(RuntimeError):
    """Operator-safe error: never contains the API key or document content."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _image_mime(data_b64: str) -> str:
    head = data_b64[:16]
    if head.startswith("iVBOR"):
        return "image/png"
    if head.startswith("/9j/"):
        return "image/jpeg"
    if head.startswith("UklGR"):
        return "image/webp"
    raise CloudProviderError("CLOUD_UNSUPPORTED_IMAGE_FORMAT")


def _truthy(value) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes"}


class AlibabaCloudQwenProvider:
    name = PROVIDER_NAME

    def __init__(self, opener=None, *, base=None, model=None, api_key=None, timeout=None, max_retries=None,
                 enable_thinking=None, response_format=None, sleep=time.sleep, environ=None):
        env = os.environ if environ is None else environ
        if not (_truthy(env.get("QWEN_CLOUD_ENABLED")) and _truthy(env.get("QWEN_CLOUD_ALLOW_DOCUMENT_UPLOAD"))):
            raise CloudProviderError("CLOUD_PROVIDER_DISABLED: set QWEN_CLOUD_ENABLED=true and "
                                     "QWEN_CLOUD_ALLOW_DOCUMENT_UPLOAD=true to permit sending document images off this machine")
        self.base = (base or env.get("QWEN_CLOUD_BASE_URL", "")).strip().rstrip("/")
        self.model = (model or env.get("QWEN_CLOUD_MODEL", "")).strip()
        self._api_key = (api_key or env.get("QWEN_CLOUD_API_KEY") or env.get("DASHSCOPE_API_KEY", "")).strip()
        try:
            url = urlsplit(self.base) if self.base else None
            valid_port = url is not None and url.port in {None, 443}
        except ValueError:
            url, valid_port = None, False
        if (not url or url.scheme != "https" or not url.hostname or url.username or url.password or url.query
                or url.fragment or not valid_port or url.path != '/compatible-mode/v1'
                or not (url.hostname == 'dashscope-intl.aliyuncs.com' or SINGAPORE_WORKSPACE.fullmatch(url.hostname))):
            raise CloudProviderError("CLOUD_BASE_URL_INVALID: expected the Singapore Model Studio compatible-mode endpoint")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", self.model):
            raise CloudProviderError("CLOUD_MODEL_INVALID: set QWEN_CLOUD_MODEL to the exact model ID")
        if len(self._api_key) < 16 or any(c.isspace() for c in self._api_key):
            raise CloudProviderError("CLOUD_API_KEY_MISSING")
        try:
            self.timeout = float(timeout if timeout is not None else env.get("QWEN_CLOUD_TIMEOUT_SECONDS", "60"))
            self.max_retries = int(max_retries if max_retries is not None else env.get("QWEN_CLOUD_MAX_RETRIES", "0"))
        except (TypeError, ValueError):
            raise CloudProviderError("CLOUD_TIMEOUT_OR_RETRY_INVALID") from None
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300 or not 0 <= self.max_retries <= 3:
            raise CloudProviderError("CLOUD_TIMEOUT_OR_RETRY_INVALID: timeout 0-300 s, retries 0-3")
        self.enable_thinking = _truthy(enable_thinking if enable_thinking is not None
                                       else env.get("QWEN_CLOUD_ENABLE_THINKING", "false"))
        self.response_format = (response_format or env.get("QWEN_CLOUD_RESPONSE_FORMAT", "json_object")).strip()
        if self.response_format not in {"json_object", "json_schema", "none"}:
            raise CloudProviderError("CLOUD_RESPONSE_FORMAT_INVALID")
        self.opener = opener if opener is not None else build_opener(_NoRedirect())
        self._sleep = sleep
        self.last_call = None  # provider, model, status, attempts, seconds; never content

    def __repr__(self):
        return f"AlibabaCloudQwenProvider(base={self.base!r}, model={self.model!r}, api_key=<redacted>)"

    def metadata(self, path):
        """Local answers only: the cloud API has no Ollama inventory; nothing is downloaded."""
        if path == "/api/version":
            return {"version": "openai-compatible", "provider": PROVIDER_NAME}
        if path == "/api/tags":
            return {"models": [{"name": self.model, "model": self.model, "digest": None}]}
        if path == "/api/ps":
            return {"models": []}
        raise ValueError("Unsupported metadata endpoint")

    def build_request_body(self, payload):
        if payload.get("model") != self.model or payload.get("stream") is not False:
            raise ValueError("Request model/stream settings differ from provider configuration")
        message = (payload.get("messages") or [{}])[0]
        images = message.get("images") or []
        if len(images) != 1 or not isinstance(message.get("content"), str):
            raise ValueError("Expected exactly one page image and a text prompt")
        schema = payload.get("format")
        prompt = message["content"]
        if schema is not None:
            prompt += "\nReturn only a JSON object matching this JSON schema: " + json.dumps(schema, ensure_ascii=False)
        options = payload.get("options") or {}
        body = {
            "model": self.model, "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{_image_mime(images[0])};base64,{images[0]}"}},
                {"type": "text", "text": prompt}]}],
            "temperature": options.get("temperature", 0),
            "max_tokens": options.get("num_predict", 512),
            "enable_thinking": self.enable_thinking,
        }
        if self.response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        elif self.response_format == "json_schema" and schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "accordai_fields", "schema": schema, "strict": True}}
        return body

    def chat(self, payload):
        body = json.dumps(self.build_request_body(payload), allow_nan=False).encode("utf-8")
        started = time.perf_counter()
        attempt, status = 0, None
        while True:
            attempt += 1
            request = Request(self.base + "/chat/completions", data=body, method="POST",
                              headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._api_key})
            try:
                status, raw = self._send_within_deadline(request)
                break
            except HTTPError as error:
                status = error.code
                code = None
                try:
                    code = (json.loads(error.read() or b"{}").get("error") or {}).get("code")
                except Exception:
                    pass
                if status in RETRYABLE_STATUS and attempt <= self.max_retries:
                    self._sleep(min(8.0, 2 ** (attempt - 1)))
                    continue
                self._record(status, attempt, started)
                safe_code = code if isinstance(code, str) and re.fullmatch(r"[\w.-]{1,64}", code) else None
                raise CloudProviderError(f"CLOUD_HTTP_{status}" + (f":{safe_code}" if safe_code else "")) from None
            except (URLError, TimeoutError, OSError) as error:
                if attempt <= self.max_retries:
                    self._sleep(min(8.0, 2 ** (attempt - 1)))
                    continue
                self._record(None, attempt, started)
                raise CloudProviderError("CLOUD_UNREACHABLE: " + type(error).__name__) from None
        elapsed = time.perf_counter() - started
        self._record(status, attempt, started)
        envelope = self.to_ollama_envelope(raw, elapsed)
        self.last_call.update(prompt_tokens=envelope.get('prompt_eval_count'),
                              completion_tokens=envelope.get('eval_count'))
        return json.dumps(envelope, ensure_ascii=False)

    def _send_within_deadline(self, request):
        """One attempt, bounded by self.timeout in wall-clock time.

        urllib's timeout applies to each socket operation, so connect, TLS and a slow
        read could each use all of it (measured: 30.6 s against a 15 s setting). The
        caller stops waiting at the deadline; the abandoned daemon thread ends at its
        own socket timeout and its answer is discarded.
        """
        box = {}

        def send():
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    box["result"] = (getattr(response, "status", 200), response.read().decode("utf-8"))
            except BaseException as error:  # Re-raised in the caller's thread below.
                box["error"] = error

        thread = threading.Thread(target=send, name="qwen-cloud-request", daemon=True)
        thread.start()
        thread.join(self.timeout)
        if thread.is_alive():
            raise TimeoutError("CLOUD_DEADLINE_EXCEEDED")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _record(self, status, attempts, started):
        self.last_call = {"provider": PROVIDER_NAME, "model": self.model, "http_status": status,
                          "attempts": attempts, "seconds": round(time.perf_counter() - started, 3)}

    def to_ollama_envelope(self, raw, elapsed):
        try:
            from services.extraction.ai.qwen_provider import parse_json
            response = parse_json(raw)
            choice = response["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError('Malformed assistant message')
        except (ValueError, KeyError, IndexError, TypeError):
            raise CloudProviderError("CLOUD_RESPONSE_MALFORMED") from None
        finish = choice.get("finish_reason")
        usage = response.get("usage") or {}
        return {"model": response.get("model"), "done": True, "done_reason": "stop" if finish == "stop" else str(finish),
                "message": {"role": message.get("role", "assistant"), "content": message.get("content"),
                            **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {})},
                "prompt_eval_count": usage.get("prompt_tokens"), "eval_count": usage.get("completion_tokens"),
                "total_duration": int(elapsed * 1e9), "provider": PROVIDER_NAME}
