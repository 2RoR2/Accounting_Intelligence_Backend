"""The owner's business summary must agree with the existing reports on the KNT demo books."""
import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

import main  # noqa: F401  (registers the routes before the report modules import their helpers)
from ai.synthetic.knt_pack import MONTHS
from database.schema_compatibility import apply_migrations
from services.admin.demo_company import _remote_destination
from services.admin.knt_reset import reset
from services.admin.knt_seed import locked_database
from services.admin.knt_tally import tally
from services.routes.reporting import business_summary, financial

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
ZERO = Decimal("0.00")


@pytest.fixture(scope="module")
def knt(tmp_path_factory):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a disposable loopback TEST_DATABASE_URL")
    if _remote_destination(url, os.environ):
        pytest.fail("KNT tests require a disposable loopback PostgreSQL database")
    name = "test_summary_" + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = urlsplit(url)._replace(path="/" + name).geturl()
    patch = pytest.MonkeyPatch()
    for key, value in (("DATABASE_URL", target), ("APP_ENV", "development"), ("S3_BUCKET", ""), ("DB_POOL_SIZE", "0"),
                       ("AUTH_SECRET", "synthetic-auth-secret-for-business-summary-tests"),
                       ("DOCUMENT_STORAGE_PATH", str(tmp_path_factory.mktemp("objects")))):
        patch.setenv(key, value)
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / "database/PostgreSQL Database schema.sql", ROOT / "database/migrations")
        with locked_database() as connect:
            reset(connect, tmp_path_factory.mktemp("pack"), "FixtureDemo!2026")
            with connect() as db:
                tenant = db.execute("SELECT id FROM tenants WHERE tenant_code='KNT'").fetchone()["id"]
                yield connect, tenant, tally(db)
    finally:
        patch.undo()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _summary(connect, tenant, cutoff, day=None):
    with connect() as db:
        return business_summary.build_summary(db, tenant, start=cutoff.replace(day=1), end=cutoff, day=day)


@pytest.mark.parametrize("month", list(MONTHS))
def test_totals_equal_profit_and_loss_balance_sheet_ageing_and_tally(knt, month):
    connect, tenant, tallies = knt
    cutoff = MONTHS[month]
    start = cutoff.replace(day=1)
    expected = next(row for row in tallies if row[0] == cutoff)
    summary = _summary(connect, tenant, cutoff, day=cutoff)
    result = summary["month"]
    with connect() as db:
        pnl = financial._profit_and_loss(db, tenant, start, cutoff)
        balance = financial._balance_sheet(db, tenant, cutoff)
        receivables = financial._ageing(db, tenant, cutoff, "receivables")
        payables = financial._ageing(db, tenant, cutoff, "payables")
        tax = financial._tax_summary(db, tenant, start, cutoff)
        cash = business_summary._cash_accounts(db, tenant)
        before = business_summary._position(db, tenant, start - timedelta(days=1), cash)[0]["bank"]
    # Profit and loss: the same figures, and the net profit tally checks against the posted documents.
    assert (result["sales"], result["expenses"], result["netProfit"]) == (
        pnl["totalIncome"], pnl["totalExpenses"], pnl["netProfit"])
    assert result["netProfit"] == expected[2]
    assert result["grossProfit"] == result["sales"] - result["costOfSales"]
    assert result["operatingExpenses"] == result["expenses"] - result["costOfSales"]
    assert result["costOfSalesTracked"] is True and result["costOfSales"] > ZERO
    # Owed to us / by us: the ageing totals (tally's AR and AP), split into overdue and not yet due.
    position = result["position"]
    assert position["receivables"]["total"] == receivables["totals"]["total"] == expected[3]
    assert position["payables"]["total"] == payables["totals"]["total"] == expected[4]
    for key, report in (("receivables", receivables), ("payables", payables)):
        owed = position[key]
        assert owed["overdue"] + report["totals"]["current"] == owed["total"]
        assert owed["openCount"] == len(report["rows"])
        # Every KNT receipt and payment is allocated, so the open items and the control account agree.
        assert owed["openItems"] == report["totals"]["total"] and owed["unallocated"] == ZERO
        assert sum(owed["buckets"].values()) == owed["total"]
    # Bank balance per account: the balance sheet's own cash and bank accounts.
    cash_ids = {str(row["account_id"]) for row in cash}
    sheet_cash = {row["accountId"]: row["amount"] for section in balance["sections"] if section["key"] == "assets"
                  for row in section["accounts"] if row["accountId"] in cash_ids}
    assert {a["accountId"]: a["balance"] for a in position["bank"]["accounts"]} == sheet_cash
    assert position["bank"]["total"] == sum(sheet_cash.values(), ZERO)
    assert next(a["balance"] for a in position["bank"]["accounts"] if a["code"] == "1010") == expected[5]
    # Cash in and out explain the movement of the cash and bank balances over the month.
    assert result["cashIn"] > ZERO and result["cashOut"] > ZERO
    assert result["cashIn"] - result["cashOut"] == position["bank"]["total"] - before["total"]
    # Recorded tax is the existing tax summary and never enters profit.
    assert result["recordedTax"] == {"outputTax": tax["totals"]["outputTax"], "inputTax": tax["totals"]["inputTax"],
                                     "net": tax["totals"]["net"], "note": "Recorded tax, not a tax return"}
    assert summary["profitNote"] == "Profit figures exclude recorded tax."
    # Every breakdown adds up to its headline figure.
    assert sum(r["amount"] for r in summary["salesByCustomer"]) == result["sales"]
    assert sum(r["amount"] for r in summary["purchasesBySupplier"]) == result["expenses"]
    assert sum(r["amount"] for r in summary["salesByProduct"]) == result["sales"]
    assert [r["name"] for r in summary["topCustomers"]] == [r["name"] for r in summary["salesByCustomer"] if r["named"]][:5]
    assert len(summary["topSuppliers"]) <= 5 and all(r["named"] for r in summary["topSuppliers"])
    assert result["sales"] > ZERO and summary["salesByCustomer"][0]["named"]
    # Overdue lists never show an item that is not overdue and stay within the top 10.
    for rows, report in ((summary["overdueInvoices"], receivables), (summary["overdueBills"], payables)):
        overdue = {r["documentNumber"] for r in report["rows"] if r["bucket"] != "current"}
        assert len(rows) <= 10 and {r["documentNumber"] for r in rows} <= overdue
        assert all(r["daysOverdue"] > 0 for r in rows)


