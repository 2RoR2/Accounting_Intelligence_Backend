"""Endpoints: flag off gives 403 AI_ASSIST_OFF, roles are enforced, the request model is strict."""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import main  # noqa: F401  (the app imports the route modules; importing a route module first would be circular)
from services.routes.assistant import routes

ADMIN = {'id': 'u1', 'tenant_id': 't1', 'role': 'local_admin', 'name': 'Admin'}


@pytest.fixture
def signed_in(monkeypatch):
    def fake(request, roles=None):
        user = request
        if roles and user['role'] not in roles:
            raise HTTPException(403, 'You do not have permission for this workspace.')
        return user
    monkeypatch.setattr(routes, 'require_workspace_user', fake)


@pytest.mark.parametrize('value', ['', 'off', 'false', 'OFF'])
def test_flag_off_returns_403_and_runs_nothing(monkeypatch, signed_in, value):
    monkeypatch.setenv('AI_ASSIST', value)
    monkeypatch.setattr(routes, 'ask', lambda *a, **k: pytest.fail('ask must not run'))
    monkeypatch.setattr(routes, 'check_question_budget', lambda *a, **k: pytest.fail('budget must not run'))
    with pytest.raises(HTTPException) as error:
        routes.ask_question(ADMIN, routes.AskInput(question='Which bills are due?'))
    assert error.value.status_code == 403 and error.value.detail['code'] == 'AI_ASSIST_OFF'
    with pytest.raises(HTTPException):
        routes.suggestions(ADMIN, page='today')


def test_default_is_off(monkeypatch, signed_in):
    monkeypatch.delenv('AI_ASSIST', raising=False)
    with pytest.raises(HTTPException) as error:
        routes.suggestions(ADMIN, page='today')
    assert error.value.detail['code'] == 'AI_ASSIST_OFF'


def test_super_admin_and_no_company_are_refused(monkeypatch, signed_in):
    monkeypatch.setenv('AI_ASSIST', 'on')
    with pytest.raises(HTTPException) as error:
        routes.suggestions({**ADMIN, 'role': 'super_admin', 'tenant_id': None}, page='today')
    assert error.value.status_code == 403


def test_flag_on_checks_the_budget_then_answers(monkeypatch, signed_in):
    monkeypatch.setenv('AI_ASSIST', 'on')
    seen = {}
    monkeypatch.setattr(routes, 'check_question_budget', lambda user, **limits: seen.update(limits))
    monkeypatch.setattr(routes, 'ask', lambda user, question, context: {'answer': question, 'context': context})
    body = routes.AskInput(question='  Which bills are due?  ', context={'page': 'today'})
    assert routes.ask_question(ADMIN, body) == {'answer': 'Which bills are due?', 'context': {'page': 'today'}}
    assert seen['user_limit'] == 20 and seen['tenant_limit'] == 200
    assert routes.suggestions(ADMIN, page='Today')['suggestions'][0] == 'Which bills are due next week?'


def test_budget_429_propagates(monkeypatch, signed_in):
    monkeypatch.setenv('AI_ASSIST', 'on')

    def limited(user, **limits):
        raise HTTPException(429, {'code': 'AI_ASSIST_RATE_LIMITED', 'message': 'wait'})
    monkeypatch.setattr(routes, 'check_question_budget', limited)
    monkeypatch.setattr(routes, 'ask', lambda *a: pytest.fail('a limited question must not fall back'))
    with pytest.raises(HTTPException) as error:
        routes.ask_question(ADMIN, routes.AskInput(question='Which bills are due?'))
    assert error.value.status_code == 429


@pytest.mark.parametrize('body', [
    {'question': ''}, {'question': 'x' * 501}, {'question': 'q', 'tenantId': 't2'},
    {'question': 'q', 'context': {'page': 'today', 'tenant': 't2'}},
    {'question': 'q', 'context': {'documentId': 'not-a-uuid'}},
    {'question': 'q', 'context': {'reportType': 'all-companies'}},
    {'question': 'q', 'context': {'period': '2026-13'}},
    {'question': 'q', 'context': {'page': '../admin'}},
])
def test_request_model_is_strict(body):
    with pytest.raises(ValidationError):
        routes.AskInput.model_validate(body)
