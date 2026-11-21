from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import HTTPException

import main  # Load the route registry before importing its accounting module.
from services.routes.accounting import routes as accounting_routes


TENANT_ID = UUID("10000000-0000-0000-0000-000000000001")
USER_ID = UUID("10000000-0000-0000-0000-000000000002")
TRANSACTION_ID = UUID("10000000-0000-0000-0000-000000000003")
SETTLEMENT_ID = UUID("10000000-0000-0000-0000-000000000004")
RECORD_ID = UUID("10000000-0000-0000-0000-000000000005")
PERIOD_ID = UUID("10000000-0000-0000-0000-000000000006")
PAYABLE_ID = UUID("10000000-0000-0000-0000-000000000007")
BANK_ACCOUNT_ID = UUID("10000000-0000-0000-0000-000000000008")
JOURNAL_ID = UUID("10000000-0000-0000-0000-000000000009")


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        if self.row is None:
            return []
        if isinstance(self.row, list):
            return self.row
        return [self.row]


class FakeDatabase:
    def __init__(self, existing_journal=None, confirmed_match=True, currency="MYR", bill_payable=None, kind="bill",
                 existing_lines=None):
        self.existing_journal = existing_journal
        self.bill_payable = bill_payable
        self.kind = kind
        self.existing_lines = existing_lines
        self.confirmed_match = confirmed_match
        self.currency = currency
        self.journal_inserts = []
        self.line_inserts = []
        self.audit_inserts = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        sql = " ".join(query.split())
        if "FROM bank_transactions" in sql:
            return Cursor({
                "id": TRANSACTION_ID,
                "direction": "money_out",
                "status": "matched",
                "posted_date": date(2026, 5, 10),
                "signed_amount_decimal": Decimal("-125.00"),
                "currency": self.currency,
            })
        if "FROM settlements" in sql:
            return Cursor({
                "id": SETTLEMENT_ID,
                "settlement_ref": "BANK-001",
                "direction": "money_out",
                "amount": Decimal("125.00"),
                "currency": self.currency,
                "status": "recorded",
            })
        if "FROM journal_lines l JOIN ledger_accounts" in sql:
            return Cursor(self.existing_lines if self.existing_lines is not None else [
                {"account_type": "liability", "debit": Decimal("125.00"), "credit": Decimal("0")},
                {"account_type": "asset", "debit": Decimal("0"), "credit": Decimal("125.00")}])
        if "FROM journal_headers j JOIN journal_lines" in sql:
            return Cursor({"account_id": self.bill_payable} if self.bill_payable else None)
        if "FROM journal_headers WHERE tenant_id=%s AND idempotency_key=%s" in sql:
            journal = self.existing_journal
            return Cursor({"source_type": journal["source_type"], "source_id": journal["source_id"]}
                          if journal and journal.get("idempotency_key") == params[1] else None)
        if "FROM journal_headers" in sql:
            journal = self.existing_journal
            if journal and "status='posted'" in sql and journal["status"] != "posted":
                journal = None
            return Cursor([journal] if journal else [])
        if "FROM reconciliation_candidates c" in sql:
            if not self.confirmed_match:
                return Cursor()
            return Cursor({
                "record_id": RECORD_ID,
                "document_id": RECORD_ID,
                "kind": self.kind,
                "category": "accounts_payable",
                "amount_due": Decimal("125.00"),
                "currency": self.currency,
                "is_current": True,
                "validation_status": "passed",
            })
        if "FROM settlement_allocations" in sql:
            return Cursor({
                "record_count": 1,
                "allocated_amount": Decimal("125.00"),
                "record_amount": Decimal("125.00"),
            })
        if "FROM accounting_periods" in sql:
            return Cursor({
                "id": PERIOD_ID,
                "status": "open",
                "starts_on": date(2026, 5, 1),
                "ends_on": date(2026, 5, 31),
            })
        if "FROM ledger_accounts" in sql:
            account_type = "liability" if params[0] == PAYABLE_ID else "asset"
            return Cursor({"account_type": account_type, "is_active": True})
        if "INSERT INTO journal_headers" in sql:
            self.journal_inserts.append((sql, params))
            return Cursor({"id": JOURNAL_ID})
        if "INSERT INTO journal_lines" in sql:
            self.line_inserts.append((sql, params))
            return Cursor()
        if "INSERT INTO audit_logs" in sql:
            self.audit_inserts.append((sql, params))
            return Cursor()
        raise AssertionError(f"Unexpected SQL: {sql}")


