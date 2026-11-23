"""Plan M (S1) on the real schema: the sales flow KNT issues, printed copies, direction and naming.

Same disposable database and request harness as test_erp_documents_postgres.py. Checks the "done when" rules of M1
(issue twice posts once; a credit note never exceeds what is outstanding; void reverses; partial payment; numbering
unique under concurrency), B1 (typed documents never reach extraction), B8 (one stored, unchangeable PDF per
revision), M2 (a captured document where the company is the seller never posts) and M4 (display names).
Skipped without TEST_DATABASE_URL.
"""
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pypdf import PdfReader

from tests.integration.test_erp_documents_postgres import (  # noqa: F401  (fixtures)
    balanced, call, company, database, invoice_body, journal_count, journal_lines, ok, shape)

pytestmark = pytest.mark.runtime_defaults


def issue(path_type, doc, user):
    return call('POST', f"/api/erp/documents/{path_type}/{doc['id']}/transition", user, json={'status': 'ready'})


def pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)


def set_profile(connect, ids):
    with connect() as db:
        db.execute("""UPDATE tenants SET name=%s, registration_number=%s, profile=%s WHERE id=%s""",
                   ('Ketapang Nibong Trading Sdn. Bhd. ' + uuid4().hex[:4], uuid4().hex[:12], Jsonb({
                       'tin': 'C99999999990', 'sstNumber': 'W10-0000-99999999', 'address': 'Lot 1, Jalan Contoh\n93000 Kuching',
                       'bankName': 'Demo Bank', 'bankAccountName': 'KNT (fictional)', 'bankAccountNumber': '000-111-222'}),
                    ids['tenant']))
        return db.execute("SELECT name FROM tenants WHERE id=%s", (ids['tenant'],)).fetchone()['name']


def test_invoice_issue_twice_posts_once_and_stores_one_unchangeable_copy(company):
    connect, ids = company
    admin = ids['admin']
    company_name = set_profile(connect, ids)
    draft = ok(call('POST', '/api/erp/documents/invoice', ids['accountant'], json=invoice_body(
        ids, referenceNo='PO-77', lines=[{'description': 'Simen 50kg', 'quantity': '10', 'unitPrice': '25.50',
                                          'taxCode': 'SV-8', 'tax': '20.40'}])), 201)
    assert re.fullmatch(r'IV-2026-\d{5}', draft['number']), draft['number']
    assert draft['displayName'].startswith('2026-08-12_') and draft['displayName'].endswith(f"_INV_{draft['number']}.pdf")

    preview = call('GET', f"/api/erp/documents/invoice/{draft['id']}/preview.pdf", ids['accountant'])
    assert preview.status_code == 200 and preview.headers['content-type'] == 'application/pdf'
    text = pdf_text(preview.content)
    assert 'DRAFT' in text and company_name in text and 'C99999999990' in text and 'SV-8' in text
    assert 'This is a computer-generated document. No signature is required.' in text
    assert ok(call('GET', f"/api/erp/documents/invoice/{draft['id']}/files", admin))['items'] == []  # preview stores nothing

    # The accountant drafts; only the company admin issues (posts).
    assert issue('invoice', draft, ids['accountant']).status_code == 403
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda _: issue('invoice', draft, admin), range(6)))
    assert {r.status_code for r in results} == {200}, [r.text for r in results]
    assert journal_count(connect, ids) == 1
    issued = ok(call('GET', f"/api/erp/documents/invoice/{draft['id']}", admin))
    assert issued['status'] == 'ready' and issued['posted'] and issued['contactCode'] == 'KRM'
    assert issued['displayName'] == f"2026-08-12_KRM_INV_{issued['number']}.pdf"
    assert [f['revision'] for f in issued['files']] == [1] and issued['files'][0]['fileName'] == issued['displayName']
    assert shape(connect, issued['journalId']) == [('1100', Decimal('275.40'), 0), ('4000', 0, Decimal('255.00')),
                                                   ('2030', 0, Decimal('20.40'))]

    download = call('GET', f"/api/erp/documents/invoice/{draft['id']}/files/1", admin)
    assert download.status_code == 200 and 'attachment' in download.headers['content-disposition']
    assert hashlib.sha256(download.content).hexdigest() == issued['files'][0]['sha256']
    text = pdf_text(download.content)
    assert issued['number'] in text and 'DRAFT' not in text and 'Kedai Runcit Maju' in text and '275.40' in text
    # The issued preview is the stored copy, byte for byte.
    assert call('GET', f"/api/erp/documents/invoice/{draft['id']}/preview.pdf", admin).content == download.content

    # Edit only in draft; the stored copy cannot be changed or removed.
    assert call('PATCH', f"/api/erp/documents/invoice/{draft['id']}", admin, json={'description': 'x'}).status_code == 409
    with connect() as db:
        for sql in ("UPDATE erp_document_files SET file_name='x.pdf' WHERE document_id=%s",
                    "DELETE FROM erp_document_files WHERE document_id=%s"):
            with pytest.raises(psycopg.errors.RaiseException):
                with db.transaction():
                    db.execute(sql, (draft['id'],))

    # Void posts a reversal and stores a VOID revision; the first copy stays.
    voided = ok(call('POST', f"/api/erp/documents/invoice/{draft['id']}/transition", admin, json={'status': 'void', 'reason': 'Wrong price'}))
    assert voided['status'] == 'void' and balanced(journal_lines(connect, voided['reversalJournalId']))
    assert [(f['revision'], f['reason']) for f in voided['files']] == [(1, 'issued'), (2, 'void')]
    assert 'VOID' in pdf_text(call('GET', f"/api/erp/documents/invoice/{draft['id']}/files/2", admin).content)
    assert journal_count(connect, ids) == 2


