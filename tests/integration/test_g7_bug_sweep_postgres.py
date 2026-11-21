"""G7 bug sweep on the full schema in the disposable loopback TEST_DATABASE_URL.

Assignment: assigned work reaches the assignee's Documents list and Today counts; reassignment
and unassignment take it away; foreign targets and accountants are refused without writes.
Bulk read-again: only FAILED documents the caller may see are queued, within the attempt
budget, one audit row each, and another company's documents are never touched.
Display names: the list shows "<supplier> <number>" and keeps the original filename.
"""
import hashlib
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row

import main  # noqa: F401 - initialise the application's route registry before route imports.
from main import BulkActionInput, WorkloadInput
from services.review import reread_failed
from services.review.reread_failed import ReadAgainInput
from services.routes.documents import routes as documents
from services.routes.home import routes as home

SCHEMA_FILE = Path(__file__).resolve().parents[2] / 'database' / 'PostgreSQL Database schema.sql'
TODAY = date(2026, 11, 19)


def _hash():
    return hashlib.sha256(uuid4().bytes).hexdigest()


@pytest.fixture()
def company():
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'g7_sweep_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute('CREATE DATABASE ' + name)
    target = parts._replace(path='/' + name).geturl()

    def connect():
        return psycopg.connect(target, row_factory=dict_row)

    try:
        with connect() as db:
            db.execute(SCHEMA_FILE.read_text(encoding='utf-8-sig'))
        yield connect, _seed(connect)
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


def _seed(connect):
    ids = {}
    with connect() as db:
        def tenant(label):
            return db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,%s) RETURNING id",
                              (label, label + '-REG', label.lower().replace(' ', '.') + '@example.test')).fetchone()['id']

        def user(tenant_id, role, label):
            return db.execute("INSERT INTO users(tenant_id,name,email,role,status) VALUES (%s,%s,%s,%s,'active') RETURNING id",
                              (tenant_id, label, label.lower().replace(' ', '.') + '@example.test', role)).fetchone()['id']

        def document(tenant_id, owner, status, attempts_used=None):
            row = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,
                    mime_type,file_size_bytes,file_hash_sha256,status,uploaded_at)
                VALUES (%s,%s,'images (2).png','bill','accounts_payable','synthetic/none.png','image/png',100,%s,%s,%s)
                RETURNING id""", (tenant_id, owner, _hash(), status, TODAY)).fetchone()['id']
            for used in attempts_used or ():
                db.execute("""INSERT INTO pending_jobs(tenant_id,document_id,status,attempt_count,last_error,finished_at)
                    VALUES (%s,%s,'failed',%s,'EXTRACTION_FAILED',now())""", (tenant_id, row, used))
            return row

        a, b = tenant('Synthetic Trading A'), tenant('Synthetic Trading B')
        admin, acct, acct2 = user(a, 'local_admin', 'Admin A'), user(a, 'accountant', 'Accountant One'), user(a, 'accountant', 'Accountant Two')
        admin_b = user(b, 'local_admin', 'Admin B')
        ids.update(tenant=a, tenant_b=b, admin=admin, acct=acct, acct2=acct2, admin_b=admin_b)
        ids['work'] = [document(a, admin, 'extracted') for _ in range(5)]
        ids['failed'] = [document(a, admin, 'failed', [1]) for _ in range(3)]
        ids['failed_mine'] = document(a, acct, 'failed', [1])
        # The old engine used all three worker claims: that does not stop a fresh read.
        ids['spent'] = document(a, admin, 'failed', [1, 1, 1])
        # Already read again three times in bulk: the read-again budget is used.
        ids['budget'] = document(a, admin, 'failed', [1])
        for _ in range(3):
            db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
                VALUES (%s,%s,'Admin A','local_admin','extraction.read_again_requested',%s,'{}')""", (a, admin, str(ids['budget'])))
        ids['failed_b'] = document(b, admin_b, 'failed', [1])
    return ids


def _as(user_id, tenant_id, role):
    # As session_user returns them: UUID values, not text.
    return {'id': user_id, 'tenant_id': tenant_id, 'role': role, 'name': role}


def _call(connect, user, function, *args, **kwargs):
    with patch.object(documents, 'require_workspace_user', return_value=user), patch.object(documents, 'connect', connect):
        return function(*args, **kwargs)


