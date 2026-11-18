from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import secrets
from typing import Any
from uuid import UUID

from main import *
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from services.notifications.mailer import send_transaction_email


CONTACT_KINDS = {"customer", "supplier", "employee"}
CATALOG_KINDS = {
    "product-group", "category", "coupon", "content-page", "price-level", "payment-term",
    "payment-method", "currency", "tag", "asset-type", "location", "custom-field",
    "number-format", "form-design", "bank-account", "fixed-asset", "store-setting",
}
DOCUMENT_TYPES = {
    "quotation", "sales-order", "delivery-order", "invoice", "credit-note", "payment", "refund",
    "purchase-order", "goods-received", "bill", "purchase-credit-note", "purchase-payment",
    "purchase-refund", "money-in", "money-out", "bank-transfer", "stock-adjustment", "stock-transfer",
    "journal-entry", "contra", "deemed-payment", "store-order", "minipos-order",
}
DOCUMENT_STATUSES = {"draft", "pending_approval", "ready", "void"}
STATUS_TRANSITIONS = {
    "draft": {"draft", "pending_approval", "ready", "void"},
    "pending_approval": {"draft", "ready", "void"},
    "ready": {"void"},
    "void": set(),
}
PREFIXES = {
    "quotation": "QT", "sales-order": "SO", "delivery-order": "DO", "invoice": "IV",
    "credit-note": "CN", "payment": "OR", "refund": "PV", "purchase-order": "PO",
    "goods-received": "GR", "bill": "BL", "purchase-credit-note": "SC", "purchase-payment": "PV",
    "purchase-refund": "OR", "money-in": "OR", "money-out": "PV", "bank-transfer": "FT",
    "stock-adjustment": "SA", "stock-transfer": "ST", "journal-entry": "JE", "contra": "CT",
    "deemed-payment": "DP", "store-order": "BS", "minipos-order": "POS",
}


class ERPContactInput(BaseModel):
    kind: str = Field(default="customer", min_length=1, max_length=30)
    legalName: str = Field(min_length=1, max_length=300)
    code: str | None = Field(default=None, max_length=80)
    registrationNumber: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=80)
    address: str | None = Field(default=None, max_length=4000)
    city: str | None = Field(default=None, max_length=160)
    state: str | None = Field(default=None, max_length=160)
    postcode: str | None = Field(default=None, max_length=30)
    fields: dict[str, Any] = Field(default_factory=dict)


class ERPProductInput(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    sku: str | None = Field(default=None, max_length=120)
    kind: str = Field(default="inventory", pattern=r"^(inventory|non-inventory|service|bundle)$")
    unit: str = Field(default="unit", min_length=1, max_length=40)
    salePrice: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=4)
    purchasePrice: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=4)
    quantity: Decimal = Field(default=Decimal("0"), decimal_places=3)
    groupName: str | None = Field(default=None, max_length=200)
    fields: dict[str, Any] = Field(default_factory=dict)


class ERPCatalogInput(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    code: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=4000)
    active: bool = True
    data: dict[str, Any] = Field(default_factory=dict)


class ERPLineInput(BaseModel):
    itemId: str | None = None
    description: str = Field(min_length=1, max_length=500)
    quantity: Decimal = Field(default=Decimal("1"), gt=0, decimal_places=3)
    unitPrice: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=4)
    discount: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    tax: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    accountId: str | None = None


class ERPDocumentInput(BaseModel):
    number: str | None = Field(default=None, max_length=120)
    contactId: str | None = None
    contactName: str | None = Field(default=None, max_length=250)
    documentDate: date | None = None
    dueDate: date | None = None
    currency: str = Field(default="MYR", pattern=r"^[A-Za-z]{3}$")
    referenceNo: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    status: str = Field(default="draft", pattern=r"^(draft|pending_approval|ready)$")
    amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    lines: list[ERPLineInput] = Field(default_factory=list, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=100)
    fields: dict[str, Any] = Field(default_factory=dict)


class ERPDocumentPatch(BaseModel):
    contactId: str | None = None
    contactName: str | None = Field(default=None, max_length=250)
    documentDate: date | None = None
    dueDate: date | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    referenceNo: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    lines: list[ERPLineInput] | None = Field(default=None, max_length=500)
    tags: list[str] | None = Field(default=None, max_length=100)
    fields: dict[str, Any] | None = None


class ERPTransitionInput(BaseModel):
    status: str = Field(pattern=r"^(draft|pending_approval|ready|void)$")
    reason: str | None = Field(default=None, max_length=1000)


class ERPTransferInput(BaseModel):
    targetType: str = Field(min_length=1, max_length=50)


class ERPShareInput(BaseModel):
    channel: str = Field(pattern=r"^(link|email|whatsapp|sms)$")
    recipient: str | None = Field(default=None, max_length=254)
    formDesign: str | None = Field(default=None, max_length=100)


class ConsolidatedSubmissionInput(BaseModel):
    documentType: str = Field(min_length=1, max_length=50)
    dateFrom: date
    dateTo: date
    documentIds: list[str] = Field(min_length=1, max_length=500)
    idempotencyKey: str = Field(min_length=1, max_length=300)


