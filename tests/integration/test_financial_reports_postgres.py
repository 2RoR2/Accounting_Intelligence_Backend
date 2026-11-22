"""Financial report and period APIs against a disposable loopback PostgreSQL database."""
import csv
import hashlib
import json
import os
from contextlib import contextmanager
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import main
from security.authentication import create_access_token
from services.routes.accounting import routes as accounting
from services.routes.reporting import financial

ROOT = Path(__file__).resolve().parents[2]
PERIODS = "/api/ledger/periods"
JOURNALS = "/api/ledger/journals"
REPORTS = "/api/reports/financial"
PACK = "/api/reports/month-pack"
AUGUST = {"fromDate": "2026-08-01", "toDate": "2026-08-31"}
AS_OF = {"asOf": AUGUST["toDate"]}


@pytest.fixture(scope="module")
def ledger():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires the disposable local TEST_DATABASE_URL")
    parts = urlsplit(url)
    assert parts.hostname in {"127.0.0.1", "localhost", "::1"}
    name = "financial_reports_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute("CREATE DATABASE " + name)
    target = parts._replace(path="/" + name).geturl()

    @contextmanager
    def connect():
        with psycopg.connect(target, row_factory=dict_row) as db:
            yield db

    try:
        schema = (ROOT / "database" / "PostgreSQL Database schema.sql").read_text(encoding="utf-8-sig")
        migration = (ROOT / "database" / "migrations" / "20261124_ledger_account_report_fields.sql").read_text(encoding="utf-8")
        assert migration.strip() in schema
        with connect() as db:
            db.execute(schema.replace(migration.strip(), "", 1))
            tenant = _company(db, "Financial Test Co.", "FIN-1")
            other = _company(db, "Other Test Co.", "FIN-2")
            admin = _user(db, tenant, "local_admin", "admin")
            accountant = _user(db, tenant, "accountant", "accountant")
            outsider = _user(db, other, "local_admin", "outsider")
            accounts = {code: db.execute("""INSERT INTO ledger_accounts(tenant_id,code,name,account_type)
                VALUES (%s,%s,%s,%s) RETURNING id""", (tenant, code, label, kind)).fetchone()["id"]
                for code, label, kind in (("1010", "Bank", "asset"), ("1160", "SST input", "asset"),
                                          ("2000", "Payables", "liability"), ("2080", "SST output", "liability"),
                                          ("3000", "Capital", "equity"), ("4000", "Sales", "income"),
                                          ("5000", "Supplies", "expense"))}
            db.execute(migration)
            db.execute(migration)
            bank = db.execute("SELECT statement,normal_balance FROM ledger_accounts WHERE id=%s",
                              (accounts["1010"],)).fetchone()
            assert bank == {"statement": "balance_sheet", "normal_balance": "debit"}
            db.execute("UPDATE ledger_accounts SET report_section='Trading income' WHERE id=%s", (accounts["4000"],))
            db.execute("""INSERT INTO erp_posting_accounts(tenant_id,purpose,account_id)
                VALUES (%s,'tax_output',%s),(%s,'tax_input',%s)""", (tenant, accounts["2080"], tenant, accounts["1160"]))
            _seed_open_items(db, tenant, admin["id"])
        yield connect, {"tenant": tenant, "users": (admin, accountant, outsider), "accounts": accounts}
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _company(db, name, registration):
    return db.execute("""INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,%s) RETURNING id""",
                      (name, registration, f"{registration}@example.test")).fetchone()["id"]


def _user(db, tenant, role, name):
    return db.execute("""INSERT INTO users(tenant_id,name,email,role,status)
        VALUES (%s,%s,%s,%s,'active') RETURNING *""", (tenant, name, f"{name}@example.test", role)).fetchone()


