"""Demo company seed (plan v2 task A3) and the A2 import routes, without a database.

The seed must refuse anything but a local development database before it connects; its scoring of
reconciliation proposals against expected_matches.csv is the C5 baseline, so its rules are pinned here.
"""
import argparse
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import main
from ai.synthetic import company_pack
from services.admin import demo_company
from services.routes.imports import routes as import_routes

pytestmark = pytest.mark.runtime_defaults
LOCAL = {'APP_ENV': 'development', 'DATABASE_URL': 'postgresql://postgres:x@127.0.0.1:5433/accounting_db'}


@pytest.fixture(scope='module')
def packs(tmp_path_factory):
    root = tmp_path_factory.mktemp('packs')
    return {key: company_pack.generate_pack(key, root) for key in ('trading', 'fnb')}


@pytest.mark.parametrize('environ,expected', [
    ({**LOCAL}, None),
    ({**LOCAL, 'DATABASE_URL': 'postgresql://postgres:x@localhost/accounting_db'}, None),
    ({**LOCAL, 'APP_ENV': 'production'}, 'APP_ENV must be development'),
    ({'DATABASE_URL': LOCAL['DATABASE_URL']}, 'APP_ENV must be development'),
    ({**LOCAL, 'DATABASE_URL': ''}, 'DATABASE_URL is not set'),
    ({**LOCAL, 'DATABASE_URL': 'postgresql://postgres:x@db.example.supabase.co:5432/postgres'}, 'only a database on this machine'),
    ({**LOCAL, 'DATABASE_URL': 'postgresql://postgres:x@10.0.0.5/accounting_db'}, 'only a database on this machine'),
    ({**LOCAL, 'DATABASE_URL': 'host=127.0.0.1 dbname=accounting_db'}, 'postgresql:// URL'),
    ({**LOCAL, 'S3_BUCKET': 'team-bucket'}, 'S3_BUCKET is set'),
    # libpq connects to what the parsed parameters say, not to the URL's host part.
    ({**LOCAL, 'DATABASE_URL': 'postgresql://localhost/demo?hostaddr=203.0.113.10'}, 'connects to 203.0.113.10'),
    ({**LOCAL, 'DATABASE_URL': 'postgresql://localhost/demo?host=db.example.com'}, 'points at db.example.com'),
    ({**LOCAL, 'DATABASE_URL': 'postgresql://localhost,db.example.com/demo'}, 'points at db.example.com'),
    ({**LOCAL, 'DATABASE_URL': 'postgresql://localhost/demo?service=prod'}, 'connection service'),
    ({**LOCAL, 'PGSERVICE': 'prod'}, 'connection service'),
    ({**LOCAL, 'DATABASE_URL': 'postgresql:///demo'}, 'names no host'),
    ({**LOCAL, 'DATABASE_URL': 'postgresql:///demo', 'PGHOST': 'db.example.com'}, 'points at db.example.com'),
    ({**LOCAL, 'DATABASE_URL': 'postgresql:///demo', 'PGHOST': '127.0.0.1'}, None),
    ({**LOCAL, 'PGHOSTADDR': '203.0.113.10'}, 'connects to 203.0.113.10'),
    ({**LOCAL, 'DATABASE_URL': 'postgresql://localhost/demo?hostaddr=127.0.0.1'}, None),
    ({**LOCAL, 'DATABASE_URL': 'postgresql://[::1]:5433/demo'}, None),
])
def test_seed_guard(environ, expected):
    reason = demo_company.refusal_reason(environ)
    assert reason is None if expected is None else expected in reason


def test_seed_command_refuses_a_remote_database_before_connecting(capsys):
    args = argparse.Namespace(pack='trading', month=None, pack_root='unused', prompt_password=False,
                              reset_passwords=False, timeout_minutes=1, report=None)
    remote = {**LOCAL, 'DATABASE_URL': 'postgresql://postgres:x@shared-db.example.com:5432/accounting_db'}
    with patch.dict('os.environ', remote, clear=True), \
         patch('database.connection.connect', side_effect=AssertionError('must not connect')):
        assert demo_company.run(args) == 2
    assert 'Refusing to seed' in capsys.readouterr().err


