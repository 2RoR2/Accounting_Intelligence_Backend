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
    def __init__(self, existing_journal=None, confirmed_match=True, currency="MYR"):
        self.existing_journal = existing_journal
        self.confirmed_match = confirmed_match
        self.currency = currency
        self.journal_inserts = []
        self.line_inserts = []

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
        if "FROM journal_headers" in sql:
            return Cursor([self.existing_journal] if self.existing_journal else [])
        if "FROM reconciliation_candidates c" in sql:
            if not self.confirmed_match:
                return Cursor()
            return Cursor({
                "record_id": RECORD_ID,
                "category": "accounts_payable",
                "total_amount": Decimal("125.00"),
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
        raise AssertionError(f"Unexpected SQL: {sql}")


def _configure(monkeypatch, database, payload=None):
    user = {"tenant_id": TENANT_ID, "id": USER_ID}
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
    assert "VALUES (%s,%s,%s,1,%s,%s,0,'supplier_payment')" in database.line_inserts[0][0]
    assert "VALUES (%s,%s,%s,2,%s,0,%s,'supplier_payment')" in database.line_inserts[1][0]
    assert database.line_inserts[0][1][-1] == database.line_inserts[1][1][-1] == Decimal("125.00")


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