def _listing(connect, user):
    return {row['id']: row for row in _call(connect, user, documents.list_documents, None)}


def _to_review(connect, user):
    with patch.object(home, 'require_workspace_user', return_value=user), patch.object(home, 'connect', connect):
        return home.home_summary(None, asOf=TODAY)['tiles']['toReview']['count']


def _one(connect, sql, params=()):
    with connect() as db:
        return db.execute(sql, params).fetchone()


def test_bulk_assignment_shows_documents_to_the_assignee_and_on_today(company):
    connect, ids = company
    admin, acct = _as(ids['admin'], ids['tenant'], 'local_admin'), _as(ids['acct'], ids['tenant'], 'accountant')
    work = [str(i) for i in ids['work']]
    assert not set(_listing(connect, acct)) & set(work)
    before = _to_review(connect, acct)
    result = _call(connect, admin, documents.bulk_document_action,
                   BulkActionInput(documentIds=work, action='assign', userId=str(ids['acct'])), None)
    assert result == {'updated': 5, 'action': 'assign'}
    assert set(work) <= set(_listing(connect, acct))
    assert _to_review(connect, acct) == before + 5
    assert _one(connect, "SELECT count(*) AS n FROM documents WHERE id=ANY(%s::uuid[]) AND assigned_to=%s", (work, ids['acct']))['n'] == 5
    assert _one(connect, "SELECT count(*) AS n FROM workload_assignments WHERE assigned_to=%s", (ids['acct'],))['n'] == 5
    assert _one(connect, "SELECT count(*) AS n FROM audit_logs WHERE action='document.assigned'")['n'] == 5
    assert _one(connect, "SELECT count(*) AS n FROM notifications WHERE user_id=%s AND type='document_assigned'", (ids['acct'],))['n'] == 5


def test_reassignment_and_unassignment_take_the_document_away(company):
    connect, ids = company
    admin = _as(ids['admin'], ids['tenant'], 'local_admin')
    acct, acct2 = _as(ids['acct'], ids['tenant'], 'accountant'), _as(ids['acct2'], ids['tenant'], 'accountant')
    document = str(ids['work'][0])
    _call(connect, admin, documents.assign_workload, document, WorkloadInput(userId=str(ids['acct'])), None)
    moved = _call(connect, admin, documents.assign_workload, document, WorkloadInput(userId=str(ids['acct2']), priority='high'), None)
    assert moved['assignedTo'] == str(ids['acct2']) and moved['previousAssignedTo'] == str(ids['acct'])
    assert document not in _listing(connect, acct) and document in _listing(connect, acct2)
    assert _one(connect, "SELECT count(*) AS n FROM notifications WHERE user_id=%s AND title='Document reassigned'", (ids['acct'],))['n'] == 1
    removed = _call(connect, admin, documents.unassign_workload, document, None)
    assert removed == {'documentId': document, 'assignedTo': None, 'previousAssignedTo': str(ids['acct2'])}
    assert document not in _listing(connect, acct2)
    assert _one(connect, "SELECT assigned_to FROM documents WHERE id=%s", (document,))['assigned_to'] is None
    assert _one(connect, "SELECT count(*) AS n FROM workload_assignments WHERE document_id=%s", (document,))['n'] == 0
    with connect() as db:
        actions = [row['action'] for row in db.execute(
            "SELECT action FROM audit_logs WHERE resource=%s ORDER BY created_at", (document,)).fetchall()]
    assert actions == ['document.assigned', 'document.assigned', 'document.unassigned']