def test_credentials_are_printed_before_a_later_step_fails(packs, capsys):
    from unittest.mock import MagicMock
    args = argparse.Namespace(pack='fnb', month=None, pack_root=str(packs['fnb'].parent), prompt_password=False,
                              reset_passwords=False, timeout_minutes=1, report=None)
    passwords = [('accountant', 'accountant@fnb.demo.test', 'Generated-Secret-1a!')]
    with patch.dict('os.environ', LOCAL, clear=True), \
         patch('database.connection.connect', return_value=MagicMock()), \
         patch.object(main, 'check_schema_compatibility'), patch.object(main, 'apply_compatibility_migrations'), \
         patch.object(demo_company, 'ensure_company', return_value=({'id': 'tenant'}, {}, passwords)), \
         patch.object(demo_company, 'issue_tokens', side_effect=demo_company.SeedError('sessions failed')):
        assert demo_company.run(args) == 2
    out = capsys.readouterr()
    assert 'Generated-Secret-1a!' in out.out and 'sessions failed' in out.err
    assert out.out.count('Generated-Secret-1a!') == 1


def test_legacy_knt_pack_cannot_load_as_generic_demo(packs):
    with patch.dict('os.environ', LOCAL, clear=True):
        with pytest.raises(demo_company.SeedError, match='seed-knt or reset-knt'):
            demo_company.seed('trading', pack_root=packs['trading'].parent)


def test_upload_kind_follows_the_pack_manifest():
    row = {'document_id': 'X', 'kind': 'supplier_invoice', 'manifest': 'invoice', 'direction': 'purchase'}
    assert demo_company.upload_kind(row) == ('invoice', 'accounts_payable')
    assert demo_company.upload_kind({**row, 'kind': 'utility_bill', 'manifest': 'bill'}) == ('bill', 'accounts_payable')
    assert demo_company.upload_kind({**row, 'kind': 'credit_note', 'direction': 'sales'}) == ('credit_note', 'accounts_receivable')
    assert demo_company.upload_kind({**row, 'kind': 'sales_invoice', 'direction': 'sales'}) == ('invoice', 'accounts_receivable')


def test_score_proposals_rules():
    expected = [
        {'bank_file': 'bank_2026-08.csv', 'source_row': '1', 'category': 'document', 'match_type': 'exact', 'document_ids': 'A'},
        {'bank_file': 'bank_2026-08.csv', 'source_row': '2', 'category': 'document', 'match_type': 'combined', 'document_ids': 'B;C'},
        {'bank_file': 'bank_2026-08.csv', 'source_row': '3', 'category': 'document', 'match_type': 'exact', 'document_ids': 'D'},
        {'bank_file': 'bank_2026-08.csv', 'source_row': '4', 'category': 'fee', 'match_type': '', 'document_ids': ''},
        {'bank_file': 'bank_2026-08.csv', 'source_row': '5', 'category': 'document', 'match_type': 'exact', 'document_ids': 'E'},
    ]
    proposals = {('bank_2026-08.csv', 1): {'A'}, ('bank_2026-08.csv', 2): {'B'}, ('bank_2026-08.csv', 3): {'Z'},
                 ('bank_2026-08.csv', 4): {'F'}}
    score = demo_company.score_proposals(expected, proposals)
    assert {k: score[k] for k in ('bank_rows', 'rows_with_documents', 'correct', 'complete', 'wrong', 'missed')} == {
        'bank_rows': 5, 'rows_with_documents': 4, 'correct': 2, 'complete': 1, 'wrong': 2, 'missed': 1}
    assert score['by_match_type']['fee'] == {'rows': 1, 'wrong': 1}


def test_bank_files_are_mapped_from_the_database_so_a_reseed_keeps_earlier_imports():
    from datetime import date
    from unittest.mock import MagicMock
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        {'id': 'a1', 'first_posted': date(2026, 8, 3)}, {'id': 'b2', 'first_posted': date(2026, 9, 1)},
        {'id': 'c3', 'first_posted': None}]
    files = demo_company.bank_import_files(db, 'tenant')
    assert files == {'a1': 'bank_2026-08.csv', 'b2': 'bank_2026-09.csv'}
    assert "status='confirmed'" in db.execute.call_args[0][0]


