"""Insights overview, commentary and the pack's Commentary sheet on the KNT demo books (disposable local PostgreSQL)."""
import os
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from psycopg import sql
from psycopg.rows import dict_row

import main
from ai.synthetic.knt_pack import MONTHS
from database.schema_compatibility import apply_migrations
from security.authentication import create_access_token
from services.admin.demo_company import _remote_destination
from services.admin.knt_reset import reset
from services.admin.knt_seed import locked_database
from services.extraction.ai.text_llm import TextModelError
from services.insights import commentary
from services.routes.reporting import business_summary, financial

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
ZERO = Decimal("0.00")
OVERVIEW = "/api/insights/overview"
COMMENTARY = "/api/insights/commentary"
PACK = "/api/reports/month-pack"
AI_ANSWER = {"paragraphs": [
    {"template": "Sales for {{f1}} were {{f3}}, compared with {{f4}} in {{f2}}.", "factIds": ["f3", "f4"]},
    {"template": "Expenses were {{f8}} against {{f9}}.", "factIds": ["f8", "f9"]},
    {"template": "The net result was {{f13}}.", "factIds": ["f13"]}]}


@pytest.fixture(scope="module")
def knt(tmp_path_factory):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a disposable loopback TEST_DATABASE_URL")
    if _remote_destination(url, os.environ):
        pytest.fail("KNT tests require a disposable loopback PostgreSQL database")
    name = "test_insights_" + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = urlsplit(url)._replace(path="/" + name).geturl()
    patch = pytest.MonkeyPatch()
    for key, value in (("DATABASE_URL", target), ("APP_ENV", "development"), ("S3_BUCKET", ""), ("DB_POOL_SIZE", "0"),
                       ("AUTH_SECRET", "synthetic-auth-secret-for-insights-tests-" + uuid4().hex),
                       ("DOCUMENT_STORAGE_PATH", str(tmp_path_factory.mktemp("objects")))):
        patch.setenv(key, value)
    clients = []
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / "database/PostgreSQL Database schema.sql", ROOT / "database/migrations")
        with locked_database() as connect:
            reset(connect, tmp_path_factory.mktemp("pack"), "FixtureDemo!2026")
            with connect() as db:
                tenant = db.execute("SELECT id FROM tenants WHERE tenant_code='KNT'").fetchone()["id"]
                other = db.execute("""INSERT INTO tenants(name,registration_number,admin_email)
                    VALUES ('Empty Co.','EMPTY-1','empty@example.test') RETURNING id""").fetchone()["id"]
                db.execute("""INSERT INTO users(tenant_id,name,email,role,status)
                    VALUES (%s,'outsider','outsider@example.test','local_admin','active')""", (other,))
                users = {role: db.execute("SELECT * FROM users WHERE tenant_id=%s AND role=%s", (tenant, role)).fetchone()
                         for role in ("local_admin", "accountant")}
                users["outsider"] = db.execute("SELECT * FROM users WHERE tenant_id=%s", (other,)).fetchone()
                for user in users.values():
                    session_id, _, _ = main.issue_session(db, user)
                    token = create_access_token(str(user["id"]), session_id)
                    client = TestClient(main.app, cookies={main.ACCESS: token},
                                        headers={"X-Requested-With": "AccountingIntelligence"})
                    clients.append(client)
                    user["client"] = client
            yield connect, tenant, users
    finally:
        for client in clients:
            client.close()
        patch.undo()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def ai_off(monkeypatch):
    monkeypatch.setenv("AI_ASSIST", "off")
    commentary.clear_cache()
    yield
    commentary.clear_cache()


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setenv("AI_ASSIST", "on")
    commentary.clear_cache()
    calls = []

    def model(system, user):
        calls.append(user)
        return AI_ANSWER, {"provider": "custom", "model": "openai/gpt-oss-20b", "prompt_tokens": 800,
                           "completion_tokens": 100}
    monkeypatch.setattr(commentary, "call_model", model)
    yield calls
    commentary.clear_cache()


def admin_client(knt):
    return knt[2]["local_admin"]["client"]


