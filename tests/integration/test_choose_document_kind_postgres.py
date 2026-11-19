"""F1: a person chooses the type of a document that reading could not type, on the real schema.

Creates a uniquely named database next to TEST_DATABASE_URL (loopback only), loads the
consolidated schema, uploads through the real routes, persists a synthetic "nothing was read"
extraction (type unknown, NO_AUTOMATIC_MAPPING, no fields, no lines) through the real worker
persistence, and drops the database afterwards. The real role check runs: only the session
lookup is replaced. Skipped without TEST_DATABASE_URL.
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
from services.extraction.persistence import kind_choice, persistence
from services.queue import attempts
from services.routes.documents import routes as document_routes

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/extraction/data/contracts/syn001_m2.json'
HEADERS = {'X-Requested-With': 'AccountingIntelligence', 'Origin': 'http://localhost:3000'}
NOT_READ = {'value': 'unknown', 'status': 'unknown', 'method': 'none', 'reason': 'NO_AUTOMATIC_MAPPING'}


@pytest.fixture(scope='module')
def database(tmp_path_factory):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'f1_choose_kind_' + uuid4().hex[:12]
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


class Session:
    """Whoever is signed in for the next request."""
    user = None


@pytest.fixture
def workspace(database, tmp_path):
    def connect():
        return psycopg.connect(database, row_factory=dict_row)

    def person(db, tenant, role, label):
        return dict(db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
            VALUES (%s,%s,%s,'x',%s,'active') RETURNING id,tenant_id,name,role""",
                               (tenant, 'F1 ' + label, uuid4().hex + '@x.test', role)).fetchone())

    with connect() as db:
        def company(label):
            key = uuid4().hex
            return db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,'a@x.test') RETURNING id",
                              ('F1 ' + label + ' ' + key[:8], key[:12])).fetchone()['id']
        tenant, other_tenant = company('own'), company('other')
        people = {'owner': person(db, tenant, 'accountant', 'owner'),
                  'colleague': person(db, tenant, 'accountant', 'colleague'),
                  'admin': person(db, tenant, 'local_admin', 'admin'),
                  'outsider': person(db, other_tenant, 'accountant', 'outsider'),
                  'outside_admin': person(db, other_tenant, 'local_admin', 'outside admin'),
                  'super': dict(db.execute("""INSERT INTO users(name,email,password_hash,role,status)
                      VALUES ('F1 super',%s,'x','super_admin','active') RETURNING id,tenant_id,name,role""",
                                           (uuid4().hex + '@x.test',)).fetchone())}
    Session.user = people['owner']
    patches = [patch.object(document_routes, 'connect', side_effect=connect),
               patch.object(api, 'connect', side_effect=connect),
               patch.object(api, 'session_user', side_effect=lambda _db, _request: dict(Session.user)),
               patch.object(attempts, 'connect', side_effect=connect),
               patch.object(persistence, 'connect', side_effect=connect),
               patch('services.queue.durable.dispatch_due_jobs'),
               patch('services.queue.scope.replay_document_ids', return_value=set()),
               patch.dict(os.environ, {'DOCUMENT_STORAGE_PATH': str(tmp_path)})]
    for item in patches:
        item.start()
    try:
        yield connect, people
    finally:
        for item in reversed(patches):
            item.stop()


def call(method, path, user, **kwargs):
    Session.user = user
    # No lifespan: the app's startup checks are not under test here.
    return TestClient(api.app).request(method, path, headers=HEADERS, **kwargs)


def unread_document(connect, owner, tmp_path, kind='auto'):
    """Upload, then persist what AI-off reading leaves for a PDF: type unknown, no fields, no lines."""
    response = call('POST', '/api/documents', owner, params={'kind': kind, 'category': 'accounts_payable'},
                    files={'file': ('scan.pdf', b'%PDF-1.7 f1 ' + uuid4().bytes, 'application/pdf')})
    assert response.status_code == 201, response.text
    document_id = response.json()['id']
    result = json.loads(FIXTURE.read_text())
    result.update(document_id=document_id, document_type=deepcopy(NOT_READ), fields={}, line_items=[])
    result['provenance']['service_run_id'] = 'f1-' + uuid4().hex
    with connect() as db:
        job = db.execute('SELECT id FROM pending_jobs WHERE document_id=%s', (document_id,)).fetchone()['id']
        delivery = attempts.reserve_dispatch(db, document_id, str(owner['tenant_id']), job_id=job)
    claim = attempts.claim_delivery(delivery, delivery.dispatch_token)
    target = tmp_path / (uuid4().hex + '.json')
    target.write_text(json.dumps(result))
    persistence.persist_extraction_result(document_id, {
        'service_run_id': result['provenance']['service_run_id'], 'public_result_path': str(target)}, claim=claim)
    return document_id


