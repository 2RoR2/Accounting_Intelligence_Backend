from collections import defaultdict
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Iterable, Mapping


MODIFIED_Z_NORMALIZATION = Decimal("0.6745")
MODIFIED_Z_CUTOFF = Decimal("3.5")


def _to_decimal(value: Any) -> Decimal:
    """Convert an amount to Decimal and reject invalid financial values."""
    if isinstance(value, bool):
        raise ValueError("Transaction amount cannot be a boolean.")

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid transaction amount: {value!r}") from None

    if not amount.is_finite() or amount < 0:
        raise ValueError("Amount must be finite and non-negative.")

    return amount


def detect_transaction_anomalies(
    transactions,
    *,
    reference_transactions=None,
):
    """Detect unusually large amounts using a separate reference dataset.

    If reference_transactions is provided, only reference data is used
    to calculate the statistical baseline. Test transactions are not
    included in that baseline.
    """
    def prepare_rows(items):
        rows = []
        seen_ids = set()

        for transaction in items:
            transaction_id = str(
                transaction.get("transaction_id", "")
            ).strip()
            currency = str(transaction.get("currency", "")).strip().upper()

            if not transaction_id:
                raise ValueError("Every transaction needs a transaction_id.")

            if transaction_id in seen_ids:
                raise ValueError(
                    f"Duplicate transaction_id: {transaction_id}"
                )

            if not currency:
                raise ValueError(
                    f"Transaction {transaction_id} needs a currency."
                )

            amount = _to_decimal(transaction.get("amount"))

            seen_ids.add(transaction_id)
            rows.append({
                "transaction_id": transaction_id,
                "amount": amount,
                "currency": currency,
            })

        return rows

    test_rows = prepare_rows(transactions)

    if reference_transactions is None:
        reference_rows = test_rows
    else:
        reference_rows = prepare_rows(reference_transactions)

    groups = defaultdict(list)

    for row in reference_rows:
        groups[row["currency"]].append(row)

    results = []

    for row in test_rows:
        amount = row["amount"]
        currency = row["currency"]
        comparable_rows = [
            reference for reference in groups.get(currency, [])
            if reference["transaction_id"] != row["transaction_id"]
        ]
        baseline = None
        # Five varied comparable approved records are the minimum statistical
        # baseline. A constant history is also retained with fewer rows so the
        # caller can report the zero-MAD condition explicitly.
        if (len(comparable_rows) >= 5
                or (comparable_rows and len({reference["amount"] for reference in comparable_rows}) == 1)):
            amounts = [reference["amount"] for reference in comparable_rows]
            center = Decimal(str(median(amounts)))
            deviations = [abs(value - center) for value in amounts]
            baseline = {
                "median": center,
                "mad": Decimal(str(median(deviations))),
                "count": len(amounts),
            }

        result = {
            "transaction_id": row["transaction_id"],
            "amount": str(amount),
            "currency": currency,
            "is_anomaly": False,
            "status": "insufficient_baseline",
            "anomaly_score": None,
            "baseline_median": None,
            "upper_bound": None,
            "baseline_count": 0,
            "reason": f"No reference transactions are available for {currency}.",
        }

        if baseline is not None:
            center = baseline["median"]
            mad = baseline["mad"]

            result.update({
                "baseline_median": str(center),
                "baseline_count": baseline["count"],
            })

            if mad == 0:
                # A short constant history, or a zero-valued history, cannot
                # support an automatic decision. Keep it visible as
                # insufficient baseline; a sufficiently large non-zero
                # constant history can still identify a different amount.
                if len(comparable_rows) < 5 or center == 0:
                    result.update({
                        "status": "insufficient_baseline",
                        "reason": f"The {currency} reference baseline has zero MAD; more varied history is needed.",
                    })
                    results.append(result)
                    continue
                is_anomaly = amount != center
                result.update({
                    "is_anomaly": is_anomaly,
                    "status": "anomaly" if is_anomaly else "normal",
                    "upper_bound": str(center),
                    "reason": (
                        f"The amount differs from the constant {currency} reference baseline."
                        if is_anomaly else
                        f"The amount matches the constant {currency} reference baseline."
                    ),
                })
                results.append(result)
                continue

            # Iglewicz-Hoaglin modified Z score, as documented by NIST:
            # M_i = 0.6745 * (x_i - median) / MAD; >3.5 labels a high-side
            # potential outlier for this amount-only review feature.
            # Source: https://itl.nist.gov/div898/handbook/eda/section3/eda35h.htm
            modified_z = MODIFIED_Z_NORMALIZATION * (amount - center) / mad
            upper_bound = center + (MODIFIED_Z_CUTOFF * mad / MODIFIED_Z_NORMALIZATION)
            is_anomaly = modified_z > MODIFIED_Z_CUTOFF

            result.update({
                "is_anomaly": is_anomaly,
                "status": "anomaly" if is_anomaly else "normal",
                "anomaly_score": str(modified_z),
                "upper_bound": str(upper_bound),
                "reason": (
                    f"Modified Z-score {modified_z} exceeds {MODIFIED_Z_CUTOFF} for {currency}."
                    if is_anomaly
                    else f"Modified Z-score {modified_z} is at or below {MODIFIED_Z_CUTOFF} for {currency}."
                ),
            })

        results.append(result)

    return results