def _seed_open_items(db, tenant, admin):
    invoice = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,
            contact_name,document_date,due_date,total_amount)
        VALUES (%s,'invoice','INV-TEST','ready','Typed Buyer','2026-07-22','2026-07-22',100) RETURNING id""",
        (tenant,)).fetchone()["id"]
    for number, day, amount in (("PAY-1", "2026-08-20", "25.00"), ("PAY-2", "2026-09-01", "75.00")):
        payment = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,
                document_date,total_amount) VALUES (%s,'payment',%s,'ready',%s,%s) RETURNING id""",
            (tenant, number, day, amount)).fetchone()["id"]
        db.execute("""INSERT INTO erp_document_allocations(tenant_id,source_document_id,target_document_id,amount,created_by)
            VALUES (%s,%s,%s,%s,%s)""", (tenant, payment, invoice, amount, admin))

    doc = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,
            storage_path,mime_type,file_size_bytes,file_hash_sha256,status)
        VALUES (%s,%s,'scan.pdf','bill','accounts_payable','synthetic/scan.pdf','application/pdf',100,%s,'completed')
        RETURNING id,document_code""", (tenant, admin, hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()
    submission = db.execute("""INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision)
        VALUES (%s,%s,%s,'submitted',1) RETURNING id""", (doc["id"], tenant, admin)).fetchone()["id"]
    db.execute("""INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision)
        VALUES (%s,%s,%s,%s,'approved',1)""", (submission, doc["id"], tenant, admin))
    record = db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,supplier_name,reference_number,
            invoice_date,total_amount,amount_due,snapshot)
        VALUES (%s,%s,'accounts_payable','Scanned Supplier','BILL-TEST','2026-08-21',90,60,%s) RETURNING id""",
        (doc["id"], tenant, Jsonb({}))).fetchone()["id"]
    settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,
            source_type,created_by) VALUES (%s,'SET-TEST','2026-08-25','money_out',10,'MYR','manual_record',%s)
        RETURNING id""", (tenant, admin)).fetchone()["id"]
    db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by)
        VALUES (%s,%s,%s,10,%s)""", (tenant, settlement, record, admin))
    db.execute("""INSERT INTO exceptions(tenant_id,document_id,type,severity,message,status)
        VALUES (%s,%s,'review','low','Check invoice details','open'),(%s,%s,'duplicate','low','Resolved item','resolved')""",
        (tenant, doc["id"], tenant, doc["id"]))


def _post_journal(client, period, accounts, day, description, *entries):
    return client.post(JOURNALS, json={
        "periodId": period, "idempotencyKey": str(uuid4()), "sourceType": "manual", "journalDate": day,
        "description": description, "lines": [
            {"accountId": str(accounts[code]), side: amount} for code, side, amount in entries
        ],
    })


@pytest.fixture(scope="module")
def workspace(ledger):
    connect, ids = ledger
    with pytest.MonkeyPatch.context() as patch:
        for module in (main, accounting, financial):
            patch.setattr(module, "connect", connect)
        patch.setenv("AUTH_SECRET", "financial-report-integration-test-" + uuid4().hex)
        clients = []
        with connect() as db:
            for user in ids["users"]:
                session_id, _, _ = main.issue_session(db, user)
                token = create_access_token(str(user["id"]), session_id)
                clients.append(TestClient(main.app, cookies={main.ACCESS: token},
                                          headers={"X-Requested-With": "AccountingIntelligence"}))
        admin, accountant, outsider = clients
        july = _create_period(admin, "2026-07", "2026-07-01", "2026-07-31")
        august = _create_period(admin, "2026-08", "2026-08-01", "2026-08-31")
        september = _create_period(admin, "2026-09", "2026-09-01", "2026-09-30")
        accounts = ids["accounts"]
        for period, day, description, entries in (
            (july, "2026-07-01", "Capital", (("1010", "debit", "1000"), ("3000", "credit", "1000"))),
            (july, "2026-07-20", "July sale", (("1010", "debit", "106"), ("4000", "credit", "100"), ("2080", "credit", "6"))),
            (august, "2026-08-05", "August sale", (("1010", "debit", "212"), ("4000", "credit", "200"), ("2080", "credit", "12"))),
            (august, "2026-08-10", "Supplies", (("5000", "debit", "50"), ("1160", "debit", "3"), ("2000", "credit", "53"))),
            (september, "2026-09-01", "Future sale", (("1010", "debit", "99"), ("4000", "credit", "99"))),
        ):
            posted = _post_journal(admin, period, accounts, day, description, *entries)
            assert posted.is_success, posted.text
        temporary = _post_journal(admin, august, accounts, "2026-08-15", "Temporary expense",
                                  ("5000", "debit", "15"), ("1010", "credit", "15"))
        assert temporary.is_success, temporary.text
        reversed_journal = admin.post(f"{JOURNALS}/{temporary.json()['id']}/reverse")
        assert reversed_journal.is_success, reversed_journal.text
        try:
            yield admin, accountant, outsider, {**ids, "august": august, "original": temporary.json()["id"],
                                                "reversal": reversed_journal.json()["id"]}
        finally:
            for client in clients:
                client.close()


