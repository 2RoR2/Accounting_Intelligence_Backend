"""Financial report and period APIs against a disposable loopback PostgreSQL database."""
import csv
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import main
from security.authentication import create_access_token
from services.imports import opening
from services.routes.accounting import routes as accounting
from services.routes.erp import posting
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
                for code, label, kind in (("1010", "Bank", "asset"), ("1100", "Receivables", "asset"),
                                          ("1160", "SST input", "asset"),
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
            open_items = _seed_open_items(db, tenant, admin["id"])
        yield connect, {"tenant": tenant, "users": (admin, accountant, outsider), "accounts": accounts, **open_items}
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _company(db, name, registration):
    return db.execute("""INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,%s) RETURNING id""",
                      (name, registration, f"{registration}@example.test")).fetchone()["id"]


def _user(db, tenant, role, name):
    return db.execute("""INSERT INTO users(tenant_id,name,email,role,status)
        VALUES (%s,%s,%s,%s,'active') RETURNING *""", (tenant, name, f"{name}@example.test", role)).fetchone()


def _approve_document(db, tenant, admin, document):
    submission = db.execute("""INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision)
        VALUES (%s,%s,%s,'submitted',1) RETURNING id""", (document, tenant, admin)).fetchone()["id"]
    db.execute("""INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision)
        VALUES (%s,%s,%s,%s,'approved',1)""", (submission, document, tenant, admin))


def _seed_open_items(db, tenant, admin):
    invoice = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,
            contact_name,document_date,due_date,total_amount)
        VALUES (%s,'invoice','INV-TEST','ready','Typed Buyer','2026-07-22','2026-07-22',100) RETURNING id""",
        (tenant,)).fetchone()["id"]
    typed_allocations = []
    for number, day, amount in (("PAY-1", "2026-08-20", "25.00"), ("PAY-2", "2026-09-01", "75.00")):
        payment = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,
                document_date,total_amount) VALUES (%s,'payment',%s,'ready',%s,%s) RETURNING id""",
            (tenant, number, day, amount)).fetchone()["id"]
        typed_allocations.append(db.execute("""INSERT INTO erp_document_allocations
            (tenant_id,source_document_id,target_document_id,amount,created_by,created_at)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (tenant, payment, invoice, amount, admin, f"{day}T10:00:00+08")).fetchone()["id"])

    doc = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,
            storage_path,mime_type,file_size_bytes,file_hash_sha256,status)
        VALUES (%s,%s,'scan.pdf','bill','accounts_payable','synthetic/scan.pdf','application/pdf',100,%s,'completed')
        RETURNING id,document_code""", (tenant, admin, hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()
    _approve_document(db, tenant, admin, doc["id"])
    record = db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,supplier_name,reference_number,
            invoice_date,total_amount,amount_due,snapshot)
        VALUES (%s,%s,'accounts_payable','Scanned Supplier','BILL-TEST','2026-08-21',90,60,%s) RETURNING id""",
        (doc["id"], tenant, Jsonb({}))).fetchone()["id"]
    settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,
            source_type,created_by) VALUES (%s,'SET-TEST','2026-08-25','money_out',10,'MYR','manual_record',%s)
        RETURNING id""", (tenant, admin)).fetchone()["id"]
    allocation = db.execute("""INSERT INTO settlement_allocations
        (tenant_id,settlement_id,record_id,amount,created_by,created_at)
        VALUES (%s,%s,%s,10,%s,'2026-08-25T10:00:00+08') RETURNING id""", (tenant, settlement, record, admin)).fetchone()["id"]
    db.execute("""INSERT INTO exceptions(tenant_id,document_id,type,severity,message,status,created_at)
        VALUES (%s,%s,'review','low','Check invoice details','open','2026-08-21T10:00:00+08'),
               (%s,%s,'duplicate','low','Resolved item','resolved','2026-08-21T10:00:00+08')""",
        (tenant, doc["id"], tenant, doc["id"]))
    return {"invoice": invoice, "typed_allocation": typed_allocations[0], "scanned_doc": doc["id"], "record": record,
            "settlement": settlement, "scanned_allocation": allocation}


def _journal_payload(period, accounts, day, description, *entries, source_type="manual", source_id=None, key=None):
    return {
        "periodId": str(period), "idempotencyKey": key or str(uuid4()), "sourceType": source_type,
        "sourceId": str(source_id) if source_id else None, "journalDate": day,
        "description": description, "lines": [
            {"accountId": str(accounts[code]), side: amount} for code, side, amount in entries
        ],
    }


def _post_journal(client, *args, **kwargs):
    return client.post(JOURNALS, json=_journal_payload(*args, **kwargs))


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
            source_type, source_id = {
                "July sale": ("erp_document", ids["invoice"]),
                "August sale": ("erp_document", ids["invoice"]),
                "Supplies": ("document", ids["scanned_doc"]),
            }.get(description, ("manual", None))
            posted = _post_journal(admin, period, accounts, day, description, *entries,
                                   source_type=source_type, source_id=source_id)
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


@pytest.fixture
def report_db(workspace, ledger):
    connect, _ = ledger
    with connect() as db:
        try:
            yield db, workspace[3]
        finally:
            db.rollback()


def _record(db, ids, day, description, *entries, period=None, **source):
    data = _journal_payload(period or ids["august"], ids["accounts"], day, description, *entries, **source)
    return accounting.record_journal(db, ids["users"][0], accounting.JournalInput(**data))["id"]


def test_statements_and_running_balances(workspace):
    _, accountant, _, ids = workspace
    pnl = _report(accountant, "profit-and-loss", AUGUST)
    assert (pnl["totalIncome"], pnl["totalExpenses"], pnl["netProfit"]) == ("200.00", "50.00", "150.00")
    assert [(s["label"], s["total"]) for s in pnl["sections"]] == [("Trading income", "200.00"), ("Expenses", "50.00")]
    balance = _report(accountant, "balance-sheet", AS_OF)
    assert balance["balanced"] is True
    assert balance["totalAssets"] == balance["totalLiabilitiesAndEquity"] == "1321.00"
    assert balance["retainedEarnings"] == "0.00"
    assert balance["currentYearEarnings"] == "250.00"
    assert balance["sections"][2]["total"] == "1250.00"
    assert [row["accountId"] for row in balance["sections"][2]["accounts"] if row["code"] == ""] == [
        "retained-earnings", "current-year-earnings",
    ]

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


def test_usd_sources_and_their_reversals_are_excluded_from_myr_reports(report_db):
    db, ids = report_db
    tenant, user = ids["tenant"], ids["users"][0]
    day = date(2026, 8, 22)
    baseline_balance = financial._balance_sheet(db, tenant, day)
    baseline_trial = financial._trial_balance_sheet(db, tenant, day)
    document = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,
            storage_path,mime_type,file_size_bytes,file_hash_sha256,status)
        VALUES (%s,%s,'usd.pdf','invoice','accounts_receivable','synthetic/usd.pdf','application/pdf',100,%s,'completed')
        RETURNING id""", (tenant, user["id"], hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()["id"]
    _approve_document(db, tenant, user["id"], document)
    record = db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,currency,snapshot,is_current)
        VALUES (%s,%s,'accounts_receivable','USD','{}',false) RETURNING id""", (document, tenant)).fetchone()["id"]
    db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,currency,snapshot,revision_number)
        VALUES (%s,%s,'accounts_receivable','MYR','{}',2)""", (document, tenant))
    typed = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,
            document_date,currency,total_amount)
        VALUES (%s,'invoice','USD-INV','ready',%s,'USD',110) RETURNING id""", (tenant, day)).fetchone()["id"]
    settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,
            source_type,created_by)
        VALUES (%s,'USD-SET',%s,'money_in',110,'USD','manual_record',%s) RETURNING id""",
        (tenant, day, user["id"])).fetchone()["id"]
    journals = []
    for source_type, source_id, key in (
        ("document", document, f"customer-invoice:{record}:revision:1"),
        ("adjustment", document, f"adjustment:debit_note:{record}:revision:1"),
        ("erp_document", typed, None),
        ("settlement", settlement, None),
    ):
        journals.append(_record(db, ids, day, f"USD {source_type}", ("1010", "debit", "110"),
                                ("4000", "credit", "100"), ("2080", "credit", "10"),
                                source_type=source_type, source_id=source_id, key=key))
    assert financial._balance_sheet(db, tenant, day) == baseline_balance
    assert financial._trial_balance_sheet(db, tenant, day) == baseline_trial
    assert financial._profit_and_loss(db, tenant, day, day)["sections"] == []
    assert financial._tax_summary(db, tenant, day, day)["months"] == []

    for journal in journals:
        posting.reverse_in_transaction(db, user, journal, reason="USD correction")
    ledger = financial._general_ledger(db, tenant, day, day, None)
    assert all(not account["lines"] for account in ledger["accounts"])


def test_tax_counts_transaction_reversals_and_excludes_manual_and_settlement_movements(report_db):
    db, ids = report_db
    day = date(2026, 8, 22)
    tax_lines = (("1010", "debit", "7"), ("2080", "credit", "7"))
    _record(db, ids, day, "Manual tax movement", *tax_lines)
    _record(db, ids, day, "Settlement tax movement", *tax_lines,
            source_type="settlement", source_id=ids["settlement"])
    adjustment = _record(db, ids, day, "Scanned tax adjustment", *tax_lines,
                         source_type="adjustment", source_id=ids["scanned_doc"])
    assert financial._tax_summary(db, ids["tenant"], day, day)["totals"]["outputTax"] == 7
    posting.reverse_in_transaction(db, ids["users"][0], adjustment, reason="Correct transaction tax")
    assert financial._tax_summary(db, ids["tenant"], day, day)["totals"]["outputTax"] == 0


def test_ageing_applies_allocation_reversals_on_their_effective_date(report_db):
    db, ids = report_db
    tenant = ids["tenant"]
    reversal_time = "2026-09-01T16:30:00Z"  # September 2 in Kuala Lumpur.
    db.execute("""UPDATE erp_document_allocations SET status='reversed',reversed_at=%s WHERE id=%s""",
               (reversal_time, ids["typed_allocation"]))
    db.execute("""UPDATE erp_document_headers SET document_date='2026-08-26'
        WHERE tenant_id=%s AND document_number='PAY-2'""", (tenant,))
    db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,status,created_by,
            reversed_at,reversal_reason,reverses_allocation_id)
        SELECT tenant_id,settlement_id,record_id,amount,'reversed',created_by,%s,'Voided payment',id
        FROM settlement_allocations WHERE id=%s""", (reversal_time, ids["scanned_allocation"]))
    db.execute("UPDATE settlements SET status='voided' WHERE id=%s", (ids["settlement"],))

    august_end = date(2026, 8, 31)
    undo_day = date(2026, 9, 2)
    assert financial._ageing(db, tenant, august_end, "receivables")["totals"]["total"] == 75
    assert financial._ageing(db, tenant, august_end, "payables")["totals"]["total"] == 50
    assert financial._ageing(db, tenant, undo_day, "receivables")["totals"]["total"] == 25
    assert financial._ageing(db, tenant, undo_day, "payables")["totals"]["total"] == 60