def stored(connect, document_id):
    with connect() as db:
        document = db.execute('SELECT kind,document_code,status,content_revision FROM documents WHERE id=%s', (document_id,)).fetchone()
        audits = db.execute('SELECT details FROM audit_logs WHERE resource=%s AND action=%s', (str(document_id), kind_choice.KIND_CHOSEN_ACTION)).fetchall()
        fields = db.execute("""SELECT fp.field_key FROM field_predictions fp JOIN raw_extractions re ON re.id=fp.extraction_id AND re.is_current
            WHERE fp.document_id=%s""", (document_id,)).fetchall()
    return document, [json.loads(row['details']) for row in audits], {row['field_key'] for row in fields}


def type_errors(validation):
    """Failed type findings, from POST /revalidate (errors) or GET /validation (stored results)."""
    errors = [e for e in validation.get('errors', []) if e.get('field') == 'document_type']
    stored = [r for r in validation.get('results', []) if r.get('field_key') == 'document_type' and r.get('status') == 'failed']
    return errors + stored


def choose(document_id, user, kind, revision):
    return call('PATCH', f'/api/documents/{document_id}/kind', user, json={'kind': kind, 'expectedContentRevision': revision})


def test_unread_document_blocks_on_type_until_a_person_chooses_it(workspace, tmp_path):
    connect, people = workspace
    document_id = unread_document(connect, people['owner'], tmp_path)
    before, audits, fields = stored(connect, document_id)
    assert before['status'] == 'exception' and before['document_code'].startswith('DOC_') and not audits and not fields
    live = call('GET', f'/api/documents/{document_id}/validation', people['owner']).json()
    assert type_errors(live), live

    response = choose(document_id, people['owner'], 'bill', before['content_revision'])
    assert response.status_code == 200, response.text
    body = response.json()
    after, audits, fields = stored(connect, document_id)
    assert after['kind'] == 'bill' and after['document_code'].startswith('BILL_') and body['code'] == after['document_code']
    assert after['content_revision'] == before['content_revision'] + 1 == body['contentRevision']
    assert body['previousKind'] == 'INVOICE' and body['kindDetected'] is True
    assert not type_errors(body['validation']), body['validation']
    assert {'supplier', 'date', 'currency', 'total', 'account_number', 'bill_number'} <= fields
    assert body['fieldsAdded'] == len(fields)
    assert len(audits) == 1 and audits[0]['kind'] == 'bill' and audits[0]['previous_code'] == before['document_code']
    assert audits[0]['detected'] == {'value': 'unknown', 'status': 'unknown', 'reason': 'NO_AUTOMATIC_MAPPING'}
    extraction = call('GET', f'/api/documents/{document_id}/extraction', people['owner']).json()
    assert extraction['classification']['reason'] == 'NO_AUTOMATIC_MAPPING'  # what reading found is kept
    assert extraction['kindDecision']['value'] == 'bill' and extraction['kindDecision']['chosenBy'] == 'F1 owner'
    live = call('GET', f'/api/documents/{document_id}/validation', people['owner']).json()
    assert not type_errors(live), live
    with connect() as db:
        open_type = db.execute("""SELECT 1 FROM exceptions WHERE document_id=%s AND type='VALIDATION:EX-002'
            AND status<>'resolved' AND message LIKE 'document_type:%%'""", (document_id,)).fetchone()
        raw = db.execute('SELECT raw_json FROM raw_extractions WHERE document_id=%s AND is_current', (document_id,)).fetchone()['raw_json']
    assert open_type is None
    assert raw['document_type'] == NOT_READ  # the extraction is never rewritten


def test_chosen_type_document_can_be_filled_in_submitted_approved_and_saved(workspace, tmp_path):
    connect, people = workspace
    owner, admin = people['owner'], people['admin']
    document_id = unread_document(connect, owner, tmp_path)
    revision = stored(connect, document_id)[0]['content_revision']
    response = choose(document_id, owner, 'bill', revision)
    assert response.status_code == 200, response.text
    revision = response.json()['contentRevision']
    for key, value in [('supplier', 'Syarikat Air Kuching'), ('account_number', 'ACC-778812'), ('date', '2026-10-01'),
                       ('currency', 'MYR'), ('subtotal', '78.00'), ('tax', '0.00'), ('total', '78.00'), ('amount_due', '78.00')]:
        corrected = call('PATCH', f'/api/documents/{document_id}/extraction/{key}', owner,
                         json={'value': value, 'expectedContentRevision': revision})
        assert corrected.status_code == 200, (key, corrected.text)
        revision = corrected.json()['contentRevision']
    line = call('POST', f'/api/documents/{document_id}/line-items', owner,
                json={'description': 'Water supply October', 'amount': '78.00', 'sourcePage': 1,
                      'sourceNote': 'Printed on page 1, charges table', 'expectedContentRevision': revision})
    assert line.status_code == 201, line.text
    revision = stored(connect, document_id)[0]['content_revision']
    checked = call('POST', f'/api/documents/{document_id}/revalidate', owner).json()
    assert checked['validationStatus'] == 'PASS', [(c['rule'], c.get('required'), c['message']) for c in checked['checks'] if c.get('required') and c.get('checkStatus') != 'CHECKED']
    submitted = call('POST', f'/api/documents/{document_id}/review', owner, json={'expectedContentRevision': revision})
    assert submitted.status_code == 200, submitted.text
    # Submitted: the type is now fixed.
    assert choose(document_id, owner, 'receipt', revision).status_code == 409
    approved = call('POST', f'/api/documents/{document_id}/review/decision', admin,
                    json={'decision': 'approved', 'expectedContentRevision': revision})
    assert approved.status_code == 200, approved.text
    saved = call('POST', f'/api/records/from-document/{document_id}', admin)
    assert saved.status_code == 201, saved.text
    with connect() as db:
        snapshot = db.execute('SELECT snapshot FROM standardised_records WHERE document_id=%s', (document_id,)).fetchone()['snapshot']
    assert snapshot['documentKind'] == 'bill'
    assert snapshot['documentTypeDecision']['reason'] == kind_choice.CHOSEN_REASON
    assert snapshot['extraction']['document_type'] == NOT_READ


