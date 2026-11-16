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
