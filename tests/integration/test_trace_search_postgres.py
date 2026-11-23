"""Plan P1: trace (`GET /api/trace/{type}/{id}`) and global search (`GET /api/search`) on the real schema.

A disposable loopback database next to TEST_DATABASE_URL gets the consolidated schema; requests go through the real
routes with only the session lookup replaced. Covers: the chain around a KNT-style invoice (quotation, part payment,
credit note, matched bank line, journals and a reversal), the scanned chain (upload, record, settlement, bank match,
journal), accountant upload visibility, tenant isolation for both endpoints, and search by number, contact, reference
and exact amount with a per-type limit, a two-character minimum and escaped wildcards. Both endpoints are read-only.
Skipped without TEST_DATABASE_URL.
"""
import hashlib
from unittest.mock import patch
from uuid import uuid4

import pytest

from tests.integration.test_erp_documents_postgres import (  # noqa: F401  (fixtures are used by name)
    _scanned_bill, call, company, database, invoice_body, ok,
)
from services.routes.search import routes as search_routes
from services.routes.trace import routes as trace_routes

pytestmark = pytest.mark.runtime_defaults


@pytest.fixture
def world(company):
    """The ERP test company, with the two new route modules reading from the same disposable database."""
    connect, ids = company
    patches = [patch.object(module, 'connect', side_effect=connect) for module in (trace_routes, search_routes)]
    for item in patches:
        item.start()
    try:
        yield connect, ids
    finally:
        for item in reversed(patches):
            item.stop()


def trace(user, kind, record_id, status=200):
    response = call('GET', f'/api/trace/{kind}/{record_id}', user)
    assert response.status_code == status, response.text
    return response.json()


def node(result, type_, number=None):
    found = [n for n in result['nodes'] if n['type'] == type_ and (number is None or n['number'] == number)]
    assert len(found) == 1, (type_, number, [(n['type'], n['number']) for n in result['nodes']])
    return found[0]


def counts(connect):
    with connect() as db:
        return dict(db.execute("""SELECT (SELECT count(*) FROM erp_document_headers) AS documents,
            (SELECT count(*) FROM journal_headers) AS journals, (SELECT count(*) FROM audit_logs) AS audit,
            (SELECT count(*) FROM erp_document_events) AS events""").fetchone())


