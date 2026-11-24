"""Cross-check the KNT ledger, reports, open items and imported statements.

The checks themselves are generic (services/admin/company_tally.py); this module keeps the pieces both share."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from ai.synthetic.knt_pack import MONTHS, ZERO
from services.admin.demo_company import SeedError


def require_equal(label, actual, expected):
    if actual != expected:
        raise SeedError(f"{label}: expected {expected}, got {actual}.")


def expected_open(cutoff, documents, bank):
    outstanding = {doc.number: (doc, doc.total) for doc in documents if doc.issued <= cutoff
                   and doc.state == "posted" and doc.kind in {"invoice", "bill", "debit-note"}}
    for doc in documents:
        if doc.kind == "credit-note" and doc.issued <= cutoff:
            target, balance = outstanding[doc.related]
            outstanding[doc.related] = target, balance - doc.total
    for line in bank:
        if line.posted <= cutoff:
            for number, amount in line.targets:
                if number in outstanding:
                    target, balance = outstanding[number]
                    outstanding[number] = target, balance - amount
    return {number: (doc.issued, doc.due, balance)
            for number, (doc, balance) in outstanding.items() if balance}


def expected_pnl(month, sales, purchases, bank):
    accounts = defaultdict(Decimal)
    for docs in (sales, purchases):
        for doc in docs:
            if f"{doc.issued:%Y-%m}" == month and doc.state == "posted":
                sign = -1 if doc.kind in {"credit_note", "credit-note"} else 1
                for account, amount in doc.amounts.items():
                    accounts[account] += sign * amount
    accounts["6230"] = -sum(line.amount for line in bank if line.kind == "fee" and f"{line.posted:%Y-%m}" == month)
    return dict(accounts)


def compare_ageing(label, report, open_items, control, display_to_fixture):
    rows = report["rows"]
    actual = {display_to_fixture.get(row["documentNumber"], row["documentNumber"]):
              (row["documentDate"], row["dueDate"], row["outstanding"]) for row in rows}
    require_equal(f"{label} item count", len(rows), len(open_items))
    require_equal(f"{label} items", actual, open_items)
    buckets = dict.fromkeys(report["buckets"], ZERO)
    for row in rows:
        buckets[row["bucket"]] += row["outstanding"]
    require_equal(f"{label} totals", report["totals"], {**buckets, "total": control})
    return control


def verify_quotations(db, tenant, sales, extra_quotes):
    """Every quotation, its outcome and status, the sales/delivery orders it passed through and the invoice it ended in."""
    rows = db.execute("""WITH RECURSIVE chain AS (
            SELECT q.id AS quote_id, l.target_document_id AS node, 1 AS depth
            FROM erp_document_headers q
            JOIN erp_document_links l ON l.tenant_id=q.tenant_id AND l.source_document_id=q.id AND l.link_type='transfer'
            WHERE q.tenant_id=%(tenant)s AND q.document_type='quotation'
          UNION ALL
            SELECT c.quote_id, l.target_document_id, c.depth+1 FROM chain c
            JOIN erp_document_links l ON l.tenant_id=%(tenant)s AND l.source_document_id=c.node AND l.link_type='transfer')
        SELECT q.document_number, q.status, q.due_date, q.payload->'fields'->>'quoteOutcome' AS outcome,
            c.depth, h.document_type, h.status AS step_status, h.payload->'fields'->>'fixtureNumber' AS invoice
        FROM erp_document_headers q LEFT JOIN chain c ON c.quote_id=q.id
        LEFT JOIN erp_document_headers h ON h.id=c.node AND h.tenant_id=q.tenant_id
        WHERE q.tenant_id=%(tenant)s AND q.document_type='quotation'
        ORDER BY q.document_number, c.depth""", {"tenant": tenant}).fetchall()
    actual, expiry = {}, {}
    for row in rows:
        outcome, status, steps, invoice = actual.get(row["document_number"], (row["outcome"], row["status"], (), None))
        if row["document_type"] == "invoice":
            invoice = row["invoice"]
        elif row["document_type"]:
            if row["step_status"] != "ready":
                raise SeedError(f"{row['document_number']} passed through an unissued {row['document_type']}.")
            steps += (row["document_type"],)
        actual[row["document_number"]] = (outcome, status, steps, invoice)
        expiry[row["document_number"]] = row["due_date"]
    # The original three accepted/expired pairs are checked as before; their expired partner never left "sent".
    expected = {doc.quote: ("accepted", "accepted", (), doc.number) for doc in sales if doc.quote and not doc.quoted}
    expected.update({doc.quote.replace("-01", "-02"): ("expired", "sent", (), None)
                     for doc in sales if doc.quote and not doc.quoted})
    expected.update({doc.quote: ("accepted", "accepted", doc.flow, doc.number) for doc in sales if doc.quoted})
    status = {"open": "sent", "expired": "expired", "accepted": "accepted"}
    expected.update({q.number: (q.outcome, status[q.outcome], q.via, None) for q in extra_quotes})
    require_equal("Quotation transfers, outcomes and order chains", actual, expected)
    end = list(MONTHS.values())[-1]
    lapsed = {number for number, (outcome, *_rest) in expected.items() if outcome == "expired"}
    require_equal("Expired quotations with recorded expiry",
                  {number for number in lapsed if expiry[number] <= end}, lapsed)
    orders = Counter(row["document_type"] for row in db.execute(
        """SELECT document_type FROM erp_document_headers WHERE tenant_id=%s AND status='ready'
        AND document_type IN ('sales-order','delivery-order')""", (tenant,)).fetchall())
    steps = Counter(step for doc in sales for step in doc.flow)
    steps.update(step for q in extra_quotes for step in q.via)
    require_equal("Sales orders and delivery orders issued", dict(orders), dict(steps))


def verify_bills(db, tenant, bills):
    """Every typed supplier bill is posted with the typed amounts; the original suppliers' bills are scanned instead."""
    rows = {row["fixture"]: row for row in db.execute(
        """SELECT payload->'fields'->>'fixtureNumber' AS fixture, document_date, due_date, subtotal, tax_amount,
        total_amount, status, journal_id FROM erp_document_headers WHERE tenant_id=%s AND document_type='bill'""",
        (tenant,)).fetchall()}
    require_equal("Typed supplier bills", set(rows), {bill.number for bill in bills})
    for bill in bills:
        row = rows[bill.number]
        require_equal(f"{bill.number} posted bill evidence",
                      (row["document_date"], row["due_date"], row["subtotal"], row["tax_amount"], row["total_amount"],
                       row["status"], bool(row["journal_id"])),
                      (bill.issued, bill.due, bill.subtotal, bill.tax, bill.total, "ready", True))


def tally(db):
    """KNT's tally: the generic company tally with the KNT books (see services/admin/company_tally.py)."""
    from ai.synthetic.sme_books import books_for
    from services.admin.company_tally import tally as company_tally

    return company_tally(db, books_for("KNT"))


def print_tally(summaries):
    from services.admin.company_tally import print_tally as company_print

    company_print(summaries, "KNT demo books")