def _bucket_of(due, cutoff):
    if due is None:
        return "none"
    days = (due - cutoff).days
    return "overdue" if days <= 0 else "d1_30" if days <= 30 else "d31_60" if days <= 60 else "d61_90" if days <= 90 else "later"


# ------------------------------------------------------------------------------------------ overview

@pytest.mark.parametrize("month", list(MONTHS))
def test_overview_agrees_with_the_reports_it_reads(knt, month, ai_off):
    connect, tenant, _ = knt
    start = MONTHS[month].replace(day=1)
    response = admin_client(knt).get(OVERVIEW, params={"period": month})
    assert response.status_code == 200, response.text
    body = response.json()
    # The figures run to the end of the month, or to today for the month in progress.
    cutoff = min(date(start.year + start.month // 12, start.month % 12 + 1, 1) - timedelta(days=1), date.today())
    assert (body["period"], body["fromDate"], body["toDate"], body["asOf"], body["currency"]) == (
        month, start.isoformat(), cutoff.isoformat(), cutoff.isoformat(), "MYR")
    with connect() as db:
        summary = business_summary.build_summary(db, tenant, start=start, end=cutoff)
        pnl = financial._profit_and_loss(db, tenant, start, cutoff)
        ageing = {kind: financial._ageing(db, tenant, cutoff, kind) for kind in ("receivables", "payables")}
        trend_pnl = [financial._profit_and_loss(db, tenant, m["fromDate"], m["toDate"])
                     for m in business_summary._trend(db, tenant, start, cutoff)]

    forecast = body["forecast"]
    assert forecast["kind"] == "projection" and "recorded due dates" in forecast["basis"]
    opening = summary["month"]["position"]["bank"]["total"]
    assert forecast["openingBalance"] == f"{opening:.2f}"
    # Each bucket is the sum of the open items due in it, re-derived here from the ageing rows.
    expected = {}
    for kind in ("receivables", "payables"):
        for row in ageing[kind]["rows"]:
            slot = expected.setdefault(_bucket_of(row["dueDate"], cutoff), {"receivables": ZERO, "payables": ZERO})
            slot[kind] += row["outstanding"]
    for bucket in forecast["buckets"]:
        want = expected.get(bucket["key"], {"receivables": ZERO, "payables": ZERO})
        assert (bucket["receivables"], bucket["payables"]) == (f"{want['receivables']:.2f}", f"{want['payables']:.2f}"), bucket
    undated = expected.get("none", {"receivables": ZERO, "payables": ZERO})
    assert forecast["noDueDate"]["receivables"] == f"{undated['receivables']:.2f}"
    assert forecast["totals"]["openReceivables"] == f"{ageing['receivables']['totals']['total']:.2f}"
    assert forecast["totals"]["openPayables"] == f"{ageing['payables']['totals']['total']:.2f}"
    for horizon, keys in zip(forecast["horizons"], (("overdue", "d1_30"), ("overdue", "d1_30", "d31_60"),
                                                    ("overdue", "d1_30", "d31_60", "d61_90"))):
        running_in = sum((expected.get(k, {"receivables": ZERO})["receivables"] for k in keys), ZERO)
        running_out = sum((expected.get(k, {"payables": ZERO})["payables"] for k in keys), ZERO)
        assert horizon["projectedBalance"] == f"{opening + running_in - running_out:.2f}"
        assert horizon["date"] == (cutoff + timedelta(days=horizon["days"])).isoformat()

    suppliers, categories = body["spendBySupplier"], body["spendByCategory"]
    assert suppliers["total"] == categories["total"] == f"{pnl['totalExpenses']:.2f}"
    assert sum(Decimal(i["amount"]) for i in suppliers["items"]) == pnl["totalExpenses"]
    assert sum(Decimal(i["amount"]) for i in categories["items"]) == pnl["totalExpenses"]
    assert len([i for i in suppliers["items"] if not i["isOther"]]) <= 10 and len(categories["items"]) <= 11

    months = body["trend"]["months"]
    assert len(months) == 6 and months[-1]["month"] == month
    for entry, report in zip(months, trend_pnl):
        assert (entry["revenue"], entry["expenses"], entry["net"]) == (
            f"{report['totalIncome']:.2f}", f"{report['totalExpenses']:.2f}", f"{report['netProfit']:.2f}")
    assert all(a["reason"] and a["kind"] in ("month", "supplier") and a["id"] for a in body["anomalies"])
    assert body["anomalyMethod"] and "generatedAt" in body


def test_overview_flags_a_dominant_supplier_with_a_reason_on_the_demo_books(knt, ai_off):
    body = admin_client(knt).get(OVERVIEW, params={"period": "2026-10"}).json()
    top = next(a for a in body["anomalies"] if a["subject"].startswith("Seladah"))
    assert top["direction"] == "high" and top["basis"] == "peer_group" and "RM 13,440.00" in top["reason"]


def test_overview_works_with_ai_off_and_defaults_to_the_current_month(knt, ai_off):
    response = admin_client(knt).get(OVERVIEW)
    assert response.status_code == 200, response.text
    assert response.json()["period"] == date.today().strftime("%Y-%m")
    assert response.json()["forecast"]["kind"] == "projection"


@pytest.mark.parametrize("period", ["2026-13", "nov", "2026-1", "2026-11-01", "2099-01"])
def test_overview_and_commentary_reject_a_bad_or_future_period(knt, period, ai_off):
    for path in (OVERVIEW, COMMENTARY):
        response = admin_client(knt).get(path, params={"period": period})
        assert response.status_code == 422, (path, response.text)


def test_overview_and_commentary_need_a_signed_in_workspace_user_and_stay_in_their_workspace(knt, ai_off):
    anonymous = TestClient(main.app, headers={"X-Requested-With": "AccountingIntelligence"})
    for path in (OVERVIEW, COMMENTARY):
        assert anonymous.get(path, params={"period": "2026-10"}).status_code in (401, 403)
        assert knt[2]["accountant"]["client"].get(path, params={"period": "2026-10"}).status_code == 200
    # Another workspace sees none of KNT's books.
    empty = knt[2]["outsider"]["client"].get(OVERVIEW, params={"period": "2026-10"}).json()
    assert empty["spendBySupplier"] == {"total": "0.00", "items": []}
    assert empty["forecast"]["openingBalance"] == "0.00" and empty["anomalies"] == []
    assert empty["forecast"]["totals"] == {"openReceivables": "0.00", "openPayables": "0.00"}
    text = knt[2]["outsider"]["client"].get(COMMENTARY, params={"period": "2026-10"}).json()
    assert "Seladah" not in str(text) and text["fallback"] is True


# ------------------------------------------------------------------------------------------ commentary

def _fact(body, label):
    return next(f for f in body["facts"] if f["label"] == label)


def test_commentary_with_ai_off_is_a_labelled_template_built_from_database_figures(knt, ai_off, monkeypatch):
    monkeypatch.setattr(commentary, "call_model", lambda *a: pytest.fail("AI is off: no model call"))
    connect, tenant, _ = knt
    response = admin_client(knt).get(COMMENTARY, params={"period": "2026-10"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fallback"] is True and body["fallbackReason"] == "ai_disabled" and "AI assistant is off" in body["label"]
    assert (body["period"], body["partialPeriod"], body["cached"]) == ("2026-10", False, False)
    with connect() as db:
        october = financial._profit_and_loss(db, tenant, date(2026, 10, 1), date(2026, 10, 31))
        september = financial._profit_and_loss(db, tenant, date(2026, 9, 1), date(2026, 9, 30))
    assert _fact(body, "Sales this period")["value"] == f"{october['totalIncome']:.2f}"
    assert _fact(body, "Expenses this period")["value"] == f"{october['totalExpenses']:.2f}"
    assert _fact(body, "Sales in the comparison period")["value"] == f"{september['totalIncome']:.2f}"
    ids = {f["id"] for f in body["facts"]}
    text = " ".join(p["text"] for p in body["paragraphs"])
    assert f"RM {october['totalIncome']:,.2f}" in text and "{{" not in text and "tax" not in text.lower()
    assert all(set(p["factIds"]) <= ids and p["factIds"] for p in body["paragraphs"])
    assert all(f["href"] is None or f["href"].startswith("/reports/") for f in body["facts"])


def test_commentary_with_ai_on_fills_placeholders_from_the_fact_table_and_caches(knt, ai_on):
    connect, tenant, _ = knt
    first = admin_client(knt).get(COMMENTARY, params={"period": "2026-10"}).json()
    assert first["fallback"] is False and first["fallbackReason"] is None and first["label"] == commentary.LABEL_AI
    sales = _fact(first, "Sales this period")["display"]
    assert first["paragraphs"][0]["text"] == (
        f"Sales for October 2026 were {sales}, compared with {_fact(first, 'Sales in the comparison period')['display']} "
        f"in September 2026.")
    assert first["paragraphs"][0]["factIds"] == ["f1", "f3", "f4", "f2"]
    again = admin_client(knt).get(COMMENTARY, params={"period": "2026-10"}).json()
    assert again["cached"] is True and again["paragraphs"] == first["paragraphs"] and len(ai_on) == 1
    # The prompt holds facts as data, with no tenant, user or SQL parameter.
    assert "Seladah" in ai_on[0] and str(tenant) not in ai_on[0]
    with connect() as db:
        audit = db.execute("""SELECT action,resource,details FROM audit_logs WHERE tenant_id=%s AND action='AI_COMMENTARY'
            ORDER BY created_at DESC LIMIT 1""", (tenant,)).fetchone()
    assert audit["action"] == "AI_COMMENTARY" and "gpt-oss-20b" in audit["details"] and '"outcome": "ai"' in audit["details"]


def test_commentary_falls_back_when_the_provider_is_down(knt, monkeypatch):
    monkeypatch.setenv("AI_ASSIST", "on")
    commentary.clear_cache()

    def down(system, user):
        raise TextModelError("AI_UNREACHABLE")
    monkeypatch.setattr(commentary, "call_model", down)
    body = admin_client(knt).get(COMMENTARY, params={"period": "2026-09"}).json()
    assert body["fallback"] is True and body["fallbackReason"] == "provider_unavailable" and body["paragraphs"]
    commentary.clear_cache()


def test_commentary_for_the_month_to_date_compares_the_same_days_of_last_month(knt, ai_off):
    body = admin_client(knt).get(COMMENTARY, params={"period": "2026-11"}).json()
    assert body["period"] == "2026-11" and body["facts"]
    if body["partialPeriod"]:
        assert _fact(body, "Reporting period")["display"] == "November 2026 so far"
        assert _fact(body, "Comparison period")["display"] == "October 2026 (same days)"


# ------------------------------------------------------------------------------------------ month pack

PACK_SHEETS = ["Business summary", "Commentary", "Profit and loss", "Balance sheet", "Trial balance", "General ledger",
               "Receivables ageing", "Payables ageing", "Tax summary", "Open exceptions"]


def _expected_other_sheets(connect, tenant, start, end):
    with connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        company = financial._company_name(db, tenant)
        sheets = [business_summary.month_pack_summary_sheet(db, tenant, start, end),
                  financial._profit_and_loss_sheet(financial._profit_and_loss(db, tenant, start, end)),
                  financial._balance_sheet_sheet(financial._balance_sheet(db, tenant, end)),
                  financial._trial_balance_sheet(db, tenant, end),
                  financial._general_ledger_sheet(financial._general_ledger(db, tenant, start, end, None)),
                  financial._ageing_sheet(financial._ageing(db, tenant, end, "receivables")),
                  financial._ageing_sheet(financial._ageing(db, tenant, end, "payables")),
                  financial._tax_summary_sheet(financial._tax_summary(db, tenant, start, end)),
                  financial._exceptions_sheet(db, tenant, start, end)]
    return load_workbook(BytesIO(financial._xlsx_export(company, sheets, "x.xlsx").body), data_only=True)


def test_month_pack_has_a_commentary_sheet_and_every_other_sheet_is_unchanged(knt, ai_on):
    connect, tenant, _ = knt
    response = admin_client(knt).get(PACK, params={"period": "2026-10"})
    assert response.status_code == 200, response.text
    book = load_workbook(BytesIO(response.content), data_only=True)
    assert book.sheetnames == PACK_SHEETS
    expected = _expected_other_sheets(connect, tenant, date(2026, 10, 1), date(2026, 10, 31))
    for name in expected.sheetnames:
        assert [tuple(row) for row in book[name].values] == [tuple(row) for row in expected[name].values], name
    rows = [tuple(row) for row in book["Commentary"].values]
    assert rows[0][0] == "Ketapang Nibong Trading Sdn. Bhd. | Commentary | 2026-10-01 to 2026-10-31"
    assert rows[1][0].startswith(commentary.LABEL_AI) and "no tax is calculated" in rows[1][0]
    assert rows[2] == ("Section", "Item", "Value", "Facts")
    paragraphs = [row for row in rows if row[0] == "Commentary"]
    facts = [row for row in rows if row[0] == "Facts"]
    api = admin_client(knt).get(COMMENTARY, params={"period": "2026-10"}).json()
    assert [row[2] for row in paragraphs] == [p["text"] for p in api["paragraphs"]]
    assert len(facts) == len(api["facts"]) and len(ai_on) == 1  # the pack and the API share one cached commentary
    sales = next(row for row in facts if row[1].endswith("Sales this period"))
    assert Decimal(str(sales[2])) == Decimal(_fact(api, "Sales this period")["value"])


def test_month_pack_commentary_with_ai_off_is_the_labelled_template(knt, ai_off):
    book = load_workbook(BytesIO(admin_client(knt).get(PACK, params={"period": "2026-09"}).content), data_only=True)
    rows = [tuple(row) for row in book["Commentary"].values]
    assert "AI assistant is off" in rows[1][0]
    assert [row for row in rows if row[0] == "Commentary"] and [row for row in rows if row[0] == "Facts"]


def test_month_pack_never_fails_because_of_the_commentary(knt, ai_off, monkeypatch):
    def broken(db, tenant, start, end):
        db.execute("SELECT 1/0")  # a failed query must not abort the pack's own snapshot
    monkeypatch.setattr(commentary, "build_facts", broken)
    response = admin_client(knt).get(PACK, params={"period": "2026-10"})
    assert response.status_code == 200, response.text
    book = load_workbook(BytesIO(response.content), data_only=True)
    assert book.sheetnames == PACK_SHEETS
    assert book["Commentary"]["A3"].value == "Section" and "unavailable" in book["Commentary"]["C4"].value
    assert book["Profit and loss"].max_row > 2


# Posted ledger history is append-only, so this test posts a journal it cannot remove: keep it last.
def test_a_newly_posted_record_changes_the_data_version_so_the_commentary_is_regenerated(knt, ai_on):
    connect, tenant, _ = knt
    client = admin_client(knt)
    client.get(COMMENTARY, params={"period": "2026-09"})
    client.get(COMMENTARY, params={"period": "2026-09"})
    assert len(ai_on) == 1
    with connect() as db:
        period = db.execute("SELECT id FROM accounting_periods WHERE tenant_id=%s AND period_code='2026-09'",
                            (tenant,)).fetchone()["id"]
        accounts = {r["code"]: r["id"] for r in db.execute(
            "SELECT id,code FROM ledger_accounts WHERE tenant_id=%s AND code IN ('6140','1010')", (tenant,))}
        journal = db.execute("""INSERT INTO journal_headers(tenant_id,period_id,idempotency_key,source_type,status,
                journal_date,description,posted_at) VALUES (%s,%s,%s,'manual','posted','2026-09-15','Insights test',now())
            RETURNING id""", (tenant, period, "insights-" + uuid4().hex)).fetchone()["id"]
        for number, (code, debit, credit) in enumerate((("6140", 100, 0), ("1010", 0, 100)), start=1):
            db.execute("""INSERT INTO journal_lines(journal_id,tenant_id,account_id,line_number,debit,credit)
                VALUES (%s,%s,%s,%s,%s,%s)""", (journal, tenant, accounts[code], number, debit, credit))
    changed = client.get(COMMENTARY, params={"period": "2026-09"}).json()
    assert changed["cached"] is False and len(ai_on) == 2
