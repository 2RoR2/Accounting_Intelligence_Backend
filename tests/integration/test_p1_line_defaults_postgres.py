"""P1 line defaults on the real schema: the tax-code catalog, a product's default tax-code label, a contact's payment
terms and the due date of a document converted from a quotation or purchase order.

Same disposable database and request harness as test_erp_documents_postgres.py. Skipped without TEST_DATABASE_URL.
"""
from datetime import date, timedelta

import pytest
from psycopg.types.json import Jsonb

from tests.integration.test_erp_documents_postgres import (  # noqa: F401  (fixtures)
    call, company, database, invoice_body, ok)

pytestmark = pytest.mark.runtime_defaults


def set_terms(connect, table, contact_id, days):
    with connect() as db:
        db.execute(f"UPDATE {table} SET fields=%s WHERE id=%s", (Jsonb({'paymentTermDays': days} if days is not None else {}), contact_id))


def convert(user, source_type, source, target):
    return ok(call('POST', f"/api/erp/documents/{source_type}/{source['id']}/transfer", user, json={'targetType': target}), 201)


def codes(user):
    return {entry['code'] for entry in ok(call('GET', '/api/erp/catalog/tax-code', user))}


def test_invoice_converted_from_a_quotation_is_due_on_the_customers_terms(company):
    connect, ids = company
    admin = ids['admin']
    set_terms(connect, 'customers', ids['customer'], 14)
    # The quotation's own date is its validity, not a payment due date: the invoice must not inherit it.
    quote = ok(call('POST', '/api/erp/documents/quotation', admin, json={**invoice_body(ids), 'dueDate': '2026-08-26', 'status': 'ready'}), 201)
    invoice = convert(admin, 'quotation', quote, 'invoice')
    assert invoice['documentDate'] == str(date.today())
    assert invoice['dueDate'] == str(date.today() + timedelta(days=14))
    assert ok(call('GET', f"/api/erp/documents/invoice/{invoice['id']}", admin))['dueDate'] == invoice['dueDate']


def test_invoice_converted_for_a_customer_without_terms_has_no_due_date(company):
    _, ids = company
    quote = ok(call('POST', '/api/erp/documents/quotation', ids['admin'],
                    json={**invoice_body(ids), 'dueDate': '2026-08-26', 'status': 'ready'}), 201)
    assert convert(ids['admin'], 'quotation', quote, 'invoice')['dueDate'] is None


def test_zero_day_terms_are_due_on_the_document_date(company):
    connect, ids = company
    set_terms(connect, 'customers', ids['customer'], 0)
    quote = ok(call('POST', '/api/erp/documents/quotation', ids['admin'], json={**invoice_body(ids), 'status': 'ready'}), 201)
    assert convert(ids['admin'], 'quotation', quote, 'invoice')['dueDate'] == str(date.today())


def test_bill_converted_from_a_purchase_order_uses_the_suppliers_terms(company):
    connect, ids = company
    set_terms(connect, 'vendors', ids['supplier'], 30)
    order = ok(call('POST', '/api/erp/documents/purchase-order', ids['admin'],
                    json={**invoice_body(ids, contactId=str(ids['supplier'])), 'status': 'ready'}), 201)
    assert convert(ids['admin'], 'purchase-order', order, 'bill')['dueDate'] == str(date.today() + timedelta(days=30))



def test_bill_converted_for_a_supplier_imported_from_csv_uses_the_imported_terms(company):
    """Codex P2: the contacts CSV import keeps a supplier's terms in its supplier contact notes, not in fields."""
    import json

    connect, ids = company
    with connect() as db:
        db.execute("""INSERT INTO supplier_contacts(tenant_id,vendor_id,contact_name,role,notes)
            VALUES (%s,%s,'Syarikat Bekalan Jaya','accounts',%s)""",
                   (ids['tenant'], ids['supplier'], json.dumps({'source': 'contacts-import', 'payment_terms_days': 30})))
    order = ok(call('POST', '/api/erp/documents/purchase-order', ids['admin'],
                    json={**invoice_body(ids, contactId=str(ids['supplier'])), 'status': 'ready'}), 201)
    assert convert(ids['admin'], 'purchase-order', order, 'bill')['dueDate'] == str(date.today() + timedelta(days=30))
    # Terms set on the contact page win over the imported ones.
    set_terms(connect, 'vendors', ids['supplier'], 7)
    order = ok(call('POST', '/api/erp/documents/purchase-order', ids['admin'],
                    json={**invoice_body(ids, contactId=str(ids['supplier'])), 'status': 'ready'}), 201)
    assert convert(ids['admin'], 'purchase-order', order, 'bill')['dueDate'] == str(date.today() + timedelta(days=7))

def test_a_debit_note_still_follows_the_due_date_of_its_invoice(company):
    connect, ids = company
    admin = ids['admin']
    set_terms(connect, 'customers', ids['customer'], 14)
    invoice = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    debit = convert(admin, 'invoice', invoice, 'debit-note')
    assert debit['dueDate'] == '2026-08-26'  # carried from the invoice, not recomputed from today


