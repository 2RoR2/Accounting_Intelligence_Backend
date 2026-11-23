"""Global search (plan P1): `GET /api/search?q=&limit=` for the top bar.

Finds a record by its number, contact name, reference or an exact amount, and returns the matches grouped by type with
what the frontend needs to link to each. Read-only, tenant-scoped, parameterised SQL (the text is bound as a LIKE
pattern with its wildcards escaped; it is never put into the statement). At least two characters are required and each
type returns at most `limit` rows (default 5, never more than 20). Accountants see only the uploads they own or are
assigned (the documents rule); every other type is company-wide.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from main import HTTPException, Request, connect

from services.routes.erp.common import erp_user, money
from services.routes.erp.contacts import TABLES as CONTACT_TABLES
from services.routes.home.lanes import ACCOUNTANT_DOCUMENT_VISIBILITY

MIN_CHARS = 2
MAX_CHARS = 100
DEFAULT_LIMIT = 5
MAX_LIMIT = 20
GROUP_ORDER = ["quotation", "sales-order", "delivery-order", "invoice", "credit-note", "debit-note", "payment", "refund",
               "purchase-order", "goods-received", "bill", "purchase-credit-note", "purchase-debit-note", "purchase-payment",
               "purchase-refund", "money-in", "money-out", "bank-transfer", "record", "upload", "bank-transaction", "journal",
               "contact"]
AMOUNT = re.compile(r"^\d{1,12}(\.\d{1,2})?$")


def parse_amount(text: str):
    """`1,234.50`, `RM 1234.5` and `MYR1234` are amounts; anything else is not."""
    cleaned = re.sub(r"^(RM|MYR)\s*", "", text.strip(), flags=re.I).replace(",", "").strip()
    if not AMOUNT.match(cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:  # pragma: no cover - the pattern already limits the text to a decimal
        return None


def like_pattern(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _item(kind, item_id, type_, number, title, status, date, amount, currency):
    return {"kind": kind, "id": str(item_id), "type": type_, "number": number, "title": title, "status": status,
            "date": str(date) if date else None, "amount": money(amount) if amount is not None else None,
            "currency": currency}


def search(request: Request, q: str = "", limit: int = DEFAULT_LIMIT):
    user = erp_user(request)
    text = (q or "").strip()
    if len(text) < MIN_CHARS:
        raise HTTPException(422, f"Type at least {MIN_CHARS} characters to search.")
    if len(text) > MAX_CHARS:
        raise HTTPException(422, "Search text is too long.")
    limit = max(1, min(int(limit), MAX_LIMIT))
    like, amount, tenant = like_pattern(text), parse_amount(text), user["tenant_id"]
    groups: dict[str, dict] = {}

    def add(group, label, item):
        groups.setdefault(group, {"group": group, "label": label, "items": []})["items"].append(item)

    with connect() as db:
        # Typed documents, up to `limit` per document type. An exact number match comes first.
        for r in db.execute("""SELECT * FROM (
                SELECT id,document_type,document_number,status,contact_name,document_date,total_amount,currency,
                       row_number() OVER (PARTITION BY document_type ORDER BY
                           (upper(document_number)=upper(%(text)s)) DESC, document_date DESC, created_at DESC) AS n
                FROM erp_document_headers
                WHERE tenant_id=%(tenant)s AND (document_number ILIKE %(like)s ESCAPE '\\' OR reference_no ILIKE %(like)s ESCAPE '\\'
                    OR contact_name ILIKE %(like)s ESCAPE '\\' OR (%(amount)s::numeric IS NOT NULL AND total_amount=%(amount)s::numeric))
            ) t WHERE n<=%(limit)s ORDER BY document_type, n""",
                            {"text": text, "tenant": tenant, "like": like, "amount": amount, "limit": limit}).fetchall():
            add(r["document_type"], r["document_type"].replace("-", " ").capitalize(),
                _item("erp", r["id"], r["document_type"], r["document_number"], r["contact_name"], r["status"],
                      r["document_date"], r["total_amount"], r["currency"]))
        # Approved scanned records (the current revision). An exact number match comes first, before the limit.
        for r in db.execute("""SELECT r.id,r.record_code,r.invoice_number,r.supplier_name,r.buyer_name,r.category,r.invoice_date,
                    r.total_amount,r.currency,d.kind
                FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
                WHERE r.tenant_id=%(tenant)s AND r.is_current AND (r.record_code ILIKE %(like)s ESCAPE '\\'
                    OR r.invoice_number ILIKE %(like)s ESCAPE '\\' OR r.reference_number ILIKE %(like)s ESCAPE '\\'
                    OR r.supplier_name ILIKE %(like)s ESCAPE '\\' OR r.buyer_name ILIKE %(like)s ESCAPE '\\'
                    OR (%(amount)s::numeric IS NOT NULL AND r.total_amount=%(amount)s::numeric))
                ORDER BY (upper(COALESCE(r.invoice_number,''))=upper(%(text)s) OR upper(COALESCE(r.record_code,''))=upper(%(text)s)
                          OR upper(COALESCE(r.reference_number,''))=upper(%(text)s)) DESC,
                    r.invoice_date DESC NULLS LAST, r.created_at DESC LIMIT %(limit)s""",
                            {"text": text, "tenant": tenant, "like": like, "amount": amount, "limit": limit}).fetchall():
            add("record", "Scanned records", _item(
                "record", r["id"], r["kind"], r["invoice_number"] or r["record_code"],
                r["buyer_name"] if r["category"] == "accounts_receivable" else r["supplier_name"], "approved",
                r["invoice_date"], r["total_amount"], r["currency"]))
        # Uploads (the documents the user may see).
        scope, params = "d.tenant_id=%(tenant)s AND d.excluded_at IS NULL", {"tenant": tenant, "like": like, "limit": limit}
        if user["role"] == "accountant":
            scope += " AND " + ACCOUNTANT_DOCUMENT_VISIBILITY.replace("%s", "%(user)s")
            params["user"] = user["id"]
        for r in db.execute(f"""SELECT d.id,d.document_code,d.original_filename,d.kind,d.status,d.uploaded_at FROM documents d
                WHERE {scope} AND (d.document_code ILIKE %(like)s ESCAPE '\\' OR d.original_filename ILIKE %(like)s ESCAPE '\\')
                ORDER BY d.uploaded_at DESC LIMIT %(limit)s""", params).fetchall():
            add("upload", "Uploaded documents", _item(
                "upload", r["id"], r["kind"], r["document_code"], r["original_filename"], r["status"],
                r["uploaded_at"].date() if r["uploaded_at"] else None, None, None))
        # Bank lines: reference, description or the exact amount in either direction.
        for r in db.execute("""SELECT id,posted_date,description,reference,signed_amount_decimal,currency,status
                FROM bank_transactions WHERE tenant_id=%(tenant)s AND (reference ILIKE %(like)s ESCAPE '\\'
                    OR description ILIKE %(like)s ESCAPE '\\'
                    OR (%(amount)s::numeric IS NOT NULL AND abs(signed_amount_decimal)=%(amount)s::numeric))
                ORDER BY posted_date DESC NULLS LAST, created_at DESC LIMIT %(limit)s""",
                            {"tenant": tenant, "like": like, "amount": amount, "limit": limit}).fetchall():
            add("bank-transaction", "Bank lines", _item(
                "bank", r["id"], "bank-transaction", r["reference"] or "Bank line", r["description"], r["status"],
                r["posted_date"], r["signed_amount_decimal"], r["currency"]))
        # Journals: description, or a line with the exact amount.
        for r in db.execute("""SELECT j.id,j.description,j.journal_date,j.status,j.source_type,
                    (SELECT sum(l.debit) FROM journal_lines l WHERE l.journal_id=j.id AND l.tenant_id=j.tenant_id) AS total
                FROM journal_headers j WHERE j.tenant_id=%(tenant)s AND (j.description ILIKE %(like)s ESCAPE '\\'
                    OR (%(amount)s::numeric IS NOT NULL AND EXISTS (SELECT 1 FROM journal_lines l
                        WHERE l.journal_id=j.id AND l.tenant_id=j.tenant_id
                          AND (l.debit=%(amount)s::numeric OR l.credit=%(amount)s::numeric))))
                ORDER BY j.journal_date DESC, j.created_at DESC LIMIT %(limit)s""",
                            {"tenant": tenant, "like": like, "amount": amount, "limit": limit}).fetchall():
            add("journal", "Journals", _item("journal", r["id"], "journal", (r["description"] or "")[:60], r["source_type"],
                                             r["status"], r["journal_date"], r["total"], None))
        # Contacts (customers, suppliers, employees): name or code.
        for kind, (table, column) in CONTACT_TABLES.items():
            for r in db.execute(f"""SELECT id,{column} AS name,contact_code,is_active FROM {table}
                    WHERE tenant_id=%(tenant)s AND ({column} ILIKE %(like)s ESCAPE '\\' OR COALESCE(contact_code,'') ILIKE %(like)s ESCAPE '\\')
                    ORDER BY {column} LIMIT %(limit)s""", {"tenant": tenant, "like": like, "limit": limit}).fetchall():
                add("contact", "Contacts", _item("contact", r["id"], kind, r["contact_code"] or r["name"], r["name"],
                                                 "active" if r["is_active"] else "inactive", None, None, None))
    if "contact" in groups:  # three contact tables share one group and one limit
        groups["contact"]["items"] = sorted(groups["contact"]["items"], key=lambda i: i["title"].casefold())[:limit]
    # A group holding an exact number match first, then sales, purchases, scanned, bank, ledger and contacts.
    def rank(group):
        exact = any(i["number"].casefold() == text.casefold() for i in group["items"])
        return (not exact, GROUP_ORDER.index(group["group"]) if group["group"] in GROUP_ORDER else len(GROUP_ORDER), group["group"])
    return {"query": text, "limit": limit, "groups": sorted(groups.values(), key=rank)}


def register(router, handlers):
    router.add_api_route("/api/search", search, methods=["GET"], tags=["Search"])
