"""Set-aside guarantees (25 Nov admin audit) against a disposable loopback PostgreSQL database.

1. A set-aside document leaves every list and count: All documents, To review, Exceptions, review lanes, the
   workload list, Today counts, month-end progress, reports, exports and search.
2. It is kept: the reason, the person and the time are stored and shown in the audit trail.
3. It is listed under Set aside, where only a local admin restores it, and the restore is audited.
4. Nothing posts from it: submission, approval, saving to records, posting and bank matching refuse it.

Uses the D5-D7 fixtures (TEST_DATABASE_URL).
"""
import json

import pytest
from psycopg.types.json import Jsonb

import main  # noqa: F401  (loads the app before its route modules: they import from main)

from services.routes.queue import routes as queue
from services.routes.reporting import routes as reporting
from services.routes.reporting import workload
from services.routes.review import lanes
from services.routes.review import routes as review
from services.routes.search import routes as search
from services.routes.validation import routes as validation
from services.routes.workspace import routes as workspace
from services.routes.effort import routes as effort
from services.set_aside import SET_ASIDE_DETAIL
from tests.integration.test_uat_d5_d7_postgres import (  # noqa: F401  (pytest fixtures)
    BANK, SCANNED, company, database, insert_bank_line, insert_record, ok, review_document,
)

pytestmark = pytest.mark.runtime_defaults
REASON = "Belongs to another company, not KNT"


@pytest.fixture
def books(company, monkeypatch):
    """The D5-D7 company, with every list module this test reads pointed at the test database."""
    for module in (queue, reporting, workload, effort, lanes, review, search, validation, workspace):
        monkeypatch.setattr(module, "connect", company[0])
    return company


def open_exception(connect, tenant, document):
    with connect() as db:
        db.execute("""INSERT INTO exceptions(document_id,tenant_id,type,severity,message)
            VALUES (%s,%s,'VALIDATION:NOT_ADDRESSED','high','Addressed to another company')""", (document, tenant))


def set_aside(request, document, actor="accountant", reason=REASON):
    return request("POST", f"/api/documents/{document}/exclude", actor, json={"reason": reason})


def ids(rows, key="id"):
    return {str(row[key]) for row in rows}


def views(request, document):
    """Every list and count the document can appear in, as (where it is listed, counts)."""
    listed = {
        "all documents (admin)": document in ids(ok(request("GET", "/api/documents"))),
        "all documents (accountant)": document in ids(ok(request("GET", "/api/documents", "accountant"))),
        "to review": document in ids(ok(request("GET", "/api/documents", "accountant", params={"lane": "to-review"}))),
        "needs fixing": document in ids(ok(request("GET", "/api/documents", params={"lane": "needs-fixing"}))),
        "exceptions": document in ids(ok(request("GET", "/api/exceptions")), "documentId"),
        "exceptions (accountant)": document in ids(ok(request("GET", "/api/exceptions", "accountant")), "documentId"),
        "review lanes": document in {item["id"] for lane in ok(request(
            "GET", "/api/review/lanes", "accountant", params={"stage": "submit"}))["lanes"].values() for item in lane},
        "accountant inbox": document in ids(ok(request("GET", "/api/accountant/inbox", "accountant"))),
        "workload": document in ids(ok(request("GET", "/api/workload")), "documentId"),
        "workload report": document in {row["id"] for row in ok(request("GET", "/api/reports/workload"))["documents"]},
        "search": document in {item.get("id") for group in ok(request("GET", "/api/search", params={"q": "scan"}))["groups"]
                               for item in group["items"]},
    }
    home = ok(request("GET", "/api/home/summary"))
    reports = ok(request("GET", "/api/reports/accounting"))
    counts = {
        "toReview": home["tiles"]["toReview"]["count"],
        "needsFixing": home["tiles"]["needsFixing"]["count"],
        "monthEndReviewedTotal": next(s for s in home["monthEnd"]["steps"] if s["key"] == "reviewed")["total"],
        "reportOpenExceptions": reports["summary"]["openExceptions"],
        "reportDocuments": len(reports["documents"]),
    }
    return listed, counts


