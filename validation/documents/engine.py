from datetime import date
from decimal import Decimal
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


def duplicate_not_run_message(missing_labels: List[str]) -> str:
    """'Duplicate check can't run yet: fill the supplier and invoice number.' Names only what is missing."""
    return f"Duplicate check can't run yet: fill the {' and '.join(missing_labels)}."


def _readable_date(value) -> Optional[str]:
    text = str(value or '')[:10]
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return text or None
    return f'{parsed.day} {parsed.strftime("%b %Y")}'


def duplicate_message(number, existing: Dict[str, Any]) -> str:
    """'Possible duplicate of PI-2611-04 (INVOICE_100003, record 10012, 20 Nov 2026).' Only what is known is named."""
    parts = [existing.get('document_code') or None,
             f"record {existing['record_code']}" if existing.get('record_code') else None,
             _readable_date(existing.get('invoice_date') or existing.get('uploaded_at'))]
    named = ', '.join(part for part in parts if part)
    return f'Possible duplicate of {number}' + (f' ({named})' if named else '') + '. Compare the two before approving.'


def validate_invoice_document(
    invoice_data: Dict[str, Any],
    known_vendors: Optional[List[Dict[str, Any]]] = None,
    existing_invoices: Optional[List[Dict[str, Any]]] = None,
    purchase_order: Optional[Dict[str, Any]] = None,
    reference_transactions: Optional[List[Dict[str, Any]]] = None,
    transaction_id: Optional[str] = None,
    policy: Optional[Dict[str, Any]] = None,
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
    # Tenant settings and the counterparty being matched, supplied by the caller. Absent means the defaults.
    policy = policy or {}
    counterparty_role = policy.get('counterparty_role', 'supplier')
    counterparty_name = policy['counterparty_name'] if 'counterparty_name' in policy else invoice_data.get('vendor_name')
    reviewed_anomaly = (policy.get('reviewed') or {}).get('EX-010')

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

                # Compared as numbers: a stored "264140.80" and a read "264140.8" are the same amount.
                accepted = reviewed_anomaly and reviewed_anomaly.get('currency') == anomaly_result['currency'] and (
                    Decimal(str(reviewed_anomaly.get('amount'))) == Decimal(anomaly_result['amount']))
                evidence = {
                    'actual_value': anomaly_result['amount'], 'expected_value': anomaly_result['upper_bound'],
                    'difference': (str(Decimal(anomaly_result['amount']) - Decimal(anomaly_result['upper_bound']))
                                   if anomaly_result['is_anomaly'] else None),
                }
                if anomaly_result["is_anomaly"] and accepted:
                    # The finding stays on record with its numbers and who accepted it. A later correction starts a new revision and undoes this.
                    checks.append({
                        "rule": "EX-010", "result": "PASS", "checkStatus": "CHECKED", "required": False, **evidence,
                        "field": "total_amount",
                        "message": (anomaly_result["reason"] + f" Accepted on review by {reviewed_anomaly.get('reviewed_by')}: "
                                    f"{reviewed_anomaly.get('note')}"),
                        "transaction_anomaly": anomaly_result,
                    })
                elif anomaly_result["is_anomaly"]:
                    errors.append({
                        "code": "EX-010",
                        "severity": "high",
                        "field": "total_amount",
                        "message": (anomaly_result["reason"] + f" Compared with {anomaly_result['baseline_count']} approved "
                                    f"records for the same party, type and currency. Human review is required."),
                        "transaction_anomaly": anomaly_result, **evidence,
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
                        "required": False, **evidence,
                        "message": (
                            f"Amount checked against {anomaly_result['baseline_count']} approved records "
                            "for the same party, type and currency."
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
    else:
        checks.append({'rule': 'EX-010', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED', 'required': False,
                       'message': 'Comparable approved history could not be read, so the anomaly check did not run.'})

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
    else:
        # The model checks these on every valid invoice, so they were run and passed. Say so.
        checks.append({'rule': 'EX-004', 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False,
                       'message': 'Line amounts add up to the pre-tax amount.'})
        checks.append({'rule': 'EX-005', 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False,
                       'message': 'The printed amounts add up to the total.'})

    # ---------------------------------------------------------
    # 2. Duplicate detection
    # ---------------------------------------------------------
    # Compare the target to each reference. A duplicate pair elsewhere in the
    # reference history says nothing about this invoice.
    # Three states, never mixed up (25 Nov): it could not run (say what to fill), it found a match (name it), or
    # it passed. A check that did not run never reads as "possible duplicate".
    matches = []
    missing = [(key, label) for key, label in (('vendor_name', 'supplier'), ('invoice_number', 'invoice number'))
               if not (isinstance(invoice_data.get(key), str) and invoice_data[key].strip())]
    if policy.get('check_duplicate') is False:
        checks.append({'rule': 'EX-007', 'result': 'NOT_APPLICABLE', 'checkStatus': 'NOT_APPLICABLE', 'required': False,
                       'message': "This company's validation settings do not include a duplicate check."})
    elif missing:
        # Said before the history: with no number there is nothing to compare, whatever the history holds.
        checks.append({'rule': 'EX-007', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED', 'required': True,
                       'field': missing[-1][0], 'missing_fields': [key for key, _ in missing],
                       'message': duplicate_not_run_message([label for _, label in missing])})
    elif existing_invoices is None:
        checks.append({'rule': 'EX-007', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED', 'required': True,
                       'message': "Duplicate check can't run yet: invoice history could not be read. Run the checks again."})
    else:
        for existing in existing_invoices:
            if invoice_data.get('document_id') and existing.get('document_id') == invoice_data['document_id']:
                continue
            if find_duplicate_invoices([existing, invoice_data]):
                matches.append(existing)
        if not matches:
            checks.append({'rule': 'EX-007', 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': True,
                           'message': (f'Checked against {len(existing_invoices)} other invoices for this company; no match.'
                                       if existing_invoices else 'History was read and this company has no other invoices yet, so there is nothing to duplicate.')})

    for existing in matches:
        errors.append({
            "code": "EX-007",
            "severity": EXCEPTION_SEVERITY["EX-007"],
            "field": "invoice_number",
            "message": duplicate_message(invoice_data.get('invoice_number'), existing),
            "duplicate_of": existing.get('document_id') or existing.get('source_file'),
            # Stored with the result, so the review page can link to the other document.
            "actual_value": invoice_data.get('invoice_number'),
            "expected_value": existing.get('document_id') or existing.get('source_file'),
        })

    # ---------------------------------------------------------
    # 3. Vendor matching
    # ---------------------------------------------------------
    vendor_name = counterparty_name
    if known_vendors is None or not known_vendors or not isinstance(vendor_name, str) or not vendor_name.strip():
        vendor_match = {'status': 'NOT_CHECKED', 'vendor_id': None, 'vendor_name': None, 'similarity': None}
        # A list that could not be read may hold a mismatch, so it still blocks. A company with no list has nothing to compare;
        # a missing name already fails its own required-field rule.
        unreadable = known_vendors is None and isinstance(vendor_name, str) and vendor_name.strip()
        checks.append({'rule': 'EX-008', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED', 'required': bool(unreadable),
                       'message': (f'The {counterparty_role} list could not be read, so this name was not matched.' if unreadable else
                                   f'No {counterparty_role} name to match.' if not (isinstance(vendor_name, str) and vendor_name.strip()) else
                                   f'This company has no {counterparty_role} list yet, so this name was not matched.')})
    else:
        # Legacy helper calls this confidence; it measures name similarity only.
        # Never write it into extraction confidence or automatically accept an entity.
        vendor_match = find_vendor_match(vendor_name, known_vendors)
        vendor_match['similarity'] = vendor_match['confidence']
        vendor_match['score_method'] = 'normalized_name_sequence_matcher'
        if vendor_match['status'] == 'ACCEPT':
            checks.append({'rule': 'EX-008', 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False,
                           'message': f'Name matches the {counterparty_role} list exactly; this does not approve the {counterparty_role}.'})

    if vendor_match["status"] not in {"ACCEPT", "NOT_CHECKED"}:
        errors.append({
            "code": "EX-008",
            "severity": EXCEPTION_SEVERITY["EX-008"],
            "field": "customer" if counterparty_role == 'customer' else "vendor_name",
            "message": (
                f"{counterparty_role.capitalize()} matching requires review: "
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
            checks.append({'rule': 'EX-009', 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False,
                           'message': 'Supplier, lines and total agree with the purchase order.'})
    elif policy.get('po_check'):
        checks.append(policy['po_check'])
    elif invoice_data.get('purchase_order_number'):
        checks.append({'rule': 'EX-009', 'result': 'WARNING', 'checkStatus': 'NOT_CHECKED', 'required': True,
                       'message': 'The purchase-order reference could not be matched automatically; review it.'})
    else:
        checks.append({'rule': 'EX-009', 'result': 'NOT_APPLICABLE', 'checkStatus': 'NOT_APPLICABLE', 'required': False,
                       'message': 'No purchase order is printed or required, so matching does not apply.'})

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