def test_reassignment_keeps_priority_and_due_date_unless_sent(company):
    connect, ids = company
    admin = _as(ids['admin'], ids['tenant'], 'local_admin')
    document, due = str(ids['work'][0]), datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)

    def scheduling():
        row = _one(connect, "SELECT priority, due_at FROM workload_assignments WHERE document_id=%s", (document,))
        return row['priority'], row['due_at']

    _call(connect, admin, documents.assign_workload, document,
          WorkloadInput(userId=str(ids['acct']), priority='urgent', dueAt=due), None)
    # Bulk reassignment sends no scheduling: the existing priority and due date stay.
    _call(connect, admin, documents.bulk_document_action,
          BulkActionInput(documentIds=[document], action='assign', userId=str(ids['acct2'])), None)
    assert scheduling() == ('urgent', due)
    assert _one(connect, "SELECT assigned_to FROM documents WHERE id=%s", (document,))['assigned_to'] == ids['acct2']
    # A single reassignment keeps what it does not send, and changes what it does.
    kept = _call(connect, admin, documents.assign_workload, document, WorkloadInput(userId=str(ids['acct'])), None)
    assert (kept['priority'], scheduling()) == ('urgent', ('urgent', due))
    _call(connect, admin, documents.assign_workload, document, WorkloadInput(userId=str(ids['acct']), dueAt=None), None)
    assert scheduling() == ('urgent', None)
    _call(connect, admin, documents.assign_workload, document, WorkloadInput(userId=str(ids['acct']), priority='low'), None)
    assert scheduling() == ('low', None)
    # A first bulk assignment gets the defaults.
    fresh = str(ids['work'][1])
    _call(connect, admin, documents.bulk_document_action,
          BulkActionInput(documentIds=[fresh], action='assign', userId=str(ids['acct'])), None)
    assert _one(connect, "SELECT priority, due_at FROM workload_assignments WHERE document_id=%s", (fresh,)) ==         {'priority': 'normal', 'due_at': None}


def test_foreign_target_and_accountant_assignment_are_refused_without_writes(company):
    connect, ids = company
    admin, acct = _as(ids['admin'], ids['tenant'], 'local_admin'), _as(ids['acct'], ids['tenant'], 'accountant')
    work = [str(i) for i in ids['work']]
    with pytest.raises(HTTPException) as foreign:
        _call(connect, admin, documents.bulk_document_action,
              BulkActionInput(documentIds=work, action='assign', userId=str(ids['admin_b'])), None)
    assert foreign.value.status_code == 422
    with pytest.raises(HTTPException) as denied:
        _call(connect, acct, documents.bulk_document_action,
              BulkActionInput(documentIds=work, action='assign', userId=str(ids['acct'])), None)
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as other_company:
        _call(connect, _as(ids['admin_b'], ids['tenant_b'], 'local_admin'), documents.assign_workload,
              work[0], WorkloadInput(userId=str(ids['admin_b'])), None)
    assert other_company.value.status_code == 404
    assert _one(connect, "SELECT count(*) AS n FROM documents WHERE assigned_to IS NOT NULL")['n'] == 0
    assert _one(connect, "SELECT count(*) AS n FROM workload_assignments")['n'] == 0
    assert _one(connect, "SELECT count(*) AS n FROM audit_logs WHERE action LIKE 'document.%%assigned'")['n'] == 0


def _read_again(connect, user, **body):
    with patch.object(reread_failed, 'connect', connect), patch.object(reread_failed, '_wake_dispatcher') as wake:
        result = reread_failed.read_again(user, ReadAgainInput(**body))
    return result, wake


def test_local_admin_reads_three_failed_documents_again_in_one_action(company):
    connect, ids = company
    admin = _as(ids['admin'], ids['tenant'], 'local_admin')
    failed = [str(i) for i in ids['failed']]
    result, wake = _read_again(connect, admin, documentIds=failed)
    assert (result['requested'], result['queued'], result['skipped']) == (3, 3, 0)
    assert [r['id'] for r in result['results']] == failed and {r['outcome'] for r in result['results']} == {'queued'}
    wake.assert_called_once()
    assert _one(connect, "SELECT count(*) AS n FROM documents WHERE id=ANY(%s::uuid[]) AND status='queued'", (failed,))['n'] == 3
    # Earlier attempts are kept; each document gets one new pending attempt and one user audit row.
    assert _one(connect, "SELECT count(*) AS n FROM pending_jobs WHERE document_id=ANY(%s::uuid[]) AND status='pending'", (failed,))['n'] == 3
    assert _one(connect, "SELECT count(*) AS n FROM pending_jobs WHERE document_id=ANY(%s::uuid[]) AND status='failed'", (failed,))['n'] == 3
    assert _one(connect, """SELECT count(*) AS n FROM audit_logs WHERE action='extraction.read_again_requested'
        AND user_id=%s AND resource=ANY(%s)""", (ids['admin'], failed))['n'] == 3
    # Asking again while they are queued changes nothing.
    again, _ = _read_again(connect, admin, documentIds=failed)
    assert again['queued'] == 0 and {r['reasonCode'] for r in again['results']} == {'NOT_FAILED'}


