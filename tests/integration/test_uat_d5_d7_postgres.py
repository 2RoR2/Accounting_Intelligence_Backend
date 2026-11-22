"""D5-D7 API contracts against a disposable loopback PostgreSQL database."""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import main
from services.routes.accounting import routes as accounting
from services.routes.bank import routes as bank
from services.routes.documents import routes as documents
from services.routes.erp import documents as erp
from services.routes.home import routes as home
from tests.extraction.test_multidocument import contract
from tests.extraction.test_receipt_line_recovery import receipt

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
REVIEW = "/api/documents/review"
SCANNED = "/api/erp/scanned"
BANK = "/api/bank/transactions"
DAY = "2026-08-12"
CLOSED = "2026-09-12"


@pytest.fixture(scope="module")
def database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires the disposable local TEST_DATABASE_URL")
    parts = urlsplit(url)
    assert parts.hostname in {"127.0.0.1", "localhost", "::1"}
    name = "uat_d5_d7_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute(f"CREATE DATABASE {name}")
    target = parts._replace(path="/" + name).geturl()

    @contextmanager
    def connect():
        with psycopg.connect(target, row_factory=dict_row) as db:
            yield db

    try:
        migrations = [(ROOT / f"database/migrations/{name}.sql").read_text(encoding="utf-8") for name in (
            "20261125_bank_recorded_document", "20261126_bank_recorded_document_void",
        )]
        schema = (ROOT / "database/PostgreSQL Database schema.sql").read_text(encoding="utf-8-sig")
        for migration in migrations:
            assert migration.strip() in schema
            schema = schema.replace(migration.strip(), "", 1)
        with connect() as db:
            db.execute(schema)
            for migration in migrations:
                db.execute(migration)
                db.execute(migration)
        yield connect
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def company(database, monkeypatch):
    def tenant(db):
        tag = uuid4().hex
        return db.execute("""INSERT INTO tenants(name,registration_number,admin_email)
            VALUES (%s,%s,%s) RETURNING id""", (f"UAT Company {tag[:8]}", tag[:12], f"{tag}@example.test")).fetchone()["id"]

    def person(db, tenant_id, role):
        return db.execute("""INSERT INTO users(tenant_id,name,email,role,status)
            VALUES (%s,%s,%s,%s,'active') RETURNING *""",
            (tenant_id, role, f"{uuid4().hex}@example.test", role)).fetchone()

    with database() as db:
        own, other = tenant(db), tenant(db)
        users = {"admin": person(db, own, "local_admin"), "accountant": person(db, own, "accountant"),
                 "colleague": person(db, own, "accountant"), "outsider": person(db, other, "local_admin")}
        for code, start, end, status in (("2026-08", "2026-08-01", "2026-08-31", "open"),
                                         ("2026-09", "2026-09-01", "2026-09-30", "closed")):
            db.execute("""INSERT INTO accounting_periods(tenant_id,period_code,starts_on,ends_on,status)
                VALUES (%s,%s,%s,%s,%s)""", (own, code, start, end, status))
        accounts = {code: db.execute("""INSERT INTO ledger_accounts(tenant_id,code,name,account_type)
            VALUES (%s,%s,%s,%s) RETURNING id""", (own, code, label, kind)).fetchone()["id"]
            for code, label, kind in (("1010", "Bank", "asset"), ("1140", "SST input", "asset"),
                                       ("2000", "Payables", "liability"), ("4000", "Interest", "income"),
                                       ("5000", "Bank fees", "expense"))}
        db.execute("""INSERT INTO erp_catalog_entries(tenant_id,kind,name,code,ledger_account_id,data)
            VALUES (%s,'bank-account','Operating account','OPS',%s,%s)""",
            (own, accounts["1010"], Jsonb({"accountNumber": "123456"})))
        db.execute("INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,'Triple Six Point Enterprise 666')", (own,))

    for module in (main, accounting, bank, documents, erp, home):
        monkeypatch.setattr(module, "connect", database)
    monkeypatch.setattr(main, "session_user", lambda db, request: users[request.headers["X-Test-Actor"]])
    client = TestClient(main.app)

    def request(method, url, actor="admin", **kwargs):
        return client.request(method, url, headers={"X-Test-Actor": actor,
            "X-Requested-With": "AccountingIntelligence", "Origin": "http://localhost:3000"}, **kwargs)

    return database, own, users, accounts, request


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def insert_document(db, user, *, kind="bill", category="accounts_payable", status="extracted", revision=1):
    return db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,
        storage_path,mime_type,file_size_bytes,file_hash_sha256,status,content_revision)
        VALUES (%s,%s,'scan.pdf',%s,%s,'synthetic/scan.pdf','application/pdf',100,%s,%s,%s) RETURNING id""",
        (user["tenant_id"], user["id"], kind, category, hashlib.sha256(uuid4().bytes).hexdigest(), status, revision)).fetchone()["id"]


def review_document(company, *, blocked=False):
    connect, tenant, users, _, request = company
    actor = users["accountant"]
    raw, mapped = contract(receipt())
    with connect() as db:
        document = insert_document(db, actor, kind="receipt")
        extraction = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
            VALUES (%s,1,true,'parser_only',%s) RETURNING id""", (document, Jsonb(raw))).fetchone()["id"]
        keys = ("field_key", "field_label", "current_value", "normalized_value", "raw_value", "extraction_state",
                "resolution", "evidence", "candidates")
        for row in mapped["fields"]:
            values = [Jsonb(row[key]) if key in {"evidence", "candidates"} else row[key] for key in keys]
            db.execute("INSERT INTO field_predictions(document_id,extraction_id," + ",".join(keys) + ") VALUES ("
                       + ",".join(["%s"] * 11) + ")", [document, extraction, *values])
    revision = 1
    for description, amount in (("VS FUJI500ml", "10.00"), ("Apollo cake original 30s", "6.50"),
                                ("TG Cashew nuts mixwd Almond smoke", "4.20"), ("TG Coated coconut peanuts", "1.90")):
        revision = ok(request("POST", f"/api/documents/{document}/line-items", "accountant", json={
            "description": description, "quantity": "1", "unitPrice": amount, "amount": amount, "sourcePage": 1,
            "sourceNote": "Transcribed the source receipt", "expectedContentRevision": revision,
        }), 201)["contentRevision"]
    with connect() as db:
        db.execute("""UPDATE field_predictions SET corrected_by=%s,corrected_at=now(),
            current_value=COALESCE(current_value,normalized_value) WHERE document_id=%s AND field_key IN
            ('supplier','date','currency','total')""", (actor["id"], document))
    ok(request("POST", f"/api/documents/{document}/revalidate", "accountant"))
    if blocked:
        with connect() as db:
            db.execute("""INSERT INTO validation_results(document_id,tenant_id,content_revision,rule_id,document_type,
                status,severity,prevents_approval,message) VALUES (%s,%s,%s,'TEST-BLOCK','receipt','failed','blocking',true,
                'Check this blocking result first.')""", (document, tenant, revision))
    return {"documentId": str(document), "expectedContentRevision": revision}


