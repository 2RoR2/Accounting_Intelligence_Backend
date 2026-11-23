"""Statement of account: one customer's receivable or one supplier's payable over a date range (read-only).

The statement lists every MYR transaction of the contact once, in date order, with a running balance:

    customer: invoices and debit notes (+), credit notes (-), payments received (-), refunds paid (+)
    supplier: bills and supplier debit notes (+), supplier credit notes (-), payments made (-), refunds received (+)

from the same sources as the AR/AP ageing report (``financial._ageing``):
- typed documents (erp_document_headers) of the contact that are ready; a voided document is left out, as
  its journal is reversed on the same date;
- approved scanned invoices/bills of the contact, except a scan that a ready typed document already
  records (``posting.typed_copy_record_ids``), so one obligation is counted once; a scanned credit note
  counts only when it was posted against the receivable/payable account;
- payments applied to those scanned documents through a settlement (bank match or manual record); a
  settlement made by a typed payment or credit note is already that document's line, so it is skipped;
- opening balances imported for the contact (the opening journal keeps the contact in its line label).

Opening balance = the movements before the start date. The ageing footer is the ageing report's open items
of this contact at the end date; payments and credits not allocated to an item are shown beside it so the
footer reconciles to the closing balance. Nothing is posted and no tax is calculated.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder

from main import connect, require_workspace_user
from services.routes.accounting.journal_sources import OPENING_JOURNAL
from services.routes.erp.common import money
from services.routes.erp.posting import DEFAULT_CODES, PURPOSE_TYPES, typed_copy_record_ids
from services.routes.home.lanes import APPROVED_RECORD, OPEN_ITEM_KINDS
from services.routes.reporting import financial

ZERO = Decimal("0.00")


class Side:
    def __init__(self, kind, report, purpose, category, column, signs, party_column):
        self.kind, self.report, self.purpose, self.category = kind, report, purpose, category
        self.column, self.signs, self.party_column = column, signs, party_column


SIDES = {
    "customer": Side("customer", "receivables", "receivable", "accounts_receivable", "customer_id",
                     {"invoice": 1, "debit-note": 1, "credit-note": -1, "payment": -1, "refund": 1}, "buyer_name"),
    "supplier": Side("supplier", "payables", "payable", "accounts_payable", "vendor_id",
                     {"bill": 1, "purchase-debit-note": 1, "purchase-credit-note": -1, "purchase-payment": -1,
                      "purchase-refund": 1}, "supplier_name"),
}
LABELS = {
    "invoice": "Invoice", "debit-note": "Debit note", "credit-note": "Credit note", "payment": "Payment received",
    "refund": "Refund paid", "bill": "Bill", "purchase-debit-note": "Supplier debit note",
    "purchase-credit-note": "Supplier credit note", "purchase-payment": "Payment made",
    "purchase-refund": "Refund received", "opening": "Opening balance",
}
# Same-day order: what is owed first, then what reduces it.
ORDER = {"opening": 0, "invoice": 1, "bill": 1, "debit-note": 2, "purchase-debit-note": 2, "credit-note": 3,
         "purchase-credit-note": 3, "payment": 4, "purchase-payment": 4, "refund": 5, "purchase-refund": 5}
ALLOCATING = {"payment", "credit-note", "purchase-payment", "purchase-credit-note"}
REFUNDS = {"refund", "purchase-refund"}


def _normal(name):
    from validation.matching.vendor_matching import normalize_vendor_name
    return normalize_vendor_name(name or "")


def _contact(db, tenant, contact_id):
    from services.routes.erp.contacts import TABLES, find_contact
    kind, row = find_contact(db, tenant, contact_id)
    if not row:
        raise HTTPException(404, "Contact not found.")
    if kind not in SIDES:
        raise HTTPException(422, "A statement of account is for a customer or a supplier.")
    address = "\n".join(x for x in [row.get("address"), " ".join(p for p in [row.get("postcode"), row.get("city")] if p),
                                    row.get("state")] if x)
    return kind, {"id": str(row["id"]), "kind": kind, "name": row[TABLES[kind][1]], "code": row.get("contact_code"),
                  "registrationNumber": row.get("registration_number"), "tin": (row.get("fields") or {}).get("tin"),
                  "address": address, "phone": row.get("phone"), "email": row.get("email")}


def _event(kind, day, number, description, amount, source, *, due=None, key=None):
    return {"date": day, "type": kind, "label": LABELS[kind], "number": number or "", "description": description or "",
            "dueDate": due, "amount": amount, "source": source, "_key": key}


def _typed(db, tenant, side, contact_id, end):
    """Ready typed documents of the contact, and how much of each payment/credit is allocated by the end date."""
    rows = db.execute(f"""SELECT h.id,h.document_type,h.document_number,h.document_date,h.due_date,h.reference_no,
            h.description,h.total_amount,h.settlement_id,
            COALESCE((SELECT SUM(a.amount) FROM erp_document_allocations a
                WHERE a.tenant_id=h.tenant_id AND a.source_document_id=h.id
                    AND (a.reversed_at IS NULL OR (a.reversed_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date>%(end)s)),0)
            + COALESCE((SELECT SUM(a.amount) FROM settlement_allocations a
                WHERE a.tenant_id=h.tenant_id AND a.settlement_id=h.settlement_id AND a.status='active'
                    AND NOT EXISTS (SELECT 1 FROM settlement_allocations x
                        WHERE x.tenant_id=a.tenant_id AND x.reverses_allocation_id=a.id
                            AND (COALESCE(x.reversed_at,x.created_at) AT TIME ZONE 'Asia/Kuala_Lumpur')::date<=%(end)s)),0)
            AS allocated
        FROM erp_document_headers h
        WHERE h.tenant_id=%(tenant)s AND h.{side.column}=%(contact)s AND h.status='ready' AND h.currency='MYR'
            AND h.document_type=ANY(%(types)s) AND h.document_date<=%(end)s""",
                      {"tenant": tenant, "contact": contact_id, "types": list(side.signs), "end": end}).fetchall()
    events, unallocated = [], ZERO
    for r in rows:
        kind = r["document_type"]
        amount = side.signs[kind] * r["total_amount"]
        description = r["description"] or (f"Ref {r['reference_no']}" if r["reference_no"] else "")
        owed = side.signs[kind] > 0 and kind not in REFUNDS
        events.append(_event(kind, r["document_date"], r["document_number"], description, amount, "typed",
                             due=r["due_date"] if owed else None, key=("typed", r["document_number"]) if owed else None))
        if kind in ALLOCATING:
            unallocated += r["total_amount"] - r["allocated"]
        elif kind in REFUNDS:
            unallocated -= r["total_amount"]
    return events, unallocated


def _scanned(db, tenant, side, contact, end):
    """Approved scanned invoices/bills (and posted credit notes) of the contact, counted once."""
    kinds = [*OPEN_ITEM_KINDS[side.category], "credit_note"]
    rows = db.execute(f"""SELECT r.id,r.document_id,r.vendor_id,r.{side.party_column} AS party,d.kind,r.due_date,
            COALESCE(NULLIF(BTRIM(r.invoice_number),''),NULLIF(BTRIM(r.reference_number),''),d.document_code) AS number,
            COALESCE(r.invoice_date,(d.uploaded_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date) AS day,
            COALESCE(r.amount_due,r.total_amount) AS outstanding,r.total_amount,
            EXISTS (SELECT 1 FROM journal_headers j JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id
                JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
                WHERE j.tenant_id=r.tenant_id AND j.source_type IN ('document','adjustment') AND j.source_id=r.document_id
                    AND j.status='posted' AND a.account_type=%(account_type)s
                    AND a.id IN (SELECT account_id FROM erp_posting_accounts WHERE tenant_id=r.tenant_id AND purpose=%(purpose)s
                                 UNION SELECT id FROM ledger_accounts WHERE tenant_id=r.tenant_id AND code=%(code)s)) AS on_control
        FROM standardised_records r
        JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id AND d.excluded_at IS NULL
        WHERE r.tenant_id=%(tenant)s AND {APPROVED_RECORD} AND r.category=%(category)s AND d.kind=ANY(%(kinds)s)
            AND r.currency='MYR'""",
                      {"tenant": tenant, "category": side.category, "kinds": kinds, "purpose": side.purpose,
                       "code": DEFAULT_CODES[side.purpose], "account_type": PURPOSE_TYPES[side.purpose]}).fetchall()
    wanted = _normal(contact["name"])
    mine = [r for r in rows if (side.kind == "supplier" and r["vendor_id"] and str(r["vendor_id"]) == contact["id"])
            or ((side.kind == "customer" or not r["vendor_id"]) and wanted and _normal(r["party"]) == wanted)]
    copies = typed_copy_record_ids(db, tenant, [r["id"] for r in mine if r["kind"] != "credit_note"]) if mine else set()
    events, unallocated, counted = [], ZERO, []
    for r in mine:
        if r["id"] in copies or r["day"] > end:
            continue
        if r["kind"] == "credit_note":
            if not r["on_control"]:  # e.g. refunded in cash: it never changed what is owed
                continue
            label = "credit-note" if side.kind == "customer" else "purchase-credit-note"
            events.append(_event(label, r["day"], r["number"], "Scanned credit note", -r["total_amount"], "scanned"))
            unallocated += r["total_amount"]
            continue
        counted.append(r["id"])
        label = "invoice" if side.kind == "customer" else "bill"
        events.append(_event(label, r["day"], r["number"], "Scanned document", r["outstanding"], "scanned",
                             due=r["due_date"], key=("scanned", r["number"], _normal(r["party"]))))
    return events, unallocated, counted


def _settled(db, tenant, side, record_ids, end):
    """Payments applied to the contact's scanned documents through settlements not made by a typed document."""
    if not record_ids:
        return []
    # A bank-matched payment shows the bank line's own reference rather than the internal settlement key.
    rows = db.execute("""SELECT a.amount,COALESCE(NULLIF(BTRIM(t.reference),''),s.settlement_ref) AS settlement_ref,
            s.settlement_date,
            COALESCE(NULLIF(BTRIM(r.invoice_number),''),NULLIF(BTRIM(r.reference_number),''),r.record_code) AS number
        FROM settlement_allocations a
        JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id
        JOIN standardised_records r ON r.id=a.record_id AND r.tenant_id=a.tenant_id
        LEFT JOIN bank_transactions t ON t.tenant_id=s.tenant_id AND s.source_type='bank_import' AND t.id=s.source_id
        WHERE a.tenant_id=%(tenant)s AND a.record_id=ANY(%(records)s) AND a.status='active' AND s.settlement_date<=%(end)s
            AND NOT EXISTS (SELECT 1 FROM settlement_allocations x
                WHERE x.tenant_id=a.tenant_id AND x.reverses_allocation_id=a.id
                    AND (COALESCE(x.reversed_at,x.created_at) AT TIME ZONE 'Asia/Kuala_Lumpur')::date<=%(end)s)
            AND NOT EXISTS (SELECT 1 FROM erp_document_headers h WHERE h.tenant_id=s.tenant_id AND h.settlement_id=s.id)""",
                      {"tenant": tenant, "records": record_ids, "end": end}).fetchall()
    kind = "payment" if side.kind == "customer" else "purchase-payment"
    return [_event(kind, r["settlement_date"], r["settlement_ref"], f"Applied to {r['number']}", -r["amount"], "settlement")
            for r in rows]


def _opening(db, tenant, side, contact, end):
    """Opening balances imported for this contact (the same journal lines the ageing report reads)."""
    rows = db.execute(f"""WITH control_account AS (
            SELECT COALESCE(
                (SELECT account_id FROM erp_posting_accounts WHERE tenant_id=%(tenant)s AND purpose=%(purpose)s),
                (SELECT id FROM ledger_accounts WHERE tenant_id=%(tenant)s AND code=%(code)s AND account_type=%(account_type)s)
            ) AS id
        )
        SELECT BTRIM(substring(l.description FROM ', contact ([^,]+)')) AS label,j.journal_date,
            CASE WHEN %(purpose)s='receivable' THEN l.debit-l.credit ELSE l.credit-l.debit END AS amount
        {financial.POSTED_LINES}
        WHERE {financial.POSTED_JOURNALS} AND {OPENING_JOURNAL} AND j.reversed_journal_id IS NULL
            AND j.journal_date<=%(end)s AND a.account_type=%(account_type)s
            AND (l.account_id=(SELECT id FROM control_account)
                OR (l.source_field LIKE 'opening_item:%%' AND l.description LIKE %(item_description)s))
            AND NOT EXISTS (SELECT 1 FROM journal_headers undo
                WHERE undo.tenant_id=j.tenant_id AND undo.reversed_journal_id=j.id
                    AND undo.status='posted' AND undo.journal_date<=%(end)s)""",
                      {"tenant": tenant, "end": end, "purpose": side.purpose, "code": DEFAULT_CODES[side.purpose],
                       "account_type": PURPOSE_TYPES[side.purpose], "item_description": f"Opening {side.purpose} %"}
                      ).fetchall()
    wanted = _normal(contact["name"])
    events, unallocated = [], ZERO
    for r in rows:
        if not r["label"] or _normal(r["label"]) != wanted:
            continue
        events.append(_event("opening", r["journal_date"], "Opening balance", "Imported opening balance", r["amount"],
                             "opening", key=("opening", _normal(r["label"]))))
        if r["amount"] < 0:  # a credit balance is not an open item in the ageing report
            unallocated -= r["amount"]
    return events, unallocated


def _ageing_footer(db, tenant, side, end, events, unallocated, closing):
    """The ageing report's open items of exactly the transactions on this statement, at the end date."""
    keys = {e["_key"] for e in events if e["_key"]}
    report = financial._ageing(db, tenant, end, side.report)
    totals = dict.fromkeys(financial.BUCKETS, ZERO)
    items = []
    for row in report["rows"]:
        if row["source"] == "typed":
            key = ("typed", row["documentNumber"])
        elif row["source"] == "scanned":
            key = ("scanned", row["documentNumber"], _normal(row["contactName"]))
        else:
            key = ("opening", _normal(row["contactName"]))
        if key not in keys:
            continue
        totals[row["bucket"]] += row["outstanding"]
        items.append({"number": row["documentNumber"], "date": row["documentDate"], "dueDate": row["dueDate"],
                      "outstanding": row["outstanding"], "bucket": row["bucket"]})
    open_items = sum(totals.values(), ZERO)
    difference = closing - (open_items - unallocated)
    return {"asOf": end, "buckets": list(financial.BUCKETS), "totals": totals, "openItems": open_items,
            "unallocatedCredits": unallocated, "balance": open_items - unallocated, "items": items,
            "reconciles": difference == 0, "difference": difference}


def statement(db, tenant, contact_id, start, end):
    kind, contact = _contact(db, tenant, contact_id)
    side = SIDES[kind]
    typed, typed_unallocated = _typed(db, tenant, side, contact["id"], end)
    scanned, scanned_unallocated, records = _scanned(db, tenant, side, contact, end)
    opening_lines, opening_unallocated = _opening(db, tenant, side, contact, end)
    events = typed + scanned + _settled(db, tenant, side, records, end) + opening_lines
    events.sort(key=lambda e: (e["date"], ORDER[e["type"]], e["number"]))
    opening = sum((e["amount"] for e in events if e["date"] < start), ZERO)
    balance = opening
    lines = []
    for e in events:
        if e["date"] < start:
            continue
        balance += e["amount"]
        increase = e["amount"] if e["amount"] > 0 else ZERO
        decrease = -e["amount"] if e["amount"] < 0 else ZERO
        # Customer: what they owe is a debit. Supplier: what we owe is a credit.
        debit, credit = (increase, decrease) if side.kind == "customer" else (decrease, increase)
        lines.append({k: v for k, v in e.items() if k != "_key"} | {"debit": debit, "credit": credit, "balance": balance})
    unallocated = typed_unallocated + scanned_unallocated + opening_unallocated
    return {
        "contact": contact, "kind": kind, "fromDate": start, "toDate": end, "currency": "MYR",
        "openingBalance": opening, "lines": lines,
        "totalDebit": sum((line["debit"] for line in lines), ZERO),
        "totalCredit": sum((line["credit"] for line in lines), ZERO),
        "closingBalance": balance,
        "balanceLabel": "Amount owed by the customer" if kind == "customer" else "Amount owed to the supplier",
        "ageing": _ageing_footer(db, tenant, side, end, events, unallocated, balance),
    }


def _pdf_name(report):
    code = report["contact"]["code"] or report["contact"]["name"]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in code).strip("-") or "contact"
    return f"statement-{safe}-{report['fromDate']}-to-{report['toDate']}.pdf"


def contact_statement(contact_id: str, request: Request, from_date: str | None = Query(None, alias="from"),
                      to_date: str | None = Query(None, alias="to"), format: str | None = None):
    tenant = require_workspace_user(request, {"local_admin", "accountant"})["tenant_id"]
    if format not in (None, "json", "pdf"):
        raise HTTPException(422, "Choose json or pdf for format.")
    start, end = financial._date_range(from_date, to_date)
    with connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        report = statement(db, tenant, contact_id, start, end)
        company = None
        if format == "pdf":
            from services.routes.erp.documents import _company_for_print
            company = _company_for_print(db, tenant)
    data = jsonable_encoder(report, custom_encoder={Decimal: money})
    if format != "pdf":
        return data
    from services.routes.erp.pdf import render_statement_pdf
    return Response(render_statement_pdf(data, company), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{_pdf_name(report)}"'})


def register(router):
    router.add_api_route("/api/contacts/{contact_id}/statement", contact_statement, methods=["GET"], tags=["Reports"])
