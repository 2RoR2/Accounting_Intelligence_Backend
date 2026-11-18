"""Lanes endpoint and bulk submit / approve orchestration (task B5a, spec P3.1-P3.2), on an in-memory fake.

The single-document functions and the readiness SQL are covered in
tests/integration/test_review_lanes_postgres.py; here they are stubbed so each bulk rule is isolated.
"""
import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import main  # noqa: F401 - initialise the route registry before importing route modules.
from services.routes.review import lanes

TENANT, OTHER_TENANT = str(uuid4()), str(uuid4())
ACCOUNTANT = {'id': str(uuid4()), 'tenant_id': TENANT, 'role': 'accountant', 'name': 'Synthetic accountant'}
ADMIN = {'id': str(uuid4()), 'tenant_id': TENANT, 'role': 'local_admin', 'name': 'Synthetic admin'}
SUPER = {'id': str(uuid4()), 'tenant_id': None, 'role': 'super_admin', 'name': 'Platform'}


def doc(status='extracted', revision=3, tenant=TENANT, owner=ACCOUNTANT['id']):
    return {'id': str(uuid4()), 'tenant_id': tenant, 'owner_id': owner, 'assigned_to': None, 'status': status,
            'content_revision': revision, 'kind': 'receipt', 'category': 'accounts_payable',
            'document_code': 'DOC', 'original_filename': 'receipt.pdf'}


class FakeDB:
    def __init__(self, documents):
        self.documents = {d['id']: d for d in documents}
        self.audit = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.rows = []
        if sql.startswith('INSERT INTO audit_logs'):
            self.audit.append({'resource': params[5], 'action': params[4], **json.loads(params[6])})
        elif 'WHERE d.id=%s AND d.tenant_id=%s' in sql:
            found = self.documents.get(params[0])
            visible = found and found['tenant_id'] == params[1] and (
                len(params) == 2 or params[2] in (found['owner_id'], found['assigned_to']))
            self.rows = [found] if visible else []
        elif 'WHERE d.tenant_id=%s AND d.status = ANY' in sql:
            self.rows = [d for d in self.documents.values() if d['tenant_id'] == params[0] and d['status'] in params[1]
                         and (len(params) == 2 or params[2] in (d['owner_id'], d['assigned_to']))]
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


def acting_as(user):
    def require(request, roles=None):
        if roles and user['role'] not in roles:
            raise HTTPException(403, 'You do not have permission for this workspace.')
        return user
    return require


@pytest.fixture
def world():
    """A fake DB, a configurable readiness map and recorded single-document calls."""
    state = {'reasons': {}, 'calls': [], 'fail': {}}

    def assess(db, tenant_id, documents, approver_id=None):
        for d in documents:
            if d['id'] in state.get('assess_fail', ()):
                raise RuntimeError('readiness query failed')
        return ({d['id']: state['reasons'].get(d['id'], []) for d in documents}, {}, True)

    def single(name, status):
        def run(document_id, data, request):
            state['calls'].append((name, document_id, data.expectedContentRevision))
            if document_id in state['fail']:
                raise state['fail'][document_id]
            return {'documentId': document_id, 'status': status}
        return run

    def build(documents, user):
        db = FakeDB(documents)
        patches = [patch.object(lanes, 'connect', return_value=db),
                   patch.object(lanes, 'require_workspace_user', side_effect=acting_as(user)),
                   patch.object(lanes, 'assess_documents', side_effect=assess),
                   patch.object(lanes.document_routes, 'submit_document_review', side_effect=single('submit', 'IN_REVIEW')),
                   patch.object(lanes.document_routes, 'decide_document_review', side_effect=single('approve', 'VALIDATED'))]
        for p in patches:
            p.start()
        state['db'] = db
        return db
    yield state, build
    patch.stopall()


def body(*documents, revision=None, cls=lanes.BulkReviewInput, **extra):
    return cls(items=[{'documentId': d['id'], 'contentRevision': revision or d['content_revision']} for d in documents], **extra)


def outcomes(response):
    return [(r['outcome'], (r.get('reason') or {}).get('code')) for r in response['results']]


def test_bulk_submit_runs_each_ready_document_through_the_single_submit(world):
    state, build = world
    docs = [doc(), doc(), doc()]
    db = build(docs, ACCOUNTANT)
    response = lanes.bulk_submit(body(*docs), None)
    assert response['done'] == 3 and response['skipped'] == 0 and response['requested'] == 3
    assert [c[:2] for c in state['calls']] == [('submit', d['id']) for d in docs]
    assert {a['bulkRequestId'] for a in db.audit} == {response['bulkRequestId']}
    assert [a['resource'] for a in db.audit] == [d['id'] for d in docs]
    assert {a['action'] for a in db.audit} == {'review.bulk_submit'} and all(r['auditRecorded'] for r in response['results'])


