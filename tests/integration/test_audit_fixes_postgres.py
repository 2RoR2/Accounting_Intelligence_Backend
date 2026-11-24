"""24 Nov demo audit fixes: add a field the reader did not store (a), say why approval is blocked (b),
and match a bank line to the money document that already recorded it (f).

Runs against a disposable loopback PostgreSQL database (TEST_DATABASE_URL), with the D5-D7 fixtures.
"""
from uuid import uuid4

import pytest

from tests.integration.test_uat_d5_d7_postgres import (  # noqa: F401  (pytest fixtures)
    BANK, DAY, company, database, insert_bank_line, ok, review_document,
)

pytestmark = pytest.mark.runtime_defaults


def stored_keys(connect, document):
    with connect() as db:
        return {row["field_key"]: row for row in db.execute("""SELECT fp.* FROM field_predictions fp
            JOIN raw_extractions re ON re.id=fp.extraction_id AND re.is_current WHERE fp.document_id=%s""",
                                                             (document,)).fetchall()}


def test_payment_terms_and_due_date_can_be_added_when_not_read(company):
    connect, _, _, _, request = company
    doc = review_document(company)
    document, revision = doc["documentId"], doc["expectedContentRevision"]
    assert "payment_terms" not in stored_keys(connect, document)

    added = ok(request("PATCH", f"/api/documents/{document}/extraction/payment_terms", "accountant",
                       json={"value": "NET 30", "expectedContentRevision": revision}))
    assert added["added"] and added["value"] == "NET 30" and added["contentRevision"] == revision + 1
    due = ok(request("PATCH", f"/api/documents/{document}/extraction/due", "accountant",
                     json={"value": "2026-09-11", "expectedContentRevision": revision + 1}))
    assert due["added"]
    rows = stored_keys(connect, document)
    assert rows["payment_terms"]["current_value"] == "NET 30" and rows["payment_terms"]["corrected_by"]
    assert rows["due"]["current_value"] == "2026-09-11"

    # Correcting it again updates the same row; no second row is created.
    again = ok(request("PATCH", f"/api/documents/{document}/extraction/payment_terms", "accountant",
                       json={"value": "NET 45", "expectedContentRevision": revision + 2}))
    assert not again["added"]
    # An alias of a stored field is refused: the document must not hold two dates.
    alias = request("PATCH", f"/api/documents/{document}/extraction/invoice_date", "accountant",
                    json={"value": "2026-08-01", "expectedContentRevision": revision + 3})
    assert alias.status_code == 409 and "Correct that field instead" in alias.json()["detail"]
    # A key that is correctable but not addable keeps the old answer.
    other = request("PATCH", f"/api/documents/{document}/extraction/late_fee", "accountant",
                    json={"value": "1.00", "expectedContentRevision": revision + 3})
    assert other.status_code == 404


def test_blocked_submission_names_what_blocks_it(company):
    _, _, _, _, request = company
    doc = review_document(company, blocked=True)
    refused = request("POST", f"/api/documents/{doc['documentId']}/review", "accountant",
                      json={"decision": "submitted", "note": "ready", "expectedContentRevision": doc["expectedContentRevision"]})
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail.startswith("Resolve the current validation findings") or "Save and revalidate" in detail


