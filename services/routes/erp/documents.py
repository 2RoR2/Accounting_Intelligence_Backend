"""Plan L4/L5: sales and purchase documents (latest_update's 23 document types) that reach the ledger.

Status: draft -> pending_approval -> ready -> void (latest_update's rules). "ready" is where a financial
document posts (posting.py); other types are record-only and the response says so. Only a company admin
makes a document ready or voids a ready one, as with the existing ledger endpoints. A ready document is
never edited or deleted: voiding a posted one adds a reversal journal.

L5: the invoice, bill and credit-note lists also show approved scanned documents (source "scanned"),
which post through the same rule and the same idempotency keys as the record endpoints.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from main import *  # noqa: F401,F403  (Request, HTTPException, connect)
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from services import naming

from . import posting
from .common import DOCUMENT_TYPES, audit, erp_user, money, parse_uuid
from .contacts import find_contact, payment_term_days

logger = logging.getLogger(__name__)

READ_ROLES = {"local_admin", "accountant"}
WRITE_ROLES = {"local_admin", "accountant"}
POST_ROLES = {"local_admin"}
STATUSES = {"draft", "pending_approval", "ready", "void", "sent", "accepted", "expired"}
TRANSITIONS = {"draft": {"pending_approval", "ready", "void"}, "pending_approval": {"draft", "ready", "void"},
               "ready": {"void"}, "void": set()}
# Plan M: a quotation is sent (its "issue"), then accepted or expired; it never posts. Accepted -> converted to an invoice.
QUOTE_TRANSITIONS = {"draft": {"sent", "void"}, "pending_approval": {"draft", "sent", "void"},
                     "sent": {"accepted", "expired", "void"}, "accepted": {"void"},
                     "expired": {"sent", "void"}, "void": set()}
# Documents the company prints and sends: edited only as drafts; issuing stores an unchangeable PDF revision.
PRINTED_TYPES = {"quotation", "invoice", "credit-note", "debit-note", "payment", "purchase-payment", "delivery-order"}
# A note must reference the document it corrects (credit notes settle it; debit notes add to what is owed).
NOTE_TARGETS = {"credit-note": ("invoice", "debit-note"), "debit-note": ("invoice",),
                "purchase-credit-note": ("bill", "purchase-debit-note"), "purchase-debit-note": ("bill",)}
PREFIXES = {"quotation": "QT", "sales-order": "SO", "delivery-order": "DO", "invoice": "IV", "credit-note": "CN",
            "debit-note": "DN", "payment": "OR", "refund": "CR", "purchase-order": "PO", "goods-received": "GR",
            "bill": "BL", "purchase-credit-note": "SC", "purchase-debit-note": "SD", "purchase-payment": "PV",
            "purchase-refund": "SR", "money-in": "MI",
            "money-out": "MO", "bank-transfer": "FT", "stock-adjustment": "SA", "stock-transfer": "ST",
            "journal-entry": "JE", "contra": "CT", "deemed-payment": "DP", "store-order": "BS", "minipos-order": "POS"}
LABELS = {t: t.replace("-", " ").capitalize() for t in PREFIXES}
# Which document a ready/pending one can turn into (quotation -> invoice keeps the lines).
TRANSFERS = {"quotation": {"sales-order", "delivery-order", "invoice"}, "sales-order": {"delivery-order", "invoice"},
             "delivery-order": {"invoice"}, "invoice": {"credit-note", "debit-note", "payment", "delivery-order"},
             "debit-note": {"credit-note", "payment"},
             "purchase-order": {"goods-received", "bill"}, "goods-received": {"bill"},
             "bill": {"purchase-credit-note", "purchase-debit-note", "purchase-payment"},
             "purchase-debit-note": {"purchase-credit-note", "purchase-payment"}}
MONEY_ONLY = {"payment", "refund", "purchase-payment", "purchase-refund", "bank-transfer"}
# Open items carry a due date (the frontend's hasOutstanding).
OUTSTANDING_TYPES = {"invoice", "bill", "debit-note", "purchase-debit-note"}


def due_date_from_terms(document_date: date, days: int | None) -> date | None:
    """Document date plus the contact's payment terms; None when the contact has no terms."""
    return document_date + timedelta(days=days) if days is not None else None


def _converted_due_date(db, tenant_id, source, target_type, document_date) -> date | None:
    """The due date a transferred document starts with. An open item made from something that has no payment due date
    of its own (a quotation, order or delivery order; a quotation's date is its validity) is due on the contact's terms
    (none set: no due date). Anything else keeps the source's due date, as before."""
    if target_type not in OUTSTANDING_TYPES or source["document_type"] in OUTSTANDING_TYPES:
        return source["due_date"]
    contact = None
    if source["customer_id"]:
        contact = db.execute("SELECT fields FROM customers WHERE id=%s AND tenant_id=%s", (source["customer_id"], tenant_id)).fetchone()
    elif source["vendor_id"]:
        contact = db.execute("SELECT fields FROM vendors WHERE id=%s AND tenant_id=%s", (source["vendor_id"], tenant_id)).fetchone()
    days = payment_term_days(contact)
    if days is None and source["vendor_id"]:
        days = _imported_supplier_terms(db, tenant_id, source["vendor_id"])
    return due_date_from_terms(document_date, days)


def _imported_supplier_terms(db, tenant_id, vendor_id) -> int | None:
    """Payment terms the contacts CSV import kept for a supplier (its supplier_contacts notes), used when the contact
    page has none of its own."""
    rows = db.execute("""SELECT notes FROM supplier_contacts WHERE tenant_id=%s AND vendor_id=%s AND notes<>''
        ORDER BY created_at DESC""", (tenant_id, vendor_id)).fetchall()
    for row in rows:
        try:
            notes = json.loads(row["notes"])
        except (TypeError, ValueError):
            continue
        if isinstance(notes, dict) and notes.get("source") == "contacts-import":
            days = payment_term_days({"fields": {"paymentTermDays": notes.get("payment_terms_days")}})
            if days is not None:
                return days
    return None


class LineInput(BaseModel):
    itemId: str | None = None
    description: str = Field(default="", max_length=500)
    quantity: Decimal = Field(default=Decimal("1"), gt=0, max_digits=15, decimal_places=3)
    unitPrice: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=4)
    discount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    tax: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    # A label from the editable tax-code catalog; the tax amount is typed, never calculated (Plan M).
    taxCode: str | None = Field(default=None, max_length=40)
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
    # Credit/debit notes: the issued invoice or bill they correct.
    relatedDocumentId: str | None = None


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
    relatedDocumentId: str | None = None


class TransitionInput(BaseModel):
    status: str = Field(pattern=r"^(draft|pending_approval|ready|void|sent|accepted|expired)$")
    reason: str | None = Field(default=None, max_length=1000)


class TransferInput(BaseModel):
    targetType: str = Field(min_length=1, max_length=50)


class ScannedPostInput(BaseModel):
    accountId: str | None = None
    journalDate: date | None = None
    expectedPlanHash: str | None = None


class ScannedBulkPostInput(BaseModel):
    recordIds: list[str] = Field(min_length=1, max_length=100)


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


