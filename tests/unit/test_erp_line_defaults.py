"""P1 line defaults without a database: payment terms on a contact, the due date of a converted document and the
tax-code catalog rules. The real-schema checks are in tests/integration/test_p1_line_defaults_postgres.py."""
from datetime import date

import pytest
from fastapi import HTTPException

import main  # noqa: F401  (the route modules import from main)

from services.routes.erp import catalog, contacts, documents


class FakeDb:
    """Answers the one lookup _converted_due_date makes, by table name."""

    def __init__(self, customers=None, vendors=None):
        self.rows = {'customers': customers, 'vendors': vendors}
        self.queries = []

    def execute(self, sql, params):
        self.queries.append(sql)
        table = 'customers' if 'FROM customers' in sql else 'vendors'
        row = self.rows[table]

        class Result:
            def fetchone(self_inner):
                return row
        return Result()


@pytest.mark.parametrize('fields,expected', [
    ({'paymentTermDays': 14}, 14), ({'paymentTermDays': 0}, 0), ({'paymentTermDays': 365}, 365),
    ({'paymentTermDays': 366}, None), ({'paymentTermDays': -1}, None), ({'paymentTermDays': '14'}, None),
    ({'paymentTermDays': True}, None), ({'paymentTermDays': 14.5}, None), ({}, None), (None, None)])
def test_payment_term_days_reads_only_a_whole_number_of_days(fields, expected):
    assert contacts.payment_term_days({'fields': fields}) == expected
    assert contacts.payment_term_days(None) is None


def test_a_contact_cannot_be_saved_with_invalid_payment_terms():
    contacts._values(contacts.ContactInput(legalName='A', fields={'paymentTermDays': 30}))
    contacts._values(contacts.ContactInput(legalName='A', fields={}))
    for bad in (-1, 400, '30', 1.5, True):
        with pytest.raises(HTTPException) as error:
            contacts._values(contacts.ContactInput(legalName='A', fields={'paymentTermDays': bad}))
        assert error.value.status_code == 422


def test_due_date_is_the_document_date_plus_terms():
    assert documents.due_date_from_terms(date(2026, 11, 24), 14) == date(2026, 12, 8)
    assert documents.due_date_from_terms(date(2026, 11, 24), 0) == date(2026, 11, 24)
    assert documents.due_date_from_terms(date(2026, 12, 25), 30) == date(2027, 1, 24)
    assert documents.due_date_from_terms(date(2026, 11, 24), None) is None


def source(document_type='quotation', **extra):
    return {'document_type': document_type, 'customer_id': 'c1', 'vendor_id': None, 'due_date': None, **extra}


def test_invoice_from_a_quotation_is_due_on_the_customers_terms():
    db = FakeDb(customers={'fields': {'paymentTermDays': 14}})
    assert documents._converted_due_date(db, 't', source(), 'invoice', date(2026, 11, 24)) == date(2026, 12, 8)


def test_a_quotations_validity_date_is_not_the_invoices_due_date():
    db = FakeDb(customers={'fields': {'paymentTermDays': 30}})
    quote = source(due_date=date(2026, 11, 30))  # "valid until"
    assert documents._converted_due_date(db, 't', quote, 'invoice', date(2026, 11, 24)) == date(2026, 12, 24)
    no_terms = FakeDb(customers={'fields': {}})
    assert documents._converted_due_date(no_terms, 't', quote, 'invoice', date(2026, 11, 24)) is None


def test_a_bill_from_a_purchase_order_uses_the_suppliers_terms():
    db = FakeDb(vendors={'fields': {'paymentTermDays': 60}})
    order = source('purchase-order', customer_id=None, vendor_id='v1')
    assert documents._converted_due_date(db, 't', order, 'bill', date(2026, 11, 24)) == date(2027, 1, 23)


def test_other_transfers_keep_the_source_due_date():
    db = FakeDb(customers={'fields': {'paymentTermDays': 14}})
    invoice = source('invoice', due_date=date(2026, 12, 1))
    assert documents._converted_due_date(db, 't', invoice, 'debit-note', date(2026, 11, 24)) == date(2026, 12, 1)
    assert documents._converted_due_date(db, 't', invoice, 'payment', date(2026, 11, 24)) == date(2026, 12, 1)
    assert documents._converted_due_date(db, 't', source(), 'delivery-order', date(2026, 11, 24)) is None
    assert db.queries == []  # no contact lookup unless an open item is made from a non-open source


class Data:
    def __init__(self, code=None, active=True, values=None, ledger=None):
        self.code, self.active, self.data, self.ledgerAccountId = code, active, values or {}, ledger


def test_a_tax_code_needs_a_short_label_in_the_code_field():
    assert catalog._validate_catalog_values('tax-code', Data(code=' SV-8 ')) == 'SV-8'
    for bad in (None, '', '   ', 'X' * 41):
        with pytest.raises(HTTPException) as error:
            catalog._validate_catalog_values('tax-code', Data(code=bad))
        assert error.value.status_code == 422
    assert 'tax-code' in catalog.VISIBLE_CATALOG_KINDS and 'tax-code' in catalog.CATALOG_KINDS


def test_a_tax_code_amount_is_never_calculated_from_the_code():
    """The catalog entry is a label: no rate is stored, and the line's tax stays what was typed."""
    entry = catalog._catalog_dto('tax-code', {'id': 'i', 'code': 'SV-8', 'name': 'Service tax 8%', 'description': None,
                                              'data': {}, 'is_active': True, 'created_at': 'now', 'updated_at': 'now'})
    assert entry['code'] == 'SV-8' and 'rate' not in entry['data']
    line = documents.LineInput(description='x', unitPrice='100', taxCode='SV-8', tax='1.00')
    assert documents._clean_lines([line])[0]['tax'] == '1.00'
