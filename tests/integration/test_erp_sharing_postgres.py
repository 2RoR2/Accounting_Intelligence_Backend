"""Plan L7 (share links, MyInvois prepared record) on the real schema; fixtures as in the L4/L5 test.

Original fixture notes: Plan L4/L5 on the real schema: typed sales/purchase documents post to the ledger, once.

A disposable database next to TEST_DATABASE_URL (loopback only) gets the consolidated schema; requests go through
the real routes with only the session lookup replaced. Checks the "done when" rules of L4 and L5:
balanced journals, a repeated "ready" posts once, void reverses, a closed period refuses, tenant isolation,
quotation -> invoice keeps the lines, payments settle invoices, typed documents show in P&L and ageing, and a
scanned bill and a typed bill each post once across both posting paths.
Skipped without TEST_DATABASE_URL.
"""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import main as api
from database.schema_compatibility import apply_migrations
from services.routes.accounting import routes as accounting
from services.routes.erp import catalog, contacts, documents, sharing
from services.routes.home import routes as home

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
HEADERS = {'X-Requested-With': 'AccountingIntelligence', 'Origin': 'http://localhost:3000'}
CHART = (('1010', 'Bank - Current Account', 'asset'), ('1020', 'Bank - Savings', 'asset'), ('1100', 'Trade Receivables', 'asset'),
         ('1140', 'Tax Paid on Purchases', 'asset'), ('2000', 'Trade Payables', 'liability'),
         ('2030', 'Tax Charged on Sales', 'liability'), ('4000', 'Sales - Goods', 'income'), ('4010', 'Sales - Services', 'income'),
         ('5000', 'Purchases', 'expense'), ('6100', 'Rent', 'expense'))


class Session:
    user = None


