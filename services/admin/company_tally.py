"""Cross-check one company's demo books against its fixed scenario (KNT, TKK, EHB or CDA).

    python services/admin/manage.py tally --company TKK      # or --all; read-only, safe on any database

Checks, at each of the three cut-offs: the trial balance balances; P&L by account equals the posted documents and
bank fees; AR and AP open items equal the documents less payments (ageing report); cash in hand; every statement
line is matched or deliberately left for the reconciliation; the reconciliation period balances. Once per company:
contacts with their full headers, products, tax codes, payment terms, quotation chains, typed bills, the Inbox
documents and their lanes, and the months that must be closed.

Team Inbox documents (`team-inbox`) carry their printed values in their read record; the ones that were posted are
added to the P&L and AP expectations, and each one must still sit in the lane it was created for. Run this right after
seeding or applying: once people work in the company, its books move on and the tally no longer applies.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from ai.synthetic.company_pack import ZERO
from services.admin.demo_company import SeedError
from services.admin.knt_tally import compare_ageing, expected_open, expected_pnl, require_equal, verify_bills, \
    verify_quotations

TEAM_MODE = "team_inbox_fixture"


def find_company(db, books):
    if books.code == "KNT":
        from services.admin.knt_seed import find_company as knt_find

        return knt_find(db)
    from services.admin.company_seed import find_company as pack_find

    return pack_find(db, books)


def verify_statement(db, books, tenant, cutoff, lines, opening, book, cumulative_unmatched):
    from services.routes.bank.routes import _period_result

    starts = cutoff.replace(day=1)
    month = cutoff.strftime("%Y-%m")
    bank_ref = books.company["bank_account_number"]
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
                OR (j.source_type='adjustment' AND j.idempotency_key=%s || t.reference))
        ) evidence ON true
        WHERE t.tenant_id=%s AND i.source_bank_account_ref=%s AND i.status='confirmed'
          AND t.posted_date BETWEEN %s AND %s ORDER BY t.posted_date,t.source_row""",
                         (f"{books.key_prefix}:purchase:", tenant, bank_ref, starts, cutoff)).fetchall()
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


def verify_catalogue(db, books, tenant):
    """Contacts with their full headers, the products, tax codes and payment terms, and that each is used."""
    accounts = {row["id"]: row["code"] for row in db.execute(
        "SELECT id,code FROM ledger_accounts WHERE tenant_id=%s", (tenant,)).fetchall()}
    for kind, table, column in (("customer", "customers", "customer_name"), ("supplier", "vendors", "vendor_name")):
        rows = {row["contact_code"]: row for row in db.execute(
            f"SELECT * FROM {table} WHERE tenant_id=%s", (tenant,)).fetchall()}
        wanted = {code for code, (each, *_rest) in books.contacts.items() if each == kind}
        require_equal(f"{kind} codes", set(rows), wanted)
        for code in wanted:
            _, name, account, address = books.contacts[code]
            head, row = books.contact_header(code), rows[code]
            expected = {column: name, "registration_number": head.ssm, "email": books.contact_email(code),
                        "phone": head.phone, "address": address, "city": head.city, "postcode": head.postcode,
                        "fields": {"tin": head.tin, "sstNumber": head.sst, "paymentTermDays": head.terms,
                                   "defaultAccountCode": account, "currency": "MYR", "category": head.category}}
            require_equal(f"{code} contact header", {key: row[key] for key in expected}, expected)
            if kind == "supplier":
                require_equal(f"{code} default expense account", accounts.get(row["default_expense_account_id"]), account)
    products = {row["sku"]: row for row in db.execute("SELECT * FROM erp_products WHERE tenant_id=%s", (tenant,)).fetchall()}
    require_equal("Product SKUs", set(products), {product.sku for product in books.products})
    for product in books.products:
        row = products[product.sku]
        require_equal(f"{product.sku} product", (
            row["name"], row["kind"], row["unit"], row["sale_price"], row["purchase_price"], row["default_tax_code"],
            accounts.get(row["income_account_id"]), accounts.get(row["expense_account_id"])),
            (product.name, product.kind, product.unit, product.sale, product.cost, product.tax_code,
             product.income, product.expense))
    for kind, wanted in (("tax-code", set(books.tax_codes)), ("payment-term", set(books.payment_terms))):
        have = {row["code"] for row in db.execute(
            "SELECT code FROM erp_catalog_entries WHERE tenant_id=%s AND kind=%s AND is_active", (tenant, kind)).fetchall()}
        require_equal(f"{kind} catalogue", have, wanted)
    require_equal("Cash-in-hand account", [row["code"] for row in db.execute(
        """SELECT e.code FROM erp_catalog_entries e JOIN ledger_accounts a ON a.id=e.ledger_account_id
        WHERE e.tenant_id=%s AND e.kind='bank-account' AND a.code=%s""",
        (tenant, books.cash_account["ledger"])).fetchall()], [books.cash_account["code"]])
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
                  {code for code, (kind, *_rest) in books.contacts.items() if kind == "customer"})


