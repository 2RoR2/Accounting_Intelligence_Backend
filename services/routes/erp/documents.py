"""Plan L4/L5: sales and purchase documents (latest_update's 23 document types) that reach the ledger.

Status: draft -> pending_approval -> ready -> void (latest_update's rules). "ready" is where a financial
document posts (posting.py); other types are record-only and the response says so. Only a company admin
makes a document ready or voids a ready one, as with the existing ledger endpoints. A ready document is
never edited or deleted: voiding a posted one adds a reversal journal.

L5: the invoice, bill and credit-note lists also show approved scanned documents (source "scanned"),
which post through the same rule and the same idempotency keys as the record endpoints.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from main import *  # noqa: F401,F403  (Request, HTTPException, connect)
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import posting
from .common import DOCUMENT_TYPES, audit, erp_user, money, parse_uuid
from .contacts import find_contact

READ_ROLES = {"local_admin", "accountant"}
WRITE_ROLES = {"local_admin", "accountant"}
POST_ROLES = {"local_admin"}
STATUSES = {"draft", "pending_approval", "ready", "void"}
TRANSITIONS = {"draft": {"pending_approval", "ready", "void"}, "pending_approval": {"draft", "ready", "void"},
               "ready": {"void"}, "void": set()}
PREFIXES = {"quotation": "QT", "sales-order": "SO", "delivery-order": "DO", "invoice": "IV", "credit-note": "CN",
            "payment": "OR", "refund": "CR", "purchase-order": "PO", "goods-received": "GR", "bill": "BL",
            "purchase-credit-note": "SC", "purchase-payment": "PV", "purchase-refund": "SR", "money-in": "MI",
            "money-out": "MO", "bank-transfer": "FT", "stock-adjustment": "SA", "stock-transfer": "ST",
            "journal-entry": "JE", "contra": "CT", "deemed-payment": "DP", "store-order": "BS", "minipos-order": "POS"}
LABELS = {t: t.replace("-", " ").capitalize() for t in PREFIXES}
# Which document a ready/pending one can turn into (quotation -> invoice keeps the lines).
TRANSFERS = {"quotation": {"sales-order", "delivery-order", "invoice"}, "sales-order": {"delivery-order", "invoice"},
             "delivery-order": {"invoice"}, "invoice": {"credit-note", "payment"},
             "purchase-order": {"goods-received", "bill"}, "goods-received": {"bill"},
             "bill": {"purchase-credit-note", "purchase-payment"}}
MONEY_ONLY = {"payment", "refund", "purchase-payment", "purchase-refund", "bank-transfer"}


class LineInput(BaseModel):
    itemId: str | None = None
    description: str = Field(default="", max_length=500)
    quantity: Decimal = Field(default=Decimal("1"), gt=0, max_digits=15, decimal_places=3)
    unitPrice: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=4)
    discount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    tax: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    accountId: str | None = None
    debit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    credit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)


class AllocationInput(BaseModel):
    targetId: str
    source: str = Field(default="typed", pattern=r"^(typed|scanned)$")
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)


class DocumentInput(BaseModel):
    number: str | None = Field(default=None, max_length=120)
    contactId: str | None = None
    contactName: str | None = Field(default=None, max_length=250)
    documentDate: date | None = None
    dueDate: date | None = None
    currency: str = Field(default="MYR", pattern=r"^[A-Za-z]{3}$")
    referenceNo: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    status: str = Field(default="draft", pattern=r"^(draft|pending_approval|ready)$")
    amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    bankAccountId: str | None = None
    lines: list[LineInput] = Field(default_factory=list, max_length=500)
    allocations: list[AllocationInput] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=100)
    fields: dict[str, Any] = Field(default_factory=dict)


class DocumentPatch(BaseModel):
    contactId: str | None = None
    contactName: str | None = Field(default=None, max_length=250)
    documentDate: date | None = None
    dueDate: date | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    referenceNo: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    bankAccountId: str | None = None
    lines: list[LineInput] | None = Field(default=None, max_length=500)
    allocations: list[AllocationInput] | None = Field(default=None, max_length=100)
    tags: list[str] | None = Field(default=None, max_length=100)
    fields: dict[str, Any] | None = None


class TransitionInput(BaseModel):
    status: str = Field(pattern=r"^(draft|pending_approval|ready|void)$")
    reason: str | None = Field(default=None, max_length=1000)


class TransferInput(BaseModel):
    targetType: str = Field(min_length=1, max_length=50)


class ScannedPostInput(BaseModel):
    accountId: str | None = None
    journalDate: date | None = None


class PostingAccountsInput(BaseModel):
    accounts: dict[str, str]


# ------------------------------------------------------------------ helpers

def _type(document_type: str) -> str:
    if document_type not in DOCUMENT_TYPES:
        raise HTTPException(422, "Invalid document type.")
    return document_type


def _event(db, user, document_id, event_type, *, from_status=None, to_status=None, reason=None):
    db.execute("""INSERT INTO erp_document_events(tenant_id,document_id,event_type,from_status,to_status,reason,actor_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""", (user["tenant_id"], document_id, event_type, from_status, to_status, reason, user["id"]))


def _next_number(db, tenant_id, document_type: str) -> str:
    fmt = db.execute("""SELECT data FROM erp_catalog_entries WHERE tenant_id=%s AND kind='number-format' AND is_active
        AND data->>'documentType'=%s ORDER BY created_at LIMIT 1""", (tenant_id, document_type)).fetchone()
    prefix = (fmt["data"].get("prefix") if fmt else None) or PREFIXES.get(document_type, "TX")
    padding = int((fmt["data"].get("padding") if fmt else None) or 5)
    db.execute("""INSERT INTO erp_number_counters(tenant_id,document_type,next_value) VALUES (%s,%s,1)
        ON CONFLICT (tenant_id,document_type) DO NOTHING""", (tenant_id, document_type))
    while True:
        value = db.execute("""UPDATE erp_number_counters SET next_value=next_value+1 WHERE tenant_id=%s AND document_type=%s
            RETURNING next_value-1 AS value""", (tenant_id, document_type)).fetchone()["value"]
        number = f"{prefix}-{int(value):0{padding}d}"
        taken = db.execute("SELECT 1 FROM erp_document_headers WHERE tenant_id=%s AND document_type=%s AND document_number=%s",
                           (tenant_id, document_type, number)).fetchone()
        if not taken:  # a typed-in number may already use the next value
            return number


def _contact(db, tenant_id, document_type, contact_id):
    """(vendor_id, customer_id, employee_id, name) for a document's contact; checks the kind fits the type."""
    if not contact_id:
        return None, None, None, None
    kind, row = find_contact(db, tenant_id, contact_id)
    if not row:
        raise HTTPException(422, "The selected contact is not available.")
    if not row["is_active"]:
        raise HTTPException(422, "The selected contact is inactive. Reactivate it first.")
    if document_type in posting.SALES_TYPES and kind != "customer":
        raise HTTPException(422, "Choose a customer for a sales document.")
    if document_type in posting.PURCHASE_TYPES and kind != "supplier":
        raise HTTPException(422, "Choose a supplier for a purchase document.")
    name = row.get("vendor_name") or row.get("customer_name") or row.get("legal_name")
    return (row["id"] if kind == "supplier" else None, row["id"] if kind == "customer" else None,
            row["id"] if kind == "employee" else None, name)


def _bank_account(db, tenant_id, bank_account_id):
    if not bank_account_id:
        return None
    entry_id = parse_uuid(bank_account_id, "bank account id")
    row = db.execute("""SELECT id FROM erp_catalog_entries WHERE id=%s AND tenant_id=%s AND kind='bank-account' AND is_active""",
                     (entry_id, tenant_id)).fetchone()
    if not row:
        raise HTTPException(422, "Choose an active bank account.")
    return row["id"]


def _totals(document_type, lines: list[dict], amount):
    """(subtotal, tax, total). Lines decide the total when there are any; payments and transfers use the amount."""
    if document_type == "journal-entry":
        debit = sum((posting.cents(l.get("debit")) for l in lines), Decimal("0"))
        return debit, Decimal("0"), debit
    if lines and document_type not in MONEY_ONLY:
        subtotal = sum((posting.line_net(l) for l in lines), Decimal("0"))
        tax = sum((posting.cents(l.get("tax")) for l in lines), Decimal("0"))
        if subtotal < 0:
            raise HTTPException(422, "A line discount cannot be more than the line amount.")
        return subtotal, tax, subtotal + tax
    total = posting.cents(amount or 0)
    return total, Decimal("0"), total


def _clean_lines(lines) -> list[dict]:
    cleaned = []
    for line in lines:
        item = line.model_dump(mode="json") if hasattr(line, "model_dump") else dict(line)
        for key in ("itemId", "accountId"):
            if item.get(key):
                item[key] = str(parse_uuid(item[key], key))
        cleaned.append(item)
    return cleaned


def _allocations(document_type, allocations) -> list[dict]:
    if allocations and document_type not in posting.ALLOCATING_TYPES:
        raise HTTPException(422, "Only payments and credit notes can be allocated to invoices or bills.")
    result = []
    for a in allocations:
        item = a.model_dump(mode="json") if hasattr(a, "model_dump") else dict(a)
        item["targetId"] = str(parse_uuid(item["targetId"], "allocation target id"))
        if item["source"] == "scanned" and document_type in {"credit-note", "purchase-credit-note"}:
            raise HTTPException(422, "A typed credit note can only be allocated to a typed invoice or bill.")
        result.append(item)
    return result


def typed_outstanding(db, tenant_id, document_id) -> Decimal:
    row = db.execute("""SELECT h.total_amount - COALESCE((SELECT SUM(a.amount) FROM erp_document_allocations a
            WHERE a.tenant_id=h.tenant_id AND a.target_document_id=h.id AND a.status='active'),0) AS balance
        FROM erp_document_headers h WHERE h.id=%s AND h.tenant_id=%s""", (document_id, tenant_id)).fetchone()
    return Decimal(row["balance"]) if row else Decimal("0")


def _dto(db, row) -> dict:
    payload = row["payload"] or {}
    contact_kind = "supplier" if row["vendor_id"] else "customer" if row["customer_id"] else "employee" if row["employee_id"] else None
    contact_id = row["vendor_id"] or row["customer_id"] or row["employee_id"]
    result = {"id": str(row["id"]), "source": "typed", "type": row["document_type"], "number": row["document_number"],
              "contactId": str(contact_id) if contact_id else None, "contactKind": contact_kind, "contactName": row["contact_name"],
              "status": row["status"], "documentDate": str(row["document_date"]), "dueDate": str(row["due_date"]) if row["due_date"] else None,
              "currency": row["currency"], "referenceNo": row["reference_no"], "description": row["description"],
              "subtotal": money(row["subtotal"]), "taxAmount": money(row["tax_amount"]), "amount": money(row["total_amount"]),
              "bankAccountId": str(row["bank_account_id"]) if row["bank_account_id"] else None,
              "lines": payload.get("lines", []), "allocations": payload.get("allocations", []), "tags": payload.get("tags", []),
              "fields": payload.get("fields", {}), "posts": row["document_type"] in posting.POSTING_TYPES,
              "posted": bool(row["journal_id"]), "journalId": str(row["journal_id"]) if row["journal_id"] else None,
              "reversalJournalId": str(row["reversal_journal_id"]) if row["reversal_journal_id"] else None,
              "postedAt": str(row["posted_at"]) if row["posted_at"] else None, "voidReason": row["void_reason"],
              "createdAt": str(row["created_at"]), "updatedAt": str(row["updated_at"]),
              "voidedAt": str(row["voided_at"]) if row["voided_at"] else None}
    if row["document_type"] in {"invoice", "bill"}:
        balance = typed_outstanding(db, row["tenant_id"], row["id"]) if row["status"] == "ready" else Decimal(row["total_amount"])
        result["outstanding"] = money(balance)
        result["paymentStatus"] = ("void" if row["status"] == "void" else "paid" if row["status"] == "ready" and balance <= 0
                                   else "partly_paid" if row["status"] == "ready" and balance < row["total_amount"] else "unpaid")
    return result


def _load(db, user, document_type, document_id, *, for_update=False):
    row = db.execute(f"""SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s
        {'FOR UPDATE' if for_update else ''}""", (parse_uuid(document_id, "document id"), user["tenant_id"], document_type)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found.")
    return row


# ------------------------------------------------------------------ posting on ready, reversal on void

def _check_duplicate_of_scanned(db, tenant_id, doc):
    """A typed bill/invoice that repeats an approved scanned one would post the same purchase or sale twice."""
    if doc["document_type"] not in {"bill", "invoice"}:
        return
    number = (doc["reference_no"] if doc["document_type"] == "bill" else doc["document_number"]) or ""
    if not number.strip():
        return
    category = "accounts_payable" if doc["document_type"] == "bill" else "accounts_receivable"
    party = "vendor_id" if doc["document_type"] == "bill" else None
    rows = db.execute("""SELECT r.record_code,r.vendor_id,r.supplier_name,r.buyer_name FROM standardised_records r
        WHERE r.tenant_id=%s AND r.is_current AND r.validation_status='passed' AND r.category=%s
            AND lower(btrim(r.invoice_number))=lower(btrim(%s))""", (tenant_id, category, number)).fetchall()
    from validation.matching.vendor_matching import normalize_vendor_name
    wanted = normalize_vendor_name(doc["contact_name"] or "")
    for r in rows:
        same_party = ((party and r["vendor_id"] and r["vendor_id"] == doc["vendor_id"])
                      or (wanted and normalize_vendor_name(r["supplier_name"] if party else (r["buyer_name"] or "")) == wanted))
        if same_party:
            raise HTTPException(409, f"This {doc['document_type']} was already captured from a scanned document "
                                     f"({r['record_code']}); it is posted from there.")


def _apply_allocations(db, user, doc):
    """Payment / credit note: link to the invoices or bills it settles (typed in erp_document_allocations, scanned
    through a settlement in settlement_allocations, whose guard trigger checks the amounts)."""
    from psycopg.errors import RaiseException
    allocations = (doc["payload"] or {}).get("allocations", [])
    if not allocations:
        return None
    target_type = posting.ALLOCATING_TYPES[doc["document_type"]]
    total = sum((posting.cents(a["amount"]) for a in allocations), Decimal("0"))
    if total > posting.cents(doc["total_amount"]):
        raise HTTPException(422, "The allocations add up to more than the document amount.")
    settlement_id = None
    for a in allocations:
        amount = posting.cents(a["amount"])
        if a["source"] == "typed":
            target = db.execute("""SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s FOR UPDATE""",
                                (a["targetId"], user["tenant_id"], target_type)).fetchone()
            if not target or target["status"] != "ready":
                raise HTTPException(409, f"Allocate only to a ready {target_type}.")
            if (target["customer_id"] or target["vendor_id"]) != (doc["customer_id"] or doc["vendor_id"]):
                raise HTTPException(422, f"The {target_type} {target['document_number']} belongs to another contact.")
            if amount > typed_outstanding(db, user["tenant_id"], target["id"]):
                raise HTTPException(409, f"{target['document_number']} has less than {amount} outstanding.")
            db.execute("""INSERT INTO erp_document_allocations(tenant_id,source_document_id,target_document_id,amount,created_by)
                VALUES (%s,%s,%s,%s,%s)""", (user["tenant_id"], doc["id"], target["id"], amount, user["id"]))
        else:
            category, kinds = posting.SCANNED_TYPES[target_type]
            record = db.execute("""SELECT r.id FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
                WHERE r.id=%s AND r.tenant_id=%s AND r.is_current AND r.validation_status='passed' AND r.category=%s AND d.kind = ANY(%s)""",
                                (a["targetId"], user["tenant_id"], category, list(kinds))).fetchone()
            if not record:
                raise HTTPException(409, f"Allocate only to a current approved scanned {target_type}.")
            if settlement_id is None:
                settlement_id = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,
                        source_type,source_id,created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,'manual_record',%s,%s) RETURNING id""",
                                           (user["tenant_id"], f"ERP-{doc['document_number']}-{str(doc['id'])[:8]}", doc["document_date"],
                                            "money_in" if doc["document_type"] == "payment" else "money_out", doc["total_amount"],
                                            doc["currency"], doc["id"], user["id"])).fetchone()["id"]
            try:
                db.execute("SAVEPOINT erp_allocation")
                db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by)
                    VALUES (%s,%s,%s,%s,%s)""", (user["tenant_id"], settlement_id, record["id"], amount, user["id"]))
                db.execute("RELEASE SAVEPOINT erp_allocation")
            except RaiseException as error:
                db.execute("ROLLBACK TO SAVEPOINT erp_allocation")
                raise HTTPException(409, str(error).split("\n")[0])
    return settlement_id