@pytest.fixture(scope='module')
def database(tmp_path_factory):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'erp_sharing_' + uuid4().hex[:12]
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
    """A fresh company per test: users, Aug/Sep periods (Sep closed), the trading chart, one customer, one supplier."""
    @contextmanager
    def connect():
        with psycopg.connect(database, row_factory=dict_row) as db:
            yield db

    with connect() as db:
        def tenant(label):
            key = uuid4().hex
            return db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,'a@x.test') RETURNING id",
                              (label + ' ' + key[:8], key[:12])).fetchone()['id']

        def person(tenant_id, role):
            return dict(db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                VALUES (%s,%s,%s,'x',%s,'active') RETURNING id,tenant_id,name,role""",
                                   (tenant_id, role.title(), uuid4().hex + '@x.test', role)).fetchone())

        own, other = tenant('Own'), tenant('Other')
        ids = {'tenant': own, 'admin': person(own, 'local_admin'), 'accountant': person(own, 'accountant'),
               'outsider': person(other, 'local_admin')}
        for code, starts, ends, status in (('2026-08', '2026-08-01', '2026-08-31', 'open'),
                                           ('2026-09', '2026-09-01', '2026-09-30', 'closed')):
            db.execute("INSERT INTO accounting_periods(tenant_id,period_code,starts_on,ends_on,status) VALUES (%s,%s,%s,%s,%s)",
                       (own, code, starts, ends, status))
        ids['accounts'] = {code: db.execute("INSERT INTO ledger_accounts(tenant_id,code,name,account_type) VALUES (%s,%s,%s,%s) RETURNING id",
                                            (own, code, name, kind)).fetchone()['id'] for code, name, kind in CHART}
        ids['customer'] = db.execute("INSERT INTO customers(tenant_id,customer_name) VALUES (%s,'Kedai Runcit Maju') RETURNING id",
                                     (own,)).fetchone()['id']
        ids['supplier'] = db.execute("INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,'Syarikat Bekalan Jaya') RETURNING id",
                                     (own,)).fetchone()['id']
        ids['bank'] = db.execute("""INSERT INTO erp_catalog_entries(tenant_id,kind,name,code,ledger_account_id)
            VALUES (%s,'bank-account','Current account','CA',%s) RETURNING id""", (own, ids['accounts']['1010'])).fetchone()['id']
        ids['savings'] = db.execute("""INSERT INTO erp_catalog_entries(tenant_id,kind,name,code,ledger_account_id)
            VALUES (%s,'bank-account','Savings account','SA',%s) RETURNING id""", (own, ids['accounts']['1020'])).fetchone()['id']

    patches = [patch.object(module, 'connect', side_effect=connect) for module in (api, accounting, contacts, documents, catalog, home, sharing)]
    patches.append(patch.object(api, 'session_user', side_effect=lambda _db, _request: dict(Session.user)))
    for item in patches:
        item.start()
    try:
        yield connect, ids
    finally:
        for item in reversed(patches):
            item.stop()


def call(method, path, user, **kwargs):
    Session.user = user
    return TestClient(api.app).request(method, path, headers=HEADERS, **kwargs)


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def journal_lines(connect, journal_id):
    with connect() as db:
        return db.execute("""SELECT a.code,l.debit,l.credit FROM journal_lines l JOIN ledger_accounts a ON a.id=l.account_id
            WHERE l.journal_id=%s ORDER BY l.line_number""", (journal_id,)).fetchall()


def shape(connect, journal_id):
    return [(l['code'], l['debit'], l['credit']) for l in journal_lines(connect, journal_id)]


def balanced(lines):
    return sum(l['debit'] for l in lines) == sum(l['credit'] for l in lines) > 0


def journal_count(connect, ids):
    with connect() as db:
        return db.execute("SELECT count(*) AS n FROM journal_headers WHERE tenant_id=%s", (ids['tenant'],)).fetchone()['n']


def invoice_body(ids, **extra):
    body = {'contactId': str(ids['customer']), 'documentDate': '2026-08-12', 'dueDate': '2026-08-26',
            'lines': [{'description': 'Simen 50kg', 'quantity': '10', 'unitPrice': '25.50', 'tax': '25.50'},
                      {'description': 'Delivery', 'quantity': '1', 'unitPrice': '40.00', 'discount': '5.00',
                       'accountId': str(ids['accounts']['4010'])}]}
    return {**body, **extra}


def test_share_links_are_off_by_default_and_follow_the_rules(company, monkeypatch):
    connect, ids = company
    admin = ids['admin']
    invoice = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    monkeypatch.delenv('ENABLE_SHARE_LINKS', raising=False)
    assert call('POST', f"/api/erp/documents/invoice/{invoice['id']}/shares", admin, json={}).status_code == 404
    assert ok(call('GET', '/api/erp/sharing-status', admin))['shareLinks'] is False

    monkeypatch.setenv('ENABLE_SHARE_LINKS', '1')
    draft = ok(call('POST', '/api/erp/documents/invoice', admin, json=invoice_body(ids)), 201)
    assert call('POST', f"/api/erp/documents/invoice/{draft['id']}/shares", admin, json={}).status_code == 409
    share = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/shares", ids['accountant'], json={'expiresInDays': 3}), 201)
    token = share['url'].rsplit('/share/', 1)[1]
    assert len(token) >= 40 and share['status'] == 'active'
    with connect() as db:  # only the hash is stored
        assert db.execute("SELECT count(*) AS n FROM erp_document_shares WHERE token_hash=%s", (token,)).fetchone()['n'] == 0
    public = TestClient(api.app).get(f'/api/public/erp-share/{token}')
    assert public.status_code == 200, public.text
    page = public.json()
    assert page['number'] == invoice['number'] and page['total'] == '315.50' and len(page['lines']) == 2
    assert 'id' not in page and 'tenantId' not in page and 'journalId' not in page
    assert TestClient(api.app).get('/api/public/erp-share/' + 'x' * 43).status_code == 404
    assert call('GET', f"/api/erp/documents/invoice/{invoice['id']}/shares", admin).json()[0]['openCount'] == 1
    # Another company cannot revoke it; revoking makes it unavailable.
    assert call('POST', f"/api/erp/shares/{share['id']}/revoke", ids['outsider']).status_code == 404
    ok(call('POST', f"/api/erp/shares/{share['id']}/revoke", admin))
    assert TestClient(api.app).get(f'/api/public/erp-share/{token}').status_code == 410
    # Expired and voided documents are unavailable too.
    second = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/shares", admin, json={}), 201)
    token2 = second['url'].rsplit('/share/', 1)[1]
    with connect() as db:
        db.execute("UPDATE erp_document_shares SET created_at=now()-interval '9 days', expires_at=now()-interval '1 day' WHERE id=%s", (second['id'],))
    assert TestClient(api.app).get(f'/api/public/erp-share/{token2}').status_code == 410
    third = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/shares", admin, json={}), 201)
    ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transition", admin, json={'status': 'void'}))
    assert TestClient(api.app).get(f"/api/public/erp-share/{third['url'].rsplit('/share/', 1)[1]}").status_code == 410


def test_myinvois_record_is_prepared_only_and_off_by_default(company, monkeypatch):
    _, ids = company
    admin = ids['admin']
    a = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    b = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids, documentDate='2026-08-20'), 'status': 'ready'}), 201)
    body = {'documentType': 'invoice', 'dateFrom': '2026-08-01', 'dateTo': '2026-08-31', 'documentIds': [a['id'], b['id']],
            'idempotencyKey': 'aug-2026'}
    monkeypatch.delenv('ENABLE_MYINVOIS_PREP', raising=False)
    assert call('POST', '/api/myinvois/consolidated', admin, json=body).status_code == 404
    monkeypatch.setenv('ENABLE_MYINVOIS_PREP', 'true')
    assert call('POST', '/api/myinvois/consolidated', ids['accountant'], json=body).status_code == 403
    made = ok(call('POST', '/api/myinvois/consolidated', admin, json=body), 201)
    assert made['status'] == 'prepared' and made['documentCount'] == 2 and made['totalAmount'] == '631.00'
    assert 'not submitted to LHDN' in made['note']
    assert ok(call('POST', '/api/myinvois/consolidated', admin, json=body), 201)['idempotent']
    again = call('POST', '/api/myinvois/consolidated', admin, json={**body, 'idempotencyKey': 'other', 'documentIds': [a['id']]})
    assert again.status_code == 409
    outside = call('POST', '/api/myinvois/consolidated', admin, json={**body, 'idempotencyKey': 'x', 'dateTo': '2026-08-15', 'documentIds': [b['id']]})
    assert outside.status_code == 422
    assert [r['documentCount'] for r in ok(call('GET', '/api/myinvois/consolidated', admin))] == [2]