def test_opening_import_appears_in_ageing_and_ledger_but_never_in_period_profit_or_tax(report_db):
    db, ids = report_db
    tenant, user = ids["tenant"], ids["users"][0]
    day = date(2026, 8, 18)
    content = b"""row_type,as_at,account_code,contact_id,document_number,document_date,due_date,debit,credit
account,2026-08-18,1100,,,,,120,
account,2026-08-18,2000,,,,,,40
account,2026-08-18,4000,,,,,,70
account,2026-08-18,2080,,,,,,10
receivable,2026-08-18,1100,Opening Buyer,BEFORE-1,2026-06-10,2026-06-30,120,
"""
    preview, journal = opening.preview_plan(db, tenant, content)
    assert preview["canConfirm"], preview
    posted = accounting.record_journal(db, user, accounting.JournalInput(**journal))
    new_control = db.execute("""INSERT INTO ledger_accounts(tenant_id,code,name,account_type)
        VALUES (%s,'1120','New receivables','asset') RETURNING id""", (tenant,)).fetchone()["id"]
    db.execute("""INSERT INTO erp_posting_accounts(tenant_id,purpose,account_id)
        VALUES (%s,'receivable',%s)""", (tenant, new_control))

    cutoff = date(2026, 9, 18)
    for report_type, contact, amount in (
        ("receivables", "Opening Buyer", 120),
        ("payables", "Opening balance (not itemised)", 40),
    ):
        report = financial._ageing(db, tenant, cutoff, report_type)
        assert [row for row in report["rows"] if row["source"] == "opening"] == [{
            "source": "opening", "contactName": contact, "documentNumber": "Opening balance",
            "documentDate": day, "dueDate": None, "outstanding": amount, "bucket": "31-60",
        }]
    assert financial._profit_and_loss(db, tenant, day, day)["sections"] == []
    assert financial._tax_summary(db, tenant, day, day)["months"] == []
    balance = financial._balance_sheet(db, tenant, date(2026, 8, 31))
    assert balance["balanced"] and balance["currentYearEarnings"] == 320
    trial = financial._trial_balance_sheet(db, tenant, day)
    assert next(row for row in trial.rows if row[0] == "1100")[3] == 120
    ledger = financial._general_ledger(db, tenant, day, day, ids["accounts"]["1100"])
    assert ledger["accounts"][0]["lines"][0]["journalId"] == posted["id"]

    posting.reverse_in_transaction(db, user, posted["id"], reason="Replace opening import")
    assert financial._profit_and_loss(db, tenant, day, day)["sections"] == []
    assert all(row["source"] != "opening" for row in financial._ageing(db, tenant, cutoff, "receivables")["rows"])