def _make_ready(db, user, doc):
    """Post a financial document (or mark a record-only one ready). The caller holds the row lock."""
    if user["role"] not in POST_ROLES:
        raise HTTPException(403, "Only a company admin can make a document ready (post it).")
    kind = doc["document_type"]
    if kind in posting.CONTACT_REQUIRED and not (doc["vendor_id"] or doc["customer_id"]):
        raise HTTPException(422, f"Choose the {'supplier' if kind in posting.PURCHASE_TYPES else 'customer'} first.")
    lines = (doc["payload"] or {}).get("lines", [])
    if kind in {"invoice", "credit-note", "bill", "purchase-credit-note"} and not lines:
        raise HTTPException(422, "Add at least one line.")
    if kind in posting.POSTING_TYPES and posting.cents(doc["total_amount"]) <= 0:
        raise HTTPException(422, "The document total must be more than zero.")
    journal_id = settlement_id = None
    if kind in posting.POSTING_TYPES:
        if doc["currency"] != "MYR":
            raise HTTPException(409, "Posting supports MYR only; foreign-currency conversion is not configured.")
        _check_duplicate_of_scanned(db, user["tenant_id"], doc)
        plan = posting.plan_for_document(db, user["tenant_id"], doc)
        result = posting.post_plan(db, user, plan, key=f"erp:{doc['id']}", source_type="erp_document", source_id=doc["id"],
                                   journal_date=doc["document_date"],
                                   description=f"{LABELS[kind]} {doc['document_number']}" + (f" {doc['contact_name']}" if doc["contact_name"] else ""))
        journal_id = result["id"]
        if kind in posting.ALLOCATING_TYPES:
            settlement_id = _apply_allocations(db, user, doc)
    row = db.execute("""UPDATE erp_document_headers SET status='ready',journal_id=%s,settlement_id=%s,
            posted_at=CASE WHEN %s::uuid IS NULL THEN NULL ELSE now() END,updated_by=%s,updated_at=now()
        WHERE id=%s AND tenant_id=%s RETURNING *""", (journal_id, settlement_id, journal_id, user["id"], doc["id"], user["tenant_id"])).fetchone()
    if journal_id:
        audit(db, user, "erp.document_posted", str(doc["id"]), {"type": kind, "number": doc["document_number"], "journalId": str(journal_id)})
    return row


