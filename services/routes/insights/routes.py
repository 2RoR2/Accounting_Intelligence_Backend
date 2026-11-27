"""Fraud Shield (F2) and AI Inbox (F3) endpoints. Contract: docs/api/insights-fraud-inbox.md.

All of them sit behind AI_ASSIST (default off). When it is off they answer 404 AI_ASSIST_OFF, and nothing else changes.
The GET endpoints run in a READ ONLY transaction, so a bug cannot write. Each POST writes a single audit_logs
row (an "approve anyway" reason, or a dismissal) and nothing else.
"""
from __future__ import annotations

import json
import uuid

from fastapi import Depends
from pydantic import BaseModel, Field

from main import *
from services.assistant import config as assistant_config
from services.insights import fraud, inbox

ROLES = {'local_admin', 'accountant'}


def ai_assist_enabled(environ=None) -> bool:
    """The one AI_ASSIST switch, shared with Ask AccordAI (services/assistant/config.enabled)."""
    return assistant_config.enabled(environ)


def require_ai_assist():
    """Route dependency: runs before body validation, so a request with the flag off gets 404, as if the route
    did not exist."""
    if not ai_assist_enabled():
        raise HTTPException(404, {'code': 'AI_ASSIST_OFF', 'message': 'AI features are turned off on this server.'})


def _user(request: Request):
    require_ai_assist()
    user = require_workspace_user(request, ROLES)
    if not user.get('tenant_id'):
        raise HTTPException(403, 'A company workspace is required.')
    return user


def _read_only(db):
    db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')


def _document_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise HTTPException(422, 'documentId must be a document id.') from None


class AcknowledgeInput(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=5)
    reason: str = Field(min_length=1, max_length=1000)


class DismissInput(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


def get_fraud(request: Request, documentId: str):
    user = _user(request)
    document_id = _document_id(documentId)
    with connect() as db:
        _read_only(db)
        result = fraud.document_flags(db, user, document_id)
    if result is None:
        raise HTTPException(404, 'Document not found.')
    return result


def get_fraud_summary(request: Request):
    user = _user(request)
    with connect() as db:
        _read_only(db)
        return fraud.summary(db, user)


def acknowledge_fraud(documentId: str, data: AcknowledgeInput, request: Request):
    user = _user(request)
    document_id = _document_id(documentId)
    reason = data.reason.strip()
    if len(reason) < 5:
        raise HTTPException(422, 'Give a reason of at least 5 characters for approving despite the warning.')
    codes = sorted(set(data.codes))
    with connect() as db:
        current = fraud.document_flags(db, user, document_id)
        if current is None:
            raise HTTPException(404, 'Document not found.')
        if not set(codes) <= {found['code'] for found in current['flags']}:
            raise HTTPException(422, 'Acknowledge only the warnings shown on this document.')
        row = db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING created_at""",
                         (user['tenant_id'], user['id'], user.get('name') or 'Workspace user', user['role'],
                          fraud.ACKNOWLEDGED, document_id, json.dumps({'codes': codes, 'reason': reason}))).fetchone()
    return {'documentId': document_id, 'codes': codes, 'reason': reason,
            'by': f"{user.get('name') or 'Workspace user'} ({user['role'].replace('_', ' ')})",
            'at': row['created_at'].isoformat()}


def get_inbox(request: Request):
    user = _user(request)
    with connect() as db:
        _read_only(db)
        return inbox.build_inbox(db, user)


def dismiss_inbox_item(item_id: str, request: Request, data: DismissInput | None = None):
    user = _user(request)
    if not inbox.ID_PATTERN.match(item_id or ''):
        raise HTTPException(422, 'An inbox item id is 20 hexadecimal characters.')
    with connect() as db:
        return inbox.dismiss(db, user, item_id, data.reason if data else None)


def register(router, handlers):
    flag = [Depends(require_ai_assist)]
    router.add_api_route('/api/insights/fraud/summary', get_fraud_summary, methods=['GET'], dependencies=flag)
    router.add_api_route('/api/insights/fraud', get_fraud, methods=['GET'], dependencies=flag)
    router.add_api_route('/api/insights/fraud/{documentId}/acknowledge', acknowledge_fraud, methods=['POST'],
                         status_code=201, dependencies=flag)
    router.add_api_route('/api/inbox', get_inbox, methods=['GET'], dependencies=flag)
    router.add_api_route('/api/inbox/{item_id}/dismiss', dismiss_inbox_item, methods=['POST'], dependencies=flag)