def test_balance_sheet_carries_prior_year_profit_into_retained_earnings_and_exports(report_db):
    db, ids = report_db
    tenant = ids["tenant"]
    end = date(2027, 1, 31)
    prior_year = financial._balance_sheet(db, tenant, date(2026, 12, 31))
    prior_profit = prior_year["currentYearEarnings"]
    assert prior_profit >= 349
    period = db.execute("""INSERT INTO accounting_periods(tenant_id,period_code,starts_on,ends_on)
        VALUES (%s,'2027-01','2027-01-01',%s) RETURNING id""", (tenant, end)).fetchone()["id"]
    _record(db, ids, "2027-01-01", "New year sale", ("1010", "debit", "25"), ("4000", "credit", "25"), period=period)
    balance = financial._balance_sheet(db, tenant, end)
    assert balance["balanced"]
    assert balance["totalAssets"] == balance["totalLiabilitiesAndEquity"] == prior_year["totalAssets"] + 25
    assert balance["retainedEarnings"] == prior_profit
    assert balance["currentYearEarnings"] == 25
    equity = next(section for section in balance["sections"] if section["key"] == "equity")
    assert [row for row in equity["accounts"] if not row["code"]] == [
        {"accountId": "retained-earnings", "code": "", "name": "Retained earnings (prior years)", "amount": prior_profit},
        {"accountId": "current-year-earnings", "code": "", "name": "Current year earnings", "amount": 25},
    ]
    sheet = financial._balance_sheet_sheet(balance)
    exported = financial._csv_export(sheet, "balance-sheet.csv")
    rows = list(csv.reader(StringIO(exported.body.decode("utf-8"))))
    assert [row for row in rows if row[0] == "Equity" and not row[1]][:2] == [
        ["Equity", "", "Retained earnings (prior years)", f"{prior_profit:.2f}"],
        ["Equity", "", "Current year earnings", "25.00"],
    ]
    exported = financial._xlsx_export("Financial Test Co.", [sheet], "balance-sheet.xlsx")
    book = load_workbook(BytesIO(exported.body), data_only=True)
    assert [(row[2], row[3]) for row in book.active.values if row[0] == "Equity" and row[1] is None][:2] == [
        ("Retained earnings (prior years)", prior_profit), ("Current year earnings", 25),
    ]


