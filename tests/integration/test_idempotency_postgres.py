"""Idempotency-Key retries on the real schema (services/routes/idempotency.py).

A disposable database next to TEST_DATABASE_URL (loopback only) gets the consolidated schema; requests go through the
real routes with only the session lookup replaced. Checks: a repeat returns the first record, a different body with the
same key is refused, parallel twins make one record, no header keeps the old behaviour, another company's key never
matches, and transitions / duplicate / set aside replay instead of acting twice. Skipped without TEST_DATABASE_URL.
"""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

import main as api
from database.schema_compatibility import apply_migrations
from services.routes.accounting import routes as accounting
from services.routes.documents import routes as document_routes
from services.routes.erp import catalog, contacts, documents

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
HEADERS = {'X-Requested-With': 'AccountingIntelligence', 'Origin': 'http://localhost:3000'}


class Session:
    user = None


@pytest.fixture(scope='module')
def database(tmp_path_factory):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'idempotency_' + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE {name}')
    target = parts._replace(path='/' + name).geturl()
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / 'database/PostgreSQL Database schema.sql', tmp_path_factory.mktemp('no-migrations'))
        yield target
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


@pytest.fixture
def company(database):
    """Two companies, each with a local admin, an open August period, a sales account and one customer."""
    @contextmanager
    def connect():
        with psycopg.connect(database, row_factory=dict_row) as db:
            yield db

    with connect() as db:
        def setup(label):
            key = uuid4().hex
            tenant = db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,'a@x.test') RETURNING id",
                                (label + ' ' + key[:8], key[:12])).fetchone()['id']
            admin = dict(db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                VALUES (%s,'Admin',%s,'x','local_admin','active') RETURNING id,tenant_id,name,role""",
                                    (tenant, key + '@x.test')).fetchone())
            db.execute("""INSERT INTO accounting_periods(tenant_id,period_code,starts_on,ends_on,status)
                VALUES (%s,'2026-08','2026-08-01','2026-08-31','open')""", (tenant,))
            for code, name, kind in (('1100', 'Trade Receivables', 'asset'), ('4000', 'Sales - Goods', 'income'),
                                     ('2030', 'Tax Charged on Sales', 'liability')):
                db.execute("INSERT INTO ledger_accounts(tenant_id,code,name,account_type) VALUES (%s,%s,%s,%s)", (tenant, code, name, kind))
            customer = db.execute("INSERT INTO customers(tenant_id,customer_name) VALUES (%s,'Kedai Runcit Maju') RETURNING id",
                                  (tenant,)).fetchone()['id']
            return {'tenant': tenant, 'admin': admin, 'customer': customer}

        ids = {'own': setup('Own'), 'other': setup('Other')}

    patches = [patch.object(module, 'connect', side_effect=connect)
               for module in (api, accounting, contacts, documents, catalog, document_routes)]
    patches.append(patch.object(api, 'session_user', side_effect=lambda _db, _request: dict(Session.user)))
    for item in patches:
        item.start()
    try:
        yield connect, ids
    finally:
        for item in reversed(patches):
            item.stop()


def call(method, path, user, key=None, **kwargs):
    Session.user = user
    headers = {**HEADERS, **({'Idempotency-Key': key} if key is not None else {})}
    return TestClient(api.app).request(method, path, headers=headers, **kwargs)


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def invoice(company, **extra):
    return {'contactId': str(company['customer']), 'documentDate': '2026-08-12', 'dueDate': '2026-08-26',
            'lines': [{'description': 'Simen 50kg', 'quantity': '10', 'unitPrice': '25.50'}], **extra}


def count(connect, tenant, table='erp_document_headers'):
    with connect() as db:
        return db.execute(f'SELECT count(*) AS n FROM {table} WHERE tenant_id=%s', (tenant,)).fetchone()['n']


def test_repeat_with_same_key_returns_the_first_record(company):
    connect, ids = company
    own = ids['own']
    first = call('POST', '/api/erp/documents/invoice', own['admin'], 'create-1', json=invoice(own))
    again = call('POST', '/api/erp/documents/invoice', own['admin'], 'create-1', json=invoice(own))
    assert first.status_code == again.status_code == 201, again.text
    assert again.json() == first.json() and again.headers.get('Idempotency-Replayed') == 'true'
    assert 'Idempotency-Replayed' not in first.headers
    assert count(connect, own['tenant']) == 1


def test_same_key_with_a_different_body_is_refused(company):
    connect, ids = company
    own = ids['own']
    ok(call('POST', '/api/erp/documents/invoice', own['admin'], 'create-2', json=invoice(own)), 201)
    changed = call('POST', '/api/erp/documents/invoice', own['admin'], 'create-2', json=invoice(own, description='Other'))
    assert changed.status_code == 409 and 'different request' in changed.json()['detail']
    # The key is bound to its URL too: reusing it on another action is a different request.
    elsewhere = call('POST', '/api/erp/documents/quotation', own['admin'], 'create-2', json=invoice(own))
    assert elsewhere.status_code == 409
    assert count(connect, own['tenant']) == 1


def test_parallel_twins_make_one_record(company):
    connect, ids = company
    own = ids['own']
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda _: call('POST', '/api/erp/documents/invoice', own['admin'], 'create-parallel',
                                               json=invoice(own)), range(6)))
    assert {r.status_code for r in results} == {201}, [r.text for r in results]
    assert len({r.json()['id'] for r in results}) == 1
    assert count(connect, own['tenant']) == 1
    assert count(connect, own['tenant'], 'idempotency_keys') == 1


def test_without_a_header_behaviour_is_unchanged(company):
    connect, ids = company
    own = ids['own']
    a = ok(call('POST', '/api/erp/documents/invoice', own['admin'], json=invoice(own)), 201)
    b = ok(call('POST', '/api/erp/documents/invoice', own['admin'], json=invoice(own)), 201)
    assert a['id'] != b['id'] and count(connect, own['tenant']) == 2
    assert count(connect, own['tenant'], 'idempotency_keys') == 0
    # The existing guard still answers a repeated void without a key.
    ok(call('POST', f"/api/erp/documents/invoice/{a['id']}/transition", own['admin'], json={'status': 'void', 'reason': 'Typed twice'}))
    assert ok(call('POST', f"/api/erp/documents/invoice/{a['id']}/transition", own['admin'], json={'status': 'void'}))['idempotent']


def test_another_companys_key_never_matches(company):
    connect, ids = company
    own, other = ids['own'], ids['other']
    mine = ok(call('POST', '/api/erp/documents/invoice', own['admin'], 'shared-key', json=invoice(own)), 201)
    theirs = call('POST', '/api/erp/documents/invoice', other['admin'], 'shared-key', json=invoice(other))
    assert theirs.status_code == 201 and 'Idempotency-Replayed' not in theirs.headers
    assert theirs.json()['id'] != mine['id']
    assert count(connect, own['tenant']) == 1 and count(connect, other['tenant']) == 1


def test_transition_and_duplicate_replay_instead_of_acting_twice(company):
    connect, ids = company
    own = ids['own']
    draft = ok(call('POST', '/api/erp/documents/invoice', own['admin'], json=invoice(own)), 201)
    path = f"/api/erp/documents/invoice/{draft['id']}"
    copy = ok(call('POST', path + '/duplicate', own['admin'], 'dup-1'), 201)
    assert ok(call('POST', path + '/duplicate', own['admin'], 'dup-1'), 201)['id'] == copy['id']
    assert count(connect, own['tenant']) == 2

    issued = ok(call('POST', path + '/transition', own['admin'], 'ready-1', json={'status': 'ready'}))
    replay = call('POST', path + '/transition', own['admin'], 'ready-1', json={'status': 'ready'})
    assert replay.status_code == 200 and replay.json() == issued and 'idempotent' not in replay.json()
    with connect() as db:
        journals = db.execute('SELECT count(*) AS n FROM journal_headers WHERE tenant_id=%s', (own['tenant'],)).fetchone()['n']
        events = db.execute("SELECT count(*) AS n FROM erp_document_events WHERE document_id=%s AND event_type='status_changed'",
                            (draft['id'],)).fetchone()['n']
    assert journals == 1 and events == 1
    # A different action with the same key is refused, not run.
    assert call('POST', path + '/transition', own['admin'], 'ready-1', json={'status': 'void', 'reason': 'Wrong'}).status_code == 409


def test_set_aside_replays_and_a_failed_request_is_not_stored(company):
    connect, ids = company
    own = ids['own']

    def scanned(status):
        with connect() as db:
            return db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,
                    file_size_bytes,file_hash_sha256,status)
                VALUES (%s,%s,'bill.pdf','bill','accounts_payable','synthetic/none.pdf','application/pdf',100,%s,%s) RETURNING id""",
                              (own['tenant'], own['admin']['id'], hashlib.sha256(uuid4().bytes).hexdigest(), status)).fetchone()['id']

    document = scanned('extracted')
    path = f'/api/documents/{document}/exclude'
    first = ok(call('POST', path, own['admin'], 'aside-1', json={'reason': 'Belongs to another company'}))
    again = call('POST', path, own['admin'], 'aside-1', json={'reason': 'Belongs to another company'})
    assert first['idempotent'] is False and again.json() == first  # replayed, not re-run (a re-run says idempotent: true)
    with connect() as db:
        audits = db.execute("SELECT count(*) AS n FROM audit_logs WHERE resource=%s AND action='document.excluded'",
                            (str(document),)).fetchone()['n']
    assert audits == 1

    # A refused action is not stored: after the cause is fixed, the same key runs it.
    submitted = scanned('in_review')
    body = {'reason': 'Duplicate upload of the same bill'}
    assert call('POST', f'/api/documents/{submitted}/exclude', own['admin'], 'aside-2', json=body).status_code == 409
    with connect() as db:
        db.execute("UPDATE documents SET status='extracted' WHERE id=%s", (submitted,))
    assert ok(call('POST', f'/api/documents/{submitted}/exclude', own['admin'], 'aside-2', json=body))['excluded']


def test_a_key_must_be_1_to_160_characters(company):
    _, ids = company
    own = ids['own']
    assert call('POST', '/api/erp/documents/invoice', own['admin'], 'x' * 161, json=invoice(own)).status_code == 422
    assert call('POST', '/api/erp/documents/invoice', own['admin'], '   ', json=invoice(own)).status_code == 422
    assert call('POST', '/api/erp/documents/invoice', own['admin'], 'x' * 160, json=invoice(own)).status_code == 201