def test_bulk_review_isolated_and_separation_of_duties(company):
    connect, _, users, _, request = company
    good = review_document(company)
    stale = review_document(company)
    stale["expectedContentRevision"] -= 1
    blocked = review_document(company, blocked=True)
    submit = {"items": [stale, good, blocked], "note": "Ready to check"}
    assert request("POST", f"{REVIEW}/bulk-submit", json=submit).status_code == 403
    results = ok(request("POST", f"{REVIEW}/bulk-submit", "accountant", json=submit))["results"]
    assert [result["ok"] for result in results] == [False, True, False]
    assert "changed" in results[0]["reason"] and results[1]["status"] == "IN_REVIEW"
    assert "revalidate" in results[2]["reason"]
    self_review = review_document(company)
    ok(request("POST", f"{REVIEW}/bulk-submit", "accountant", json={"items": [self_review]}))
    with connect() as db:
        # Simulate a submitted document whose original reviewer later changed roles.
        db.execute("UPDATE review_submissions SET submitted_by=%s WHERE document_id=%s",
                   (users["admin"]["id"], self_review["documentId"]))
    body = {"items": [self_review, good], "decision": "approved", "note": "Checked the evidence"}
    assert request("POST", f"{REVIEW}/bulk-decision", "accountant", json=body).status_code == 403
    results = ok(request("POST", f"{REVIEW}/bulk-decision", json=body))["results"]
    assert not results[0]["ok"] and "you submitted" in results[0]["reason"]
    assert results[1] == {"documentId": good["documentId"], "ok": True, "status": "VALIDATED"}
    with connect() as db:
        events = db.execute("SELECT action FROM audit_logs WHERE resource=%s", (good["documentId"],)).fetchall()
        assert {"document.submitted", "document.approved"} <= {row["action"] for row in events}


