"""P1 statement of account on the real schema: one customer's receivable and one supplier's payable.

Proves, through the real routes on a disposable loopback database, that the closing balance equals
(a) that contact's open items in the AR/AP ageing report at the end date and (b) that contact's net in the
receivable/payable control account; a credit note and a part payment appear once, a void is excluded, a scanned
copy of a typed bill is counted once; and on the KNT books one customer and one supplier tie to `tally`.
Skipped without TEST_DATABASE_URL.
"""
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from tests.integration.test_erp_documents_postgres import (  # noqa: F401  (fixtures are used by name)
    _scanned_bill, call, company, database, ok,
)
from tests.integration.test_knt_books_postgres import local_knt  # noqa: F401
from services.routes.reporting import financial, statement  # after main (imported above): avoids a circular import

pytestmark = pytest.mark.runtime_defaults
TODAY = datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date()


@pytest.fixture
def books(company):
    connect, ids = company
    with patch.object(statement, 'connect', side_effect=connect):
        yield connect, ids


def _doc(admin, kind, body, status=201):
    return ok(call('POST', f'/api/erp/documents/{kind}', admin, json={'status': 'ready', **body}), status)


def _statement(user, contact, start, end=TODAY, **params):
    return call('GET', f'/api/contacts/{contact}/statement', user, params={'from': str(start), 'to': str(end), **params})


def _control_net(connect, ids, code, source_ids):
    """The contact's net on the control account: journals of its typed and scanned documents (and their reversals).
    Receivable = debit - credit; payable = credit - debit."""
    with connect() as db:
        row = db.execute("""SELECT COALESCE(SUM(l.debit-l.credit),0) AS net FROM journal_headers j
            JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id
            JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
            LEFT JOIN journal_headers original ON original.id=j.reversed_journal_id AND original.tenant_id=j.tenant_id
            WHERE j.tenant_id=%s AND a.code=%s AND j.status IN ('posted','reversed') AND j.journal_date<=%s
                AND COALESCE(original.source_id,j.source_id)=ANY(%s::uuid[])""",
                         (ids['tenant'], code, TODAY, [str(s) for s in source_ids])).fetchone()
    return row['net'] if code == '1100' else -row['net']


def _contact_ageing(connect, ids, report_type, name):
    with connect() as db:
        report = financial._ageing(db, ids['tenant'], TODAY, report_type)
    return sum((r['outstanding'] for r in report['rows'] if r['contactName'] == name), Decimal('0'))


def _lines(amount, description='Goods'):
    return [{'description': description, 'unitPrice': amount}]


def test_customer_statement_ties_to_ageing_and_receivable_control(books):
    connect, ids = books
    admin = ids['admin']
    customer = str(ids['customer'])
    a = _doc(admin, 'invoice', {'contactId': customer, 'documentDate': '2026-08-03', 'dueDate': '2026-08-17',
                                'lines': _lines('1000.00')})
    b = _doc(admin, 'invoice', {'contactId': customer, 'documentDate': '2026-08-10', 'dueDate': '2026-08-24',
                                'lines': _lines('500.00')})
    note = _doc(admin, 'credit-note', {'contactId': customer, 'documentDate': '2026-08-12', 'referenceNo': a['number'],
                                       'lines': _lines('200.00'),
                                       'allocations': [{'targetId': a['id'], 'source': 'typed', 'amount': '200.00'}]})
    voided = _doc(admin, 'invoice', {'contactId': customer, 'documentDate': '2026-08-15', 'lines': _lines('400.00')})
    ok(call('POST', f"/api/erp/documents/invoice/{voided['id']}/transition", admin, json={'status': 'void', 'reason': 'Typo'}))
    part = _doc(admin, 'payment', {'contactId': customer, 'documentDate': '2026-08-20', 'amount': '300.00',
                                   'bankAccountId': str(ids['bank']),
                                   'allocations': [{'targetId': a['id'], 'source': 'typed', 'amount': '300.00'}]})

    report = ok(_statement(ids['accountant'], customer, '2026-08-11'))
    assert report['kind'] == 'customer' and report['contact']['name'] == 'Kedai Runcit Maju'
    assert report['openingBalance'] == '1500.00'  # both invoices are before the start date
    assert [(l['date'], l['label'], l['number'], l['debit'], l['credit'], l['balance']) for l in report['lines']] == [
        ('2026-08-12', 'Credit note', note['number'], '0.00', '200.00', '1300.00'),
        ('2026-08-20', 'Payment received', part['number'], '0.00', '300.00', '1000.00'),
    ]
    assert report['closingBalance'] == '1000.00'
    numbers = [l['number'] for l in ok(_statement(admin, customer, '2026-08-01'))['lines']]
    assert numbers == [a['number'], b['number'], note['number'], part['number']]  # each once; the void never

    closing = Decimal(report['closingBalance'])
    ageing = report['ageing']
    assert ageing['reconciles'] and ageing['unallocatedCredits'] == '0.00'
    assert Decimal(ageing['openItems']) == closing == _contact_ageing(connect, ids, 'receivables', 'Kedai Runcit Maju')
    assert sorted((i['number'], i['outstanding']) for i in ageing['items']) == sorted(
        [(a['number'], '500.00'), (b['number'], '500.00')])
    assert ageing['totals']['90+'] == '1000.00'  # both were due in August
    typed = [a['id'], b['id'], note['id'], voided['id'], part['id']]
    assert _control_net(connect, ids, '1100', typed) == closing

    # A payment on account lowers the balance but is not an open item: the footer shows it, so it still reconciles.
    on_account = _doc(admin, 'payment', {'contactId': customer, 'documentDate': '2026-08-25', 'amount': '50.00',
                                         'bankAccountId': str(ids['bank'])})
    after = ok(_statement(admin, customer, '2026-08-01'))
    assert after['closingBalance'] == '950.00'
    assert after['ageing']['openItems'] == '1000.00' and after['ageing']['unallocatedCredits'] == '50.00'
    assert after['ageing']['balance'] == '950.00' and after['ageing']['reconciles']
    assert _control_net(connect, ids, '1100', [*typed, on_account['id']]) == Decimal('950.00')

    # An end date before later documents leaves them out, and the ageing is taken at that date.
    early = ok(_statement(admin, customer, '2026-07-01', '2026-08-05'))
    assert early['openingBalance'] == '0.00' and early['closingBalance'] == '1000.00'
    assert [l['number'] for l in early['lines']] == [a['number']]
    assert early['ageing']['reconciles'] and early['ageing']['totals']['current'] == '1000.00'


