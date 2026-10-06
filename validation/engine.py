from typing import Any, Dict, List, Optional

from validation.models import validate_invoice
from validation.duplicate import find_duplicate_invoices
from validation.vendor_matching import find_vendor_match


EXCEPTION_SEVERITY = {
    "EX-001": "medium",
    "EX-002": "medium",
    "EX-003": "high",
    "EX-004": "high",
    "EX-005": "high",
    "EX-006": "medium",
    "EX-007": "high",
    "EX-008": "medium",
}


def validate_invoice_document(
    invoice_data: Dict[str, Any],
    known_vendors: Optional[List[Dict[str, Any]]] = None,
    existing_invoices: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """
    Run the complete validation flow for one invoice.

    The engine combines:
    1. Invoice schema and accounting validation
    2. Duplicate invoice detection
    3. Vendor matching

    Returns one consolidated validation result.
    """

    known_vendors = known_vendors or []
    existing_invoices = existing_invoices or []

    errors = []

    # ---------------------------------------------------------
    # 1. Core invoice validation
    # ---------------------------------------------------------
    invoice_result = validate_invoice(invoice_data)

    if invoice_result["status"] != "VALID":
        for error in invoice_result["errors"]:
            error["severity"] = EXCEPTION_SEVERITY.get(
                error["code"],
                "medium",
            )
            errors.append(error)

    # ---------------------------------------------------------
    # 2. Duplicate detection
    # ---------------------------------------------------------
    duplicate_input = existing_invoices + [invoice_data]
    duplicates = find_duplicate_invoices(duplicate_input)

    for duplicate in duplicates:
        errors.append({
            "code": duplicate["code"],
            "severity": EXCEPTION_SEVERITY[duplicate["code"]],
            "field": "invoice_number",
            "message": (
                f"Duplicate invoice detected for "
                f"{duplicate['vendor_name']} / "
                f"{duplicate['invoice_number']}"
            ),
            "duplicate_of": duplicate["duplicate_of"],
        })

    # ---------------------------------------------------------
    # 3. Vendor matching
    # ---------------------------------------------------------
    vendor_match = find_vendor_match(
        invoice_data.get("vendor_name", ""),
        known_vendors,
    )

    if vendor_match["status"] != "ACCEPT":
        errors.append({
            "code": "EX-008",
            "severity": EXCEPTION_SEVERITY["EX-008"],
            "field": "vendor_name",
            "message": (
                f"Vendor matching requires review: "
                f"{vendor_match['status']}"
            ),
        })

    # ---------------------------------------------------------
    # 4. Final decision
    # ---------------------------------------------------------
    status = "VALID" if not errors else "REVIEW_REQUIRED"

    return {
        "status": status,
        "invoice": invoice_result["invoice"],
        "vendor_match": vendor_match,
        "errors": errors,
    }