def test_numbering_is_per_type_and_year_and_unique_under_concurrency(company):
    _, ids = company
    admin = ids['admin']
    with ThreadPoolExecutor(8) as pool:
        made = list(pool.map(lambda _: call('POST', '/api/erp/documents/invoice', admin, json=invoice_body(ids)), range(16)))
    numbers = [ok(r, 201)['number'] for r in made]
    assert len(set(numbers)) == 16 and all(re.fullmatch(r'IV-2026-\d{5}', n) for n in numbers)
    assert sorted(numbers) == [f'IV-2026-{i:05d}' for i in range(1, 17)]
    older = ok(call('POST', '/api/erp/documents/invoice', admin, json=invoice_body(ids, documentDate='2025-12-30')), 201)
    quote = ok(call('POST', '/api/erp/documents/quotation', admin, json=invoice_body(ids)), 201)
    assert older['number'] == 'IV-2025-00001' and quote['number'] == 'QT-2026-00001'


def test_credit_note_references_an_issued_invoice_and_never_exceeds_outstanding(company):
    connect, ids = company
    admin = ids['admin']
    invoice = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)  # 315.50
    lines = [{'description': 'Return 2 bags', 'quantity': '2', 'unitPrice': '25.50', 'tax': '5.10'}]  # 56.10
    body = {'contactId': str(ids['customer']), 'documentDate': '2026-08-15', 'lines': lines}

    loose = ok(call('POST', '/api/erp/documents/credit-note', admin, json=body), 201)
    refused = issue('credit-note', loose, admin)
    assert refused.status_code == 422 and 'must reference an issued invoice' in refused.json()['detail']

    draft_invoice = ok(call('POST', '/api/erp/documents/invoice', admin, json=invoice_body(ids)), 201)
    on_draft = ok(call('POST', '/api/erp/documents/credit-note', admin, json={**body, 'relatedDocumentId': draft_invoice['id']}), 201)
    assert issue('credit-note', on_draft, admin).status_code == 409

    # Partly paid: 300 of 315.50 -> 15.50 outstanding; a 56.10 credit note is refused, a 15.50 one is accepted.
    ok(call('POST', '/api/erp/documents/payment', admin, json={
        'contactId': str(ids['customer']), 'documentDate': '2026-08-20', 'amount': '300.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': invoice['id'], 'amount': '300.00'}], 'status': 'ready'}), 201)
    too_big = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transfer", admin, json={'targetType': 'credit-note'}), 201)
    assert too_big['relatedDocumentId'] == invoice['id'] and too_big['related']['number'] == invoice['number']
    too_big = ok(call('PATCH', f"/api/erp/documents/credit-note/{too_big['id']}", admin, json={'documentDate': '2026-08-21', 'lines': lines}))
    refused = issue('credit-note', too_big, admin)
    assert refused.status_code == 409 and 'outstanding' in refused.json()['detail']
    fits = ok(call('PATCH', f"/api/erp/documents/credit-note/{too_big['id']}", admin, json={
        'lines': [{'description': 'Price adjustment', 'unitPrice': '15.50'}]}))
    credited = ok(issue('credit-note', fits, admin))
    assert shape(connect, credited['journalId']) == [('1100', 0, Decimal('15.50')), ('4000', Decimal('15.50'), 0)]
    after = ok(call('GET', f"/api/erp/documents/invoice/{invoice['id']}", admin))
    assert after['outstanding'] == '0.00' and after['paymentStatus'] == 'paid'
    # Nothing left: any further credit note is refused.
    again = ok(call('POST', '/api/erp/documents/credit-note', admin, json={**body, 'relatedDocumentId': invoice['id'],
                                                                               'lines': [{'description': 'x', 'unitPrice': '1'}]}), 201)
    assert issue('credit-note', again, admin).status_code == 409
    # Voiding the credit note reverses it and reopens the invoice.
    ok(call('POST', f"/api/erp/documents/credit-note/{credited['id']}/transition", admin, json={'status': 'void'}))
    assert ok(call('GET', f"/api/erp/documents/invoice/{invoice['id']}", admin))['outstanding'] == '15.50'