def _next_number(db, tenant_id, document_type: str, on: date | None = None) -> str:
    """Next number per type and year, e.g. IV-2026-00001 (Plan M). A company number format (catalog) keeps its
    own prefix/padding and adds the year only with includeYear. The counter row lock serialises concurrent
    requests; the unique (tenant, type, number) constraint is the backstop."""
    fmt = db.execute("""SELECT data FROM erp_catalog_entries WHERE tenant_id=%s AND kind='number-format' AND is_active
        AND data->>'documentType'=%s ORDER BY created_at LIMIT 1""", (tenant_id, document_type)).fetchone()
    prefix = (fmt["data"].get("prefix") if fmt else None) or PREFIXES.get(document_type, "TX")
    padding = int((fmt["data"].get("padding") if fmt else None) or 5)
    year = (on or date.today()).year
    with_year = bool(fmt["data"].get("includeYear")) if fmt else True
    counter = f"{document_type}:{year}" if with_year else document_type
    db.execute("""INSERT INTO erp_number_counters(tenant_id,document_type,next_value) VALUES (%s,%s,1)
        ON CONFLICT (tenant_id,document_type) DO NOTHING""", (tenant_id, counter))
    while True:
        value = db.execute("""UPDATE erp_number_counters SET next_value=next_value+1 WHERE tenant_id=%s AND document_type=%s
            RETURNING next_value-1 AS value""", (tenant_id, counter)).fetchone()["value"]
        number = f"{prefix}-{year}-{int(value):0{padding}d}" if with_year else f"{prefix}-{int(value):0{padding}d}"
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
        if any(posting.line_net(l) < 0 for l in lines):
            raise HTTPException(422, "A line discount cannot be more than the line amount.")
        subtotal = sum((posting.line_net(l) for l in lines), Decimal("0"))
        tax = sum((posting.cents(l.get("tax")) for l in lines), Decimal("0"))
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
        if item["source"] == "scanned" and document_type == "credit-note":
            raise HTTPException(422, "A credit note can only be allocated to an invoice issued here (Sales).")
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
              "voidedAt": str(row["voided_at"]) if row["voided_at"] else None,
              "relatedDocumentId": payload.get("relatedDocumentId"), "printed": row["document_type"] in PRINTED_TYPES}
    code = row["contact_code"] if "contact_code" in row else _contact_code(db, row)
    result["contactCode"] = code
    issued_name = row["issued_file_name"] if "issued_file_name" in row else db.execute(
        "SELECT file_name FROM erp_document_files WHERE tenant_id=%s AND document_id=%s AND revision=1",
        (row["tenant_id"], row["id"])).fetchone()
    if issued_name and not isinstance(issued_name, str):
        issued_name = issued_name["file_name"]
    # Plan M4: the one display/download name, from the record (S2 lists show this field).
    result["displayName"] = issued_name or naming.display_name(row["document_date"], code, naming.type_code(row["document_type"]),
                                                               row["document_number"], ".pdf")
    if row["document_type"] in posting.OPEN_ITEM_TYPES:
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


def _contact_code(db, row):
    if row["customer_id"]:
        found = db.execute("SELECT contact_code FROM customers WHERE id=%s AND tenant_id=%s", (row["customer_id"], row["tenant_id"])).fetchone()
    elif row["vendor_id"]:
        found = db.execute("SELECT contact_code FROM vendors WHERE id=%s AND tenant_id=%s", (row["vendor_id"], row["tenant_id"])).fetchone()
    else:
        return None
    return (found["contact_code"] or "").strip().upper() or None if found else None


CONTACT_CODE_SQL = """(SELECT NULLIF(upper(btrim(COALESCE(c.contact_code, v.contact_code))),'')
    FROM (SELECT 1) one LEFT JOIN customers c ON c.id=h.customer_id AND c.tenant_id=h.tenant_id
    LEFT JOIN vendors v ON v.id=h.vendor_id AND v.tenant_id=h.tenant_id) AS contact_code,
    (SELECT f.file_name FROM erp_document_files f WHERE f.tenant_id=h.tenant_id AND f.document_id=h.id AND f.revision=1)
        AS issued_file_name"""


def _related(db, tenant_id, document_type, related_id, *, lock=False):
    """The invoice/bill (or debit note) a credit or debit note corrects; checks type and tenant."""
    allowed = NOTE_TARGETS[document_type]
    row = db.execute(f"""SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type = ANY(%s)
        {'FOR UPDATE' if lock else ''}""", (parse_uuid(related_id, "related document id"), tenant_id, list(allowed))).fetchone()
    if not row:
        raise HTTPException(422, f"Choose the {' or '.join(t.replace('-', ' ') for t in allowed)} this {document_type.replace('-', ' ')} corrects.")
    return row


def _possible_duplicates(db, row) -> list[dict]:
    """Plan M duplicate warning (not a block): same type and contact, and the same reference, or the same amount
    on the same date. Void documents do not count."""
    contact = row["customer_id"] or row["vendor_id"]
    if not contact:
        return []
    found = db.execute("""SELECT id,document_number,status,document_date,total_amount,reference_no FROM erp_document_headers
        WHERE tenant_id=%s AND document_type=%s AND id<>%s AND status<>'void' AND COALESCE(customer_id,vendor_id)=%s
          AND ((NULLIF(btrim(COALESCE(%s,'')),'') IS NOT NULL AND lower(btrim(reference_no))=lower(btrim(%s)))
               OR (total_amount=%s AND document_date=%s AND total_amount>0))
        ORDER BY created_at LIMIT 5""", (row["tenant_id"], row["document_type"], row["id"], contact, row["reference_no"],
                                       row["reference_no"], row["total_amount"], row["document_date"])).fetchall()
    return [{"id": str(r["id"]), "number": r["document_number"], "status": r["status"], "documentDate": str(r["document_date"]),
             "amount": money(r["total_amount"]),
             "reason": "same reference" if row["reference_no"] and (r["reference_no"] or "").strip().lower() == row["reference_no"].strip().lower()
             else "same amount and date"} for r in found]


def _detail(db, row) -> dict:
    """Detail DTO: the list DTO plus the duplicate warning and the stored printed copies."""
    result = _dto(db, row)
    result["possibleDuplicates"] = _possible_duplicates(db, row)
    for allocation in result["allocations"]:  # the number a person recognises, typed or scanned
        if allocation.get("source") == "scanned":
            target = db.execute("""SELECT COALESCE(NULLIF(btrim(invoice_number),''),reference_number,record_code) AS number
                FROM standardised_records WHERE id=%s AND tenant_id=%s""", (allocation["targetId"], row["tenant_id"])).fetchone()
        else:
            target = db.execute("SELECT document_number AS number FROM erp_document_headers WHERE id=%s AND tenant_id=%s",
                                (allocation["targetId"], row["tenant_id"])).fetchone()
        allocation["targetNumber"] = target["number"] if target else None
    result["files"] = _file_list(db, row["tenant_id"], row["id"])
    related = (row["payload"] or {}).get("relatedDocumentId")
    if related:
        target = db.execute("SELECT document_type,document_number,status FROM erp_document_headers WHERE id=%s AND tenant_id=%s",
                            (related, row["tenant_id"])).fetchone()
        if target:
            result["related"] = {"id": related, "type": target["document_type"], "number": target["document_number"],
                                 "status": target["status"]}
    return result