def test_partial_selection_reports_each_document_and_touches_nothing_else(company):
    connect, ids = company
    admin = _as(ids['admin'], ids['tenant'], 'local_admin')
    body = [str(ids['failed'][0]), str(ids['work'][0]), str(ids['spent']), str(ids['budget']), str(ids['failed_b']), 'not-a-uuid']
    result, _ = _read_again(connect, admin, documentIds=body)
    outcome = {r['id']: (r['outcome'], r.get('reasonCode')) for r in result['results']}
    assert outcome == {str(ids['failed'][0]): ('queued', None), str(ids['work'][0]): ('skipped', 'NOT_FAILED'),
                       str(ids['spent']): ('queued', None), str(ids['budget']): ('skipped', 'ATTEMPT_BUDGET_USED'),
                       str(ids['failed_b']): ('skipped', 'NOT_FOUND'), 'not-a-uuid': ('skipped', 'NOT_FOUND')}
    assert (result['queued'], result['skipped']) == (2, 4)
    for untouched, status in ((ids['budget'], 'failed'), (ids['failed_b'], 'failed'), (ids['work'][0], 'extracted')):
        assert _one(connect, "SELECT status FROM documents WHERE id=%s", (untouched,))['status'] == status
    assert _one(connect, "SELECT count(*) AS n FROM pending_jobs WHERE document_id=ANY(%s::uuid[]) AND status='pending'",
                ([str(ids['budget']), str(ids['failed_b'])],))['n'] == 0


