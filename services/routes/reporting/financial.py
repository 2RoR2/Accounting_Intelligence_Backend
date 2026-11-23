"""Tenant-scoped MYR financial statements and their CSV and Excel exports."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO
from urllib.parse import quote

from fastapi import HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from openpyxl import Workbook
from openpyxl.styles import Font

from main import _uuid_or_422, connect, require_workspace_user
from services.routes.accounting.journal_sources import (
    JOURNAL_CURRENCY,
    JOURNAL_CURRENCY_JOINS,
    JOURNAL_SOURCE_TYPE,
    OPENING_JOURNAL,
)
from services.routes.erp.common import money
from services.routes.erp.posting import DEFAULT_CODES, PURPOSE_TYPES
from services.routes.home.lanes import APPROVED_RECORD, OPEN_ITEM_KINDS

ZERO = Decimal("0.00")
BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")
AGEING_TYPES = {
    # Plan M: debit notes are open items like the invoice/bill they add to.
    "receivables": (("invoice", "debit-note"), "accounts_receivable", "receivable"),
    "payables": (("bill", "purchase-debit-note"), "accounts_payable", "payable"),
}
TAX_NOTE = "Recorded tax only - not a tax return"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

POSTED_LINES = f"""
    FROM journal_headers j
    JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id
    JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
    {JOURNAL_CURRENCY_JOINS}
