"""Cross-check the KNT ledger, reports, open items and imported statements."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from ai.synthetic.knt_extra import CASH_ACCOUNT, PAYMENT_TERMS, PRODUCTS, TAX_CODES
from ai.synthetic.knt_pack import COMPANY, CONTACTS, MONTHS, OPENING_BANK, OPENING_CASH, ZERO, books, contact_header, extras
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


def verify_catalogue(db, tenant, sales, extra_quotes):
    """Contacts with their full headers, the product catalogue, tax codes and payment terms, and that each is used."""
    accounts = {row["id"]: row["code"] for row in db.execute(
        "SELECT id,code FROM ledger_accounts WHERE tenant_id=%s", (tenant,)).fetchall()}
    for kind, table, column in (("customer", "customers", "customer_name"), ("supplier", "vendors", "vendor_name")):
        rows = {row["contact_code"]: row for row in db.execute(
            f"SELECT * FROM {table} WHERE tenant_id=%s", (tenant,)).fetchall()}
        wanted = {code for code, (each, *_rest) in CONTACTS.items() if each == kind}
        require_equal(f"{kind} codes", set(rows), wanted)
        for code in wanted:
            _, name, account, address = CONTACTS[code]
            head, row = contact_header(code), rows[code]
            expected = {column: name, "registration_number": head.ssm, "email": f"accounts@{code.lower()}.knt.test",
                        "phone": head.phone, "address": address, "city": head.city, "postcode": head.postcode,
                        "fields": {"tin": head.tin, "sstNumber": head.sst, "paymentTermDays": head.terms,
                                   "defaultAccountCode": account, "currency": "MYR", "category": head.category}}
            require_equal(f"{code} contact header", {key: row[key] for key in expected}, expected)
            if kind == "supplier":
                require_equal(f"{code} default expense account", accounts.get(row["default_expense_account_id"]), account)
    products = {row["sku"]: row for row in db.execute("SELECT * FROM erp_products WHERE tenant_id=%s", (tenant,)).fetchall()}
    require_equal("Product SKUs", set(products), {product.sku for product in PRODUCTS})
    for product in PRODUCTS:
        row = products[product.sku]
        require_equal(f"{product.sku} product", (
            row["name"], row["kind"], row["unit"], row["sale_price"], row["purchase_price"], row["default_tax_code"],
            accounts.get(row["income_account_id"]), accounts.get(row["expense_account_id"])),
            (product.name, product.kind, product.unit, product.sale, product.cost, product.tax_code,
             product.income, product.expense))
    for kind, wanted in (("tax-code", set(TAX_CODES)), ("payment-term", set(PAYMENT_TERMS))):
        have = {row["code"] for row in db.execute(
            "SELECT code FROM erp_catalog_entries WHERE tenant_id=%s AND kind=%s AND is_active", (tenant, kind)).fetchall()}
        require_equal(f"{kind} catalogue", have, wanted)
    require_equal("Cash-in-hand account", [row["code"] for row in db.execute(
        """SELECT e.code FROM erp_catalog_entries e JOIN ledger_accounts a ON a.id=e.ledger_account_id
        WHERE e.tenant_id=%s AND e.kind='bank-account' AND a.code=%s""", (tenant, CASH_ACCOUNT["ledger"])).fetchall()],
        [CASH_ACCOUNT["code"]])
    # Every product is on a document line and every customer has an invoice (typed quotations count as lines too).
    used = {row["item"] for row in db.execute(
        """SELECT DISTINCT line->>'itemId' AS item FROM erp_document_headers h,
        jsonb_array_elements(h.payload->'lines') AS line WHERE h.tenant_id=%s AND h.status<>'void'
        AND line->>'itemId' IS NOT NULL""", (tenant,)).fetchall()}
    require_equal("Products used on documents", {sku for sku, row in products.items() if str(row["id"]) in used},
                  set(products))
    invoiced = {row["contact_code"] for row in db.execute(
        """SELECT DISTINCT c.contact_code FROM erp_document_headers h JOIN customers c ON c.id=h.customer_id
        WHERE h.tenant_id=%s AND h.document_type='invoice' AND h.status='ready'""", (tenant,)).fetchall()}
    require_equal("Customers with an invoice", invoiced,
                  {code for code, (kind, *_rest) in CONTACTS.items() if kind == "customer"})


def expected_cash(cutoff, purchases, cash):
    """Cash in hand: the opening float, less cash receipts for purchases, plus collections, less cash payments."""
    spent = sum((doc.total for doc in purchases if doc.kind == "receipt" and doc.state == "posted"
                 and doc.issued <= cutoff), ZERO)
    return OPENING_CASH - spent + sum((line.amount for line in cash if line.posted <= cutoff), ZERO)


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
    extra_quotes, cash, bills = extras()
    verify_quotations(db, tenant, sales, extra_quotes)
    verify_bills(db, tenant, bills)
    end = list(MONTHS.values())[-1]
    verify_catalogue(db, tenant, sales, extra_quotes)
    typed = db.execute("""SELECT document_number, payload->'fields'->>'fixtureNumber' AS fixture, document_date,
        subtotal,tax_amount,total_amount,document_type,status,journal_id FROM erp_document_headers
        WHERE tenant_id=%s AND document_type IN ('invoice','credit-note','debit-note')""", (tenant,)).fetchall()
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
        WHERE d.tenant_id=%s AND d.excluded_at IS NULL""", (tenant,)).fetchall()}
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
        expected = expected_pnl(month, sales, purchases + bills, bank)
        require_equal(f"{month} P&L by account against posted documents and bank fees", actual_pnl, expected)
        income = sum(amount for code, amount in expected.items() if code.startswith("4"))
        expenses = sum(amount for code, amount in expected.items() if not code.startswith("4"))
        require_equal(f"{month} P&L totals", (pnl["totalIncome"], pnl["totalExpenses"], pnl["netProfit"]),
                      (income, expenses, income - expenses))
        ar = compare_ageing(f"{month} AR", _ageing(db, tenant, cutoff, "receivables"),
                            expected_open(cutoff, sales, bank + cash), net.get("1100", ZERO), display_to_fixture)
        ap = compare_ageing(f"{month} AP", _ageing(db, tenant, cutoff, "payables"),
                            expected_open(cutoff, purchases + bills, bank + cash), -net.get("2000", ZERO), {})
        require_equal(f"{month} cash in hand", net.get("1000", ZERO), expected_cash(cutoff, purchases, cash))
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