def insert_money_document(company, *, document_type="purchase-payment", amount="100.00", status="ready",
                          bank_account="OPS"):
    """A ready money document paid through the OPS bank account, another catalog account code, or none (None)."""
    connect, tenant, users, _, _ = company
    with connect() as db:
        bank = None
        if bank_account:
            bank = db.execute("SELECT id FROM erp_catalog_entries WHERE tenant_id=%s AND kind='bank-account' AND code=%s",
                              (tenant, bank_account)).fetchone()["id"]
        return str(db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,
                contact_name,document_date,currency,subtotal,total_amount,bank_account_id,created_by)
            VALUES (%s,%s,%s,%s,'Triple Six Point Enterprise 666',%s,'MYR',%s,%s,%s,%s) RETURNING id""",
            (tenant, document_type, f"PV-T-{uuid4().hex[:6]}", status, DAY, amount, amount, bank,
             users["admin"]["id"])).fetchone()["id"])


def test_bank_line_matches_the_payment_voucher_that_recorded_it(company):
    connect, _, _, _, request = company
    line = insert_bank_line(company)  # money out 100.00 on the OPS statement
    voucher = insert_money_document(company)
    other_amount = insert_money_document(company, amount="99.00")
    receipt = insert_money_document(company, document_type="payment")

    found = ok(request("GET", f"{BANK}/{line}/recorded-documents", "accountant"))
    by_id = {row["documentId"]: row for row in found}
    assert found[0]["documentId"] == voucher and by_id[voucher]["problem"] is None
    assert "differs" in by_id[other_amount]["problem"]
    assert receipt not in by_id  # a payment received cannot be the copy of money going out

    wrong = request("POST", f"{BANK}/{line}/match-document", "accountant", json={"documentId": other_amount})
    assert wrong.status_code == 409
    matched = ok(request("POST", f"{BANK}/{line}/match-document", "accountant", json={"documentId": voucher}))
    assert matched["status"] == "matched"
    with connect() as db:
        row = db.execute("SELECT status,recorded_document_id FROM bank_transactions WHERE id=%s", (line,)).fetchone()
        allocations = db.execute("""SELECT count(*) AS n FROM settlement_allocations a JOIN settlements s ON s.id=a.settlement_id
            WHERE s.source_type='bank_import' AND s.source_id=%s""", (line,)).fetchone()["n"]
    assert row["status"] == "matched" and str(row["recorded_document_id"]) == voucher
    assert allocations == 0  # nothing is allocated or posted a second time
    assert ok(request("GET", "/api/home/summary"))["tiles"]["paymentsToPost"]["count"] == 0

    # The same voucher cannot close a second line, and a matched line cannot be matched again.
    second = insert_bank_line(company)
    assert voucher not in {row["documentId"] for row in ok(request("GET", f"{BANK}/{second}/recorded-documents"))}
    assert request("POST", f"{BANK}/{second}/match-document", json={"documentId": voucher}).status_code == 409
    assert request("POST", f"{BANK}/{line}/match-document", json={"documentId": voucher}).status_code == 409


def test_journal_detail_shows_lines_and_its_source(company):
    connect, _, _, accounts, request = company
    fee = insert_bank_line(company, amount="2.50")
    expense = ok(request("POST", f"{BANK}/{fee}/record-expense", "accountant",
                         json={"ledgerAccountId": str(accounts["5000"]), "description": "Monthly fee"}), 201)
    detail = ok(request("GET", f"/api/ledger/journals/{expense['journalId']}", "accountant"))
    assert detail["balanced"] and detail["totalDebit"] == detail["totalCredit"] == "2.50"
    assert [(line["code"], line["debit"], line["credit"]) for line in detail["lines"]] == [
        ("5000", "2.50", "0.00"), ("1010", "0.00", "2.50")]
    assert detail["source"]["kind"] == "erp" and detail["source"]["number"] == expense["documentNumber"]
    assert detail["periodCode"] == "2026-08" and detail["reversedByJournalId"] is None
    assert request("GET", f"/api/ledger/journals/{uuid4()}").status_code == 404


def test_contact_balances_come_from_the_open_items(company):
    connect, tenant, users, _, request = company
    from tests.integration.test_uat_d5_d7_postgres import insert_record
    with connect() as db:
        vendor = db.execute("SELECT id FROM vendors WHERE tenant_id=%s", (tenant,)).fetchone()["id"]
        insert_record(db, users, total="150.00", supplier_name="Triple Six Point Enterprise 666")
        insert_record(db, users, total="40.00", supplier_name="Somebody Not In Contacts")
    balances = ok(request("GET", "/api/erp/contacts/balances", "accountant", params={"asOf": "2026-12-31"}))
    row, = [item for item in balances["contacts"] if item["contactId"] == str(vendor)]
    assert row["payable"] == "150.00" and row["receivable"] == "0" and row["openItems"] == 1
    assert row["overdue"] == "150.00"  # dated 12 Aug with no due date: overdue at the end of December
    assert {"contactName": "Somebody Not In Contacts", "side": "payable", "outstanding": "40.00"} in balances["unmatched"]
    assert request("GET", "/api/erp/contacts/balances", params={"asOf": "31/12/2026"}).status_code == 422


def test_a_wrong_upload_is_set_aside_with_a_reason_and_restored(company):
    connect, _, _, _, request = company
    doc = review_document(company)
    document = doc["documentId"]
    assert request("POST", f"/api/documents/{document}/exclude", "accountant", json={"reason": "no"}).status_code == 422
    set_aside = ok(request("POST", f"/api/documents/{document}/exclude", "accountant", json={"reason": "Duplicate photo of RC-1"}))
    assert set_aside["excluded"] and not set_aside["idempotent"]
    assert ok(request("POST", f"/api/documents/{document}/exclude", "accountant", json={"reason": "Duplicate photo of RC-1"}))["idempotent"]
    assert document not in {row["id"] for row in ok(request("GET", "/api/documents", "accountant"))}
    assert [row["reason"] for row in ok(request("GET", "/api/documents/excluded", "accountant"))] == ["Duplicate photo of RC-1"]
    detail = ok(request("GET", f"/api/documents/{document}", "accountant"))
    assert detail["excludedAt"] and detail["excludedReason"] == "Duplicate photo of RC-1"
    # Only a local admin restores, and the document is back in the list.
    assert request("POST", f"/api/documents/{document}/restore", "accountant").status_code == 403
    ok(request("POST", f"/api/documents/{document}/restore"))
    assert document in {row["id"] for row in ok(request("GET", "/api/documents", "accountant"))}
    # A colleague who neither owns nor is assigned the document cannot set it aside.
    assert request("POST", f"/api/documents/{document}/exclude", "colleague", json={"reason": "Not mine at all"}).status_code == 404


def test_a_submitted_document_cannot_be_set_aside(company):
    _, _, _, _, request = company
    doc = review_document(company)
    ok(request("POST", f"/api/documents/{doc['documentId']}/review", "accountant",
               json={"decision": "submitted", "note": "ready", "expectedContentRevision": doc["expectedContentRevision"]}))
    refused = request("POST", f"/api/documents/{doc['documentId']}/exclude", "accountant", json={"reason": "Changed my mind"})
    assert refused.status_code == 409 and "Return or reject it first" in refused.json()["detail"]


def match_rows(connect, line):
    with connect() as db:
        return db.execute("""SELECT document_id,unmatched_at,unmatch_reason FROM bank_document_matches
            WHERE bank_transaction_id=%s ORDER BY matched_at""", (line,)).fetchall()


def test_a_wrong_match_is_unmatched_with_a_reason_and_matched_again(company):
    import psycopg
    connect, _, _, _, request = company
    line = insert_bank_line(company)
    wrong, right = insert_money_document(company), insert_money_document(company)  # two RM 100 vouchers
    ok(request("POST", f"{BANK}/{line}/match-document", "accountant", json={"documentId": wrong}))
    detail = ok(request("GET", f"{BANK}/{line}", "accountant"))
    assert detail["recordedDocumentId"] == wrong and detail["recordedByMatch"] is True

    assert request("POST", f"{BANK}/{line}/unmatch-document", "accountant", json={"reason": "no"}).status_code == 422
    undone = ok(request("POST", f"{BANK}/{line}/unmatch-document", "accountant",
                        json={"reason": "Picked the wrong RM 100 voucher"}))
    assert undone["status"] == "imported"
    with connect() as db:
        row = db.execute("SELECT status,recorded_document_id FROM bank_transactions WHERE id=%s", (line,)).fetchone()
        audit = db.execute("SELECT details FROM audit_logs WHERE action='bank.unmatch_recorded_document' AND resource=%s",
                           (line,)).fetchone()
    assert row["status"] == "imported" and row["recorded_document_id"] is None
    assert "Picked the wrong RM 100 voucher" in audit["details"]
    # Unmatching twice is refused; the wrong voucher is free again and the right one can be matched.
    assert request("POST", f"{BANK}/{line}/unmatch-document", json={"reason": "Again, by mistake"}).status_code == 409
    assert wrong in {item["documentId"] for item in ok(request("GET", f"{BANK}/{line}/recorded-documents"))}
    ok(request("POST", f"{BANK}/{line}/match-document", "accountant", json={"documentId": right}))
    rows = match_rows(connect, line)
    assert [(str(r["document_id"]), r["unmatched_at"] is not None) for r in rows] == [(wrong, True), (right, False)]
    assert rows[0]["unmatch_reason"] == "Picked the wrong RM 100 voucher"
    # A match row is kept for audit: it cannot be deleted.
    with pytest.raises(psycopg.errors.RaiseException):
        with connect() as db:
            db.execute("DELETE FROM bank_document_matches WHERE bank_transaction_id=%s", (line,))


def test_a_line_recorded_as_a_money_document_still_cannot_be_unlinked(company):
    import psycopg
    connect, _, _, accounts, request = company
    fee = insert_bank_line(company, amount="2.50")
    expense = ok(request("POST", f"{BANK}/{fee}/record-expense", "accountant",
                         json={"ledgerAccountId": str(accounts["5000"]), "description": "Monthly fee"}), 201)
    assert ok(request("GET", f"{BANK}/{fee}"))["recordedByMatch"] is False
    refused = request("POST", f"{BANK}/{fee}/unmatch-document", json={"reason": "Trying to unlink it"})
    assert refused.status_code == 409 and "voiding that document" in refused.json()["detail"]
    with pytest.raises(psycopg.errors.RaiseException, match="cannot be changed"):
        with connect() as db:
            db.execute("UPDATE bank_transactions SET status='imported',recorded_document_id=NULL WHERE id=%s", (fee,))
    with connect() as db:
        linked = db.execute("SELECT recorded_document_id FROM bank_transactions WHERE id=%s", (fee,)).fetchone()
    assert str(linked["recorded_document_id"]) == expense["documentId"]


def test_both_bank_accounts_must_be_known_and_the_same(company):
    from psycopg.types.json import Jsonb
    connect, tenant, _, accounts, request = company
    with connect() as db:
        db.execute("""INSERT INTO erp_catalog_entries(tenant_id,kind,name,code,ledger_account_id,data)
            VALUES (%s,'bank-account','Cash in hand','CASH',%s,%s)""",
                   (tenant, accounts["1010"], Jsonb({"accountNumber": "CASH-1"})))
    line = insert_bank_line(company)
    cash = insert_money_document(company, bank_account="CASH")
    no_account = insert_money_document(company, bank_account=None)
    found = {row["documentId"]: row["problem"] for row in ok(request("GET", f"{BANK}/{line}/recorded-documents"))}
    assert "another bank account" in found[cash]
    assert "names no bank account" in found[no_account]
    for document in (cash, no_account):
        assert request("POST", f"{BANK}/{line}/match-document", json={"documentId": document}).status_code == 409
    # A statement line whose account is not known cannot be matched either, even to a voucher with an account.
    unknown = insert_bank_line(company, reference="NOT-A-KNOWN-ACCOUNT")
    voucher = insert_money_document(company)
    candidates = {row["documentId"]: row["problem"] for row in ok(request("GET", f"{BANK}/{unknown}/recorded-documents"))}
    assert "not known" in candidates[voucher]
    assert request("POST", f"{BANK}/{unknown}/match-document", json={"documentId": voucher}).status_code == 409


def test_a_matched_voucher_cannot_become_an_expense_and_voiding_it_reopens_the_line(company):
    connect, _, _, accounts, request = company
    line = insert_bank_line(company)
    voucher = insert_money_document(company)
    ok(request("POST", f"{BANK}/{line}/match-document", json={"documentId": voucher}))
    refused = request("POST", f"{BANK}/{line}/record-expense", json={"ledgerAccountId": str(accounts["5000"])})
    assert refused.status_code == 409 and "Unmatch it first" in refused.json()["detail"]
    voided = request("POST", f"/api/erp/documents/purchase-payment/{voucher}/transition",
                     json={"status": "void", "reason": "Entered twice"})
    assert voided.status_code == 200, voided.text
    with connect() as db:
        row = db.execute("SELECT status,recorded_document_id FROM bank_transactions WHERE id=%s", (line,)).fetchone()
    assert row["status"] == "imported" and row["recorded_document_id"] is None
    assert match_rows(connect, line)[0]["unmatch_reason"].startswith("Voided ")