def test_suggested_fixes_only_use_current_evidence(company):
    connect, tenant, users, _, request = company
    with connect() as db:
        document = insert_document(db, users["accountant"], revision=2)
        extraction = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
            VALUES (%s,1,true,'parser_only','{}') RETURNING id""", (document,)).fetchone()["id"]
        db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,extraction_state,candidates)
            VALUES (%s,%s,'total','Total amount','ambiguous',%s)""",
            (extraction, document, Jsonb([{"source": "parser", "value": "42.50"}])))
        for revision, code, field, actual, reason in (
            (2, "TEST-TOTAL", "total", None, "Check printed total."),
            (2, "TEST-CURRENCY", "currency", None, "Currency is missing."),
            (2, "EX-008", "supplier", "Triple Six Point Enterprize 666", "Supplier matching requires review."),
            (2, "BILL-FORMAT", "date", "2026/8/12", "Enter the date in ISO format."),
            (1, "TEST-OLD", "tax", None, "Old finding."),
        ):
            db.execute("""INSERT INTO validation_results(document_id,tenant_id,content_revision,rule_id,document_type,
                status,severity,field_key,actual_value,message,prevents_approval)
                VALUES (%s,%s,%s,%s,'bill','failed','high',%s,%s,%s,true)""",
                (document, tenant, revision, code, field, actual, reason))
        db.execute("""INSERT INTO exceptions(document_id,tenant_id,type,severity,message)
            VALUES (%s,%s,'VALIDATION:TEST-TOTAL','high','total: Check printed total.'),
                   (%s,%s,'open-hold','medium','Supplier asked to hold this document.')""",
            (document, tenant, document, tenant))
    path = f"/api/documents/{document}/suggested-fixes"
    for actor in ("admin", "accountant"):
        suggestions = ok(request("GET", path, actor))
        fixes = {item["field"]: item for item in suggestions}
        assert len(suggestions) == len(fixes) == 5
        assert fixes["total"]["suggestedValue"] == "42.50" and fixes["total"]["source"] == "candidate"
        assert fixes["currency"]["suggestedValue"] is None and fixes["currency"]["explanation"]
        assert fixes["supplier"]["suggestedValue"] == "Triple Six Point Enterprise 666"
        assert fixes["supplier"]["source"] == "supplier-match"
        assert fixes["date"]["suggestedValue"] == DAY and fixes["date"]["source"] == "rule"
        assert fixes["document"]["suggestedValue"] is None and fixes["document"]["reason"].startswith("Supplier asked")
    for actor in ("colleague", "outsider"):
        assert request("GET", path, actor).status_code == 404


def test_supplier_suggestions_drop_unscoped_saved_matches(company):
    connect, tenant, users, _, request = company
    actor = users["accountant"]
    original = "Triple Six Point Enterprize 666"

    def extraction(db, number, supplier):
        row = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
            VALUES (%s,%s,true,'parser_only','{}') RETURNING id""", (document, number)).fetchone()
        db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,extraction_state,current_value)
            VALUES (%s,%s,'supplier','Supplier','resolved',%s)""", (row["id"], document, supplier))
        return row["id"]

    def finding(db, revision, supplier):
        db.execute("""INSERT INTO validation_results(document_id,tenant_id,content_revision,rule_id,document_type,
            status,severity,field_key,actual_value,message,prevents_approval)
            VALUES (%s,%s,%s,'EX-008','bill','failed','high','supplier',%s,'Supplier matching requires review.',true)""",
            (document, tenant, revision, supplier))

    with connect() as db:
        document = insert_document(db, actor)
        first = extraction(db, 1, original)
        finding(db, 1, original)
        db.execute("""INSERT INTO supplier_matches(tenant_id,document_id,candidate_name,confidence,source,decision)
            VALUES (%s,%s,'Triple Six Point Enterprise 666',0.99,'ai','accepted'),
                   (%s,%s,'Previous Supplier LLC',0.95,'rule','pending')""", (tenant, document, tenant, document))

    path = f"/api/documents/{document}/suggested-fixes"
    assert ok(request("GET", path, "accountant"))[0]["suggestedValue"] == "Triple Six Point Enterprise 666"
    with connect() as db:
        corrected = "Unlisted Paper Traders"
        db.execute("UPDATE documents SET content_revision=2 WHERE id=%s", (document,))
        db.execute("""UPDATE field_predictions SET current_value=%s,corrected_by=%s,corrected_at=now()
            WHERE extraction_id=%s AND field_key='supplier'""", (corrected, actor["id"], first))
        finding(db, 2, corrected)
    suggestion, = ok(request("GET", path, "accountant"))
    assert suggestion["suggestedValue"] is None and suggestion["reason"] and suggestion["explanation"]

    with connect() as db:
        db.execute("UPDATE documents SET content_revision=3 WHERE id=%s", (document,))
        db.execute("UPDATE raw_extractions SET is_current=false WHERE id=%s", (first,))
        extracted = "Orchid Timber Wholesale"
        extraction(db, 2, extracted)
        finding(db, 3, extracted)
    suggestion, = ok(request("GET", path, "accountant"))
    assert suggestion["suggestedValue"] is None and suggestion["reason"] and suggestion["explanation"]


