"""Plan L2 on the real schema: one contact list, stored in the rows the rest of the system already uses.

A contact made on the Contacts page is the same vendors/customers row that /api/suppliers, /api/customers,
document matching and the contacts import use; nothing goes to a separate table except employees.
Skipped without TEST_DATABASE_URL.
"""
import os
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
from services.routes.erp import contacts
from services.routes.workspace import routes as workspace

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
    name = 'erp_contacts_' + uuid4().hex[:12]
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
        people = {'tenant': own, 'admin': person(own, 'local_admin'), 'accountant': person(own, 'accountant'),
                  'outsider': person(other, 'local_admin')}
    patches = [patch.object(module, 'connect', side_effect=connect) for module in (api, contacts, workspace)]
    patches.append(patch.object(api, 'session_user', side_effect=lambda _db, _request: dict(Session.user)))
    for item in patches:
        item.start()
    try:
        yield connect, people
    finally:
        for item in reversed(patches):
            item.stop()


def call(method, path, user, **kwargs):
    Session.user = user
    return TestClient(api.app).request(method, path, headers=HEADERS, **kwargs)


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def test_contacts_page_and_existing_endpoints_share_the_same_rows(company):
    connect, people = company
    supplier = ok(call('POST', '/api/erp/contacts', people['accountant'], json={
        'kind': 'supplier', 'legalName': 'Syarikat Bekalan Jaya Sdn Bhd', 'email': 'akaun@bekalan.test', 'phone': '082-123456',
        'registrationNumber': '201901012345', 'city': 'Kuching', 'state': 'Sarawak', 'code': 'S-001'}), 201)
    assert supplier['kind'] == 'supplier' and supplier['isActive']
    # The same row is the supplier the rest of the system uses.
    assert [s['id'] for s in ok(call('GET', '/api/suppliers', people['admin']))] == [supplier['id']]
    with connect() as db:
        row = db.execute("SELECT vendor_name,email,city FROM vendors WHERE id=%s", (supplier['id'],)).fetchone()
        assert row == {'vendor_name': 'Syarikat Bekalan Jaya Sdn Bhd', 'email': 'akaun@bekalan.test', 'city': 'Kuching'}
        assert db.execute("SELECT to_regclass('public.erp_contacts') AS t").fetchone()['t'] is None

    # One made through the old endpoint appears in the one list, with a customer and an employee.
    old = ok(call('POST', '/api/customers', people['admin'], json={'name': 'Kedai Runcit Maju'}), 201)
    ok(call('POST', '/api/erp/contacts', people['admin'], json={'kind': 'employee', 'legalName': 'Aminah binti Ali'}), 201)
    listing = ok(call('GET', '/api/erp/contacts', people['accountant']))
    assert [(c['legalName'], c['kind']) for c in listing] == [
        ('Aminah binti Ali', 'employee'), ('Kedai Runcit Maju', 'customer'), ('Syarikat Bekalan Jaya Sdn Bhd', 'supplier')]
    assert [c['id'] for c in ok(call('GET', '/api/erp/contacts', people['admin'], params={'kind': 'customer'}))] == [old['id']]
    assert len(ok(call('GET', '/api/erp/contacts', people['admin'], params={'search': 'bekalan'}))) == 1

    # Same name ignoring case and punctuation: refused, like the existing create.
    dup = call('POST', '/api/erp/contacts', people['admin'], json={'kind': 'supplier', 'legalName': 'syarikat bekalan jaya sdn. bhd.'})
    assert dup.status_code == 409
    # A company that is both: one row in each list.
    ok(call('POST', '/api/erp/contacts', people['admin'], json={'kind': 'customer', 'legalName': 'Syarikat Bekalan Jaya Sdn Bhd'}), 201)


def test_update_deactivate_and_history(company):
    connect, people = company
    contact = ok(call('POST', '/api/erp/contacts', people['admin'], json={'kind': 'customer', 'legalName': 'Old Name Trading'}), 201)
    updated = ok(call('PUT', f"/api/erp/contacts/{contact['id']}", people['accountant'], json={
        'kind': 'customer', 'legalName': 'New Name Trading', 'email': 'a@b.test', 'fields': {'tin': 'C1234567890'}}))
    assert updated['legalName'] == 'New Name Trading' and updated['fields'] == {'tin': 'C1234567890'}
    assert call('PUT', f"/api/erp/contacts/{contact['id']}", people['admin'], json={'kind': 'supplier', 'legalName': 'X'}).status_code == 422
    assert call('PUT', f"/api/erp/contacts/{contact['id']}", people['admin'],
                json={'kind': 'customer', 'legalName': 'Y', 'email': 'not-an-email'}).status_code == 422

    ok(call('DELETE', f"/api/erp/contacts/{contact['id']}", people['admin']))
    assert ok(call('GET', '/api/erp/contacts', people['admin'])) == []
    inactive = ok(call('GET', '/api/erp/contacts', people['admin'], params={'includeInactive': 'true'}))
    assert [c['isActive'] for c in inactive] == [False]
    with connect() as db:  # never removed: earlier documents and matching still see it
        assert db.execute("SELECT count(*) AS n FROM customers WHERE id=%s", (contact['id'],)).fetchone()['n'] == 1
    ok(call('POST', f"/api/erp/contacts/{contact['id']}/reactivate", people['admin']))
    history = ok(call('GET', f"/api/erp/contacts/{contact['id']}/history", people['admin']))
    assert [h['action'] for h in history][-1] == 'contact.customer_created' and len(history) == 4


def test_tenant_isolation_and_roles(company):
    _, people = company
    contact = ok(call('POST', '/api/erp/contacts', people['admin'], json={'kind': 'supplier', 'legalName': 'Private Supplier'}), 201)
    outsider = people['outsider']
    assert ok(call('GET', '/api/erp/contacts', outsider)) == []
    for method, path, body in (('GET', f"/api/erp/contacts/{contact['id']}", None),
                               ('PUT', f"/api/erp/contacts/{contact['id']}", {'kind': 'supplier', 'legalName': 'Taken'}),
                               ('DELETE', f"/api/erp/contacts/{contact['id']}", None),
                               ('GET', f"/api/erp/contacts/{contact['id']}/history", None)):
        assert call(method, path, outsider, json=body).status_code == 404
    # The same name in another company is a different contact.
    ok(call('POST', '/api/erp/contacts', outsider, json={'kind': 'supplier', 'legalName': 'Private Supplier'}), 201)
    assert call('GET', '/api/erp/contacts/not-a-uuid', people['admin']).status_code == 422