# ------------------------------------------------------------------ printed copies (Plan M B8)

def _company_for_print(db, tenant_id) -> dict:
    row = db.execute("SELECT name,registration_number,profile FROM tenants WHERE id=%s", (tenant_id,)).fetchone()
    profile = (row["profile"] if row else None) or {}
    return {"name": row["name"] if row else "", "registrationNumber": row["registration_number"] if row else "",
            **{k: profile.get(k) for k in ("tin", "sstNumber", "address", "phone", "email", "bankName", "bankAccountName",
                                            "bankAccountNumber", "paymentTerms", "documentTerms")}}


def _contact_for_print(db, row) -> dict:
    if row["customer_id"]:
        c = db.execute("""SELECT customer_name AS name,contact_code,registration_number,address,city,state,postcode,phone,email,
                fields FROM customers WHERE id=%s AND tenant_id=%s""", (row["customer_id"], row["tenant_id"])).fetchone()
    elif row["vendor_id"]:
        c = db.execute("""SELECT vendor_name AS name,contact_code,registration_number,address,city,state,postcode,phone,email,
                fields FROM vendors WHERE id=%s AND tenant_id=%s""", (row["vendor_id"], row["tenant_id"])).fetchone()
    else:
        c = None
    if not c:
        return {"name": row["contact_name"] or ""}
    address = "\n".join(x for x in [c.get("address"), " ".join(p for p in [c.get("postcode"), c.get("city")] if p), c.get("state")] if x)
    return {"name": row["contact_name"] or c["name"], "code": c.get("contact_code"), "registrationNumber": c.get("registration_number"),
            "tin": (c.get("fields") or {}).get("tin"), "address": address, "phone": c.get("phone"), "email": c.get("email")}


CONTACT_LABELS = {"quotation": "Quotation for", "invoice": "Bill to", "credit-note": "Credit to", "debit-note": "Bill to",
                  "payment": "Received from", "purchase-payment": "Pay to", "delivery-order": "Deliver to"}


def _print_data(db, row, *, revision=None, issued_at=None) -> dict:
    payload = row["payload"] or {}
    fields = payload.get("fields") or {}
    lines = [{"description": l.get("description") or "", "quantity": str(l.get("quantity") or ""),
              "unitPrice": _unit_price(l.get("unitPrice")), "discount": money(l.get("discount") or 0),
              "taxCode": l.get("taxCode") or "", "tax": money(l.get("tax") or 0), "amount": money(posting.line_net(l) + posting.cents(l.get("discount") or 0))}
             for l in payload.get("lines", [])]
    # What the payment/credit settles, typed or scanned, from the document's own allocation list (Codex review P2).
    allocations = []
    for a in payload.get("allocations", []):
        if a.get("source") == "scanned":
            target = db.execute("""SELECT COALESCE(NULLIF(btrim(invoice_number),''),reference_number,record_code) AS number,
                    invoice_date AS day FROM standardised_records WHERE id=%s AND tenant_id=%s""", (a["targetId"], row["tenant_id"])).fetchone()
        else:
            target = db.execute("SELECT document_number AS number,document_date AS day FROM erp_document_headers WHERE id=%s AND tenant_id=%s",
                                (a["targetId"], row["tenant_id"])).fetchone()
        allocations.append({"number": target["number"] if target else "", "date": str(target["day"]) if target and target["day"] else "",
                            "amount": money(a.get("amount") or 0)})
    discount = sum((posting.cents(l.get("discount") or 0) for l in payload.get("lines", [])), Decimal("0"))
    data = {"type": row["document_type"], "number": row["document_number"], "documentDate": str(row["document_date"]),
            # Only an open item (invoice, bill, debit note) is due: a credit note or receipt copied from one prints no due date.
            "dueDate": str(row["due_date"]) if row["due_date"] and row["document_type"] in OUTSTANDING_TYPES else None,
            "referenceNo": row["reference_no"], "paymentTerms": fields.get("paymentTerms"), "currency": row["currency"], "contact": _contact_for_print(db, row),
            "contactLabel": CONTACT_LABELS.get(row["document_type"], "Contact"), "lines": lines,
            "subtotal": money(Decimal(row["subtotal"]) + discount), "discountTotal": money(discount), "taxAmount": money(row["tax_amount"]),
            "total": money(row["total_amount"]), "allocations": allocations, "notes": row["description"] or fields.get("notes"),
            "terms": fields.get("terms"), "paymentMethod": fields.get("paymentMethod"), "revision": revision,
            "issuedAt": issued_at}
    related = payload.get("relatedDocumentId") or payload.get("transferFrom")
    if related:
        target = db.execute("SELECT document_type,document_number FROM erp_document_headers WHERE id=%s AND tenant_id=%s",
                            (related, row["tenant_id"])).fetchone()
        if target:
            data["relatedNumber"] = target["document_number"]
            data["relatedLabel"] = "Original " + target["document_type"].replace("-", " ")
    return data


def _unit_price(value) -> str:
    """Unit prices keep the precision typed (up to 4 dp, at least 2); amounts are always 2 dp."""
    amount = Decimal(str(value or 0))
    text = format(amount.quantize(Decimal("0.0001")).normalize(), "f")
    whole, _, cents = text.partition(".")
    return f"{whole}.{(cents or '').ljust(2, '0')}"


def _file_name(db, row) -> str:
    code = row["contact_code"] if "contact_code" in row else _contact_code(db, row)
    return naming.display_name(row["document_date"], code, naming.type_code(row["document_type"]), row["document_number"], ".pdf")


def _render(db, row, *, watermark=None, revision=None, issued_at=None) -> bytes:
    from .pdf import render_document_pdf
    company = _company_for_print(db, row["tenant_id"])
    data = _print_data(db, row, revision=revision, issued_at=issued_at)
    # The company's standard terms print unless the document carries its own.
    if row["document_type"] in {"quotation", "invoice", "debit-note"}:
        data["paymentTerms"] = data.get("paymentTerms") or company.get("paymentTerms")
    data["terms"] = data.get("terms") or company.get("documentTerms")
    return render_document_pdf(data, company, watermark=watermark)


def _store_copy(db, user, row, reason: str) -> dict:
    """Append the next revision of the printed copy. The table refuses updates and deletes (unchangeable)."""
    if row["document_type"] not in PRINTED_TYPES:
        return {}
    if row["customer_id"]:
        naming.ensure_contact_code(db, row["tenant_id"], "customer", row["customer_id"])
    elif row["vendor_id"]:
        naming.ensure_contact_code(db, row["tenant_id"], "supplier", row["vendor_id"])
    current = db.execute("SELECT COALESCE(MAX(revision),0) AS n FROM erp_document_files WHERE tenant_id=%s AND document_id=%s",
                         (row["tenant_id"], row["id"])).fetchone()["n"]
    revision = int(current) + 1
    issued_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    content = _render(db, row, watermark="VOID" if reason == "void" else None, revision=revision, issued_at=issued_at)
    name = _file_name(db, row)
    if revision > 1:
        name = name[:-4] + f"_rev{revision}.pdf"
    stored = db.execute("""INSERT INTO erp_document_files(tenant_id,document_id,revision,reason,file_name,content,byte_size,sha256,created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,revision,file_name,sha256""",
                        (row["tenant_id"], row["id"], revision, reason, name, content, len(content),
                         hashlib.sha256(content).hexdigest(), user["id"])).fetchone()
    _event(db, user, row["id"], "printed", from_status=row["status"], to_status=row["status"],
           reason=f"Printed copy revision {revision} ({reason}) stored as {name}")
    return dict(stored)


