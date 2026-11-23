"""Plan M2: the company profile's identity decides the direction of a captured document.

Rule of thumb: what the company issues is typed under Sales and never extracted; what it receives is captured in
the Inbox and lands in Purchases. A captured document whose seller is the company itself is its own sales document:
it is flagged ("create it under Sales") and never posts. No captured document posts as a sale.
"""
from __future__ import annotations

import re

from fastapi import HTTPException

OWN_SALES_MESSAGE = "This is your own sales document: create it under Sales."
DIRECTIONS = ("sales", "purchase", "unknown")


def _digits(value) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def company_identity(db, tenant_id) -> dict:
    row = db.execute("SELECT name,registration_number,profile FROM tenants WHERE id=%s", (tenant_id,)).fetchone()
    if not row:
        return {"name": "", "registrationNumber": "", "tin": ""}
    profile = row.get("profile") or {}
    return {"name": row.get("name") or "", "registrationNumber": row.get("registration_number") or "",
            "tin": profile.get("tin") or "", "sstNumber": profile.get("sstNumber") or "", "profile": profile}


def _is_company(identity: dict, name=None, registration=None, tin=None) -> bool:
    from validation.matching.vendor_matching import normalize_vendor_name
    own_tin, own_reg = _digits(identity.get("tin")), _digits(identity.get("registrationNumber"))
    if own_tin and _digits(tin) == own_tin:
        return True
    if own_reg and len(own_reg) >= 6 and _digits(registration) == own_reg:
        return True
    own_name = normalize_vendor_name(identity.get("name") or "")
    return bool(own_name) and normalize_vendor_name(name or "") == own_name


def direction(identity: dict, *, seller_name=None, seller_registration=None, seller_tin=None,
              buyer_name=None, buyer_registration=None, buyer_tin=None) -> str:
    """'sales' when the company is the seller, 'purchase' when it is the buyer, else 'unknown'."""
    if _is_company(identity, seller_name, seller_registration, seller_tin):
        return "sales"
    if _is_company(identity, buyer_name, buyer_registration, buyer_tin):
        return "purchase"
    return "unknown"


def _reviewed(record) -> dict:
    """The approved field values kept in the record snapshot (reviewedFields, else the field predictions)."""
    snapshot = record.get("snapshot") or {}
    values = dict(snapshot.get("reviewedFields") or {}) if isinstance(snapshot, dict) else {}
    if isinstance(snapshot, dict):
        for field in snapshot.get("fieldPredictions") or []:
            if isinstance(field, dict) and field.get("key") and values.get(field["key"]) in (None, ""):
                values[field["key"]] = field.get("value")
    return values


def record_direction(db, tenant_id, record, identity=None) -> str:
    identity = identity or company_identity(db, tenant_id)
    fields = _reviewed(record)
    pick = lambda *keys: next((fields.get(k) for k in keys if fields.get(k)), None)  # noqa: E731
    return direction(identity, seller_name=record.get("supplier_name") or pick("supplier"),
                     seller_registration=pick("supplier_registration_id", "supplier_registration_number"),
                     seller_tin=pick("supplier_tin", "supplier_tax_id"), buyer_name=record.get("buyer_name") or pick("customer"),
                     buyer_registration=pick("customer_registration_id", "buyer_registration_number"),
                     buyer_tin=pick("customer_tax_id", "customer_tin", "buyer_tin"))


def direction_warning(value: str) -> str | None:
    return OWN_SALES_MESSAGE if value == "sales" else None


def refuse_own_sales_document(db, tenant_id, record) -> None:
    """Scanned posting guard: a captured document never posts as a sale, and never posts at all when the company
    is its seller (that document belongs under Sales, typed)."""
    if record_direction(db, tenant_id, record) == "sales":
        raise HTTPException(409, OWN_SALES_MESSAGE)
    if record.get("category") == "accounts_receivable":
        raise HTTPException(409, "Captured documents post as purchases only. Sales documents are typed under Sales.")
