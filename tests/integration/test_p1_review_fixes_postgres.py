"""Codex review of the P0/P1 backend: the statement of account and the business summary agree with the posted ledger.

Findings not covered by the review fixes already on the original PR branches (bank replay and preview, summary AR/AP,
statement payments and PDF name). Each test reproduces one through the real routes on a disposable loopback database
(TEST_DATABASE_URL): an approved but unposted scan is not owed yet, opening balances reach the contact the company pack
names by id, typed numbers that repeat across document types stay apart, every cash or bank account counts in the bank
balance, and on the KNT books the statements and the summary equal the control accounts with an unposted match present.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from tests.integration.test_erp_documents_postgres import (  # noqa: F401  (fixtures are used by name)
    _period, _scanned_bill, call, company, database, ok,
)
from tests.integration.test_knt_books_postgres import local_knt  # noqa: F401
from services.routes.reporting import business_summary, financial, statement  # after main (imported above)

pytestmark = pytest.mark.runtime_defaults
END = date(2026, 8, 31)


@pytest.fixture
def books(company):
    connect, ids = company
    with patch.object(statement, 'connect', side_effect=connect):
        yield connect, ids


def _statement(user, contact, start='2026-08-01', end=END, **params):
    return call('GET', f'/api/contacts/{contact}/statement', user, params={'from': start, 'to': str(end), **params})


def _journal(ids, key, day, lines, source_type='manual', source_id=None):
    """Post a balanced journal through the ledger route: lines are (account id, debit, credit, description)."""
    return ok(call('POST', '/api/ledger/journals', ids['admin'], json={
        'periodId': str(ids['period']), 'idempotencyKey': key, 'sourceType': source_type,
        'sourceId': str(source_id) if source_id else None, 'journalDate': day, 'description': key,
        'lines': [{'accountId': str(account), 'debit': str(debit), 'credit': str(credit), 'description': text}
                  for account, debit, credit, text in lines]}), 201)


def _setup(connect, ids):
    ids['period'] = _period(connect, ids, '2026-08')
    with connect() as db:
        ids['accounts']['3000'] = db.execute("""INSERT INTO ledger_accounts(tenant_id,code,name,account_type)
            VALUES (%s,'3000','Capital','equity') RETURNING id""", (ids['tenant'],)).fetchone()['id']
    return connect, ids


def _control(connect, ids, code):
    with connect() as db:
        net = {row['code']: row['debit'] - row['credit'] for row in financial._account_balances(db, ids['tenant'], END)}
    amount = net.get(code, Decimal('0.00'))
    return str(amount if code == '1100' else -amount)


def test_an_approved_scan_counts_once_it_is_posted(books):
    """Codex P1 (statement.py:136): approving a supplier scan for later posting does not change the payable yet."""
    connect, ids = _setup(*books)
    admin, supplier = ids['admin'], str(ids['supplier'])
    record = _scanned_bill(connect, ids, number='INV-900', total='900.00', tax='0')
    report = ok(_statement(admin, supplier))
    assert report['lines'] == [] and report['closingBalance'] == '0.00' == _control(connect, ids, '2000')
    assert report['ageing']['reconciles']

    ok(call('POST', f'/api/erp/scanned/{record}/post', admin, json={}))
    report = ok(_statement(admin, supplier))
    assert [(line['number'], line['credit']) for line in report['lines']] == [('INV-900', '900.00')]
    assert report['closingBalance'] == '900.00' == _control(connect, ids, '2000')
    assert report['ageing']['reconciles']


def test_an_opening_balance_reaches_the_contact_the_pack_names_by_id(books):
    """Codex P1 (statement.py:209): the opening import labels its line with the pack's contact id, not the name."""
    connect, ids = _setup(*books)
    customer = str(ids['customer'])
    with connect() as db:
        db.execute("UPDATE customers SET contact_code='KNT-C01' WHERE id=%s", (customer,))
    _journal(ids, 'opening-balances:2026-08-01', '2026-08-01', [
        (ids['accounts']['1100'], '1200.00', '0', 'Opening receivable OB-INV-1, contact KNT-C01, dated 2026-07-15'),
        (ids['accounts']['3000'], '0', '1200.00', 'Opening balance')])

    report = ok(_statement(ids['admin'], customer))
    assert [(line['label'], line['debit']) for line in report['lines']] == [('Opening balance', '1200.00')]
    assert report['closingBalance'] == '1200.00' == _control(connect, ids, '1100')
    assert report['ageing']['reconciles'] and report['ageing']['openItems'] == '1200.00'