def _file_list(db, tenant_id, document_id) -> list[dict]:
    return [{"revision": f["revision"], "reason": f["reason"], "fileName": f["file_name"], "byteSize": f["byte_size"],
             "sha256": f["sha256"], "createdAt": str(f["created_at"])}
            for f in db.execute("""SELECT revision,reason,file_name,byte_size,sha256,created_at FROM erp_document_files
                WHERE tenant_id=%s AND document_id=%s ORDER BY revision""", (tenant_id, document_id)).fetchall()]


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
    # Plan M B1: an excluded own-sales capture (its journal reversed by planm-tidy) no longer blocks the typed entry.
    rows = db.execute("""SELECT r.record_code,r.vendor_id,r.supplier_name,r.buyer_name FROM standardised_records r
        JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id AND d.excluded_at IS NULL
        WHERE r.tenant_id=%s AND r.is_current AND r.validation_status='passed' AND r.category=%s
            AND lower(btrim(COALESCE(NULLIF(btrim(r.invoice_number),''), r.reference_number)))=lower(btrim(%s))""",
                      (tenant_id, category, number)).fetchall()
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
    target_types = posting.ALLOCATING_TYPES[doc["document_type"]]
    target_type = target_types[0]  # invoice or bill: the label, and the scanned list it can settle
    total = sum((posting.cents(a["amount"]) for a in allocations), Decimal("0"))
    if total > posting.cents(doc["total_amount"]):
        raise HTTPException(422, "The allocations add up to more than the document amount.")
    settlement_id = None
    # Lock the targets in one order so two payments for the same invoices cannot deadlock.
    for a in sorted(allocations, key=lambda x: (x["source"], x["targetId"])):
        amount = posting.cents(a["amount"])
        if a["source"] == "typed":
            target = db.execute("""SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type = ANY(%s) FOR UPDATE""",
                                (a["targetId"], user["tenant_id"], list(target_types))).fetchone()
            if not target or target["status"] != "ready":
                raise HTTPException(409, f"Allocate only to an issued {' or '.join(t.replace('-', ' ') for t in target_types)}.")
            if (target["customer_id"] or target["vendor_id"]) != (doc["customer_id"] or doc["vendor_id"]):
                raise HTTPException(422, f"The {target_type} {target['document_number']} belongs to another contact.")
            if amount > typed_outstanding(db, user["tenant_id"], target["id"]):
                raise HTTPException(409, f"{target['document_number']} has less than {amount} outstanding.")
            db.execute("""INSERT INTO erp_document_allocations(tenant_id,source_document_id,target_document_id,amount,created_by)
                VALUES (%s,%s,%s,%s,%s)""", (user["tenant_id"], doc["id"], target["id"], amount, user["id"]))
        else:
            category, kinds = posting.SCANNED_TYPES[target_type]
            record = db.execute("""SELECT r.id,r.vendor_id,r.supplier_name,r.buyer_name,r.record_code FROM standardised_records r
                JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
                WHERE r.id=%s AND r.tenant_id=%s AND r.is_current AND r.validation_status='passed' AND r.category=%s AND d.kind = ANY(%s)""",
                                (a["targetId"], user["tenant_id"], category, list(kinds))).fetchone()
            if not record:
                raise HTTPException(409, f"Allocate only to a current approved scanned {target_type}.")
            if category == "accounts_payable":
                same = (record["vendor_id"] and record["vendor_id"] == doc["vendor_id"]) or posting._same_party(record["supplier_name"], doc["contact_name"])
            else:
                same = posting._same_party(record["buyer_name"], doc["contact_name"])
            if not same:
                raise HTTPException(422, f"The scanned {target_type} {record['record_code']} belongs to another contact.")
            if settlement_id is None:
                scanned_total = sum((posting.cents(x["amount"]) for x in allocations if x["source"] == "scanned"), Decimal("0"))
                settlement_id = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,
                        source_type,source_id,created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,'manual_record',%s,%s) RETURNING id""",
                                           (user["tenant_id"], f"ERP-{doc['document_number']}-{str(doc['id'])[:8]}", doc["document_date"],
                                            "money_in" if doc["document_type"] == "payment" else "money_out", scanned_total,
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


def _make_ready(db, user, doc, *, bank_evidence=False):
    """Post a financial document (or mark a record-only one ready). The caller holds the row lock."""
    bank_posting_allowed = bank_evidence and user["role"] == "accountant" and doc["document_type"] in {"money-in", "money-out"}
    if user["role"] not in POST_ROLES and not bank_posting_allowed:
        raise HTTPException(403, "Only a company admin can make a document ready (post it).")
    kind = doc["document_type"]
    if kind in posting.CONTACT_REQUIRED and not (doc["vendor_id"] or doc["customer_id"]):
        raise HTTPException(422, f"Choose the {'supplier' if kind in posting.PURCHASE_TYPES else 'customer'} first.")
    lines = (doc["payload"] or {}).get("lines", [])
    if kind in {"invoice", "credit-note", "debit-note", "bill", "purchase-credit-note", "purchase-debit-note"} and not lines:
        raise HTTPException(422, "Add at least one line.")
    if kind in posting.POSTING_TYPES and posting.cents(doc["total_amount"]) <= 0:
        raise HTTPException(422, "The document total must be more than zero.")
    if kind in NOTE_TARGETS:
        doc = _check_note(db, user, doc)
    journal_id = settlement_id = None
    if kind in posting.POSTING_TYPES:
        if doc["currency"] != "MYR":
            raise HTTPException(409, "Posting supports MYR only; foreign-currency conversion is not configured.")
        _check_duplicate_of_scanned(db, user["tenant_id"], doc)
        scanned = sorted(a["targetId"] for a in (doc["payload"] or {}).get("allocations", []) if a.get("source") == "scanned")
        if scanned:  # same lock order as scanned posting: records before the period (Codex review P2)
            db.execute("SELECT id FROM standardised_records WHERE tenant_id=%s AND id = ANY(%s) ORDER BY id FOR UPDATE",
                       (user["tenant_id"], scanned))
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
    _store_copy(db, user, row, "issued")
    return row


def _check_note(db, user, doc):
    """Credit/debit note rules (Plan M): it references an issued invoice/bill of the same contact; a credit note
    settles exactly its total against that document, so it can never exceed what is outstanding there.
    Returns the document with its allocations filled in (saved on the row)."""
    kind = doc["document_type"]
    payload = dict(doc["payload"] or {})
    allocations = payload.get("allocations", [])
    related_id = payload.get("relatedDocumentId")
    if not related_id:
        typed = [a for a in allocations if a.get("source") == "typed"]
        related_id = typed[0]["targetId"] if typed else None
    scanned_only = not related_id and kind == "purchase-credit-note" and any(a.get("source") == "scanned" for a in allocations)
    if not related_id and not scanned_only:
        label = " or ".join(t.replace("-", " ") for t in NOTE_TARGETS[kind])
        raise HTTPException(422, f"A {kind.replace('-', ' ')} must reference an issued {label}. Choose it first.")
    if related_id:
        # Same lock order as a payment: the accounting period first (record_journal), then the invoice/bill.
        period = posting.open_period(db, user["tenant_id"], doc["document_date"])
        db.execute("SELECT id FROM accounting_periods WHERE id=%s AND tenant_id=%s FOR UPDATE", (period["id"], user["tenant_id"]))
        related = _related(db, user["tenant_id"], kind, related_id, lock=True)
        if related["status"] != "ready":
            raise HTTPException(409, f"{related['document_number']} is not issued; a note can only correct an issued document.")
        if (related["customer_id"] or related["vendor_id"]) != (doc["customer_id"] or doc["vendor_id"]):
            raise HTTPException(422, f"{related['document_number']} belongs to another contact.")
        payload["relatedDocumentId"] = str(related["id"])
    total = posting.cents(doc["total_amount"])
    if kind in {"credit-note", "purchase-credit-note"}:
        if not allocations:
            allocations = [{"targetId": str(related["id"]), "source": "typed", "amount": money(total)}]
        if related_id and any(a.get("source") != "typed" or a["targetId"] != str(related["id"]) for a in allocations):
            raise HTTPException(422, f"A credit note is applied to the document it corrects ({related['document_number']}) only.")
        allocated = sum((posting.cents(a["amount"]) for a in allocations), Decimal("0"))
        if allocated != total:
            raise HTTPException(422, f"A credit note is applied in full: allocate {money(total)} (now {money(allocated)}).")
        for a in allocations:
            if a.get("source") == "typed":
                outstanding = typed_outstanding(db, user["tenant_id"], a["targetId"])
                if posting.cents(a["amount"]) > outstanding:
                    number = db.execute("SELECT document_number FROM erp_document_headers WHERE id=%s AND tenant_id=%s",
                                        (a["targetId"], user["tenant_id"])).fetchone()
                    raise HTTPException(409, f"The credit note ({money(a['amount'])}) is more than "
                                             f"{number['document_number'] if number else 'the document'} has outstanding ({money(outstanding)}).")
        payload["allocations"] = allocations
    if payload != (doc["payload"] or {}):
        doc = db.execute("UPDATE erp_document_headers SET payload=%s,updated_at=now() WHERE id=%s AND tenant_id=%s RETURNING *",
                         (Jsonb(payload), doc["id"], user["tenant_id"])).fetchone()
    return doc


def _void(db, user, doc, reason):
    if doc["status"] == "ready" and user["role"] not in POST_ROLES:
        raise HTTPException(403, "Only a company admin can void a ready document.")
    # Money documents created from a bank line, and vouchers/receipts matched to one, release that line when voided.
    releases_bank_line = doc["document_type"] in {"money-in", "money-out", "payment", "purchase-payment"}
    if releases_bank_line:
        # Lock the bank evidence before taking the journal/period locks.
        db.execute("""SELECT id FROM bank_transactions WHERE tenant_id=%s AND recorded_document_id=%s
            ORDER BY id FOR UPDATE""", (user["tenant_id"], doc["id"]))
    reversal = None
    if doc["status"] in {"ready", "sent", "accepted"} and doc["document_type"] in {"invoice", "bill"}:
        note = db.execute("""SELECT document_number FROM erp_document_headers WHERE tenant_id=%s AND status='ready'
            AND document_type IN ('debit-note','purchase-debit-note') AND payload->>'relatedDocumentId'=%s LIMIT 1""",
                          (user["tenant_id"], str(doc["id"]))).fetchone()
        if note:
            raise HTTPException(409, f"Debit note {note['document_number']} refers to this {doc['document_type']}; void it first.")
    if doc["journal_id"]:
        if doc["document_type"] in posting.OPEN_ITEM_TYPES:
            used = db.execute("""SELECT h.document_number FROM erp_document_allocations a
                JOIN erp_document_headers h ON h.id=a.source_document_id AND h.tenant_id=a.tenant_id
                WHERE a.tenant_id=%s AND a.target_document_id=%s AND a.status='active' LIMIT 1""",
                              (user["tenant_id"], doc["id"])).fetchone()
            if used:
                raise HTTPException(409, f"{used['document_number']} is allocated to this {doc['document_type'].replace('-', ' ')}; void it first.")
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
    updated = db.execute("""UPDATE erp_document_headers SET status='void',voided_at=now(),void_reason=%s,reversal_journal_id=%s,
            updated_by=%s,updated_at=now() WHERE id=%s AND tenant_id=%s RETURNING *""",
                      (reason, reversal, user["id"], doc["id"], user["tenant_id"])).fetchone()
    if releases_bank_line:
        reopened = db.execute("""UPDATE bank_transactions SET status='imported',recorded_document_id=NULL
            WHERE tenant_id=%s AND recorded_document_id=%s RETURNING id""",
            (user["tenant_id"], doc["id"])).fetchall()
        for bank in reopened:
            audit(db, user, "bank.recorded_document_voided", bank["id"], {"documentId": str(doc["id"])})
    if db.execute("SELECT 1 FROM erp_document_files WHERE tenant_id=%s AND document_id=%s LIMIT 1",
                  (user["tenant_id"], doc["id"])).fetchone():
        _store_copy(db, user, updated, "void")  # the sent copy stays; a VOID revision records the cancellation
    return updated


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
            rows = db.execute(f"SELECT h.*,{CONTACT_CODE_SQL} FROM erp_document_headers h WHERE {' AND '.join(where)} "
                              "ORDER BY document_date DESC, created_at DESC LIMIT 1000", params).fetchall()
            items.extend(_dto(db, row) for row in rows)
        # Plan M B1/M2: Sales lists hold typed documents only; captured documents are purchases.
        if (source in {"all", "scanned"} and document_type in posting.SCANNED_TYPES
                and posting.SCANNED_TYPES[document_type][0] == "accounts_payable" and status in {None, "ready", "approved"}):
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
            AND (%s::uuid IS NULL OR (r.category='accounts_payable' AND r.vendor_id=%s::uuid)
                 OR (r.category='accounts_receivable'
                     AND lower(btrim(COALESCE(r.buyer_name,''))) = lower(btrim((SELECT customer_name FROM customers WHERE id=%s::uuid)))))
            AND (%s::date IS NULL OR r.invoice_date >= %s::date) AND (%s::date IS NULL OR r.invoice_date <= %s::date)
            AND (%s::text IS NULL OR COALESCE(r.invoice_number,'') ILIKE %s OR COALESCE(r.supplier_name,'') ILIKE %s
                 OR COALESCE(r.buyer_name,'') ILIKE %s OR r.record_code ILIKE %s)
        ORDER BY r.invoice_date DESC NULLS LAST LIMIT 1000""",
                      (tenant_id, category, list(kinds), contact, contact, contact, date_from, date_from, date_to, date_to,
                       search, *([f"%{search}%"] * 4 if search else [None] * 4))).fetchall()
    result = []
    codes: dict = {}
    for r in rows:
        journal = posting.scanned_posted_journal(db, tenant_id, r)
        posted = bool(journal and journal["status"] == "posted")
        state = "ready" if posted else "approved"  # a reversed journal no longer counts as posted
        if status and status != state:
            continue
        total = Decimal(r["total_amount"] or 0)
        due = Decimal(r["amount_due"] if r["amount_due"] is not None else total)
        party = r["supplier_name"] if category == "accounts_payable" else r["buyer_name"]
        code = codes.setdefault(party, naming.code_for_party_name(db, tenant_id, "supplier" if category == "accounts_payable" else "customer", party))
        result.append({"id": str(r["id"]), "source": "scanned", "type": document_type, "number": r["invoice_number"] or r["record_code"],
                       "contactCode": code,
                       "displayName": naming.display_name(r["invoice_date"], code, naming.type_code(kind=r["kind"], category=category),
                                                          r["invoice_number"] or r["reference_number"] or r["record_code"],
                                                          naming.extension_of(r["original_filename"])),
                       "recordCode": r["record_code"], "documentId": str(r["document_id"]), "fileName": r["original_filename"],
                       "contactId": str(r["vendor_id"]) if r["vendor_id"] and category == "accounts_payable" else None,
                       "contactName": r["supplier_name"] if category == "accounts_payable" else r["buyer_name"],
                       "status": state, "documentDate": str(r["invoice_date"]) if r["invoice_date"] else None,
                       "dueDate": str(r["due_date"]) if r["due_date"] else None, "currency": r["currency"],
                       "referenceNo": r["reference_number"], "subtotal": money(r["subtotal"]), "taxAmount": money(r["tax_amount"]),
                       "amount": money(total), "outstanding": money(max(due - Decimal(r["allocated"]), Decimal("0"))),
                       "posts": True, "posted": posted, "reversed": bool(journal and journal["status"] == "reversed"),
                       "journalId": str(journal["id"]) if journal else None,
                       "createdAt": str(r["created_at"]), "updatedAt": str(r["created_at"])})
    return result


