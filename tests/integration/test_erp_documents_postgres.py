"""Plan L4/L5 on the real schema: typed sales/purchase documents post to the ledger, once.

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
from services.routes.erp import catalog, contacts, documents
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
    name = 'erp_documents_' + uuid4().hex[:12]
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

    patches = [patch.object(module, 'connect', side_effect=connect) for module in (api, accounting, contacts, documents, catalog, home)]
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


def test_invoice_posts_balanced_once_and_void_reverses(company):
    connect, ids = company
    admin = ids['admin']
    draft = ok(call('POST', '/api/erp/documents/invoice', ids['accountant'], json=invoice_body(ids)), 201)
    assert draft['status'] == 'draft' and draft['number'].startswith('IV-') and draft['amount'] == '315.50'
    assert draft['subtotal'] == '290.00' and draft['taxAmount'] == '25.50' and not draft['posted']

    assert call('POST', f"/api/erp/documents/invoice/{draft['id']}/transition", ids['accountant'], json={'status': 'ready'}).status_code == 403
    pending = ok(call('POST', f"/api/erp/documents/invoice/{draft['id']}/transition", ids['accountant'], json={'status': 'pending_approval'}))
    assert pending['status'] == 'pending_approval'

    # Five concurrent "ready" requests: one journal.
    with ThreadPoolExecutor(5) as pool:
        results = list(pool.map(lambda _: call('POST', f"/api/erp/documents/invoice/{draft['id']}/transition", admin,
                                               json={'status': 'ready'}), range(5)))
    assert {r.status_code for r in results} == {200}, [r.text for r in results]
    ready = ok(call('GET', f"/api/erp/documents/invoice/{draft['id']}", admin))
    assert ready['status'] == 'ready' and ready['posted'] and ready['outstanding'] == '315.50'
    assert journal_count(connect, ids) == 1
    lines = journal_lines(connect, ready['journalId'])
    assert balanced(lines)
    assert shape(connect, ready['journalId']) == [
        ('1100', Decimal('315.50'), 0), ('4000', 0, Decimal('255.00')), ('4010', 0, Decimal('35.00')), ('2030', 0, Decimal('25.50'))]

    assert call('PATCH', f"/api/erp/documents/invoice/{draft['id']}", admin, json={'description': 'x'}).status_code == 409
    assert call('DELETE', f"/api/erp/documents/invoice/{draft['id']}", admin).status_code == 409

    report = ok(call('GET', '/api/ledger/profit-and-loss', admin, params={'fromDate': '2026-08-01', 'toDate': '2026-08-31'}))
    assert report['totals'] == [{'currency': 'MYR', 'income': '290.00', 'expenses': '0.00', 'netProfit': '290.00'}]
    assert any(a['category'] == 'accounts_receivable' and a['amount'] == '315.50' for a in report['aging'])
    overdue = ok(call('GET', '/api/home/summary', admin))['tiles']['invoicesOverdue']  # due 26 Aug, so overdue now
    assert overdue['count'] == 1 and overdue['amount'] == '315.50'

    voided = ok(call('POST', f"/api/erp/documents/invoice/{draft['id']}/transition", admin, json={'status': 'void', 'reason': 'Wrong customer'}))
    assert voided['status'] == 'void' and voided['reversalJournalId']
    assert ok(call('POST', f"/api/erp/documents/invoice/{draft['id']}/transition", admin, json={'status': 'void'}))['idempotent']
    reversal = journal_lines(connect, voided['reversalJournalId'])
    assert balanced(reversal) and reversal[0]['credit'] == Decimal('315.50')
    assert journal_count(connect, ids) == 2
    report = ok(call('GET', '/api/ledger/profit-and-loss', admin, params={'fromDate': '2026-08-01', 'toDate': '2026-08-31'}))
    assert report['totals'][0]['income'] == '0.00'
    assert not any(a['category'] == 'accounts_receivable' for a in report['aging'])
    history = ok(call('GET', f"/api/erp/documents/invoice/{draft['id']}/history", admin))
    assert [e['toStatus'] for e in history['events'] if e['eventType'] != 'printed'] == ['draft', 'pending_approval', 'ready', 'void']
    assert [e['eventType'] for e in history['events']].count('printed') == 2  # issued copy, then the VOID copy


def test_closed_period_refuses_posting_and_no_period_refuses(company):
    _, ids = company
    september = ok(call('POST', '/api/erp/documents/invoice', ids['admin'], json=invoice_body(ids, documentDate='2026-09-05')), 201)
    refused = call('POST', f"/api/erp/documents/invoice/{september['id']}/transition", ids['admin'], json={'status': 'ready'})
    assert refused.status_code == 409 and 'closed' in refused.json()['detail']
    assert ok(call('GET', f"/api/erp/documents/invoice/{september['id']}", ids['admin']))['status'] == 'draft'
    october = ok(call('POST', '/api/erp/documents/invoice', ids['admin'], json=invoice_body(ids, documentDate='2026-10-05')), 201)
    refused = call('POST', f"/api/erp/documents/invoice/{october['id']}/transition", ids['admin'], json={'status': 'ready'})
    assert refused.status_code == 409 and 'No accounting period' in refused.json()['detail']


def test_void_in_a_period_closed_after_posting_is_refused(company):
    connect, ids = company
    posted = ok(call('POST', '/api/erp/documents/invoice', ids['admin'], json={**invoice_body(ids), 'status': 'ready'}), 201)
    assert posted['posted']
    with connect() as db:
        db.execute("UPDATE accounting_periods SET status='closed' WHERE tenant_id=%s AND period_code='2026-08'", (ids['tenant'],))
    refused = call('POST', f"/api/erp/documents/invoice/{posted['id']}/transition", ids['admin'], json={'status': 'void'})
    assert refused.status_code == 409 and 'closed' in refused.json()['detail']
    assert ok(call('GET', f"/api/erp/documents/invoice/{posted['id']}", ids['admin']))['status'] == 'ready'


def test_quotation_to_invoice_to_payment(company):
    connect, ids = company
    admin = ids['admin']
    quote = ok(call('POST', '/api/erp/documents/quotation', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    assert quote['status'] == 'sent' and not quote['posted'] and not quote['posts']  # Plan M: issuing a quotation sends it; never posts
    invoice = ok(call('POST', f"/api/erp/documents/quotation/{quote['id']}/transfer", admin, json={'targetType': 'invoice'}), 201)
    assert invoice['type'] == 'invoice' and invoice['status'] == 'draft' and invoice['referenceNo'] == quote['number']
    assert [l['description'] for l in invoice['lines']] == ['Simen 50kg', 'Delivery'] and invoice['amount'] == quote['amount']
    assert call('POST', f"/api/erp/documents/quotation/{quote['id']}/transfer", admin, json={'targetType': 'bill'}).status_code == 422
    # A transferred document is dated today; this company's periods are Aug/Sep 2026.
    invoice = ok(call('PATCH', f"/api/erp/documents/invoice/{invoice['id']}", admin, json={'documentDate': '2026-08-12'}))
    invoice = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transition", admin, json={'status': 'ready'}))

    payment = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transfer", admin, json={'targetType': 'payment'}), 201)
    assert payment['amount'] == '315.50' and payment['allocations'][0]['targetId'] == invoice['id']
    payment = ok(call('PATCH', f"/api/erp/documents/payment/{payment['id']}", admin,
                      json={'amount': '300.00', 'bankAccountId': str(ids['bank']), 'documentDate': '2026-08-20',
                            'allocations': [{'targetId': invoice['id'], 'amount': '300.00'}]}))
    payment = ok(call('POST', f"/api/erp/documents/payment/{payment['id']}/transition", admin, json={'status': 'ready'}))
    assert shape(connect, payment['journalId']) == [('1010', Decimal('300.00'), 0), ('1100', 0, Decimal('300.00'))]
    after = ok(call('GET', f"/api/erp/documents/invoice/{invoice['id']}", admin))
    assert after['outstanding'] == '15.50' and after['paymentStatus'] == 'partly_paid'

    # The invoice cannot be voided while the payment is allocated to it; voiding the payment frees it.
    assert call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transition", admin, json={'status': 'void'}).status_code == 409
    ok(call('POST', f"/api/erp/documents/payment/{payment['id']}/transition", admin, json={'status': 'void'}))
    assert ok(call('GET', f"/api/erp/documents/invoice/{invoice['id']}", admin))['outstanding'] == '315.50'

    over = ok(call('POST', '/api/erp/documents/payment', admin, json={
        'contactId': str(ids['customer']), 'documentDate': '2026-08-20', 'amount': '400.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': invoice['id'], 'amount': '400.00'}]}), 201)
    assert call('POST', f"/api/erp/documents/payment/{over['id']}/transition", admin, json={'status': 'ready'}).status_code == 409


def test_bill_supplier_payment_transfer_money_and_journal_entry(company):
    connect, ids = company
    admin = ids['admin']
    bill = ok(call('POST', '/api/erp/documents/bill', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-03', 'referenceNo': 'SBJ-0091', 'status': 'ready',
        'lines': [{'description': 'Pasir', 'quantity': '2', 'unitPrice': '100', 'tax': '12.00'},
                  {'description': 'Shop rent', 'quantity': '1', 'unitPrice': '500', 'accountId': str(ids['accounts']['6100'])}]}), 201)
    assert shape(connect, bill['journalId']) == [
        ('5000', Decimal('200.00'), 0), ('6100', Decimal('500.00'), 0), ('1140', Decimal('12.00'), 0), ('2000', 0, Decimal('712.00'))]
    pay = ok(call('POST', '/api/erp/documents/purchase-payment', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-25', 'amount': '712.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': bill['id'], 'amount': '712.00'}], 'status': 'ready'}), 201)
    assert shape(connect, pay['journalId']) == [('2000', Decimal('712.00'), 0), ('1010', 0, Decimal('712.00'))]
    assert ok(call('GET', f"/api/erp/documents/bill/{bill['id']}", admin))['paymentStatus'] == 'paid'

    assert call('POST', '/api/erp/documents/bill', admin, json={'contactId': str(ids['customer'])}).status_code == 422

    transfer = ok(call('POST', '/api/erp/documents/bank-transfer', admin, json={
        'documentDate': '2026-08-15', 'amount': '1000.00', 'bankAccountId': str(ids['bank']),
        'fields': {'toBankAccountId': str(ids['savings'])}, 'status': 'ready'}), 201)
    assert shape(connect, transfer['journalId']) == [('1020', Decimal('1000.00'), 0), ('1010', 0, Decimal('1000.00'))]
    money_out = ok(call('POST', '/api/erp/documents/money-out', admin, json={
        'documentDate': '2026-08-16', 'bankAccountId': str(ids['bank']), 'status': 'ready',
        'lines': [{'description': 'Petty rent', 'unitPrice': '50', 'accountId': str(ids['accounts']['6100'])}]}), 201)
    assert balanced(journal_lines(connect, money_out['journalId']))
    unbalanced = call('POST', '/api/erp/documents/journal-entry', admin, json={
        'documentDate': '2026-08-31', 'status': 'ready', 'lines': [
            {'accountId': str(ids['accounts']['6100']), 'debit': '10.00'}, {'accountId': str(ids['accounts']['1010']), 'credit': '9.00'}]})
    assert unbalanced.status_code == 422
    journal = ok(call('POST', '/api/erp/documents/journal-entry', admin, json={
        'documentDate': '2026-08-31', 'status': 'ready', 'lines': [
            {'accountId': str(ids['accounts']['6100']), 'debit': '10.00'}, {'accountId': str(ids['accounts']['1010']), 'credit': '10.00'}]}), 201)
    assert balanced(journal_lines(connect, journal['journalId']))
    usd = call('POST', '/api/erp/documents/bill', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-03', 'currency': 'USD', 'status': 'ready',
        'lines': [{'description': 'Import', 'unitPrice': '10'}]})
    assert usd.status_code == 409 and 'MYR' in usd.json()['detail']


def _scanned_bill(connect, ids, number='INV-7781', total='106.00', tax='6.00'):
    with connect() as db:
        document = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,
                file_size_bytes,file_hash_sha256,status)
            VALUES (%s,%s,'bill.pdf','bill','accounts_payable','synthetic/none.pdf','application/pdf',100,%s,'completed') RETURNING id""",
                              (ids['tenant'], ids['admin']['id'], hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()['id']
        submission = db.execute("""INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision)
            VALUES (%s,%s,%s,'submitted',1) RETURNING id""", (document, ids['tenant'], ids['admin']['id'])).fetchone()['id']
        db.execute("""INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision)
            VALUES (%s,%s,%s,%s,'approved',1)""", (submission, document, ids['tenant'], ids['admin']['id']))
        return db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,vendor_id,supplier_name,invoice_number,
                invoice_date,due_date,currency,total_amount,tax_amount,snapshot)
            VALUES (%s,%s,'accounts_payable',%s,'Syarikat Bekalan Jaya',%s,'2026-08-07','2026-09-06','MYR',%s,%s,%s) RETURNING id""",
                          (document, ids['tenant'], ids['supplier'], number, total, tax, Jsonb({}))).fetchone()['id']


def _period(connect, ids, code):
    with connect() as db:
        return db.execute("SELECT id FROM accounting_periods WHERE tenant_id=%s AND period_code=%s", (ids['tenant'], code)).fetchone()['id']


def test_scanned_bill_shows_in_the_bill_list_and_posts_once_across_both_paths(company):
    connect, ids = company
    admin = ids['admin']
    record = _scanned_bill(connect, ids)
    listing = ok(call('GET', '/api/erp/documents/bill', admin))
    scanned = [d for d in listing['items'] if d['source'] == 'scanned']
    assert len(scanned) == 1 and scanned[0]['id'] == str(record) and scanned[0]['status'] == 'approved'
    assert ok(call('GET', '/api/erp/documents/bill', admin, params={'source': 'typed'}))['total'] == 0

    posted = ok(call('POST', f'/api/erp/scanned/{record}/post', admin, json={}))
    assert not posted['idempotent']
    assert shape(connect, posted['id']) == [('5000', Decimal('100.00'), 0), ('1140', Decimal('6.00'), 0), ('2000', 0, Decimal('106.00'))]
    assert ok(call('POST', f'/api/erp/scanned/{record}/post', admin, json={}))['idempotent']
    # The existing record endpoint uses the same key: it reports the same journal instead of posting again.
    old_path = ok(call('POST', f'/api/ledger/journals/from-supplier-bill/{record}', admin, json={
        'periodId': str(_period(connect, ids, '2026-08')), 'expenseAccountId': str(ids['accounts']['5000']),
        'payableAccountId': str(ids['accounts']['2000']), 'journalDate': '2026-08-07'}), 201)
    assert old_path['id'] == posted['id'] and old_path['idempotent']
    assert journal_count(connect, ids) == 1
    assert ok(call('GET', '/api/erp/documents/bill', admin, params={'source': 'scanned'}))['items'][0]['status'] == 'ready'

    # A typed bill repeating the scanned supplier invoice is refused; a different one posts once.
    duplicate = call('POST', '/api/erp/documents/bill', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-07', 'referenceNo': 'inv-7781 ', 'status': 'ready',
        'lines': [{'description': 'Same goods', 'unitPrice': '100', 'tax': '6'}]})
    assert duplicate.status_code == 409 and 'scanned' in duplicate.json()['detail']
    typed = ok(call('POST', '/api/erp/documents/bill', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-08', 'referenceNo': 'INV-7790', 'status': 'ready',
        'lines': [{'description': 'Other goods', 'unitPrice': '50'}]}), 201)
    again = ok(call('POST', f"/api/erp/documents/bill/{typed['id']}/transition", admin, json={'status': 'ready'}))
    assert again['idempotent'] and again['journalId'] == typed['journalId']
    assert journal_count(connect, ids) == 2

    # A supplier payment can settle the scanned bill through a settlement.
    pay = ok(call('POST', '/api/erp/documents/purchase-payment', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-28', 'amount': '106.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': str(record), 'source': 'scanned', 'amount': '106.00'}], 'status': 'ready'}), 201)
    assert ok(call('GET', '/api/erp/documents/bill', admin, params={'source': 'scanned'}))['items'][0]['outstanding'] == '0.00'
    ok(call('POST', f"/api/erp/documents/purchase-payment/{pay['id']}/transition", admin, json={'status': 'void'}))
    assert ok(call('GET', '/api/erp/documents/bill', admin, params={'source': 'scanned'}))['items'][0]['outstanding'] == '106.00'


def test_tenant_isolation(company):
    connect, ids = company
    invoice = ok(call('POST', '/api/erp/documents/invoice', ids['admin'], json=invoice_body(ids)), 201)
    outsider = ids['outsider']
    assert ok(call('GET', '/api/erp/documents/invoice', outsider))['total'] == 0
    for method, path, body in (('GET', f"/api/erp/documents/invoice/{invoice['id']}", None),
                               ('PATCH', f"/api/erp/documents/invoice/{invoice['id']}", {'description': 'x'}),
                               ('POST', f"/api/erp/documents/invoice/{invoice['id']}/transition", {'status': 'ready'}),
                               ('DELETE', f"/api/erp/documents/invoice/{invoice['id']}", None)):
        assert call(method, path, outsider, json=body).status_code == 404
    record = _scanned_bill(connect, ids, number='INV-1')
    assert call('POST', f'/api/erp/scanned/{record}/post', outsider, json={}).status_code == 404
    # Another company's customer cannot be put on a document here.
    assert call('POST', '/api/erp/documents/invoice', outsider, json=invoice_body(ids)).status_code == 422


def test_record_only_types_and_numbering(company):
    connect, ids = company
    admin = ids['admin']
    with connect() as db:
        db.execute("""INSERT INTO erp_catalog_entries(tenant_id,kind,name,data) VALUES (%s,'number-format','Sales orders',%s)""",
                   (ids['tenant'], Jsonb({'documentType': 'sales-order', 'prefix': 'SOX', 'padding': 3})))
    order = ok(call('POST', '/api/erp/documents/sales-order', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    assert order['number'] == 'SOX-001' and order['status'] == 'ready' and not order['posted']
    assert call('POST', '/api/erp/documents/sales-order', admin, json={**invoice_body(ids), 'number': 'SOX-001'}).status_code == 409
    assert ok(call('POST', '/api/erp/documents/sales-order', admin, json=invoice_body(ids)), 201)['number'] == 'SOX-002'
    copy = ok(call('POST', f"/api/erp/documents/sales-order/{order['id']}/duplicate", admin), 201)
    assert copy['status'] == 'draft' and copy['number'] == 'SOX-003' and len(copy['lines']) == 2
    ok(call('DELETE', f"/api/erp/documents/sales-order/{copy['id']}", admin))
    assert journal_count(connect, ids) == 0


def test_review_fixes_control_accounts_reversals_duplicates_and_cash_report(company):
    """Codex review of L2-L4: each P1 and the cash-basis P2."""
    connect, ids = company
    admin = ids['admin']
    # The invoice posts to 1100; then the default receivable changes to 1110. Its payment must still clear 1100.
    invoice = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    with connect() as db:
        other_ar = db.execute("INSERT INTO ledger_accounts(tenant_id,code,name,account_type) VALUES (%s,'1110','Other Receivables','asset') RETURNING id",
                              (ids['tenant'],)).fetchone()['id']
    ok(call('PUT', '/api/erp/posting-accounts', admin, json={'accounts': {'receivable': str(other_ar)}}))
    payment = ok(call('POST', '/api/erp/documents/payment', admin, json={
        'contactId': str(ids['customer']), 'documentDate': '2026-08-20', 'amount': '400.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': invoice['id'], 'amount': '315.50'}], 'status': 'ready'}), 201)
    assert shape(connect, payment['journalId']) == [
        ('1010', Decimal('400.00'), 0), ('1100', 0, Decimal('315.50')), ('1110', 0, Decimal('84.50'))]

    # A typed document's journal is reversed by voiding the document, not from the ledger.
    refused = call('POST', f"/api/ledger/journals/{payment['journalId']}/reverse", admin)
    assert refused.status_code == 409 and 'Void the document' in refused.json()['detail']

    # A duplicated payment does not point at the original payment.
    copy = ok(call('POST', f"/api/erp/documents/payment/{payment['id']}/duplicate", admin), 201)
    assert copy['allocations'] == []

    # A discount larger than its own line is refused even when another line covers it.
    bad = call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'lines': [
        {'description': 'A', 'unitPrice': '10', 'discount': '15'}, {'description': 'B', 'unitPrice': '100'}]})
    assert bad.status_code == 422

    # Cash basis counts the typed receipt on its date.
    cash = ok(call('GET', '/api/ledger/profit-and-loss', admin, params={'fromDate': '2026-08-01', 'toDate': '2026-08-31', 'basis': 'cash'}))
    assert any(a['accountCode'] == 'ALLOC-AR' and a['amount'] == '315.50' for a in cash['accounts'])


def test_review_fixes_typed_first_then_scanned_and_counterparty(company):
    connect, ids = company
    admin = ids['admin']
    typed = ok(call('POST', '/api/erp/documents/bill', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-07', 'referenceNo': 'INV-5501', 'status': 'ready',
        'lines': [{'description': 'Goods', 'unitPrice': '100', 'tax': '6'}]}), 201)
    record = _scanned_bill(connect, ids, number='INV-5501')
    for path, body in ((f'/api/erp/scanned/{record}/post', {}),
                       (f'/api/ledger/journals/from-supplier-bill/{record}', {
                           'periodId': str(_period(connect, ids, '2026-08')), 'expenseAccountId': str(ids['accounts']['5000']),
                           'payableAccountId': str(ids['accounts']['2000']), 'journalDate': '2026-08-07'})):
        refused = call('POST', path, admin, json=body)
        assert refused.status_code == 409 and typed['number'] in refused.json()['detail'], refused.text
    assert journal_count(connect, ids) == 1

    # A payment to another supplier cannot settle this supplier's scanned bill.
    other = _scanned_bill(connect, ids, number='INV-9000')
    with connect() as db:
        stranger = db.execute("INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,'Another Supplier') RETURNING id", (ids['tenant'],)).fetchone()['id']
    pay = call('POST', '/api/erp/documents/purchase-payment', admin, json={
        'contactId': str(stranger), 'documentDate': '2026-08-28', 'amount': '106.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': str(other), 'source': 'scanned', 'amount': '106.00'}], 'status': 'ready'})
    assert pay.status_code == 422 and 'another contact' in pay.json()['detail']

    # A settlement made by a typed payment cannot be changed from the generic allocation endpoints.
    paid = ok(call('POST', '/api/erp/documents/purchase-payment', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-28', 'amount': '150.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': str(other), 'source': 'scanned', 'amount': '106.00'}], 'status': 'ready'}), 201)
    with connect() as db:
        settlement = db.execute("SELECT settlement_id FROM erp_document_headers WHERE id=%s", (paid['id'],)).fetchone()['settlement_id']
        assert db.execute("SELECT amount FROM settlements WHERE id=%s", (settlement,)).fetchone()['amount'] == Decimal('106.00')
        allocation = db.execute("SELECT id FROM settlement_allocations WHERE settlement_id=%s", (settlement,)).fetchone()['id']
    assert call('POST', f'/api/ledger/allocations/{allocation}/reverse', admin).status_code == 409
    assert call('POST', '/api/ledger/allocations', admin, json={
        'settlementId': str(settlement), 'recordId': str(record), 'amount': '1.00'}).status_code == 409