def _erp_user(request: Request, *, write: bool = False):
    roles = {"local_admin", "accountant"} if write else {"super_admin", "local_admin", "accountant"}
    user = require_workspace_user(request, roles)
    if not user.get("tenant_id"):
        raise HTTPException(403, "This operation requires a company workspace.")
    return user


def _erp_uuid(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(422, f"Invalid {label}.")


def _money(value: Decimal | int | float | str | None) -> str:
    if value is None:
        return "0.00"
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _ensure_erp_schema(db):
    db.execute("SELECT pg_advisory_xact_lock(hashtext('accordai.erp.schema.v1'))")
    db.execute("""CREATE TABLE IF NOT EXISTS erp_contacts (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('customer','supplier','employee')), legal_name TEXT NOT NULL,
        code TEXT, registration_number TEXT, email TEXT, phone TEXT, address TEXT, city TEXT, state TEXT, postcode TEXT,
        fields JSONB NOT NULL DEFAULT '{}'::jsonb, is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_by UUID REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, kind, legal_name), UNIQUE (id, tenant_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS erp_products (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        name TEXT NOT NULL, sku TEXT, kind TEXT NOT NULL CHECK (kind IN ('inventory','non-inventory','service','bundle')),
        unit TEXT NOT NULL DEFAULT 'unit', sale_price NUMERIC(18,4) NOT NULL DEFAULT 0, purchase_price NUMERIC(18,4) NOT NULL DEFAULT 0,
        quantity NUMERIC(18,3) NOT NULL DEFAULT 0, group_name TEXT, fields JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (id, tenant_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS erp_catalog_entries (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, code TEXT, name TEXT NOT NULL, description TEXT, data JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (id, tenant_id),
        UNIQUE (tenant_id, kind, name)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS erp_document_headers (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_type TEXT NOT NULL, document_number TEXT NOT NULL, contact_id UUID, contact_name TEXT,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','pending_approval','ready','void')),
        document_date DATE, due_date DATE, currency CHAR(3) NOT NULL DEFAULT 'MYR', reference_no TEXT, description TEXT,
        total_amount NUMERIC(18,2) NOT NULL DEFAULT 0, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by UUID REFERENCES users(id) ON DELETE SET NULL, updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), voided_at TIMESTAMPTZ,
        UNIQUE (tenant_id, document_type, document_number), UNIQUE (id, tenant_id)
    )""")
    db.execute("ALTER TABLE erp_document_headers ADD COLUMN IF NOT EXISTS contact_name TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS erp_document_links (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        source_document_id UUID NOT NULL, target_document_id UUID NOT NULL, link_type TEXT NOT NULL,
        created_by UUID REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, source_document_id, target_document_id, link_type)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS erp_document_events (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_id UUID NOT NULL, event_type TEXT NOT NULL, from_status TEXT, to_status TEXT, reason TEXT,
        actor_id UUID REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS erp_document_shares (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_id UUID NOT NULL, document_type TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
        channel TEXT NOT NULL CHECK (channel IN ('link','email','whatsapp','sms')),
        recipient TEXT, form_design TEXT, subject TEXT, message TEXT,
        status TEXT NOT NULL DEFAULT 'ready', created_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_opened_at TIMESTAMPTZ
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS myinvois_consolidated_submissions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_type TEXT NOT NULL, date_from DATE NOT NULL, date_to DATE NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','submitted','failed')),
        idempotency_key TEXT NOT NULL, total_amount NUMERIC(18,2) NOT NULL DEFAULT 0,
        created_by UUID REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, idempotency_key)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS myinvois_consolidated_documents (
        submission_id UUID NOT NULL REFERENCES myinvois_consolidated_submissions(id) ON DELETE CASCADE,
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_id UUID NOT NULL, document_type TEXT NOT NULL,
        PRIMARY KEY (submission_id, document_id), UNIQUE (tenant_id, document_id, document_type)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS erp_number_counters (
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, document_type TEXT NOT NULL, next_value INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (tenant_id, document_type)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_erp_documents_list ON erp_document_headers(tenant_id, document_type, document_date DESC, created_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_erp_contacts_list ON erp_contacts(tenant_id, kind, legal_name)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_erp_products_list ON erp_products(tenant_id, name)")


def _next_number(db, tenant_id: str, document_type: str) -> str:
    db.execute("INSERT INTO erp_number_counters(tenant_id,document_type,next_value) VALUES (%s,%s,2) ON CONFLICT (tenant_id,document_type) DO NOTHING", (tenant_id, document_type))
    row = db.execute("UPDATE erp_number_counters SET next_value=next_value+1 WHERE tenant_id=%s AND document_type=%s RETURNING next_value-1 AS value", (tenant_id, document_type)).fetchone()
    return f"{PREFIXES.get(document_type, 'TX')}-{int(row['value']):05d}"


def _contact_dto(row):
    return {"id": str(row["id"]), "kind": row["kind"], "legalName": row["legal_name"], "code": row["code"],
            "registrationNumber": row["registration_number"], "email": row["email"], "phone": row["phone"],
            "address": row["address"], "city": row["city"], "state": row["state"], "postcode": row["postcode"],
            "fields": row["fields"] or {}, "isActive": row["is_active"], "createdAt": str(row["created_at"]), "updatedAt": str(row["updated_at"])}


def _product_dto(row):
    return {"id": str(row["id"]), "name": row["name"], "sku": row["sku"], "kind": row["kind"], "unit": row["unit"],
            "salePrice": _money(row["sale_price"]), "purchasePrice": _money(row["purchase_price"]), "quantity": str(row["quantity"]),
            "groupName": row["group_name"], "fields": row["fields"] or {}, "isActive": row["is_active"], "createdAt": str(row["created_at"]), "updatedAt": str(row["updated_at"])}


def _catalog_dto(row):
    return {"id": str(row["id"]), "kind": row["kind"], "code": row["code"], "name": row["name"], "description": row["description"],
            "data": row["data"] or {}, "isActive": row["is_active"], "createdAt": str(row["created_at"]), "updatedAt": str(row["updated_at"])}


def _document_dto(row):
    payload = row["payload"] or {}
    return {"id": str(row["id"]), "type": row["document_type"], "number": row["document_number"], "contactId": str(row["contact_id"]) if row["contact_id"] else None,
            "contactName": row["contact_name"] if "contact_name" in row else None,
            "status": row["status"], "documentDate": str(row["document_date"]) if row["document_date"] else None,
            "dueDate": str(row["due_date"]) if row["due_date"] else None, "currency": row["currency"], "referenceNo": row["reference_no"],
            "description": row["description"], "amount": _money(row["total_amount"]), "lines": payload.get("lines", []), "tags": payload.get("tags", []),
            "fields": payload.get("fields", {}), "createdAt": str(row["created_at"]), "updatedAt": str(row["updated_at"]), "voidedAt": str(row["voided_at"]) if row["voided_at"] else None}


def _audit(db, user, document_id: str, event_type: str, *, from_status: str | None = None, to_status: str | None = None, reason: str | None = None):
    db.execute("INSERT INTO erp_document_events(tenant_id,document_id,event_type,from_status,to_status,reason,actor_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (user["tenant_id"], document_id, event_type, from_status, to_status, reason, user["id"]))


def list_contacts(request: Request, kind: str | None = None, search: str | None = None, includeInactive: bool = False):
    user = _erp_user(request)
    if kind and kind not in CONTACT_KINDS: raise HTTPException(422, "Invalid contact kind.")
    with connect() as db:
        _ensure_erp_schema(db)
        rows = db.execute("""SELECT * FROM erp_contacts WHERE tenant_id=%s AND (%s::text IS NULL OR kind=%s::text)
            AND (%s::boolean OR is_active) AND (%s::text IS NULL OR legal_name ILIKE %s OR code ILIKE %s)
            ORDER BY legal_name""", (user["tenant_id"], kind, kind, includeInactive, search, f"%{search}%" if search else None, f"%{search}%" if search else None)).fetchall()
    return [_contact_dto(row) for row in rows]


def create_contact(data: ERPContactInput, request: Request):
    user = _erp_user(request, write=True)
    if data.kind not in CONTACT_KINDS: raise HTTPException(422, "Invalid contact kind.")
    with connect() as db:
        _ensure_erp_schema(db)
        try:
            row = db.execute("""INSERT INTO erp_contacts(tenant_id,kind,legal_name,code,registration_number,email,phone,address,city,state,postcode,fields,created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], data.kind, data.legalName.strip(), data.code, data.registrationNumber, data.email, data.phone, data.address, data.city, data.state, data.postcode, Jsonb(data.fields), user["id"])).fetchone()
        except UniqueViolation:
            raise HTTPException(409, "A contact with this name already exists.")
    return _contact_dto(row)


def get_contact(contact_id: str, request: Request):
    user = _erp_user(request)
    contact_id = _erp_uuid(contact_id, "contact id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("SELECT * FROM erp_contacts WHERE id=%s AND tenant_id=%s", (contact_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Contact not found.")
    return _contact_dto(row)


def update_contact(contact_id: str, data: ERPContactInput, request: Request):
    user = _erp_user(request, write=True)
    contact_id = _erp_uuid(contact_id, "contact id")
    if data.kind not in CONTACT_KINDS: raise HTTPException(422, "Invalid contact kind.")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("""UPDATE erp_contacts SET kind=%s,legal_name=%s,code=%s,registration_number=%s,email=%s,phone=%s,address=%s,city=%s,state=%s,postcode=%s,fields=%s,updated_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (data.kind, data.legalName.strip(), data.code, data.registrationNumber, data.email, data.phone, data.address, data.city, data.state, data.postcode, Jsonb(data.fields), contact_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Contact not found.")
    return _contact_dto(row)


def delete_contact(contact_id: str, request: Request):
    user = _erp_user(request, write=True)
    contact_id = _erp_uuid(contact_id, "contact id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("UPDATE erp_contacts SET is_active=FALSE,updated_at=now() WHERE id=%s AND tenant_id=%s RETURNING id", (contact_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Contact not found.")
    return {"id": str(row["id"]), "isActive": False}


def list_products(request: Request, search: str | None = None, kind: str | None = None, includeInactive: bool = False):
    user = _erp_user(request)
    with connect() as db:
        _ensure_erp_schema(db)
        rows = db.execute("""SELECT * FROM erp_products WHERE tenant_id=%s AND (%s::text IS NULL OR kind=%s::text)
            AND (%s::boolean OR is_active) AND (%s::text IS NULL OR name ILIKE %s OR COALESCE(sku,'') ILIKE %s) ORDER BY name""",
            (user["tenant_id"], kind, kind, includeInactive, search, f"%{search}%" if search else None, f"%{search}%" if search else None)).fetchall()
    return [_product_dto(row) for row in rows]


def create_product(data: ERPProductInput, request: Request):
    user = _erp_user(request, write=True)
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("""INSERT INTO erp_products(tenant_id,name,sku,kind,unit,sale_price,purchase_price,quantity,group_name,fields,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], data.name.strip(), data.sku, data.kind, data.unit, data.salePrice, data.purchasePrice, data.quantity, data.groupName, Jsonb(data.fields), user["id"])).fetchone()
    return _product_dto(row)


def get_product(product_id: str, request: Request):
    user = _erp_user(request)
    product_id = _erp_uuid(product_id, "product id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("SELECT * FROM erp_products WHERE id=%s AND tenant_id=%s", (product_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Product not found.")
    return _product_dto(row)


def update_product(product_id: str, data: ERPProductInput, request: Request):
    user = _erp_user(request, write=True)
    product_id = _erp_uuid(product_id, "product id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("""UPDATE erp_products SET name=%s,sku=%s,kind=%s,unit=%s,sale_price=%s,purchase_price=%s,quantity=%s,group_name=%s,fields=%s,updated_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (data.name.strip(), data.sku, data.kind, data.unit, data.salePrice, data.purchasePrice, data.quantity, data.groupName, Jsonb(data.fields), product_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Product not found.")
    return _product_dto(row)


def delete_product(product_id: str, request: Request):
    user = _erp_user(request, write=True)
    product_id = _erp_uuid(product_id, "product id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("UPDATE erp_products SET is_active=FALSE,updated_at=now() WHERE id=%s AND tenant_id=%s RETURNING id", (product_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Product not found.")
    return {"id": str(row["id"]), "isActive": False}


def list_catalog(kind: str, request: Request, includeInactive: bool = False):
    user = _erp_user(request)
    if kind not in CATALOG_KINDS: raise HTTPException(422, "Invalid catalog kind.")
    with connect() as db:
        _ensure_erp_schema(db)
        rows = db.execute("SELECT * FROM erp_catalog_entries WHERE tenant_id=%s AND kind=%s AND (%s OR is_active) ORDER BY name", (user["tenant_id"], kind, includeInactive)).fetchall()
    return [_catalog_dto(row) for row in rows]


def create_catalog(kind: str, data: ERPCatalogInput, request: Request):
    user = _erp_user(request, write=True)
    if kind not in CATALOG_KINDS: raise HTTPException(422, "Invalid catalog kind.")
    with connect() as db:
        _ensure_erp_schema(db)
        try:
            row = db.execute("INSERT INTO erp_catalog_entries(tenant_id,kind,code,name,description,data,is_active,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *", (user["tenant_id"], kind, data.code, data.name.strip(), data.description, Jsonb(data.data), data.active, user["id"])).fetchone()
        except UniqueViolation:
            raise HTTPException(409, "This catalog entry already exists.")
    return _catalog_dto(row)


def get_catalog(kind: str, entry_id: str, request: Request):
    user = _erp_user(request)
    if kind not in CATALOG_KINDS: raise HTTPException(422, "Invalid catalog kind.")
    entry_id = _erp_uuid(entry_id, "catalog entry id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("SELECT * FROM erp_catalog_entries WHERE id=%s AND tenant_id=%s AND kind=%s", (entry_id, user["tenant_id"], kind)).fetchone()
    if not row: raise HTTPException(404, "Catalog entry not found.")
    return _catalog_dto(row)


def update_catalog(kind: str, entry_id: str, data: ERPCatalogInput, request: Request):
    user = _erp_user(request, write=True)
    if kind not in CATALOG_KINDS: raise HTTPException(422, "Invalid catalog kind.")
    entry_id = _erp_uuid(entry_id, "catalog entry id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("UPDATE erp_catalog_entries SET code=%s,name=%s,description=%s,data=%s,is_active=%s,updated_at=now() WHERE id=%s AND tenant_id=%s AND kind=%s RETURNING *", (data.code, data.name.strip(), data.description, Jsonb(data.data), data.active, entry_id, user["tenant_id"], kind)).fetchone()
    if not row: raise HTTPException(404, "Catalog entry not found.")
    return _catalog_dto(row)


def delete_catalog(kind: str, entry_id: str, request: Request):
    user = _erp_user(request, write=True)
    if kind not in CATALOG_KINDS: raise HTTPException(422, "Invalid catalog kind.")
    entry_id = _erp_uuid(entry_id, "catalog entry id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("UPDATE erp_catalog_entries SET is_active=FALSE,updated_at=now() WHERE id=%s AND tenant_id=%s AND kind=%s RETURNING id", (entry_id, user["tenant_id"], kind)).fetchone()
    if not row: raise HTTPException(404, "Catalog entry not found.")
    return {"id": str(row["id"]), "isActive": False}


def _validate_document_type(document_type: str):
    if document_type not in DOCUMENT_TYPES: raise HTTPException(422, "Invalid document type.")


def _calculate_total(lines: list[dict[str, Any]], amount: Decimal | None) -> Decimal:
    if lines:
        total = Decimal("0")
        for line in lines:
            gross = Decimal(str(line.get("quantity", 0))) * Decimal(str(line.get("unitPrice", 0)))
            discount = Decimal(str(line.get("discount", 0)))
            tax = Decimal(str(line.get("tax", 0)))
            total += gross - discount + tax
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (amount or Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _check_contact(db, tenant_id: str, contact_id: str | None):
    if not contact_id: return None
    contact_id = _erp_uuid(contact_id, "contact id")
    if not db.execute("SELECT id FROM erp_contacts WHERE id=%s AND tenant_id=%s AND is_active", (contact_id, tenant_id)).fetchone():
        raise HTTPException(422, "The selected contact is not available.")
    return contact_id


def list_documents(document_type: str, request: Request, page: int = 1, pageSize: int = 30, search: str | None = None, status: str | None = None, contactId: str | None = None, dateFrom: date | None = None, dateTo: date | None = None):
    _validate_document_type(document_type)
    user = _erp_user(request)
    if page < 1 or pageSize < 1 or pageSize > 100: raise HTTPException(422, "Invalid pagination.")
    if status and status not in DOCUMENT_STATUSES: raise HTTPException(422, "Invalid document status.")
    contact_id = _erp_uuid(contactId, "contact id") if contactId else None
    where = ["tenant_id=%s", "document_type=%s"]
    params: list[Any] = [user["tenant_id"], document_type]
    if status: where.append("status=%s"); params.append(status)
    if contact_id: where.append("contact_id=%s"); params.append(contact_id)
    if dateFrom: where.append("document_date >= %s"); params.append(dateFrom)
    if dateTo: where.append("document_date <= %s"); params.append(dateTo)
    if search: where.append("(document_number ILIKE %s OR COALESCE(reference_no,'') ILIKE %s OR COALESCE(description,'') ILIKE %s)"); params.extend([f"%{search}%"] * 3)
    clause = " AND ".join(where)
    with connect() as db:
        _ensure_erp_schema(db)
        total = db.execute(f"SELECT count(*) AS count FROM erp_document_headers WHERE {clause}", params).fetchone()["count"]
        rows = db.execute(f"SELECT * FROM erp_document_headers WHERE {clause} ORDER BY document_date DESC NULLS LAST, created_at DESC LIMIT %s OFFSET %s", params + [pageSize, (page - 1) * pageSize]).fetchall()
    return {"items": [_document_dto(row) for row in rows], "total": total, "page": page, "pageSize": pageSize, "nextPage": page + 1 if page * pageSize < total else None}


def create_document(document_type: str, data: ERPDocumentInput, request: Request):
    _validate_document_type(document_type)
    user = _erp_user(request, write=True)
    if data.status not in DOCUMENT_STATUSES - {"void"}: raise HTTPException(422, "Invalid initial document status.")
    lines = [line.model_dump(mode="json") for line in data.lines]
    with connect() as db:
        _ensure_erp_schema(db)
        contact_id = _check_contact(db, user["tenant_id"], data.contactId)
        number = data.number.strip() if data.number else _next_number(db, user["tenant_id"], document_type)
        total = _calculate_total(lines, data.amount)
        try:
            row = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,contact_id,contact_name,status,document_date,due_date,currency,reference_no,description,total_amount,payload,created_by,updated_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], document_type, number, contact_id, data.contactName, data.status, data.documentDate or date.today(), data.dueDate, data.currency.upper(), data.referenceNo, data.description, total, Jsonb({"lines": lines, "tags": data.tags, "fields": data.fields}), user["id"], user["id"])).fetchone()
        except UniqueViolation:
            raise HTTPException(409, "A document with this number already exists.")
        _audit(db, user, str(row["id"]), "created", to_status=data.status, reason="Created from local ERP workflow")
    return _document_dto(row)


def get_document(document_type: str, document_id: str, request: Request):
    _validate_document_type(document_type)
    user = _erp_user(request)
    document_id = _erp_uuid(document_id, "document id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s", (document_id, user["tenant_id"], document_type)).fetchone()
    if not row: raise HTTPException(404, "Document not found.")
    return _document_dto(row)


def update_document(document_type: str, document_id: str, data: ERPDocumentPatch, request: Request):
    _validate_document_type(document_type)
    user = _erp_user(request, write=True)
    document_id = _erp_uuid(document_id, "document id")
    changes = data.model_dump(exclude_unset=True, mode="json")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s FOR UPDATE", (document_id, user["tenant_id"], document_type)).fetchone()
        if not row: raise HTTPException(404, "Document not found.")
        if row["status"] == "void": raise HTTPException(409, "A void document cannot be edited.")
        contact_id = _check_contact(db, user["tenant_id"], changes.get("contactId", str(row["contact_id"]) if row["contact_id"] else None))
        payload = row["payload"] or {}
        lines = changes.get("lines", payload.get("lines", []))
        tags = changes.get("tags", payload.get("tags", []))
        fields = changes.get("fields", payload.get("fields", {}))
        total = _calculate_total(lines, Decimal(str(changes.get("amount", row["total_amount"]))))
        updated = db.execute("""UPDATE erp_document_headers SET contact_id=%s,contact_name=%s,document_date=%s,due_date=%s,currency=%s,reference_no=%s,description=%s,total_amount=%s,payload=%s,updated_by=%s,updated_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (contact_id, changes.get("contactName", row["contact_name"]), changes.get("documentDate", row["document_date"]), changes.get("dueDate", row["due_date"]), (changes.get("currency") or row["currency"]).upper(), changes.get("referenceNo", row["reference_no"]), changes.get("description", row["description"]), total, Jsonb({"lines": lines, "tags": tags, "fields": fields}), user["id"], document_id, user["tenant_id"])).fetchone()
        _audit(db, user, document_id, "updated", from_status=row["status"], to_status=row["status"], reason="Document fields updated")
    return _document_dto(updated)


def delete_document(document_type: str, document_id: str, request: Request):
    _validate_document_type(document_type)
    user = _erp_user(request, write=True)
    document_id = _erp_uuid(document_id, "document id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("SELECT status FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s FOR UPDATE", (document_id, user["tenant_id"], document_type)).fetchone()
        if not row: raise HTTPException(404, "Document not found.")
        if row["status"] != "draft": raise HTTPException(409, "Only draft documents can be deleted. Void a posted document instead.")
        db.execute("DELETE FROM erp_document_headers WHERE id=%s AND tenant_id=%s", (document_id, user["tenant_id"]))
        _audit(db, user, document_id, "deleted", from_status="draft", reason="Draft deleted")
    return {"id": document_id, "deleted": True}


def transition_document(document_type: str, document_id: str, data: ERPTransitionInput, request: Request):
    _validate_document_type(document_type)
    user = _erp_user(request, write=True)
    document_id = _erp_uuid(document_id, "document id")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s FOR UPDATE", (document_id, user["tenant_id"], document_type)).fetchone()
        if not row: raise HTTPException(404, "Document not found.")
        if data.status not in STATUS_TRANSITIONS[row["status"]]: raise HTTPException(409, f"Cannot move a {row['status']} document to {data.status}.")
        updated = db.execute("UPDATE erp_document_headers SET status=%s,voided_at=%s,updated_by=%s,updated_at=now() WHERE id=%s AND tenant_id=%s RETURNING *", (data.status, datetime.now(timezone.utc) if data.status == "void" else None, user["id"], document_id, user["tenant_id"])).fetchone()
        _audit(db, user, document_id, "status_changed", from_status=row["status"], to_status=data.status, reason=data.reason)
    return _document_dto(updated)


def duplicate_document(document_type: str, document_id: str, request: Request):
    _validate_document_type(document_type)
    user = _erp_user(request, write=True)
    document_id = _erp_uuid(document_id, "document id")
    with connect() as db:
        _ensure_erp_schema(db)
        source = db.execute("SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s", (document_id, user["tenant_id"], document_type)).fetchone()
        if not source: raise HTTPException(404, "Document not found.")
        number = _next_number(db, user["tenant_id"], document_type)
        payload = dict(source["payload"] or {})
        payload["duplicatedFrom"] = str(source["id"])
        row = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,contact_id,contact_name,status,document_date,due_date,currency,reference_no,description,total_amount,payload,created_by,updated_by)
            VALUES (%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], document_type, number, source["contact_id"], source["contact_name"], date.today(), source["due_date"], source["currency"], source["reference_no"], source["description"], source["total_amount"], Jsonb(payload), user["id"], user["id"])).fetchone()
        db.execute("INSERT INTO erp_document_links(tenant_id,source_document_id,target_document_id,link_type,created_by) VALUES (%s,%s,%s,'duplicate',%s)", (user["tenant_id"], document_id, row["id"], user["id"]))
        _audit(db, user, str(row["id"]), "duplicated", to_status="draft", reason=f"Copied from {source['document_number']}")
    return _document_dto(row)


def transfer_document(document_type: str, document_id: str, data: ERPTransferInput, request: Request):
    _validate_document_type(document_type)
    _validate_document_type(data.targetType)
    user = _erp_user(request, write=True)
    document_id = _erp_uuid(document_id, "document id")
    with connect() as db:
        _ensure_erp_schema(db)
        source = db.execute("SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s", (document_id, user["tenant_id"], document_type)).fetchone()
        if not source: raise HTTPException(404, "Document not found.")
        if source["status"] not in {"ready", "pending_approval"}: raise HTTPException(409, "Only an active document can be transferred.")
        number = _next_number(db, user["tenant_id"], data.targetType)
        payload = dict(source["payload"] or {})
        payload["transferredFrom"] = str(source["id"])
        row = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,contact_id,contact_name,status,document_date,due_date,currency,reference_no,description,total_amount,payload,created_by,updated_by)
            VALUES (%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], data.targetType, number, source["contact_id"], source["contact_name"], date.today(), source["due_date"], source["currency"], source["reference_no"], source["description"], source["total_amount"], Jsonb(payload), user["id"], user["id"])).fetchone()
        db.execute("INSERT INTO erp_document_links(tenant_id,source_document_id,target_document_id,link_type,created_by) VALUES (%s,%s,%s,'transfer',%s)", (user["tenant_id"], document_id, row["id"], user["id"]))
        _audit(db, user, str(row["id"]), "transferred", to_status="draft", reason=f"Transferred from {source['document_number']}")
    return _document_dto(row)


def document_history(document_type: str, document_id: str, request: Request):
    _validate_document_type(document_type)
    user = _erp_user(request)
    document_id = _erp_uuid(document_id, "document id")
    with connect() as db:
        _ensure_erp_schema(db)
        if not db.execute("SELECT id FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s", (document_id, user["tenant_id"], document_type)).fetchone(): raise HTTPException(404, "Document not found.")
        rows = db.execute("SELECT * FROM erp_document_events WHERE document_id=%s AND tenant_id=%s ORDER BY created_at", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(row["id"]), "eventType": row["event_type"], "fromStatus": row["from_status"], "toStatus": row["to_status"], "reason": row["reason"], "createdAt": str(row["created_at"]), "actorId": str(row["actor_id"]) if row["actor_id"] else None} for row in rows]


def share_document(document_type: str, document_id: str, data: ERPShareInput, request: Request):
    _validate_document_type(document_type)
    user = _erp_user(request)
    document_id = _erp_uuid(document_id, "document id")
    if data.channel == "email" and not data.recipient:
        raise HTTPException(422, "An email recipient is required.")
    public_base = (os.getenv("APP_PUBLIC_URL") or os.getenv("FRONTEND_URL") or "http://127.0.0.1:3000").rstrip("/")
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("SELECT * FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s", (document_id, user["tenant_id"], document_type)).fetchone()
        if not row: raise HTTPException(404, "Document not found.")
        token = secrets.token_urlsafe(32)
        share_status = "ready"
        share_row = db.execute("""INSERT INTO erp_document_shares(tenant_id,document_id,document_type,token,channel,recipient,form_design,status,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", (user["tenant_id"], document_id, document_type, token, data.channel, data.recipient, data.formDesign or "Default", share_status, user["id"])).fetchone()
        _audit(db, user, document_id, "shared", reason=f"{data.channel}:{data.recipient or ''}")
    share_url = f"{public_base}/share/{token}"
    if data.channel == "email":
        subject = f"{row['document_number']} from Accounting Intelligence"
        text = f"You have received {document_type.replace('-', ' ')} {row['document_number']}.\n\nOpen the secure document: {share_url}"
        html = f"<p>You have received <strong>{document_type.replace('-', ' ')}</strong> <strong>{row['document_number']}</strong>.</p><p><a href=\"{share_url}\">Open the secure document</a></p>"
        try:
            send_transaction_email(recipient=data.recipient, subject=subject, text=text, html=html)
        except Exception as exc:
            with connect() as db:
                db.execute("UPDATE erp_document_shares SET status='failed' WHERE id=%s AND tenant_id=%s", (share_row["id"], user["tenant_id"]))
            raise HTTPException(503, f"The document share was created, but the email could not be sent: {exc}") from exc
        with connect() as db:
            db.execute("UPDATE erp_document_shares SET status='sent' WHERE id=%s AND tenant_id=%s", (share_row["id"], user["tenant_id"]))
        share_status = "sent"
    return {"documentId": document_id, "documentNumber": row["document_number"], "channel": data.channel, "recipient": data.recipient, "formDesign": data.formDesign or "Default", "status": share_status, "url": share_url}


def get_public_share(token: str):
    with connect() as db:
        _ensure_erp_schema(db)
        row = db.execute("""SELECT s.id AS share_id, s.tenant_id AS share_tenant_id, s.document_id AS share_document_id,
            s.document_type AS share_document_type, s.token, s.channel, s.recipient, s.form_design, s.status AS share_status,
            s.created_at AS share_created_at, s.last_opened_at, d.* FROM erp_document_shares s
            JOIN erp_document_headers d ON d.id=s.document_id AND d.tenant_id=s.tenant_id AND d.document_type=s.document_type
            WHERE s.token=%s""", (token,)).fetchone()
        if not row:
            raise HTTPException(404, "Share link not found or expired.")
        db.execute("UPDATE erp_document_shares SET last_opened_at=now() WHERE id=%s", (row["share_id"],))
    return {"share": {"documentType": row["share_document_type"], "documentNumber": row["document_number"], "formDesign": row["form_design"], "channel": row["channel"]}, "document": _document_dto(row)}


def submit_consolidated_document(data: ConsolidatedSubmissionInput, request: Request):
    allowed_types = {"invoice", "credit-note", "bill", "purchase-credit-note", "minipos-order"}
    if data.documentType not in allowed_types:
        raise HTTPException(422, "This document type cannot be submitted as a consolidated MyInvois document.")
    if data.dateTo < data.dateFrom:
        raise HTTPException(422, "The submission end date must be on or after the start date.")
    user = _erp_user(request, write=True)
    with connect() as db:
        _ensure_erp_schema(db)
        existing = db.execute("""SELECT id,document_type,status,total_amount FROM myinvois_consolidated_submissions
            WHERE tenant_id=%s AND idempotency_key=%s""", (user["tenant_id"], data.idempotencyKey)).fetchone()
        if existing:
            count = db.execute("SELECT count(*) AS count FROM myinvois_consolidated_documents WHERE submission_id=%s", (existing["id"],)).fetchone()["count"]
            return {"id": str(existing["id"]), "documentType": existing["document_type"], "status": existing["status"], "documentCount": count, "totalAmount": _money(existing["total_amount"])}
        documents = []
        total = Decimal("0")
        for raw_id in data.documentIds:
            document_id = _erp_uuid(raw_id, "document id")
            row = db.execute("""SELECT id,document_type,document_date,status,total_amount FROM erp_document_headers
                WHERE id=%s AND tenant_id=%s AND document_type=%s FOR UPDATE""", (document_id, user["tenant_id"], data.documentType)).fetchone()
            if not row:
                raise HTTPException(404, "One or more selected documents were not found in this company workspace.")
            if row["document_date"] is None or not (data.dateFrom <= row["document_date"] <= data.dateTo):
                raise HTTPException(422, f"Document {raw_id} is outside the selected submission date range.")
            if row["status"] != "ready":
                raise HTTPException(409, f"Document {raw_id} must be Ready before it can be submitted.")
            if db.execute("SELECT 1 FROM myinvois_consolidated_documents WHERE tenant_id=%s AND document_id=%s AND document_type=%s", (user["tenant_id"], document_id, data.documentType)).fetchone():
                raise HTTPException(409, f"Document {raw_id} has already been submitted in a consolidated document.")
            documents.append(row)
            total += row["total_amount"] or Decimal("0")
        submission = db.execute("""INSERT INTO myinvois_consolidated_submissions(tenant_id,document_type,date_from,date_to,status,idempotency_key,total_amount,created_by)
            VALUES (%s,%s,%s,%s,'queued',%s,%s,%s) RETURNING id""", (user["tenant_id"], data.documentType, data.dateFrom, data.dateTo, data.idempotencyKey, total, user["id"])).fetchone()
        for row in documents:
            db.execute("INSERT INTO myinvois_consolidated_documents(submission_id,tenant_id,document_id,document_type) VALUES (%s,%s,%s,%s)", (submission["id"], user["tenant_id"], row["id"], data.documentType))
    return {"id": str(submission["id"]), "documentType": data.documentType, "status": "queued", "documentCount": len(documents), "totalAmount": _money(total)}


def register(router, handlers):
    router.add_api_route('/api/erp/contacts', list_contacts, methods=['GET'], tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts', create_contact, methods=['POST'], status_code=201, tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts/{contact_id}', get_contact, methods=['GET'], tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts/{contact_id}', update_contact, methods=['PUT'], tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts/{contact_id}', delete_contact, methods=['DELETE'], tags=['ERP Contacts'])
    router.add_api_route('/api/erp/products', list_products, methods=['GET'], tags=['ERP Products'])
    router.add_api_route('/api/erp/products', create_product, methods=['POST'], status_code=201, tags=['ERP Products'])
    router.add_api_route('/api/erp/products/{product_id}', get_product, methods=['GET'], tags=['ERP Products'])
    router.add_api_route('/api/erp/products/{product_id}', update_product, methods=['PUT'], tags=['ERP Products'])
    router.add_api_route('/api/erp/products/{product_id}', delete_product, methods=['DELETE'], tags=['ERP Products'])
    router.add_api_route('/api/erp/catalog/{kind}', list_catalog, methods=['GET'], tags=['ERP Catalog'])
    router.add_api_route('/api/erp/catalog/{kind}', create_catalog, methods=['POST'], status_code=201, tags=['ERP Catalog'])
    router.add_api_route('/api/erp/catalog/{kind}/{entry_id}', get_catalog, methods=['GET'], tags=['ERP Catalog'])
    router.add_api_route('/api/erp/catalog/{kind}/{entry_id}', update_catalog, methods=['PUT'], tags=['ERP Catalog'])
    router.add_api_route('/api/erp/catalog/{kind}/{entry_id}', delete_catalog, methods=['DELETE'], tags=['ERP Catalog'])
    router.add_api_route('/api/erp/documents/{document_type}', list_documents, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}', create_document, methods=['POST'], status_code=201, tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}', get_document, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}', update_document, methods=['PATCH'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}', delete_document, methods=['DELETE'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/transition', transition_document, methods=['POST'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/duplicate', duplicate_document, methods=['POST'], status_code=201, tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/transfer', transfer_document, methods=['POST'], status_code=201, tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/history', document_history, methods=['GET'], tags=['ERP Documents'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/share', share_document, methods=['POST'], tags=['ERP Documents'])
    router.add_api_route('/api/public/erp-share/{token}', get_public_share, methods=['GET'], tags=['Public Shares'])
    router.add_api_route('/api/myinvois/consolidated', submit_consolidated_document, methods=['POST'], status_code=201, tags=['MyInvois'])