def create_document(document_type: str, data: DocumentInput, request: Request):
    _type(document_type)
    user = erp_user(request, WRITE_ROLES)
    with connect() as db:
        return _detail(db, _create_typed_document(db, user, document_type, data))


def _check_related_input(db, user, document_type, related_id):
    if not related_id:
        return None
    if document_type not in NOTE_TARGETS:
        raise HTTPException(422, "Only credit and debit notes reference another document.")
    return str(_related(db, user["tenant_id"], document_type, related_id)["id"])


def _create_typed_document(db, user, document_type, data, *, bank_evidence=False):
    lines = _clean_lines(data.lines)
    allocations = _allocations(document_type, data.allocations)
    subtotal, tax, total = _totals(document_type, lines, data.amount)
    vendor_id, customer_id, employee_id, contact_name = _contact(db, user["tenant_id"], document_type, data.contactId)
    bank_account_id = _bank_account(db, user["tenant_id"], data.bankAccountId)
    related_id = _check_related_input(db, user, document_type, data.relatedDocumentId)
    if document_type == "quotation" and data.status == "pending_approval":
        raise HTTPException(422, "A quotation is a draft until it is sent; it has no approval step.")
    on = data.documentDate or date.today()
    number = data.number.strip() if data.number and data.number.strip() else _next_number(db, user["tenant_id"], document_type, on)
    try:
        db.execute("SAVEPOINT erp_create")
        row = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,vendor_id,customer_id,
                employee_id,contact_name,document_date,due_date,currency,reference_no,description,subtotal,tax_amount,total_amount,
                payload,bank_account_id,created_by,updated_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                         (user["tenant_id"], document_type, number, "pending_approval" if data.status == "pending_approval" else "draft",
                          vendor_id, customer_id, employee_id, data.contactName or contact_name,
                          on, data.dueDate, data.currency.upper(), data.referenceNo,
                          data.description, subtotal, tax, total,
                          Jsonb({"lines": lines, "allocations": allocations, "tags": data.tags, "fields": data.fields,
                                 **({"relatedDocumentId": related_id} if related_id else {})}),
                          bank_account_id, user["id"], user["id"])).fetchone()
        db.execute("RELEASE SAVEPOINT erp_create")
    except UniqueViolation:
        db.execute("ROLLBACK TO SAVEPOINT erp_create")
        raise HTTPException(409, "A document with this number already exists.")
    _event(db, user, row["id"], "created", to_status=row["status"])
    if data.status == "ready" and document_type == "quotation":  # issuing a quotation is sending it
        row = _send_quotation(db, user, row)
        _event(db, user, row["id"], "status_changed", from_status="draft", to_status="sent", reason="Sent to the customer")
    elif data.status == "ready":
        row = _make_ready(db, user, row, bank_evidence=bank_evidence)
        _event(db, user, row["id"], "status_changed", from_status="draft", to_status="ready",
               reason="Posted to the ledger" if row["journal_id"] else "Record only: this type does not post")
    return row