def test_six_month_trend_matches_each_months_profit_and_loss(knt):
    connect, tenant, _ = knt
    summary = _summary(connect, tenant, MONTHS["2026-11"])
    assert [row["month"] for row in summary["trend"]] == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]
    with connect() as db:
        for row in summary["trend"]:
            pnl = financial._profit_and_loss(db, tenant, row["fromDate"], row["toDate"])
            assert (row["sales"], row["expenses"], row["netProfit"]) == (
                pnl["totalIncome"], pnl["totalExpenses"], pnl["netProfit"])
    assert summary["trend"][-1]["netProfit"] == summary["month"]["netProfit"]
    assert [row["netProfit"] for row in summary["trend"][3:]] == [Decimal("5571.30"), Decimal("11670.00"), Decimal("12269.30")]


def test_today_is_the_single_day_and_a_quiet_day_is_zero(knt):
    connect, tenant, _ = knt
    cutoff = MONTHS["2026-11"]
    with connect() as db:
        day = db.execute("""SELECT j.journal_date FROM journal_headers j JOIN journal_lines l ON l.journal_id=j.id
            JOIN ledger_accounts a ON a.id=l.account_id WHERE j.tenant_id=%s AND a.account_type='income'
            AND j.journal_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 1 LIMIT 1""",
                         (tenant, cutoff.replace(day=1), cutoff)).fetchone()["journal_date"]
        expected = financial._profit_and_loss(db, tenant, day, day)
    summary = _summary(connect, tenant, cutoff, day=day)
    assert summary["today"]["date"] == day
    assert (summary["today"]["sales"], summary["today"]["netProfit"]) == (expected["totalIncome"], expected["netProfit"])
    assert ZERO < summary["today"]["sales"] <= summary["month"]["sales"]
    assert summary["today"]["position"]["asOf"] == day
    quiet = _summary(connect, tenant, cutoff, day=date(2026, 8, 15))["today"]
    assert (quiet["sales"], quiet["expenses"], quiet["netProfit"], quiet["cashIn"], quiet["cashOut"]) == (ZERO,) * 5
    assert quiet["costOfSalesTracked"] is True