def test_supplier_statement_counts_scanned_documents_once_and_ties_to_payable_control(books):
    connect, ids = books
    admin = ids['admin']
    supplier = str(ids['supplier'])
    bill = _doc(admin, 'bill', {'contactId': supplier, 'documentDate': '2026-08-05', 'dueDate': '2026-09-04',
                                'referenceNo': 'SUP-800', 'lines': _lines('800.00', 'Cement')})
    scanned = _scanned_bill(connect, ids, number='INV-600', total='600.00', tax='0')
    ok(call('POST', f'/api/erp/scanned/{scanned}/post', admin, json={}))
    _scanned_bill(connect, ids, number='sup-800', total='800.00', tax='0')  # the typed bill, scanned again
    payment = _doc(admin, 'purchase-payment', {'contactId': supplier, 'documentDate': '2026-08-20', 'amount': '250.00',
                                               'bankAccountId': str(ids['bank']),
                                               'allocations': [{'targetId': str(scanned), 'source': 'scanned', 'amount': '250.00'}]})
    note = _doc(admin, 'purchase-credit-note', {'contactId': supplier, 'documentDate': '2026-08-22', 'referenceNo': 'SCN-1',
                                                'lines': _lines('100.00', 'Returned'),
                                                'allocations': [{'targetId': bill['id'], 'source': 'typed', 'amount': '100.00'}]})
    voided = _doc(admin, 'bill', {'contactId': supplier, 'documentDate': '2026-08-23', 'referenceNo': 'SUP-300',
                                  'lines': _lines('300.00', 'Sand')})
    ok(call('POST', f"/api/erp/documents/bill/{voided['id']}/transition", admin, json={'status': 'void', 'reason': 'Duplicate'}))

    report = ok(_statement(ids['accountant'], supplier, '2026-08-01'))
    assert report['kind'] == 'supplier' and report['openingBalance'] == '0.00'
    # The payment of the scanned bill shows once (not again as its settlement); the scanned copy of SUP-800 never.
    assert [(l['label'], l['number'], l['source'], l['debit'], l['credit'], l['balance']) for l in report['lines']] == [
        ('Bill', bill['number'], 'typed', '0.00', '800.00', '800.00'),
        ('Bill', 'INV-600', 'scanned', '0.00', '600.00', '1400.00'),
        ('Payment made', payment['number'], 'typed', '250.00', '0.00', '1150.00'),
        ('Supplier credit note', note['number'], 'typed', '100.00', '0.00', '1050.00'),
    ]
    closing = Decimal(report['closingBalance'])
    assert closing == Decimal('1050.00')
    ageing = report['ageing']
    assert ageing['reconciles'] and Decimal(ageing['openItems']) == closing
    assert closing == _contact_ageing(connect, ids, 'payables', 'Syarikat Bekalan Jaya')
    assert sorted((i['number'], i['outstanding']) for i in ageing['items']) == sorted(
        [(bill['number'], '700.00'), ('INV-600', '350.00')])
    with connect() as db:
        scanned_document = db.execute("SELECT document_id FROM standardised_records WHERE id=%s", (scanned,)).fetchone()['document_id']
    sources = [bill['id'], payment['id'], note['id'], voided['id'], scanned_document]
    assert _control_net(connect, ids, '2000', sources) == closing


