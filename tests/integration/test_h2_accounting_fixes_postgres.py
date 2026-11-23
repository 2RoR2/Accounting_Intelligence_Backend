"""Codex H2 review of `features`: accounting figures that would have been wrong for UAT evaluators.

Each test reproduces one reported defect on the real schema (disposable loopback database, real routes):
1. the supplier-bill / customer-invoice record routes must refuse a scanned credit note (it would raise
   expenses/payables or revenue/receivables instead of lowering them);
2. cash-basis P&L must not count a supplier credit note applied to a bill as a cash payment;
3. AP/AR ageing must not add a typed bill and the approved scan of the same bill.
Skipped without TEST_DATABASE_URL.
"""
from datetime import date
from decimal import Decimal

import pytest

from tests.integration.test_erp_documents_postgres import (  # noqa: F401  (fixtures are used by name)
    _period, _scanned_bill, call, company, database, journal_count, ok,
)
from services.routes.reporting import financial  # after main (imported above): avoids a circular import

pytestmark = pytest.mark.runtime_defaults


def _scanned_credit_note(connect, ids, category, number, total):
    """An approved scanned credit note (records are immutable, so it is written as one)."""
    with connect() as db:
        document = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,
                file_size_bytes,file_hash_sha256,status)
            VALUES (%s,%s,'note.pdf','credit_note',%s,'synthetic/none.pdf','application/pdf',100,md5(random()::text)||md5(random()::text),
                'completed') RETURNING id""", (ids['tenant'], ids['admin']['id'], category)).fetchone()['id']
        submission = db.execute("""INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision)
            VALUES (%s,%s,%s,'submitted',1) RETURNING id""", (document, ids['tenant'], ids['admin']['id'])).fetchone()['id']
        db.execute("""INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision)
            VALUES (%s,%s,%s,%s,'approved',1)""", (submission, document, ids['tenant'], ids['admin']['id']))
        return db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,vendor_id,supplier_name,buyer_name,
                invoice_number,invoice_date,currency,total_amount,tax_amount,snapshot)
            VALUES (%s,%s,%s,%s,'Syarikat Bekalan Jaya','Kedai Runcit Maju',%s,'2026-08-07','MYR',%s,0,'{}') RETURNING id""",
                          (document, ids['tenant'], category, ids['supplier'], number, total)).fetchone()['id']


def test_record_routes_refuse_a_scanned_credit_note(company):
    connect, ids = company
    admin = ids['admin']
    supplier_credit = _scanned_credit_note(connect, ids, 'accounts_payable', 'SCN-1', '500.00')
    refused = call('POST', f'/api/ledger/journals/from-supplier-bill/{supplier_credit}', admin, json={
        'periodId': str(_period(connect, ids, '2026-08')), 'expenseAccountId': str(ids['accounts']['5000']),
        'payableAccountId': str(ids['accounts']['2000']), 'journalDate': '2026-08-07'})
    assert refused.status_code == 422 and 'credit note' in refused.json()['detail'], refused.text

    customer_credit = _scanned_credit_note(connect, ids, 'accounts_receivable', 'CN-1', '300.00')
    refused = call('POST', f'/api/ledger/journals/from-customer-invoice/{customer_credit}', admin, json={
        'periodId': str(_period(connect, ids, '2026-08')), 'receivableAccountId': str(ids['accounts']['1100']),
        'revenueAccountId': str(ids['accounts']['4000']), 'journalDate': '2026-08-07'})
    assert refused.status_code == 422 and 'credit note' in refused.json()['detail'], refused.text
    assert journal_count(connect, ids) == 0


def test_cash_basis_ignores_a_supplier_credit_note_applied_to_a_scanned_bill(company):
    connect, ids = company
    admin = ids['admin']
    bill = _scanned_bill(connect, ids, number='INV-500', total='500.00', tax='0')
    ok(call('POST', f'/api/erp/scanned/{bill}/post', admin, json={}))
    note = ok(call('POST', '/api/erp/documents/purchase-credit-note', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-15', 'referenceNo': 'SCN-500', 'status': 'ready',
        'lines': [{'description': 'Returned goods', 'unitPrice': '500.00'}],
        'allocations': [{'targetId': str(bill), 'source': 'scanned', 'amount': '500.00'}]}), 201)
    assert note['posted']

    params = {'fromDate': '2026-08-01', 'toDate': '2026-08-31', 'basis': 'cash'}
    cash = ok(call('GET', '/api/ledger/profit-and-loss', admin, params=params))
    # No money left the bank: the credit note only reduced what is owed.
    assert all(t['expenses'] == '0.00' for t in cash['totals']), cash['totals']

    # A real payment of a scanned bill is still cash.
    other = _scanned_bill(connect, ids, number='INV-501', total='200.00', tax='0')
    ok(call('POST', f'/api/erp/scanned/{other}/post', admin, json={}))
    ok(call('POST', '/api/erp/documents/purchase-payment', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-20', 'amount': '200.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': str(other), 'source': 'scanned', 'amount': '200.00'}], 'status': 'ready'}), 201)
    cash = ok(call('GET', '/api/ledger/profit-and-loss', admin, params=params))
    assert [t['expenses'] for t in cash['totals']] == ['200.00'], cash['totals']


def test_records_export_contains_only_the_records_the_page_showed(company):
    from unittest.mock import patch
    from services.routes.reporting import routes as reporting
    connect, ids = company
    shown = _scanned_bill(connect, ids, number='INV-SHOWN', total='10.00', tax='0')
    _scanned_bill(connect, ids, number='INV-HIDDEN', total='20.00', tax='0')
    with patch.object(reporting, 'connect', side_effect=connect):
        created = ok(call('POST', '/api/exports', ids['accountant'], json={
            'resource': 'records', 'filters': {'recordIds': [str(shown)]}, 'format': 'csv'}), 201)
        assert created['rowCount'] == 1
        text = call('GET', f"/api/exports/{created['id']}/download", ids['accountant']).text
        everything = ok(call('POST', '/api/exports', ids['accountant'], json={'resource': 'records', 'format': 'csv'}), 201)
    assert 'INV-SHOWN' in text and 'INV-HIDDEN' not in text
    assert everything['rowCount'] == 2  # no recordIds: every current record, as before


def test_ageing_counts_a_typed_bill_and_its_approved_scan_once(company):
    connect, ids = company
    admin = ids['admin']
    typed = ok(call('POST', '/api/erp/documents/bill', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-07', 'dueDate': '2026-09-06', 'referenceNo': 'INV-1000',
        'status': 'ready', 'lines': [{'description': 'Goods', 'unitPrice': '1000.00'}]}), 201)
    assert typed['posted']
    _scanned_bill(connect, ids, number='inv-1000 ', total='1000.00', tax='0')  # the same supplier invoice, scanned later
    _scanned_bill(connect, ids, number='INV-2000', total='250.00', tax='0')    # a different bill still counts

    with connect() as db:
        payables = financial._ageing(db, ids['tenant'], date(2026, 8, 31), 'payables')
    assert payables['totals']['total'] == Decimal('1250.00'), payables['rows']
    assert sorted((r['source'], r['documentNumber']) for r in payables['rows']) == [
        ('scanned', 'INV-2000'), ('typed', typed['number'])]
