
"""Basic candidate selection for bank reconciliation."""


def direction_matches(category, direction):
    """Bank outflows settle payables; inflows settle receivables."""
    expected = {'accounts_payable': 'money_out', 'accounts_receivable': 'money_in'}.get(category)
    return expected is not None and direction == expected


def find_bank_match_candidates(accounting_record, bank_transactions):
    """Find bank transactions that exactly match an accounting record.

    Candidates must have the same amount, currency, and non-empty reference.
    This function does not confirm or save a reconciliation.
    """

    accounting_amount = accounting_record.get("total_amount")
    accounting_currency = accounting_record.get("currency")
    accounting_reference = accounting_record.get("reference_number")

    if accounting_amount is None:
        return {
            "status": "REVIEW_REQUIRED",
            "matched_bank_transaction_id": None,
            "candidates": [],
            "reason": "ACCOUNTING_AMOUNT_MISSING",
        }

    if not accounting_currency or not accounting_reference:
        return {
            "status": "REVIEW_REQUIRED",
            "matched_bank_transaction_id": None,
            "candidates": [],
            "reason": "ACCOUNTING_CURRENCY_OR_REFERENCE_MISSING",
        }

    category = accounting_record.get("category")
    if category not in (None, 'accounts_payable', 'accounts_receivable'):
        return {"status": "REVIEW_REQUIRED", "matched_bank_transaction_id": None,
                "candidates": [], "reason": "ACCOUNTING_DIRECTION_UNSUPPORTED"}

    from decimal import Decimal, InvalidOperation

    try:
        amount = Decimal(str(accounting_amount))
        if not amount.is_finite():
            raise ValueError("Accounting amount must be finite")
    except (InvalidOperation, ValueError, TypeError):
        return {
            "status": "REVIEW_REQUIRED",
            "matched_bank_transaction_id": None,
            "candidates": [],
            "reason": "INVALID_ACCOUNTING_AMOUNT",
        }

    matches = []

    for transaction in bank_transactions:
        try:
            bank_amount = Decimal(str(transaction["amount"]))
            if not bank_amount.is_finite():
                continue
        except (KeyError, InvalidOperation, ValueError, TypeError):
            continue

        bank_currency = transaction.get("currency")
        bank_reference = transaction.get("reference")
        transaction_id = transaction.get("transaction_id")

        if category and not direction_matches(category, transaction.get('direction')):
            continue

        if not bank_currency or not bank_reference or not transaction_id:
            continue

        same_amount = amount == bank_amount
        same_currency = (
            accounting_currency.strip().upper()
            == bank_currency.strip().upper()
        )
        same_reference = (
            accounting_reference.strip().casefold()
            == bank_reference.strip().casefold()
        )

        if same_amount and same_currency and same_reference:
            matches.append(transaction)

    if len(matches) == 1:
        return {
            "status": "MATCHED",
            "matched_bank_transaction_id": matches[0]["transaction_id"],
            "candidates": [matches[0]["transaction_id"]],
            "reason": None,
        }

    if len(matches) > 1:
        return {
            "status": "REVIEW_REQUIRED",
            "matched_bank_transaction_id": None,
            "candidates": [
                transaction["transaction_id"] for transaction in matches
            ],
            "reason": "MULTIPLE_MATCH_CANDIDATES",
        }

    return {
        "status": "UNMATCHED",
        "matched_bank_transaction_id": None,
        "candidates": [],
        "reason": "NO_MATCHING_TRANSACTION",
    }


def reconcile_records(accounting_records, bank_transactions):
    """Match multiple accounting records to bank transactions.

    Each bank transaction can be assigned at most once in this run.
    Ambiguous matches require review.
    """

    results = []
    used_transaction_ids = set()

    for record in accounting_records:
        candidates_result = find_bank_match_candidates(
            record,
            [
                transaction
                for transaction in bank_transactions
                if transaction.get("transaction_id") not in used_transaction_ids
            ],
        )

        if candidates_result["status"] == "MATCHED":
            transaction_id = candidates_result["matched_bank_transaction_id"]
            used_transaction_ids.add(transaction_id)

        results.append({
            "accounting_record_id": record.get("record_id"),
            **candidates_result,
        })

    return {
        "results": results,
        "used_bank_transaction_ids": sorted(used_transaction_ids),
    }