def bank_line(connect, ids, amount='10.00', direction='money_out', description='BANK LINE', reference='REF-1'):
    with connect() as db:
        batch = db.execute("""INSERT INTO bank_imports(tenant_id,source_bank_account_ref,source_hash,input_format,mapping_version,
                imported_by,status) VALUES (%s,'CA',%s,'csv','v1',%s,'confirmed') RETURNING id""",
                           (ids['tenant'], hashlib.sha256(uuid4().bytes).hexdigest(), ids['admin']['id'])).fetchone()['id']
        incoming = direction == 'money_in'
        return str(db.execute("""INSERT INTO bank_transactions(tenant_id,import_id,source_row,posted_date,description,reference,
                debit_decimal,credit_decimal,signed_amount_decimal,direction,currency,fingerprint)
            VALUES (%s,%s,1,'2026-08-21',%s,%s,%s,%s,%s,%s,'MYR',%s) RETURNING id""",
                              (ids['tenant'], batch, description, reference, None if incoming else amount,
                               amount if incoming else None, amount if incoming else '-' + amount, direction,
                               hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()['id'])


def knt_sale(connect, ids):
    """Quotation -> invoice (RM 315.50) -> part payment RM 300 -> credit note RM 10, with the payment on a bank line."""
    admin = ids['admin']
    quote = ok(call('POST', '/api/erp/documents/quotation', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    invoice = ok(call('POST', f"/api/erp/documents/quotation/{quote['id']}/transfer", admin, json={'targetType': 'invoice'}), 201)
    invoice = ok(call('PATCH', f"/api/erp/documents/invoice/{invoice['id']}", admin, json={'documentDate': '2026-08-12'}))
    invoice = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transition", admin, json={'status': 'ready'}))
    payment = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transfer", admin, json={'targetType': 'payment'}), 201)
    payment = ok(call('PATCH', f"/api/erp/documents/payment/{payment['id']}", admin, json={
        'amount': '300.00', 'bankAccountId': str(ids['bank']), 'documentDate': '2026-08-20',
        'allocations': [{'targetId': invoice['id'], 'amount': '300.00'}]}))
    payment = ok(call('POST', f"/api/erp/documents/payment/{payment['id']}/transition", admin, json={'status': 'ready'}))
    note = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transfer", admin, json={'targetType': 'credit-note'}), 201)
    note = ok(call('PATCH', f"/api/erp/documents/credit-note/{note['id']}", admin, json={
        'documentDate': '2026-08-22', 'lines': [{'description': 'Damaged bag', 'quantity': '1', 'unitPrice': '10.00'}]}))
    note = ok(call('POST', f"/api/erp/documents/credit-note/{note['id']}/transition", admin, json={'status': 'ready'}))
    line = bank_line(connect, ids, amount='300.00', direction='money_in', description='KEDAI RUNCIT MAJU', reference='DEP-300')
    with connect() as db:
        db.execute("UPDATE bank_transactions SET status='matched',recorded_document_id=%s WHERE id=%s", (payment['id'], line))
    return quote, invoice, payment, note, line


def test_invoice_chain_has_every_link_and_is_the_same_from_any_end(world):
    connect, ids = world
    quote, invoice, payment, note, line = knt_sale(connect, ids)
    admin = ids['admin']

    chain = trace(admin, 'invoice', invoice['id'])
    assert chain['subject'] == f"erp:{invoice['id']}" and not chain['truncated']
    assert [n['type'] for n in chain['nodes'] if n['kind'] == 'erp'] == ['quotation', 'invoice', 'credit-note', 'payment']
    assert [n['kind'] for n in chain['nodes']] == ['erp', 'erp', 'erp', 'erp', 'bank', 'journal', 'journal', 'journal']
    assert [n['id'] for n in chain['nodes'] if n['isSubject']] == [invoice['id']]

    assert node(chain, 'quotation')['number'] == quote['number'] and node(chain, 'quotation')['status'] == 'accepted'
    assert node(chain, 'invoice')['via'] == [{'text': 'Created from', 'key': f"erp:{quote['id']}", 'number': quote['number'],
                                             'type': 'quotation', 'amount': None, 'status': None}]
    credit = node(chain, 'credit-note')
    assert credit['amount'] == '10.00' and credit['status'] == 'ready'
    assert {(v['text'], v['amount']) for v in credit['via']} == {('Created from', None), ('Applied to', '10.00')}
    paid = node(chain, 'payment')
    assert paid['amount'] == '300.00' and paid['party'] == 'Kedai Runcit Maju'
    assert ('Applied to', '300.00') in {(v['text'], v['amount']) for v in paid['via']}
    bank = node(chain, 'bank-transaction')
    assert bank['amount'] == '300.00' and bank['via'][0]['text'] == 'Matched to' and bank['via'][0]['number'] == paid['number']
    journals = [n for n in chain['nodes'] if n['kind'] == 'journal']
    assert {j['via'][0]['number'] for j in journals} == {invoice['number'], payment['number'], note['number']}
    assert {j['amount'] for j in journals} == {'315.50', '300.00', '10.00'}

    # The same chain is found from the payment, the bank line, a journal, the quotation and the credit note.
    keys = {n['key'] for n in chain['nodes']}
    for kind, record_id in (('payment', payment['id']), ('bank-transaction', line), ('quotation', quote['id']),
                            ('credit-note', note['id']), ('journal', payment['journalId'])):
        assert {n['key'] for n in trace(admin, kind, record_id)['nodes']} == keys, kind

    # Voiding the credit note adds its reversal journal after the original. Tracing itself writes nothing.
    ok(call('POST', f"/api/erp/documents/credit-note/{note['id']}/transition", admin, json={'status': 'void', 'reason': 'Wrong note'}))
    before = counts(connect)
    again = trace(admin, 'invoice', invoice['id'])
    trace(admin, 'bank-transaction', line)
    call('GET', '/api/search', admin, params={'q': 'runcit'})
    assert counts(connect) == before
    assert node(again, 'credit-note')['status'] == 'void'
    reversal = [n for n in again['nodes'] if n['type'] == 'reversal']
    assert len(reversal) == 1 and reversal[0]['via'][0]['text'] == 'Reverses'
    original = next(n for n in again['nodes'] if n['id'] == note['journalId'])
    assert again['nodes'].index(reversal[0]) > again['nodes'].index(original)


def test_scanned_chain_upload_record_settlement_bank_and_journal(world):
    connect, ids = world
    admin, accountant = ids['admin'], ids['accountant']
    record = _scanned_bill(connect, ids, number='INV-7781', total='106.00')
    ok(call('POST', f'/api/erp/scanned/{record}/post', admin, json={}))
    pay = ok(call('POST', '/api/erp/documents/purchase-payment', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-28', 'amount': '106.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': str(record), 'source': 'scanned', 'amount': '106.00'}], 'status': 'ready'}), 201)
    line = bank_line(connect, ids, amount='106.00', description='SYARIKAT BEKALAN JAYA', reference='TT-106')
    with connect() as db:
        document = db.execute("SELECT document_id FROM standardised_records WHERE id=%s", (record,)).fetchone()['document_id']
        candidate = db.execute("""INSERT INTO reconciliation_candidates(tenant_id,bank_transaction_id,record_id,proposed_by)
            VALUES (%s,%s,%s,'accountant') RETURNING id""", (ids['tenant'], line, record)).fetchone()['id']
        db.execute("""INSERT INTO reconciliation_decisions(tenant_id,candidate_id,decision,reviewer_id,expected_revision)
            VALUES (%s,%s,'confirmed',%s,1)""", (ids['tenant'], candidate, admin['id']))

    chain = trace(admin, 'record', record)
    assert [n['kind'] for n in chain['nodes']] == ['upload', 'record', 'erp', 'settlement', 'bank', 'journal', 'journal']
    assert node(chain, 'bill', 'INV-7781')['via'][0]['text'] == 'Scanned from'
    upload = next(n for n in chain['nodes'] if n['kind'] == 'upload')
    assert upload['id'] == str(document) and upload['detail'] == 'bill.pdf'
    settlement = next(n for n in chain['nodes'] if n['kind'] == 'settlement')
    assert settlement['amount'] == '106.00' and any(v['text'] == 'Payment' and v['number'] == pay['number'] for v in settlement['via'])
    assert next(n for n in chain['nodes'] if n['type'] == 'purchase-payment')['number'] == pay['number']
    assert next(n for n in chain['nodes'] if n['kind'] == 'bank')['via'][0]['text'] == 'Matched to'
    assert {n['detail'] for n in chain['nodes'] if n['kind'] == 'journal'} == {'document', 'erp_document'}
    keys = {n['key'] for n in chain['nodes']}
    for kind, record_id in (('upload', str(document)), ('settlement', settlement['id']), ('purchase-payment', pay['id'])):
        assert {n['key'] for n in trace(admin, kind, record_id)['nodes']} == keys, kind

    # The upload belongs to the admin: an accountant who neither owns it nor is assigned it does not see it.
    hidden = trace(accountant, 'record', record)
    assert {n['key'] for n in hidden['nodes']} == keys - {f'upload:{document}'}
    trace(accountant, 'upload', str(document), 404)
    with connect() as db:
        db.execute("UPDATE documents SET assigned_to=%s WHERE id=%s", (accountant['id'], document))
    assert f'upload:{document}' in {n['key'] for n in trace(accountant, 'record', record)['nodes']}


def test_trace_is_tenant_scoped_and_validates_input(world):
    connect, ids = world
    quote, invoice, payment, note, line = knt_sale(connect, ids)
    outsider, admin = ids['outsider'], ids['admin']
    for kind, record_id in (('invoice', invoice['id']), ('payment', payment['id']), ('bank-transaction', line),
                            ('journal', payment['journalId']), ('quotation', quote['id'])):
        trace(outsider, kind, record_id, 404)
    trace(admin, 'bill', invoice['id'], 404)  # the id exists, but not as a bill
    trace(admin, 'no-such-type', invoice['id'], 422)
    trace(admin, 'invoice', 'not-a-uuid', 422)
    trace(admin, 'invoice', str(uuid4()), 404)
    platform = {**admin, 'role': 'super_admin', 'tenant_id': None}
    assert call('GET', f"/api/trace/invoice/{invoice['id']}", platform).status_code == 403


def test_search_by_number_contact_reference_and_amount_grouped_with_limit(world):
    connect, ids = world
    quote, invoice, payment, note, line = knt_sale(connect, ids)
    admin = ids['admin']

    def find(user, q, **params):
        response = call('GET', '/api/search', user, params={'q': q, **params})
        assert response.status_code == 200, response.text
        return {g['group']: g for g in response.json()['groups']}

    by_number = find(admin, invoice['number'])
    assert [i['id'] for i in by_number['invoice']['items']] == [invoice['id']] and by_number['invoice']['label'] == 'Invoice'
    assert by_number['invoice']['items'][0]['kind'] == 'erp' and by_number['invoice']['items'][0]['amount'] == '315.50'
    assert find(admin, invoice['number'].lower())['invoice']['items'][0]['id'] == invoice['id']  # not case sensitive
    assert next(iter(find(admin, invoice['number']))) == 'invoice'  # the exact number match leads, ahead of the credit note that cites it

    by_contact = find(admin, 'runcit')
    assert {'quotation', 'invoice', 'payment', 'credit-note', 'contact', 'bank-transaction'} <= set(by_contact)
    assert by_contact['contact']['items'][0]['title'] == 'Kedai Runcit Maju' and by_contact['contact']['items'][0]['type'] == 'customer'

    by_reference = find(admin, quote['number'])  # the invoice's reference is the quotation it came from
    assert {'quotation', 'invoice'} <= set(by_reference)
    assert find(admin, 'DEP-300')['bank-transaction']['items'][0]['id'] == line

    for text in ('315.50', 'RM 315.50', 'myr315.5'):
        assert invoice['id'] in {i['id'] for i in find(admin, text)['invoice']['items']}, text
    assert {i['id'] for i in find(admin, '300.00')['payment']['items']} == {payment['id']}
    assert 'bank-transaction' in find(admin, '300')  # a bank line is found by its amount in either direction
    assert 'journal' in find(admin, '10.00') and 'credit-note' in find(admin, '10.00')
    assert find(admin, '31550') == {}  # an amount is exact, not a digit pattern

    # Per-type limit: a second invoice for the same contact; limit=1 returns one row per type.
    second = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    both = find(admin, 'runcit')
    assert {i['id'] for i in both['invoice']['items']} == {invoice['id'], second['id']}
    assert all(len(g['items']) == 1 for g in find(admin, 'runcit', limit=1).values())
    assert max(len(g['items']) for g in find(admin, 'runcit', limit=500).values()) <= 20

    # Minimum two characters; wildcards and quotes are literal text.
    for short in ('', ' ', 'k', ' k '):
        assert call('GET', '/api/search', admin, params={'q': short}).status_code == 422
    assert call('GET', '/api/search', admin, params={'q': 'x' * 101}).status_code == 422
    assert find(admin, '%%') == {} and find(admin, '__') == {} and find(admin, "'; DROP TABLE tenants;--") == {}
    with connect() as db:
        assert db.execute("SELECT to_regclass('tenants') IS NOT NULL AS ok").fetchone()['ok']


def test_search_is_tenant_scoped_and_hides_other_accountants_uploads(world):
    connect, ids = world
    knt_sale(connect, ids)
    record = _scanned_bill(connect, ids, number='INV-7781', total='106.00')
    outsider, admin, accountant = ids['outsider'], ids['admin'], ids['accountant']
    with connect() as db:
        other_customer = db.execute("INSERT INTO customers(tenant_id,customer_name) VALUES (%s,'Kedai Runcit Lain') RETURNING id",
                                    (outsider['tenant_id'],)).fetchone()['id']
        code = db.execute("SELECT d.document_code FROM standardised_records r JOIN documents d ON d.id=r.document_id WHERE r.id=%s",
                          (record,)).fetchone()['document_code']
    for text in ('runcit', 'IV-', '315.50', 'DEP-300', 'INV-7781', 'Bekalan', code):
        mine = call('GET', '/api/search', admin, params={'q': text}).json()['groups']
        assert mine, text
        theirs = call('GET', '/api/search', outsider, params={'q': text}).json()['groups']
        assert {i['id'] for g in theirs for i in g['items']} <= {str(other_customer)}, (text, theirs)
    platform = {**admin, 'role': 'super_admin', 'tenant_id': None}
    assert call('GET', '/api/search', platform, params={'q': 'runcit'}).status_code == 403

    scanned = {g['group']: g for g in call('GET', '/api/search', admin, params={'q': 'INV-7781'}).json()['groups']}
    assert scanned['record']['items'][0]['id'] == str(record) and scanned['record']['items'][0]['status'] == 'approved'
    assert 'upload' in {g['group'] for g in call('GET', '/api/search', admin, params={'q': code}).json()['groups']}
    # The upload is the admin's: the accountant finds the record (company-wide) but not the file.
    assert 'record' in {g['group'] for g in call('GET', '/api/search', accountant, params={'q': 'INV-7781'}).json()['groups']}
    assert 'upload' not in {g['group'] for g in call('GET', '/api/search', accountant, params={'q': code}).json()['groups']}


def test_trace_keeps_two_allocations_between_the_same_settlement_and_record(world):
    """Codex P2: RM40 then RM60 from one settlement to one bill are two links, not one."""
    connect, ids = world
    record = _scanned_bill(connect, ids, number='INV-100', total='100.00', tax='0')
    with connect() as db:
        settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,
                currency,source_type,created_by)
            VALUES (%s,'SET-SPLIT','2026-08-20','money_out',100,'MYR','manual_record',%s) RETURNING id""",
                                (ids['tenant'], ids['admin']['id'])).fetchone()['id']
        for amount in ('40.00', '60.00'):
            db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by)
                VALUES (%s,%s,%s,%s,%s)""", (ids['tenant'], settlement, record, amount, ids['admin']['id']))
    for kind, start in (('record', record), ('settlement', settlement)):
        found = next(n for n in trace(ids['admin'], kind, start)['nodes'] if n['kind'] == 'settlement')
        assert sorted(v['amount'] for v in found['via'] if v['text'] == 'Applied to') == ['40.00', '60.00'], kind


def test_search_puts_an_exact_scanned_number_ahead_of_newer_partial_matches(world):
    """Codex P2: the exact invoice number must survive the limit, however many newer numbers start with it."""
    connect, ids = world
    exact = _scanned_bill(connect, ids, number='INV-12', total='12.00', tax='0')
    for number in ('INV-120', 'INV-121', 'INV-122'):  # same invoice date, recorded later: newer than the exact match
        _scanned_bill(connect, ids, number=number, total='5.00', tax='0')
    response = call('GET', '/api/search', ids['admin'], params={'q': 'INV-12', 'limit': 2})
    assert response.status_code == 200, response.text
    records = next(g for g in response.json()['groups'] if g['group'] == 'record')['items']
    assert records[0]['id'] == str(exact) and len(records) == 2
