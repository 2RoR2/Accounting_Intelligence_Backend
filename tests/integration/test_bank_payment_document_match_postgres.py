"""Pay by voucher first, see the bank line later: the line is matched to that voucher/receipt and nothing posts twice.

Runs against a disposable loopback PostgreSQL database (TEST_DATABASE_URL), with the D5-D7 fixtures.
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.integration.test_uat_d5_d7_postgres import (  # noqa: F401  (pytest fixtures)
    BANK, DAY, company, database, insert_record, ok,
)

pytestmark = pytest.mark.runtime_defaults
DECISIONS = "/api/bank/reconciliation/decisions"


def bank_account(connect, tenant):
    with connect() as db:
        return str(db.execute("""SELECT id FROM erp_catalog_entries WHERE tenant_id=%s AND kind='bank-account'
            AND code='OPS'""", (tenant,)).fetchone()["id"])


def vendor(connect, tenant):
    with connect() as db:
        return str(db.execute("SELECT id FROM vendors WHERE tenant_id=%s ORDER BY created_at LIMIT 1",
                              (tenant,)).fetchone()["id"])


def bank_line(company, *, amount="100.00", direction="money_out", day=DAY, description="Bank payment",
              reference=None, account_ref="123456"):
    connect, tenant, users, _, _ = company
    with connect() as db:
        batch = db.execute("""INSERT INTO bank_imports(tenant_id,source_bank_account_ref,source_hash,input_format,
            mapping_version,imported_by,status) VALUES (%s,%s,%s,'csv','v1',%s,'confirmed') RETURNING id""",
            (tenant, account_ref, hashlib.sha256(uuid4().bytes).hexdigest(), users["admin"]["id"])).fetchone()["id"]
        incoming = direction == "money_in"
        return str(db.execute("""INSERT INTO bank_transactions(tenant_id,import_id,source_row,posted_date,description,
            reference,debit_decimal,credit_decimal,signed_amount_decimal,direction,currency,fingerprint)
            VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,'MYR',%s) RETURNING id""",
            (tenant, batch, day, description, reference, None if incoming else amount, amount if incoming else None,
             amount if incoming else "-" + amount, direction, hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()["id"])


def voucher(company, *, amount="100.00", day="2026-08-10", record=None, bank=None):
    connect, tenant, _, _, request = company
    allocations = [{"targetId": record, "source": "scanned", "amount": amount}] if record else []
    return ok(request("POST", "/api/erp/documents/purchase-payment", json={
        "contactId": vendor(connect, tenant), "documentDate": day, "amount": amount,
        "bankAccountId": bank or bank_account(connect, tenant), "allocations": allocations, "status": "ready"}), 201)


def receipt(company, *, amount="250.00", day="2026-08-11"):
    connect, tenant, _, accounts, request = company
    with connect() as db:
        db.execute("""INSERT INTO ledger_accounts(tenant_id,code,name,account_type) VALUES (%s,'1100','Receivables','asset')
            ON CONFLICT DO NOTHING""", (tenant,))
        customer = db.execute("""INSERT INTO customers(tenant_id,customer_name) VALUES (%s,%s) RETURNING id""",
                              (tenant, f"Kedai Runcit Maju {uuid4().hex[:6]}")).fetchone()["id"]
    return ok(request("POST", "/api/erp/documents/payment", json={
        "contactId": str(customer), "documentDate": day, "amount": amount,
        "bankAccountId": bank_account(connect, tenant), "status": "ready"}), 201)


def match(request, line, document, actor="accountant"):
    return request("POST", f"{BANK}/{line}/match-payment-document", actor, json={"documentId": document})


def unmatch(request, line, actor="accountant", reason="Wrong voucher"):
    return request("POST", f"{BANK}/{line}/unmatch-payment-document", actor, json={"reason": reason})


def books(connect, tenant):
    """Posted journals, bank (1010) balance, and bank-import settlement allocations for the company."""
    with connect() as db:
        return dict(db.execute("""SELECT
            (SELECT count(*) FROM journal_headers WHERE tenant_id=%(t)s AND status='posted') AS journals,
            (SELECT COALESCE(SUM(l.debit-l.credit),0) FROM journal_lines l
                JOIN journal_headers j ON j.id=l.journal_id AND j.tenant_id=l.tenant_id
                JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
                WHERE l.tenant_id=%(t)s AND a.code='1010') AS bank_balance,
            (SELECT count(*) FROM settlement_allocations a JOIN settlements s ON s.id=a.settlement_id
                WHERE s.tenant_id=%(t)s AND s.source_type='bank_import') AS bank_allocations""",
                               {"t": tenant}).fetchone())


def audits(connect, line, action):
    with connect() as db:
        return db.execute("SELECT count(*) AS n FROM audit_logs WHERE resource=%s AND action=%s",
                          (line, action)).fetchone()["n"]


def test_voucher_then_bank_line_matches_without_posting_twice(company):
    connect, tenant, users, _, request = company
    with connect() as db:
        supplier = db.execute("SELECT id,vendor_name FROM vendors WHERE tenant_id=%s ORDER BY created_at LIMIT 1",
                              (tenant,)).fetchone()
        record = insert_record(db, users, total="100.00", reference="INV-7001", vendor_id=supplier["id"],
                               supplier_name=supplier["vendor_name"])
    pv = voucher(company, record=record)
    assert pv["posted"]
    before = books(connect, tenant)
    assert before["bank_balance"] == Decimal("-100.00")  # the voucher already paid the money out of 1010
    line = bank_line(company, description=f"IBG {pv['number']} TRIPLE SIX POINT", day="2026-08-12")
    # The bill also has a proposed candidate on this line (amount and reference agree) ...
    ok(request("POST", "/api/bank/reconciliation/candidates", "accountant",
               json={"bankTransactionId": line, "recordId": record, "reasonCodes": ["EXACT_AMOUNT"]}), 201)

    detail = ok(request("GET", f"{BANK}/{line}", "accountant"))
    suggestion, = detail["paymentCandidates"]
    assert suggestion["documentId"] == pv["id"] and suggestion["documentNumber"] == pv["number"]
    assert suggestion["documentLabel"] == "Payment voucher" and suggestion["daysApart"] == 2
    assert suggestion["explanation"][:4] == ["Same amount (MYR 100.00)", f"Reference {pv['number']} found on the bank line",
                                             "Payee Triple Six Point Enterprise 666 found in the description",
                                             "2 days apart"]
    assert "Already posted; confirming the match posts nothing" in suggestion["explanation"]
    # ... but the voucher already paid that bill, so the bill points to the voucher and ranks below it.
    bill, = detail["candidates"]
    assert bill["settledBy"]["documentNumber"] == pv["number"] and not bill["settledBy"]["bankMatched"]
    assert bill["score"] < suggestion["score"]
    assert any(text.startswith(f"Already paid by payment voucher {pv['number']}") for text in bill["explanation"])
    refused = request("POST", DECISIONS, "accountant", json={"candidateId": bill["id"], "decision": "confirmed",
                                                            "expectedRevision": bill["revision"]})
    assert refused.status_code == 409
    assert "already settled" in refused.json()["detail"] and f"Match the bank line to {pv['number']} instead" \
        in refused.json()["detail"]

    # The accountant confirms the voucher match: nothing is posted, the line is matched and links to the voucher.
    matched = ok(match(request, line, pv["id"]), 201)
    assert matched == {"bankTransactionId": line, "documentId": pv["id"], "documentType": "purchase-payment",
                       "documentNumber": pv["number"], "journalId": matched["journalId"],
                       "bankTransactionStatus": "matched", "postedJournal": False, "alreadyMatched": False}
    assert books(connect, tenant) == before
    detail = ok(request("GET", f"{BANK}/{line}", "accountant"))
    assert detail["matchStatus"] == "matched" and detail["matchedPaymentDocument"]
    assert detail["recordedDocumentId"] == pv["id"] and detail["recordedDocumentType"] == "purchase-payment"
    assert detail["paymentPosted"] and not detail["paymentPending"] and not detail["canPostPayment"]
    assert detail["paymentCandidates"] == [] and detail["candidates"][0]["status"] == "superseded"
    assert ok(request("GET", f"{BANK}/{line}/payment-preview"))["canPost"] is False
    assert request("POST", f"{BANK}/{line}/post-payment", json={}).status_code == 409
    assert request("POST", f"{BANK}/{line}/record-expense", "accountant",
                   json={"ledgerAccountId": str(uuid4())}).status_code == 409
    assert books(connect, tenant) == before

    # The voucher is used up: another line of the same amount neither suggests nor accepts it.
    other = bank_line(company, description=f"IBG {pv['number']}")
    assert ok(request("GET", f"{BANK}/{other}", "accountant"))["paymentCandidates"] == []
    taken = match(request, other, pv["id"])
    assert taken.status_code == 409 and "already matched to the bank line of 2026-08-12" in taken.json()["detail"]


def test_receipt_then_bank_line_money_in(company):
    connect, tenant, _, _, request = company
    official = receipt(company)
    before = books(connect, tenant)
    assert before["bank_balance"] == Decimal("250.00")
    line = bank_line(company, amount="250.00", direction="money_in", description="DUITNOW KEDAI RUNCIT MAJU",
                     day="2026-08-13")
    suggestion, = ok(request("GET", f"{BANK}/{line}", "accountant"))["paymentCandidates"]
    assert suggestion["documentLabel"] == "Official receipt" and suggestion["documentId"] == official["id"]
    assert "Payer" in suggestion["explanation"][1] and "2 days apart" in suggestion["explanation"]
    # A receipt never explains money out, and a voucher never explains money in.
    out_line = bank_line(company, amount="250.00", direction="money_out")
    assert ok(request("GET", f"{BANK}/{out_line}", "accountant"))["paymentCandidates"] == []
    assert match(request, out_line, official["id"]).status_code == 422

    ok(match(request, line, official["id"]), 201)
    assert books(connect, tenant) == before
    detail = ok(request("GET", f"{BANK}/{line}", "accountant"))
    assert detail["matchStatus"] == "matched" and detail["recordedDocumentNumber"] == official["number"]
    assert detail["recordedDocumentType"] == "payment" and detail["paymentPosted"]


def test_confirming_the_same_voucher_match_is_idempotent_under_concurrent_retries(company):
    connect, tenant, _, _, request = company
    pv = voucher(company)
    line = bank_line(company)
    before = books(connect, tenant)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = [ok(response, 201) for response in pool.map(lambda _: match(request, line, pv["id"]), range(5))]
    assert sorted(result["alreadyMatched"] for result in results) == [False, True, True, True, True]
    assert all(result["bankTransactionStatus"] == "matched" and not result["postedJournal"] for result in results)
    assert books(connect, tenant) == before
    assert audits(connect, line, "bank.payment_document_matched") == 1
    assert ok(match(request, line, pv["id"]), 201)["alreadyMatched"]  # a later retry too


def test_unmatch_returns_the_line_and_the_voucher_and_posts_or_reverses_nothing(company):
    connect, tenant, _, _, request = company
    pv = voucher(company)
    line = bank_line(company)
    ok(match(request, line, pv["id"]), 201)
    before = books(connect, tenant)

    assert request("POST", f"{BANK}/{line}/unmatch-payment-document", "accountant", json={}).status_code == 422
    undone = ok(unmatch(request, line))
    assert undone == {"bankTransactionId": line, "documentId": pv["id"], "documentNumber": pv["number"],
                      "bankTransactionStatus": "imported", "reversedJournal": False}
    assert books(connect, tenant) == before
    detail = ok(request("GET", f"{BANK}/{line}", "accountant"))
    assert detail["matchStatus"] == "unmatched" and detail["recordedDocumentId"] is None
    assert not detail["matchedPaymentDocument"] and not detail["paymentPosted"]
    assert [item["documentId"] for item in detail["paymentCandidates"]] == [pv["id"]]
    voucher_now = ok(request("GET", f"/api/erp/documents/purchase-payment/{pv['id']}"))
    assert voucher_now["status"] == "ready"  # the voucher and its journal are untouched
    assert unmatch(request, line).status_code == 409  # nothing left to undo
    assert audits(connect, line, "bank.payment_document_matched") == 1
    assert audits(connect, line, "bank.payment_document_unmatched") == 1

    # The line can be matched again; a completed reconciliation period then protects it.
    ok(match(request, line, pv["id"]), 201)
    period = ok(request("POST", "/api/bank/reconciliation/periods", json={
        "bankAccountRef": "123456", "startsOn": "2026-08-01", "endsOn": "2026-08-31", "openingBalance": "0.00",
        "closingBalance": "-100.00", "bookBalance": str(books(connect, tenant)["bank_balance"])}), 201)
    ok(request("POST", f"/api/bank/reconciliation/periods/{period['id']}/complete", json={}))
    locked = unmatch(request, line)
    assert locked.status_code == 409 and "completed reconciliation period" in locked.json()["detail"]


def test_voiding_a_matched_voucher_releases_its_bank_line(company):
    connect, tenant, _, _, request = company
    pv = voucher(company)
    line = bank_line(company)
    ok(match(request, line, pv["id"]), 201)
    ok(request("POST", f"/api/erp/documents/purchase-payment/{pv['id']}/transition",
               json={"status": "void", "reason": "Entered twice"}))
    detail = ok(request("GET", f"{BANK}/{line}", "accountant"))
    assert detail["matchStatus"] == "unmatched" and detail["recordedDocumentId"] is None
    assert books(connect, tenant)["bank_balance"] == Decimal("0.00")  # voucher journal reversed, line open again


def test_a_voucher_paid_from_another_bank_account_is_not_offered(company):
    connect, tenant, _, _, request = company
    with connect() as db:
        ledger = db.execute("""INSERT INTO ledger_accounts(tenant_id,code,name,account_type)
            VALUES (%s,'1020','Second bank','asset') RETURNING id""", (tenant,)).fetchone()["id"]
        second = str(db.execute("""INSERT INTO erp_catalog_entries(tenant_id,kind,name,code,ledger_account_id,data)
            VALUES (%s,'bank-account','Savings account','SAV',%s,'{}') RETURNING id""", (tenant, ledger)).fetchone()["id"])
    pv = voucher(company, bank=second)
    line = bank_line(company)  # statement of the operating account (123456 -> 1010)
    assert ok(request("GET", f"{BANK}/{line}", "accountant"))["paymentCandidates"] == []
    refused = match(request, line, pv["id"])
    assert refused.status_code == 409 and "another bank account" in refused.json()["detail"]


def test_a_record_added_by_hand_says_when_its_amount_differs(company):
    connect, _, users, _, request = company
    line = bank_line(company, amount="707.00")
    with connect() as db:
        bill = insert_record(db, users, total="1507.00")
    ok(request("POST", "/api/bank/reconciliation/candidates", "accountant",
               json={"bankTransactionId": line, "recordId": bill, "reasonCodes": ["MANUAL_SELECTION"]}), 201)
    candidate, = ok(request("GET", f"{BANK}/{line}", "accountant"))["candidates"]
    assert candidate["explanation"][0] == "Record total MYR 1507.00 differs from the bank line by MYR 800.00"
    assert candidate["amountDelta"] == "800.00"  # the Difference column agrees (it showed 0.00 before)
    assert candidate["score"] < 60  # ranks below any same-amount suggestion


def test_bill_match_still_posts_its_payment_and_the_period_reconciles_to_zero(company):
    """Mixed month: a voucher, a receipt and a bill matched and posted from the bank line. Books = statement."""
    connect, tenant, users, _, request = company
    pv = voucher(company, amount="100.00")
    official = receipt(company, amount="250.00")
    voucher_line = bank_line(company, amount="100.00", day="2026-08-12")
    receipt_line = bank_line(company, amount="250.00", direction="money_in", day="2026-08-13")
    bill_line = bank_line(company, amount="40.00", day="2026-08-14")
    with connect() as db:
        bill = insert_record(db, users, total="40.00")
    ok(request("POST", "/api/bank/reconciliation/candidates", "accountant",
               json={"bankTransactionId": bill_line, "recordId": bill, "reasonCodes": ["EXACT_AMOUNT"]}), 201)
    candidate, = ok(request("GET", f"{BANK}/{bill_line}"))["candidates"]
    assert candidate["explanation"][0] == "Same amount (MYR 40.00)" and candidate["settledBy"] is None

    ok(match(request, voucher_line, pv["id"]), 201)
    ok(match(request, receipt_line, official["id"]), 201)
    before = books(connect, tenant)
    # The existing bill match is unchanged: confirming it can post its payment (Dr payables / Cr bank) once.
    posted = ok(request("POST", DECISIONS, json={"candidateId": candidate["id"], "decision": "confirmed",
                                                 "expectedRevision": candidate["revision"], "postPayment": True}), 201)
    assert not posted["alreadyConfirmed"] and not posted["payment"]["idempotent"]
    after = books(connect, tenant)
    assert after["journals"] == before["journals"] + 1 and after["bank_allocations"] == before["bank_allocations"] + 1
    assert after["bank_balance"] == Decimal("110.00")  # -100 voucher +250 receipt -40 bill: each posted once

    # Statement: opening 0, -100 +250 -40 = closing 110. Book balance is the 1010 ledger balance.
    period = ok(request("POST", "/api/bank/reconciliation/periods", json={
        "bankAccountRef": "123456", "startsOn": "2026-08-01", "endsOn": "2026-08-31", "openingBalance": "0.00",
        "closingBalance": "110.00", "bookBalance": str(after["bank_balance"])}), 201)
    assert (period["moneyIn"], period["moneyOut"]) == ("250.00", "140.00")
    assert (period["statementDifference"], period["unexplainedDifference"], period["unaccountedLines"]) == \
        ("0.00", "0.00", 0)
    assert ok(request("POST", f"/api/bank/reconciliation/periods/{period['id']}/complete", json={}))["status"] == \
        "completed"
