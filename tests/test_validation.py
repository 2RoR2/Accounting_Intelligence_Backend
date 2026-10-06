"""
Module 3 - Validation Engine
Dummy JSON mockups + tests confirming the Invoice/LineItem models accept
valid data and reject invalid data 

"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from validation.models import Invoice
from validation.duplicate import find_duplicate_invoices
from validation.vendor_matching import find_vendor_match
from validation.engine import validate_invoice_document

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

INVALID_EMPTY_INVOICE_NUMBER = {**VALID_INVOICE, "invoice_number": ""}
INVALID_EMPTY_VENDOR_NAME = {**VALID_INVOICE, "vendor_name": ""}
INVALID_EMPTY_DESCRIPTION = {
    **VALID_INVOICE,
    "line_items": [
        {
            "description": "",
            "quantity": 10,
            "unit_price": "12.50",
            "amount": "125.00",
        },
    ],
}


def test_valid_invoice_is_accepted():
    invoice = Invoice(**VALID_INVOICE)
    assert invoice.invoice_number == "INV-2026-0091"
    assert invoice.total_amount == invoice.subtotal + invoice.tax_amount


def test_comma_formatted_amounts_from_extraction_engine_are_accepted():
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
    assert invoice.subtotal == Decimal("2193713.00")


@pytest.mark.parametrize(
    "bad_payload,reason",
    [
        (INVALID_MATH_MISMATCH, "total != subtotal + tax"),
        (INVALID_SUBTOTAL_MISMATCH, "subtotal != sum(line_items)"),
        (INVALID_DATE_FORMAT, "invoice_date not ISO format"),
        (INVALID_TOTAL_TYPE, "total_amount not numeric"),
        (INVALID_DUE_BEFORE_INVOICE, "due_date before invoice_date"),
        (INVALID_LINE_ITEM_MATH, "line item amount != quantity * unit_price"),
        (INVALID_MISSING_FIELD, "required field missing"),
        (INVALID_EMPTY_LINE_ITEMS, "no line items"),
        (INVALID_EMPTY_INVOICE_NUMBER, "empty invoice number"),
        (INVALID_EMPTY_VENDOR_NAME, "empty vendor name"),
        (INVALID_EMPTY_DESCRIPTION, "empty line item description"),
    ],
)
def test_invalid_invoice_is_rejected(bad_payload, reason):
    with pytest.raises(ValidationError):
        Invoice(**bad_payload)


def test_duplicate_invoice_is_detected():
    invoices = [
        {
            "vendor_name": "TechVision Distributors Pvt Ltd",
            "invoice_number": "51109397",
            "source_file": "invoice_51109386.pdf",
        },
        {
            "vendor_name": "TechVision Distributors Pvt Ltd",
            "invoice_number": "51109397",
            "source_file": "invoice_51109368.pdf",
        },
    ]

    duplicates = find_duplicate_invoices(invoices)

    assert len(duplicates) == 1
    assert duplicates[0]["code"] == "EX-007"
    assert duplicates[0]["invoice_number"] == "51109397"
    assert duplicates[0]["duplicate_of"] == "invoice_51109386.pdf"

def test_vendor_exact_match_is_accepted():
    vendors = [
        {
            "id": "vendor-001",
            "vendor_name": "TechVision Distributors Pvt Ltd",
        },
        {
            "id": "vendor-002",
            "vendor_name": "Sinar Trading Sdn Bhd",
        },
    ]

    match = find_vendor_match(
        "TECHVISION DISTRIBUTORS PVT. LTD.",
        vendors,
    )

    assert match["status"] == "ACCEPT"
    assert match["vendor_id"] == "vendor-001"
    assert match["confidence"] == 1.0


def test_vendor_fuzzy_match_requires_review():
    vendors = [
        {
            "id": "vendor-001",
            "vendor_name": "TechVision Distributors Pvt Ltd",
        },
    ]

    match = find_vendor_match(
        "TechVision Distributors Pvt",
        vendors,
    )

    assert match["status"] == "REVIEW_REQUIRED"
    assert match["vendor_id"] == "vendor-001"
    assert match["confidence"] >= 0.80
    assert match["confidence"] < 1.0


def test_unknown_vendor_returns_no_match():
    vendors = [
        {
            "id": "vendor-001",
            "vendor_name": "TechVision Distributors Pvt Ltd",
        },
    ]

    match = find_vendor_match("ABC Unknown Company", vendors)

    assert match["status"] == "NO_MATCH"
    assert match["vendor_id"] is None

def test_validation_engine_accepts_valid_invoice():
    result = validate_invoice_document(
        VALID_INVOICE,
        known_vendors=[
            {"id": "vendor-001", "vendor_name": "Sinar Trading Sdn Bhd"},
        ],
    )

    assert result["status"] == "VALID"
    assert result["errors"] == []
    assert result["vendor_match"]["status"] == "ACCEPT"


def test_validation_engine_detects_duplicate_invoice():
    invoice = {
        **VALID_INVOICE,
        "vendor_name": "TechVision Distributors Pvt Ltd",
        "invoice_number": "51109397",
        "source_file": "invoice_51109368.pdf",
    }

    existing_invoices = [
        {
            "vendor_name": "TechVision Distributors Pvt Ltd",
            "invoice_number": "51109397",
            "source_file": "invoice_51109386.pdf",
        }
    ]

    result = validate_invoice_document(
        invoice,
        known_vendors=[
            {"id": "vendor-001", "vendor_name": "TechVision Distributors Pvt Ltd"},
        ],
        existing_invoices=existing_invoices,
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert any(error["code"] == "EX-007" for error in result["errors"])


def test_validation_engine_requires_review_for_fuzzy_vendor():
    invoice = {
        **VALID_INVOICE,
        "vendor_name": "TechVision Distributors Pvt",
    }

    result = validate_invoice_document(
        invoice,
        known_vendors=[
            {"id": "vendor-001", "vendor_name": "TechVision Distributors Pvt Ltd"},
        ],
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["vendor_match"]["status"] == "REVIEW_REQUIRED"
    assert any(error["code"] == "EX-008" for error in result["errors"])


def test_validation_engine_reports_unknown_vendor():
    result = validate_invoice_document(
        VALID_INVOICE,
        known_vendors=[
            {"id": "vendor-001", "vendor_name": "ABC Company"},
        ],
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["vendor_match"]["status"] == "NO_MATCH"

def test_validation_engine_assigns_duplicate_exception_severity():
    invoice = {
        **VALID_INVOICE,
        "vendor_name": "TechVision Distributors Pvt Ltd",
        "invoice_number": "51109397",
        "source_file": "invoice_51109368.pdf",
    }

    existing_invoices = [
        {
            "vendor_name": "TechVision Distributors Pvt Ltd",
            "invoice_number": "51109397",
            "source_file": "invoice_51109386.pdf",
        }
    ]

    result = validate_invoice_document(
        invoice,
        known_vendors=[
            {
                "id": "vendor-001",
                "vendor_name": "TechVision Distributors Pvt Ltd",
            }
        ],
        existing_invoices=existing_invoices,
    )

    duplicate_error = next(
        error for error in result["errors"]
        if error["code"] == "EX-007"
    )

    assert duplicate_error["severity"] == "high"

def test_validation_engine_assigns_po_exception():
    invoice = {
        "invoice_number": "INV-PO-001",
        "vendor_name": "ABC Supplies",
        "invoice_date": "2026-01-15",
        "due_date": "2026-02-15",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 10,
                "unit_price": "6.00",
                "amount": "60.00",
            }
        ],
        "subtotal": "60.00",
        "tax_amount": "0.00",
        "total_amount": "60.00",
        "currency": "MYR",
    }

    purchase_order = {
        "po_number": "PO-001",
        "vendor_name": "ABC Supplies",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 10,
                "unit_price": "5.00",
            }
        ],
        "total_amount": "50.00",
    }

    result = validate_invoice_document(
        invoice,
        known_vendors=[
            {"vendor_name": "ABC Supplies"}
        ],
        purchase_order=purchase_order,
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["po_match"]["status"] == "REVIEW_REQUIRED"

    po_errors = [
        error
        for error in result["errors"]
        if error["code"] == "EX-009"
    ]

    assert len(po_errors) == 1
    assert po_errors[0]["severity"] == "high"