def test_the_contacts_import_keeps_the_pack_contact_id_as_the_code(books):
    """The same finding upstream: an imported contact keeps the id its opening balances name, unless it is taken."""
    from services.imports import contacts

    connect, ids = books
    blank = {'ssm': '', 'address': '', 'default_account_code': '', 'category': '', 'tin': '', 'sst': '',
             'currency': '', 'payment_terms_days': '', 'email': '', 'phone': ''}
    rows = [{'status': 'new', 'values': {**blank, 'contact_id': 'PX-C01', 'type': 'customer', 'name': 'Pelanggan Baru'}},
            {'status': 'new', 'values': {**blank, 'contact_id': 'PX-S01', 'type': 'supplier', 'name': 'Pembekal Baru'}}]
    with connect() as db:
        db.execute("UPDATE customers SET contact_code='px-c01' WHERE id=%s", (ids['customer'],))  # already taken
        contacts.apply(db, ids['admin'], rows)
        codes = {row['name']: row['contact_code'] for row in db.execute(
            """SELECT customer_name AS name,contact_code FROM customers WHERE tenant_id=%(t)s
               UNION ALL SELECT vendor_name,contact_code FROM vendors WHERE tenant_id=%(t)s""",
            {'t': ids['tenant']}).fetchall()}
    assert (codes['Pelanggan Baru'], codes['Pembekal Baru']) == (None, 'PX-S01')


def test_a_typed_number_shared_by_two_document_types_stays_with_its_own_contact(books):
    """Codex P1 (statement.py:226): numbering is unique per document type, so the footer matches by document."""
    connect, ids = books
    admin, customer = ids['admin'], str(ids['customer'])
    with connect() as db:
        other = str(db.execute("INSERT INTO customers(tenant_id,customer_name) VALUES (%s,'Kedai Lain') RETURNING id",
                               (ids['tenant'],)).fetchone()['id'])
    ok(call('POST', '/api/erp/documents/invoice', admin, json={
        'status': 'ready', 'number': '001', 'contactId': customer, 'documentDate': '2026-08-03',
        'lines': [{'description': 'Goods', 'unitPrice': '100.00'}]}), 201)
    invoice = ok(call('POST', '/api/erp/documents/invoice', admin, json={
        'status': 'ready', 'number': '002', 'contactId': other, 'documentDate': '2026-08-03',
        'lines': [{'description': 'Goods', 'unitPrice': '50.00'}]}), 201)
    ok(call('POST', '/api/erp/documents/debit-note', admin, json={
        'status': 'ready', 'number': '001', 'contactId': other, 'documentDate': '2026-08-04',
        'relatedDocumentId': invoice['id'], 'lines': [{'description': 'Extra', 'unitPrice': '70.00'}]}), 201)

    ageing = ok(_statement(admin, customer))['ageing']
    assert [(item['number'], item['outstanding']) for item in ageing['items']] == [('001', '100.00')]
    assert ageing['openItems'] == '100.00' and ageing['reconciles']
    other_ageing = ok(_statement(admin, other))['ageing']
    assert other_ageing['openItems'] == '120.00' and other_ageing['reconciles']


