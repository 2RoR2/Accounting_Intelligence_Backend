"""P3-lite and demo bug f: reconcile a bank line against the typed payment that already posted its cash.

Runs against a disposable loopback PostgreSQL database (TEST_DATABASE_URL), with the D5-D7 fixtures.
"""
import hashlib
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from tests.integration.test_uat_d5_d7_postgres import (  # noqa: F401  (pytest fixtures)
    BANK, DAY, company, database, insert_record, ok,
)

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "database/migrations/20261206_bank_document_reconciliation.sql"
DECISIONS = "/api/bank/reconciliation/decisions"


@pytest.fixture(scope="module", autouse=True)
def reconciliation_migration(database):
    # The D5-D7 fixture replays two older bank-guard migrations after the schema; apply this one last, twice.
    with database() as db:
        db.execute(MIGRATION.read_text(encoding="utf-8"))
        db.execute(MIGRATION.read_text(encoding="utf-8"))


def statement_line(company, *, amount, reference=None, description="IBG PAYMENT", day=DAY, direction="money_out",
                   account="123456"):
    connect, tenant, users, _, _ = company
    incoming = direction == "money_in"
    with connect() as db:
        batch = db.execute("""INSERT INTO bank_imports(tenant_id,source_bank_account_ref,source_hash,input_format,
            mapping_version,imported_by,status) VALUES (%s,%s,%s,'csv','v1',%s,'confirmed') RETURNING id""",
            (tenant, account, hashlib.sha256(uuid4().bytes).hexdigest(), users["admin"]["id"])).fetchone()["id"]
        return str(db.execute("""INSERT INTO bank_transactions(tenant_id,import_id,source_row,posted_date,description,reference,
            debit_decimal,credit_decimal,signed_amount_decimal,direction,currency,fingerprint)
            VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,'MYR',%s) RETURNING id""",
            (tenant, batch, day, description, reference, None if incoming else amount, amount if incoming else None,
             amount if incoming else "-" + amount, direction, hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()["id"])


def ids(company):
    connect, tenant, _, _, _ = company
    with connect() as db:
        vendor = db.execute("SELECT id FROM vendors WHERE tenant_id=%s ORDER BY vendor_name LIMIT 1", (tenant,)).fetchone()["id"]
        bank = db.execute("SELECT id FROM erp_catalog_entries WHERE tenant_id=%s AND kind='bank-account' AND code='OPS'",
                          (tenant,)).fetchone()["id"]
    return str(vendor), str(bank)


def payment_voucher(company, *, amount, record=None, day=DAY, bank_account=None, reference=None):
    _, _, _, _, request = company
    vendor, bank = ids(company)
    body = {"contactId": vendor, "documentDate": day, "amount": amount, "bankAccountId": bank_account or bank,
            "status": "ready"}
    if reference:
        body["referenceNo"] = reference
    if record:
        body["allocations"] = [{"targetId": record, "source": "scanned", "amount": amount}]
    return ok(request("POST", "/api/erp/documents/purchase-payment", json=body), 201)


def scanned_bill(company, *, total, reference):
    connect, _, users, _, _ = company
    vendor, _ = ids(company)
    with connect() as db:
        return insert_record(db, users, total=total, reference=reference, vendor_id=vendor,
                             supplier_name="Triple Six Point Enterprise 666")


def ledger_state(connect, tenant, line):
    with connect() as db:
        return db.execute("""SELECT
            (SELECT count(*) FROM journal_headers WHERE tenant_id=%(t)s AND status='posted') AS journals,
            (SELECT count(*) FROM settlement_allocations a JOIN settlements s ON s.id=a.settlement_id
                WHERE s.source_type='bank_import' AND s.source_id=%(l)s) AS bank_allocations,
            (SELECT status FROM bank_transactions WHERE id=%(l)s) AS line_status""", {"t": tenant, "l": line}).fetchone()


def test_bug_f_line_for_a_bill_paid_by_a_voucher_reconciles_against_the_voucher(company):
    connect, tenant, _, _, request = company
    record = scanned_bill(company, total="462.00", reference="INV-88123")
    voucher = payment_voucher(company, amount="462.00", record=record)
    number = voucher["number"]
    assert number.startswith("PV-")
    line = statement_line(company, amount="462.00", reference="INV-88123", description="IBG TRIPLE SIX POINT")
    before = ledger_state(connect, tenant, line)

    # The suggestion (as in the audit: the bill is proposed for the line) says why, and that the voucher paid it.
    ok(request("POST", "/api/bank/reconciliation/candidates", "accountant",
               json={"bankTransactionId": line, "recordId": record, "reasonCodes": ["EXACT_AMOUNT", "REFERENCE_MATCH"]}), 201)
    candidate, = ok(request("GET", f"{BANK}/{line}", "accountant"))["candidates"]
    assert candidate["matchLabel"] == "exact"
    assert candidate["matchReason"].startswith("Amount 462.00 and reference INV-88123 match")
    assert f"already paid by {number}" in candidate["matchReason"]
    assert candidate["settledBy"]["documentNumber"] == number and candidate["settledBy"]["documentLabel"] == "Payment voucher"
    preview = ok(request("GET", f"{BANK}/{line}/payment-preview", params={"candidateId": candidate["id"]}))
    assert not preview["canPost"] and number in preview["blockedReason"] and "no new journal" in preview["blockedReason"]

    # Confirming no longer fails with "Record allocation exceeds the unmatched amount": it reconciles.
    confirmed = ok(request("POST", DECISIONS, "accountant", json={
        "candidateId": candidate["id"], "decision": "confirmed", "expectedRevision": candidate["revision"]}), 201)
    linked = confirmed["reconciliation"]
    assert (linked["documentNumber"], linked["documentType"], linked["newJournal"]) == (number, "purchase-payment", False)
    assert linked["journalId"] == voucher["journalId"] and linked["matchLabel"] == "exact"
    after = ledger_state(connect, tenant, line)
    assert (after["journals"], after["bank_allocations"], after["line_status"]) == (before["journals"], 0, "matched")
    detail = ok(request("GET", f"{BANK}/{line}", "accountant"))
    assert detail["matchStatus"] == "matched" and detail["recordedDocumentNumber"] == number
    assert detail["recordedDocumentType"] == "purchase-payment"
    assert detail["reconciliation"]["documentNumber"] == number and detail["reconciliation"]["matchLabel"] == "exact"
    assert not detail["paymentPending"] and not detail["canPostPayment"] and detail["documentSuggestions"] == []
    assert ok(request("GET", f"{BANK}/{line}"))["canPostPayment"] is False
    assert request("POST", f"{BANK}/{line}/post-payment", json={}).status_code == 409
    # A double click returns the same reconciliation.
    again = ok(request("POST", DECISIONS, "accountant", json={
        "candidateId": candidate["id"], "decision": "confirmed", "expectedRevision": candidate["revision"]}), 201)
    assert again["reconciliation"]["reconciliationId"] == linked["reconciliationId"] and again["reconciliation"]["idempotent"]

    # Undo reopens the line; the voucher and its journal are untouched.
    undone = ok(request("POST", f"{BANK}/{line}/reconcile-document/undo", "accountant", json={"reason": "Wrong line"}))
    assert undone == {"bankTransactionId": line, "documentId": voucher["id"], "documentNumber": number,
                      "bankTransactionStatus": "imported"}
    reopened = ok(request("GET", f"{BANK}/{line}", "accountant"))
    assert reopened["matchStatus"] == "unmatched" and reopened["recordedDocumentId"] is None
    assert reopened["reconciliation"] is None
    assert ledger_state(connect, tenant, line)["journals"] == before["journals"]
    assert request("POST", f"{BANK}/{line}/reconcile-document/undo", "accountant", json={}).status_code == 409

    # Searching "PV" now finds the voucher, with the reason (the bill it paid is on the bank line).
    targets = ok(request("GET", f"{BANK}/{line}/match-targets", "accountant", params={"q": "PV"}))
    found, = [d for d in targets["documents"] if d["documentNumber"] == number]
    assert (found["action"], found["linkable"], found["matchLabel"]) == ("link", True, "exact")
    assert found["matchReason"] == f"Amount 462.00 and reference INV-88123 match (INV-88123 was paid by {number})"
    assert found["documentLabel"] == "Payment voucher" and found["party"] == "Triple Six Point Enterprise 666"
    suggestion, = ok(request("GET", f"{BANK}/{line}", "accountant"))["documentSuggestions"]
    assert suggestion["documentNumber"] == number and suggestion["matchLabel"] == "exact"
    # The bill itself shows as paid by the voucher; matching it the "several records" way also reconciles.
    bill, = [r for r in ok(request("GET", f"{BANK}/{line}/match-search", "accountant", params={"q": "INV-88123"}))]
    assert bill["action"] == "link" and bill["settledBy"]["documentNumber"] == number and bill["remainingAmount"] == "0.00"
    many = ok(request("POST", f"{BANK}/{line}/match-many", "accountant", json={
        "expectedAllocated": "0.00", "allocations": [{"recordId": record, "amount": "462.00"}]}), 201)
    assert many["bankTransactionStatus"] == "matched" and many["reconciliation"]["documentNumber"] == number
    assert ledger_state(connect, tenant, line)["bank_allocations"] == 0
    ok(request("POST", f"{BANK}/{line}/reconcile-document/undo", "accountant", json={}))

    # The direct action, as the search result's button sends it; repeated, it is idempotent.
    direct = ok(request("POST", f"{BANK}/{line}/reconcile-document", "accountant", json={"documentId": voucher["id"]}), 201)
    assert direct["matchReason"] == found["matchReason"] and not direct["idempotent"]
    repeat = ok(request("POST", f"{BANK}/{line}/reconcile-document", json={"documentId": voucher["id"]}), 201)
    assert repeat["idempotent"] and repeat["reconciliationId"] == direct["reconciliationId"]
    assert ledger_state(connect, tenant, line)["journals"] == before["journals"]
    with connect() as db:
        actions = [r["action"] for r in db.execute("""SELECT action FROM audit_logs WHERE tenant_id=%s AND resource=%s
            AND action LIKE 'bank.document_reconcil%%' ORDER BY created_at""", (tenant, line)).fetchall()]
    assert actions == ["bank.document_reconciled", "bank.document_reconciliation_undone"] * 2 + ["bank.document_reconciled"]


def test_reconcile_refusals_keep_the_line_and_the_ledger_unchanged(company):
    connect, tenant, users, accounts, request = company
    voucher = payment_voucher(company, amount="100.00", reference="SUP-100")
    short = statement_line(company, amount="99.00", reference="SUP-100")
    refused = request("POST", f"{BANK}/{short}/reconcile-document", "accountant", json={"documentId": voucher["id"]})
    assert refused.status_code == 409 and "is 100.00 but the bank line is 99.00" in refused.json()["detail"]
    target, = ok(request("GET", f"{BANK}/{short}/match-targets", "accountant", params={"q": voucher["number"]}))["documents"]
    assert target["matchLabel"] == "reference" and not target["linkable"] and "amounts differ" in target["matchReason"]

    incoming = statement_line(company, amount="100.00", direction="money_in")
    refused = request("POST", f"{BANK}/{incoming}/reconcile-document", "accountant", json={"documentId": voucher["id"]})
    assert refused.status_code == 409 and "money-in" in refused.json()["detail"]
    assert ok(request("GET", f"{BANK}/{incoming}/match-targets", "accountant", params={"q": "PV"}))["documents"] == []

    first, second = statement_line(company, amount="100.00"), statement_line(company, amount="100.00")
    ok(request("POST", f"{BANK}/{first}/reconcile-document", "accountant", json={"documentId": voucher["id"]}), 201)
    refused = request("POST", f"{BANK}/{second}/reconcile-document", "accountant", json={"documentId": voucher["id"]})
    assert refused.status_code == 409 and "already reconciled with another bank line" in refused.json()["detail"]
    assert request("POST", f"{BANK}/{first}/reconcile-document", "outsider",
                   json={"documentId": voucher["id"]}).status_code == 404

    # A voucher paid from another bank account does not explain this statement's line.
    with connect() as db:
        petty = db.execute("""INSERT INTO erp_catalog_entries(tenant_id,kind,name,code,ledger_account_id,data)
            VALUES (%s,'bank-account','Petty cash','PETTY',%s,'{}') RETURNING id""", (tenant, accounts["1010"])).fetchone()["id"]
    cash = payment_voucher(company, amount="100.00", bank_account=str(petty))
    refused = request("POST", f"{BANK}/{second}/reconcile-document", "accountant", json={"documentId": cash["id"]})
    assert refused.status_code == 409 and "Petty cash" in refused.json()["detail"]

    # A money document recorded from the line is not a reconciliation: undo refuses and points at voiding it.
    fee = statement_line(company, amount="2.50")
    ok(request("POST", f"{BANK}/{fee}/record-expense", "accountant", json={"ledgerAccountId": str(accounts["5000"])}), 201)
    refused = request("POST", f"{BANK}/{fee}/reconcile-document/undo", "accountant", json={})
    assert refused.status_code == 409 and "Void that document" in refused.json()["detail"]
    with pytest.raises(psycopg.errors.RaiseException, match="link cannot be changed"), connect() as db:
        db.execute("UPDATE bank_transactions SET status='imported',recorded_document_id=NULL WHERE id=%s", (fee,))

    # Voiding a reconciled voucher reopens its bank line.
    void = ok(request("POST", f"/api/erp/documents/purchase-payment/{voucher['id']}/transition",
                      json={"status": "void", "reason": "Duplicate voucher"}))
    assert void["status"] == "void"
    reopened = ok(request("GET", f"{BANK}/{first}", "accountant"))
    assert reopened["matchStatus"] == "unmatched" and reopened["reconciliation"] is None
    with connect() as db:
        link = db.execute("SELECT undone_by,undo_reason FROM bank_document_reconciliations WHERE bank_transaction_id=%s",
                          (first,)).fetchone()
    assert link == {"undone_by": users["admin"]["id"], "undo_reason": f"Voided {voucher['number']}"}


def test_suggestions_carry_a_label_and_a_reason(company):
    connect, tenant, _, _, request = company
    vendor, _ = ids(company)
    # amount+date: same amount, no reference, three days apart.
    nearby = payment_voucher(company, amount="75.40", day="2026-08-09")
    line = statement_line(company, amount="75.40", description="DUITNOW 88231")
    suggestion, = ok(request("GET", f"{BANK}/{line}", "accountant"))["documentSuggestions"]
    assert (suggestion["documentNumber"], suggestion["matchLabel"]) == (nearby["number"], "amount+date")
    assert suggestion["matchReason"] == "Amount 75.40 matches and the dates are 3 days apart"
    assert suggestion["linkable"] and suggestion["action"] == "link"

    # memory: an earlier "SELADAH CEMENT" line went to this supplier; a new one with the same amount as another
    # voucher a month away is suggested from that memory.
    earlier = payment_voucher(company, amount="10.00")
    old_line = statement_line(company, amount="10.00", description="IBG TRANSFER SELADAH CEMENT & CONC")
    ok(request("POST", f"{BANK}/{old_line}/reconcile-document", "accountant", json={"documentId": earlier["id"]}), 201)
    later = payment_voucher(company, amount="640.00", day="2026-08-28")
    new_line = statement_line(company, amount="640.00", description="IBG TRANSFER SELADAH CEMENT & CONC", day="2026-08-01")
    remembered, = ok(request("GET", f"{BANK}/{new_line}", "accountant"))["documentSuggestions"]
    assert (remembered["documentNumber"], remembered["matchLabel"]) == (later["number"], "memory")
    assert remembered["matchReason"] == ('Amount 640.00 matches, and earlier bank lines like "SELADAH CEMENT CONC" '
                                         "were matched to Triple Six Point Enterprise 666")

    # Open typed bills are listed so the accountant sees them, but they are paid through a voucher first.
    bill = ok(request("POST", "/api/erp/documents/bill", json={
        "contactId": vendor, "documentDate": DAY, "referenceNo": "TSP-2208", "status": "ready",
        "lines": [{"description": "Cement", "unitPrice": "640.00"}]}), 201)
    open_bill, = [d for d in ok(request("GET", f"{BANK}/{new_line}/match-targets", "accountant",
                                        params={"q": "TSP-2208"}))["documents"]]
    assert (open_bill["documentNumber"], open_bill["action"], open_bill["linkable"]) == (bill["number"], "pay", False)
    assert open_bill["outstandingAmount"] == "640.00" and "Record its payment voucher first" in open_bill["blockedReason"]
    refused = request("POST", f"{BANK}/{new_line}/reconcile-document", "accountant", json={"documentId": bill["id"]})
    assert refused.status_code == 409
    assert ledger_state(connect, tenant, new_line)["line_status"] == "imported"
