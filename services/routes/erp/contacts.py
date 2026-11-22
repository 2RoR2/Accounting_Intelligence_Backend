"""Plan L2: one contact list over the rows the rest of the system already uses.

latest_update kept its own erp_contacts table, so a supplier typed on the Contacts page was a different
row from the supplier that document matching, supplier memory and the contacts import use. Here the
contact endpoints read and write those rows directly:

    supplier -> vendors       (vendor_name)
    customer -> customers     (customer_name)
    employee -> erp_employees (legal_name; no table existed before)

A contact id is the row id; the kind says which table it lives in, and it cannot change (a company that
is both a customer and a supplier has one row in each list, as before). Deactivating hides a contact
from the list and from new documents; matching still sees it, so earlier documents keep their link.
"""
from __future__ import annotations

import json
from typing import Any

from main import *  # noqa: F401,F403  (Request, HTTPException, connect)
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .common import audit, erp_user, parse_uuid

READ_ROLES = {"local_admin", "accountant"}
WRITE_ROLES = {"local_admin", "accountant"}
TABLES = {
    "supplier": ("vendors", "vendor_name"),
    "customer": ("customers", "customer_name"),
    "employee": ("erp_employees", "legal_name"),
}
DETAIL_COLUMNS = ("contact_code", "registration_number", "email", "phone", "address", "city", "state", "postcode")


class ContactInput(BaseModel):
    kind: str = Field(default="customer", pattern=r"^(customer|supplier|employee)$")
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


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _values(data: ContactInput):
    if len(json.dumps(data.fields, default=str)) > 16384:
        raise HTTPException(422, "Contact fields are too large.")
    email = _clean(data.email)
    if email and ("@" not in email or " " in email):
        raise HTTPException(422, "Enter a valid email address.")
    return {"contact_code": _clean(data.code), "registration_number": _clean(data.registrationNumber), "email": email,
            "phone": _clean(data.phone), "address": _clean(data.address), "city": _clean(data.city),
            "state": _clean(data.state), "postcode": _clean(data.postcode)}


def _dto(kind: str, row) -> dict:
    name_column = TABLES[kind][1]
    return {"id": str(row["id"]), "kind": kind, "legalName": row[name_column], "code": row.get("contact_code"),
            "registrationNumber": row.get("registration_number"), "email": row.get("email"), "phone": row.get("phone"),
            "address": row.get("address"), "city": row.get("city"), "state": row.get("state"),
            "postcode": row.get("postcode"), "fields": row.get("fields") or {}, "isActive": row["is_active"],
            "createdAt": str(row["created_at"]), "updatedAt": str(row.get("updated_at") or row["created_at"])}


def _duplicate_name(db, tenant_id, kind: str, name: str, exclude_id=None):
    """Same rule as the existing supplier/customer create: one entry per name, ignoring case and punctuation."""
    from validation.matching.vendor_matching import normalize_vendor_name
    wanted = normalize_vendor_name(name)
    if not wanted:
        raise HTTPException(422, "A contact needs a name.")
    table, column = TABLES[kind]
    rows = db.execute(f"SELECT id,{column} AS name FROM {table} WHERE tenant_id=%s", (tenant_id,)).fetchall()
    same = next((r["name"] for r in rows if normalize_vendor_name(r["name"]) == wanted and r["id"] != exclude_id), None)
    if same:
        raise HTTPException(409, f'This contact already exists as "{same}".')


def find_contact(db, tenant_id, contact_id, *, for_update: bool = False):
    """(kind, row) for a contact id of this company, or (None, None). Used by the document module too."""
    contact_id = parse_uuid(contact_id, "contact id")
    lock = " FOR UPDATE" if for_update else ""
    for kind, (table, _column) in TABLES.items():
        row = db.execute(f"SELECT * FROM {table} WHERE id=%s AND tenant_id=%s{lock}", (contact_id, tenant_id)).fetchone()
        if row:
            return kind, row
    return None, None


def list_contacts(request: Request, kind: str | None = None, search: str | None = None, includeInactive: bool = False):
    user = erp_user(request, READ_ROLES)
    if kind and kind not in TABLES:
        raise HTTPException(422, "Invalid contact kind.")
    pattern = f"%{search.strip()}%" if search and search.strip() else None
    results = []
    with connect() as db:
        for each, (table, column) in TABLES.items():
            if kind and kind != each:
                continue
            rows = db.execute(f"""SELECT * FROM {table} WHERE tenant_id=%s AND (%s::boolean OR is_active)
                AND (%s::text IS NULL OR {column} ILIKE %s OR COALESCE(contact_code,'') ILIKE %s
                     OR COALESCE(registration_number,'') ILIKE %s OR COALESCE(email,'') ILIKE %s)""",
                              (user["tenant_id"], includeInactive, pattern, pattern, pattern, pattern, pattern)).fetchall()
            results.extend(_dto(each, row) for row in rows)
    return sorted(results, key=lambda c: (c["legalName"].casefold(), c["kind"]))


