"""
Module 3 - Validation Engine
Dummy JSON mockups + tests confirming the Invoice/LineItem models accept
valid data and reject invalid data (FCG1-17).

Run with: python -m unittest discover -s tests -p test_validation.py -v
Requires: pip install pydantic
"""

from decimal import Decimal
import unittest

from pydantic import ValidationError

from validation.models import Invoice

# ---------------------------------------------------------------------------
# 1. VALID mockup - should be accepted
# ---------------------------------------------------------------------------
VALID_INVOICE = {
    "invoice_number": "INV-2026-0091",
    "vendor_name": "Sinar Trading Sdn Bhd",
    "invoice_date": "2026-09-01",
    "due_date": "2026-09-30",
    "line_items": [
        {"description": "A4 Paper (Ream)", "quantity": 10, "unit_price": "12.50", "amount": "125.00"},
        {"description": "Printer Toner", "quantity": 2, "unit_price": "180.00", "amount": "360.00"},
    ],
    "subtotal": "485.00",
    "tax_amount": "29.10",
    "total_amount": "514.10",
    "currency": "MYR",
}

# ---------------------------------------------------------------------------
# 2. INVALID mockups - each breaks exactly ONE rule, so failures are easy
#    to trace back to the validator that caught them.
# ---------------------------------------------------------------------------
INVALID_MATH_MISMATCH = {**VALID_INVOICE, "total_amount": "999.99"}
INVALID_SUBTOTAL_MISMATCH = {**VALID_INVOICE, "subtotal": "100.00"}
INVALID_DATE_FORMAT = {**VALID_INVOICE, "invoice_date": "01/09/2026"}
INVALID_TOTAL_TYPE = {**VALID_INVOICE, "total_amount": "not-a-number"}
INVALID_DUE_BEFORE_INVOICE = {**VALID_INVOICE, "due_date": "2026-08-01"}
INVALID_LINE_ITEM_MATH = {
    **VALID_INVOICE,
    "line_items": [
        {"description": "A4 Paper (Ream)", "quantity": 10, "unit_price": "12.50", "amount": "999.00"},
    ],
}
INVALID_MISSING_FIELD = {k: v for k, v in VALID_INVOICE.items() if k != "vendor_name"}
INVALID_EMPTY_LINE_ITEMS = {**VALID_INVOICE, "line_items": []}


class InvoiceValidationTests(unittest.TestCase):
    def test_valid_invoice_is_accepted(self):
        invoice = Invoice(**VALID_INVOICE)
        self.assertEqual(invoice.invoice_number, "INV-2026-0091")
        self.assertEqual(invoice.total_amount, invoice.subtotal + invoice.tax_amount)

    def test_comma_formatted_amounts_from_extraction_engine_are_accepted(self):
        # Mirrors the real format Jonathan's Path A extraction outputs
        payload = {
            **VALID_INVOICE,
            "subtotal": "2,193,713.00",
            "tax_amount": "219,371.30",
            "total_amount": "2,413,084.30",
            "line_items": [
                {"description": "Bulk order", "quantity": 1, "unit_price": "2,193,713.00", "amount": "2,193,713.00"},
            ],
        }
        invoice = Invoice(**payload)
        self.assertEqual(invoice.subtotal, Decimal("2193713.00"))

    def test_invalid_invoice_is_rejected(self):
        cases = [
            (INVALID_MATH_MISMATCH, "total != subtotal + tax"),
            (INVALID_SUBTOTAL_MISMATCH, "subtotal != sum(line_items)"),
            (INVALID_DATE_FORMAT, "invoice_date not ISO format"),
            (INVALID_TOTAL_TYPE, "total_amount not numeric"),
            (INVALID_DUE_BEFORE_INVOICE, "due_date before invoice_date"),
            (INVALID_LINE_ITEM_MATH, "line item amount != quantity * unit_price"),
            (INVALID_MISSING_FIELD, "required field missing"),
            (INVALID_EMPTY_LINE_ITEMS, "no line items"),
        ]
        for bad_payload, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(ValidationError):
                    Invoice(**bad_payload)
