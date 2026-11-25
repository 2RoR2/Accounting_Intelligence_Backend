"""Text-only model client for field structuring. OFF unless AI_EXTRACTION=text.

It sends masked OCR text (never the document image) to an OpenAI-compatible
Chat Completions endpoint and returns the parsed JSON answer. The answer is only
a candidate: the caller verifies every value against the local OCR text.

Configuration (environment):
    AI_EXTRACTION        off (default) | text
    AI_PROVIDERS         comma list tried in order, e.g. "gemini" or "gemini,qwen"
                         known names: gemini, qwen, openrouter, ollama, custom
    AI_MODEL / AI_BASE_URL / AI_API_KEY / AI_EXTRA_BODY
                         settings for the first provider
    AI_<NAME>_MODEL / AI_<NAME>_BASE_URL / AI_<NAME>_API_KEY / AI_<NAME>_EXTRA_BODY
                         per-provider settings (NAME upper-case, e.g. AI_QWEN_MODEL)
    AI_TIMEOUT_SECONDS   default 30 (1-120)
    AI_SERVER_RETRIES    default 2 (0-4), for 500/502/503/504 answers (a provider that is
                         briefly overloaded); waits 1.5, 3 s, or Retry-After up to 20 s
    AI_MAX_RETRIES       default 3 (0-4), for 429 and network errors (429 waits Retry-After,
                         up to 60 s, else 5/10/20 s)
    AI_MAX_CONCURRENCY   default 2 (1-8) simultaneous requests from this process
    AI_RESPONSE_FORMAT   json_object (default) | none
    AI_REASONING_EFFORT  low | medium | high | off. Sent as "reasoning_effort" for
                         reasoning models; default "low" for gpt-oss models (the
                         answer is copy-and-normalise work, not deep reasoning, and
                         medium effort spent ~2k hidden tokens and ~15 s per call),
                         off (not sent) for every other model. AI_EXTRA_BODY wins.

EXTRA_BODY is a JSON object merged into the request for provider-specific options
(for example {"reasoning_effort": "low"}). It cannot replace model or messages.

"ollama" targets a model on this machine or network (on-premises mode): nothing
leaves the organisation. Plain http is accepted only for a private address.

API keys are never logged, returned in errors or written to run records.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SERVER_ERROR_STATUS = {500, 502, 503, 504}
UNREADABLE_ANSWER_ERRORS = {'AI_RESPONSE_NOT_JSON', 'AI_RESPONSE_MALFORMED'}
RETRYABLE_STATUS = {429} | SERVER_ERROR_STATUS
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024
PRESETS = {
    'gemini': {'base': 'https://generativelanguage.googleapis.com/v1beta/openai',
               'model': 'gemini-3.5-flash-lite', 'keys': ('GEMINI_API_KEY', 'GOOGLE_API_KEY'),
               'hosts': r'generativelanguage\.googleapis\.com'},
    # The exact Model Studio model id and workspace URL are account configuration.
    'qwen': {'base': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1', 'model': '',
             'keys': ('DASHSCOPE_API_KEY', 'QWEN_CLOUD_API_KEY'),
             'hosts': r'dashscope-intl\.aliyuncs\.com|[a-z0-9][a-z0-9-]*\.ap-southeast-1\.maas\.aliyuncs\.com'},
    'openrouter': {'base': 'https://openrouter.ai/api/v1', 'model': '', 'keys': ('OPENROUTER_API_KEY',),
                   'hosts': r'openrouter\.ai'},
    'ollama': {'base': 'http://127.0.0.1:11434/v1', 'model': '', 'keys': (), 'hosts': None, 'local': True},
    'custom': {'base': '', 'model': '', 'keys': (), 'hosts': None},
}
_slots: threading.BoundedSemaphore | None = None
_slots_lock = threading.Lock()


class TextModelError(RuntimeError):
    """Operator-safe error code: never contains the API key or document text."""
    def __init__(self, code, status=None):
        self.code = code
        self.status = status  # HTTP status when the provider answered with an error
        self.failures = []
        super().__init__(code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def ai_mode(environ=None) -> str:
    env = os.environ if environ is None else environ
    mode = env.get('AI_EXTRACTION', 'off').strip().lower() or 'off'
    if mode not in {'off', 'text'}:
        raise TextModelError('AI_EXTRACTION_INVALID')
    return mode


def _number(env, name, default, low, high, kind=float):
    try:
        value = kind(env.get(name, str(default)))
    except (TypeError, ValueError):
        raise TextModelError(name + '_INVALID') from None
    if not math.isfinite(float(value)) or not low <= value <= high:
        raise TextModelError(name + '_INVALID')
    return value


def _private_host(host):
    if host == 'localhost' or host.endswith('.local') or host.endswith('.internal'):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


class TextModelProvider:
    """One configured endpoint."""

    def __init__(self, name, *, first=False, environ=None, opener=None, sleep=time.sleep):
        env = os.environ if environ is None else environ
        if name not in PRESETS:
            raise TextModelError('AI_PROVIDER_UNKNOWN')
        preset = PRESETS[name]
        prefix = 'AI_' + name.upper() + '_'

        def setting(key, default=''):
            value = env.get(prefix + key, '')
            if not value and first:
                value = env.get('AI_' + key, '')
            return (value or default).strip()

        self.name = name
        self.base = setting('BASE_URL', preset['base']).rstrip('/')
        self.model = setting('MODEL', preset['model'])
        key = setting('API_KEY')
        for variable in preset['keys']:
            key = key or env.get(variable, '').strip()
        self._api_key = key
        self.local = bool(preset.get('local'))
        try:
            url = urlsplit(self.base)
            host = (url.hostname or '').lower()
            url.port  # Raises ValueError for a malformed port.
        except ValueError:
            raise TextModelError('AI_BASE_URL_INVALID') from None
        if not host or url.username or url.password or url.query or url.fragment:
            raise TextModelError('AI_BASE_URL_INVALID')
        if url.scheme == 'http':
            # Plain http only to a model inside the organisation's own network.
            if not (self.local or name == 'custom') or not _private_host(host):
                raise TextModelError('AI_BASE_URL_INVALID')
            self.local = True
        elif url.scheme != 'https':
            raise TextModelError('AI_BASE_URL_INVALID')
        if preset['hosts'] and not re.fullmatch(preset['hosts'], host):
            raise TextModelError('AI_BASE_URL_INVALID')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}', self.model):
            raise TextModelError('AI_MODEL_MISSING')
        if not self.local and (len(self._api_key) < 16 or any(c.isspace() for c in self._api_key)):
            raise TextModelError('AI_API_KEY_MISSING')
        extra = setting('EXTRA_BODY')
        try:
            self.extra_body = json.loads(extra) if extra else {}
        except ValueError:
            raise TextModelError('AI_EXTRA_BODY_INVALID') from None
        if not isinstance(self.extra_body, dict) or {'model', 'messages', 'stream'} & set(self.extra_body):
            raise TextModelError('AI_EXTRA_BODY_INVALID')
        effort = env.get('AI_REASONING_EFFORT', '').strip().lower()
        if not effort:
            effort = 'low' if 'gpt-oss' in self.model.lower() else 'off'
        if effort not in {'low', 'medium', 'high', 'off'}:
            raise TextModelError('AI_REASONING_EFFORT_INVALID')
        self.reasoning_effort = None if effort == 'off' else effort
        self.timeout = _number(env, 'AI_TIMEOUT_SECONDS', 30, 1, 120)
        self.max_retries = _number(env, 'AI_MAX_RETRIES', 3, 0, 4, int)
        # Server errors are usually a short overload; they get their own budget so that a
        # low AI_MAX_RETRIES (set to keep rate-limit waits short) does not drop the document.
        self.server_retries = _number(env, 'AI_SERVER_RETRIES', 2, 0, 4, int)
        self.response_format = env.get('AI_RESPONSE_FORMAT', 'json_object').strip() or 'json_object'
        if self.response_format not in {'json_object', 'none'}:
            raise TextModelError('AI_RESPONSE_FORMAT_INVALID')
        self.opener = opener if opener is not None else build_opener(_NoRedirect())
        self._sleep = sleep

    def __repr__(self):
        return f'TextModelProvider(name={self.name!r}, base={self.base!r}, model={self.model!r}, api_key=<redacted>)'

    def complete_json(self, system, user, *, max_tokens=4000):
        """Return (parsed JSON object, call record). The record holds no content."""
        body = {'temperature': 0, 'max_tokens': max_tokens,
                **({'reasoning_effort': self.reasoning_effort} if self.reasoning_effort else {}), **self.extra_body,
                'model': self.model, 'stream': False,
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
        if self.response_format == 'json_object':
            body.setdefault('response_format', {'type': 'json_object'})
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        if len(data) > MAX_REQUEST_BYTES:
            raise TextModelError('AI_REQUEST_TOO_LARGE')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'User-Agent': 'accounting-extraction/1'}
        if self._api_key:
            headers['Authorization'] = 'Bearer ' + self._api_key
        started = time.perf_counter()
        attempts, status = 0, None
        while True:
            attempts += 1
            request = Request(self.base + '/chat/completions', data=data, headers=headers, method='POST')
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    status = getattr(response, 'status', 200)
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                break
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get('Retry-After') if error.headers else None
                error.close()
                budget = self.server_retries if status in SERVER_ERROR_STATUS else self.max_retries
                if status in RETRYABLE_STATUS and attempts <= budget and not exhausted_limit(status, retry_after):
                    self._sleep(retry_delay(status, retry_after, attempts))
                    continue
                code = {400: 'AI_BAD_REQUEST', 401: 'AI_AUTH_REJECTED', 402: 'AI_BILLING_REQUIRED',
                        403: 'AI_AUTH_REJECTED', 404: 'AI_MODEL_OR_ENDPOINT_NOT_FOUND', 413: 'AI_REQUEST_TOO_LARGE',
                        429: 'AI_RATE_LIMITED'}.get(status)
                if code is None:
                    code = 'AI_PROVIDER_SERVER_ERROR' if status in SERVER_ERROR_STATUS else 'AI_HTTP_ERROR'
                raise TextModelError(code, status) from None
            except (URLError, TimeoutError, OSError):
                if attempts <= self.max_retries:
                    self._sleep(min(20.0, 1.5 * 2 ** (attempts - 1)))
                    continue
                raise TextModelError('AI_UNREACHABLE') from None
        seconds = round(time.perf_counter() - started, 3)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TextModelError('AI_RESPONSE_TOO_LARGE')
        try:
            envelope = json.loads(payload)
            choice = envelope['choices'][0]
            content = choice['message']['content']
        except (ValueError, KeyError, IndexError, TypeError):
            raise TextModelError('AI_RESPONSE_MALFORMED') from None
        if choice.get('finish_reason') == 'length':
            raise TextModelError('AI_RESPONSE_TRUNCATED')
        answer = parse_json_object(content)
        usage = envelope.get('usage') if isinstance(envelope.get('usage'), dict) else {}
        return answer, {'provider': self.name, 'model': self.model, 'local': self.local, 'status': status,
                        'attempts': attempts, 'seconds': seconds,
                        'prompt_tokens': usage.get('prompt_tokens'),
                        'completion_tokens': usage.get('completion_tokens')}


RATE_LIMIT_MAX_WAIT_SECONDS = 60.0


def _hinted_seconds(retry_after):
    try:
        hinted = float(retry_after) if retry_after else 0.0
    except ValueError:
        hinted = 0.0
    return hinted if math.isfinite(hinted) and hinted > 0 else 0.0


def exhausted_limit(status, retry_after):
    """True for a 429 whose Retry-After is beyond a minute: a daily (or longer) quota is spent.

    Waiting cannot help within one document, so the call fails at once and the next
    configured provider (AI_PROVIDERS) is tried instead of holding the job for minutes.
    """
    return status == 429 and _hinted_seconds(retry_after) > RATE_LIMIT_MAX_WAIT_SECONDS


def retry_delay(status, retry_after, attempt):
    """Seconds to wait before retry number `attempt` (1-based).

    A per-minute token limit (429) on a free tier resets within a minute, so a
    rate-limited call honours Retry-After up to 60 s and otherwise backs off
    5, 10, 20 s; server errors back off 1.5, 3, 6 s (both capped).
    """
    hinted = _hinted_seconds(retry_after)
    if status == 429:
        return min(60.0, max(hinted, 5.0 * 2 ** (attempt - 1)))
    return min(20.0, max(hinted, 1.5 * 2 ** (attempt - 1)))


def parse_json_object(content):
    """Parse a model answer that should be one JSON object (tolerates a ``` fence)."""
    if not isinstance(content, str):
        raise TextModelError('AI_RESPONSE_MALFORMED')
    text = content.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        answer = json.loads(text)
    except ValueError:
        raise TextModelError('AI_RESPONSE_NOT_JSON') from None
    if not isinstance(answer, dict):
        raise TextModelError('AI_RESPONSE_NOT_JSON')
    return answer


def configured_providers(environ=None, **kwargs):
    """Providers in fallback order. Raises when AI is on but nothing usable is configured."""
    env = os.environ if environ is None else environ
    names = [n.strip().lower() for n in env.get('AI_PROVIDERS', 'gemini').split(',') if n.strip()]
    if not names or len(names) > 4 or len(set(names)) != len(names):
        raise TextModelError('AI_PROVIDERS_INVALID')
    return [TextModelProvider(name, first=index == 0, environ=env, **kwargs) for index, name in enumerate(names)]


def _request_slots(environ=None):
    global _slots
    with _slots_lock:
        if _slots is None:
            env = os.environ if environ is None else environ
            _slots = threading.BoundedSemaphore(_number(env, 'AI_MAX_CONCURRENCY', 2, 1, 8, int))
        return _slots


def complete_json(system, user, *, providers=None, max_tokens=4000):
    """Ask each configured provider in turn until one returns a JSON object.

    Returns (answer, call record). The record lists every failed provider by
    error code so an operator can see why a fallback was used.
    """
    providers = configured_providers() if providers is None else providers
    failures = []
    with _request_slots():
        for provider in providers:
            for attempt in (1, 2):
                try:
                    answer, record = provider.complete_json(system, user, max_tokens=max_tokens)
                except TextModelError as error:
                    failures.append({'provider': provider.name, 'error': error.code,
                                     **({'status': error.status} if getattr(error, 'status', None) else {})})
                    if error.code in UNREADABLE_ANSWER_ERRORS and attempt == 1:
                        continue  # A sampled answer that is not JSON is usually a one-off: ask once more.
                    break
                record['failed_before'] = failures
                return answer, record
    error = TextModelError(failures[-1]['error'] if failures else 'AI_NO_PROVIDER')
    error.failures = failures
    raise error
