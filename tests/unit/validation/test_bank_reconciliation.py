
from validation.matching.bank_reconciliation import find_bank_match_candidates
from validation.matching.bank_reconciliation import reconcile_records


def make_accounting_record(**overrides):
    record = {
        "total_amount": "100.00",
        "currency": "MYR",
        "reference_number": "PAY-001",
    }
    record.update(overrides)
    return record


def make_bank_transaction(**overrides):
    transaction = {
        "transaction_id": "bank-001",
        "amount": "100.00",
        "currency": "MYR",
        "reference": "PAY-001",
    }
    transaction.update(overrides)
    return transaction


def test_single_candidate_is_matched():
    result = find_bank_match_candidates(
        make_accounting_record(),
        [make_bank_transaction()],
    )

    assert result["status"] == "MATCHED"
    assert result["matched_bank_transaction_id"] == "bank-001"
    assert result["candidates"] == ["bank-001"]


def test_multiple_candidates_require_review():
    result = find_bank_match_candidates(
        make_accounting_record(),
        [
            make_bank_transaction(transaction_id="bank-001"),
            make_bank_transaction(transaction_id="bank-002"),
        ],
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["matched_bank_transaction_id"] is None
    assert result["candidates"] == ["bank-001", "bank-002"]
    assert result["reason"] == "MULTIPLE_MATCH_CANDIDATES"


def test_no_candidate_is_unmatched():
    result = find_bank_match_candidates(
        make_accounting_record(),
        [make_bank_transaction(amount="90.00")],
    )

    assert result["status"] == "UNMATCHED"
    assert result["candidates"] == []


def test_different_currency_is_not_a_candidate():
    result = find_bank_match_candidates(
        make_accounting_record(),
        [make_bank_transaction(currency="USD")],
    )

    assert result["status"] == "UNMATCHED"


def test_different_reference_is_not_a_candidate():
    result = find_bank_match_candidates(
        make_accounting_record(),
        [make_bank_transaction(reference="OTHER-001")],
    )

    assert result["status"] == "UNMATCHED"


def test_missing_accounting_reference_requires_review():
    result = find_bank_match_candidates(
        make_accounting_record(reference_number=None),
        [make_bank_transaction()],
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reason"] == "ACCOUNTING_CURRENCY_OR_REFERENCE_MISSING"


def test_invalid_accounting_amount_requires_review():
    result = find_bank_match_candidates(
        make_accounting_record(total_amount="invalid"),
        [make_bank_transaction()],
    )

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["reason"] == "INVALID_ACCOUNTING_AMOUNT"


def test_reconciliation_does_not_reuse_bank_transaction():
    records = [
        make_accounting_record(record_id="record-001"),
        make_accounting_record(record_id="record-002"),
    ]
    transactions = [make_bank_transaction()]

    result = reconcile_records(records, transactions)

    assert result["results"][0]["status"] == "MATCHED"
    assert result["results"][1]["status"] == "UNMATCHED"
    assert result["used_bank_transaction_ids"] == ["bank-001"]


def test_reconciliation_can_match_two_distinct_transactions():
    records = [
        make_accounting_record(
            record_id="record-001",
            reference_number="PAY-001",
        ),
        make_accounting_record(
            record_id="record-002",
            reference_number="PAY-002",
        ),
    ]

    transactions = [
        make_bank_transaction(
            transaction_id="bank-001",
            reference="PAY-001",
        ),
        make_bank_transaction(
            transaction_id="bank-002",
            reference="PAY-002",
        ),
    ]

    result = reconcile_records(records, transactions)

    assert all(
        item["status"] == "MATCHED"
        for item in result["results"]
    )
    assert result["used_bank_transaction_ids"] == [
        "bank-001",
        "bank-002",
    ]


def test_reconciliation_returns_result_for_every_record():
    records = [
        make_accounting_record(record_id="record-001"),
        make_accounting_record(
            record_id="record-002",
            reference_number="NO-MATCH",
        ),
    ]

    result = reconcile_records(records, [make_bank_transaction()])

    assert len(result["results"]) == 2
    assert result["results"][0]["status"] == "MATCHED"
    assert result["results"][1]["status"] == "UNMATCHED"


def test_direction_compatibility_filters_payable_and_receivable_matches():
    record = {"record_id": "record-1", "category": "accounts_payable",
              "total_amount": "25.00", "currency": "MYR", "reference_number": "REF-1"}
    transactions = [
        {"transaction_id": "in-1", "amount": "25.00", "currency": "MYR",
         "reference": "REF-1", "direction": "money_in"},
        {"transaction_id": "out-1", "amount": "25.00", "currency": "MYR",
         "reference": "REF-1", "direction": "money_out"},
    ]

    result = find_bank_match_candidates(record, transactions)

    assert result["status"] == "MATCHED"
    assert result["matched_bank_transaction_id"] == "out-1"
