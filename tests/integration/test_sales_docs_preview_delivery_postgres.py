"""Sales documents, two-week items (docs/SALES_DOCS_AND_DO_PLAN.md §5): the unsaved live preview, the fields the PDF
now prints (buyer SST no., quotation validity, attention, delivery address), sales orders as printed documents, the
delivery order's "delivered" record, and the plain-words posting preview behind the Issue confirm step.

Same disposable database and request harness as test_erp_documents_postgres.py. Skipped without TEST_DATABASE_URL.
"""
from io import BytesIO

import pytest
from psycopg.types.json import Jsonb
from pypdf import PdfReader

from tests.integration.test_erp_documents_postgres import (  # noqa: F401  (fixtures)
    call, company, database, invoice_body, journal_count, ok)

pytestmark = pytest.mark.runtime_defaults


def pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)


def counts(connect, ids):
    with connect() as db:
        headers = db.execute("SELECT count(*) AS n FROM erp_document_headers WHERE tenant_id=%s", (ids['tenant'],)).fetchone()['n']
        counters = db.execute("SELECT count(*) AS n FROM erp_number_counters WHERE tenant_id=%s", (ids['tenant'],)).fetchone()['n']
        files = db.execute("SELECT count(*) AS n FROM erp_document_files WHERE tenant_id=%s", (ids['tenant'],)).fetchone()['n']
    return headers, counters, files


def with_sst(connect, ids):
    with connect() as db:
        db.execute("UPDATE customers SET fields=%s WHERE id=%s", (Jsonb({'sstNumber': 'W10-2611-99910009'}), ids['customer']))


EXTRA_FIELDS = {'attention': 'Lim Siew Ling (Purchasing)', 'deliveryAddress': 'Jetty 3, Jalan Pending Contoh, 93450 Kuching',
                'terms': 'Prices valid for 30 days.'}


def test_unsaved_preview_prints_the_form_and_stores_nothing(company):
    connect, ids = company
    with_sst(connect, ids)
    before = counts(connect, ids)
    body = invoice_body(ids, dueDate='2026-09-11', referenceNo='BMS-PO-2611-22', fields=EXTRA_FIELDS)
    response = call('POST', '/api/erp/documents/quotation/preview.pdf', ids['accountant'], json=body)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'application/pdf' and 'no-store' in response.headers['cache-control']
    text = pdf_text(response.content)
    for needle in ('PREVIEW - NOT SAVED', 'QT-2026-(next)', 'QUOTATION', 'Kedai Runcit Maju', 'SST No: W10-2611-99910009',
                   'Valid until', '11/09/2026', 'Attn: Lim Siew Ling (Purchasing)', 'Deliver to', 'Jetty 3, Jalan Pending Contoh',
                   'Prices valid for 30 days.', 'BMS-PO-2611-22', '315.50'):
        assert needle in text, needle
    assert counts(connect, ids) == before  # no header, no number counter, no stored copy

    # Not every type prints, and a contact of the wrong kind is refused as it is on save.
    assert call('POST', '/api/erp/documents/bill/preview.pdf', ids['accountant'],
                json={**body, 'contactId': str(ids['supplier'])}).status_code == 422
    assert call('POST', '/api/erp/documents/quotation/preview.pdf', ids['accountant'],
                json={**body, 'contactId': str(ids['supplier'])}).status_code == 422


