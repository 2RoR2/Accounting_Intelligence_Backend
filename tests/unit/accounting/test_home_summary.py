"""Today page summary (plan v2 B3) without a database: roles, scope, null-with-reason and money format.

The SQL itself runs against the full schema in tests/integration/test_home_summary_postgres.py.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import main  # noqa: F401 - initialise the normal route registry first.
from services.routes.documents import routes as documents
from services.routes.home import lanes
from services.routes.home import routes as home

TENANT = "11111111-1111-4111-8111-111111111111"
ACCOUNTANT = {"id": "22222222-2222-4222-8222-222222222222", "tenant_id": TENANT, "role": "accountant"}
ADMIN = {"id": "33333333-3333-4333-8333-333333333333", "tenant_id": TENANT, "role": "local_admin"}
TODAY = date(2026, 11, 16)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeDb:
    """Answers each summary query by a marker in its SQL and records every query with its parameters."""

    def __init__(self, tables=True, reconciliation_periods=0):
        self.tables = tables
        self.reconciliation_periods = reconciliation_periods
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.queries.append((sql, list(params)))
        if "to_regclass" in sql:
            return Result([{"ok": self.tables}])
        if "AT TIME ZONE" in sql:
            return Result([{"today": TODAY}])
        if "AS to_review" in sql:
            return Result([{"to_review": 5, "ready": 2, "needs_fixing": 3, "waiting_approval": 1}])
        if "regexp_replace(e.type" in sql:
            return Result([{"code": "EX-008", "documents": 2}, {"code": "TOTAL", "documents": 1}])
        if "AS money_in" in sql:
            return Result([{"lines": 4, "money_in": 1, "money_out": 3}])
        if "AS soon_count" in sql:
            return Result([{"soon_count": 2, "soon_amount": Decimal("1250.5"), "soon_other": 1,
                            "late_count": 1, "late_amount": Decimal("99"), "late_other": 0, "no_due_date": 1}])
        if "AS drafts" in sql:
            return Result([{"records": 6, "drafts": 0}])
        if "FROM accounting_periods" in sql:
            return Result([])
        if "FROM bank_reconciliation_periods" in sql:
            return Result([{"total": self.reconciliation_periods, "done": 0}])
        if "AS total" in sql:
            return Result([{"total": 10, "done": 4}])
        raise AssertionError("Unexpected query: " + sql)


def summary(user, db, **kwargs):
    with patch.object(home, "require_workspace_user", return_value=user), patch.object(home, "connect", return_value=db):
        return home.home_summary(None, **kwargs)


def test_only_accountants_and_local_admins_can_read_the_summary():
    super_admin = {"id": "44444444-4444-4444-8444-444444444444", "tenant_id": None, "role": "super_admin"}
    with patch.object(main, "session_user", return_value=super_admin), patch.object(main, "connect", return_value=FakeDb()):
        with pytest.raises(HTTPException) as denied:
            home.home_summary(None)
    assert denied.value.status_code == 403
    assert home.ROLES == {"accountant", "local_admin"}


def test_accountant_document_counts_use_the_ownership_rule():
    db = FakeDb()
    summary(ACCOUNTANT, db)
    document_queries = [(sql, params) for sql, params in db.queries if "AS to_review" in sql or "regexp_replace(e.type" in sql]
    assert len(document_queries) == 2
    for sql, params in document_queries:
        assert lanes.ACCOUNTANT_DOCUMENT_VISIBILITY in sql
        assert params[:3] == [TENANT, ACCOUNTANT["id"], ACCOUNTANT["id"]]
    # The month-end checklist is the company's, like the bank, records and ledger figures.
    month_end = [sql for sql, _ in db.queries if "uploaded_at::date BETWEEN" in sql]
    assert month_end and lanes.ACCOUNTANT_DOCUMENT_VISIBILITY not in month_end[0]


def test_local_admin_sees_every_company_document():
    db = FakeDb()
    result = summary(ADMIN, db)
    assert not any(lanes.ACCOUNTANT_DOCUMENT_VISIBILITY in sql for sql, _ in db.queries)
    assert result["tiles"]["waitingApproval"]["link"] == {"page": "approvals", "query": None}


def test_every_data_query_is_limited_to_the_users_company():
    db = FakeDb(reconciliation_periods=1)
    summary(ACCOUNTANT, db)
    data_queries = [(sql, params) for sql, params in db.queries if "to_regclass" not in sql]
    assert data_queries
    for sql, params in data_queries:
        assert "tenant_id=%s" in sql
        assert TENANT in params


def test_tiles_link_to_the_lists_they_count_and_carry_the_top_reasons():
    result = summary(ACCOUNTANT, FakeDb(), asOf=TODAY)
    tiles = result["tiles"]
    assert result["asOf"] == "2026-11-16" and result["currency"] == "MYR"
    assert tiles["toReview"] == {"available": True, "scope": "yours", "count": 5,
                                 "link": {"page": "documents", "query": "lane=to-review"}}
    assert tiles["readyToApprove"]["link"]["query"] == "lane=ready"
    assert tiles["needsFixing"]["link"] == {"page": "exceptions", "query": "show=open"}
    assert tiles["needsFixing"]["topReasons"] == [{"code": "EX-008", "documents": 2}, {"code": "TOTAL", "documents": 1}]
    assert tiles["waitingApproval"]["link"] == {"page": "documents", "query": "lane=waiting-approval"}
    assert tiles["billsDueSoon"]["link"]["query"] == "category=ACCOUNTS_PAYABLE&due=next-7-days"
    assert tiles["invoicesOverdue"]["link"]["query"] == "category=ACCOUNTS_RECEIVABLE&due=overdue"
    assert tiles["journalsToPost"]["link"] == {"page": "records", "query": "posting=to-post"}
    assert tiles["bankToMatch"]["scope"] == "company"
    for tile in tiles.values():  # Every lane the summary links to is accepted by the documents list.
        query = (tile["link"] or {}).get("query") or ""
        if query.startswith("lane="):
            assert query[5:] in lanes.DOCUMENT_LANES


def test_amounts_are_decimal_strings_in_rm_with_other_currencies_counted_separately():
    tiles = summary(ADMIN, FakeDb())["tiles"]
    assert tiles["billsDueSoon"]["amount"] == "1250.50"
    assert tiles["billsDueSoon"]["otherCurrencyCount"] == 1
    assert tiles["billsDueSoon"]["count"] == 2 and tiles["billsDueSoon"]["days"] == 7
    assert tiles["billsOverdue"]["amount"] == "99.00"
    assert all(isinstance(t["amount"], str) for t in tiles.values() if "amount" in t)
    assert home._money(None) == "0.00" and home._money(Decimal("-3.456")) == "-3.46"


def test_missing_tables_give_null_with_a_reason_never_a_number():
    db = FakeDb(tables=False)
    result = summary(ADMIN, db)
    # The open-item query reads settlement_allocations, so it must not run at all without those tables.
    assert not any("settlement_allocations" in sql for sql, _ in db.queries)
    for key in ("bankToMatch", "journalsToPost", "billsDueSoon", "billsOverdue", "invoicesOverdue"):
        tile = result["tiles"][key]
        assert tile["available"] is False and tile["reason"] and "count" not in tile
    steps = {step["key"]: step for step in result["monthEnd"]["steps"]}
    assert steps["reviewed"]["available"] is True
    for key in ("matched", "posted", "reconciled"):
        assert steps[key]["available"] is False and steps[key]["reason"] and "done" not in steps[key]
    assert result["monthEnd"]["period"]["source"] == "calendar_month"


def test_month_end_reconciled_step_is_null_until_a_reconciliation_period_exists():
    without = {s["key"]: s for s in summary(ADMIN, FakeDb(reconciliation_periods=0))["monthEnd"]["steps"]}
    assert without["reconciled"]["available"] is False
    assert "reconciliation period" in without["reconciled"]["reason"]
    with_period = {s["key"]: s for s in summary(ADMIN, FakeDb(reconciliation_periods=2))["monthEnd"]["steps"]}
    assert (with_period["reconciled"]["available"], with_period["reconciled"]["done"], with_period["reconciled"]["total"]) == (True, 0, 2)


def test_calendar_month_period_covers_the_whole_month():
    db = FakeDb()
    period = home.current_period(db, ADMIN, date(2026, 2, 10))
    assert (period["startsOn"], period["endsOn"], period["code"]) == (date(2026, 2, 1), date(2026, 2, 28), "2026-02")
    assert home.current_period(db, ADMIN, date(2026, 12, 31))["endsOn"] == date(2026, 12, 31)


def test_document_visibility_matches_the_documents_list_rule():
    source = Path(documents.__file__).read_text(encoding="utf-8")
    assert lanes.ACCOUNTANT_DOCUMENT_VISIBILITY in source


def test_due_windows_use_one_today_parameter_per_placeholder():
    sql, params = lanes.due_window("next-7-days", TODAY)
    assert params == [TODAY, TODAY] and sql.count("%s") == 2
    base, base_params = lanes.open_item_base("accounts_payable")
    assert base_params == ["accounts_payable", ["invoice", "bill"]] and base.count("%s") == 2


def test_lists_reject_unknown_today_filters():
    with patch.object(documents, "require_workspace_user", return_value=ADMIN):
        with pytest.raises(HTTPException) as lane:
            documents.list_documents(None, lane="everything")
        with pytest.raises(HTTPException) as due:
            documents.list_records(None, due="overdue")  # A due list needs a category.
        with pytest.raises(HTTPException) as posting:
            documents.list_records(None, posting="all")
    assert lane.value.status_code == due.value.status_code == posting.value.status_code == 422