def _configure(monkeypatch, database, payload=None):
    user = {"tenant_id": TENANT_ID, "id": USER_ID, "name": "Company admin", "role": "local_admin"}
    monkeypatch.setattr(accounting_routes, "connect", lambda: database)
    monkeypatch.setattr(accounting_routes, "require_workspace_user", lambda *_args: user)
    return payload or accounting_routes.SupplierPaymentPostingInput(
        periodId=str(PERIOD_ID), payableAccountId=str(PAYABLE_ID),
        bankAccountId=str(BANK_ACCOUNT_ID), journalDate=date(2026, 5, 10))


def test_supplier_payment_posts_one_balanced_settlement_journal(monkeypatch):
    database = FakeDatabase()
    payload = _configure(monkeypatch, database)

    result = accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert result == {
        "id": str(JOURNAL_ID),
        "status": "posted",
        "idempotent": False,
        "recordId": str(RECORD_ID),
        "settlementId": str(SETTLEMENT_ID),
        "amount": "125.00",
        "currency": "MYR",
    }
    assert len(database.journal_inserts) == 1
    assert len(database.line_inserts) == 2
    assert database.line_inserts[0][1][2] == PAYABLE_ID
    assert database.line_inserts[1][1][2] == BANK_ACCOUNT_ID
    assert database.line_inserts[0][1][5:] == (Decimal("125.00"), Decimal("0"), "supplier_payment")
    assert database.line_inserts[1][1][5:] == (Decimal("0"), Decimal("125.00"), "supplier_payment")
    assert len(database.audit_inserts) == 1


def test_existing_supplier_payment_journal_is_linked_without_duplicate(monkeypatch):
    database = FakeDatabase(existing_journal={
        "id": JOURNAL_ID,
        "status": "posted",
        "source_type": "settlement",
        "source_id": SETTLEMENT_ID,
    })
    payload = accounting_routes.SupplierPaymentPostingInput()
    _configure(monkeypatch, database, payload)

    result = accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert result["id"] == str(JOURNAL_ID)
    assert result["idempotent"] is True
    assert database.journal_inserts == []
    assert database.line_inserts == []
    assert database.audit_inserts == []


def test_supplier_payment_requires_a_confirmed_bill_match(monkeypatch):
    database = FakeDatabase(confirmed_match=False)
    payload = _configure(monkeypatch, database)

    with pytest.raises(HTTPException) as error:
        accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert error.value.status_code == 409
    assert "Confirm a reconciliation match" in error.value.detail
    assert database.journal_inserts == []
    assert database.line_inserts == []


def test_non_myr_supplier_payment_is_not_posted_without_conversion(monkeypatch):
    database = FakeDatabase(currency="USD")
    payload = _configure(monkeypatch, database)

    with pytest.raises(HTTPException) as error:
        accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert error.value.status_code == 409
    assert "conversion is not configured" in error.value.detail
    assert database.journal_inserts == []


def test_supplier_payment_must_clear_the_payable_account_the_bill_credited(monkeypatch):
    other_payable = UUID("10000000-0000-0000-0000-000000000010")
    database = FakeDatabase(bill_payable=other_payable)
    payload = _configure(monkeypatch, database)

    with pytest.raises(HTTPException) as error:
        accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert error.value.status_code == 422
    assert "payable account this supplier bill was posted to" in error.value.detail
    assert database.journal_inserts == []