def test_contact_payment_terms_are_stored_and_validated(company):
    _, ids = company
    admin = ids['admin']
    created = ok(call('POST', '/api/erp/contacts', admin, json={'kind': 'customer', 'legalName': 'Kedai Baru',
                                                                'fields': {'paymentTermDays': 21}}), 201)
    assert created['fields']['paymentTermDays'] == 21
    assert ok(call('GET', f"/api/erp/contacts/{created['id']}", admin))['fields']['paymentTermDays'] == 21
    for bad in (-1, 366, '14', 1.5):
        refused = call('POST', '/api/erp/contacts', admin, json={'kind': 'customer', 'legalName': f'Kedai {bad}',
                                                                 'fields': {'paymentTermDays': bad}})
        assert refused.status_code == 422, bad
    refused = call('PUT', f"/api/erp/contacts/{created['id']}", admin,
                   json={'kind': 'customer', 'legalName': 'Kedai Baru', 'fields': {'paymentTermDays': 999}})
    assert refused.status_code == 422


def test_tax_code_catalog_is_an_editable_list_of_labels(company):
    _, ids = company
    admin, accountant = ids['admin'], ids['accountant']
    sv8 = ok(call('POST', '/api/erp/catalog/tax-code', admin, json={'code': 'SV-8', 'name': 'Service tax 8%'}), 201)
    assert sv8['kind'] == 'tax-code' and sv8['code'] == 'SV-8' and sv8['isActive']
    assert call('POST', '/api/erp/catalog/tax-code', admin, json={'code': 'sv-8', 'name': 'Same label again'}).status_code == 409
    assert call('POST', '/api/erp/catalog/tax-code', admin, json={'name': 'No label'}).status_code == 422
    assert call('POST', '/api/erp/catalog/tax-code', admin, json={'code': 'X' * 41, 'name': 'Too long'}).status_code == 422
    assert call('POST', '/api/erp/catalog/tax-code', accountant, json={'code': 'NIL', 'name': 'Nil'}).status_code == 403
    sst = ok(call('POST', '/api/erp/catalog/tax-code', admin, json={'code': 'SST-10', 'name': 'Sales tax 10%'}), 201)
    renamed = ok(call('PUT', f"/api/erp/catalog/tax-code/{sst['id']}", admin, json={'code': 'SST-10', 'name': 'Sales tax (10%)'}))
    assert renamed['name'] == 'Sales tax (10%)'
    assert call('PUT', f"/api/erp/catalog/tax-code/{sst['id']}", admin, json={'code': 'SV-8', 'name': 'x'}).status_code == 409
    assert codes(accountant) == {'SST-10', 'SV-8'}
    ok(call('DELETE', f"/api/erp/catalog/tax-code/{sv8['id']}", admin))
    assert codes(accountant) == {'SST-10'}
    # A label is not a calculation: the tax on the line is whatever was typed.
    line = {'description': 'Service', 'quantity': '1', 'unitPrice': '100', 'taxCode': 'SV-8', 'tax': '1.00'}
    invoice = ok(call('POST', '/api/erp/documents/invoice', admin, json=invoice_body(ids, lines=[line])), 201)
    assert invoice['taxAmount'] == '1.00' and invoice['lines'][0]['taxCode'] == 'SV-8'


def test_product_keeps_default_accounts_and_tax_code_label(company):
    _, ids = company
    admin = ids['admin']
    body = {'name': 'Simen 50kg', 'sku': 'SIM-50', 'kind': 'inventory', 'unit': 'bag', 'salePrice': '25.50', 'purchasePrice': '20',
            'incomeAccountId': str(ids['accounts']['4000']), 'expenseAccountId': str(ids['accounts']['5000']),
            'defaultTaxCode': ' SST-10 '}
    created = ok(call('POST', '/api/erp/products', admin, json=body), 201)
    assert created['defaultTaxCode'] == 'SST-10'
    assert created['incomeAccountId'] == str(ids['accounts']['4000']) and created['expenseAccountId'] == str(ids['accounts']['5000'])
    assert ok(call('GET', f"/api/erp/products/{created['id']}", admin))['defaultTaxCode'] == 'SST-10'
    assert [p['defaultTaxCode'] for p in ok(call('GET', '/api/erp/products', admin))] == ['SST-10']
    cleared = ok(call('PUT', f"/api/erp/products/{created['id']}", admin, json={**body, 'defaultTaxCode': '  '}))
    assert cleared['defaultTaxCode'] is None
    plain = ok(call('POST', '/api/erp/products', admin, json={'name': 'Delivery', 'kind': 'service'}), 201)
    assert plain['defaultTaxCode'] is None
    assert call('POST', '/api/erp/products', admin, json={'name': 'Too long', 'defaultTaxCode': 'X' * 41}).status_code == 422


def pdf_text(user, document_type, document):
    import io
    from pypdf import PdfReader
    response = call('GET', f"/api/erp/documents/{document_type}/{document['id']}/preview.pdf", user)
    assert response.status_code == 200, response.text
    return ' '.join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages)


def test_only_open_items_print_a_due_date(company):
    _, ids = company
    admin = ids['admin']
    invoice = ok(call('POST', '/api/erp/documents/invoice', admin, json={**invoice_body(ids), 'status': 'ready'}), 201)
    credit = convert(admin, 'invoice', invoice, 'credit-note')
    assert credit['dueDate'] == '2026-08-26'  # copied with the rest of the invoice; it just must not print
    assert 'Due date' in pdf_text(admin, 'invoice', invoice)
    assert 'Due date' not in pdf_text(admin, 'credit-note', credit)