def expected_cash(books, cutoff, purchases, cash):
    spent = sum((doc.total for doc in purchases if doc.kind == "receipt" and doc.state == "posted"
                 and doc.issued <= cutoff), ZERO)
    return books.opening_cash - spent + sum((line.amount for line in cash if line.posted <= cutoff), ZERO)


def lane_sql():
    from services.routes.home.lanes import CURRENT_REVISION_PASSED, OPEN_EXCEPTION_ON_DOCUMENT

    return f"""CASE WHEN {OPEN_EXCEPTION_ON_DOCUMENT} THEN 'exception'
             WHEN d.status IN ('extracted','corrected') AND {CURRENT_REVISION_PASSED} THEN 'ready'
             WHEN d.status IN ('extracted','corrected') THEN 'to_review'
             ELSE d.status END"""


def team_documents(db, tenant):
    """Team Inbox documents: their gold values, lane now, assignee and whether a posted journal exists."""
    return db.execute(f"""SELECT d.id, d.assigned_to, d.status, x.raw_json->'gold' AS gold, {lane_sql()} AS lane,
        EXISTS(SELECT 1 FROM journal_headers j WHERE j.tenant_id=d.tenant_id AND j.source_id=d.id
            AND j.source_type IN ('document','adjustment') AND j.status='posted') AS posted
        FROM documents d JOIN raw_extractions x ON x.document_id=d.id AND x.is_current
        WHERE d.tenant_id=%s AND d.excluded_at IS NULL AND x.raw_json->>'mode'=%s""", (tenant, TEAM_MODE)).fetchall()


def verify_team_lanes(team):
    for row in team:
        gold = row["gold"]
        lane = "posted" if row["posted"] else row["lane"]
        require_equal(f"Team Inbox {gold['number'] or gold['file']} lane", lane, gold["lane"])
        require_equal(f"Team Inbox {gold['number'] or gold['file']} assignee", str(row["assigned_to"]), gold["assignee"])