def test_every_bank_account_counts_in_the_bank_balance(books):
    """Codex P1 (business_summary.py:88): a second bank account without a catalog entry or a Bank/Cash name prefix."""
    connect, ids = _setup(*books)
    with connect() as db:
        maybank = db.execute("""INSERT INTO ledger_accounts(tenant_id,code,name,account_type)
            VALUES (%s,'1030','Maybank Current Account','asset') RETURNING id""", (ids['tenant'],)).fetchone()['id']
    _journal(ids, 'capital-injection', '2026-08-05', [(maybank, '20000.00', '0', 'Capital paid in'),
                                                      (ids['accounts']['3000'], '0', '20000.00', 'Capital')])
    with connect() as db:
        balances = {row['account_id']: row['debit'] - row['credit']
                    for row in financial._account_balances(db, ids['tenant'], END)}
        bank = business_summary._bank_balances(END, balances, business_summary._cash_accounts(db, ids['tenant']))
    balances = {account['code']: account['balance'] for account in bank['accounts']}
    assert balances['1030'] == Decimal('20000.00') and bank['total'] == Decimal('20000.00')
    assert '1100' not in balances and '1140' not in balances  # receivables and tax paid are not cash


def test_knt_statements_and_summary_equal_the_control_accounts_with_an_unposted_match(local_knt):
    """The acceptance check: on the KNT books, Sep/Oct/Nov, with one confirmed-but-unposted payment present.
    Covers this branch together with the statement and summary fixes it carries from their PR branches."""
    from ai.synthetic.knt_pack import MONTHS
    from services.admin.knt_reset import reset
    from services.admin.knt_seed import locked_database

    with locked_database() as connect:
        reset(connect, local_knt, "FixtureDemo!2026")
        with connect() as db:
            tenant = db.execute("SELECT id FROM tenants WHERE tenant_code='KNT'").fetchone()['id']
            admin = db.execute("SELECT id FROM users WHERE tenant_id=%s AND role='local_admin' LIMIT 1",
                               (tenant,)).fetchone()['id']
            # The first posted bill still open at a cutoff gets a confirmed settlement whose payment is never posted.
            posted = {row['id'] for row in db.execute("""SELECT r.id FROM standardised_records r
                JOIN journal_headers j ON j.tenant_id=r.tenant_id AND j.source_type='document' AND j.source_id=r.document_id
                    AND j.status='posted' WHERE r.tenant_id=%s""", (tenant,)).fetchall()}
            first, bill = next((cutoff, row) for cutoff in MONTHS.values()
                               for row in financial._ageing(db, tenant, cutoff, 'payables', ids=True)['rows']
                               if row['source'] == 'scanned' and row['itemId'] in {str(i) for i in posted})
            settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,
                    currency,source_type,created_by)
                VALUES (%s,'KNT-UNPOSTED',%s,'money_out',%s,'MYR','manual_record',%s) RETURNING id""",
                                    (tenant, first, bill['outstanding'], admin)).fetchone()['id']
            db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by)
                VALUES (%s,%s,%s,%s,%s)""", (tenant, settlement, bill['itemId'], bill['outstanding'], admin))
        with connect() as db:
            # The operational ageing already treats the bill as settled; the ledger views must not.
            assert not any(row['itemId'] == bill['itemId']
                           for row in financial._ageing(db, tenant, first, 'payables', ids=True)['rows'])
            for cutoff in MONTHS.values():
                net = {row['code']: row['debit'] - row['credit'] for row in financial._account_balances(db, tenant, cutoff)}
                control = {'customer': net.get('1100', Decimal('0')), 'supplier': -net.get('2000', Decimal('0'))}
                totals = dict.fromkeys(control, Decimal('0'))
                for kind, table in (('customer', 'customers'), ('supplier', 'vendors')):
                    for contact in db.execute(f"SELECT id FROM {table} WHERE tenant_id=%s", (tenant,)).fetchall():
                        report = statement.statement(db, tenant, contact['id'], cutoff.replace(day=1), cutoff)
                        assert report['ageing']['reconciles'], (cutoff, report['contact']['name'], report['ageing'])
                        totals[kind] += report['closingBalance']
                assert totals == control, (cutoff, totals, control)
                position = business_summary.build_summary(db, tenant, start=cutoff.replace(day=1), end=cutoff,
                                                          day=cutoff)['month']['position']
                assert (position['receivables']['total'], position['payables']['total']) == (
                    control['customer'], control['supplier']), cutoff
