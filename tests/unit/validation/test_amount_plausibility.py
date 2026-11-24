"""Printed amounts compared with each other (24 Nov demo, bug d): tax 206.00 read with a total of 20.60."""
from services.extraction.persistence.persistence import _usable_document_identifier
from services import naming
from validation.documents.profiles import AMOUNT_PLAUSIBLE, validate_profile


def findings(kind, **values):
    base = {"supplier": "Rengas Stationers", "date": "2026-11-12", "currency": "MYR"}
    return [(e["code"], e["field"]) for e in validate_profile(kind, {**base, **values}, [])["errors"]
            if e["code"] == AMOUNT_PLAUSIBLE]


def test_tax_larger_than_total_flags_both_fields():
    assert findings("receipt", tax="206.00", total="20.60") == [(AMOUNT_PLAUSIBLE, "tax"), (AMOUNT_PLAUSIBLE, "total")]


def test_normal_amounts_pass():
    assert findings("receipt", subtotal="18.69", tax="1.91", total="20.60") == []
    assert findings("invoice", subtotal="420.00", tax="42.00", total="462.00") == []


def test_subtotal_above_total_only_without_a_reduction():
    assert findings("bill", subtotal="500.00", tax="0", total="50.00") == [
        (AMOUNT_PLAUSIBLE, "subtotal"), (AMOUNT_PLAUSIBLE, "total")]
    assert findings("bill", subtotal="500.00", discount="460.00", total="40.00") == []


def test_missing_or_unreadable_amounts_are_not_flagged():
    assert findings("receipt", total="20.60") == []
    assert findings("receipt", tax="abc", total="20.60") == []


def test_bill_number_names_the_stored_file():
    fields = [{"field_key": "invoice", "current_value": None},
              {"field_key": "bill_number", "current_value": "INV-88123"}]
    identifier = _usable_document_identifier(fields, "bill")
    assert identifier == "INV-88123"
    assert naming.display_name("2026-11-03", "PKH", "BILL", identifier, ".pdf") == "2026-11-03_PKH_BILL_INV-88123.pdf"


def test_note_number_wins_over_the_invoice_it_corrects():
    fields = [{"field_key": "invoice", "current_value": "PI-2610-01"},
              {"field_key": "reference", "current_value": "CN-2610-01"}]
    assert _usable_document_identifier(fields, "credit_note") == "CN-2610-01"