def create_document_from_bank(db, user, bank, bank_account_id, account_id, description):
    """Only statement-derived money-in/out can use this ready path as an accountant."""
    kind = bank["direction"].replace("_", "-")
    data = DocumentInput(
        documentDate=bank["posted_date"], currency=bank["currency"], bankAccountId=str(bank_account_id),
        referenceNo=(bank["reference"] or "")[:200] or None, description=description, status="ready",
        lines=[LineInput(description=description[:500], unitPrice=abs(bank["signed_amount_decimal"]),
                         accountId=str(account_id))],
    )
    return _create_typed_document(db, user, kind, data, bank_evidence=True)


def get_document(document_type: str, document_id: str, request: Request):
    _type(document_type)
    user = erp_user(request, READ_ROLES)
    with connect() as db:
        return _detail(db, _load(db, user, document_type, document_id))


def update_document(document_type: str, document_id: str, data: DocumentPatch, request: Request):
    _type(document_type)
    user = erp_user(request, WRITE_ROLES)
    changes = data.model_dump(exclude_unset=True)
    with connect() as db:
        row = _load(db, user, document_type, document_id, for_update=True)
        if document_type in PRINTED_TYPES and row["status"] != "draft":
            raise HTTPException(409, "Only a draft can be edited. An issued document is void-and-reissue only.")
        if row["status"] not in {"draft", "pending_approval"}:
            raise HTTPException(409, "Only a draft or pending document can be edited. Void a ready document and create a new one.")
        payload = dict(row["payload"] or {})
        if "relatedDocumentId" in changes:
            related_id = _check_related_input(db, user, document_type, data.relatedDocumentId)
            if related_id:
                payload["relatedDocumentId"] = related_id
            else:
                payload.pop("relatedDocumentId", None)
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
        return _detail(db, updated)


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
        if document_type == "quotation":
            return _transition_quotation(db, user, row, data)
        if row["status"] == data.status and data.status in {"ready", "void"}:
            return {**_detail(db, row), "idempotent": True}  # a repeated "ready" posts once
        if data.status not in TRANSITIONS.get(row["status"], set()):
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
        return _detail(db, updated)


def _send_quotation(db, user, row):
    if not (row["customer_id"]):
        raise HTTPException(422, "Choose the customer first.")
    if not (row["payload"] or {}).get("lines"):
        raise HTTPException(422, "Add at least one line.")
    updated = db.execute("""UPDATE erp_document_headers SET status='sent',updated_by=%s,updated_at=now()
        WHERE id=%s AND tenant_id=%s RETURNING *""", (user["id"], row["id"], user["tenant_id"])).fetchone()
    _store_copy(db, user, updated, "issued")
    return updated