def create_contact(data: ContactInput, request: Request):
    user = erp_user(request, WRITE_ROLES)
    table, column = TABLES[data.kind]
    name = data.legalName.strip()
    values = _values(data)
    with connect() as db:
        _duplicate_name(db, user["tenant_id"], data.kind, name)
        columns = [column, *DETAIL_COLUMNS, "fields"]
        params = [name, *(values[c] for c in DETAIL_COLUMNS), Jsonb(data.fields)]
        if data.kind == "employee":
            columns.append("created_by")
            params.append(user["id"])
        row = db.execute(f"""INSERT INTO {table}(tenant_id,{','.join(columns)}) VALUES (%s,{','.join(['%s'] * len(columns))})
            RETURNING *""", (user["tenant_id"], *params)).fetchone()
        audit(db, user, f"contact.{data.kind}_created", table, {"id": str(row["id"]), "name": name, "source": "contacts"})
    return _dto(data.kind, row)


def get_contact(contact_id: str, request: Request):
    user = erp_user(request, READ_ROLES)
    with connect() as db:
        kind, row = find_contact(db, user["tenant_id"], contact_id)
    if not row:
        raise HTTPException(404, "Contact not found.")
    return _dto(kind, row)


def update_contact(contact_id: str, data: ContactInput, request: Request):
    user = erp_user(request, WRITE_ROLES)
    values = _values(data)
    name = data.legalName.strip()
    with connect() as db:
        kind, row = find_contact(db, user["tenant_id"], contact_id, for_update=True)
        if not row:
            raise HTTPException(404, "Contact not found.")
        if kind != data.kind:
            raise HTTPException(422, "A contact's type cannot change. Add it again as the other type.")
        table, column = TABLES[kind]
        if name != row[column]:
            _duplicate_name(db, user["tenant_id"], kind, name, exclude_id=row["id"])
        assignments = ", ".join(f"{c}=%s" for c in (column, *DETAIL_COLUMNS, "fields"))
        updated = db.execute(f"UPDATE {table} SET {assignments}, updated_at=now() WHERE id=%s AND tenant_id=%s RETURNING *",
                             (name, *(values[c] for c in DETAIL_COLUMNS), Jsonb(data.fields), row["id"], user["tenant_id"])).fetchone()
        audit(db, user, f"contact.{kind}_updated", table, {"id": str(row["id"]), "name": name})
    return _dto(kind, updated)


def _set_active(contact_id: str, request: Request, active: bool):
    user = erp_user(request, WRITE_ROLES)
    with connect() as db:
        kind, row = find_contact(db, user["tenant_id"], contact_id, for_update=True)
        if not row:
            raise HTTPException(404, "Contact not found.")
        table, column = TABLES[kind]
        db.execute(f"UPDATE {table} SET is_active=%s, updated_at=now() WHERE id=%s AND tenant_id=%s",
                   (active, row["id"], user["tenant_id"]))
        audit(db, user, f"contact.{kind}_{'reactivated' if active else 'deactivated'}", table,
              {"id": str(row["id"]), "name": row[column]})
    return {"id": str(row["id"]), "kind": kind, "isActive": active}


def delete_contact(contact_id: str, request: Request):
    """Deactivates (latest_update's DELETE did the same); rows are never removed, documents point at them."""
    return _set_active(contact_id, request, False)


def reactivate_contact(contact_id: str, request: Request):
    return _set_active(contact_id, request, True)


def contact_history(contact_id: str, request: Request):
    """Audit history of a contact for the detail page."""
    user = erp_user(request, READ_ROLES)
    with connect() as db:
        kind, row = find_contact(db, user["tenant_id"], contact_id)
        if not row:
            raise HTTPException(404, "Contact not found.")
        events = db.execute("""SELECT action,user_name,created_at FROM audit_logs
            WHERE tenant_id=%s AND action LIKE 'contact.%%' AND details LIKE %s ORDER BY created_at DESC LIMIT 50""",
                            (user["tenant_id"], f'%{row["id"]}%')).fetchall()
    return [{"action": e["action"], "by": e["user_name"], "at": str(e["created_at"])} for e in events]


def register(router):
    router.add_api_route('/api/erp/contacts', list_contacts, methods=['GET'], tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts', create_contact, methods=['POST'], status_code=201, tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts/{contact_id}', get_contact, methods=['GET'], tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts/{contact_id}', update_contact, methods=['PUT'], tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts/{contact_id}', delete_contact, methods=['DELETE'], tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts/{contact_id}/reactivate', reactivate_contact, methods=['POST'], tags=['ERP Contacts'])
    router.add_api_route('/api/erp/contacts/{contact_id}/history', contact_history, methods=['GET'], tags=['ERP Contacts'])
