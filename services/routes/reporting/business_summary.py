"""Owner's business summary: one page of headline figures built only from posted books.

Nothing here posts or re-derives accounting. Sales, expenses and profit come from the existing
profit and loss, bank balances from the existing account balances (the balance sheet's figures),
owed to us / by us from the existing ageing, and recorded tax from the existing tax summary.
The breakdown tables read the same posted journals, so each one adds up to its headline figure
(anything that cannot be attributed to a customer, supplier or product is shown as its own row).
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date as Date
from decimal import Decimal

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder

from main import connect
from services.routes.accounting.journal_sources import (
    JOURNAL_SOURCE_ID,
    JOURNAL_SOURCE_TYPE,
    OPENING_JOURNAL,
)
from services.routes.erp.common import money
from services.routes.home.lanes import company_today
from services.routes.reporting import financial
from services.routes.reporting.financial import (
    POSTED_JOURNALS,
    POSTED_LINES,
    ZERO,
    Sheet,
)

TOP = 5
OVERDUE_LIMIT = 10
TREND_MONTHS = 6
RECORDED_TAX_NOTE = "Recorded tax, not a tax return"
PROFIT_NOTE = "Profit figures exclude recorded tax."
OTHER_CUSTOMER = "Other sales (no customer named)"
OTHER_SUPPLIER = "Other costs (no supplier named)"
NOT_ITEMISED = "Not itemised by product"
# Cost of sales is read from the account's report section, which the chart import keeps.
COST_OF_SALES_SECTION = "cost of"
# Cash and bank accounts. The ledger has no cash/bank account type (asset, liability, equity, income, expense only), so
# an asset account counts when it is linked to a bank-account catalog entry, is the bank posting account, sits in a
# cash or bank report section, or names a bank or cash account anywhere in its name ("Maybank Current Account",
# "Petty cash", "CIMB savings account"); not only when the name starts with Bank or Cash.
CASH_NAME_PATTERN = r"(bank|cash|current account|current a/c|savings account|savings a/c|fixed deposit)"


def _month_bounds(month: str | None, today: Date):
    if not month:
        return today.replace(day=1), today
    try:
        year, number = (int(part) for part in month.split("-"))
        first = Date(year, number, 1)
    except (ValueError, TypeError):
        raise HTTPException(422, "month must be in YYYY-MM format.") from None
    if month != f"{first:%Y-%m}":
        raise HTTPException(422, "month must be in YYYY-MM format.")
    if first > today:
        raise HTTPException(422, "Choose this month or an earlier month.")
    last = first.replace(day=monthrange(first.year, first.month)[1])
    return first, min(last, today)


def _shift_month(first: Date, months: int) -> Date:
    index = first.year * 12 + first.month - 1 + months
    return Date(index // 12, index % 12 + 1, 1)


def _cash_accounts(db, tenant):
    return db.execute("""WITH catalog AS (
            SELECT DISTINCT ON (ledger_account_id) ledger_account_id,name
            FROM erp_catalog_entries
            WHERE tenant_id=%(tenant)s AND kind='bank-account' AND ledger_account_id IS NOT NULL
            ORDER BY ledger_account_id,is_active DESC,created_at,id
        ), bank_posting AS (
            SELECT COALESCE(
                (SELECT account_id FROM erp_posting_accounts WHERE tenant_id=%(tenant)s AND purpose='bank'),
                (SELECT id FROM ledger_accounts WHERE tenant_id=%(tenant)s AND code=%(bank_code)s
                    AND account_type='asset')) AS id
        )
        SELECT a.id AS account_id,a.code,a.name AS ledger_name,c.name AS catalog_name,
            (c.ledger_account_id IS NOT NULL) AS in_catalog
        FROM ledger_accounts a
        LEFT JOIN catalog c ON c.ledger_account_id=a.id
        WHERE a.tenant_id=%(tenant)s AND a.account_type='asset'
            AND (c.ledger_account_id IS NOT NULL OR a.id=(SELECT id FROM bank_posting)
                OR a.name ~* %(pattern)s OR COALESCE(a.report_section,'') ~* %(pattern)s)
        ORDER BY a.code""", {"tenant": tenant, "bank_code": financial.DEFAULT_CODES["bank"],
                             "pattern": CASH_NAME_PATTERN}).fetchall()


def _bank_balances(as_of, balances, cash_accounts):
    """Balance per cash/bank account: the same account balances the balance sheet uses."""
    accounts = []
    for account in cash_accounts:
        balance = balances.get(account["account_id"], ZERO)
        if balance == ZERO and not account["in_catalog"]:
            continue
        accounts.append({"accountId": str(account["account_id"]), "code": account["code"],
                         "name": account["catalog_name"] or account["ledger_name"], "balance": balance})
    return {"asOf": as_of, "accounts": accounts, "total": sum((a["balance"] for a in accounts), ZERO)}


def _cash_movement(db, tenant, start, end, cash_accounts):
    """Money in and out of cash/bank accounts, entry by entry, so a transfer between two of them is neither."""
    ids = [account["account_id"] for account in cash_accounts]
    if not ids:
        return {"cashIn": ZERO, "cashOut": ZERO}
    row = db.execute(f"""WITH moves AS (
            SELECT j.id,SUM(l.debit-l.credit) AS net
            {POSTED_LINES}
            WHERE {POSTED_JOURNALS} AND j.journal_date BETWEEN %(start)s AND %(end)s
                AND l.account_id=ANY(%(accounts)s) AND NOT ({OPENING_JOURNAL})
            GROUP BY j.id
        )
        SELECT COALESCE(SUM(net) FILTER (WHERE net>0),0) AS cash_in,
            COALESCE(-SUM(net) FILTER (WHERE net<0),0) AS cash_out FROM moves""",
                     {"tenant": tenant, "start": start, "end": end, "accounts": ids}).fetchone()
    return {"cashIn": ZERO + row["cash_in"], "cashOut": ZERO + row["cash_out"]}


def _is_cost_of_sales(section):
    return (section["label"] or "").strip().lower().startswith(COST_OF_SALES_SECTION)


def _cost_of_sales_tracked(db, tenant):
    """Whether the chart has any cost-of-sales account; without one, gross profit equals sales."""
    return bool(db.execute("""SELECT 1 FROM ledger_accounts WHERE tenant_id=%s AND account_type='expense'
        AND lower(btrim(COALESCE(report_section,''))) LIKE %s LIMIT 1""",
                           (tenant, COST_OF_SALES_SECTION + "%")).fetchone())


def _result(pnl, tracked):
    """Headline profit figures taken from the existing profit and loss."""
    cost_of_sales = sum((s["total"] for s in pnl["sections"] if _is_cost_of_sales(s)), ZERO)
    return {"sales": pnl["totalIncome"], "expenses": pnl["totalExpenses"],
            "costOfSales": cost_of_sales, "operatingExpenses": pnl["totalExpenses"] - cost_of_sales,
            "grossProfit": pnl["totalIncome"] - cost_of_sales, "netProfit": pnl["netProfit"],
            "costOfSalesTracked": tracked}


def _control_balance(db, tenant, balances, report_type):
    """What the books say is owed: the receivables or payables control account, found as the ageing finds it."""
    _, _, purpose = financial.AGEING_TYPES[report_type]
    account = db.execute("""SELECT COALESCE(
            (SELECT account_id FROM erp_posting_accounts WHERE tenant_id=%(tenant)s AND purpose=%(purpose)s),
            (SELECT id FROM ledger_accounts WHERE tenant_id=%(tenant)s AND code=%(code)s
                AND account_type=%(account_type)s)) AS id""",
                         {"tenant": tenant, "purpose": purpose, "code": financial.DEFAULT_CODES[purpose],
                          "account_type": financial.PURPOSE_TYPES[purpose]}).fetchone()["id"]
    net = balances.get(account, ZERO)
    return net if report_type == "receivables" else -net


def _owed(db, tenant, as_of, report_type, balances):
    report = financial._ageing(db, tenant, as_of, report_type)
    control = _control_balance(db, tenant, balances, report_type)
    overdue = [r for r in report["rows"] if r["bucket"] != "current"]
    rows = [{"contactName": r["contactName"], "documentNumber": r["documentNumber"],
             "documentDate": r["documentDate"], "dueDate": r["dueDate"], "outstanding": r["outstanding"],
             "daysOverdue": (as_of - (r["dueDate"] or r["documentDate"])).days}
            for r in overdue]
    rows.sort(key=lambda r: (-r["outstanding"], -r["daysOverdue"], r["documentNumber"] or ""))
    open_items = report["totals"]["total"]
    # The total is the control account, like the balance sheet. Open items miss payments posted on account
    # and not yet allocated to an invoice or bill; the difference is shown, not hidden.
    totals = {"total": control, "openItems": open_items, "unallocated": open_items - control,
              "overdue": sum((r["outstanding"] for r in overdue), ZERO),
              "overdueCount": len(overdue), "openCount": len(report["rows"]),
              "buckets": {bucket: report["totals"][bucket] for bucket in report["buckets"]}}
    return totals, rows[:OVERDUE_LIMIT]


def _position(db, tenant, as_of, cash_accounts):
    balances = {row["account_id"]: row["debit"] - row["credit"]
                for row in financial._account_balances(db, tenant, as_of)}
    receivables, overdue_invoices = _owed(db, tenant, as_of, "receivables", balances)
    payables, overdue_bills = _owed(db, tenant, as_of, "payables", balances)
    return {"asOf": as_of, "bank": _bank_balances(as_of, balances, cash_accounts),
            "receivables": receivables, "payables": payables}, overdue_invoices, overdue_bills


def _by_contact(db, tenant, start, end, kind):
    """Posted income (customers) or expense (suppliers) by the party named on the source document."""
    other = OTHER_CUSTOMER if kind == "income" else OTHER_SUPPLIER
    sign = "l.credit-l.debit" if kind == "income" else "l.debit-l.credit"
    rows = db.execute(f"""SELECT COALESCE(NULLIF(BTRIM(typed.contact_name),''),NULLIF(BTRIM(rec.party),''),%(other)s) AS name,
            SUM({sign}) AS amount,COUNT(DISTINCT j.id) AS entries
        {POSTED_LINES}
        LEFT JOIN LATERAL (
            SELECT CASE WHEN a.account_type='income' THEN r.buyer_name ELSE r.supplier_name END AS party
            FROM standardised_records r
            WHERE r.tenant_id=j.tenant_id AND r.document_id={JOURNAL_SOURCE_ID}
                AND {JOURNAL_SOURCE_TYPE} IN ('document','adjustment')
            ORDER BY r.is_current DESC,r.revision_number DESC LIMIT 1
        ) rec ON true
        WHERE {POSTED_JOURNALS} AND j.journal_date BETWEEN %(start)s AND %(end)s
            AND a.account_type=%(kind)s AND NOT ({OPENING_JOURNAL})
        GROUP BY 1 HAVING SUM({sign})<>0""",
                      {"tenant": tenant, "start": start, "end": end, "kind": kind, "other": other}).fetchall()
    items = [{"name": r["name"], "amount": r["amount"], "entries": r["entries"], "named": r["name"] != other}
             for r in rows]
    # Named parties first, biggest first; the unattributed row always sits last.
    items.sort(key=lambda r: (not r["named"], -r["amount"], r["name"].lower()))
    return items


LINE_NET = """ROUND(COALESCE(NULLIF(ln->>'quantity','')::numeric,1)*COALESCE(NULLIF(ln->>'unitPrice','')::numeric,0),2)
    - ROUND(COALESCE(NULLIF(ln->>'discount','')::numeric,0),2)"""


def _sales_by_product(db, tenant, start, end, total_sales):
    """Typed invoice / debit note / credit note lines of posted documents, net of recorded tax."""
    rows = db.execute(f"""WITH lines AS (
            SELECT CASE WHEN h.document_type='credit-note' THEN -1 ELSE 1 END AS sign,ln,
                CASE WHEN (ln->>'itemId') ~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
                    THEN (ln->>'itemId')::uuid END AS item_id
            FROM erp_document_headers h
            JOIN journal_headers j ON j.id=h.journal_id AND j.tenant_id=h.tenant_id AND j.status='posted'
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(h.payload->'lines','[]'::jsonb)) ln
            WHERE h.tenant_id=%(tenant)s AND h.status='ready' AND h.currency='MYR'
                AND h.document_type IN ('invoice','debit-note','credit-note')
                AND j.journal_date BETWEEN %(start)s AND %(end)s
        ), priced AS (
            SELECT lines.sign,lines.item_id,p.name AS product_name,p.sku,
                NULLIF(BTRIM(lines.ln->>'description'),'') AS description,
                COALESCE(NULLIF(lines.ln->>'quantity','')::numeric,1) AS quantity,
                {LINE_NET.replace('ln->>', 'lines.ln->>')} AS net
            FROM lines LEFT JOIN erp_products p ON p.id=lines.item_id AND p.tenant_id=%(tenant)s
        )
        SELECT COALESCE(product_name,description,'Unnamed item') AS name,sku,
            SUM(sign*quantity) AS quantity,SUM(sign*net) AS amount
        FROM priced
        GROUP BY COALESCE(item_id::text,lower(description)),COALESCE(product_name,description,'Unnamed item'),sku
        HAVING SUM(sign*net)<>0
        ORDER BY amount DESC,name""", {"tenant": tenant, "start": start, "end": end}).fetchall()
    items = [{"name": r["name"], "sku": r["sku"], "quantity": format(r["quantity"].normalize(), "f"),
              "amount": r["amount"], "named": True}
             for r in rows]
    rest = total_sales - sum((i["amount"] for i in items), ZERO)
    if rest != ZERO:
        items.append({"name": NOT_ITEMISED, "sku": None, "quantity": None, "amount": rest, "named": False})
    return items


def _trend(db, tenant, first, end):
    months = []
    for back in range(TREND_MONTHS - 1, -1, -1):
        month_start = _shift_month(first, -back)
        month_end = min(month_start.replace(day=monthrange(month_start.year, month_start.month)[1]), end)
        pnl = financial._profit_and_loss(db, tenant, month_start, month_end)
        months.append({"month": f"{month_start:%Y-%m}", "fromDate": month_start, "toDate": month_end,
                       "sales": pnl["totalIncome"], "expenses": pnl["totalExpenses"], "netProfit": pnl["netProfit"]})
    return months


def build_summary(db, tenant, *, start, end, day=None):
    """The summary for the period start..end, plus the single day `day` when one is asked for."""
    cash_accounts = _cash_accounts(db, tenant)
    tracked = _cost_of_sales_tracked(db, tenant)
    pnl = financial._profit_and_loss(db, tenant, start, end)
    tax = financial._tax_summary(db, tenant, start, end)
    position, overdue_invoices, overdue_bills = _position(db, tenant, end, cash_accounts)
    customers = _by_contact(db, tenant, start, end, "income")
    suppliers = _by_contact(db, tenant, start, end, "expense")
    month = {"fromDate": start, "toDate": end, **_result(pnl, tracked), **_cash_movement(db, tenant, start, end, cash_accounts),
             "recordedTax": {"outputTax": tax["totals"]["outputTax"], "inputTax": tax["totals"]["inputTax"],
                             "net": tax["totals"]["net"], "note": RECORDED_TAX_NOTE},
             "position": position}
    result = {"currency": "MYR", "profitNote": PROFIT_NOTE, "month": month,
              "topCustomers": [c for c in customers if c["named"]][:TOP],
              "topSuppliers": [s for s in suppliers if s["named"]][:TOP],
              "overdueInvoices": overdue_invoices, "overdueBills": overdue_bills,
              "trend": _trend(db, tenant, start.replace(day=1), end),
              "salesByCustomer": customers, "purchasesBySupplier": suppliers,
              "salesByProduct": _sales_by_product(db, tenant, start, end, pnl["totalIncome"])}
    if day is not None:
        day_pnl = financial._profit_and_loss(db, tenant, day, day)
        day_position = position if day == end else _position(db, tenant, day, cash_accounts)[0]
        result["today"] = {"date": day, **_result(day_pnl, tracked), **_cash_movement(db, tenant, day, day, cash_accounts),
                           "position": day_position}
    return result


def _summary_sheet(summary, start, end):
    """The summary as the first page of the month pack."""
    month = summary["month"]
    rows = [("Profit and loss", "Sales", month["sales"]),
            ("Profit and loss", "Cost of sales", month["costOfSales"]),
            ("Profit and loss", "Gross profit", month["grossProfit"]),
            ("Profit and loss", "Operating expenses", month["operatingExpenses"]),
            ("Profit and loss", "Total purchases and expenses", month["expenses"]),
            ("Profit and loss", "Net profit", month["netProfit"]),
            ("Cash", "Cash in", month["cashIn"]),
            ("Cash", "Cash out", month["cashOut"])]
    position = month["position"]
    rows += [("Bank balance", account["name"], account["balance"]) for account in position["bank"]["accounts"]]
    rows.append(("Bank balance", "Total", position["bank"]["total"]))
    for label, key in (("Owed to the company (receivables)", "receivables"), ("Owed by the company (payables)", "payables")):
        rows += [(label, "Total outstanding", position[key]["total"]), (label, "Overdue", position[key]["overdue"])]
        if position[key]["unallocated"] != ZERO:
            rows.append((label, "Open items less unallocated payments", position[key]["openItems"]))
            rows.append((label, "Unallocated payments", position[key]["unallocated"]))
    rows += [("Recorded tax", "Output tax", month["recordedTax"]["outputTax"]),
             ("Recorded tax", "Input tax", month["recordedTax"]["inputTax"])]
    rows += [("Top customers", c["name"], c["amount"]) for c in summary["topCustomers"]]
    rows += [("Top suppliers", s["name"], s["amount"]) for s in summary["topSuppliers"]]
    rows += [("Overdue invoices", f"{r['contactName']} {r['documentNumber']} ({r['daysOverdue']} days)", r["outstanding"])
             for r in summary["overdueInvoices"]]
    rows += [("Overdue bills", f"{r['contactName']} {r['documentNumber']} ({r['daysOverdue']} days)", r["outstanding"])
             for r in summary["overdueBills"]]
    rows += [("Six-month trend", f"{t['month']} net profit", t["netProfit"]) for t in summary["trend"]]
    return Sheet("Business summary", f"{start} to {end}", ("Section", "Item", "Amount"), rows,
                 f"{RECORDED_TAX_NOTE}. {PROFIT_NOTE}")


def month_pack_summary_sheet(db, tenant, start, end):
    return _summary_sheet(build_summary(db, tenant, start=start, end=end), start, end)


def business_summary(request: Request, date: str | None = None, month: str | None = None):
    tenant = financial._tenant(request)
    with connect() as db:
        # One snapshot, so every figure on the page comes from the same books.
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        company_day = company_today(db, tenant)
        day = financial._report_date(date, "date") if date else company_day
        if day > company_day:
            raise HTTPException(422, "Choose today or an earlier date.")
        start, end = _month_bounds(month, day)
        summary = build_summary(db, tenant, start=start, end=end, day=day)
        summary["company"] = financial._company_name(db, tenant)
        summary["date"] = day
        summary["monthCode"] = f"{start:%Y-%m}"
    return jsonable_encoder(summary, custom_encoder={Decimal: money})


def register(router):
    router.add_api_route("/api/reports/business-summary", business_summary, methods=["GET"])