def insert_record(db, users, *, day=DAY, category="accounts_payable", total="100.00", amount_due=None):
    user = users["accountant"]
    tenant = user["tenant_id"]
    kind = "bill" if category == "accounts_payable" else "invoice"
    document = insert_document(db, user, kind=kind, category=category, status="completed")
    submission = db.execute("""INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision)
        VALUES (%s,%s,%s,'submitted',1) RETURNING id""", (document, tenant, user["id"])).fetchone()["id"]
    db.execute("""INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision)
        VALUES (%s,%s,%s,%s,'approved',1)""", (submission, document, tenant, users["admin"]["id"]))
    return str(db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,supplier_name,reference_number,
        invoice_date,total_amount,amount_due,tax_amount,currency,validation_status,snapshot)
        VALUES (%s,%s,%s,'Supplier',%s,%s,%s,%s,0,'MYR','passed','{}') RETURNING id""",
        (document, tenant, category, uuid4().hex, day, total, amount_due)).fetchone()["id"])


def journal(connect, journal_id):
    with connect() as db:
        rows = db.execute("""SELECT l.account_id,a.code,a.name,l.description,l.debit,l.credit,j.journal_date
            FROM journal_lines l JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
            JOIN journal_headers j ON j.id=l.journal_id AND j.tenant_id=l.tenant_id
            WHERE j.id=%s ORDER BY l.line_number""", (journal_id,)).fetchall()
    return str(rows[0]["journal_date"]), [
        {"accountId": str(row["account_id"]), "code": row["code"], "name": row["name"],
         "description": row["description"], "debit": str(row["debit"]), "credit": str(row["credit"])} for row in rows]


def test_post_preview_and_bulk_post(company):
    connect, tenant, users, _, request = company
    with connect() as db:
        preview_id, bulk_id = (insert_record(db, users) for _ in range(2))
        closed_id = insert_record(db, users, day=CLOSED)
    preview = ok(request("GET", f"{SCANNED}/{preview_id}/post-preview", "accountant"))
    assert preview["balanced"] and not preview["alreadyPosted"] and not preview["previouslyReversed"]
    assert len(bytes.fromhex(preview["planHash"])) == 32
    assert preview["totalDebit"] == preview["totalCredit"] == "100.00"
    conflict = request("POST", f"{SCANNED}/{preview_id}/post",
                       json={"expectedPlanHash": preview["planHash"], "journalDate": "2026-08-13"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "The journal changed since you previewed it. Preview it again."
    with connect() as db:
        assert not db.execute("SELECT 1 FROM journal_headers WHERE tenant_id=%s", (tenant,)).fetchone()
        assert not db.execute("SELECT 1 FROM audit_logs WHERE tenant_id=%s AND action='erp.scanned_posted'", (tenant,)).fetchone()
    closed = ok(request("GET", f"{SCANNED}/{closed_id}/post-preview"))
    assert closed["balanced"] and "closed" in closed["problem"]
    posted = ok(request("POST", f"{SCANNED}/{preview_id}/post", json={"expectedPlanHash": preview["planHash"]}))
    assert journal(connect, posted["id"]) == (preview["journalDate"], preview["lines"])
    assert ok(request("GET", f"{SCANNED}/{preview_id}/post-preview")) == {**preview, "alreadyPosted": True}
    body = {"recordIds": [closed_id, bulk_id, preview_id, bulk_id]}
    assert request("POST", f"{SCANNED}/post-bulk", "accountant", json=body).status_code == 403
    failed, new, retry, bulk_retry = ok(request("POST", f"{SCANNED}/post-bulk", json=body))["results"]
    assert not failed["ok"] and "closed" in failed["reason"]
    assert new["ok"] and not new["idempotent"]
    assert retry["idempotent"] and retry["journalId"] == posted["id"]
    assert bulk_retry == {**new, "idempotent": True}
    assert request("GET", f"{SCANNED}/{preview_id}/post-preview", "outsider").status_code == 404
    foreign = ok(request("POST", f"{SCANNED}/post-bulk", "outsider", json={"recordIds": [preview_id]}))["results"][0]
    assert not foreign["ok"] and foreign["reason"] == "Record not found."
    to_post = ok(request("GET", "/api/records?posting=to-post"))
    assert [row["id"] for row in to_post] == [closed_id]


def test_reversed_scanned_record_can_repost_singly_and_in_bulk(company):
    connect, tenant, users, _, request = company
    with connect() as db:
        record = insert_record(db, users)
    path = f"{SCANNED}/{record}"
    original = ok(request("GET", f"{path}/post-preview"))
    first = ok(request("POST", f"{path}/post", json={"expectedPlanHash": original["planHash"]}))
    ok(request("POST", f"/api/ledger/journals/{first['id']}/reverse"), 201)

    def waiting():
        return [row["id"] for row in ok(request("GET", "/api/records?posting=to-post"))]

    assert waiting() == [record]
    reversed_preview = ok(request("GET", f"{path}/post-preview"))
    assert reversed_preview == {**original, "previouslyReversed": True}
    with connect() as db:
        new_account = db.execute("""INSERT INTO ledger_accounts(tenant_id,code,name,account_type)
            VALUES (%s,'5001','New expense','expense') RETURNING id""", (tenant,)).fetchone()["id"]
        db.execute("INSERT INTO erp_posting_accounts(tenant_id,purpose,account_id) VALUES (%s,'expense',%s)",
                   (tenant, new_account))
    conflict = request("POST", f"{path}/post", json={"expectedPlanHash": reversed_preview["planHash"]})
    assert conflict.status_code == 409 and waiting() == [record]
    fresh = ok(request("GET", f"{path}/post-preview"))
    assert not fresh["alreadyPosted"] and fresh["previouslyReversed"] and fresh["planHash"] != original["planHash"]
    assert fresh["lines"][0]["accountId"] == str(new_account)
    second = ok(request("POST", f"{path}/post", json={"expectedPlanHash": fresh["planHash"]}))
    assert not second["idempotent"] and second["id"] != first["id"] and waiting() == []
    assert ok(request("GET", f"{path}/post-preview")) == {**fresh, "alreadyPosted": True}
    ok(request("POST", f"/api/ledger/journals/{second['id']}/reverse"), 201)
    assert waiting() == [record]
    new, retry = ok(request("POST", f"{SCANNED}/post-bulk", json={"recordIds": [record, record]}))["results"]
    assert new["ok"] and not new["idempotent"] and new["journalId"] not in {first["id"], second["id"]}
    assert retry == {**new, "idempotent": True} and waiting() == []
    with connect() as db:
        keys = db.execute("SELECT idempotency_key FROM journal_headers WHERE tenant_id=%s AND source_type='document'",
                          (tenant,)).fetchall()
    assert len(keys) == 3 and sum(":repost:" in row["idempotency_key"] for row in keys) == 2


def insert_bank_line(company, *, reference="123456", day=DAY, amount="100.00", direction="money_out"):
    connect, tenant, users, _, _ = company
    with connect() as db:
        batch = db.execute("""INSERT INTO bank_imports(tenant_id,source_bank_account_ref,source_hash,input_format,
            mapping_version,imported_by,status) VALUES (%s,%s,%s,'csv','v1',%s,'confirmed') RETURNING id""",
            (tenant, reference, hashlib.sha256(uuid4().bytes).hexdigest(), users["admin"]["id"])).fetchone()["id"]
        incoming = direction == "money_in"
        return str(db.execute("""INSERT INTO bank_transactions(tenant_id,import_id,source_row,posted_date,description,
            debit_decimal,credit_decimal,signed_amount_decimal,direction,currency,fingerprint)
            VALUES (%s,%s,1,%s,'Bank charge',%s,%s,%s,%s,'MYR',%s) RETURNING id""",
            (tenant, batch, day, None if incoming else amount, amount if incoming else None,
             amount if incoming else "-" + amount, direction, hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()["id"])


def test_record_expense_balanced_idempotent_and_removed_from_today(company):
    connect, tenant, _, accounts, request = company
    fee = insert_bank_line(company, amount="2.50")
    assert ok(request("GET", "/api/home/summary"))["tiles"]["bankToMatch"]["count"] == 1
    body = {"ledgerAccountId": str(accounts["5000"]), "description": "Monthly fee"}
    path = f"{BANK}/{fee}/record-expense"
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: request("POST", path, "accountant", json=body), range(3)))
    expenses = [ok(response, 201) for response in responses]
    expense = expenses[0]
    assert all(response == expense for response in expenses)
    assert expense["documentNumber"].startswith("MO-") and expense["bankTransactionStatus"] == "matched"
    date, lines = journal(connect, expense["journalId"])
    assert date == DAY
    assert [(line["code"], line["debit"], line["credit"]) for line in lines] == [
        ("5000", "2.50", "0.00"), ("1010", "0.00", "2.50")]
    for line in (ok(request("GET", f"{BANK}/{fee}")), ok(request("GET", f"{BANK}?status=matched"))[0]):
        assert line["id"] == fee and line["matchStatus"] == "matched"
        assert line["recordedDocumentId"] == expense["documentId"]
        assert line["recordedDocumentNumber"] == expense["documentNumber"]
        assert line["recordedDocumentType"] == "money-out"
    assert ok(request("GET", "/api/home/summary"))["tiles"]["bankToMatch"]["count"] == 0
    assert ok(request("POST", path, json=body), 201) == expense
    assert request("POST", path, "outsider", json=body).status_code == 404
    with connect() as db:
        row = db.execute("SELECT status,document_type FROM erp_document_headers WHERE id=%s",
                         (expense["documentId"],)).fetchone()
        assert row == {"status": "ready", "document_type": "money-out"}
        assert not db.execute("SELECT 1 FROM unmatched_bank_transactions WHERE id=%s", (fee,)).fetchone()
        count = db.execute("SELECT count(*) AS n FROM audit_logs WHERE tenant_id=%s AND action='bank.expense_recorded'",
                           (tenant,)).fetchone()
        assert count["n"] == 1


def test_void_reopens_bank_line_and_allows_a_new_money_document(company):
    connect, tenant, users, accounts, request = company
    line = insert_bank_line(company, amount="2.50")
    expense = f"{BANK}/{line}/record-expense"
    body = {"ledgerAccountId": str(accounts["5000"])}
    first = ok(request("POST", expense, json=body), 201)
    void = f"/api/erp/documents/money-out/{first['documentId']}/transition"
    with pytest.raises(psycopg.errors.RaiseException, match="link cannot be changed"), connect() as db:
        db.execute("UPDATE bank_transactions SET status='imported',recorded_document_id=NULL WHERE id=%s", (line,))
    assert request("POST", void, "accountant", json={"status": "void"}).status_code == 403
    assert ok(request("GET", f"{BANK}/{line}"))["recordedDocumentId"] == first["documentId"]
    result = ok(request("POST", void, json={"status": "void", "reason": "Wrong expense account"}))
    assert result["status"] == "void" and result["reversalJournalId"]
    detail = ok(request("GET", f"{BANK}/{line}"))
    assert (detail["status"], detail["matchStatus"]) == ("imported", "unmatched")
    assert (detail["allocatedAmount"], detail["remainingAmount"]) == ("0.00", "2.50")
    assert all(detail[field] is None for field in ("recordedDocumentId", "recordedDocumentNumber", "recordedDocumentType"))
    assert ok(request("GET", "/api/home/summary"))["tiles"]["bankToMatch"]["count"] == 1
    with connect() as db:
        event = db.execute("""SELECT user_id,details::jsonb AS details FROM audit_logs
            WHERE tenant_id=%s AND resource=%s AND action='bank.recorded_document_voided'""", (tenant, line)).fetchone()
        assert event == {"user_id": users["admin"]["id"], "details": {"documentId": first["documentId"]}}
        assert db.execute("SELECT status FROM journal_headers WHERE id=%s", (first["journalId"],)).fetchone()["status"] == "reversed"
    second = ok(request("POST", expense, json=body), 201)
    assert all(second[key] != first[key] for key in ("documentId", "documentNumber", "journalId"))
    assert ok(request("POST", void, json={"status": "void"}))["idempotent"]
    assert ok(request("GET", f"{BANK}/{line}"))["recordedDocumentId"] == second["documentId"]


def test_void_migration_reopens_an_already_voided_link_once(company):
    connect, tenant, users, accounts, request = company
    line = insert_bank_line(company, amount="2.50")
    first = ok(request("POST", f"{BANK}/{line}/record-expense",
                       json={"ledgerAccountId": str(accounts["5000"])}), 201)
    migration = (ROOT / "database/migrations/20261126_bank_recorded_document_void.sql").read_text(encoding="utf-8")
    with connect() as db:
        reversal = erp.posting.reverse_in_transaction(db, users["admin"], first["journalId"], reason="Legacy void")
        db.execute("""UPDATE erp_document_headers SET status='void',voided_at=now(),reversal_journal_id=%s
            WHERE tenant_id=%s AND id=%s""", (reversal, tenant, first["documentId"]))
        with pytest.raises(psycopg.errors.RaiseException, match="link cannot be changed"), db.transaction():
            db.execute("UPDATE bank_transactions SET status='ignored',recorded_document_id=NULL WHERE id=%s", (line,))
        db.execute(migration)
        db.execute(migration)
        events = db.execute("""SELECT role,details::jsonb AS details FROM audit_logs
            WHERE tenant_id=%s AND resource=%s AND action='bank.recorded_document_voided'""", (tenant, line)).fetchall()
        assert events == [{"role": "system", "details": {"documentId": first["documentId"]}}]
    detail = ok(request("GET", f"{BANK}/{line}"))
    assert detail["status"] == "imported" and detail["recordedDocumentId"] is None


def test_cash_basis_includes_bank_money_documents_once_on_statement_date(company):
    connect, tenant, _, accounts, request = company
    fee = insert_bank_line(company, amount="2.50")
    interest = insert_bank_line(company, amount="3.25", direction="money_in")
    with connect() as db:
        legacy = db.execute("""INSERT INTO coa_accounts(tenant_id,code,name,account_class)
            VALUES (%s,'5999','Legacy fees','expense') RETURNING id""", (tenant,)).fetchone()["id"]
    ok(request("POST", f"{BANK}/{fee}/coding", json={"accountId": str(legacy), "reason": "Previous coding"}), 201)

    def report(day):
        return ok(request("GET", f"/api/ledger/profit-and-loss?basis=cash&granularity=day&fromDate={day}&toDate={day}"))

    assert [row["accountCode"] for row in report(DAY)["accounts"]] == ["5999"]
    ok(request("POST", f"{BANK}/{fee}/record-expense", json={"ledgerAccountId": str(accounts['5000'])}), 201)
    money_in = ok(request("POST", f"{BANK}/{interest}/record-expense",
                          json={"ledgerAccountId": str(accounts["4000"])}), 201)
    pnl = report(DAY)
    assert pnl["totals"] == [{"currency": "MYR", "income": "3.25", "expenses": "2.50", "netProfit": "0.75"}]
    assert {row["accountCode"]: row["amount"] for row in pnl["accounts"]} == {"4000": "3.25", "5000": "2.50"}
    assert [row["period"] for row in pnl["periods"]] == [DAY]
    assert report("2026-08-13")["accounts"] == []
    ok(request("POST", f"/api/erp/documents/money-in/{money_in['documentId']}/transition", json={"status": "void"}))
    assert [row["accountCode"] for row in report(DAY)["accounts"]] == ["5000"]


def test_record_expense_catalog_closed_period_and_interest(company):
    connect, tenant, _, accounts, request = company
    fee = {"ledgerAccountId": str(accounts["5000"])}
    unknown = insert_bank_line(company, reference="No catalog entry")
    closed = insert_bank_line(company, day=CLOSED)
    missing = request("POST", f"{BANK}/{unknown}/record-expense", json=fee)
    assert missing.status_code == 422 and missing.json()["detail"] == "Add this bank account in Catalog > Bank accounts first"
    assert request("POST", f"{BANK}/{closed}/record-expense", json=fee).status_code == 409
    interest = insert_bank_line(company, reference=" operating account ", amount="3.25", direction="money_in")
    assert request("POST", f"{BANK}/{interest}/record-expense", json=fee).status_code == 422
    result = ok(request("POST", f"{BANK}/{interest}/record-expense", "accountant",
                        json={"ledgerAccountId": str(accounts["4000"])}), 201)
    assert result["documentNumber"].startswith("MI-")
    assert {(line["code"], line["debit"], line["credit"]) for line in journal(connect, result["journalId"])[1]} == {
        ("1010", "3.25", "0.00"), ("4000", "0.00", "3.25")}
    with connect() as db:
        db.execute("""INSERT INTO erp_catalog_entries(tenant_id,kind,name,code,ledger_account_id)
            VALUES (%s,'bank-account','123456','ALT',%s)""", (tenant, accounts["1010"]))
    ambiguous = insert_bank_line(company)
    assert request("POST", f"{BANK}/{ambiguous}/record-expense", json=fee).json() == missing.json()


def test_match_many_partial_full_rollback_and_tenant_isolation(company):
    connect, _, users, _, request = company
    line = insert_bank_line(company)
    with connect() as db:
        bill_a = insert_record(db, users, total="90.00", amount_due="70.00")
        bill_b = insert_record(db, users, total="30.00")
        invoice = insert_record(db, users, category="accounts_receivable")
        foreign = insert_record(db, {"admin": users["outsider"], "accountant": users["outsider"]})

    def match(*pairs, actor="accountant", expected="0.00"):
        return request("POST", f"{BANK}/{line}/match-many", actor,
                       json={"expectedAllocated": expected,
                             "allocations": [{"recordId": record, "amount": amount} for record, amount in pairs]})

    assert match((bill_a, "10.00"), actor="outsider").status_code == 404
    assert match((bill_a, "10.00"), (foreign, "10.00")).status_code == 404
    assert match((bill_a, "10.00"), (invoice, "10.00")).status_code == 422
    assert match((bill_a, "71.00")).status_code == 409
    assert match((bill_a, "70.00"), (bill_b, "30.01")).status_code == 409
    with connect() as db:
        assert not db.execute("SELECT 1 FROM settlements WHERE source_id=%s", (line,)).fetchone()
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: match((bill_a, "30.00"), (bill_b, "10.00")), range(2)))
    success, retry = sorted(attempts, key=lambda response: response.status_code)
    first = ok(success, 201)
    assert retry.status_code == 409
    assert retry.json()["detail"] == "This bank line changed since you opened it. Refresh and check what is already matched."
    assert (first["allocated"], first["remaining"], first["bankTransactionStatus"]) == ("40.00", "60.00", "imported")
    detail = ok(request("GET", f"{BANK}/{line}"))
    assert (detail["matchStatus"], detail["allocatedAmount"], detail["remainingAmount"]) == ("partially_matched", "40.00", "60.00")
    partial, = ok(request("GET", f"{BANK}?status=partially_matched"))
    assert (partial["id"], partial["status"], partial["allocatedAmount"], partial["remainingAmount"]) == (line, "partially_matched", "40.00", "60.00")
    assert ok(request("GET", "/api/home/summary"))["tiles"]["bankToMatch"]["count"] == 1
    search = {row["recordId"]: row for row in ok(request("GET", f"{BANK}/{line}/match-search"))}
    assert search[bill_a]["remainingAmount"] == "40.00" and search[bill_b]["remainingAmount"] == "20.00"
    assert foreign not in search
    def allocate(record, amount):
        return ok(request("POST", "/api/ledger/allocations", "accountant",
                          json={"settlementId": first["settlementId"], "recordId": record, "amount": amount}), 201)

    allocate(bill_a, "40.00")
    assert ok(request("GET", f"{BANK}/{line}"))["status"] == "partially_matched"
    undo = allocate(bill_b, "20.00")["id"]
    matched = ok(request("GET", f"{BANK}/{line}"))
    assert (matched["status"], matched["matchStatus"], matched["allocatedAmount"], matched["remainingAmount"]) == (
        "matched", "matched", "100.00", "0.00")
    assert ok(request("GET", "/api/home/summary"))["tiles"]["bankToMatch"]["count"] == 0
    assert match((bill_a, "1.00")).status_code == 409
    ok(request("POST", f"/api/ledger/allocations/{undo}/reverse", "accountant"), 201)
    assert ok(request("GET", f"{BANK}/{line}"))["remainingAmount"] == "20.00"
    assert ok(request("GET", "/api/home/summary"))["tiles"]["bankToMatch"]["count"] == 1
    assert ok(match((bill_b, "20.00"), expected="80.00"), 201) == {
        "settlementId": first["settlementId"], "allocated": "100.00", "remaining": "0.00", "bankTransactionStatus": "matched",
    }
