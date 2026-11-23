"""Cross-check the KNT ledger, reports, open items and imported statements."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from ai.synthetic.knt_pack import COMPANY, MONTHS, OPENING_BANK, ZERO, books
from services.admin.demo_company import SeedError
from services.admin.knt_seed import find_company


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
                accounts[doc.account] += sign * doc.subtotal
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


def verify_statement(db, tenant, cutoff, lines, opening, book, cumulative_unmatched, bank_ref):
    from services.routes.bank.routes import _period_result

    starts = cutoff.replace(day=1)
    month = cutoff.strftime("%Y-%m")
    expected_rows = []
    closing = opening
    for line in lines:
        closing += line.amount
        expected_rows.append((line.posted, line.reference, line.amount))
    imports = db.execute("""SELECT t.*,evidence.amount AS book_amount
        FROM bank_transactions t JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
        LEFT JOIN erp_document_headers h ON h.id=t.recorded_document_id AND h.tenant_id=t.tenant_id
        LEFT JOIN settlements s ON s.tenant_id=t.tenant_id AND s.source_type='bank_import' AND s.source_id=t.id
        LEFT JOIN LATERAL (
            SELECT sum(l.debit-l.credit) AS amount FROM journal_headers j
            JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id
            JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=j.tenant_id
            WHERE j.tenant_id=t.tenant_id AND j.status='posted' AND j.journal_date=t.posted_date AND a.code='1010'
              AND (j.id=h.journal_id OR (j.source_type='settlement' AND j.source_id=s.id)
                OR (j.source_type='adjustment' AND j.idempotency_key='knt:purchase:' || t.reference))
        ) evidence ON true
        WHERE t.tenant_id=%s AND i.source_bank_account_ref=%s AND i.status='confirmed'
          AND t.posted_date BETWEEN %s AND %s ORDER BY t.posted_date,t.source_row""",
                         (tenant, bank_ref, starts, cutoff)).fetchall()
    actual_rows = [(r["posted_date"], r["reference"], r["signed_amount_decimal"]) for r in imports]
    require_equal(f"{month} statement rows", actual_rows, expected_rows)
    unmatched = 0
    for row, line in zip(imports, lines):
        if line.kind == "unmatched":
            unmatched += 1
            if row["status"] not in {"imported", "needs_review", "proposed"}:
                raise SeedError(f"{line.reference} must remain unmatched.")
            require_equal(f"{line.reference} unposted movement", row["book_amount"], None)
            continue
        require_equal(f"{line.reference} bank state", row["status"], "matched")
        require_equal(f"{line.reference} posted bank movement", row["book_amount"], line.amount)
    period = db.execute("""SELECT * FROM bank_reconciliation_periods WHERE tenant_id=%s AND bank_account_ref=%s
        AND starts_on=%s AND ends_on=%s""", (tenant, bank_ref, starts, cutoff)).fetchone()
    if not period:
        raise SeedError(f"{month} bank reconciliation period is missing.")
    result = _period_result(db, period)
    for field, expected in (("openingBalance", opening), ("closingBalance", closing), ("bookBalance", book),
                            ("statementAdjustment", -cumulative_unmatched), ("bookAdjustment", ZERO),
                            ("statementDifference", ZERO), ("unexplainedDifference", ZERO)):
        require_equal(f"{month} reconciliation {field}", Decimal(result[field]), expected)
    require_equal(f"{month} unmatched statement lines", result["unaccountedLines"], unmatched)
    require_equal(f"{month} adjusted bank balance", book + cumulative_unmatched, closing)
    return closing, unmatched


def tally(db):
    # Bootstrap route registrations before importing their report helpers.
    import main  # noqa: F401
    from services.routes.home.lanes import CURRENT_REVISION_PASSED, OPEN_EXCEPTION_ON_DOCUMENT
    from services.routes.reporting.financial import _account_balances, _ageing, _profit_and_loss

    tenant_row = find_company(db)
    if not tenant_row or tenant_row["tenant_code"] != COMPANY["code"]:
        raise SeedError("KNT is missing. Run seed-knt or reset-knt first.")
    tenant = tenant_row["id"]
    sales, purchases, bank = books()
    quotes = db.execute("""SELECT q.document_number, q.due_date,q.payload->'fields'->>'quoteOutcome' AS outcome,
        h.payload->'fields'->>'fixtureNumber' AS invoice FROM erp_document_headers q
        LEFT JOIN erp_document_links l ON l.tenant_id=q.tenant_id AND l.source_document_id=q.id AND l.link_type='transfer'
        LEFT JOIN erp_document_headers h ON h.id=l.target_document_id AND h.tenant_id=q.tenant_id
        WHERE q.tenant_id=%s AND q.document_type='quotation'""", (tenant,)).fetchall()
    expected_quotes = {doc.quote: ("accepted", doc.number) for doc in sales if doc.quote}
    expected_quotes.update({doc.quote.replace("-01", "-02"): ("expired", None) for doc in sales if doc.quote})
    require_equal("Quotation transfers and outcomes",
                  {q["document_number"]: (q["outcome"], q["invoice"]) for q in quotes}, expected_quotes)
    end = list(MONTHS.values())[-1]
    require_equal("Expired quotations with recorded expiry", sum(q["outcome"] == "expired" and q["due_date"] <= end
                  for q in quotes), len(expected_quotes) // 2)
    typed = db.execute("""SELECT document_number, payload->'fields'->>'fixtureNumber' AS fixture, document_date,
        subtotal,tax_amount,total_amount,document_type,status,journal_id FROM erp_document_headers
        WHERE tenant_id=%s AND document_type IN ('invoice','credit-note')""", (tenant,)).fetchall()
    require_equal("Typed Sales documents", len(typed), len(sales))
    by_number = {row["fixture"]: row for row in typed}
    for doc in sales:
        row = by_number.get(doc.number)
        require_equal(f"{doc.number} posted sales evidence",
                      (row["document_date"], row["subtotal"], row["tax_amount"], row["total_amount"],
                       row["status"], bool(row["journal_id"])) if row else None,
                      (doc.issued, doc.subtotal, doc.tax, doc.total, "ready", True))
    display_to_fixture = {row["document_number"]: row["fixture"] for row in typed}
    scanned = {row["original_filename"]: row for row in db.execute(f"""SELECT d.original_filename,d.kind,d.category,
        r.buyer_name,r.subtotal,r.tax_amount,r.total_amount,
        EXISTS(SELECT 1 FROM journal_headers j WHERE j.tenant_id=d.tenant_id AND j.source_id=d.id
            AND j.source_type IN ('document','adjustment') AND j.status='posted') AS posted,
        CASE WHEN {OPEN_EXCEPTION_ON_DOCUMENT} THEN 'exception'
             WHEN d.status IN ('extracted','corrected') AND {CURRENT_REVISION_PASSED} THEN 'ready'
             WHEN d.status IN ('extracted','corrected') THEN 'to_review'
             ELSE d.status END AS inbox_state
        FROM documents d LEFT JOIN standardised_records r ON r.document_id=d.id AND r.tenant_id=d.tenant_id AND r.is_current
        WHERE d.tenant_id=%s""", (tenant,)).fetchall()}
    require_equal("Inbox files", set(scanned), {doc.filename for doc in purchases})
    for doc in purchases:
        row = scanned[doc.filename]
        require_equal(f"{doc.number} Inbox direction and state", (row["kind"], row["category"], row["posted"]),
                      (doc.kind, "accounts_payable", doc.state == "posted"))
        if row["posted"]:
            require_equal(f"{doc.number} posted purchase evidence",
                          (row["buyer_name"], row["subtotal"], row["tax_amount"], row["total_amount"]),
                          (COMPANY["name"], doc.subtotal, doc.tax, doc.total))
        else:
            require_equal(f"{doc.number} review lane", row["inbox_state"], doc.state)
    summaries = []
    opening = OPENING_BANK
    unknown = ZERO
    for month, cutoff in MONTHS.items():
        start = cutoff.replace(day=1)
        balances = _account_balances(db, tenant, cutoff)
        net = {row["code"]: row["debit"] - row["credit"] for row in balances}
        debits = sum(max(amount, ZERO) for amount in net.values())
        credits = sum(max(-amount, ZERO) for amount in net.values())
        require_equal(f"{month} trial balance", debits, credits)
        pnl = _profit_and_loss(db, tenant, start, cutoff)
        actual_pnl = {account["code"]: account["amount"] for section in pnl["sections"] for account in section["accounts"]}
        expected = expected_pnl(month, sales, purchases, bank)
        require_equal(f"{month} P&L by account against posted documents and bank fees", actual_pnl, expected)
        income = sum(amount for code, amount in expected.items() if code.startswith("4"))
        expenses = sum(amount for code, amount in expected.items() if not code.startswith("4"))
        require_equal(f"{month} P&L totals", (pnl["totalIncome"], pnl["totalExpenses"], pnl["netProfit"]),
                      (income, expenses, income - expenses))
        ar = compare_ageing(f"{month} AR", _ageing(db, tenant, cutoff, "receivables"),
                            expected_open(cutoff, sales, bank), net.get("1100", ZERO), display_to_fixture)
        ap = compare_ageing(f"{month} AP", _ageing(db, tenant, cutoff, "payables"),
                            expected_open(cutoff, purchases, bank), -net.get("2000", ZERO), {})
        lines = [line for line in bank if start <= line.posted <= cutoff]
        unknown += sum(line.amount for line in lines if line.kind == "unmatched")
        closing, unmatched = verify_statement(db, tenant, cutoff, lines, opening, net["1010"], unknown,
                                              COMPANY["bank_account_number"])
        summaries.append((cutoff, debits, pnl["netProfit"], ar, ap, net["1010"], closing, unmatched))
        opening = closing
    return summaries


def print_tally(summaries):
    print("KNT demo books | MYR | Sep-Nov 2026")
    print("Cutoff       TB each side   Net profit           AR           AP    Bank ledger   Bank statement   Unmatched")
    for cutoff, debits, profit, ar, ap, ledger, statement, unmatched in summaries:
        print(f"{cutoff} {debits:14,.2f} {profit:12,.2f} {ar:12,.2f} {ap:12,.2f}"
              f" {ledger:14,.2f} {statement:16,.2f} {unmatched:11}")
    print("PASS: trial balance, P&L, AR/AP open items and bank reconciliation for all three cutoffs.")