def test_a_set_aside_document_leaves_every_list_and_count(books):
    connect, tenant, users, _, request = books
    wrong, kept = review_document(books)["documentId"], review_document(books)["documentId"]
    for document in (wrong, kept):
        open_exception(connect, tenant, document)
        ok(request("POST", f"/api/documents/{document}/workload", json={"userId": str(users["accountant"]["id"])}), 201)

    before, counts_before = views(request, wrong)
    missing = [name for name, present in before.items() if not present]
    assert not missing, f"the test document should start in every view: {missing}"

    ok(set_aside(request, wrong))

    after, counts_after = views(request, wrong)
    still_listed = [name for name, present in after.items() if present]
    assert not still_listed, f"a set-aside document is still listed in: {still_listed}"
    assert {name: counts_before[name] - counts_after[name] for name in counts_before} == {name: 1 for name in counts_before}
    # The other document is untouched.
    assert all(views(request, kept)[0].values())
    # The documents export leaves it out too.
    with connect() as db:
        codes = {row["id"]: row["document_code"] for row in db.execute(
            "SELECT id::text AS id,document_code FROM documents WHERE id = ANY(%s::uuid[])", ([wrong, kept],)).fetchall()}
        _, rows = reporting._export_rows(db, tenant, "documents")
        _, exceptions = reporting._export_rows(db, tenant, "exceptions")
    exported = {row[0] for row in rows}
    assert codes[kept] in exported and codes[wrong] not in exported
    assert len(exceptions) == 1  # only the kept document's exception


def test_a_set_aside_document_is_kept_with_reason_person_and_time_and_restored_by_an_admin(books):
    connect, tenant, users, _, request = books
    document = review_document(books)["documentId"]
    ok(set_aside(request, document))

    with connect() as db:
        row = db.execute("SELECT excluded_at,excluded_reason FROM documents WHERE id=%s", (document,)).fetchone()
    assert row["excluded_at"] and row["excluded_reason"] == REASON  # kept, not deleted
    listed, = [entry for entry in ok(request("GET", "/api/documents/excluded")) if entry["id"] == document]
    assert (listed["reason"], listed["excludedBy"]) == (REASON, users["accountant"]["name"]) and listed["excludedAt"]
    trail, = [entry for entry in ok(request("GET", "/api/audit", params={"resource": document}))
              if entry["action"] == "document.excluded"]
    assert trail["user"] == users["accountant"]["name"] and trail["timestamp"] and json.loads(trail["details"])["reason"] == REASON

    # An accountant cannot restore it; a local admin can, and the restore is audited with what it undid.
    refused = request("POST", f"/api/documents/{document}/restore", "accountant")
    assert refused.status_code == 403
    ok(request("POST", f"/api/documents/{document}/restore"))
    restored, = [entry for entry in ok(request("GET", "/api/audit", params={"resource": document}))
                 if entry["action"] == "document.restored"]
    assert restored["user"] == users["admin"]["name"] and json.loads(restored["details"])["previousReason"] == REASON
    assert document not in ids(ok(request("GET", "/api/documents/excluded")))
    assert document in ids(ok(request("GET", "/api/documents", "accountant")))
    assert request("POST", f"/api/documents/{document}/restore").status_code == 404


def test_nothing_can_be_submitted_approved_or_edited_while_set_aside(books):
    connect, tenant, users, _, request = books
    doc = review_document(books)
    document, revision = doc["documentId"], doc["expectedContentRevision"]
    ok(set_aside(request, document))

    def refused(response):
        assert response.status_code == 409 and response.json()["detail"] == SET_ASIDE_DETAIL, response.text

    refused(request("POST", f"/api/documents/{document}/review", "accountant",
                    json={"decision": "submitted", "note": "ready", "expectedContentRevision": revision}))
    bulk = ok(request("POST", "/api/documents/review/bulk-submit", "accountant",
                      json={"items": [{"documentId": document, "expectedContentRevision": revision}]}))
    assert bulk["results"] == [{"documentId": document, "ok": False, "reason": SET_ASIDE_DETAIL}]
    lanes_bulk = ok(request("POST", "/api/review/bulk-submit", "accountant",
                            json={"items": [{"documentId": document, "contentRevision": revision}]}))
    assert lanes_bulk["done"] == 0
    refused(request("PATCH", f"/api/documents/{document}/extraction/total", "accountant",
                    json={"value": "99.00", "expectedContentRevision": revision}))
    refused(request("POST", f"/api/documents/{document}/line-items", "accountant", json={
        "description": "Extra", "quantity": "1", "unitPrice": "1.00", "amount": "1.00", "sourcePage": 1,
        "sourceNote": "Added in the set-aside test", "expectedContentRevision": revision}))
    with connect() as db:
        assert db.execute("SELECT status FROM documents WHERE id=%s", (document,)).fetchone()["status"] != "in_review"
        assert not db.execute("SELECT 1 FROM review_submissions WHERE document_id=%s", (document,)).fetchone()

    # Even a document that reached "in review" before it was set aside (an old row) cannot be approved or saved.
    with connect() as db:
        db.execute("UPDATE documents SET status='in_review' WHERE id=%s", (document,))
        db.execute("""INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision)
            VALUES (%s,%s,%s,'submitted',%s)""", (document, tenant, users["accountant"]["id"], revision))
    refused(request("POST", f"/api/documents/{document}/review/decision",
                    json={"decision": "approved", "note": "ok", "expectedContentRevision": revision}))
    decided = ok(request("POST", "/api/documents/review/bulk-decision", json={
        "decision": "approved", "note": "ok", "items": [{"documentId": document, "expectedContentRevision": revision}]}))
    assert decided["results"][0]["ok"] is False
    approve = ok(request("POST", "/api/review/bulk-approve", json={
        "saveToRecords": True, "note": "ok", "items": [{"documentId": document, "contentRevision": revision}]}))
    assert approve["done"] == 0
    with connect() as db:
        db.execute("UPDATE documents SET status='validated' WHERE id=%s", (document,))
    refused(request("POST", f"/api/records/from-document/{document}"))
    with connect() as db:
        assert not db.execute("SELECT 1 FROM review_decisions WHERE document_id=%s", (document,)).fetchone()
        assert not db.execute("SELECT 1 FROM standardised_records WHERE document_id=%s", (document,)).fetchone()


