"""Settings for Ask AccordAI. Every value has a safe default; AI_ASSIST is off unless set to on."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from services.extraction.ai.text_llm import TextModelError, configured_providers

DEFAULT_GROQ_MODEL = 'openai/gpt-oss-20b'
GROQ_HOST = 'api.groq.com'
MAX_ROUNDS = 3
MAX_TOOL_CALLS = 4
MAX_TOKENS_PER_CALL = 1200
MAX_FACTS = 40
FALLBACK_FACTS = 12
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}')


def _env(environ=None):
    return os.environ if environ is None else environ


def enabled(environ=None) -> bool:
    return _env(environ).get('AI_ASSIST', 'off').strip().lower() in {'on', 'true', '1', 'yes'}


def int_setting(name: str, default: int, low: int, high: int, environ=None) -> int:
    """An integer setting clamped to [low, high]; an unreadable value falls back to the default."""
    try:
        value = int(_env(environ).get(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def user_limit(environ=None) -> int:
    return int_setting('AI_ASSIST_USER_LIMIT', 20, 0, 1000, environ)


def tenant_daily_limit(environ=None) -> int:
    return int_setting('AI_ASSIST_TENANT_DAILY_LIMIT', 200, 0, 100_000, environ)


def tenant_daily_tokens(environ=None) -> int:
    return int_setting('AI_ASSIST_TENANT_DAILY_TOKENS', 150_000, 0, 10_000_000, environ)


def max_tool_result_chars(environ=None) -> int:
    return int_setting('AI_ASSIST_MAX_TOOL_RESULT_CHARS', 6000, 1000, 20_000, environ)


def assistant_providers(environ=None, **kwargs):
    """Providers for the assistant, in fallback order.

    Same provider names and per-provider settings as extraction (AI_PROVIDERS, AI_<NAME>_*), with two differences:
    AI_ASSIST_PROVIDERS can give a different order, and the first provider uses AI_ASSIST_MODEL. When that is unset
    and the first provider is Groq, it uses openai/gpt-oss-20b, which has its own Groq quota, separate from the
    extraction model's. Raises TextModelError when nothing usable is configured (the caller then uses the fallback).
    """
    env = dict(_env(environ))
    order = env.get('AI_ASSIST_PROVIDERS', '').strip()
    if order:
        env['AI_PROVIDERS'] = order
    providers = configured_providers(env, **kwargs)
    model = env.get('AI_ASSIST_MODEL', '').strip()
    first = providers[0]
    if not model and (urlsplit(first.base).hostname or '').lower() == GROQ_HOST:
        model = DEFAULT_GROQ_MODEL
    if model:
        if not _MODEL.fullmatch(model):
            raise TextModelError('AI_ASSIST_MODEL_INVALID')
        first.model = model
    return providers
