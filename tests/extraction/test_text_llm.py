"""Focused tests for provider fallback, retry handling and request capacity."""
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError

import pytest

from services.extraction.ai import text_llm


def _completion(value):
    return {
        'choices': [{'message': {'content': json.dumps(value)}, 'finish_reason': 'stop'}],
        'usage': {},
    }


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _limit):
        return json.dumps(self.payload).encode('utf-8')


class _SequenceOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)


class _Provider:
    def __init__(self, name, *, error=None):
        self.name = name
        self.error = error
        self.calls = 0

    def complete_json(self, system, user, *, max_tokens=4000):
        self.calls += 1
        if self.error:
            raise text_llm.TextModelError(self.error)
        return {'provider': self.name}, {
            'provider': self.name, 'model': 'test-model', 'local': False,
            'status': 200, 'attempts': 1, 'seconds': 0,
        }


def test_provider_fallback_rotates_to_next_configured_provider():
    first = _Provider('first', error='AI_AUTH_REJECTED')
    second = _Provider('second')

    answer, record = text_llm.complete_json('system', 'user', providers=[first, second])

    assert answer == {'provider': 'second'}
    assert record['provider'] == 'second'
    assert record['failed_before'] == [{'provider': 'first', 'error': 'AI_AUTH_REJECTED'}]
    assert first.calls == 1 and second.calls == 1


def test_rate_limit_retries_after_retry_after_then_succeeds():
    limited = HTTPError('https://example.test', 429, 'Too Many Requests',
                        {'Retry-After': '5'}, io.BytesIO())
    waits = []
    provider = text_llm.TextModelProvider(
        'custom', first=True,
        environ={
            'AI_BASE_URL': 'https://api.example.test/v1',
            'AI_MODEL': 'test-model',
            'AI_API_KEY': 'k' * 32,
            'AI_MAX_RETRIES': '1',
        },
        opener=_SequenceOpener(limited, _completion({'ok': True})),
        sleep=waits.append,
    )

    answer, record = provider.complete_json('system', 'user')

    assert answer == {'ok': True}
    assert record['attempts'] == 2
    assert waits == [5.0]


def test_long_rate_limit_fails_immediately_and_falls_back():
    spent = HTTPError('https://example.test', 429, 'Too Many Requests',
                      {'Retry-After': '900'}, io.BytesIO())
    waits = []
    first = text_llm.TextModelProvider(
        'custom', first=True,
        environ={
            'AI_BASE_URL': 'https://api.example.test/v1',
            'AI_MODEL': 'test-model',
            'AI_API_KEY': 'k' * 32,
        },
        opener=_SequenceOpener(spent),
        sleep=waits.append,
    )
    second = _Provider('second')

    answer, record = text_llm.complete_json('system', 'user', providers=[first, second])

    assert answer == {'provider': 'second'}
    assert record['failed_before'] == [
        {'provider': 'custom', 'error': 'AI_RATE_LIMITED', 'status': 429}
    ]
    assert waits == []


def test_all_provider_failures_are_preserved_for_diagnosis():
    providers = [
        _Provider('first', error='AI_UNREACHABLE'),
        _Provider('second', error='AI_PROVIDER_SERVER_ERROR'),
    ]

    with pytest.raises(text_llm.TextModelError) as caught:
        text_llm.complete_json('system', 'user', providers=providers)

    assert caught.value.code == 'AI_PROVIDER_SERVER_ERROR'
    assert caught.value.failures == [
        {'provider': 'first', 'error': 'AI_UNREACHABLE'},
        {'provider': 'second', 'error': 'AI_PROVIDER_SERVER_ERROR'},
    ]


def test_concurrent_requests_are_capped_by_ai_max_concurrency(monkeypatch):
    monkeypatch.setattr(text_llm, '_slots', None)
    monkeypatch.setenv('AI_MAX_CONCURRENCY', '2')

    class BlockingProvider(_Provider):
        def __init__(self):
            super().__init__('blocking')
            self.lock = threading.Lock()
            self.active = 0
            self.maximum = 0
            self.started_two = threading.Event()
            self.release = threading.Event()

        def complete_json(self, system, user, *, max_tokens=4000):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 2:
                    self.started_two.set()
            try:
                assert self.started_two.wait(2), 'the concurrency test did not start two calls'
                assert self.release.wait(2), 'the concurrency test was not released'
                return {'ok': True}, {
                    'provider': self.name, 'model': 'test-model', 'local': False,
                    'status': 200, 'attempts': 1, 'seconds': 0,
                }
            finally:
                with self.lock:
                    self.active -= 1

    provider = BlockingProvider()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(text_llm.complete_json, 'system', 'user', providers=[provider])
                   for _ in range(4)]
        assert provider.started_two.wait(2)
        provider.release.set()
        results = [future.result() for future in futures]

    assert len(results) == 4
    assert provider.maximum == 2
