
from validation.bank_matching import match_bank_transaction


def make_accounting_record(**overrides):
    record = {
        "total_amount": "100.00",
        "currency": "MYR",
        "reference_number": "PAY-001",
        "invoice_date": "2026-10-01",
    }
    record.update(overrides)
    return record


def make_bank_transaction(**overrides):
    transaction = {
        "transaction_id": "bank-001",
        "amount": "100.00",
        "currency": "MYR",
        "reference": "PAY-001",
        "transaction_date": "2026-10-05",
    }
    transaction.update(overrides)
    return transaction


def test_bank_match_success():
    result = match_bank_transaction(
        make_accounting_record(),
        make_bank_transaction(),
    )

    assert result["status"] == "MATCHED"
    assert result["matched_bank_transaction_id"] == "bank-001"
    assert result["reconciliation_difference"] == 0.0
    assert result["variances"] == []


def test_bank_match_any_amount_difference_requires_review():
    result = match_bank_transaction(
        make_accounting_record(),
        make_bank_transaction(amount="100.01"),
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reconciliation_difference"] == 0.01
    assert any(
        variance["type"] == "AMOUNT_VARIANCE"
        for variance in result["variances"]
    )


def test_large_amount_difference_requires_review():
    result = match_bank_transaction(
        make_accounting_record(),
        make_bank_transaction(amount="90.00"),
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert any(
        variance["type"] == "AMOUNT_VARIANCE"
        for variance in result["variances"]
    )


def test_reference_mismatch_requires_review():
    result = match_bank_transaction(
        make_accounting_record(),
        make_bank_transaction(reference="OTHER-REF"),
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert any(
        variance["type"] == "REFERENCE_MISMATCH"
        for variance in result["variances"]
    )


def test_missing_reference_requires_review():
    result = match_bank_transaction(
        make_accounting_record(reference_number=None),
        make_bank_transaction(reference=None),
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert any(
        variance["type"] == "REFERENCE_MISSING"
        for variance in result["variances"]
    )


def test_currency_mismatch_is_unmatched():
    result = match_bank_transaction(
        make_accounting_record(currency="MYR"),
        make_bank_transaction(currency="USD"),
    )

    assert result["status"] == "UNMATCHED"
    assert any(
        variance["type"] == "CURRENCY_MISMATCH"
        for variance in result["variances"]
    )


def test_missing_currency_requires_review():
    result = match_bank_transaction(
        make_accounting_record(currency=None),
        make_bank_transaction(),
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert any(
        variance["type"] == "CURRENCY_MISSING"
        for variance in result["variances"]
    )


def test_invalid_accounting_amount_raises_error():
    try:
        match_bank_transaction(
            make_accounting_record(total_amount="not-a-number"),
            make_bank_transaction(),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for invalid amount")


def test_non_finite_amount_raises_error():
    try:
        match_bank_transaction(
            make_accounting_record(total_amount="NaN"),
            make_bank_transaction(),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for non-finite amount")


def test_invalid_bank_amount_raises_error():
    try:
        match_bank_transaction(
            make_accounting_record(),
            make_bank_transaction(amount="invalid"),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for invalid bank amount")
