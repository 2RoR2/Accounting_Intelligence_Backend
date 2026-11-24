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


def insert_money_document(company, *, document_type="purchase-payment", amount="100.00", status="ready"):
    connect, tenant, users, _, _ = company
    with connect() as db:
        bank = db.execute("SELECT id FROM erp_catalog_entries WHERE tenant_id=%s AND kind='bank-account'", (tenant,)).fetchone()
        return str(db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,
                contact_name,document_date,currency,subtotal,total_amount,bank_account_id,created_by)
            VALUES (%s,%s,%s,%s,'Triple Six Point Enterprise 666',%s,'MYR',%s,%s,%s,%s) RETURNING id""",
            (tenant, document_type, f"PV-T-{uuid4().hex[:6]}", status, DAY, amount, amount, bank["id"],
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