def test_all_failed_reaches_newer_failures_when_the_oldest_used_their_budget(company):
    connect, ids = company
    admin = _as(ids['admin'], ids['tenant'], 'local_admin')
    oldest = [ids['failed'][0], ids['failed'][1]]
    with connect() as db:
        # The two oldest failures were already read again three times each.
        db.execute("UPDATE documents SET uploaded_at=%s WHERE id=ANY(%s::uuid[])", (TODAY - timedelta(days=30), oldest))
        for document in oldest:
            for _ in range(3):
                db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
                    VALUES (%s,%s,'Admin A','local_admin','extraction.read_again_requested',%s,'{}')""",
                           (ids['tenant'], ids['admin'], str(document)))
    # A limit of one: before the fix the oldest (spent) failure filled it and nothing was queued.
    with patch.object(reread_failed, 'MAX_DOCUMENTS', 1):
        result, _ = _read_again(connect, admin, allFailed=True)
    assert result['queued'] == 1 and len(result['results']) == 1
    assert result['results'][0]['id'] not in {str(d) for d in oldest + [ids['budget']]}
    assert result['budgetUsed'] == 3      # the two oldest and the seeded 'budget' document
    for document in oldest:
        assert _one(connect, "SELECT status FROM documents WHERE id=%s", (document,))['status'] == 'failed'


def test_accountant_reads_again_only_their_own_or_assigned_failed_documents(company):
    connect, ids = company
    admin, acct = _as(ids['admin'], ids['tenant'], 'local_admin'), _as(ids['acct'], ids['tenant'], 'accountant')
    _call(connect, admin, documents.assign_workload, str(ids['failed'][1]), WorkloadInput(userId=str(ids['acct'])), None)
    result, _ = _read_again(connect, acct, allFailed=True)
    assert {r['id'] for r in result['results'] if r['outcome'] == 'queued'} == {str(ids['failed_mine']), str(ids['failed'][1])}
    assert result['queued'] == len(result['results']) == 2
    # The admin's own failed documents are not the accountant's to queue, even by id.
    selected, _ = _read_again(connect, acct, documentIds=[str(ids['failed'][0])])
    assert selected['results'][0]['reasonCode'] == 'NOT_FOUND'
    assert _one(connect, "SELECT status FROM documents WHERE id=%s", (ids['failed'][0],))['status'] == 'failed'
    with pytest.raises(HTTPException) as denied:
        _read_again(connect, {'id': str(uuid4()), 'tenant_id': None, 'role': 'super_admin'}, allFailed=True)
    assert denied.value.status_code == 403


def test_document_list_shows_supplier_and_number_and_keeps_the_filename(company):
    connect, ids = company
    admin = _as(ids['admin'], ids['tenant'], 'local_admin')
    document = ids['work'][0]
    with connect() as db:
        extraction = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
            VALUES (%s,1,true,'parser_only','{}') RETURNING id""", (document,)).fetchone()['id']
        for key, value in (('supplier', 'Syarikat Maju Jaya Sdn Bhd'), ('invoice', 'INV-26-03498')):
            db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,extraction_state,
                    raw_value,normalized_value) VALUES (%s,%s,%s,%s,'resolved',%s,%s)""", (extraction, document, key, key, value, value))
    rows = _listing(connect, admin)
    assert rows[str(document)]['name'] == 'Syarikat Maju Jaya Sdn Bhd INV-26-03498'
    assert rows[str(document)]['originalFilename'] == 'images (2).png'
    # Nothing read yet: the original filename stays.
    assert rows[str(ids['work'][1])]['name'] == 'images (2).png'


def test_read_again_uses_the_reread_path_and_refuses_review_history(company):
    connect, ids = company
    admin = _as(ids['admin'], ids['tenant'], 'local_admin')
    reviewed, fresh = str(ids['failed'][0]), str(ids['failed'][1])
    with connect() as db:
        db.execute("""INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision)
            VALUES (%s,%s,%s,'submitted',1)""", (reviewed, ids['tenant'], ids['admin']))
    result, _ = _read_again(connect, admin, documentIds=[reviewed, fresh])
    outcome = {r['id']: (r['outcome'], r.get('reasonCode')) for r in result['results']}
    assert outcome == {reviewed: ('skipped', 'REVIEW_HISTORY'), fresh: ('queued', None)}
    assert _one(connect, "SELECT status FROM documents WHERE id=%s", (reviewed,))['status'] == 'failed'
    # #14's schedule_reread audits the system side; read-again adds the user's row.
    with connect() as db:
        actions = [r['action'] for r in db.execute(
            "SELECT action FROM audit_logs WHERE resource=%s ORDER BY created_at", (fresh,)).fetchall()]
    assert actions == ['extraction.reread_scheduled', 'extraction.read_again_requested']


def test_undetected_auto_upload_lists_with_readable_name_and_kind_auto(company):
    from services.extraction.persistence.auto_kind import pending_document_code
    connect, ids = company
    admin = _as(ids['admin'], ids['tenant'], 'local_admin')
    with connect() as db:
        def auto_upload():
            document_id = uuid4()
            # As upload_document(kind=auto) stores it: placeholder kind and a DOC_ code until the type is detected.
            db.execute("""INSERT INTO documents(id,tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,
                    file_size_bytes,file_hash_sha256,status,uploaded_at,document_code)
                VALUES (%s,%s,%s,'images (3).png','invoice','accounts_payable','synthetic/none.png','image/png',100,%s,
                    'exception',%s,%s)""",
                       (document_id, ids['tenant'], ids['admin'], _hash(), TODAY, pending_document_code(document_id)))
            return document_id

        read, unread = auto_upload(), auto_upload()
        extraction = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
            VALUES (%s,1,true,'parser_only','{}') RETURNING id""", (read,)).fetchone()['id']
        for key, value in (('supplier', 'Kedai Runcit Ah Seng'), ('receipt', 'CS054592')):
            db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,extraction_state,
                    raw_value,normalized_value) VALUES (%s,%s,%s,%s,'resolved',%s,%s)""", (extraction, read, key, key, value, value))
    rows = _listing(connect, admin)
    assert {k: rows[str(read)][k] for k in ('name', 'originalFilename', 'kind', 'kindDetected')} == \
        {'name': 'Kedai Runcit Ah Seng CS054592', 'originalFilename': 'images (3).png', 'kind': 'AUTO', 'kindDetected': False}
    assert {k: rows[str(unread)][k] for k in ('name', 'kind', 'kindDetected')} == \
        {'name': 'images (3).png', 'kind': 'AUTO', 'kindDetected': False}
    # A typed document keeps its stored kind alongside the readable name.
    assert (rows[str(ids['work'][0])]['kind'], rows[str(ids['work'][0])]['kindDetected']) == ('BILL', True)
