"""The additive text_llm.chat(): tool calls are parsed, retries and provider fallback work, nothing leaks."""
import io
import json
from urllib.error import HTTPError

import pytest

from services.assistant.config import assistant_providers
from services.extraction.ai import text_llm

KEY = 'gsk_test_key_that_is_long_enough_123'


class _Response:
    def __init__(self, payload):
        self.payload, self.status = payload, 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _limit):
        return json.dumps(self.payload).encode('utf-8')


class _Opener:
    def __init__(self, *outcomes):
        self.outcomes, self.bodies = list(outcomes), []

    def open(self, request, timeout=None):
        self.bodies.append(json.loads(request.data))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)


def _provider(opener, **env):
    environ = {'AI_PROVIDERS': 'custom', 'AI_BASE_URL': 'https://api.groq.com/openai/v1', 'AI_MODEL': 'openai/gpt-oss-120b',
               'AI_API_KEY': KEY, 'AI_MAX_RETRIES': '1', 'AI_SERVER_RETRIES': '0', **env}
    return assistant_providers(environ, opener=opener, sleep=lambda _s: None)


def _tool_reply():
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {'content': None, 'tool_calls': [
        {'id': 'call_9', 'type': 'function', 'function': {'name': 'due_soon', 'arguments': '{"kind":"bills"}'}}]}}],
        'usage': {'prompt_tokens': 900, 'completion_tokens': 30}}


def test_chat_sends_tools_and_parses_tool_calls():
    opener = _Opener(_tool_reply())
    providers = _provider(opener)
    message, record = text_llm.chat([{'role': 'user', 'content': 'q'}], providers=providers,
                                    tools=[{'type': 'function', 'function': {'name': 'due_soon', 'parameters': {}}}])
    assert message == {'content': None, 'tool_calls': [{'id': 'call_9', 'name': 'due_soon',
                                                         'arguments': '{"kind":"bills"}'}]}
    body = opener.bodies[0]
    assert body['tools'] and body['tool_choice'] == 'auto' and 'response_format' not in body
    assert body['model'] == 'openai/gpt-oss-20b'          # the assistant's own Groq model, not extraction's 120b
    assert body['reasoning_effort'] == 'low'
    assert record['prompt_tokens'] == 900 and KEY not in json.dumps(record)


def test_explicit_assistant_model_and_order():
    providers = _provider(_Opener(), AI_ASSIST_MODEL='openai/gpt-oss-120b')
    assert providers[0].model == 'openai/gpt-oss-120b'
    with pytest.raises(text_llm.TextModelError):
        _provider(_Opener(), AI_ASSIST_MODEL='bad model name!')


def test_structured_final_answer_passes_response_format():
    opener = _Opener({'choices': [{'finish_reason': 'stop', 'message': {'content': '{"a":1}'}}], 'usage': {}})
    message, _ = text_llm.chat([{'role': 'user', 'content': 'q'}], providers=_provider(opener),
                               response_format={'type': 'json_schema', 'json_schema': {'name': 'x'}})
    assert message['content'] == '{"a":1}' and opener.bodies[0]['response_format']['type'] == 'json_schema'
    assert 'tools' not in opener.bodies[0]


def test_rate_limit_retries_then_next_provider():
    rate_limited = HTTPError('https://api.groq.com', 429, 'Too Many', {'Retry-After': '1'}, io.BytesIO(b''))
    unavailable = HTTPError('https://api.groq.com', 503, 'Down', {}, io.BytesIO(b''))
    first = _provider(_Opener(rate_limited, unavailable))[0]
    second = _provider(_Opener(_tool_reply()))[0]
    second.name = 'backup'
    message, record = text_llm.chat([{'role': 'user', 'content': 'q'}], providers=[first, second])
    assert message['tool_calls'][0]['name'] == 'due_soon'
    assert record['failed_before'] == [{'provider': 'custom', 'error': 'AI_PROVIDER_SERVER_ERROR', 'status': 503}]


def test_all_providers_failing_raises_a_code_only_error():
    providers = _provider(_Opener(HTTPError('u', 401, 'no', {}, io.BytesIO(b''))))
    with pytest.raises(text_llm.TextModelError) as error:
        text_llm.chat([{'role': 'user', 'content': 'q'}], providers=providers)
    assert error.value.code == 'AI_AUTH_REJECTED' and KEY not in str(error.value)


@pytest.mark.parametrize('payload,code', [
    ({'choices': []}, 'AI_RESPONSE_MALFORMED'),
    ({'choices': [{'finish_reason': 'length', 'message': {'content': 'x'}}]}, 'AI_RESPONSE_TRUNCATED'),
    ({'choices': [{'message': {'content': None, 'tool_calls': [{'function': {}}]}}]}, 'AI_RESPONSE_MALFORMED'),
])
def test_malformed_answers(payload, code):
    with pytest.raises(text_llm.TextModelError) as error:
        text_llm.chat([{'role': 'user', 'content': 'q'}], providers=_provider(_Opener(payload)))
    assert error.value.code == code


def test_complete_json_is_unchanged_for_extraction():
    opener = _Opener({'choices': [{'finish_reason': 'stop', 'message': {'content': '{"fields":{}}'}}], 'usage': {}})
    provider = text_llm.TextModelProvider('custom', first=True, opener=opener, sleep=lambda _s: None, environ={
        'AI_BASE_URL': 'https://api.groq.com/openai/v1', 'AI_MODEL': 'openai/gpt-oss-120b', 'AI_API_KEY': KEY})
    answer, _ = provider.complete_json('system', 'user')
    assert answer == {'fields': {}} and opener.bodies[0]['model'] == 'openai/gpt-oss-120b'
    assert opener.bodies[0]['response_format'] == {'type': 'json_object'} and 'tools' not in opener.bodies[0]


GROQ = {'AI_BASE_URL': 'https://api.groq.com/openai/v1', 'AI_MODEL': 'openai/gpt-oss-120b', 'AI_API_KEY': 'k' * 24}


def test_a_fallback_provider_without_a_key_does_not_turn_the_assistant_off():
    providers = assistant_providers({**GROQ, 'AI_PROVIDERS': 'custom,gemini,qwen', 'GEMINI_API_KEY': 'g' * 24})
    assert [p.name for p in providers] == ['custom', 'gemini']
    assert providers[0].model == 'openai/gpt-oss-20b'


def test_when_the_first_provider_is_unusable_the_next_keeps_its_own_model():
    providers = assistant_providers({'AI_PROVIDERS': 'custom,gemini', 'GEMINI_API_KEY': 'g' * 24,
                                     'AI_ASSIST_MODEL': 'openai/gpt-oss-20b'})
    assert [(p.name, p.model) for p in providers] == [('gemini', 'gemini-3.5-flash-lite')]


def test_no_usable_provider_still_raises():
    with pytest.raises(text_llm.TextModelError):
        assistant_providers({'AI_PROVIDERS': 'custom,qwen'})