def test_revision_mismatch_is_skipped_before_the_single_function(world):
    state, build = world
    fresh, stale = doc(), doc(revision=5)
    db = build([fresh, stale], ACCOUNTANT)
    response = lanes.bulk_submit(lanes.BulkReviewInput(items=[
        {'documentId': fresh['id'], 'contentRevision': 3}, {'documentId': stale['id'], 'contentRevision': 4}]), None)
    assert outcomes(response) == [('done', None), ('skipped', 'stale_revision')]
    assert response['results'][1]['reason']['details']['currentRevision'] == 5
    assert [c[1] for c in state['calls']] == [fresh['id']]
    assert [(a['resource'], a['outcome'], a.get('reason')) for a in db.audit] == [
        (fresh['id'], 'done', None), (stale['id'], 'skipped', 'stale_revision')]


def test_non_ready_document_is_skipped_with_its_reasons(world):
    state, build = world
    ready, unready = doc(), doc()
    build([ready, unready], ACCOUNTANT)
    state['reasons'][unready['id']] = [{'code': 'field_needs_check', 'message': 'Check against the document: total.'}]
    response = lanes.bulk_submit(body(ready, unready), None)
    assert outcomes(response) == [('done', None), ('skipped', 'not_ready')]
    assert response['results'][1]['reasons'][0]['code'] == 'field_needs_check'
    assert 'total' in response['results'][1]['reason']['message']
    assert [c[1] for c in state['calls']] == [ready['id']]


def test_one_failure_never_blocks_the_rest(world):
    state, build = world
    docs = [doc(status='in_review') for _ in range(4)]
    build(docs, ADMIN)
    state['fail'][docs[1]['id']] = HTTPException(422, 'Resolve the current validation findings and revalidate before submission or approval.')
    state['fail'][docs[2]['id']] = RuntimeError('database went away')
    response = lanes.bulk_approve(body(*docs, cls=lanes.BulkApproveInput), None)
    assert outcomes(response) == [('done', None), ('skipped', 'not_ready'), ('skipped', 'action_failed'), ('done', None)]
    assert response['done'] == 2 and response['skipped'] == 2
    assert 'database' not in response['results'][2]['reason']['message']  # No internal error text to the user.
    assert [c[0] for c in state['calls']] == ['approve'] * 4


def test_a_failing_precheck_on_item_two_of_three_skips_only_that_item(world):
    state, build = world
    docs = [doc(), doc(), doc()]
    db = build(docs, ACCOUNTANT)
    state['assess_fail'] = {docs[1]['id']}
    response = lanes.bulk_submit(body(*docs), None)
    assert outcomes(response) == [('done', None), ('skipped', 'action_failed'), ('done', None)]
    assert 'readiness' not in response['results'][1]['reason']['message']
    assert [c[1] for c in state['calls']] == [docs[0]['id'], docs[2]['id']]
    assert [a['resource'] for a in db.audit] == [docs[0]['id'], docs[2]['id']]


def test_a_failing_precheck_on_item_two_of_three_skips_only_that_item(world):
    state, build = world
    docs = [doc(), doc(), doc()]
    db = build(docs, ACCOUNTANT)
    state['assess_fail'] = {docs[1]['id']}
    response = lanes.bulk_submit(body(*docs), None)
    assert outcomes(response) == [('done', None), ('skipped', 'action_failed'), ('done', None)]
    assert 'readiness' not in response['results'][1]['reason']['message']
    assert [c[1] for c in state['calls']] == [docs[0]['id'], docs[2]['id']]
    assert [a['resource'] for a in db.audit] == [docs[0]['id'], docs[2]['id']]


def test_single_function_conflicts_map_to_named_reasons(world):
    state, build = world
    docs = [doc(status='in_review') for _ in range(2)]
    build(docs, ADMIN)
    state['fail'][docs[0]['id']] = HTTPException(409, 'The document changed. Refresh it before deciding this review.')
    state['fail'][docs[1]['id']] = HTTPException(422, 'The document has not been submitted for approval.')
    assert outcomes(lanes.bulk_approve(body(*docs, cls=lanes.BulkApproveInput), None)) == [
        ('skipped', 'stale_revision'), ('skipped', 'not_ready')]


def test_wrong_status_for_the_action_is_skipped(world):
    state, build = world
    waiting, approved = doc(status='in_review'), doc(status='validated')
    build([waiting, approved], ACCOUNTANT)
    response = lanes.bulk_submit(body(waiting, approved), None)
    assert outcomes(response) == [('skipped', 'wrong_status'), ('skipped', 'wrong_status')]
    assert 'waiting for approval' in response['results'][0]['reason']['message']
    assert state['calls'] == []


@pytest.mark.parametrize('endpoint, user', [('bulk_submit', ADMIN), ('bulk_submit', SUPER),
                                            ('bulk_approve', ACCOUNTANT), ('bulk_approve', SUPER)])
def test_wrong_role_is_refused_before_anything_runs(world, endpoint, user):
    state, build = world
    document = doc(status='in_review' if endpoint == 'bulk_approve' else 'extracted')
    db = build([document], user)
    with pytest.raises(HTTPException) as refused:
        getattr(lanes, endpoint)(body(document, cls=lanes.BulkApproveInput), None)
    assert refused.value.status_code == 403
    assert state['calls'] == [] and db.audit == []