def test_partial_then_full_payment_and_debit_note(company):
    connect, ids = company
    admin = ids['admin']
    invoice = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)

    def pay(amount, target):
        return ok(call('POST', '/api/erp/documents/payment', admin, json={
            'contactId': str(ids['customer']), 'documentDate': '2026-08-20', 'amount': amount, 'bankAccountId': str(ids['bank']),
            'allocations': [{'targetId': target, 'amount': amount}], 'status': 'ready'}), 201)

    first = pay('100.00', invoice['id'])
    assert re.fullmatch(r'OR-2026-\d{5}', first['number']) and first['files'][0]['fileName'].endswith(f"_OR_{first['number']}.pdf")
    state = ok(call('GET', f"/api/erp/documents/invoice/{invoice['id']}", admin))
    assert state['outstanding'] == '215.50' and state['paymentStatus'] == 'partly_paid'
    receipt = pdf_text(call('GET', f"/api/erp/documents/payment/{first['id']}/files/1", admin).content)
    assert 'OFFICIAL RECEIPT' in receipt and invoice['number'] in receipt

    # A debit note adds a charge to the invoice; it must reference it and is settled like an invoice.
    loose = ok(call('POST', '/api/erp/documents/debit-note', admin, json={
        'contactId': str(ids['customer']), 'documentDate': '2026-08-21', 'lines': [{'description': 'x', 'unitPrice': '1'}]}), 201)
    assert issue('debit-note', loose, admin).status_code == 422
    note = ok(call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transfer", admin, json={'targetType': 'debit-note'}), 201)
    note = ok(call('PATCH', f"/api/erp/documents/debit-note/{note['id']}", admin, json={
        'documentDate': '2026-08-21', 'lines': [{'description': 'Late delivery surcharge', 'unitPrice': '20.00', 'taxCode': 'SV-8', 'tax': '1.60'}]}))
    note = ok(issue('debit-note', note, admin))
    assert re.fullmatch(r'DN-2026-\d{5}', note['number']) and note['outstanding'] == '21.60'
    assert shape(connect, note['journalId']) == [('1100', Decimal('21.60'), 0), ('4000', 0, Decimal('20.00')), ('2030', 0, Decimal('1.60'))]
    refused = call('POST', f"/api/erp/documents/invoice/{invoice['id']}/transition", admin, json={'status': 'void'})
    assert refused.status_code == 409 and note['number'] in refused.json()['detail']

    pay('215.50', invoice['id'])
    pay('21.60', note['id'])
    assert ok(call('GET', f"/api/erp/documents/invoice/{invoice['id']}", admin))['paymentStatus'] == 'paid'
    assert ok(call('GET', f"/api/erp/documents/debit-note/{note['id']}", admin))['paymentStatus'] == 'paid'


def test_quotation_draft_sent_accepted_converted_never_posts(company):
    connect, ids = company
    admin, accountant = ids['admin'], ids['accountant']
    quote = ok(call('POST', '/api/erp/documents/quotation', accountant, json=invoice_body(ids)), 201)
    assert quote['status'] == 'draft' and quote['files'] == []
    assert call('POST', f"/api/erp/documents/quotation/{quote['id']}/transfer", admin, json={'targetType': 'invoice'}).status_code == 409
    sent = ok(call('POST', f"/api/erp/documents/quotation/{quote['id']}/transition", accountant, json={'status': 'sent'}))
    assert sent['status'] == 'sent' and [f['reason'] for f in sent['files']] == ['issued']
    assert 'QUOTATION' in pdf_text(call('GET', f"/api/erp/documents/quotation/{quote['id']}/files/1", admin).content)
    assert call('PATCH', f"/api/erp/documents/quotation/{quote['id']}", admin, json={'description': 'x'}).status_code == 409

    invoice = ok(call('POST', f"/api/erp/documents/quotation/{quote['id']}/transfer", admin, json={'targetType': 'invoice'}), 201)
    assert invoice['status'] == 'draft' and invoice['referenceNo'] == quote['number']
    assert ok(call('GET', f"/api/erp/documents/quotation/{quote['id']}", admin))['status'] == 'accepted'
    again = call('POST', f"/api/erp/documents/quotation/{quote['id']}/transfer", admin, json={'targetType': 'invoice'})
    assert again.status_code == 409 and invoice['number'] in again.json()['detail']

    other = ok(call('POST', '/api/erp/documents/quotation', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    assert other['status'] == 'sent'
    expired = ok(call('POST', f"/api/erp/documents/quotation/{other['id']}/transition", admin, json={'status': 'expired'}))
    assert expired['status'] == 'expired'
    assert call('POST', f"/api/erp/documents/quotation/{other['id']}/transition", admin, json={'status': 'accepted'}).status_code == 409
    assert journal_count(connect, ids) == 0


def test_duplicate_warning_and_tenant_isolation(company):
    _, ids = company
    admin = ids['admin']
    first = ok(call('POST', '/api/erp/documents/invoice', admin, json=invoice_body(ids, referenceNo='PO-1001')), 201)
    assert first['possibleDuplicates'] == []
    second = ok(call('POST', '/api/erp/documents/invoice', admin, json=invoice_body(ids, referenceNo='po-1001 ', documentDate='2026-08-13')), 201)
    assert [(d['number'], d['reason']) for d in second['possibleDuplicates']] == [(first['number'], 'same reference')]
    third = ok(call('POST', '/api/erp/documents/invoice', admin, json=invoice_body(ids)), 201)
    assert first['number'] in [d['number'] for d in third['possibleDuplicates']]  # same amount and date
    assert call('GET', f"/api/erp/documents/invoice/{first['id']}", ids['outsider']).status_code == 404
    assert call('GET', f"/api/erp/documents/invoice/{first['id']}/preview.pdf", ids['outsider']).status_code == 404
    assert call('GET', f"/api/erp/documents/invoice/{first['id']}/files/1", ids['outsider']).status_code == 404


def test_typed_documents_never_reach_extraction_review_or_benchmark(company):
    connect, ids = company
    admin = ids['admin']
    quote = ok(call('POST', '/api/erp/documents/quotation', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    invoice = ok(call('POST', f"/api/erp/documents/quotation/{quote['id']}/transfer", admin, json={'targetType': 'invoice'}), 201)
    invoice = ok(call('PATCH', f"/api/erp/documents/invoice/{invoice['id']}", admin, json={'documentDate': '2026-08-12'}))
    ok(issue('invoice', invoice, admin))
    ok(call('POST', '/api/erp/documents/payment', admin, json={
        'contactId': str(ids['customer']), 'documentDate': '2026-08-20', 'amount': '100.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': invoice['id'], 'amount': '100.00'}], 'status': 'ready'}), 201)
    with connect() as db:
        for table in ('documents', 'pending_jobs', 'review_submissions', 'benchmark_demo_documents'):
            n = db.execute(f"SELECT count(*) AS n FROM {table} WHERE tenant_id=%s", (ids['tenant'],)).fetchone()['n']
            assert n == 0, table
        assert db.execute("SELECT count(*) AS n FROM erp_document_files WHERE tenant_id=%s", (ids['tenant'],)).fetchone()['n'] == 3


def _captured(connect, ids, *, seller, buyer, category='accounts_payable', kind='invoice', number='SUP-1', vendor=None, snapshot=None):
    with connect() as db:
        document = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,
                file_size_bytes,file_hash_sha256,status)
            VALUES (%s,%s,'scan.pdf',%s,%s,'synthetic/none.pdf','application/pdf',100,%s,'completed') RETURNING id""",
                              (ids['tenant'], ids['admin']['id'], kind, category, hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()['id']
        submission = db.execute("""INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision)
            VALUES (%s,%s,%s,'submitted',1) RETURNING id""", (document, ids['tenant'], ids['admin']['id'])).fetchone()['id']
        db.execute("""INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision)
            VALUES (%s,%s,%s,%s,'approved',1)""", (submission, document, ids['tenant'], ids['admin']['id']))
        return db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,vendor_id,supplier_name,buyer_name,
                invoice_number,invoice_date,due_date,currency,total_amount,tax_amount,snapshot)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'2026-08-07','2026-09-06','MYR',106,6,%s) RETURNING id""",
                          (document, ids['tenant'], category, vendor, seller, buyer, number, Jsonb(snapshot or {}))).fetchone()['id']


