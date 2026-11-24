"""Review lanes and bulk submit / approve (plan v3, task B5a; spec P3.1, P3.2).

GET  /api/review/lanes         ready / to_check / exceptions, with the reasons a document is not ready
POST /api/review/bulk-submit   accountant: submit many documents for approval
POST /api/review/bulk-approve  local admin: approve many submitted documents

Each document in a bulk request goes through the existing single-document function
(submit_document_review / decide_document_review / standardise_document) in its own transaction, so one
failure never blocks the rest and the approval rules live in one place. Before that call the document must be
`ready` (services/review/readiness.py); the single-document function then re-checks under its row lock.
Every document the caller can see gets one audit row, `review.bulk_submit` or `review.bulk_approve`, carrying
the shared bulk request id. A document the caller cannot see is reported as not found, and nothing is written.
Nothing here approves on its own: a person sends every bulk request.
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from main import ReviewDecisionInput, ReviewInput, connect, require_workspace_user
from services.review.readiness import (STAGE_STATUSES, _value, assess_documents, field_is_verified, lane_for, reason,
                                       summary_values)
from services.review.risk import rank, risk, spot_check
from services.routes.documents import routes as document_routes

logger = logging.getLogger(__name__)

BULK_LIMIT = 100
LANES = ('ready', 'to_check', 'exceptions')

# Same visibility as list_documents in services/routes/documents/routes.py (a unit test keeps them in sync).
ACCOUNTANT_DOCUMENT_VISIBILITY = ("(d.owner_id=%s OR d.assigned_to=%s OR EXISTS (SELECT 1 FROM benchmark_demo_documents bd "
                                  "JOIN benchmark_demo_tenants bt ON bt.tenant_id=bd.tenant_id AND bt.enabled=true "
                                  "WHERE bd.tenant_id=d.tenant_id AND bd.document_id=d.id))")
# Acting on a document needs ownership or assignment, as _lock_workspace_document in main.py requires.
ACCOUNTANT_ACTION_VISIBILITY = '(d.owner_id=%s OR d.assigned_to=%s)'

STATUS_TEXT = {'uploaded': 'still uploading', 'queued': 'waiting to be read', 'processing': 'being read',
               'validating': 'being checked', 'failed': 'not read successfully', 'extracted': 'waiting for your check',
               'corrected': 'waiting for your check', 'exception': 'waiting for your check',
               'in_review': 'waiting for approval', 'validated': 'already approved',
               'standardised': 'already saved to records', 'completed': 'already saved to records'}

ACTIONS = {
    'submit': {'roles': {'accountant'}, 'statuses': STAGE_STATUSES['submit'], 'audit': 'review.bulk_submit',
               'wrong_status': 'Only documents waiting for your check can be submitted. This one is {status}.'},
    'approve': {'roles': {'local_admin'}, 'statuses': STAGE_STATUSES['approve'], 'audit': 'review.bulk_approve',
                'wrong_status': 'Only documents waiting for approval can be approved. This one is {status}.'},
}


class BulkReviewItem(BaseModel):
    documentId: str = Field(min_length=1, max_length=64)
    contentRevision: int = Field(ge=1)


class BulkReviewInput(BaseModel):
    items: list[BulkReviewItem] = Field(min_length=1, max_length=BULK_LIMIT)
    note: str = Field(default='', max_length=4000)


class BulkApproveInput(BulkReviewInput):
    # Also save each approved document to records, through standardise_document. Off unless asked for.
    saveToRecords: bool = False


def _lane_item(document, reasons, fields):
    status = document['status']
    # Fields with a value a person has not confirmed (Please check / Suggested): the risk order counts them.
    unverified = sum(1 for row in fields if _value(row) is not None and not field_is_verified(row))
    return {'id': str(document['id']), 'code': document.get('document_code'), 'name': document.get('original_filename'),
            'kind': (document.get('kind') or '').upper(), 'category': (document.get('category') or '').upper(),
            'status': status.upper(), 'contentRevision': document['content_revision'],
            'nextAction': 'approve' if status in STAGE_STATUSES['approve'] else 'submit',
            **summary_values(document, fields), 'reasons': reasons, 'risk': risk(reasons, unverified),
            # Only a ready document can be picked: the others need a person's look anyway.
            'spotCheck': not reasons and spot_check(document['id'], document['content_revision'])}


def review_lanes(request: Request, stage: str | None = None):
    user = require_workspace_user(request, {'accountant', 'local_admin'})
    stage = stage or ('approve' if user['role'] == 'local_admin' else 'submit')
    if stage not in STAGE_STATUSES:
        raise HTTPException(422, 'stage must be submit or approve.')
    clauses = ['d.tenant_id=%s', 'd.status = ANY(%s::text[])', 'd.excluded_at IS NULL']
    params: list[object] = [user['tenant_id'], list(STAGE_STATUSES[stage])]
    if user['role'] == 'accountant':
        clauses.append(ACCOUNTANT_DOCUMENT_VISIBILITY)
        params.extend([user['id'], user['id']])
    approver = user['id'] if stage == 'approve' and user['role'] == 'local_admin' else None
    with connect() as db:
        documents = db.execute('SELECT d.* FROM documents d WHERE ' + ' AND '.join(clauses)
                               + ' ORDER BY d.uploaded_at DESC, d.id', params).fetchall()
        reasons, fields, policy = assess_documents(db, user['tenant_id'], documents, approver_id=approver)
    lanes = {lane: [] for lane in LANES}
    for document in documents:
        found = reasons[str(document['id'])]
        lanes[lane_for(found)].append(_lane_item(document, found, fields.get(str(document['id']), [])))
    # Most likely wrong first, with the reasons; a random spot-check heads the ready lane (roadmap P1-lite).
    lanes = {lane: rank(items) for lane, items in lanes.items()}
    return {'stage': stage, 'policy': {'readyLaneRequiresKnownSupplier': policy},
            'order': 'risk', 'spotCheckRate': '1 in 20 ready documents',
            'counts': {lane: len(items) for lane, items in lanes.items()}, 'lanes': lanes}


def _detail_text(exc):
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get('message') or detail)
    return str(detail)


def _reason_from_error(exc):
    text = _detail_text(exc)
    if exc.status_code == 404:
        return reason('not_found', 'Document not found.')
    if exc.status_code == 403:
        return reason('forbidden', text)
    if exc.status_code == 409:
        return reason('stale_revision' if 'changed' in text.lower() else 'wrong_status', text)
    return reason('not_ready', text)


def _audit(db, user, action, document_id, details):
    db.execute('INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details) VALUES (%s,%s,%s,%s,%s,%s,%s)',
               (user['tenant_id'], user['id'], user.get('name', 'Workspace user'), user['role'], action,
                str(document_id), json.dumps(details, default=str)))


def _skipped(document_id, found, reasons=None):
    item = {'documentId': document_id, 'outcome': 'skipped', 'reason': found}
    if reasons:
        item['reasons'] = reasons
    return item


def _precheck(db, user, action, document_id, item):
    """Read the document as the caller may act on it. Returns (document or None, skip result or None)."""
    clauses = 'd.id=%s AND d.tenant_id=%s'
    params: list[object] = [document_id, user['tenant_id']]
    if user['role'] == 'accountant':
        clauses += ' AND ' + ACCOUNTANT_ACTION_VISIBILITY
        params.extend([user['id'], user['id']])
    document = db.execute('SELECT d.* FROM documents d WHERE ' + clauses, params).fetchone()
    if not document:
        return None, _skipped(item.documentId, reason('not_found', 'Document not found.'))
    if document['content_revision'] != item.contentRevision:
        return document, _skipped(item.documentId, reason(
            'stale_revision', 'The document changed after you loaded the list. Refresh it and try again.',
            currentRevision=document['content_revision']))
    if document['status'] not in ACTIONS[action]['statuses']:
        return document, _skipped(item.documentId, reason(
            'wrong_status', ACTIONS[action]['wrong_status'].format(status=STATUS_TEXT.get(document['status'], document['status']))))
    approver = user['id'] if action == 'approve' else None
    reasons = assess_documents(db, user['tenant_id'], [document], approver_id=approver)[0][str(document['id'])]
    if reasons:
        return document, _skipped(item.documentId, reason('not_ready', 'This document is not ready: '
                                                          + ' '.join(r['message'] for r in reasons)), reasons)
    return document, None


def _run_action(request, action, document_id, item, data):
    if action == 'submit':
        return document_routes.submit_document_review(
            document_id, ReviewInput(expectedContentRevision=item.contentRevision, note=data.note), request)
    return document_routes.decide_document_review(
        document_id, ReviewDecisionInput(decision='approved', expectedContentRevision=item.contentRevision,
                                         note=data.note), request)


def _bulk_item(request, user, action, bulk_id, item, data, seen):
    try:
        document_id = str(uuid.UUID(item.documentId))
    except (ValueError, TypeError, AttributeError):
        return _skipped(item.documentId, reason('not_found', 'Document not found.'))
    if document_id in seen:
        return _skipped(item.documentId, reason('duplicate_in_request', 'This document is listed twice in the request.'))
    seen.add(document_id)
    audit = {'bulkRequestId': bulk_id, 'contentRevision': item.contentRevision}
    try:  # The pre-check is part of this document's boundary: a failure here skips only this document.
        with connect() as db:
            document, skipped = _precheck(db, user, action, document_id, item)
            if skipped:
                if document is not None:  # Not found writes nothing: the id may belong to another company.
                    _audit(db, user, ACTIONS[action]['audit'], document_id,
                           {**audit, 'outcome': 'skipped', 'reason': skipped['reason']['code']})
                return skipped
    except HTTPException as exc:
        return _skipped(item.documentId, _reason_from_error(exc))
    except Exception:  # noqa: BLE001 - rolled back with its connection; the rest of the batch still runs.
        logger.exception('Bulk %s pre-check failed for document %s', action, document_id)
        return _skipped(item.documentId, reason('action_failed', 'This document could not be checked. Try it on its own.'))
    try:
        outcome = _run_action(request, action, document_id, item, data)
        result = {'documentId': item.documentId, 'outcome': 'done', 'status': outcome['status']}
    except HTTPException as exc:
        result = _skipped(item.documentId, _reason_from_error(exc))
    except Exception:  # noqa: BLE001 - one document's failure must not stop the others.
        logger.exception('Bulk %s failed for document %s', action, document_id)
        result = _skipped(item.documentId, reason('action_failed', 'This document could not be processed. Try it on its own.'))
    if result['outcome'] == 'done' and action == 'approve' and getattr(data, 'saveToRecords', False):
        try:
            record = document_routes.standardise_document(document_id, request)
            result['record'] = {'saved': True, 'id': record['id']}
            result['status'] = record.get('status', result['status'])
        except HTTPException as exc:
            result['record'] = {'saved': False, 'reason': _detail_text(exc)}
        except Exception:  # noqa: BLE001
            logger.exception('Bulk save to records failed for document %s', document_id)
            result['record'] = {'saved': False, 'reason': 'The approved document could not be saved to records. Save it on its own.'}
    details = {**audit, 'outcome': result['outcome']}
    if result['outcome'] == 'skipped':
        details['reason'] = result['reason']['code']
    if 'record' in result:
        details['savedToRecords'] = result['record']['saved']
    try:
        with connect() as db:
            _audit(db, user, ACTIONS[action]['audit'], document_id, details)
        result['auditRecorded'] = True
    except Exception:  # noqa: BLE001 - the action's own audit row (document.submitted / approved) still stands.
        logger.exception('Bulk audit row failed for document %s', document_id)
        result['auditRecorded'] = False
    return result


def _bulk(request: Request, action: str, data: BulkReviewInput):
    user = require_workspace_user(request, ACTIONS[action]['roles'])
    bulk_id = str(uuid.uuid4())
    seen: set[str] = set()
    results = [_bulk_item(request, user, action, bulk_id, item, data, seen) for item in data.items]
    done = sum(r['outcome'] == 'done' for r in results)
    return {'bulkRequestId': bulk_id, 'action': action, 'requested': len(results), 'done': done,
            'skipped': len(results) - done, 'results': results}


def bulk_submit(data: BulkReviewInput, request: Request):
    return _bulk(request, 'submit', data)


def bulk_approve(data: BulkApproveInput, request: Request):
    return _bulk(request, 'approve', data)


def register(router):
    from services.routes.idempotency import idempotent  # Idempotency-Key retries
    router.add_api_route('/api/review/lanes', review_lanes, methods=['GET'])
    router.add_api_route('/api/review/bulk-submit', idempotent(bulk_submit), methods=['POST'])
    router.add_api_route('/api/review/bulk-approve', idempotent(bulk_approve), methods=['POST'])