def test_statement_pdf_and_request_checks(books):
    connect, ids = books
    admin = ids['admin']
    customer = str(ids['customer'])
    _doc(admin, 'invoice', {'contactId': customer, 'documentDate': '2026-08-03', 'dueDate': '2026-08-17',
                            'lines': _lines('120.00')})
    response = _statement(ids['accountant'], customer, '2026-08-01', format='pdf')
    assert response.status_code == 200 and response.headers['content-type'] == 'application/pdf'
    assert response.content.startswith(b'%PDF-1.4') and 'statement-' in response.headers['content-disposition']
    from tests.unit.test_erp_pdf import pages_text
    text = '\n'.join(pages_text(response.content))
    assert 'STATEMENT OF ACCOUNT' in text and 'Kedai Runcit Maju' in text and 'Opening balance' in text
    assert 'Amount owed by the customer' in text and '120.00' in text and 'Over 90 days' in text

    assert _statement(admin, customer, '2026-08-01', format='xml').status_code == 422
    assert _statement(admin, customer, '2026-08-31', '2026-08-01').status_code == 422
    assert call('GET', f'/api/contacts/{customer}/statement', admin,
                params={'from': '2026-8-1', 'to': '2026-08-31'}).status_code == 422
    assert _statement(admin, '00000000-0000-0000-0000-000000000000', '2026-08-01').status_code == 404
    assert _statement(ids['outsider'], customer, '2026-08-01').status_code == 404  # another company's contact
    with connect() as db:
        employee = db.execute("INSERT INTO erp_employees(tenant_id,legal_name) VALUES (%s,'Siti') RETURNING id",
                              (ids['tenant'],)).fetchone()['id']
    assert _statement(admin, employee, '2026-08-01').status_code == 422
    with connect() as db:
        before = db.execute("SELECT count(*) AS n FROM journal_headers WHERE tenant_id=%s", (ids['tenant'],)).fetchone()['n']
    ok(_statement(admin, customer, '2026-08-01'))
    with connect() as db:  # read-only: nothing is posted
        assert db.execute("SELECT count(*) AS n FROM journal_headers WHERE tenant_id=%s",
                          (ids['tenant'],)).fetchone()['n'] == before


def test_knt_statements_tie_to_tally(local_knt):
    """On the KNT books, every customer's and supplier's closing balance at the last cutoff adds up to tally's AR/AP,
    and one customer and one supplier with transactions tie item by item to the ageing report."""
    from services.admin.knt_reset import reset
    from services.admin.knt_seed import locked_database
    from services.admin.knt_tally import tally

    with locked_database() as connect:
        reset(connect, local_knt, "FixtureDemo!2026")
        with connect() as db:
            summaries = tally(db)
            cutoff, ar, ap = summaries[-1][0], summaries[-1][3], summaries[-1][4]
            tenant = db.execute("SELECT id FROM tenants WHERE tenant_code='KNT'").fetchone()['id']
            totals, checked = {}, {}
            for kind, table, column in (('customer', 'customers', 'customer_name'), ('supplier', 'vendors', 'vendor_name')):
                totals[kind] = Decimal('0')
                for contact in db.execute(f"SELECT id,{column} AS name FROM {table} WHERE tenant_id=%s ORDER BY {column}",
                                          (tenant,)).fetchall():
                    report = statement.statement(db, tenant, contact['id'], date(2026, 10, 1), cutoff)
                    assert report['ageing']['reconciles'], (contact['name'], report['ageing'])
                    totals[kind] += report['closingBalance']
                    if report['lines'] and report['closingBalance'] and kind not in checked:
                        checked[kind] = (contact['name'], report)
            assert totals == {'customer': ar, 'supplier': ap}, (totals, ar, ap)
            assert set(checked) == {'customer', 'supplier'}
            for kind, (name, report) in checked.items():
                ageing = financial._ageing(db, tenant, cutoff, 'receivables' if kind == 'customer' else 'payables')
                mine = sum((r['outstanding'] for r in ageing['rows'] if r['contactName'] == name), Decimal('0'))
                assert report['closingBalance'] == report['ageing']['openItems'] == mine, (name, report['ageing'])
                movement = sum((line['amount'] for line in report['lines']), Decimal('0'))
                assert report['openingBalance'] + movement == report['closingBalance']