def test_only_the_owner_or_assignee_or_a_local_admin_of_the_company_may_choose(workspace, tmp_path):
    connect, people = workspace
    document_id = unread_document(connect, people['owner'], tmp_path)
    revision = stored(connect, document_id)[0]['content_revision']
    assert choose(document_id, people['super'], 'bill', revision).status_code == 403
    assert choose(document_id, people['colleague'], 'bill', revision).status_code == 404
    assert choose(document_id, people['outsider'], 'bill', revision).status_code == 404
    assert choose(document_id, people['outside_admin'], 'bill', revision).status_code == 404
    document, audits, fields = stored(connect, document_id)
    assert document['kind'] == 'invoice' and document['document_code'].startswith('DOC_') and not audits and not fields
    with connect() as db:
        db.execute('UPDATE documents SET assigned_to=%s WHERE id=%s', (people['colleague']['id'], document_id))
    assert choose(document_id, people['colleague'], 'receipt', revision).status_code == 200
    assert choose(document_id, people['admin'], 'invoice', revision + 1).status_code == 200


def test_stale_revision_and_unknown_kind_change_nothing(workspace, tmp_path):
    connect, people = workspace
    document_id = unread_document(connect, people['owner'], tmp_path)
    before = stored(connect, document_id)
    revision = before[0]['content_revision']
    assert choose(document_id, people['owner'], 'bill', revision + 1).status_code == 409
    for kind in ('auto', 'unknown', 'statement', ''):
        assert choose(document_id, people['owner'], kind, revision).status_code == 422, kind
    assert stored(connect, document_id) == before


def test_choosing_the_stored_kind_of_a_numbered_upload_keeps_its_code(workspace, tmp_path):
    connect, people = workspace
    document_id = unread_document(connect, people['owner'], tmp_path, kind='invoice')
    before = stored(connect, document_id)[0]
    assert before['document_code'].startswith('INVOICE_')
    response = choose(document_id, people['owner'], 'invoice', before['content_revision'])
    assert response.status_code == 200, response.text
    after, audits, fields = stored(connect, document_id)
    assert after['kind'] == 'invoice' and after['document_code'] == before['document_code']
    assert not type_errors(response.json()['validation'])
    assert {'supplier', 'invoice', 'date', 'currency', 'total'} <= fields and audits[0]['previous_kind'] == 'invoice'


def test_a_document_confirmed_as_an_invoice_is_a_duplicate_reference(workspace, tmp_path):
    """Codex P1 on #15: the raw classification stays unknown, so references must use the person's choice."""
    connect, people = workspace
    owner = people['owner']

    def confirmed_invoice(number):
        document_id = unread_document(connect, owner, tmp_path, kind='invoice')
        response = choose(document_id, owner, 'invoice', stored(connect, document_id)[0]['content_revision'])
        assert response.status_code == 200, response.text
        revision = response.json()['contentRevision']
        for key, value in [('supplier', 'Syarikat Pendua Sdn. Bhd.'), ('invoice', number), ('date', '2026-10-01'),
                           ('currency', 'MYR'), ('total', '50.00')]:
            corrected = call('PATCH', f'/api/documents/{document_id}/extraction/{key}', owner,
                             json={'value': value, 'expectedContentRevision': revision})
            assert corrected.status_code == 200, (key, corrected.text)
            revision = corrected.json()['contentRevision']
        return document_id

    first = confirmed_invoice('INV-DUP-' + uuid4().hex[:6])
    number = call('GET', f'/api/documents/{first}/extraction', owner).json()
    number = next(f['value'] for f in number['fields'] if f['key'] == 'invoice')
    second = confirmed_invoice(number)
    checked = call('POST', f'/api/documents/{second}/revalidate', owner).json()
    assert any(e['code'] == 'EX-007' for e in checked['errors']), checked['errors']
    with connect() as db:
        flagged = db.execute("""SELECT 1 FROM exceptions WHERE document_id=%s AND type='VALIDATION:EX-007'
            AND status<>'resolved'""", (second,)).fetchone()
    assert flagged
    # A different number is not a duplicate.
    third = confirmed_invoice('INV-OTHER-' + uuid4().hex[:6])
    assert not any(e['code'] == 'EX-007' for e in call('POST', f'/api/documents/{third}/revalidate', owner).json()['errors'])
