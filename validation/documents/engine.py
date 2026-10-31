from typing import Any, Dict, List, Optional

from validation.documents.models import validate_invoice
from validation.documents.duplicate import find_duplicate_invoices
from validation.matching.vendor_matching import find_vendor_match
from validation.matching.po_matching import match_invoice_to_po
from validation.transactions.transaction_anomaly import detect_transaction_anomalies


EXCEPTION_SEVERITY = {
    "EX-001": "medium",
    "EX-002": "medium",
    "EX-003": "high",
    "EX-004": "high",
    "EX-005": "high",
    "EX-006": "medium",
    "EX-007": "high",
    "EX-008": "medium",
    "EX-009": "high",
    "EX-010": "high",
}


def validate_invoice_document(
    invoice_data: Dict[str, Any],
    known_vendors: Optional[List[Dict[str, Any]]] = None,
    existing_invoices: Optional[List[Dict[str, Any]]] = None,
    purchase_order: Optional[Dict[str, Any]] = None,
    reference_transactions: Optional[List[Dict[str, Any]]] = None,
    transaction_id: Optional[str] = None,
) -> dict:
    
    """
    Run the complete validation flow for one invoice.

    The engine combines:
    1. Invoice schema and accounting validation
    2. Duplicate invoice detection
    3. Vendor matching

    Returns one consolidated validation result.
    """

    errors = []
    checks = []


    anomaly_result = None

    if reference_transactions is not None:
        amount = invoice_data.get("total_amount")
        currency = invoice_data.get("currency")
        resolved_transaction_id = (
            transaction_id
            or invoice_data.get("transaction_id")
            or invoice_data.get("document_id")
        )

        if (
            amount is not None
            and currency
            and resolved_transaction_id
        ):
            try:
                anomaly_results = detect_transaction_anomalies(
                    [
                        {
                            "transaction_id": str(resolved_transaction_id),
                            "amount": amount,
                            "currency": currency,
                        }
                    ],
                    reference_transactions=reference_transactions,
                )

                anomaly_result = anomaly_results[0]

                if anomaly_result["is_anomaly"]:
                    errors.append({
                        "code": "EX-010",
                        "severity": "high",
                        "field": "total_amount",
                        "message": anomaly_result["reason"] + " Human review is required.",
                        "transaction_anomaly": anomaly_result,
                    })

                elif anomaly_result["status"] == "insufficient_baseline":
                    checks.append({
                        "rule": "EX-010",
                        "result": "WARNING",
                        "checkStatus": "NOT_CHECKED",
                        "message": anomaly_result["reason"],
                        "required": False,
                    })

                else:
                    checks.append({
                        "rule": "EX-010",
                        "result": "PASS",
                        "checkStatus": "CHECKED",
                        "message": (
                            "Transaction amount checked against the "
                            "reference baseline."
                        ),
                    })

            except (ValueError, TypeError):
                checks.append({
                    "rule": "EX-010",
                    "result": "WARNING",
                    "checkStatus": "NOT_CHECKED",
                    "message": "Transaction anomaly check could not use the supplied reference history.",
                    "required": False,
                })

        else:
            checks.append({
                "rule": "EX-010",
                "result": "WARNING",
                "checkStatus": "NOT_CHECKED",
                "message": (
                    "Transaction amount, currency, or transaction ID "
                    "is missing; anomaly detection was not run."
                ),
                "required": False,
            })

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
    # Compare the target to each reference. A duplicate pair elsewhere in the
    # reference history says nothing about this invoice.
    duplicates = []
    duplicate_key_ready = all(isinstance(invoice_data.get(key), str) and invoice_data[key].strip()
                              for key in ('vendor_name','invoice_number'))
    if existing_invoices is None or not duplicate_key_ready:
        checks.append({'rule': 'EX-007', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED',
                       'message': 'Authorized current-invoice references or a complete supplier/invoice key are unavailable.'})
    else:
        for existing in existing_invoices:
            if invoice_data.get('document_id') and existing.get('document_id') == invoice_data['document_id']:
                continue
            duplicates.extend(find_duplicate_invoices([existing, invoice_data]))
        if not duplicates:
            checks.append({'rule': 'EX-007', 'result': 'PASS', 'checkStatus': 'CHECKED',
                           'message': 'Current same-tenant invoice references checked.'})

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
    vendor_name = invoice_data.get('vendor_name')
    if not known_vendors or not isinstance(vendor_name, str) or not vendor_name.strip():
        vendor_match = {'status': 'NOT_CHECKED', 'vendor_id': None, 'vendor_name': None, 'similarity': None}
        checks.append({'rule': 'EX-008', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED',
                       'message': 'No applicable tenant supplier catalogue or resolved supplier name; vendor matching was not checked.'})
    else:
        # Legacy helper calls this confidence; it measures name similarity only.
        # Never write it into extraction confidence or automatically accept an entity.
        vendor_match = find_vendor_match(invoice_data.get('vendor_name'), known_vendors)
        vendor_match['similarity'] = vendor_match['confidence']
        vendor_match['score_method'] = 'normalized_name_sequence_matcher'
        if vendor_match['status'] == 'ACCEPT':
            checks.append({'rule': 'EX-008', 'result': 'PASS', 'checkStatus': 'CHECKED',
                           'message': 'Tenant vendor names compared; this does not approve an entity.'})

    if vendor_match["status"] not in {"ACCEPT", "NOT_CHECKED"}:
        errors.append({
            "code": "EX-008",
            "severity": EXCEPTION_SEVERITY["EX-008"],
            "field": "vendor_name",
            "message": (
                f"Vendor matching requires review: "
                f"{vendor_match['status']}"
            ),
            'vendor_candidate': {key: vendor_match.get(key) for key in
                                 ('vendor_id', 'vendor_name', 'similarity', 'score_method')},
        })

    # ---------------------------------------------------------
    # 4. Purchase order matching
    # ---------------------------------------------------------
    po_match = None

    if purchase_order is not None:
        po_match = match_invoice_to_po(
            invoice_data,
            purchase_order,
        )

        if po_match["status"] != "MATCHED":
            errors.append({
                "code": "EX-009",
                "severity": EXCEPTION_SEVERITY["EX-009"],
                "field": "purchase_order",
                "message": (
                    "Purchase order matching requires review."
                ),
                "po_match": po_match,
            })
    else:
        checks.append({'rule': 'EX-009', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED',
                       'message': 'No authorized purchase-order reference supplied; database PO matching is not implemented.'})

    # ---------------------------------------------------------
    # 5. Final decision
    # ---------------------------------------------------------
    status = "VALID" if not errors else "REVIEW_REQUIRED"

    return {
        "status": status,
        "invoice": invoice_result["invoice"],
        "vendor_match": vendor_match,
        "po_match": po_match,
        "transaction_anomaly": anomaly_result,
        "errors": errors,
        'checks': checks,
    }