def test_another_companys_document_is_not_found_and_nothing_is_written(world):
    state, build = world
    mine, theirs = doc(), doc(tenant=OTHER_TENANT, owner=str(uuid4()))
    db = build([mine, theirs], ACCOUNTANT)
    response = lanes.bulk_submit(body(mine, theirs, {'id': 'not-a-uuid', 'content_revision': 1}), None)
    assert outcomes(response) == [('done', None), ('skipped', 'not_found'), ('skipped', 'not_found')]
    assert [c[1] for c in state['calls']] == [mine['id']]
    assert [a['resource'] for a in db.audit] == [mine['id']]  # No row names the other company's document.
    assert theirs['status'] == 'extracted'


def test_an_accountant_cannot_act_on_a_colleagues_document(world):
    state, build = world
    colleague = doc(owner=str(uuid4()))
    db = build([colleague], ACCOUNTANT)
    assert outcomes(lanes.bulk_submit(body(colleague), None)) == [('skipped', 'not_found')]
    assert state['calls'] == [] and db.audit == []


def test_a_document_listed_twice_runs_once(world):
    state, build = world
    document = doc()
    build([document], ACCOUNTANT)
    response = lanes.bulk_submit(body(document, document), None)
    assert outcomes(response) == [('done', None), ('skipped', 'duplicate_in_request')]
    assert len(state['calls']) == 1


def test_batch_size_is_capped():
    items = [{'documentId': str(uuid4()), 'contentRevision': 1} for _ in range(lanes.BULK_LIMIT + 1)]
    with pytest.raises(ValidationError):
        lanes.BulkReviewInput(items=items)
    with pytest.raises(ValidationError):
        lanes.BulkReviewInput(items=[])
    assert len(lanes.BulkReviewInput(items=items[:lanes.BULK_LIMIT]).items) == lanes.BULK_LIMIT


def test_save_to_records_runs_standardise_only_when_asked(world):
    state, build = world
    docs = [doc(status='in_review'), doc(status='in_review'), doc(status='in_review')]
    build(docs, ADMIN)
    with patch.object(lanes.document_routes, 'standardise_document',
                      side_effect=[{'id': 'r1', 'status': 'COMPLETED'}, HTTPException(422, 'Missing required receipt fields: total.')]) as save:
        response = lanes.bulk_approve(body(*docs[:2], cls=lanes.BulkApproveInput, saveToRecords=True), None)
        assert save.call_count == 2
        assert lanes.bulk_approve(body(docs[2], cls=lanes.BulkApproveInput), None)['done'] == 1
        assert save.call_count == 2  # Not asked for: nothing saved.
    assert [r['record'] for r in response['results']] == [
        {'saved': True, 'id': 'r1'}, {'saved': False, 'reason': 'Missing required receipt fields: total.'}]
    assert outcomes(response) == [('done', None), ('done', None)]  # The approval stands when saving fails.


def test_lanes_counts_equal_lists_and_follow_reasons(world):
    state, build = world
    ready, check, blocked, waiting = doc(), doc(status='corrected'), doc(status='exception'), doc(status='in_review')
    build([ready, check, blocked, waiting], ACCOUNTANT)
    state['reasons'][check['id']] = [{'code': 'field_missing', 'message': 'Fill in: total.'}]
    state['reasons'][blocked['id']] = [{'code': 'open_exception', 'message': 'x'}]
    response = lanes.review_lanes(None)
    assert response['stage'] == 'submit'
    assert response['counts'] == {lane: len(items) for lane, items in response['lanes'].items()}
    assert [[i['id'] for i in response['lanes'][lane]] for lane in lanes.LANES] == [[ready['id']], [check['id']], [blocked['id']]]
    assert response['lanes']['to_check'][0]['reasons'][0]['code'] == 'field_missing'
    assert response['lanes']['ready'][0]['nextAction'] == 'submit'
    assert response['policy'] == {'readyLaneRequiresKnownSupplier': True}


def test_admin_lanes_default_to_documents_waiting_for_approval(world):
    state, build = world
    waiting, draft = doc(status='in_review'), doc()
    build([waiting, draft], ADMIN)
    response = lanes.review_lanes(None)
    assert response['stage'] == 'approve' and response['counts']['ready'] == 1
    assert response['lanes']['ready'][0]['nextAction'] == 'approve'
    assert lanes.review_lanes(None, stage='submit')['lanes']['ready'][0]['id'] == draft['id']
    with pytest.raises(HTTPException) as bad:
        lanes.review_lanes(None, stage='everything')
    assert bad.value.status_code == 422


def test_lanes_refuse_the_platform_super_admin(world):
    _, build = world
    build([doc()], SUPER)
    with pytest.raises(HTTPException) as refused:
        lanes.review_lanes(None)
    assert refused.value.status_code == 403


def test_accountant_lanes_hide_other_accountants_documents(world):
    _, build = world
    mine, colleague = doc(), doc(owner=str(uuid4()))
    build([mine, colleague], ACCOUNTANT)
    assert [i['id'] for i in lanes.review_lanes(None)['lanes']['ready']] == [mine['id']]