def test_csv_and_month_pack(workspace, monkeypatch):
    _, accountant, _, _ = workspace
    response = accountant.get(f"{REPORTS}/profit-and-loss", params={**AUGUST, "format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="profit-and-loss.csv"'
    rows = list(csv.reader(StringIO(response.content.decode("utf-8"))))
    assert rows[:2] == [["Section", "Code", "Account", "Amount"], ["Trading income", "4000", "Sales", "200.00"]]

    single_report = accountant.get(f"{REPORTS}/balance-sheet", params={**AS_OF, "format": "xlsx"})
    assert single_report.status_code == 200
    assert load_workbook(BytesIO(single_report.content)).sheetnames == ["Balance sheet"]

    def unexpected_row_scan(_sheet):
        pytest.fail("The exporter must track the current row without scanning the worksheet")

    with monkeypatch.context() as patch:
        patch.setattr(Worksheet, "max_row", property(unexpected_row_scan))
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


def test_month_pack_uses_saved_dates_for_custom_and_month_like_codes(workspace):
    admin, accountant, _, ids = workspace
    before = _create_period(admin, "early-october", "2026-10-01", "2026-10-14")
    custom = _create_period(admin, "Close Oct/Nov", "2026-10-15", "2026-11-05")
    after = _create_period(admin, "2026-11", "2026-11-06", "2026-11-20")
    for period, day, amount in ((before, "2026-10-14", "80"), (custom, "2026-11-05", "26"), (after, "2026-11-06", "95")):
        response = _post_journal(admin, period, ids["accounts"], day, "Boundary sale",
                                 ("1010", "debit", amount), ("4000", "credit", amount))
        assert response.is_success, response.text

    response = accountant.get(PACK, params={"period": "Close Oct/Nov"})
    assert response.status_code == 200, response.text
    book = load_workbook(BytesIO(response.content), data_only=True)
    for sheet in book:
        expected = ("As of 2026-11-05" if sheet.title in
                    ("Balance sheet", "Trial balance", "Receivables ageing", "Payables ageing")
                    else "2026-10-15 to 2026-11-05")
        assert sheet["A1"].value.endswith(expected)
    assert book["Profit and loss"]["D3"].value == 26
    assert next(row[3] for row in book["Balance sheet"].values if row[2] == "Current year earnings") == 455
    assert next(row[4] for row in book["Trial balance"].values if row[0] == "4000") == 505
    assert {row[2] for row in book["General ledger"].values if row[5] == "manual"} == {"2026-11-05"}
    assert book["Open exceptions"].max_row == 2

    response = accountant.get(PACK, params={"period": "2026-11"})
    assert response.status_code == 200, response.text
    book = load_workbook(BytesIO(response.content), data_only=True)
    assert book["Profit and loss"]["A1"].value.endswith("2026-11-06 to 2026-11-20")
    assert book["Profit and loss"]["D3"].value == 95
    assert accountant.get(PACK, params={"period": "unconfigured"}).status_code == 404


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
    ("2026-08", "2026-12-01", "2026-12-31", "already in use"),
])
def test_period_create_rejects_overlap_and_duplicate(workspace, code, start, end, message):
    admin, _, _, _ = workspace
    response = admin.post(PERIODS, json={"periodCode": code, "startsOn": start, "endsOn": end})
    assert response.status_code == 422
    assert message in response.json()["detail"]