def test_preview_of_a_saved_draft_keeps_its_number_and_origin(company):
    connect, ids = company
    admin = ids['admin']
    quote = ok(call('POST', '/api/erp/documents/quotation', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    order = ok(call('POST', f"/api/erp/documents/quotation/{quote['id']}/transfer", admin, json={'targetType': 'sales-order'}), 201)
    response = call('POST', f"/api/erp/documents/sales-order/preview.pdf?documentId={order['id']}", admin,
                    json={**invoice_body(ids), 'fields': {'attention': 'Encik Rosli'}})
    assert response.status_code == 200, response.text
    text = pdf_text(response.content)
    assert order['number'] in text and 'SALES ORDER' in text
    assert 'Original quotation' in text and quote['number'] in text and 'Attn: Encik Rosli' in text


def test_sales_order_issues_as_a_printed_record_and_flows_to_delivery_and_invoice(company):
    connect, ids = company
    admin, accountant = ids['admin'], ids['accountant']
    quote = ok(call('POST', '/api/erp/documents/quotation', admin,
                    json={**invoice_body(ids, fields=EXTRA_FIELDS), 'status': 'ready'}), 201)
    order = ok(call('POST', f"/api/erp/documents/quotation/{quote['id']}/transfer", admin, json={'targetType': 'sales-order'}), 201)
    assert order['printed'] and order['fields']['deliveryAddress'] == EXTRA_FIELDS['deliveryAddress']
    order = ok(call('PATCH', f"/api/erp/documents/sales-order/{order['id']}", admin, json={'documentDate': '2026-08-12'}))
    order = ok(call('POST', f"/api/erp/documents/sales-order/{order['id']}/transition", admin, json={'status': 'ready'}))
    assert order['status'] == 'ready' and not order['posted'] and [f['revision'] for f in order['files']] == [1]
    assert journal_count(connect, ids) == 0

    delivery = ok(call('POST', f"/api/erp/documents/sales-order/{order['id']}/transfer", admin,
                       json={'targetType': 'delivery-order'}), 201)
    delivery = ok(call('PATCH', f"/api/erp/documents/delivery-order/{delivery['id']}", admin, json={'documentDate': '2026-08-13'}))
    path = f"/api/erp/documents/delivery-order/{delivery['id']}/delivered"
    # Not issued yet: it cannot be marked delivered.
    assert call('POST', path, accountant, json={'receivedBy': 'Lim Siew Ling', 'receivedOn': '2026-08-13'}).status_code == 409
    delivery = ok(call('POST', f"/api/erp/documents/delivery-order/{delivery['id']}/transition", admin, json={'status': 'ready'}))
    stored = call('GET', f"/api/erp/documents/delivery-order/{delivery['id']}/files/1", admin)
    text = pdf_text(stored.content)
    assert 'DELIVERY ORDER' in text and 'Goods received in good order and condition' in text and 'Company chop' in text
    assert 'Jetty 3, Jalan Pending Contoh' in text and 'Bill to' in text and 'No signature is required' not in text
    assert '315.50' not in text  # no prices on a delivery order

    assert call('POST', path, accountant, json={'receivedBy': 'Lim Siew Ling', 'receivedOn': '2026-08-01'}).status_code == 422
    assert call('POST', path, accountant, json={'receivedBy': '  ', 'receivedOn': '2026-08-14'}).status_code == 422
    done = ok(call('POST', path, accountant, json={'receivedBy': 'Lim Siew Ling', 'receivedOn': '2026-08-14',
                                                   'note': 'All bags dry'}))
    delivered = done['fields']['delivered']
    assert done['status'] == 'ready' and delivered['receivedBy'] == 'Lim Siew Ling'
    assert delivered['receivedOn'] == '2026-08-14' and delivered['note'] == 'All bags dry'
    assert call('POST', path, accountant, json={'receivedBy': 'Someone', 'receivedOn': '2026-08-15'}).status_code == 409
    history = ok(call('GET', f"/api/erp/documents/delivery-order/{delivery['id']}/history", admin))
    assert any(e['eventType'] == 'delivered' and 'Lim Siew Ling' in (e['reason'] or '') for e in history['events'])
    assert call('POST', path, ids['outsider'], json={'receivedBy': 'X', 'receivedOn': '2026-08-14'}).status_code == 404
    assert journal_count(connect, ids) == 0  # neither the order nor the delivery posts

    invoice = ok(call('POST', f"/api/erp/documents/delivery-order/{delivery['id']}/transfer", admin,
                      json={'targetType': 'invoice'}), 201)
    assert invoice['amount'] == '315.50' and invoice['fields']['attention'] == EXTRA_FIELDS['attention']
    assert 'delivered' not in invoice['fields']  # the delivery record stays on the delivery order


def test_posting_preview_matches_what_issuing_posts(company):
    connect, ids = company
    admin = ids['admin']
    draft = ok(call('POST', '/api/erp/documents/invoice', ids['accountant'], json=invoice_body(ids)), 201)
    preview = ok(call('GET', f"/api/erp/documents/invoice/{draft['id']}/posting-preview", ids['accountant']))
    assert preview['posts'] and not preview['posted'] and preview['balanced'] and preview['problem'] is None
    assert preview['totalDebit'] == preview['totalCredit'] == '315.50'
    planned = sorted((line['code'], line['debit'], line['credit']) for line in preview['lines'])
    issued = ok(call('POST', f"/api/erp/documents/invoice/{draft['id']}/transition", admin, json={'status': 'ready'}))
    after = ok(call('GET', f"/api/erp/documents/invoice/{issued['id']}/posting-preview", admin))
    assert after['posted'] and planned == sorted((l['code'], l['debit'], l['credit']) for l in after['lines'])

    quote = ok(call('POST', '/api/erp/documents/quotation', admin, json=invoice_body(ids)), 201)
    assert ok(call('GET', f"/api/erp/documents/quotation/{quote['id']}/posting-preview", admin))['posts'] is False