def test_matcher_baseline_on_a_pack_is_consistent(packs):
    score = demo_company.matcher_baseline(packs['trading'])
    rows = sum(1 for _ in open(packs['trading'] / 'expected_matches.csv', encoding='utf-8')) - 1
    assert score['bank_rows'] == rows
    assert score['correct'] + score['missed'] + score['wrong'] <= score['bank_rows']
    assert score['correct'] >= score['complete'] and score['wrong'] == 0
    august = demo_company.matcher_baseline(packs['trading'], ['2026-08'])
    assert august['bank_rows'] < score['bank_rows']


def test_dr_cr_bank_file_becomes_json_rows(packs):
    pack = packs['fnb']
    rows = demo_company.bank_rows_as_json(pack / 'bank_2026-08.csv', 'dr_cr')
    gold = company_pack.read_bank_file(pack / 'bank_2026-08.csv', 'dr_cr')
    assert len(rows) == len(gold)
    for row, source in zip(rows, gold):
        assert (row['debit'] is None) == (source['amount'] > 0) and (row['credit'] is None) == (source['amount'] < 0)
        assert main.BankTransactionInput(**row)


# --- A2 routes ------------------------------------------------------------------------------------

ADMIN = {'id': '11111111-1111-1111-1111-111111111111', 'tenant_id': '22222222-2222-2222-2222-222222222222',
         'role': 'local_admin', 'name': 'Synthetic admin'}
HEADERS = {'X-Requested-With': 'AccountingIntelligence', 'Origin': 'http://localhost:3000'}


@pytest.mark.parametrize('path,columns', [('/api/contacts/import/template', company_pack.CONTACT_COLUMNS),
                                          ('/api/chart-of-accounts/import/template', company_pack.COA_COLUMNS),
                                          ('/api/opening-balances/import/template', company_pack.OPENING_COLUMNS)])
def test_template_downloads(path, columns):
    with patch.object(import_routes, 'require_workspace_user', return_value={**ADMIN, 'role': 'accountant'}):
        response = TestClient(main.app).get(path)
    assert response.status_code == 200
    assert response.text == ','.join(columns) + '\n'
    assert response.headers['content-type'].startswith('text/csv')
    assert 'attachment' in response.headers['content-disposition']


def test_import_rejects_files_that_are_not_csv():
    with patch.object(import_routes, 'require_workspace_user', return_value=ADMIN), \
         patch.object(import_routes, 'connect', side_effect=AssertionError('no database for a rejected file')):
        response = TestClient(main.app).post('/api/contacts/import', headers=HEADERS,
                                             files={'file': ('contacts.xlsx', b'PK', 'application/octet-stream')})
    assert response.status_code == 422 and 'CSV' in response.json()['detail']


def test_confirm_refuses_a_file_that_was_not_previewed():
    with patch.object(import_routes, 'require_workspace_user', return_value=ADMIN), \
         patch.object(import_routes, 'connect', side_effect=AssertionError('no database for a stale token')):
        response = TestClient(main.app).post('/api/contacts/import/confirm', headers=HEADERS,
                                             files={'file': ('contacts.csv', b'type,name\nsupplier,A\n', 'text/csv')},
                                             data={'previewToken': 'not-the-hash'})
    assert response.status_code == 409 and 'Preview it again' in response.json()['detail']


def test_imports_need_a_local_admin():
    from fastapi import HTTPException

    def deny(_request, roles=None):
        assert roles == {'local_admin'}
        raise HTTPException(403, 'You do not have permission for this workspace.')
    with patch.object(import_routes, 'require_workspace_user', side_effect=deny):
        response = TestClient(main.app).post('/api/chart-of-accounts/import', headers=HEADERS,
                                             files={'file': ('coa.csv', b'code\n1\n', 'text/csv')})
    assert response.status_code == 403
