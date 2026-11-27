"""Ask AccordAI endpoints (feature F1), behind AI_ASSIST (default off). Contract: docs/api/assistant.md.

    POST /api/assistant/ask           a read-only answer with facts, citations and an audit id
    GET  /api/assistant/suggestions   starter questions for a page

Accountants and local admins only. With AI_ASSIST off both return 403 AI_ASSIST_OFF and nothing else runs.
"""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from main import HTTPException, Request, require_workspace_user
from security.assistant_guard import check_question_budget
from services.assistant import config
from services.assistant.agent import ask
from services.assistant.suggestions import suggestions_for

ROLES = {'accountant', 'local_admin'}
OFF = {'code': 'AI_ASSIST_OFF', 'message': 'Ask AccordAI is turned off on this server.'}


class AskContext(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    page: str | None = Field(default=None, max_length=40, pattern=r'^[a-z0-9-]*$')
    documentId: UUID | None = None
    recordId: UUID | None = None
    reportType: Literal['profit-and-loss', 'balance-sheet', 'ageing', 'business-summary'] | None = None
    period: str | None = Field(default=None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$')


class AskInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=500)
    context: AskContext | None = None


def _assistant_user(request: Request) -> dict:
    user = require_workspace_user(request, ROLES)
    if not user.get('tenant_id'):
        raise HTTPException(403, 'This operation requires a company workspace.')
    if not config.enabled():
        raise HTTPException(403, OFF)
    return user


def ask_question(request: Request, body: AskInput):
    user = _assistant_user(request)
    check_question_budget(user, user_limit=config.user_limit(), tenant_limit=config.tenant_daily_limit(),
                          day=date.today().isoformat())
    context = body.context.model_dump(mode='json', exclude_none=True) if body.context else {}
    return ask(user, body.question, context)


def suggestions(request: Request, page: str = ''):
    _assistant_user(request)
    segment = page.strip().lower()[:40]
    return {'page': segment, 'suggestions': suggestions_for(segment)}


def register(router, handlers):
    router.add_api_route('/api/assistant/ask', ask_question, methods=['POST'], tags=['Assistant'])
    router.add_api_route('/api/assistant/suggestions', suggestions, methods=['GET'], tags=['Assistant'])
