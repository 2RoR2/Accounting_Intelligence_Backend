"""Fraud Shield and AI Inbox on the real KNT books in a disposable loopback PostgreSQL.

The false-positive count is taken on the clean KNT books first. Then the AI demo cases are added (the same code as
scripts/seed_ai_demo_cases.py) and each proposal, its accept action, read-only behaviour and tenant isolation are checked.
"""
import json
import os
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

import main
from database.schema_compatibility import apply_migrations
from services.admin.demo_company import HEADERS, _remote_destination, issue_tokens
from services.admin.knt_reset import reset
from services.admin.knt_seed import locked_database
from services.insights import demo_cases

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
ENDPOINTS = (('GET', '/api/insights/fraud/summary'), ('GET', '/api/inbox'),
             ('GET', f'/api/insights/fraud?documentId={uuid4()}'),
             ('POST', f'/api/insights/fraud/{uuid4()}/acknowledge'), ('POST', '/api/inbox/' + 'a' * 20 + '/dismiss'))


@pytest.fixture(scope='module')
def books(tmp_path_factory):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires a disposable loopback TEST_DATABASE_URL')
    if _remote_destination(url, os.environ):
        pytest.fail('These tests require a disposable loopback PostgreSQL database')
    tmp = tmp_path_factory.mktemp('fraud-inbox')
    name = 'test_fraud_inbox_' + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    target = urlsplit(url)._replace(path='/' + name).geturl()
    patch = pytest.MonkeyPatch()
    for key, value in {'DATABASE_URL': target, 'APP_ENV': 'development', 'S3_BUCKET': '', 'DB_POOL_SIZE': '0',
                       'AUTH_SECRET': 'synthetic-auth-secret-for-fraud-inbox-tests', 'AI_ASSIST': 'on',
                       'DOCUMENT_STORAGE_PATH': str(tmp / 'objects'), 'AI_EXTRACTION': 'off'}.items():
        patch.setenv(key, value)
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / 'database/PostgreSQL Database schema.sql', ROOT / 'database/migrations')
        with locked_database() as connect:
            reset(connect, tmp / 'pack', 'FixtureDemo!2026')
        state = _sessions(connect)
        with closing(_client()) as client:
            state['clean'] = _get(client, state['admin'], '/api/insights/fraud/summary')
            state['clean_inbox'] = _get(client, state['admin'], '/api/inbox')
        state['ids'] = demo_cases.seed(connect, apply=True, out=lambda _line: None)
        state['connect'] = connect
        yield state
    finally:
        patch.undo()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def _sessions(connect):
    from services.admin.knt_seed import EMAILS
    with connect() as db:
        knt = {role: db.execute('SELECT * FROM users WHERE lower(email)=%s', (email,)).fetchone()
               for role, email in EMAILS.items()}
        other = db.execute("""INSERT INTO tenants(name,registration_number,admin_email)
            VALUES ('Other Workspace Sdn Bhd','OTHER-001','owner@other.example.test') RETURNING id""").fetchone()['id']
        others = {role: db.execute("""INSERT INTO users(tenant_id,name,email,role,status)
            VALUES (%s,%s,%s,%s,'active') RETURNING *""", (other, f'Other {role}', f'{role}@other.example.test', role)
                                   ).fetchone() for role in ('accountant', 'local_admin')}
    tokens, _ = issue_tokens(knt)
    other_tokens, _ = issue_tokens(others)
    return {'tenant': knt['local_admin']['tenant_id'], 'admin': tokens['local_admin'],
            'accountant': tokens['accountant'], 'other': other_tokens['local_admin']}


def _client():
    from fastapi.testclient import TestClient
    return TestClient(main.app, base_url='http://127.0.0.1')


def _call(client, token, method, path, body=None):
    headers = {**HEADERS, 'Cookie': f'{main.ACCESS}={token}'}
    return client.request(method, path, headers=headers, json=body)


def _get(client, token, path):
    response = _call(client, token, 'GET', path)
    assert response.status_code == 200, response.text[:400]
    return response.json()


def _items(client, token, kind=None):
    items = _get(client, token, '/api/inbox')['items']
    return [entry for entry in items if kind is None or entry['kind'] == kind]


def _counts(connect, tenant):
    with connect() as db:
        return {table: db.execute(f'SELECT count(*) AS n FROM {table} WHERE tenant_id=%s', (tenant,)).fetchone()['n']
                for table in ('audit_logs', 'reconciliation_candidates', 'exceptions', 'settlement_allocations',
                              'vendors', 'journal_headers')}