def test_direction_own_sales_document_never_posts_and_purchases_do(company):
    connect, ids = company
    admin = ids['admin']
    me = set_profile(connect, ids)
    own = _captured(connect, ids, seller=me.upper(), buyer='Kedai Runcit Maju')  # captured as if it were a purchase
    refused = call('POST', f'/api/erp/scanned/{own}/post', admin, json={})
    assert refused.status_code == 409 and 'your own sales document' in refused.json()['detail']
    sale = _captured(connect, ids, seller='Someone Else Sdn Bhd', buyer='Kedai Runcit Maju', category='accounts_receivable', number='X-9')
    assert call('POST', f'/api/erp/scanned/{sale}/post', admin, json={}).status_code == 409
    bill = _captured(connect, ids, seller='Syarikat Bekalan Jaya', buyer=me, number='SBJ-77')
    posted = ok(call('POST', f'/api/erp/scanned/{bill}/post', admin, json={}))
    assert shape(connect, posted['id'])[-1] == ('2000', 0, Decimal('106.00'))
    assert journal_count(connect, ids) == 1
    # Sales lists show typed documents only; purchase lists name captured documents by the M4 rule.
    assert all(d['source'] == 'typed' for d in ok(call('GET', '/api/erp/documents/invoice', admin))['items'])
    listed = {d['displayName'] for d in ok(call('GET', '/api/erp/documents/bill', admin))['items'] if d['source'] == 'scanned'}
    assert '2026-08-07_SBJ_BILL_SBJ-77.pdf' in listed