def _void(db, user, doc, reason):
    if doc["status"] == "ready" and user["role"] not in POST_ROLES:
        raise HTTPException(403, "Only a company admin can void a ready document.")
    reversal = None
    if doc["journal_id"]:
        if doc["document_type"] in {"invoice", "bill"}:
            used = db.execute("""SELECT h.document_number FROM erp_document_allocations a
                JOIN erp_document_headers h ON h.id=a.source_document_id AND h.tenant_id=a.tenant_id
                WHERE a.tenant_id=%s AND a.target_document_id=%s AND a.status='active' LIMIT 1""",
                              (user["tenant_id"], doc["id"])).fetchone()
            if used:
                raise HTTPException(409, f"{used['document_number']} is allocated to this {doc['document_type']}; void it first.")
        reversal = posting.reverse_in_transaction(db, user, doc["journal_id"],
                                                  reason=f"void {doc['document_number']}" + (f": {reason}" if reason else ""))
        db.execute("""UPDATE erp_document_allocations SET status='reversed',reversed_at=now()
            WHERE tenant_id=%s AND source_document_id=%s AND status='active'""", (user["tenant_id"], doc["id"]))
        if doc["settlement_id"]:
            for a in db.execute("""SELECT a.* FROM settlement_allocations a WHERE a.tenant_id=%s AND a.settlement_id=%s AND a.status='active'
                    AND NOT EXISTS (SELECT 1 FROM settlement_allocations u WHERE u.tenant_id=a.tenant_id AND u.reverses_allocation_id=a.id)""",
                                (user["tenant_id"], doc["settlement_id"])).fetchall():
                db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,status,created_by,reversed_at,
                        reversal_reason,reverses_allocation_id)
                    VALUES (%s,%s,%s,%s,'reversed',%s,now(),%s,%s)""",
                           (user["tenant_id"], a["settlement_id"], a["record_id"], a["amount"], user["id"],
                            f"Voided {doc['document_number']}", a["id"]))
            db.execute("UPDATE settlements SET status='voided' WHERE id=%s AND tenant_id=%s", (doc["settlement_id"], user["tenant_id"]))
        audit(db, user, "erp.document_voided", str(doc["id"]), {"type": doc["document_type"], "number": doc["document_number"],
                                                                 "reversalJournalId": str(reversal)})
    return db.execute("""UPDATE erp_document_headers SET status='void',voided_at=now(),void_reason=%s,reversal_journal_id=%s,
            updated_by=%s,updated_at=now() WHERE id=%s AND tenant_id=%s RETURNING *""",
                      (reason, reversal, user["id"], doc["id"], user["tenant_id"])).fetchone()


# ------------------------------------------------------------------ handlers

def list_documents(document_type: str, request: Request, page: int = 1, pageSize: int = 30, search: str | None = None,
                   status: str | None = None, contactId: str | None = None, dateFrom: date | None = None,
                   dateTo: date | None = None, source: str = "all"):
    _type(document_type)
    user = erp_user(request, READ_ROLES)
    if page < 1 or pageSize < 1 or pageSize > 100:
        raise HTTPException(422, "Invalid pagination.")
    if status and status not in STATUSES | {"approved"}:
        raise HTTPException(422, "Invalid document status.")
    if source not in {"all", "typed", "scanned"}:
        raise HTTPException(422, "Source must be all, typed or scanned.")
    where, params = ["tenant_id=%s", "document_type=%s"], [user["tenant_id"], document_type]
    contact = parse_uuid(contactId, "contact id") if contactId else None
    if status:
        where.append("status=%s"); params.append(status)
    if contact:
        where.append("(vendor_id=%s OR customer_id=%s OR employee_id=%s)"); params.extend([contact] * 3)
    if dateFrom:
        where.append("document_date >= %s"); params.append(dateFrom)
    if dateTo:
        where.append("document_date <= %s"); params.append(dateTo)
    if search:
        where.append("(document_number ILIKE %s OR COALESCE(reference_no,'') ILIKE %s OR COALESCE(contact_name,'') ILIKE %s "
                     "OR COALESCE(description,'') ILIKE %s)"); params.extend([f"%{search}%"] * 4)
    items = []
    with connect() as db:
        if source in {"all", "typed"} and status != "approved":
            rows = db.execute(f"SELECT * FROM erp_document_headers WHERE {' AND '.join(where)} ORDER BY document_date DESC, created_at DESC LIMIT 1000",
                              params).fetchall()
            items.extend(_dto(db, row) for row in rows)
        if source in {"all", "scanned"} and document_type in posting.SCANNED_TYPES and status in {None, "ready", "approved"}:
            items.extend(_scanned_rows(db, user["tenant_id"], document_type, search=search, contact=contact,
                                       date_from=dateFrom, date_to=dateTo, status=status))
    items.sort(key=lambda d: (d["documentDate"] or "", d["createdAt"]), reverse=True)
    total = len(items)
    start = (page - 1) * pageSize
    return {"items": items[start:start + pageSize], "total": total, "page": page, "pageSize": pageSize,
            "nextPage": page + 1 if start + pageSize < total else None}


def _scanned_rows(db, tenant_id, document_type, *, search=None, contact=None, date_from=None, date_to=None, status=None):
    category, kinds = posting.SCANNED_TYPES[document_type]
    rows = db.execute("""SELECT r.*,d.kind,d.id AS document_id,d.original_filename,
            COALESCE((SELECT SUM(a.amount) FROM settlement_allocations a
                JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id AND s.status='recorded'
                WHERE a.tenant_id=r.tenant_id AND a.record_id=r.id AND a.status='active'
                  AND NOT EXISTS (SELECT 1 FROM settlement_allocations u WHERE u.tenant_id=a.tenant_id AND u.reverses_allocation_id=a.id)),0) AS allocated
        FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
        WHERE r.tenant_id=%s AND r.is_current AND r.validation_status='passed' AND r.category=%s AND d.kind = ANY(%s)
            AND (%s::uuid IS NULL OR r.vendor_id=%s::uuid)
            AND (%s::date IS NULL OR r.invoice_date >= %s::date) AND (%s::date IS NULL OR r.invoice_date <= %s::date)
            AND (%s::text IS NULL OR COALESCE(r.invoice_number,'') ILIKE %s OR COALESCE(r.supplier_name,'') ILIKE %s
                 OR COALESCE(r.buyer_name,'') ILIKE %s OR r.record_code ILIKE %s)
        ORDER BY r.invoice_date DESC NULLS LAST LIMIT 1000""",
                      (tenant_id, category, list(kinds), contact, contact, date_from, date_from, date_to, date_to,
                       search, *([f"%{search}%"] * 4 if search else [None] * 4))).fetchall()
    result = []
    for r in rows:
        journal = posting.scanned_posted_journal(db, tenant_id, r)
        state = "ready" if journal else "approved"
        if status and status != state:
            continue
        total = Decimal(r["total_amount"] or 0)
        result.append({"id": str(r["id"]), "source": "scanned", "type": document_type, "number": r["invoice_number"] or r["record_code"],
                       "recordCode": r["record_code"], "documentId": str(r["document_id"]), "fileName": r["original_filename"],
                       "contactId": str(r["vendor_id"]) if r["vendor_id"] else None,
                       "contactName": r["supplier_name"] if category == "accounts_payable" else r["buyer_name"],
                       "status": state, "documentDate": str(r["invoice_date"]) if r["invoice_date"] else None,
                       "dueDate": str(r["due_date"]) if r["due_date"] else None, "currency": r["currency"],
                       "referenceNo": r["reference_number"], "subtotal": money(r["subtotal"]), "taxAmount": money(r["tax_amount"]),
                       "amount": money(total), "outstanding": money(max(total - Decimal(r["allocated"]), Decimal("0"))),
                       "posts": True, "posted": bool(journal), "journalId": str(journal["id"]) if journal else None,
                       "createdAt": str(r["created_at"]), "updatedAt": str(r["created_at"])})
    return result


def create_document(document_type: str, data: DocumentInput, request: Request):
    _type(document_type)
    user = erp_user(request, WRITE_ROLES)
    lines = _clean_lines(data.lines)
    allocations = _allocations(document_type, data.allocations)
    subtotal, tax, total = _totals(document_type, lines, data.amount)
    with connect() as db:
        vendor_id, customer_id, employee_id, contact_name = _contact(db, user["tenant_id"], document_type, data.contactId)
        bank_account_id = _bank_account(db, user["tenant_id"], data.bankAccountId)
        number = data.number.strip() if data.number and data.number.strip() else _next_number(db, user["tenant_id"], document_type)
        try:
            db.execute("SAVEPOINT erp_create")
            row = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,vendor_id,customer_id,
                    employee_id,contact_name,document_date,due_date,currency,reference_no,description,subtotal,tax_amount,total_amount,
                    payload,bank_account_id,created_by,updated_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                             (user["tenant_id"], document_type, number, "pending_approval" if data.status == "pending_approval" else "draft",
                              vendor_id, customer_id, employee_id, data.contactName or contact_name,
                              data.documentDate or date.today(), data.dueDate, data.currency.upper(), data.referenceNo,
                              data.description, subtotal, tax, total,
                              Jsonb({"lines": lines, "allocations": allocations, "tags": data.tags, "fields": data.fields}),
                              bank_account_id, user["id"], user["id"])).fetchone()
            db.execute("RELEASE SAVEPOINT erp_create")
        except UniqueViolation:
            db.execute("ROLLBACK TO SAVEPOINT erp_create")
            raise HTTPException(409, "A document with this number already exists.")
        _event(db, user, row["id"], "created", to_status=row["status"])
        if data.status == "ready":
            row = _make_ready(db, user, row)
            _event(db, user, row["id"], "status_changed", from_status="draft", to_status="ready",
                   reason="Posted to the ledger" if row["journal_id"] else "Record only: this type does not post")
        return _dto(db, row)


def get_document(document_type: str, document_id: str, request: Request):
    _type(document_type)
    user = erp_user(request, READ_ROLES)
    with connect() as db:
        return _dto(db, _load(db, user, document_type, document_id))


def update_document(document_type: str, document_id: str, data: DocumentPatch, request: Request):
    _type(document_type)
    user = erp_user(request, WRITE_ROLES)
    changes = data.model_dump(exclude_unset=True)
    with connect() as db:
        row = _load(db, user, document_type, document_id, for_update=True)
        if row["status"] not in {"draft", "pending_approval"}:
            raise HTTPException(409, "Only a draft or pending document can be edited. Void a ready document and create a new one.")
        payload = dict(row["payload"] or {})
        if "lines" in changes:
            payload["lines"] = _clean_lines(data.lines or [])
        if "allocations" in changes:
            payload["allocations"] = _allocations(document_type, data.allocations or [])
        for key in ("tags", "fields"):
            if key in changes:
                payload[key] = changes[key] if changes[key] is not None else ([] if key == "tags" else {})
        amount = changes["amount"] if "amount" in changes else row["total_amount"]
        subtotal, tax, total = _totals(document_type, payload.get("lines", []), amount)
        vendor_id, customer_id, employee_id = row["vendor_id"], row["customer_id"], row["employee_id"]
        contact_name = changes.get("contactName", row["contact_name"])
        if "contactId" in changes:
            vendor_id, customer_id, employee_id, name = _contact(db, user["tenant_id"], document_type, data.contactId)
            contact_name = data.contactName or name
        bank_account_id = _bank_account(db, user["tenant_id"], data.bankAccountId) if "bankAccountId" in changes else row["bank_account_id"]
        updated = db.execute("""UPDATE erp_document_headers SET vendor_id=%s,customer_id=%s,employee_id=%s,contact_name=%s,
                document_date=%s,due_date=%s,currency=%s,reference_no=%s,description=%s,subtotal=%s,tax_amount=%s,total_amount=%s,
                payload=%s,bank_account_id=%s,updated_by=%s,updated_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""",
                             (vendor_id, customer_id, employee_id, contact_name, changes.get("documentDate") or row["document_date"],
                              changes.get("dueDate", row["due_date"]), (changes.get("currency") or row["currency"]).upper(),
                              changes.get("referenceNo", row["reference_no"]), changes.get("description", row["description"]),
                              subtotal, tax, total, Jsonb(payload), bank_account_id, user["id"], row["id"], user["tenant_id"])).fetchone()
        _event(db, user, row["id"], "updated", from_status=row["status"], to_status=row["status"])
        return _dto(db, updated)


def delete_document(document_type: str, document_id: str, request: Request):
    _type(document_type)
    user = erp_user(request, WRITE_ROLES)
    with connect() as db:
        row = _load(db, user, document_type, document_id, for_update=True)
        if row["status"] != "draft":
            raise HTTPException(409, "Only draft documents can be deleted. Void a ready document instead.")
        if db.execute("SELECT 1 FROM erp_document_allocations WHERE tenant_id=%s AND target_document_id=%s LIMIT 1",
                      (user["tenant_id"], row["id"])).fetchone():
            raise HTTPException(409, "Other documents refer to this draft.")
        db.execute("DELETE FROM erp_document_headers WHERE id=%s AND tenant_id=%s", (row["id"], user["tenant_id"]))
        audit(db, user, "erp.document_deleted", str(row["id"]), {"type": document_type, "number": row["document_number"]})
    return {"id": str(row["id"]), "deleted": True}


def transition_document(document_type: str, document_id: str, data: TransitionInput, request: Request):
    _type(document_type)
    user = erp_user(request, WRITE_ROLES)
    with connect() as db:
        row = _load(db, user, document_type, document_id, for_update=True)
        if row["status"] == data.status and data.status in {"ready", "void"}:
            return {**_dto(db, row), "idempotent": True}  # a repeated "ready" posts once
        if data.status not in TRANSITIONS[row["status"]]:
            raise HTTPException(409, f"Cannot move a {row['status'].replace('_', ' ')} document to {data.status.replace('_', ' ')}.")
        if data.status == "ready":
            updated = _make_ready(db, user, row)
            reason = data.reason or ("Posted to the ledger" if updated["journal_id"] else "Record only: this type does not post")
        elif data.status == "void":
            updated = _void(db, user, row, data.reason)
            reason = data.reason or ("Voided; reversal journal posted" if updated["reversal_journal_id"] else "Voided")
        else:
            updated = db.execute("""UPDATE erp_document_headers SET status=%s,updated_by=%s,updated_at=now()
                WHERE id=%s AND tenant_id=%s RETURNING *""", (data.status, user["id"], row["id"], user["tenant_id"])).fetchone()
            reason = data.reason
        _event(db, user, row["id"], "status_changed", from_status=row["status"], to_status=data.status, reason=reason)
        return _dto(db, updated)


def _copy(db, user, source, target_type, link_type):
    payload = dict(source["payload"] or {})
    payload.pop("allocations", None)
    amount = source["total_amount"]
    vendor_id, customer_id = source["vendor_id"], source["customer_id"]
    if target_type in MONEY_ONLY:  # invoice -> payment: the outstanding amount, allocated to the invoice
        amount = typed_outstanding(db, user["tenant_id"], source["id"])
        if amount <= 0:
            raise HTTPException(409, f"{source['document_number']} has nothing outstanding.")
        payload = {"lines": [], "tags": payload.get("tags", []), "fields": {},
                   "allocations": [{"targetId": str(source["id"]), "source": "typed", "amount": money(amount)}]}
    elif target_type in {"credit-note", "purchase-credit-note"}:
        payload["allocations"] = [{"targetId": str(source["id"]), "source": "typed", "amount": money(source["total_amount"])}]
    payload[f"{link_type}From"] = str(source["id"])
    subtotal, tax, total = _totals(target_type, payload.get("lines", []), amount)
    row = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,vendor_id,customer_id,
            employee_id,contact_name,document_date,due_date,currency,reference_no,description,subtotal,tax_amount,total_amount,
            payload,bank_account_id,created_by,updated_by)
        VALUES (%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                     (user["tenant_id"], target_type, _next_number(db, user["tenant_id"], target_type), vendor_id, customer_id,
                      source["employee_id"], source["contact_name"], date.today(), source["due_date"], source["currency"],
                      source["document_number"] if link_type == "transfer" else source["reference_no"], source["description"],
                      subtotal, tax, total, Jsonb(payload), source["bank_account_id"], user["id"], user["id"])).fetchone()
    db.execute("""INSERT INTO erp_document_links(tenant_id,source_document_id,target_document_id,link_type,created_by)
        VALUES (%s,%s,%s,%s,%s)""", (user["tenant_id"], source["id"], row["id"], link_type, user["id"]))
    verb = "Copied" if link_type == "duplicate" else "Transferred"
    _event(db, user, row["id"], "duplicated" if link_type == "duplicate" else "transferred", to_status="draft",
           reason=f"{verb} from {LABELS[source['document_type']]} {source['document_number']}")
    return row


def duplicate_document(document_type: str, document_id: str, request: Request):
    _type(document_type)
    user = erp_user(request, WRITE_ROLES)
    with connect() as db:
        source = _load(db, user, document_type, document_id)
        return _dto(db, _copy(db, user, source, document_type, "duplicate"))


def transfer_document(document_type: str, document_id: str, data: TransferInput, request: Request):
    _type(document_type)
    target_type = _type(data.targetType)
    user = erp_user(request, WRITE_ROLES)
    if target_type not in TRANSFERS.get(document_type, set()):
        raise HTTPException(422, f"A {LABELS[document_type].lower()} cannot be turned into a {LABELS[target_type].lower()}.")
    with connect() as db:
        source = _load(db, user, document_type, document_id, for_update=True)
        if source["status"] not in {"ready", "pending_approval"}:
            raise HTTPException(409, "Only a pending or ready document can be transferred.")
        if target_type in MONEY_ONLY | {"credit-note", "purchase-credit-note"} and source["status"] != "ready":
            raise HTTPException(409, "Only a ready (posted) document can be paid or credited.")
        return _dto(db, _copy(db, user, source, target_type, "transfer"))


def document_history(document_type: str, document_id: str, request: Request):
    _type(document_type)
    user = erp_user(request, READ_ROLES)
    with connect() as db:
        row = _load(db, user, document_type, document_id)
        events = db.execute("""SELECT e.*,u.name AS actor_name FROM erp_document_events e LEFT JOIN users u ON u.id=e.actor_id
            WHERE e.document_id=%s AND e.tenant_id=%s ORDER BY e.created_at,e.id""", (row["id"], user["tenant_id"])).fetchall()
        links = db.execute("""SELECT l.link_type,l.source_document_id,l.target_document_id,s.document_type AS source_type,
                s.document_number AS source_number,t.document_type AS target_type,t.document_number AS target_number
            FROM erp_document_links l
            JOIN erp_document_headers s ON s.id=l.source_document_id AND s.tenant_id=l.tenant_id
            JOIN erp_document_headers t ON t.id=l.target_document_id AND t.tenant_id=l.tenant_id
            WHERE l.tenant_id=%s AND (l.source_document_id=%s OR l.target_document_id=%s)""",
                           (user["tenant_id"], row["id"], row["id"])).fetchall()
        allocations = db.execute("""SELECT a.amount,a.status,s.document_type AS source_type,s.document_number AS source_number,
                t.document_type AS target_type,t.document_number AS target_number,s.id AS source_id,t.id AS target_id
            FROM erp_document_allocations a
            JOIN erp_document_headers s ON s.id=a.source_document_id AND s.tenant_id=a.tenant_id
            JOIN erp_document_headers t ON t.id=a.target_document_id AND t.tenant_id=a.tenant_id
            WHERE a.tenant_id=%s AND (a.source_document_id=%s OR a.target_document_id=%s) ORDER BY a.created_at""",
                                 (user["tenant_id"], row["id"], row["id"])).fetchall()
    return {"events": [{"id": str(e["id"]), "eventType": e["event_type"], "fromStatus": e["from_status"], "toStatus": e["to_status"],
                        "reason": e["reason"], "actorName": e["actor_name"], "createdAt": str(e["created_at"])} for e in events],
            "links": [{"linkType": l["link_type"], "sourceId": str(l["source_document_id"]), "sourceType": l["source_type"],
                       "sourceNumber": l["source_number"], "targetId": str(l["target_document_id"]), "targetType": l["target_type"],
                       "targetNumber": l["target_number"]} for l in links],
            "allocations": [{"amount": money(a["amount"]), "status": a["status"], "sourceId": str(a["source_id"]),
                             "sourceType": a["source_type"], "sourceNumber": a["source_number"], "targetId": str(a["target_id"]),
                             "targetType": a["target_type"], "targetNumber": a["target_number"]} for a in allocations]}


def post_scanned_record(record_id: str, data: ScannedPostInput, request: Request):
    """L5: post an approved scanned invoice/bill/credit note through the same rule; posts once across both paths."""
    user = erp_user(request, POST_ROLES)
    record_id = parse_uuid(record_id, "record id")
    with connect() as db:
        record = db.execute("""SELECT r.*,d.kind FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
            WHERE r.id=%s AND r.tenant_id=%s FOR UPDATE OF r""", (record_id, user["tenant_id"])).fetchone()
        if not record:
            raise HTTPException(404, "Record not found.")
        if not record["is_current"] or record["validation_status"] != "passed":
            raise HTTPException(409, "Only the current approved record can be posted.")
        if not any(record["category"] == c and record["kind"] in k for c, k in posting.SCANNED_TYPES.values()):
            raise HTTPException(422, "Only an approved invoice, bill or credit note posts from here.")
        existing = posting.scanned_posted_journal(db, user["tenant_id"], record)
        if existing:
            return {"id": str(existing["id"]), "status": existing["status"], "idempotent": True, "recordId": str(record_id)}
        if str(record["currency"]).strip().upper() != "MYR":
            raise HTTPException(409, "Posting supports MYR only; foreign-currency conversion is not configured.")
        on = data.journalDate or record["invoice_date"]
        if not on:
            raise HTTPException(422, "Choose a journal date; the document has no date.")
        plan = posting.plan_for_scanned(db, user["tenant_id"], record, data.accountId)
        result = posting.post_plan(db, user, plan, key=posting.scanned_keys(record)[0],
                                   source_type="adjustment" if record["kind"] == "credit_note" else "document",
                                   source_id=record["document_id"], journal_date=on,
                                   description=f"Scanned {record['kind'].replace('_', ' ')} {record['invoice_number'] or record['record_code']}")
        if not result["idempotent"]:
            audit(db, user, "erp.scanned_posted", str(record_id), {"journalId": result["id"], "recordCode": record["record_code"]})
    return {**result, "recordId": str(record_id)}


def get_posting_accounts(request: Request):
    user = erp_user(request, READ_ROLES)
    result = []
    with connect() as db:
        for purpose in posting.DEFAULT_CODES:
            try:
                account = posting.default_account(db, user["tenant_id"], purpose)
                result.append({"purpose": purpose, "accountId": str(account["id"]), "code": account["code"], "name": account["name"]})
            except HTTPException as missing:
                result.append({"purpose": purpose, "accountId": None, "code": None, "name": None, "problem": missing.detail})
    return result


def set_posting_accounts(data: PostingAccountsInput, request: Request):
    user = erp_user(request, POST_ROLES)
    with connect() as db:
        for purpose, account_id in data.accounts.items():
            if purpose not in posting.PURPOSE_TYPES:
                raise HTTPException(422, f"Unknown posting purpose {purpose}.")
            account = posting.ledger_account(db, user["tenant_id"], account_id, expected=posting.PURPOSE_TYPES[purpose],
                                             label=purpose.replace("_", " ") + " account")
            db.execute("""INSERT INTO erp_posting_accounts(tenant_id,purpose,account_id,updated_by) VALUES (%s,%s,%s,%s)
                ON CONFLICT (tenant_id,purpose) DO UPDATE SET account_id=EXCLUDED.account_id,updated_by=EXCLUDED.updated_by,updated_at=now()""",
                       (user["tenant_id"], purpose, account["id"], user["id"]))
            audit(db, user, "erp.posting_account_set", purpose, {"accountId": str(account["id"]), "code": account["code"]})
    return get_posting_accounts(request)


def register(router):
    router.add_api_route('/api/erp/posting-accounts', get_posting_accounts, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/posting-accounts', set_posting_accounts, methods=['PUT'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/scanned/{record_id}/post', post_scanned_record, methods=['POST'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}', list_documents, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}', create_document, methods=['POST'], status_code=201, tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}', get_document, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}', update_document, methods=['PATCH'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}', delete_document, methods=['DELETE'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/transition', transition_document, methods=['POST'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/duplicate', duplicate_document, methods=['POST'], status_code=201, tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/transfer', transfer_document, methods=['POST'], status_code=201, tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/history', document_history, methods=['GET'], tags=['ERP Documents'])
