from collections import defaultdict
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Iterable, Mapping


MIN_BASELINE_TRANSACTIONS = 5
MAD_MULTIPLIER = Decimal("4.4478")
MEDIAN_MULTIPLIER = Decimal("3")


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
    min_baseline_transactions=MIN_BASELINE_TRANSACTIONS,
):
    """Detect unusually large amounts using a separate reference dataset.

    If reference_transactions is provided, only reference data is used
    to calculate the statistical baseline. Test transactions are not
    included in that baseline.
    """
    if min_baseline_transactions < 2:
        raise ValueError("Minimum baseline must be at least 2.")

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
        groups[row["currency"]].append(row["amount"])

    baselines = {}

    for currency, amounts in groups.items():
        if len(amounts) < min_baseline_transactions:
            continue

        center = Decimal(str(median(amounts)))
        deviations = [abs(amount - center) for amount in amounts]
        mad = Decimal(str(median(deviations)))

        threshold = center + max(
            MAD_MULTIPLIER * mad,
            MEDIAN_MULTIPLIER * center,
        )

        baselines[currency] = {
            "median": center,
            "threshold": threshold,
            "count": len(amounts),
        }

    results = []

    for row in test_rows:
        amount = row["amount"]
        currency = row["currency"]
        baseline = baselines.get(currency)

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
            "reason": (
                f"Insufficient reference transactions for {currency}; "
                f"at least {min_baseline_transactions} are required."
            ),
        }

        if baseline is not None:
            threshold = baseline["threshold"]
            is_anomaly = amount > threshold

            if threshold > 0:
                score_text = str(amount / threshold)
            else:
                score_text = "Infinity" if amount > 0 else "0"

            result.update({
                "is_anomaly": is_anomaly,
                "status": "anomaly" if is_anomaly else "normal",
                "anomaly_score": score_text,
                "baseline_median": str(baseline["median"]),
                "upper_bound": str(threshold),
                "baseline_count": baseline["count"],
                "reason": (
                    f"Amount {amount} {currency} exceeds the statistical "
                    f"upper bound of {threshold} {currency}."
                    if is_anomaly
                    else f"Amount is within the expected range for {currency}."
                ),
            })

        results.append(result)

    return results
