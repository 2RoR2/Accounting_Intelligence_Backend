"""Roadmap P10: the proposed journal of a scanned document before approval, on the real schema.

Reuses the F1 helpers (upload through the real routes, an unread extraction, a person types the values) and the
real posting plan. Checks the lines, that the printed tax is used as printed, that nothing is written, and that
another company cannot read it. Skipped without TEST_DATABASE_URL.
"""
from unittest.mock import patch

import main  # noqa: F401 - initialise the application's route registry before route imports.
from services.routes.review import posting_preview
from tests.integration import test_choose_document_kind_postgres as f1
from tests.integration.test_choose_document_kind_postgres import database, workspace  # noqa: F401 - fixtures

pytestmark = f1.pytestmark


def typed_bill(connect, people, tmp_path, values):
    owner = people['owner']
    document_id = f1.unread_document(connect, owner, tmp_path)
    revision = f1.choose(document_id, owner, 'bill', f1.stored(connect, document_id)[0]['content_revision']).json()['contentRevision']
    for key, value in values:
        corrected = f1.call('PATCH', f'/api/documents/{document_id}/extraction/{key}', owner,
                            json={'value': value, 'expectedContentRevision': revision})
        assert corrected.status_code == 200, (key, corrected.text)
        revision = corrected.json()['contentRevision']
    return document_id


def chart(connect, tenant):
    with connect() as db:
        for code, name, kind in [('5000', 'Purchases', 'expense'), ('1140', 'SST input tax', 'asset'),
                                 ('2000', 'Accounts payable', 'liability')]:
            db.execute('INSERT INTO ledger_accounts(tenant_id,code,name,account_type) VALUES (%s,%s,%s,%s)', (tenant, code, name, kind))


def journal_count(connect):
    with connect() as db:
        return db.execute('SELECT count(*) AS n FROM journal_headers').fetchone()['n']


def test_the_approver_sees_the_proposed_journal_before_approving(workspace, tmp_path):  # noqa: F811
    connect, people = workspace
    with patch.object(posting_preview, 'connect', side_effect=f1.document_routes.connect):
        document_id = typed_bill(connect, people, tmp_path, [
            ('supplier', 'Syarikat Air Kuching'), ('date', '2026-11-03'), ('currency', 'MYR'),
            ('total', '106.00'), ('tax', '6.00'), ('bill_number', 'SAK-2611-118')])
        chart(connect, people['owner']['tenant_id'])
        before = journal_count(connect)
        response = f1.call('GET', f'/api/documents/{document_id}/posting-preview', people['admin'])
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['problem'] is None, body
        assert [(line['code'], line['debit'], line['credit']) for line in body['lines']] == [
            ('5000', '100.00', None), ('1140', '6.00', None), ('2000', None, '106.00')]
        assert body['sentences'][0] == 'Debit 5000 Purchases RM 100.00'
        assert 'Nothing is posted' in body['basis'] and body['date'] == '2026-11-03'
        assert journal_count(connect) == before  # read only

        # Another company never sees it; a bad id is a plain 404.
        assert f1.call('GET', f'/api/documents/{document_id}/posting-preview', people['outsider']).status_code == 404
        assert f1.call('GET', '/api/documents/not-a-uuid/posting-preview', people['admin']).status_code == 404


def test_a_missing_account_or_a_receipt_is_explained_not_an_error(workspace, tmp_path):  # noqa: F811
    connect, people = workspace
    with patch.object(posting_preview, 'connect', side_effect=f1.document_routes.connect):
        document_id = typed_bill(connect, people, tmp_path, [
            ('supplier', 'Syarikat Air Kuching'), ('date', '2026-11-03'), ('currency', 'MYR'), ('total', '78.00')])
        body = f1.call('GET', f'/api/documents/{document_id}/posting-preview', people['owner']).json()
        assert body['lines'] == [] and 'account' in body['problem']
        receipt = f1.unread_document(connect, people['owner'], tmp_path)
        revision = f1.stored(connect, receipt)[0]['content_revision']
        f1.choose(receipt, people['owner'], 'receipt', revision)
        body = f1.call('GET', f'/api/documents/{receipt}/posting-preview', people['owner']).json()
        assert body['lines'] == [] and 'receipt' in body['problem'].lower()