def test_clean_knt_books_raise_few_false_positives(books, capsys):
    clean = books['clean']
    checked, flags = clean['checkedDocuments'], clean['flags']
    with capsys.disabled():
        print(f"\n[fraud-shield] clean KNT: {checked} documents checked, {flags} flags, "
              f"{clean['flaggedDocuments']} documents flagged, by code {json.dumps(clean['byCode'])}")
        print(f"[fraud-shield] clean KNT top: {json.dumps(clean['top'])}")
        print(f"[ai-inbox] clean KNT: {books['clean_inbox']['count']} proposals: "
              f"{json.dumps([(e['kind'], e['title']) for e in books['clean_inbox']['items']])}")
    assert checked >= 10  # KNT's scanned purchases; its typed ERP documents are not scanned documents.
    assert flags <= max(1, checked // 20), clean['top']  # Target from the plan: at most 1 per 20 clean documents.


def test_seeded_bank_change_and_near_duplicate_are_flagged(books):
    ids = books['ids']
    with closing(_client()) as client:
        changed = _get(client, books['admin'], f"/api/insights/fraud?documentId={ids['sel-nov']}")
        usual = _get(client, books['admin'], f"/api/insights/fraud?documentId={ids['sel-oct']}")
        copy = _get(client, books['admin'], f"/api/insights/fraud?documentId={ids['hs-2']}")
        summary = _get(client, books['admin'], '/api/insights/fraud/summary')
    [bank] = [f for f in changed['flags'] if f['code'] == 'FS-BANK-CHANGE']
    assert bank['severity'] == 'high' and '…4417' in bank['message'] and '…2093' in bank['message']
    assert '86041233' not in json.dumps(changed) and '51409900' not in json.dumps(changed)
    assert 'FS-BANK-CHANGE' not in {f['code'] for f in usual['flags']}
    assert [(f['code'], f['severity']) for f in copy['flags'] if f['code'] == 'FS-NEAR-DUP'] == [('FS-NEAR-DUP', 'high')]
    assert summary['byCode']['FS-BANK-CHANGE'] == 1 and summary['byCode']['FS-NEAR-DUP'] >= 1


def test_inbox_proposes_each_kind_with_existing_endpoints(books, capsys):
    with closing(_client()) as client:
        items = _items(client, books['accountant'])
        summary = _get(client, books['admin'], '/api/insights/fraud/summary')
    with capsys.disabled():
        print(f"\n[fraud-shield] with demo cases: {summary['checkedDocuments']} checked, by code "
              f"{json.dumps(summary['byCode'])}")
        for entry in items:
            print(f"[ai-inbox] {entry['severity']:6} {entry['kind']:20} {entry['title']}  basis={entry['basis']}")
    kinds = {entry['kind'] for entry in items}
    assert {'FRAUD_SHIELD', 'SUPPLIER_QUERY_DRAFT', 'EXCEPTION_GROUP', 'BANK_MATCH', 'CLOSE_BLOCKER'} <= kinds, kinds
    [draft] = [e for e in items if e['kind'] == 'SUPPLIER_QUERY_DRAFT']
    assert draft['draft']['wording'] == 'template' and 'BL-0098' in draft['draft']['subject']
    assert '4417' in draft['draft']['body'] and '8604' not in draft['draft']['body']
    assert {a['method'] for a in draft['actions']} == {'COPY', 'MAILTO', 'LINK'}
    [pakar] = [e for e in items if e['kind'] == 'EXCEPTION_GROUP' and 'Pakar' in e['title']]
    add = pakar['actions'][0]
    assert (add['method'], add['path'], add['body']) == ('POST', '/api/erp/contacts',
                                                          {'kind': 'supplier', 'legalName': 'Pakar Aircond Services'})
    assert pakar['title'].startswith('2 documents') and len(add['then']) == 2
    matches = [e for e in items if e['kind'] == 'BANK_MATCH']
    seladah = next(e for e in matches if 'BL-0090' in e['title'])
    assert 'Match' in seladah['basis']
    hup_seng = next(e for e in matches if 'HS-1001' in e['title'])
    assert 'Similar' in hup_seng['basis']
    for entry in items:  # Every API action is an existing endpoint; nothing is a server-side job.
        for action in entry['actions']:
            assert action['method'] in {'POST', 'LINK', 'COPY', 'MAILTO'}
            if action['method'] == 'POST':
                assert action['path'].startswith(('/api/erp/contacts', '/api/documents/', '/api/bank/'))


def test_reading_the_inbox_and_shields_writes_nothing(books):
    before = _counts(books['connect'], books['tenant'])
    with closing(_client()) as client:
        for token in (books['admin'], books['accountant']):
            _get(client, token, '/api/inbox')
            _get(client, token, '/api/insights/fraud/summary')
            _get(client, token, f"/api/insights/fraud?documentId={books['ids']['sel-nov']}")
    assert _counts(books['connect'], books['tenant']) == before


def test_accepting_runs_the_existing_endpoint_as_the_person(books):
    journals = _counts(books['connect'], books['tenant'])['journal_headers']
    with closing(_client()) as client:
        match = next(e for e in _items(client, books['accountant'], 'BANK_MATCH') if 'BL-0090' in e['title'])
        accept = match['actions'][0]
        response = _call(client, books['accountant'], accept['method'], accept['path'], accept['body'])
        assert response.status_code in {200, 201}, response.text[:400]
        group = next(e for e in _items(client, books['accountant'], 'EXCEPTION_GROUP') if 'Pakar' in e['title'])
        add = group['actions'][0]
        assert _call(client, books['accountant'], add['method'], add['path'], add['body']).status_code == 201
        for step in add['then']:
            assert _call(client, books['accountant'], step['method'], step['path'], step['body']).status_code == 200
        after = {e['id'] for e in _items(client, books['accountant'])}
    assert match['id'] not in after and group['id'] not in after
    # Matching only: no journal was posted. The payment is posted on the existing path, by a person.
    assert _counts(books['connect'], books['tenant'])['journal_headers'] == journals


def test_approve_anyway_reason_is_audited_and_never_blocks(books):
    document = books['ids']['sel-nov']
    path = f'/api/insights/fraud/{document}/acknowledge'
    with closing(_client()) as client:
        assert _call(client, books['admin'], 'POST', path, {'codes': ['FS-ROUND'], 'reason': 'not shown'}).status_code == 422
        assert _call(client, books['admin'], 'POST', path, {'codes': ['FS-BANK-CHANGE'], 'reason': ' ok '}).status_code == 422
        done = _call(client, books['admin'], 'POST', path,
                     {'codes': ['FS-BANK-CHANGE'], 'reason': 'Confirmed by phone with Seladah finance'})
        assert done.status_code == 201, done.text
        shown = _get(client, books['admin'], f'/api/insights/fraud?documentId={document}')
        assert shown['acknowledgedCodes'] == ['FS-BANK-CHANGE']
        assert shown['acknowledgements'][0]['reason'] == 'Confirmed by phone with Seladah finance'
        assert any(f['code'] == 'FS-BANK-CHANGE' for f in shown['flags'])  # Still shown: advisory, not cleared.
    with books['connect']() as db:
        row = db.execute("""SELECT details FROM audit_logs WHERE tenant_id=%s AND action='FRAUD_SHIELD_ACKNOWLEDGED'
            AND resource=%s""", (books['tenant'], document)).fetchone()
    assert json.loads(row['details'])['codes'] == ['FS-BANK-CHANGE']


def test_dismiss_is_per_user(books):
    with closing(_client()) as client:
        target = _items(client, books['accountant'], 'CLOSE_BLOCKER')[0]
        assert _call(client, books['accountant'], 'POST', f"/api/inbox/{target['id']}/dismiss",
                     {'reason': 'Handled at month end'}).status_code == 200
        assert target['id'] not in {e['id'] for e in _items(client, books['accountant'])}
        assert target['id'] in {e['id'] for e in _items(client, books['admin'])}
        assert _call(client, books['accountant'], 'POST', '/api/inbox/not-an-id/dismiss').status_code == 422


def test_another_company_sees_and_changes_nothing_of_knt(books):
    document = books['ids']['sel-nov']
    with closing(_client()) as client:
        assert _call(client, books['other'], 'GET', f'/api/insights/fraud?documentId={document}').status_code == 404
        assert _call(client, books['other'], 'POST', f'/api/insights/fraud/{document}/acknowledge',
                     {'codes': ['FS-BANK-CHANGE'], 'reason': 'Not my company'}).status_code == 404
        summary = _get(client, books['other'], '/api/insights/fraud/summary')
        assert summary['checkedDocuments'] == 0 and summary['flags'] == 0
        other_items = _items(client, books['other'])
        assert not any('Seladah' in json.dumps(e) or 'Hup Seng' in json.dumps(e) for e in other_items)
        knt_item = _items(client, books['admin'])[0]
        assert _call(client, books['other'], 'POST', f"/api/inbox/{knt_item['id']}/dismiss").status_code == 200
        assert knt_item['id'] in {e['id'] for e in _items(client, books['admin'])}


def test_flag_off_means_no_endpoints(books, monkeypatch):
    monkeypatch.setenv('AI_ASSIST', 'off')
    with closing(_client()) as client:
        for method, path in ENDPOINTS:
            response = _call(client, books['admin'], method, path, {} if method == 'POST' else None)
            assert response.status_code == 404 and response.json()['detail']['code'] == 'AI_ASSIST_OFF', path


def test_demo_script_refuses_a_second_run_and_remote_databases(books, monkeypatch):
    with pytest.raises(demo_cases.DemoError, match='already'):
        demo_cases.seed(books['connect'], apply=False, out=lambda _line: None)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://demo@uat.example.invalid/accounting')
    with pytest.raises(demo_cases.DemoError, match='AI_DEMO_REMOTE_HOST'):
        demo_cases.require_target(False)
