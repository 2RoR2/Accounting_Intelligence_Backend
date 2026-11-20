"""B4a kind=auto against the real schema in a disposable local PostgreSQL.

Creates a uniquely named database next to TEST_DATABASE_URL (loopback only), loads the
consolidated schema, uploads through the real route with kind=auto, persists a synthetic
extraction through the real worker persistence, and drops the database afterwards.
Skipped without TEST_DATABASE_URL.
"""
import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

import main as api
from database.schema_compatibility import apply_migrations
from services.extraction.persistence import auto_kind, persistence
from services.queue import attempts
from services.routes.documents import routes as document_routes
from services.routes.reporting import routes as reporting_routes

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/extraction/data/contracts/syn001_m2.json'
HEADERS = {'X-Requested-With': 'AccountingIntelligence', 'Origin': 'http://localhost:3000'}


@pytest.fixture(scope='module')
def database(tmp_path_factory):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'b4a_kind_auto_' + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE {name}')
    target = urlunsplit(parts._replace(path='/' + name))
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / 'database/PostgreSQL Database schema.sql', tmp_path_factory.mktemp('no-migrations'))
        yield target
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


@pytest.fixture
def workspace(database, tmp_path):
    def connect():
        return psycopg.connect(database, row_factory=dict_row)
    with connect() as db:
        key = uuid4().hex
        tenant = db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,'a@x.test') RETURNING id",
                            ('B4a synthetic ' + key[:8], key[:12])).fetchone()['id']
        user = db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
            VALUES (%s,'B4a accountant',%s,'x','accountant','active') RETURNING id,tenant_id,name,role""",
                          (tenant, key + '@x.test')).fetchone()
    patches = [patch.object(document_routes, 'connect', side_effect=connect),
               patch.object(reporting_routes, 'connect', side_effect=connect),
               patch.object(reporting_routes, 'require_workspace_user', return_value=dict(user)),
               patch.object(attempts, 'connect', side_effect=connect),
               patch.object(persistence, 'connect', side_effect=connect),
               patch.object(document_routes, 'require_workspace_user', return_value=dict(user)),
               # Keep the job waiting (dispatchAccepted false): the test claims it itself.
               patch('services.queue.durable.dispatch_due_jobs'),
               patch('services.queue.scope.replay_document_ids', return_value=set()),
               patch.dict(os.environ, {'DOCUMENT_STORAGE_PATH': str(tmp_path)})]
    for item in patches:
        item.start()
    try:
        yield connect, user
    finally:
        for item in reversed(patches):
            item.stop()


def upload(kind, content):
    # No lifespan: the app's startup checks are not under test here.
    return TestClient(api.app).post('/api/documents', headers=HEADERS, params={'kind': kind, 'category': 'accounts_payable'},
                                    files={'file': ('phone-photo.pdf', content, 'application/pdf')})


def persist(connect, user, document_id, document_type, tmp_path):
    result = json.loads(FIXTURE.read_text())
    result['document_id'] = document_id
    result['provenance']['service_run_id'] = 'b4a-' + uuid4().hex
    result['document_type'] = deepcopy(document_type)
    with connect() as db:
        job = db.execute('SELECT id FROM pending_jobs WHERE document_id=%s', (document_id,)).fetchone()['id']
        delivery = attempts.reserve_dispatch(db, document_id, str(user['tenant_id']), job_id=job)
    claim = attempts.claim_delivery(delivery, delivery.dispatch_token)
    target = tmp_path / (uuid4().hex + '.json')
    target.write_text(json.dumps(result))
    return persistence.persist_extraction_result(document_id, {
        'service_run_id': result['provenance']['service_run_id'], 'public_result_path': str(target)}, claim=claim)


def row(connect, document_id):
    with connect() as db:
        return db.execute('SELECT kind,document_code,status FROM documents WHERE id=%s', (document_id,)).fetchone()


def test_auto_upload_stores_placeholder_and_neutral_code(workspace):
    connect, _user = workspace
    response = upload('auto', b'%PDF-1.7 b4a ' + uuid4().bytes)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['code'] == auto_kind.pending_document_code(body['id'])
    assert body['kind'] == 'AUTO' and body['kindDetected'] is False and body['dispatchAccepted'] is False
    stored = row(connect, body['id'])
    assert stored['kind'] == auto_kind.PLACEHOLDER_KIND and stored['document_code'] == body['code']
    assert stored['status'] == 'queued'


def read_kind_via_api(document_id):
    """(list kind, list kindDetected, detail kind, detail kindDetected) from the real GET routes."""
    client = TestClient(api.app)
    listed = client.get('/api/documents', headers=HEADERS)
    detail = client.get(f'/api/documents/{document_id}', headers=HEADERS)
    assert listed.status_code == 200 and detail.status_code == 200, (listed.text, detail.text)
    entry = next(item for item in listed.json() if item['id'] == document_id)
    return entry['kind'], entry['kindDetected'], detail.json()['kind'], detail.json()['kindDetected']


def test_get_routes_read_undetected_auto_upload_as_auto_until_classified(workspace, tmp_path):
    connect, user = workspace
    pending = upload('auto', b'%PDF-1.7 b4a get-unknown ' + uuid4().bytes).json()
    classified = upload('auto', b'%PDF-1.7 b4a get-known ' + uuid4().bytes).json()
    typed = upload('receipt', b'%PDF-1.7 b4a get-typed ' + uuid4().bytes).json()
    # Queued: the stored placeholder kind is never shown as INVOICE.
    assert read_kind_via_api(pending['id']) == ('AUTO', False, 'AUTO', False)
    # Read but not classified (needs fixing): still AUTO.
    persist(connect, user, pending['id'], {'value': None, 'status': 'unclassified', 'method': 'explicit_heading_rules',
                                           'reason': 'NO_SUPPORTED_HEADING'}, tmp_path)
    assert read_kind_via_api(pending['id']) == ('AUTO', False, 'AUTO', False)
    # Classified: the detected kind.
    persist(connect, user, classified['id'], {'value': 'invoice', 'status': 'classified', 'method': 'explicit_heading_rules',
                                              'reason': 'EXPLICIT_DOCUMENT_HEADING'}, tmp_path)
    assert read_kind_via_api(classified['id']) == ('INVOICE', True, 'INVOICE', True)
    assert read_kind_via_api(typed['id']) == ('RECEIPT', True, 'RECEIPT', True)


def test_kind_workflow_register_and_export_read_undetected_auto_upload_as_auto(workspace, tmp_path):
    connect, user = workspace
    pending = upload('auto', b'%PDF-1.7 b4a kw-unknown ' + uuid4().bytes).json()
    classified = upload('auto', b'%PDF-1.7 b4a kw-known ' + uuid4().bytes).json()
    persist(connect, user, classified['id'], {'value': 'invoice', 'status': 'classified', 'method': 'explicit_heading_rules',
                                              'reason': 'EXPLICIT_DOCUMENT_HEADING'}, tmp_path)
    classified_code = row(connect, classified['id'])['document_code']
    client = TestClient(api.app)
    workflow = client.get(f"/api/documents/{pending['id']}/kind-workflow", headers=HEADERS)
    assert workflow.status_code == 200, workflow.text
    assert (workflow.json()['kind'], workflow.json()['kindDetected']) == ('AUTO', False)
    workflow = client.get(f"/api/documents/{classified['id']}/kind-workflow", headers=HEADERS).json()
    assert (workflow['kind'], workflow['kindDetected']) == ('INVOICE', True)
    register = client.get('/api/reports/accounting', headers=HEADERS)
    assert register.status_code == 200, register.text
    kinds = {item['reference']: item['kind'] for item in register.json()['documents']}
    assert kinds[pending['code']] == 'AUTO' and kinds[classified_code] == 'INVOICE'
    with connect() as db:
        _headers, values = reporting_routes._export_rows(db, user['tenant_id'], 'documents')
    exported = {code: kind for code, kind, *_rest in values}
    assert exported[pending['code']] == 'auto' and exported[classified_code] == 'invoice'


def test_duplicate_auto_upload_is_reported_with_the_existing_code(workspace):
    content = b'%PDF-1.7 b4a duplicate ' + uuid4().bytes
    first = upload('auto', content)
    assert first.status_code == 201, first.text
    second = upload('auto', content)
    assert second.status_code == 409
    assert second.json()['detail']['existingDocumentCode'] == first.json()['code']


def test_classification_sets_final_kind_and_numbered_code(workspace, tmp_path):
    connect, user = workspace
    body = upload('auto', b'%PDF-1.7 b4a classified ' + uuid4().bytes).json()
    with connect() as db:
        expected = db.execute("SELECT last_value + (CASE WHEN is_called THEN 1 ELSE 0 END) AS n FROM document_invoice_seq").fetchone()['n']
    persist(connect, user, body['id'], {'value': 'invoice', 'status': 'classified', 'method': 'explicit_heading_rules',
                                        'reason': 'EXPLICIT_DOCUMENT_HEADING'}, tmp_path)
    stored = row(connect, body['id'])
    assert stored['kind'] == 'invoice'
    assert stored['document_code'] == f'INVOICE_{expected}'
    assert stored['status'] != 'queued'


def test_unclassified_auto_upload_keeps_neutral_code(workspace, tmp_path):
    connect, user = workspace
    body = upload('auto', b'%PDF-1.7 b4a unknown ' + uuid4().bytes).json()
    persist(connect, user, body['id'], {'value': None, 'status': 'unclassified', 'method': 'explicit_heading_rules',
                                        'reason': 'NO_SUPPORTED_HEADING'}, tmp_path)
    stored = row(connect, body['id'])
    assert stored['document_code'] == body['code'] and auto_kind.kind_pending(stored['document_code'])


@pytest.mark.parametrize('kind', ['bill', 'receipt', 'credit_note', 'debit_note'])
def test_each_kind_takes_its_own_sequence(workspace, kind):
    connect, _user = workspace
    body = upload('auto', b'%PDF-1.7 b4a ' + kind.encode() + uuid4().bytes).json()
    with connect() as db:
        document = db.execute('SELECT * FROM documents WHERE id=%s', (body['id'],)).fetchone()
        updated = auto_kind.apply_classified_kind(db, document, {'status': 'classified', 'value': kind})
        # A second call is a no-op for the code: the DOC_ guard no longer matches.
        again = auto_kind.apply_classified_kind(db, updated, {'status': 'classified', 'value': kind})
    stored = row(connect, body['id'])
    assert stored['kind'] == kind and stored['document_code'].startswith(kind.upper() + '_')
    assert stored['document_code'] == updated['document_code'] == again['document_code']


def test_typed_upload_is_unchanged(workspace):
    connect, _user = workspace
    body = upload('bill', b'%PDF-1.7 b4a typed ' + uuid4().bytes).json()
    assert body['code'].startswith('BILL_') and body['kindDetected'] is True
    assert row(connect, body['id'])['kind'] == 'bill'