REVERSED = {"id": JOURNAL_ID, "status": "reversed", "source_type": "settlement", "source_id": SETTLEMENT_ID,
            "idempotency_key": f"supplier-payment:{SETTLEMENT_ID}"}


def test_a_plain_retry_after_a_reversal_reports_the_reversed_journal_and_posts_nothing(monkeypatch):
    database = FakeDatabase(existing_journal=REVERSED)
    payload = _configure(monkeypatch, database)

    result = accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert (result["id"], result["status"], result["idempotent"]) == (str(JOURNAL_ID), "reversed", True)
    assert database.journal_inserts == []


def test_an_explicit_correction_posts_a_new_payment_after_its_journal_was_reversed(monkeypatch):
    database = FakeDatabase(existing_journal=REVERSED)
    payload = _configure(monkeypatch, database).model_copy(update={"replacesJournalId": str(JOURNAL_ID)})

    result = accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert result["idempotent"] is False
    assert database.journal_inserts[0][1][2] == f"supplier-payment:{SETTLEMENT_ID}:after:{JOURNAL_ID}"


def test_a_reversed_manual_settlement_journal_does_not_hide_the_first_payment(monkeypatch):
    database = FakeDatabase(existing_journal={"id": JOURNAL_ID, "status": "reversed", "source_type": "settlement",
                                              "source_id": SETTLEMENT_ID, "idempotency_key": "manual:settlement-fix"})
    payload = _configure(monkeypatch, database)

    result = accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert result["idempotent"] is False
    assert database.journal_inserts[0][1][2] == f"supplier-payment:{SETTLEMENT_ID}"


def test_a_repeated_correction_returns_the_correction_it_already_posted(monkeypatch):
    correction = UUID("10000000-0000-0000-0000-000000000011")
    database = FakeDatabase(existing_journal={"id": correction, "status": "posted", "source_type": "settlement",
                                              "source_id": SETTLEMENT_ID,
                                              "idempotency_key": f"supplier-payment:{SETTLEMENT_ID}:after:{JOURNAL_ID}"})
    payload = _configure(monkeypatch, database).model_copy(update={"replacesJournalId": str(JOURNAL_ID)})

    result = accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert (result["id"], result["idempotent"]) == (str(correction), True)
    assert database.journal_inserts == []


def test_a_payment_against_a_receipt_is_refused(monkeypatch):
    database = FakeDatabase(kind="receipt")
    payload = _configure(monkeypatch, database)

    with pytest.raises(HTTPException) as error:
        accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert error.value.status_code == 422
    assert "invoice or bill" in error.value.detail
    assert database.journal_inserts == []


def test_an_existing_journal_that_is_not_this_payment_is_not_reported_as_success(monkeypatch):
    database = FakeDatabase(existing_journal={"id": JOURNAL_ID, "status": "posted", "source_type": "settlement",
                                              "source_id": SETTLEMENT_ID, "idempotency_key": f"supplier-payment:{SETTLEMENT_ID}"},
                            existing_lines=[{"account_type": "expense", "debit": Decimal("125.00"), "credit": Decimal("0")},
                                            {"account_type": "asset", "debit": Decimal("0"), "credit": Decimal("125.00")}])
    payload = accounting_routes.SupplierPaymentPostingInput()
    _configure(monkeypatch, database, payload)

    with pytest.raises(HTTPException) as error:
        accounting_routes.post_supplier_payment(str(TRANSACTION_ID), payload, None)

    assert error.value.status_code == 409
    assert "does not debit payables" in error.value.detail


def test_adjustment_amount_precision_is_a_request_validation_error():
    with pytest.raises(ValueError):
        accounting_routes.AdjustmentPostingInput(periodId="p", debitAccountId="d", creditAccountId="c",
                                                 amount=Decimal("10.005"), journalDate=date(2026, 5, 10),
                                                 adjustmentKind="credit_note")