def _create_period(client, code, start, end):
    response = client.post(PERIODS, json={"periodCode": code, "startsOn": start, "endsOn": end})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _report(client, name, params):
    response = client.get(f"{REPORTS}/{name}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_statements_and_running_balances(workspace):
    _, accountant, _, ids = workspace
    pnl = _report(accountant, "profit-and-loss", AUGUST)
    assert (pnl["totalIncome"], pnl["totalExpenses"], pnl["netProfit"]) == ("200.00", "50.00", "150.00")
    assert [(s["label"], s["total"]) for s in pnl["sections"]] == [("Trading income", "200.00"), ("Expenses", "50.00")]
    balance = _report(accountant, "balance-sheet", AS_OF)
    assert balance["balanced"] is True
    assert balance["totalAssets"] == balance["totalLiabilitiesAndEquity"] == "1321.00"
    assert balance["currentYearEarnings"] == "250.00"
    assert balance["sections"][2]["total"] == "1250.00"

    ledger = _report(accountant, "general-ledger", {**AUGUST, "accountId": str(ids["accounts"]["1010"])})
    bank, = ledger["accounts"]
    assert bank["openingBalance"] == "1106.00"
    assert [line["balance"] for line in bank["lines"]] == ["1318.00", "1303.00", "1318.00"]
    assert [line["journalId"] for line in bank["lines"][1:]] == [ids["original"], ids["reversal"]]
    assert bank["closingBalance"] == "1318.00"
    capital = _report(accountant, "general-ledger", {**AUGUST, "accountId": str(ids["accounts"]["3000"])})["accounts"][0]
    assert capital["openingBalance"] == capital["closingBalance"] == "-1000.00"
    assert capital["lines"] == []


def test_tax_and_both_ageing_sources(workspace):
    _, accountant, _, _ = workspace
    tax = _report(accountant, "tax-summary", {**AUGUST, "fromDate": "2026-07-01"})
    assert tax["months"] == [
        {"month": "2026-07", "outputTax": "6.00", "inputTax": "0.00", "net": "6.00"},
        {"month": "2026-08", "outputTax": "12.00", "inputTax": "3.00", "net": "9.00"},
    ]
    assert tax["totals"] == {"outputTax": "18.00", "inputTax": "3.00", "net": "15.00"}
    received = _report(accountant, "ageing", {**AS_OF, "type": "receivables"})
    assert received["rows"] == [{"contactName": "Typed Buyer", "source": "typed", "documentNumber": "INV-TEST",
                                 "documentDate": "2026-07-22", "dueDate": "2026-07-22",
                                 "outstanding": "75.00", "bucket": "31-60"}]
    assert received["totals"]["31-60"] == received["totals"]["total"] == "75.00"
    payable = _report(accountant, "ageing", {**AS_OF, "type": "payables"})
    assert payable["rows"] == [{"contactName": "Scanned Supplier", "source": "scanned", "documentNumber": "BILL-TEST",
                                "documentDate": "2026-08-21", "dueDate": None, "outstanding": "50.00", "bucket": "1-30"}]
    assert payable["totals"]["1-30"] == payable["totals"]["total"] == "50.00"


def test_csv_and_month_pack(workspace):
    _, accountant, _, _ = workspace
    response = accountant.get(f"{REPORTS}/profit-and-loss", params={**AUGUST, "format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="profit-and-loss.csv"'
    rows = list(csv.reader(StringIO(response.content.decode("utf-8"))))
    assert rows[:2] == [["Section", "Code", "Account", "Amount"], ["Trading income", "4000", "Sales", "200.00"]]

    single_report = accountant.get(f"{REPORTS}/balance-sheet", params={**AS_OF, "format": "xlsx"})
    assert single_report.status_code == 200
    assert load_workbook(BytesIO(single_report.content)).sheetnames == ["Balance sheet"]

    response = accountant.get(PACK, params={"period": "2026-08"})
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == 'attachment; filename="month-pack-2026-08.xlsx"'
    book = load_workbook(BytesIO(response.content), data_only=True)
    assert book.sheetnames == ["Profit and loss", "Balance sheet", "Trial balance", "General ledger",
                               "Receivables ageing", "Payables ageing", "Tax summary", "Open exceptions"]
    assert book["Profit and loss"]["A1"].value == "Financial Test Co. | Profit and loss | 2026-08-01 to 2026-08-31"
    assert book["Profit and loss"]["D3"].value == 200
    assert book["Profit and loss"]["D3"].number_format == "0.00"
    assert book["Trial balance"]["D3"].value == 1333
    assert book["Trial balance"]["F10"].value == 0
    assert book["Tax summary"]["A2"].value == "Recorded tax only - not a tax return"
    assert list(book["Open exceptions"].values)[2][1:4] == ("review", "low", "Check invoice details")
    assert book["Open exceptions"].max_row == 3


def test_isolation_and_parameters(workspace):
    _, accountant, outsider, ids = workspace
    assert _report(outsider, "profit-and-loss", AUGUST)["sections"] == []
    assert _report(outsider, "general-ledger", {**AUGUST, "accountId": str(ids["accounts"]["1010"])})["accounts"] == []
    assert _report(outsider, "balance-sheet", AS_OF)["totalAssets"] == "0.00"
    assert _report(outsider, "ageing", {**AS_OF, "type": "receivables"})["rows"] == []
    assert _report(outsider, "tax-summary", AUGUST)["months"] == []
    assert outsider.get(PACK, params={"period": "2026-08"}).status_code == 404
    assert outsider.post(f"{PERIODS}/{ids['august']}/close").status_code == 404
    for path, params in (("profit-and-loss", {"toDate": "2026-08-31"}),
                         ("general-ledger", {**AUGUST, "fromDate": "2026-09-01"}),
                         ("balance-sheet", {"asOf": "2026-02-31"}),
                         ("ageing", {**AS_OF, "type": "unknown"}),
                         ("tax-summary", {**AUGUST, "format": "pdf"})):
        response = accountant.get(f"{REPORTS}/{path}", params=params)
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)


def test_period_lock_reopen_and_audit(workspace, ledger):
    admin, accountant, _, ids = workspace
    connect, _ = ledger
    path = f"{PERIODS}/{ids['august']}"
    assert accountant.post(f"{path}/close").status_code == 403
    assert accountant.post(f"{path}/reopen", json={"reason": "Correction"}).status_code == 403
    closed = admin.post(f"{path}/close", json={"reason": "Month end"})
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    assert admin.post(f"{path}/close").status_code == 409
    denied = _post_journal(admin, ids["august"], ids["accounts"], "2026-08-16", "Late entry",
                           ("1010", "debit", "1"), ("4000", "credit", "1"))
    assert denied.status_code == 409 and "open accounting period" in denied.json()["detail"]
    assert admin.post(f"{path}/reopen").status_code == 422
    assert admin.post(f"{path}/reopen", json={}).status_code == 422
    assert admin.post(f"{path}/reopen", json={"reason": "   "}).status_code == 422
    reopened = admin.post(f"{path}/reopen", json={"reason": "Post correction"})
    assert reopened.status_code == 200 and reopened.json()["status"] == "open"
    assert admin.post(f"{path}/reopen", json={"reason": "Correction"}).status_code == 409

    with connect() as db:
        actions = db.execute("""SELECT action,details FROM audit_logs WHERE tenant_id=%s AND resource=%s
            ORDER BY created_at""", (ids["tenant"], ids["august"])).fetchall()
    assert [(row["action"], json.loads(row["details"])["reason"]) for row in actions] == [
        ("ledger.period_closed", "Month end"), ("ledger.period_reopened", "Post correction"),
    ]


@pytest.mark.parametrize(("code", "start", "end", "message"), [
    ("overlap", "2026-08-31", "2026-09-01", "overlap"),
    ("2026-08", "2026-10-01", "2026-10-31", "already in use"),
])
def test_period_create_rejects_overlap_and_duplicate(workspace, code, start, end, message):
    admin, _, _, _ = workspace
    response = admin.post(PERIODS, json={"periodCode": code, "startsOn": start, "endsOn": end})
    assert response.status_code == 422
    assert message in response.json()["detail"]