"""
# Journals without a currency-bearing source, including manual and opening entries, use workspace MYR.
POSTED_JOURNALS = (
    "j.tenant_id=%(tenant)s AND j.status IN ('posted','reversed')"
    f" AND COALESCE({JOURNAL_CURRENCY},'MYR')='MYR'"
)


def _account(row):
    return {"accountId": str(row["account_id"]), "code": row["code"], "name": row["name"]}


def _account_balances(db, tenant, end, start=None, *, include_opening=True, earnings_start=None):
    opening_filter = "" if include_opening else f"AND NOT ({OPENING_JOURNAL})"
    return db.execute(f"""SELECT l.account_id,a.code,a.name,a.account_type,a.report_section,
            SUM(l.debit) AS debit,SUM(l.credit) AS credit,
            SUM(l.credit-l.debit) FILTER (WHERE j.journal_date<%(earnings_start)s::date) AS prior_earnings
        {POSTED_LINES}
        WHERE {POSTED_JOURNALS}
            AND j.journal_date<=%(end)s AND (%(start)s::date IS NULL OR j.journal_date>=%(start)s)
            {opening_filter}
        GROUP BY l.account_id,a.code,a.name,a.account_type,a.report_section
        ORDER BY a.code""", {"tenant": tenant, "start": start, "end": end, "earnings_start": earnings_start}).fetchall()


def _profit_and_loss(db, tenant, start, end):
    totals = {"income": ZERO, "expense": ZERO}
    groups = {}
    for row in _account_balances(db, tenant, end, start, include_opening=False):
        kind = row["account_type"]
        if kind not in totals:
            continue
        amount = row["credit"] - row["debit"] if kind == "income" else row["debit"] - row["credit"]
        label = row["report_section"] or ("Income" if kind == "income" else "Expenses")
        section = groups.setdefault(label, {"key": label, "label": label, "accounts": [], "total": ZERO})
        section["accounts"].append({**_account(row), "amount": amount})
        section["total"] += amount
        totals[kind] += amount
    return {"fromDate": start, "toDate": end, "sections": list(groups.values()),
            "totalIncome": totals["income"], "totalExpenses": totals["expense"],
            "netProfit": totals["income"] - totals["expense"]}


def _balance_sheet(db, tenant, end):
    sections = {kind: {"key": key, "label": key.title(), "accounts": [], "total": ZERO}
                for kind, key in (("asset", "assets"), ("liability", "liabilities"), ("equity", "equity"))}
    retained_earnings = current_year_earnings = ZERO
    # Workspace settings do not define a fiscal-year start.
    for row in _account_balances(db, tenant, end, earnings_start=date(end.year, 1, 1)):
        kind = row["account_type"]
        net = row["debit"] - row["credit"]
        if kind in {"income", "expense"}:
            prior = row["prior_earnings"] or ZERO
            retained_earnings += prior
            current_year_earnings -= net + prior
            continue
        amount = net if kind == "asset" else -net
        sections[kind]["accounts"].append({**_account(row), "amount": amount})
        sections[kind]["total"] += amount
    equity = sections["equity"]
    equity["accounts"].extend((
        {"accountId": "retained-earnings", "code": "", "name": "Retained earnings (prior years)",
         "amount": retained_earnings},
        {"accountId": "current-year-earnings", "code": "", "name": "Current year earnings",
         "amount": current_year_earnings},
    ))
    equity["total"] += retained_earnings + current_year_earnings
    assets = sections["asset"]["total"]
    liabilities_and_equity = sections["liability"]["total"] + equity["total"]
    return {"asOf": end, "sections": list(sections.values()), "retainedEarnings": retained_earnings,
            "currentYearEarnings": current_year_earnings,
            "totalAssets": assets, "totalLiabilitiesAndEquity": liabilities_and_equity,
            "balanced": assets == liabilities_and_equity}


def _general_ledger(db, tenant, start, end, account_id):
    rows = db.execute(f"""WITH entries AS (
            SELECT l.account_id,a.code,a.name,j.journal_date,j.id AS journal_id,j.created_at,l.line_number,
                COALESCE(NULLIF(l.description,''),j.description) AS description,j.source_type,l.debit,l.credit
            {POSTED_LINES}
            WHERE {POSTED_JOURNALS}
                AND j.journal_date<=%(end)s AND (%(account)s::uuid IS NULL OR l.account_id=%(account)s)
        ), opening AS (
            SELECT account_id,code,name,SUM(debit-credit) AS opening_balance
            FROM entries WHERE journal_date<%(start)s
            GROUP BY account_id,code,name HAVING SUM(debit-credit)<>0
        ), movements AS (
            SELECT * FROM entries WHERE journal_date>=%(start)s
        )
        SELECT COALESCE(m.account_id,o.account_id) AS account_id,COALESCE(m.code,o.code) AS code,
            COALESCE(m.name,o.name) AS name,o.opening_balance,m.journal_date,m.journal_id,m.description,
            m.source_type,m.debit,m.credit
        FROM movements m FULL JOIN opening o ON o.account_id=m.account_id
        ORDER BY code,m.journal_date,m.created_at,m.journal_id,m.line_number""",
        {"tenant": tenant, "start": start, "end": end, "account": account_id}).fetchall()
    accounts = {}
    for row in rows:
        opening = row["opening_balance"] or ZERO
        account = accounts.setdefault(row["account_id"], {
            **_account(row), "openingBalance": opening, "lines": [], "closingBalance": opening,
        })
        if row["journal_id"] is not None:
            account["closingBalance"] += row["debit"] - row["credit"]
            account["lines"].append({"date": row["journal_date"], "journalId": str(row["journal_id"]),
                                     "description": row["description"], "sourceType": row["source_type"],
                                     "debit": row["debit"], "credit": row["credit"], "balance": account["closingBalance"]})
    return {"fromDate": start, "toDate": end, "accounts": list(accounts.values())}


def _ageing_bucket(days):
    if days <= 0:
        return "current"
    return BUCKETS[min((days - 1) // 30 + 1, 4)]


def _without_typed_copies(db, tenant, rows):
    """Drop scanned open items that a ready typed bill/invoice already records (same party and number).
    Posting refuses the second copy of an obligation, so ageing must count it once too (Codex H2)."""
    from services.routes.erp.posting import typed_copy_record_ids
    scanned = [row["record_id"] for row in rows if row["record_id"]]
    if not scanned:
        return rows
    copies = typed_copy_record_ids(db, tenant, scanned)
    return [row for row in rows if row["record_id"] not in copies]


def _ageing(db, tenant, end, report_type):
    document_type, category, purpose = AGEING_TYPES[report_type]
    # Include allocations recorded by the cutoff for payments dated by the cutoff.
    rows = db.execute(f"""WITH control_account AS (
            SELECT COALESCE(
                (SELECT account_id FROM erp_posting_accounts WHERE tenant_id=%(tenant)s AND purpose=%(purpose)s),
                (SELECT id FROM ledger_accounts WHERE tenant_id=%(tenant)s AND code=%(code)s
                    AND account_type=%(account_type)s)
            ) AS id
        ), open_items AS (
            SELECT 'typed' AS source,h.contact_name,h.document_number,h.document_date,h.due_date,
                h.total_amount-COALESCE((
                    SELECT SUM(a.amount) FROM erp_document_allocations a
                    JOIN erp_document_headers s ON s.id=a.source_document_id AND s.tenant_id=a.tenant_id
                    WHERE a.tenant_id=h.tenant_id AND a.target_document_id=h.id
                        AND s.document_date<=%(end)s
                        AND (a.reversed_at IS NULL OR (a.reversed_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date>%(end)s)
                ),0) AS outstanding,NULL::uuid AS record_id
            FROM erp_document_headers h
            WHERE h.tenant_id=%(tenant)s AND h.document_type=ANY(%(type)s) AND h.currency='MYR'
                AND (h.status='ready' OR (h.status='void' AND (h.voided_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date>%(end)s))
            UNION ALL
            SELECT 'scanned',
                CASE WHEN r.category='accounts_payable' THEN r.supplier_name ELSE r.buyer_name END,
                COALESCE(NULLIF(BTRIM(r.invoice_number),''),NULLIF(BTRIM(r.reference_number),''),d.document_code),
                COALESCE(r.invoice_date,(d.uploaded_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date),r.due_date,
                COALESCE(r.amount_due,r.total_amount)-COALESCE((
                    SELECT SUM(a.amount) FROM settlement_allocations a
                    JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id
                    WHERE a.tenant_id=r.tenant_id AND a.record_id=r.id AND a.status='active'
                        AND s.settlement_date<=%(end)s
                        AND NOT EXISTS (SELECT 1 FROM settlement_allocations x
                            WHERE x.tenant_id=a.tenant_id AND x.reverses_allocation_id=a.id
                                AND (COALESCE(x.reversed_at,x.created_at) AT TIME ZONE 'Asia/Kuala_Lumpur')::date<=%(end)s)
                ),0),r.id
            FROM standardised_records r
            JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id AND d.excluded_at IS NULL
            WHERE r.tenant_id=%(tenant)s AND {APPROVED_RECORD} AND r.category=%(category)s
                AND d.kind=ANY(%(kinds)s) AND r.currency='MYR'
            UNION ALL
            -- The opening import keeps its contact label in the journal line description.
            SELECT 'opening',
                COALESCE(NULLIF(BTRIM(substring(l.description FROM ', contact ([^,]+)')),''),
                    'Opening balance (not itemised)'),
                'Opening balance',j.journal_date,NULL,
                CASE WHEN %(purpose)s='receivable' THEN l.debit-l.credit ELSE l.credit-l.debit END,NULL::uuid
            {POSTED_LINES}
            WHERE {POSTED_JOURNALS} AND {OPENING_JOURNAL} AND j.reversed_journal_id IS NULL
                AND j.journal_date<=%(end)s AND a.account_type=%(account_type)s
                AND (l.account_id=(SELECT id FROM control_account)
                    OR (l.source_field LIKE 'opening_item:%%' AND l.description LIKE %(item_description)s))
                AND NOT EXISTS (SELECT 1 FROM journal_headers undo
                    WHERE undo.tenant_id=j.tenant_id AND undo.reversed_journal_id=j.id
                        AND undo.status='posted' AND undo.journal_date<=%(end)s)
        )
        SELECT * FROM open_items WHERE document_date<=%(end)s AND outstanding>0
        ORDER BY COALESCE(due_date,document_date),document_date,document_number,source,contact_name""",
        {"tenant": tenant, "end": end, "type": list(document_type), "category": category,
         "purpose": purpose, "code": DEFAULT_CODES[purpose], "account_type": PURPOSE_TYPES[purpose],
         "item_description": f"Opening {purpose} %",
         "kinds": list(OPEN_ITEM_KINDS[category])}).fetchall()
    rows = _without_typed_copies(db, tenant, rows)
    items = []
    totals = dict.fromkeys(BUCKETS, ZERO)
    for row in rows:
        bucket = _ageing_bucket((end - (row["due_date"] or row["document_date"])).days)
        totals[bucket] += row["outstanding"]
        items.append({"contactName": row["contact_name"], "source": row["source"], "documentNumber": row["document_number"],
                      "documentDate": row["document_date"], "dueDate": row["due_date"],
                      "outstanding": row["outstanding"], "bucket": bucket})
    return {"type": report_type, "asOf": end, "buckets": list(BUCKETS), "rows": items,
            "totals": {**totals, "total": sum(totals.values(), ZERO)}}


def _tax_summary(db, tenant, start, end):
    rows = db.execute(f"""WITH tax_accounts AS (
            SELECT purpose.name,COALESCE(p.account_id,default_account.id) AS account_id
            FROM (VALUES ('tax_output',%(output_code)s,'liability'),('tax_input',%(input_code)s,'asset'))
                AS purpose(name,code,account_type)
            LEFT JOIN erp_posting_accounts p ON p.tenant_id=%(tenant)s AND p.purpose=purpose.name
            LEFT JOIN ledger_accounts default_account ON default_account.tenant_id=%(tenant)s
                AND default_account.code=purpose.code AND default_account.account_type=purpose.account_type
        )
        SELECT to_char(j.journal_date,'YYYY-MM') AS month,
            COALESCE(SUM(l.credit-l.debit) FILTER (WHERE t.name='tax_output'),0) AS output_tax,
            COALESCE(SUM(l.debit-l.credit) FILTER (WHERE t.name='tax_input'),0) AS input_tax
        {POSTED_LINES}
        JOIN tax_accounts t ON t.account_id=l.account_id
        WHERE {POSTED_JOURNALS} AND j.journal_date BETWEEN %(start)s AND %(end)s
            AND {JOURNAL_SOURCE_TYPE} IN ('erp_document','document','adjustment')
        GROUP BY month ORDER BY month""", {"tenant": tenant, "start": start, "end": end,
        "output_code": DEFAULT_CODES["tax_output"], "input_code": DEFAULT_CODES["tax_input"]}).fetchall()
    months = [{"month": r["month"], "outputTax": r["output_tax"], "inputTax": r["input_tax"],
               "net": r["output_tax"] - r["input_tax"]} for r in rows]
    totals = {key: sum((row[key] for row in months), ZERO) for key in ("outputTax", "inputTax", "net")}
    return {"fromDate": start, "toDate": end, "note": TAX_NOTE, "months": months, "totals": totals}


@dataclass
class Sheet:
    name: str
    dates: str
    headers: tuple[str, ...]
    rows: list[tuple]
    note: str | None = None


def _date_title(report):
    if "asOf" in report:
        return f"As of {report['asOf']}"
    return f"{report['fromDate']} to {report['toDate']}"


def _statement_sheet(name, report, total_labels):
    rows = []
    for section in report["sections"]:
        rows.extend((section["label"], account["code"], account["name"], account["amount"])
                    for account in section["accounts"])
        rows.append((section["label"], "", f"Total {section['label']}", section["total"]))
    rows.extend(("", "", label, report[key]) for key, label in total_labels)
    return Sheet(name, _date_title(report), ("Section", "Code", "Account", "Amount"), rows)


def _profit_and_loss_sheet(report):
    return _statement_sheet("Profit and loss", report, (
        ("totalIncome", "Total income"), ("totalExpenses", "Total expenses"), ("netProfit", "Net profit"),
    ))


def _balance_sheet_sheet(report):
    return _statement_sheet("Balance sheet", report, (
        ("totalAssets", "Total assets"), ("totalLiabilitiesAndEquity", "Total liabilities and equity"),
    ))


def _trial_balance_sheet(db, tenant, end):
    rows = [(r["code"], r["name"], r["account_type"], r["debit"], r["credit"], r["debit"] - r["credit"])
            for r in _account_balances(db, tenant, end)]
    rows.append(("", "Total", "", *(sum((r[i] for r in rows), ZERO) for i in (3, 4, 5))))
    return Sheet("Trial balance", f"As of {end}", ("Code", "Account", "Type", "Debit", "Credit", "Balance"), rows)


def _general_ledger_sheet(report):
    rows = []
    for account in report["accounts"]:
        identity = account["code"], account["name"]
        rows.append((*identity, "", "", "Opening balance", "", None, None, account["openingBalance"]))
        rows.extend((*identity, line["date"], line["journalId"], line["description"], line["sourceType"],
                     line["debit"], line["credit"], line["balance"]) for line in account["lines"])
        rows.append((*identity, "", "", "Closing balance", "", None, None, account["closingBalance"]))
    return Sheet("General ledger", _date_title(report),
                 ("Code", "Account", "Date", "Journal ID", "Description", "Source type", "Debit", "Credit", "Balance"), rows)


def _ageing_sheet(report):
    rows = [(r["contactName"], r["source"], r["documentNumber"], r["documentDate"], r["dueDate"],
             r["outstanding"], r["bucket"]) for r in report["rows"]]
    rows.extend(("Total", "", "", None, None, report["totals"][bucket], bucket) for bucket in (*BUCKETS, "total"))
    return Sheet(f"{report['type'].capitalize()} ageing", _date_title(report),
                 ("Contact", "Source", "Document number", "Document date", "Due date", "Outstanding", "Bucket"), rows)


def _tax_summary_sheet(report):
    months = [*report["months"], {"month": "Total", **report["totals"]}]
    rows = [(r["month"], r["outputTax"], r["inputTax"], r["net"]) for r in months]
    return Sheet("Tax summary", _date_title(report), ("Month", "Output tax", "Input tax", "Net"), rows, report["note"])


def _exceptions_sheet(db, tenant, start, end):
    rows = db.execute("""SELECT d.document_code,e.type,e.severity,e.message,e.created_at
        FROM exceptions e
        JOIN documents d ON d.id=e.document_id AND d.tenant_id=e.tenant_id
        WHERE e.tenant_id=%s AND e.status<>'resolved'
            AND (e.created_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date BETWEEN %s AND %s
        ORDER BY e.created_at,e.id""", (tenant, start, end)).fetchall()
    return Sheet("Open exceptions", f"{start} to {end}", ("Document code", "Type", "Severity", "Message", "Created"),
                 [(r["document_code"], r["type"], r["severity"], r["message"], r["created_at"].isoformat()) for r in rows])


def _attachment(filename, content, media_type):
    encoded = quote(filename, safe="")
    disposition = f'filename="{filename}"' if encoded == filename else f"filename*=UTF-8''{encoded}"
    return Response(content, media_type=media_type, headers={"Content-Disposition": f"attachment; {disposition}"})


def _text_value(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _csv_export(sheet, filename):
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(sheet.headers)
    writer.writerows([money(v) if isinstance(v, Decimal) else _text_value(v) for v in row] for row in sheet.rows)
    if sheet.note:
        writer.writerow([sheet.note, *[""] * (len(sheet.headers) - 1)])
    return _attachment(filename, output.getvalue().encode("utf-8"), "text/csv")


def _xlsx_export(company, sheets, filename):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for data in sheets:
        sheet = workbook.create_sheet(data.name)
        sheet.append([f"{company} | {data.name} | {data.dates}"])
        sheet["A1"].data_type = "s"
        sheet["A1"].font = Font(bold=True)
        if data.note:
            sheet.append([data.note])
        sheet.append(data.headers)
        for row_number, row in enumerate(data.rows, start=4 if data.note else 3):
            for column, value in enumerate(row, start=1):
                cell = sheet.cell(row_number, column, value.isoformat() if isinstance(value, date) else value)
                if isinstance(value, Decimal):
                    cell.number_format = "0.00"
                elif isinstance(value, str):
                    cell.data_type = "s"
    output = BytesIO()
    workbook.save(output)
    return _attachment(filename, output.getvalue(), XLSX)


def _report_date(value, name):
    if not value:
        raise HTTPException(422, f"Enter {name} in YYYY-MM-DD format.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a valid date in YYYY-MM-DD format.") from None
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{name} must be in YYYY-MM-DD format.")
    return parsed


def _date_range(from_date, to_date):
    start, end = _report_date(from_date, "fromDate"), _report_date(to_date, "toDate")
    if end < start:
        raise HTTPException(422, "The report end date must be on or after the start date.")
    return start, end


def _tenant(request):
    return require_workspace_user(request, {"local_admin", "accountant"})["tenant_id"]


def _company_name(db, tenant):
    return db.execute("SELECT name FROM tenants WHERE id=%s", (tenant,)).fetchone()["name"]


def _report(request, export_format, load, to_sheet, *args):
    tenant = _tenant(request)
    if export_format not in (None, "csv", "xlsx"):
        raise HTTPException(422, "Choose csv or xlsx for format.")
    with connect() as db:
        report = load(db, tenant, *args)
        company = _company_name(db, tenant) if export_format == "xlsx" else None
    if export_format is None:
        return jsonable_encoder(report, custom_encoder={Decimal: money})
    sheet = to_sheet(report)
    filename = f"{sheet.name.lower().replace(' ', '-')}.{export_format}"
    if export_format == "csv":
        return _csv_export(sheet, filename)
    return _xlsx_export(company, [sheet], filename)


def profit_and_loss(request: Request, fromDate: str | None = None, toDate: str | None = None, format: str | None = None):
    return _report(request, format, _profit_and_loss, _profit_and_loss_sheet, *_date_range(fromDate, toDate))


def balance_sheet(request: Request, asOf: str | None = None, format: str | None = None):
    return _report(request, format, _balance_sheet, _balance_sheet_sheet, _report_date(asOf, "asOf"))


def general_ledger(request: Request, fromDate: str | None = None, toDate: str | None = None,
                   accountId: str | None = None, format: str | None = None):
    start, end = _date_range(fromDate, toDate)
    account_id = _uuid_or_422(accountId, "account id") if accountId is not None else None
    return _report(request, format, _general_ledger, _general_ledger_sheet, start, end, account_id)


def ageing(request: Request, type: str | None = None, asOf: str | None = None, format: str | None = None):
    if type not in AGEING_TYPES:
        raise HTTPException(422, "Choose receivables or payables for type.")
    return _report(request, format, _ageing, _ageing_sheet, _report_date(asOf, "asOf"), type)


def tax_summary(request: Request, fromDate: str | None = None, toDate: str | None = None, format: str | None = None):
    return _report(request, format, _tax_summary, _tax_summary_sheet, *_date_range(fromDate, toDate))


def month_pack(request: Request, period: str | None = None, format: str | None = None):
    tenant = _tenant(request)
    if not period:
        raise HTTPException(422, "Enter an accounting period code.")
    if format not in (None, "xlsx"):
        raise HTTPException(422, "Choose xlsx for the month-pack format.")
    with connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        row = db.execute("SELECT starts_on,ends_on FROM accounting_periods WHERE tenant_id=%s AND period_code=%s",
                         (tenant, period)).fetchone()
        if not row:
            raise HTTPException(404, "Accounting period not found.")
        start, end = row["starts_on"], row["ends_on"]
        company = _company_name(db, tenant)
        sheets = [
            _profit_and_loss_sheet(_profit_and_loss(db, tenant, start, end)),
            _balance_sheet_sheet(_balance_sheet(db, tenant, end)),
            _trial_balance_sheet(db, tenant, end),
            _general_ledger_sheet(_general_ledger(db, tenant, start, end, None)),
            _ageing_sheet(_ageing(db, tenant, end, "receivables")),
            _ageing_sheet(_ageing(db, tenant, end, "payables")),
            _tax_summary_sheet(_tax_summary(db, tenant, start, end)),
            _exceptions_sheet(db, tenant, start, end),
        ]
    return _xlsx_export(company, sheets, f"month-pack-{period}.xlsx")


def register(router):
    for path, handler in (
        ("profit-and-loss", profit_and_loss), ("balance-sheet", balance_sheet), ("general-ledger", general_ledger),
        ("ageing", ageing), ("tax-summary", tax_summary),
    ):
        router.add_api_route(f"/api/reports/financial/{path}", handler, methods=["GET"])
    router.add_api_route("/api/reports/month-pack", month_pack, methods=["GET"])