def _transition_quotation(db, user, row, data):
    """draft -> sent (prints the copy) -> accepted / expired; any -> void. Never posts. 'ready' means 'sent'."""
    target = "sent" if data.status == "ready" else data.status
    if row["status"] == target and target in {"sent", "accepted", "expired", "void"}:
        return {**_detail(db, row), "idempotent": True}
    if target not in QUOTE_TRANSITIONS.get(row["status"], set()):
        raise HTTPException(409, f"Cannot move a {row['status']} quotation to {target}.")
    if target == "sent":
        updated = _send_quotation(db, user, row)
    elif target == "void":
        updated = _void(db, user, row, data.reason)
    else:
        updated = db.execute("""UPDATE erp_document_headers SET status=%s,updated_by=%s,updated_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (target, user["id"], row["id"], user["tenant_id"])).fetchone()
    _event(db, user, row["id"], "status_changed", from_status=row["status"], to_status=target, reason=data.reason)
    return _detail(db, updated)


def _copy(db, user, source, target_type, link_type):
    payload = dict(source["payload"] or {})
    payload.pop("allocations", None)
    amount = source["total_amount"]
    vendor_id, customer_id = source["vendor_id"], source["customer_id"]
    if link_type == "transfer" and target_type in MONEY_ONLY:  # invoice -> payment: the outstanding amount, allocated to it
        amount = typed_outstanding(db, user["tenant_id"], source["id"])
        if amount <= 0:
            raise HTTPException(409, f"{source['document_number']} has nothing outstanding.")
        payload = {"lines": [], "tags": payload.get("tags", []), "fields": {},
                   "allocations": [{"targetId": str(source["id"]), "source": "typed", "amount": money(amount)}]}
    elif link_type == "transfer" and target_type in NOTE_TARGETS:
        # The note references what it corrects; a credit note is applied to it in full when issued (_check_note).
        payload["relatedDocumentId"] = str(source["id"])
        if target_type in {"debit-note", "purchase-debit-note"}:
            payload["lines"] = []  # a debit note is an extra charge: the user types it
    elif link_type == "duplicate":
        payload.pop("relatedDocumentId", None)
    payload.pop("transferFrom", None)
    payload.pop("duplicateFrom", None)
    payload[f"{link_type}From"] = str(source["id"])
    subtotal, tax, total = _totals(target_type, payload.get("lines", []), amount)
    document_date = date.today()
    due_date = source["due_date"]
    if link_type == "transfer":
        due_date = _converted_due_date(db, user["tenant_id"], source, target_type, document_date)
    row = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,status,vendor_id,customer_id,
            employee_id,contact_name,document_date,due_date,currency,reference_no,description,subtotal,tax_amount,total_amount,
            payload,bank_account_id,created_by,updated_by)
        VALUES (%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                     (user["tenant_id"], target_type, _next_number(db, user["tenant_id"], target_type), vendor_id, customer_id,
                      source["employee_id"], source["contact_name"], document_date, due_date, source["currency"],
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
        return _detail(db, _copy(db, user, source, document_type, "duplicate"))


def transfer_document(document_type: str, document_id: str, data: TransferInput, request: Request):
    _type(document_type)
    target_type = _type(data.targetType)
    user = erp_user(request, WRITE_ROLES)
    if target_type not in TRANSFERS.get(document_type, set()):
        raise HTTPException(422, f"A {LABELS[document_type].lower()} cannot be turned into a {LABELS[target_type].lower()}.")
    with connect() as db:
        source = _load(db, user, document_type, document_id, for_update=True)
        if document_type == "quotation":
            if source["status"] not in {"sent", "accepted"}:
                raise HTTPException(409, "Only a sent or accepted quotation can be converted.")
            converted = db.execute("""SELECT t.document_number FROM erp_document_links l
                JOIN erp_document_headers t ON t.id=l.target_document_id AND t.tenant_id=l.tenant_id
                WHERE l.tenant_id=%s AND l.source_document_id=%s AND l.link_type='transfer' AND t.document_type=%s
                  AND t.status<>'void' LIMIT 1""", (user["tenant_id"], source["id"], target_type)).fetchone()
            if converted:
                raise HTTPException(409, f"This quotation was already converted to {converted['document_number']}.")
            if source["status"] == "sent":
                db.execute("""UPDATE erp_document_headers SET status='accepted',updated_by=%s,updated_at=now()
                    WHERE id=%s AND tenant_id=%s""", (user["id"], source["id"], user["tenant_id"]))
                _event(db, user, source["id"], "status_changed", from_status="sent", to_status="accepted",
                       reason=f"Accepted: converted to {LABELS[target_type].lower()}")
        elif source["status"] not in {"ready", "pending_approval"}:
            raise HTTPException(409, "Only a pending or ready document can be transferred.")
        if target_type in MONEY_ONLY | set(NOTE_TARGETS) and source["status"] != "ready":
            raise HTTPException(409, "Only an issued (posted) document can be paid, credited or debited.")
        return _detail(db, _copy(db, user, source, target_type, "transfer"))


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


def _load_scanned_record(db, tenant_id, record_id, *, lock=False):
    record = db.execute("""SELECT r.*,d.kind FROM standardised_records r
        JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
        WHERE r.id=%s AND r.tenant_id=%s""" + (" FOR UPDATE OF r" if lock else ""), (record_id, tenant_id)).fetchone()
    if not record:
        raise HTTPException(404, "Record not found.")
    return record


def _require_scanned_postable(record):
    if not record["is_current"] or record["validation_status"] != "passed":
        raise HTTPException(409, "Only the current approved record can be posted.")
    if not any(record["category"] == c and record["kind"] in k for c, k in posting.SCANNED_TYPES.values()):
        raise HTTPException(422, "Only an approved invoice, bill or credit note posts from here.")


def _prepare_scanned_post(db, tenant_id, record, data):
    if str(record["currency"]).strip().upper() != "MYR":
        raise HTTPException(409, "Posting supports MYR only; foreign-currency conversion is not configured.")
    posting.refuse_if_typed_copy(db, tenant_id, record)
    on = data.journalDate or record["invoice_date"]
    if not on:
        raise HTTPException(422, "Choose a journal date; the document has no date.")
    return on, posting.plan_for_scanned(db, tenant_id, record, data.accountId)


def _scanned_plan_hash(on, lines):
    payload = [on.isoformat() if on else None,
               [(str(account), money(debit), money(credit)) for account, debit, credit in lines]]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def _post_scanned_record(db, user, record_id, data):
    tenant_id = user["tenant_id"]
    record = _load_scanned_record(db, tenant_id, record_id, lock=True)
    _require_scanned_postable(record)
    existing = posting.scanned_posted_journal(db, tenant_id, record)
    already_posted = existing and existing["status"] == "posted"
    if data.expectedPlanHash is not None or not already_posted:
        on, plan = _prepare_scanned_post(db, tenant_id, record, data)
        if data.expectedPlanHash is not None and data.expectedPlanHash != _scanned_plan_hash(
            on, ((line.account_id, line.debit, line.credit) for line in plan.lines)
        ):
            raise HTTPException(409, "The journal changed since you previewed it. Preview it again.")
    if already_posted:
        return {"id": str(existing["id"]), "status": existing["status"], "idempotent": True, "recordId": str(record_id)}
    key = posting.scanned_key(record)
    if existing:
        key += f":repost:{existing['id']}"
    result = posting.post_plan(db, user, plan, key=key,
                               source_type="adjustment" if record["kind"] == "credit_note" else "document",
                               source_id=record["document_id"], journal_date=on,
                               description=f"Scanned {record['kind'].replace('_', ' ')} {record['invoice_number'] or record['record_code']}")
    if not result["idempotent"]:
        audit(db, user, "erp.scanned_posted", str(record_id), {"journalId": result["id"], "recordCode": record["record_code"]})
    return {**result, "recordId": str(record_id)}


def post_scanned_record(record_id: str, data: ScannedPostInput, request: Request):
    """Post an approved scanned invoice, bill or credit note with no active journal."""
    user = erp_user(request, POST_ROLES)
    record_id = parse_uuid(record_id, "record id")
    with connect() as db:
        return _post_scanned_record(db, user, record_id, data)


def bulk_post_scanned_records(data: ScannedBulkPostInput, request: Request):
    user = erp_user(request, POST_ROLES)
    options = ScannedPostInput()
    results = []
    for item in data.recordIds:
        result = {"recordId": item}
        try:
            record_id = parse_uuid(item, "record id")
            with connect() as db:
                posted = _post_scanned_record(db, user, record_id, options)
            result.update(ok=True, journalId=posted["id"], idempotent=posted["idempotent"])
        except HTTPException as error:
            result.update(ok=False, reason=error.detail)
        except Exception:
            logger.exception("Bulk posting failed for record %s", item)
            result.update(ok=False, reason="The journal could not be saved. Check the record and its posting accounts.")
        results.append(result)
    return {"results": results}


def preview_scanned_post(record_id: str, request: Request):
    user = erp_user(request, READ_ROLES)
    tenant_id = user["tenant_id"]
    record_id = parse_uuid(record_id, "record id")
    problem = None
    with connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        record = _load_scanned_record(db, tenant_id, record_id)
        existing = posting.scanned_posted_journal(db, tenant_id, record)
        already_posted = bool(existing and existing["status"] == "posted")
        on = existing["journal_date"] if already_posted else record["invoice_date"]
        lines = []
        if already_posted:
            lines = db.execute("""SELECT l.account_id,a.code,a.name,l.description,l.debit,l.credit
                FROM journal_lines l JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
                WHERE l.journal_id=%s AND l.tenant_id=%s ORDER BY l.line_number""",
                (existing["id"], tenant_id)).fetchall()
        else:
            try:
                _require_scanned_postable(record)
                on, plan = _prepare_scanned_post(db, tenant_id, record, ScannedPostInput())
                ids = {parse_uuid(line.account_id, "posting account id") for line in plan.lines}
                accounts = {str(row["id"]): row for row in db.execute("""SELECT id,code,name FROM ledger_accounts
                    WHERE tenant_id=%s AND id=ANY(%s)""", (tenant_id, list(ids))).fetchall()}
                lines = [{**accounts[line.account_id], "account_id": line.account_id,
                          "description": line.description[:500], "debit": line.debit, "credit": line.credit}
                         for line in plan.lines]
                posting.open_period(db, tenant_id, on)
            except HTTPException as error:
                problem = error.detail

    debit = sum((line["debit"] for line in lines), Decimal(0))
    credit = sum((line["credit"] for line in lines), Decimal(0))
    response = {"recordId": str(record_id), "journalDate": on.isoformat() if on else None,
                "lines": [{"accountId": str(line["account_id"]), "code": line["code"], "name": line["name"],
                           "description": line["description"], "debit": money(line["debit"]), "credit": money(line["credit"])}
                          for line in lines],
                "totalDebit": money(debit), "totalCredit": money(credit), "balanced": debit == credit and debit > 0,
                "alreadyPosted": already_posted, "previouslyReversed": bool(existing and existing["previously_reversed"]),
                "planHash": _scanned_plan_hash(on, ((line["account_id"], line["debit"], line["credit"]) for line in lines))}
    if problem:
        response["problem"] = problem
    return response


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


def _pdf_response(content: bytes, file_name: str, *, inline: bool) -> Response:
    safe = file_name.replace('"', "")
    return Response(content=content, media_type="application/pdf", headers={
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{safe}"',
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


def preview_document_pdf(document_type: str, document_id: str, request: Request):
    """Draft: rendered now, marked DRAFT (nothing stored). Issued: the latest stored copy, unchanged."""
    _type(document_type)
    user = erp_user(request, READ_ROLES)
    if document_type not in PRINTED_TYPES:
        raise HTTPException(422, "This document type is not printed.")
    with connect() as db:
        row = _load(db, user, document_type, document_id)
        stored = db.execute("""SELECT file_name,content FROM erp_document_files WHERE tenant_id=%s AND document_id=%s
            ORDER BY revision DESC LIMIT 1""", (user["tenant_id"], row["id"])).fetchone()
        if stored:
            return _pdf_response(bytes(stored["content"]), stored["file_name"], inline=True)
        # Issued before printed copies were kept: say so instead of calling it a draft (nothing is stored).
        mark = "DRAFT - NOT ISSUED" if row["status"] in {"draft", "pending_approval"} else             ("VOID" if row["status"] == "void" else "RECONSTRUCTED COPY")
        content = _render(db, row, watermark=mark)
        return _pdf_response(content, _file_name(db, row), inline=True)


def list_document_files(document_type: str, document_id: str, request: Request):
    _type(document_type)
    user = erp_user(request, READ_ROLES)
    with connect() as db:
        row = _load(db, user, document_type, document_id)
        return {"items": _file_list(db, user["tenant_id"], row["id"])}


def download_document_file(document_type: str, document_id: str, revision: int, request: Request, inline: bool = False):
    _type(document_type)
    user = erp_user(request, READ_ROLES)
    with connect() as db:
        row = _load(db, user, document_type, document_id)
        stored = db.execute("""SELECT file_name,content,sha256 FROM erp_document_files WHERE tenant_id=%s AND document_id=%s
            AND revision=%s""", (user["tenant_id"], row["id"], revision)).fetchone()
        if not stored:
            raise HTTPException(404, "No printed copy with this revision.")
        content = bytes(stored["content"])
        if hashlib.sha256(content).hexdigest() != stored["sha256"]:
            raise HTTPException(500, "The stored copy does not match its checksum.")
        audit(db, user, "erp.document_file_downloaded", str(row["id"]), {"revision": revision, "file": stored["file_name"]})
        return _pdf_response(content, stored["file_name"], inline=inline)


def register(router):
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/preview.pdf', preview_document_pdf, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/files', list_document_files, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/files/{revision}', download_document_file, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/posting-accounts', get_posting_accounts, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/posting-accounts', set_posting_accounts, methods=['PUT'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/scanned/post-bulk', bulk_post_scanned_records, methods=['POST'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/scanned/{record_id}/post-preview', preview_scanned_post, methods=['GET'], tags=['ERP Documents'])
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
