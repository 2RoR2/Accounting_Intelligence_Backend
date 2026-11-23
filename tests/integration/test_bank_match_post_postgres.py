"""P0 (UAT defect): confirming a bank match can post its payment in the same step, exactly once.

Runs against a disposable loopback PostgreSQL database (TEST_DATABASE_URL), with the D5-D7 fixtures.
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.integration.test_uat_d5_d7_postgres import (  # noqa: F401  (pytest fixtures)
    BANK, CLOSED, DAY, company, database, insert_bank_line, insert_record, journal, ok,
)

pytestmark = pytest.mark.runtime_defaults
DECISIONS = "/api/bank/reconciliation/decisions"


def proposed_match(company, *, day=DAY):
    """A money-out bank line with one proposed full-amount candidate for an approved bill."""
    connect, _, users, _, request = company
    line = insert_bank_line(company, day=day)
    with connect() as db:
        record = insert_record(db, users)
    ok(request("POST", "/api/bank/reconciliation/candidates", "accountant",
               json={"bankTransactionId": line, "recordId": record, "reasonCodes": ["EXACT_AMOUNT"]}), 201)
    candidate, = ok(request("GET", f"{BANK}/{line}"))["candidates"]
    assert candidate["status"] == "proposed"
    return line, candidate


def confirm(request, candidate, actor="admin", **extra):
    return request("POST", DECISIONS, actor, json={"candidateId": candidate["id"], "decision": "confirmed",
                                                   "expectedRevision": candidate["revision"], **extra})


def counts(connect, line, candidate):
    with connect() as db:
        return db.execute("""SELECT
            (SELECT count(*) FROM reconciliation_decisions WHERE candidate_id=%(c)s) AS decisions,
            (SELECT count(*) FROM settlement_allocations a JOIN settlements s ON s.id=a.settlement_id
                WHERE s.source_type='bank_import' AND s.source_id=%(t)s) AS allocations,
            (SELECT count(*) FROM journal_headers j JOIN settlements s ON s.id=j.source_id
                WHERE j.source_type='settlement' AND s.source_type='bank_import' AND s.source_id=%(t)s
                  AND j.status='posted') AS journals,
            (SELECT status FROM reconciliation_candidates WHERE id=%(c)s) AS candidate""",
                          {"c": candidate["id"], "t": line}).fetchone()


def payments_to_post(request, actor="admin"):
    return ok(request("GET", "/api/home/summary", actor))["tiles"]["paymentsToPost"]["count"]


def test_confirm_and_post_posts_once_under_five_concurrent_requests(company):
    connect, _, _, _, request = company
    line, candidate = proposed_match(company)
    preview = ok(request("GET", f"{BANK}/{line}/payment-preview", params={"candidateId": candidate["id"]}))
    assert preview["canPost"] and preview["journalDate"] == DAY and preview["periodCode"] == "2026-08"
    assert {row["code"]: (row["debit"], row["credit"]) for row in preview["lines"]} == {
        "2000": ("100.00", "0.00"), "1010": ("0.00", "100.00")}
    assert counts(connect, line, candidate)["journals"] == 0  # the preview writes nothing

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: confirm(request, candidate, postPayment=True), range(5)))
    results = [ok(response, 201) for response in responses]
    first = [result for result in results if not result["alreadyConfirmed"]]
    retries = [result for result in results if result["alreadyConfirmed"]]
    assert len(first) == 1 and len(retries) == 4
    posted = first[0]["payment"]
    assert not posted["idempotent"]
    assert all(retry["payment"] == {**posted, "idempotent": True} for retry in retries)
    assert journal(connect, posted["journalId"]) == (DAY, posted["lines"])
    assert posted["lines"] == preview["lines"]
    assert dict(counts(connect, line, candidate)) == {"decisions": 1, "allocations": 1, "journals": 1,
                                                      "candidate": "confirmed"}

    detail = ok(request("GET", f"{BANK}/{line}"))
    assert detail["matchStatus"] == "matched" and detail["paymentPosted"]
    assert not detail["paymentPending"] and not detail["canPostPayment"]
    assert payments_to_post(request) == 0
    # A later retry still returns the one journal; reversing a posted match stays refused as before.
    again = ok(confirm(request, candidate, postPayment=True), 201)
    assert again["alreadyConfirmed"] and again["payment"]["journalId"] == posted["journalId"]
    reverse = request("POST", DECISIONS, json={"candidateId": candidate["id"], "decision": "reversed",
                                               "expectedRevision": candidate["revision"], "reason": "Wrong bill"})
    assert reverse.status_code == 409
    assert counts(connect, line, candidate)["journals"] == 1


def test_accountant_cannot_post_and_the_match_waits_for_an_admin(company):
    connect, _, _, _, request = company
    line, candidate = proposed_match(company)
    refused = confirm(request, candidate, "accountant", postPayment=True)
    assert refused.status_code == 403
    assert dict(counts(connect, line, candidate)) == {"decisions": 0, "allocations": 0, "journals": 0,
                                                      "candidate": "proposed"}
    preview = ok(request("GET", f"{BANK}/{line}/payment-preview", "accountant",
                         params={"candidateId": candidate["id"]}))
    assert not preview["canPost"] and "local admin" in preview["blockedReason"]
    assert request("POST", f"{BANK}/{line}/post-payment", "accountant", json={}).status_code == 403

    decided = ok(confirm(request, candidate, "accountant"), 201)
    assert "payment" not in decided  # confirm only: nothing posted, no silent auto-posting
    waiting = ok(request("GET", f"{BANK}/{line}", "accountant"))
    assert waiting["matchStatus"] == "matched" and waiting["paymentPending"]
    assert not waiting["paymentPosted"] and not waiting["canPostPayment"]
    listed, = [row for row in ok(request("GET", f"{BANK}?direction=money_out", "accountant")) if row["id"] == line]
    assert listed["paymentPending"]
    assert counts(connect, line, candidate)["journals"] == 0
    assert payments_to_post(request) == 1 and payments_to_post(request, "accountant") == 1

    # The admin sees the same line in their to-post list, previews it, and posts it once.
    admin_view = ok(request("GET", f"{BANK}/{line}"))
    assert admin_view["canPostPayment"] and admin_view["paymentPending"]
    preview = ok(request("GET", f"{BANK}/{line}/payment-preview"))
    assert preview["canPost"] and len(preview["lines"]) == 2
    posted = ok(request("POST", f"{BANK}/{line}/post-payment", json={}), 201)
    assert posted["lines"] == preview["lines"] and not posted["idempotent"]
    assert payments_to_post(request) == 0
    assert not ok(request("GET", f"{BANK}/{line}", "accountant"))["paymentPending"]


def test_confirm_and_post_refused_in_a_locked_period_leaves_the_match_unconfirmed(company):
    connect, _, _, _, request = company
    line, candidate = proposed_match(company, day=CLOSED)
    preview = ok(request("GET", f"{BANK}/{line}/payment-preview", params={"candidateId": candidate["id"]}))
    assert not preview["canPost"] and "closed" in preview["blockedReason"] and preview["lines"] == []

    refused = confirm(request, candidate, postPayment=True)
    assert refused.status_code == 409 and "closed" in refused.json()["detail"]
    # One transaction: the refused posting also leaves the match unconfirmed.
    assert dict(counts(connect, line, candidate)) == {"decisions": 0, "allocations": 0, "journals": 0,
                                                      "candidate": "proposed"}
    assert ok(request("GET", f"{BANK}/{line}"))["matchStatus"] == "proposed"

    # "Confirm match only" stays available; the line then waits to be posted into an open period.
    ok(confirm(request, candidate), 201)
    detail = ok(request("GET", f"{BANK}/{line}"))
    assert detail["matchStatus"] == "matched" and detail["paymentPending"] and not detail["paymentPosted"]
    closed = request("POST", f"{BANK}/{line}/post-payment", json={})
    assert closed.status_code == 409 and "closed" in closed.json()["detail"]
    assert counts(connect, line, candidate)["journals"] == 0


def test_post_payment_flag_needs_a_confirm_decision(company):
    _, _, _, _, request = company
    _, candidate = proposed_match(company)
    response = request("POST", DECISIONS, json={"candidateId": candidate["id"], "decision": "rejected",
                                                "expectedRevision": candidate["revision"], "reason": "No",
                                                "postPayment": True})
    assert response.status_code == 422
