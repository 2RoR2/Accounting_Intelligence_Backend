
"""One-to-one bank transaction matching without amount tolerance."""

from decimal import Decimal, InvalidOperation
from validation.matching.bank_reconciliation import direction_matches


def match_bank_transaction(accounting_record, bank_transaction):
    """Compare one accounting record with one bank transaction.

    Automatic matching requires exact amount, matching currency,
    and matching non-empty references.
    """

    variances = []

    category = accounting_record.get("category")
    direction = bank_transaction.get("direction")
    if category and direction and not direction_matches(category, direction):
        return {
            "status": "UNMATCHED",
            "matched_bank_transaction_id": None,
            "bank_match_score": 0.0,
            "reconciliation_difference": None,
            "variances": [{"type": "DIRECTION_MISMATCH", "category": category,
                           "bank_direction": direction}],
        }

    # Validate amounts.
    try:
        accounting_amount = Decimal(str(accounting_record["total_amount"]))
        bank_amount = Decimal(str(bank_transaction["amount"]))
    except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(
            "Valid accounting and bank transaction amounts are required"
        ) from exc

    if not accounting_amount.is_finite() or not bank_amount.is_finite():
        raise ValueError("Amounts must be finite numbers")

    difference = bank_amount - accounting_amount

    # Check currency.
    accounting_currency = accounting_record.get("currency")
    bank_currency = bank_transaction.get("currency")

    if not accounting_currency or not bank_currency:
        return {
            "status": "REVIEW_REQUIRED",
            "matched_bank_transaction_id": None,
            "bank_match_score": 0.0,
            "reconciliation_difference": format(difference, 'f'),
            "variances": [{"type": "CURRENCY_MISSING"}],
        }

    if accounting_currency.strip().upper() != bank_currency.strip().upper():
        return {
            "status": "UNMATCHED",
            "matched_bank_transaction_id": None,
            "bank_match_score": 0.0,
            "reconciliation_difference": format(difference, 'f'),
            "variances": [{
                "type": "CURRENCY_MISMATCH",
                "accounting_currency": accounting_currency,
                "bank_currency": bank_currency,
            }],
        }

    # Exact amount comparison: any difference requires review.
    if accounting_amount != bank_amount:
        variances.append({
            "type": "AMOUNT_VARIANCE",
            "difference": format(difference, 'f'),
        })

    # References must be present and match for automatic matching.
    accounting_reference = accounting_record.get("reference_number")
    bank_reference = bank_transaction.get("reference")

    if accounting_reference and bank_reference:
        if (
            accounting_reference.strip().casefold()
            != bank_reference.strip().casefold()
        ):
            variances.append({"type": "REFERENCE_MISMATCH"})
    else:
        variances.append({"type": "REFERENCE_MISSING"})

    if variances:
        return {
            "status": "REVIEW_REQUIRED",
            "matched_bank_transaction_id": None,
            "bank_match_score": 0.0,
            "reconciliation_difference": format(difference, 'f'),
            "variances": variances,
        }

    return {
        "status": "MATCHED",
        "matched_bank_transaction_id": bank_transaction.get(
            "transaction_id"
        ),
        "bank_match_score": 1.0,
        "reconciliation_difference": format(difference, 'f'),
        "variances": [],
    }