def tally(db, books):
    import main  # noqa: F401  (route registrations before report helpers)
    from services.routes.reporting.financial import _account_balances, _ageing, _profit_and_loss

    tenant_row = find_company(db, books)
    if not tenant_row or tenant_row["tenant_code"] != books.code:
        raise SeedError(f"{books.code} is missing. Seed it first.")
    tenant = tenant_row["id"]
    sales, purchases, bank = books.books()
    extra_quotes, cash, bills = books.extras()
    verify_quotations(db, tenant, sales, extra_quotes)
    verify_bills(db, tenant, bills)
    verify_catalogue(db, books, tenant)
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
        {lane_sql()} AS inbox_state
        FROM documents d LEFT JOIN standardised_records r ON r.document_id=d.id AND r.tenant_id=d.tenant_id AND r.is_current
        WHERE d.tenant_id=%s AND d.excluded_at IS NULL AND NOT EXISTS (SELECT 1 FROM raw_extractions x
            WHERE x.document_id=d.id AND x.is_current AND x.raw_json->>'mode'=%s)""", (tenant, TEAM_MODE)).fetchall()}
    require_equal("Inbox files", set(scanned), {doc.filename for doc in purchases})
    for doc in purchases:
        row = scanned[doc.filename]
        require_equal(f"{doc.number} Inbox direction and state", (row["kind"], row["category"], row["posted"]),
                      (doc.kind, "accounts_payable", doc.state == "posted"))
        if row["posted"]:
            require_equal(f"{doc.number} posted purchase evidence",
                          (row["buyer_name"], row["subtotal"], row["tax_amount"], row["total_amount"]),
                          (books.company["name"], doc.subtotal, doc.tax, doc.total))
        else:
            require_equal(f"{doc.number} review lane", row["inbox_state"], doc.state)
    team = team_documents(db, tenant)
    verify_team_lanes(team)
    posted_team = [row["gold"] for row in team if row["posted"]]
    for code in getattr(books, "close_months", ()):
        status = db.execute("SELECT status FROM accounting_periods WHERE tenant_id=%s AND period_code=%s",
                            (tenant, code)).fetchone()
        require_equal(f"{code} period status", status["status"] if status else None, "closed")
    summaries = []
    opening = books.opening_bank
    unknown = ZERO
    for month, cutoff in books.months.items():
        start = cutoff.replace(day=1)
        balances = _account_balances(db, tenant, cutoff)
        net = {row["code"]: row["debit"] - row["credit"] for row in balances}
        debits = sum(max(amount, ZERO) for amount in net.values())
        credits = sum(max(-amount, ZERO) for amount in net.values())
        require_equal(f"{month} trial balance", debits, credits)
        pnl = _profit_and_loss(db, tenant, start, cutoff)
        actual_pnl = {account["code"]: account["amount"] for section in pnl["sections"] for account in section["accounts"]}
        expected = expected_pnl(month, sales, purchases + bills, bank)
        for gold in posted_team:
            if gold["date"][:7] == month:
                expected[gold["account"]] = expected.get(gold["account"], ZERO) + Decimal(gold["subtotal"])
        require_equal(f"{month} P&L by account against posted documents and bank fees", actual_pnl, expected)
        income = sum(amount for code, amount in expected.items() if code.startswith("4"))
        expenses = sum(amount for code, amount in expected.items() if not code.startswith("4"))
        require_equal(f"{month} P&L totals", (pnl["totalIncome"], pnl["totalExpenses"], pnl["netProfit"]),
                      (income, expenses, income - expenses))
        ar = compare_ageing(f"{month} AR", _ageing(db, tenant, cutoff, "receivables"),
                            expected_open(cutoff, sales, bank + cash), net.get("1100", ZERO), display_to_fixture)
        open_payables = expected_open(cutoff, purchases + bills, bank + cash)
        for gold in posted_team:
            if gold["date"] <= str(cutoff):
                open_payables[gold["number"]] = (_date(gold["date"]), _date(gold["due"]), Decimal(gold["total"]))
        ap = compare_ageing(f"{month} AP", _ageing(db, tenant, cutoff, "payables"), open_payables,
                            -net.get("2000", ZERO), {})
        require_equal(f"{month} cash in hand", net.get("1000", ZERO), expected_cash(books, cutoff, purchases, cash))
        lines = [line for line in bank if start <= line.posted <= cutoff]
        unknown += sum(line.amount for line in lines if line.kind == "unmatched")
        closing, unmatched = verify_statement(db, books, tenant, cutoff, lines, opening, net["1010"], unknown)
        summaries.append((cutoff, debits, pnl["netProfit"], ar, ap, net["1010"], closing, unmatched))
        opening = closing
    return summaries


def _date(value):
    from datetime import date

    return date.fromisoformat(value) if value else None


def lane_counts(db, books):
    """Team Inbox documents per assignee and lane, for the summary line."""
    tenant = find_company(db, books)
    if not tenant:
        return {}
    counts = defaultdict(lambda: defaultdict(int))
    for row in team_documents(db, tenant["id"]):
        counts[str(row["assigned_to"])]["posted" if row["posted"] else row["lane"]] += 1
    return counts


def print_tally(summaries, name="KNT demo books"):
    print(f"{name} | MYR | Sep-Nov 2026")
    print("Cutoff       TB each side   Net profit           AR           AP    Bank ledger   Bank statement   Unmatched")
    for cutoff, debits, profit, ar, ap, ledger, statement, unmatched in summaries:
        print(f"{cutoff} {debits:14,.2f} {profit:12,.2f} {ar:12,.2f} {ap:12,.2f}"
              f" {ledger:14,.2f} {statement:16,.2f} {unmatched:11}")
    print("PASS: trial balance, P&L, AR/AP open items and bank reconciliation for all three cutoffs.")
