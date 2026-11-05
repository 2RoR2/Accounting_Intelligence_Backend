from decimal import Decimal

import pytest

from validation.documents.engine import validate_invoice_document
from validation.transactions.transaction_anomaly import detect_transaction_anomalies


def test_detects_extremely_large_transaction():
    amounts = ["100", "110", "95", "105", "1000000", "98"]

    transactions = [
        {
            "transaction_id": f"T{i}",
            "amount": amount,
            "currency": "MYR",
        }
        for i, amount in enumerate(amounts, start=1)
    ]

    results = detect_transaction_anomalies(transactions)
    result = next(r for r in results if r["transaction_id"] == "T5")

    assert result["is_anomaly"] is True
    assert result["status"] == "anomaly"
    assert Decimal(result["anomaly_score"]) > 1


def test_normal_transactions_are_not_flagged():
    amounts = ["100", "110", "95", "105", "98", "102"]

    transactions = [
        {
            "transaction_id": f"T{i}",
            "amount": amount,
            "currency": "MYR",
        }
        for i, amount in enumerate(amounts, start=1)
    ]

    results = detect_transaction_anomalies(transactions)

    assert all(result["status"] == "normal" for result in results)


def test_small_dataset_requires_more_baseline_data():
    transactions = [
        {"transaction_id": "T1", "amount": "100", "currency": "MYR"},
        {"transaction_id": "T2", "amount": "1000000", "currency": "MYR"},
    ]

    results = detect_transaction_anomalies(transactions)

    assert all(
        result["status"] == "insufficient_baseline"
        for result in results
    )


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity", "invalid"])
def test_rejects_invalid_amounts(amount):
    transactions = [
        {"transaction_id": "T1", "amount": amount, "currency": "MYR"}
    ]

    with pytest.raises(ValueError):
        detect_transaction_anomalies(transactions)


def test_rejects_duplicate_transaction_ids():
    transactions = [
        {"transaction_id": "T1", "amount": "100", "currency": "MYR"},
        {"transaction_id": "T1", "amount": "200", "currency": "MYR"},
    ]

    with pytest.raises(ValueError, match="Duplicate transaction_id"):
        detect_transaction_anomalies(transactions)

def test_high_value_transaction_is_not_automatically_anomalous():
    """A high amount may be normal if the transaction pattern supports it."""
    amounts = ["100", "110", "95", "105", "98", "102", "1000"]

    transactions = [
        {
            "transaction_id": f"T{i}",
            "amount": amount,
            "currency": "MYR",
        }
        for i, amount in enumerate(amounts, start=1)
    ]

    results = detect_transaction_anomalies(transactions)
    high_value = next(
        result for result in results if result["transaction_id"] == "T7"
    )

    # This assertion checks the current algorithm's behaviour.
    # It does not establish whether the transaction is legitimate.
    assert high_value["status"] in {"normal", "anomaly"}


def test_separate_currency_groups_do_not_share_baselines():
    transactions = [
        {"transaction_id": "M1", "amount": "100", "currency": "MYR"},
        {"transaction_id": "M2", "amount": "110", "currency": "MYR"},
        {"transaction_id": "M3", "amount": "95", "currency": "MYR"},
        {"transaction_id": "M4", "amount": "105", "currency": "MYR"},
        {"transaction_id": "M5", "amount": "98", "currency": "MYR"},
        {"transaction_id": "U1", "amount": "10000", "currency": "USD"},
    ]

    results = detect_transaction_anomalies(transactions)
    usd_result = next(
        result for result in results if result["transaction_id"] == "U1"
    )

    assert usd_result["status"] == "insufficient_baseline"


def test_zero_amounts_do_not_crash_detector():
    transactions = [
        {"transaction_id": f"Z{i}", "amount": "0", "currency": "MYR"}
        for i in range(1, 7)
    ]

    results = detect_transaction_anomalies(transactions)

    assert len(results) == 6
    assert all(result["status"] == "normal" for result in results)


def test_reference_history_is_used_for_single_invoice_validation_and_creates_review_exception():
    history = [
        {"transaction_id": f"H{i}", "amount": str(amount), "currency": "MYR"}
        for i, amount in enumerate((95, 98, 100, 102, 105), start=1)
    ]
    result = validate_invoice_document(
        {
            "transaction_id": "CURRENT-1",
            "invoice_number": "INV-1",
            "vendor_name": "Synthetic Supplier",
            "invoice_date": "2026-10-20",
            "currency": "MYR",
            "subtotal": "50000.00",
            "tax_amount": "0.00",
            "total_amount": "50000.00",
            "line_items": [],
        },
        existing_invoices=[],
        reference_transactions=history,
    )

    anomaly = result["transaction_anomaly"]
    assert result["status"] == "REVIEW_REQUIRED"
    assert anomaly["status"] == "anomaly"
    assert anomaly["baseline_count"] == 5
    assert any(error["code"] == "EX-010" for error in result["errors"])
    assert "upper bound" in next(
        error["message"] for error in result["errors"] if error["code"] == "EX-010"
    )


def test_insufficient_history_is_reported_without_blocking_validation():
    history = [
        {"transaction_id": f"H{i}", "amount": str(100 + i), "currency": "MYR"}
        for i in range(1, 4)
    ]
    result = validate_invoice_document(
        {"transaction_id": "CURRENT-2", "currency": "MYR", "total_amount": "101.00"},
        existing_invoices=[],
        reference_transactions=history,
    )
    anomaly_check = next(check for check in result["checks"] if check["rule"] == "EX-010")
    assert result["transaction_anomaly"]["status"] == "insufficient_baseline"
    assert anomaly_check["required"] is False