def test_a_record_of_a_set_aside_document_does_not_post_or_match(books):
    """A record can belong to a set-aside document only from an old tidy-up (planm-tidy); it still must not move money."""
    connect, tenant, users, accounts, request = books
    with connect() as db:
        record = insert_record(db, users)
        other = insert_record(db, users)
        db.execute("""UPDATE documents SET excluded_at=now(), excluded_reason='Own sales copy (tidy-up)'
            WHERE id=(SELECT document_id FROM standardised_records WHERE id=%s)""", (record,))

    def refused(response):
        assert response.status_code == 409 and response.json()["detail"] == SET_ASIDE_DETAIL, response.text

    refused(request("GET", f"{SCANNED}/{record}/post-preview"))
    refused(request("POST", f"{SCANNED}/{record}/post", json={}))
    bulk, = ok(request("POST", f"{SCANNED}/post-bulk", json={"recordIds": [record]}))["results"]
    assert not bulk["ok"] and bulk["reason"] == SET_ASIDE_DETAIL
    with connect() as db:
        period = db.execute("SELECT id FROM accounting_periods WHERE tenant_id=%s AND status='open'", (tenant,)).fetchone()["id"]
    refused(request("POST", f"/api/ledger/journals/from-supplier-bill/{record}", json={
        "periodId": str(period), "expenseAccountId": str(accounts["5000"]), "payableAccountId": str(accounts["2000"]),
        "journalDate": "2026-08-12"}))
    assert record not in ids(ok(request("GET", "/api/records")))
    assert record not in ids(ok(request("GET", "/api/records", params={"posting": "to-post"})))
    with connect() as db:
        assert not db.execute("SELECT 1 FROM journal_headers WHERE tenant_id=%s", (tenant,)).fetchone()

    line = insert_bank_line(books)
    search_rows = ok(request("GET", f"{BANK}/{line}/match-search"))
    assert record not in {row["recordId"] for row in search_rows} and other in {row["recordId"] for row in search_rows}
    refused(request("POST", f"{BANK}/{line}/match-many", "accountant",
                    json={"expectedAllocated": "0.00", "allocations": [{"recordId": record, "amount": "10.00"}]}))
    with connect() as db:
        assert not db.execute("SELECT 1 FROM settlement_allocations WHERE record_id=%s", (record,)).fetchone()
        # A suggestion made before the tidy-up cannot be confirmed; rejecting it stays possible.
        candidate = db.execute("""INSERT INTO reconciliation_candidates(tenant_id,bank_transaction_id,record_id,reason_codes,proposed_by)
            VALUES (%s,%s,%s,%s,'rules') RETURNING id""", (tenant, line, record, Jsonb(["EXACT_AMOUNT"]))).fetchone()["id"]
    decision = {"candidateId": str(candidate), "expectedRevision": 1}
    refused(request("POST", f"/api/bank/reconciliation/candidates/{candidate}/decision",
                    json={**decision, "decision": "confirmed"}))
    ok(request("POST", f"/api/bank/reconciliation/candidates/{candidate}/decision",
               json={**decision, "decision": "rejected", "reason": "Set-aside document"}), 201)