def test_supplier_debit_and_credit_notes_link_to_their_bill_and_payments_settle_ap(company):
    connect, ids = company
    admin = ids['admin']
    bill = ok(call('POST', '/api/erp/documents/bill', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-05', 'referenceNo': 'SBJ-100', 'status': 'ready',
        'lines': [{'description': 'Goods', 'unitPrice': '100', 'tax': '6'}]}), 201)
    loose = ok(call('POST', '/api/erp/documents/purchase-debit-note', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-06', 'lines': [{'description': 'Freight', 'unitPrice': '10'}]}), 201)
    assert issue('purchase-debit-note', loose, admin).status_code == 422
    debit = ok(call('POST', '/api/erp/documents/purchase-debit-note', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-06', 'relatedDocumentId': bill['id'],
        'lines': [{'description': 'Freight', 'unitPrice': '10'}], 'status': 'ready'}), 201)
    assert shape(connect, debit['journalId']) == [('5000', Decimal('10.00'), 0), ('2000', 0, Decimal('10.00'))]
    credit = ok(call('POST', '/api/erp/documents/purchase-credit-note', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-07', 'relatedDocumentId': bill['id'],
        'lines': [{'description': 'Damaged', 'unitPrice': '20'}], 'status': 'ready'}), 201)
    assert credit['relatedDocumentId'] == bill['id']
    assert ok(call('GET', f"/api/erp/documents/bill/{bill['id']}", admin))['outstanding'] == '86.00'
    paid = ok(call('POST', '/api/erp/documents/purchase-payment', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-28', 'amount': '96.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': bill['id'], 'amount': '86.00'}, {'targetId': debit['id'], 'amount': '10.00'}], 'status': 'ready'}), 201)
    assert shape(connect, paid['journalId']) == [('2000', Decimal('96.00'), 0), ('1010', 0, Decimal('96.00'))]
    assert 'PAYMENT VOUCHER' in pdf_text(call('GET', f"/api/erp/documents/purchase-payment/{paid['id']}/files/1", admin).content)
    for doc_type, doc in (('bill', bill), ('purchase-debit-note', debit)):
        assert ok(call('GET', f"/api/erp/documents/{doc_type}/{doc['id']}", admin))['paymentStatus'] == 'paid'


def test_planm_tidy_excludes_own_sales_captures_reverses_them_and_names_purchases(company, monkeypatch):
    """B1 local clean-up: an own sales invoice captured and posted as a sale is reversed, leaves the Inbox and the
    benchmark; a purchase bill gets the M4 name; contacts get codes. Dry run writes nothing."""
    from services.admin import planm_tidy
    connect, ids = company
    admin = ids['admin']
    me = set_profile(connect, ids)
    own = _captured(connect, ids, seller=me, buyer='Kedai Runcit Maju', category='accounts_receivable', number='INV-2608-0228')
    bill = _captured(connect, ids, seller='Syarikat Bekalan Jaya', buyer=me, number='SBJ-9')
    with connect() as db:
        own_doc, bill_doc = (db.execute("SELECT document_id FROM standardised_records WHERE id=%s", (r,)).fetchone()['document_id']
                             for r in (own, bill))
        # As before Plan M: the own sales invoice was posted to the ledger as a sale.
        period = db.execute("SELECT id FROM accounting_periods WHERE tenant_id=%s AND period_code='2026-08'", (ids['tenant'],)).fetchone()['id']
        journal = db.execute("""INSERT INTO journal_headers(tenant_id,period_id,idempotency_key,source_type,source_id,journal_date,
                description,posted_by,status,posted_at) VALUES (%s,%s,%s,'document',%s,'2026-08-07','Old scanned sale',%s,'posted',now())
            RETURNING id""", (ids['tenant'], period, f'old:{own_doc}', own_doc, admin['id'])).fetchone()['id']
        for n, (code, debit, credit) in enumerate((('1100', 106, 0), ('4000', 0, 100), ('2030', 0, 6)), 1):
            db.execute("""INSERT INTO journal_lines(tenant_id,journal_id,account_id,line_number,description,debit,credit)
                VALUES (%s,%s,%s,%s,'x',%s,%s)""", (ids['tenant'], journal, ids['accounts'][code], n, debit, credit))

    with connect() as db:
        dry = planm_tidy.tidy(db, ids['tenant'], apply=False)
        db.rollback()
    assert dry['own_sales_documents'] == 1 and dry['reversed_journals'] == 1 and dry['renamed'] == 1
    with connect() as db:
        assert db.execute("SELECT excluded_at FROM documents WHERE id=%s", (own_doc,)).fetchone()['excluded_at'] is None

    with connect() as db:
        done = planm_tidy.tidy(db, ids['tenant'], apply=True)
    assert done['own_sales_documents'] == 1 and done['contact_codes'] >= 2
    with connect() as db:
        assert db.execute("SELECT status FROM journal_headers WHERE id=%s", (journal,)).fetchone()['status'] == 'reversed'
        assert db.execute("SELECT excluded_at FROM documents WHERE id=%s", (own_doc,)).fetchone()['excluded_at'] is not None
        assert db.execute("SELECT original_filename FROM documents WHERE id=%s", (bill_doc,)).fetchone()['original_filename'] \
            == '2026-08-07_SBJ_BILL_SBJ-9.pdf'
        assert db.execute("SELECT count(*) AS n FROM customers WHERE tenant_id=%s AND contact_code IS NULL", (ids['tenant'],)).fetchone()['n'] == 0
    from services.routes.documents import routes as document_routes
    monkeypatch.setattr(document_routes, 'connect', connect)
    listed = ok(call('GET', '/api/documents', admin))
    items = listed if isinstance(listed, list) else listed.get('items', [])
    assert [d['id'] for d in items] == [str(bill_doc)]
    assert items[0]['displayName'] == '2026-08-07_SBJ_BILL_SBJ-9.pdf' and items[0]['direction'] == 'purchase'
    # Running it again changes nothing.
    with connect() as db:
        again = planm_tidy.tidy(db, ids['tenant'], apply=True)
    assert again['own_sales_documents'] == 0 and again['renamed'] == 0 and again['contact_codes'] == 0


def test_codex_review_fixes_note_target_quotation_approval_and_printing(company):
    """Codex review of S1: a credit note settles only what it names; quotations have no approval step; the printed copy
    shows gross subtotal, discount and the typed unit-price precision; a voucher names the scanned bills it pays;
    direction also uses the reviewed TIN."""
    connect, ids = company
    admin = ids['admin']
    me = set_profile(connect, ids)
    a = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    b = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids, referenceNo='B'), 'status': 'ready'}), 201)
    crossed = ok(call('POST', '/api/erp/documents/credit-note', admin, json={
        'contactId': str(ids['customer']), 'documentDate': '2026-08-15', 'relatedDocumentId': a['id'],
        'lines': [{'description': 'x', 'unitPrice': '10'}], 'allocations': [{'targetId': b['id'], 'amount': '10.00'}]}), 201)
    refused = issue('credit-note', crossed, admin)
    assert refused.status_code == 422 and a['number'] in refused.json()['detail']

    assert call('POST', '/api/erp/documents/quotation', admin, json={**invoice_body(ids), 'status': 'pending_approval'}).status_code == 422

    priced = ok(call('POST', '/api/erp/documents/invoice', admin, json=invoice_body(ids, lines=[
        {'description': 'Screws', 'quantity': '1000', 'unitPrice': '0.0125', 'taxCode': 'SV-8', 'tax': '1.00'},
        {'description': 'Cable', 'quantity': '1', 'unitPrice': '100.00', 'discount': '10.00'}])), 201)
    text = pdf_text(call('GET', f"/api/erp/documents/invoice/{priced['id']}/preview.pdf", admin).content)
    assert '0.0125' in text and '112.50' in text and '10.00' in text and '103.50' in text  # gross 112.50 - 10 + 1 tax

    bill = _captured(connect, ids, seller='Syarikat Bekalan Jaya', buyer=me, number='SBJ-555', vendor=ids['supplier'])
    paid = ok(call('POST', '/api/erp/documents/purchase-payment', admin, json={
        'contactId': str(ids['supplier']), 'documentDate': '2026-08-28', 'amount': '106.00', 'bankAccountId': str(ids['bank']),
        'allocations': [{'targetId': str(bill), 'source': 'scanned', 'amount': '106.00'}], 'status': 'ready'}), 201)
    assert 'SBJ-555' in pdf_text(call('GET', f"/api/erp/documents/purchase-payment/{paid['id']}/files/1", admin).content)

    sneaky = _captured(connect, ids, seller='Different Trading Name', buyer='Kedai Runcit Maju', number='T-1',
                       snapshot={'reviewedFields': {'supplier_tin': 'C99999999990'}})
    refused = call('POST', f'/api/erp/scanned/{sneaky}/post', admin, json={})
    assert refused.status_code == 409 and 'your own sales document' in refused.json()['detail']
