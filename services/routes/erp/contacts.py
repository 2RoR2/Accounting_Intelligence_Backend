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
import re
from typing import Any

from main import *  # noqa: F401,F403  (Request, HTTPException, connect)
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .common import audit, erp_user, parse_uuid
from .posting import ledger_account

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
    defaultExpenseAccountId: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def payment_term_days(row) -> int | None:
    """The payment terms a customer or supplier is on, in days (fields.paymentTermDays), or None when none are set.
    Documents use it to suggest a due date; it is never applied silently to an existing document."""
    value = ((row or {}).get("fields") or {}).get("paymentTermDays")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 365:
        return None
    return value


def _values(data: ContactInput):
    if len(json.dumps(data.fields, default=str)) > 16384:
        raise HTTPException(422, "Contact fields are too large.")
    if data.fields.get("paymentTermDays") is not None and payment_term_days({"fields": data.fields}) is None:
        raise HTTPException(422, "Payment terms must be a whole number of days from 0 to 365.")
    email = _clean(data.email)
    if email and ("@" not in email or " " in email):
        raise HTTPException(422, "Enter a valid email address.")
    return {"contact_code": _clean(data.code), "registration_number": _clean(data.registrationNumber), "email": email,
            "phone": _clean(data.phone), "address": _clean(data.address), "city": _clean(data.city),
            "state": _clean(data.state), "postcode": _clean(data.postcode)}


def _dto(kind: str, row, account=None) -> dict:
    name_column = TABLES[kind][1]
    result = {"id": str(row["id"]), "kind": kind, "legalName": row[name_column], "code": row.get("contact_code"),
            "registrationNumber": row.get("registration_number"), "email": row.get("email"), "phone": row.get("phone"),
            "address": row.get("address"), "city": row.get("city"), "state": row.get("state"),
            "postcode": row.get("postcode"), "fields": row.get("fields") or {}, "isActive": row["is_active"],
            "createdAt": str(row["created_at"]), "updatedAt": str(row.get("updated_at") or row["created_at"])}
    if kind == "supplier":
        account_id = row["default_expense_account_id"]
        result.update(defaultExpenseAccountId=str(account_id) if account_id else None,
                      defaultExpenseAccountCode=account["code"] if account else None,
                      defaultExpenseAccountName=account["name"] if account else None)
    return result


def _expense_account(db, tenant_id, account_id):
    if account_id:
        return db.execute("SELECT id,code,name FROM ledger_accounts WHERE tenant_id=%s AND id=%s",
                          (tenant_id, account_id)).fetchone()
    return None


def _requested_expense_account(db, tenant_id, data):
    if data.defaultExpenseAccountId is None:
        return None
    if data.kind != "supplier":
        raise HTTPException(422, "Only suppliers can have a default expense account.")
    return ledger_account(db, tenant_id, data.defaultExpenseAccountId, expected="expense", label="supplier default")


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
            account_ids = list({row["default_expense_account_id"] for row in rows
                                if each == "supplier" and row["default_expense_account_id"]})
            accounts = {row["id"]: row for row in db.execute(
                "SELECT id,code,name FROM ledger_accounts WHERE tenant_id=%s AND id=ANY(%s)",
                (user["tenant_id"], account_ids)).fetchall()} if account_ids else {}
            results.extend(_dto(each, row, accounts.get(row.get("default_expense_account_id"))) for row in rows)
    return sorted(results, key=lambda c: (c["legalName"].casefold(), c["kind"]))


def create_contact(data: ContactInput, request: Request):
    user = erp_user(request, WRITE_ROLES)
    table, column = TABLES[data.kind]
    name = data.legalName.strip()
    values = _values(data)
    with connect() as db:
        _duplicate_name(db, user["tenant_id"], data.kind, name)
        values["contact_code"] = _contact_code(db, user["tenant_id"], data.kind, name, values["contact_code"])
        columns = [column, *DETAIL_COLUMNS, "fields"]
        params = [name, *(values[c] for c in DETAIL_COLUMNS), Jsonb(data.fields)]
        account = _requested_expense_account(db, user["tenant_id"], data)
        if data.kind == "supplier":
            columns.append("default_expense_account_id")
            params.append(account["id"] if account else None)
        if data.kind == "employee":
            columns.append("created_by")
            params.append(user["id"])
        row = db.execute(f"""INSERT INTO {table}(tenant_id,{','.join(columns)}) VALUES (%s,{','.join(['%s'] * len(columns))})
            RETURNING *""", (user["tenant_id"], *params)).fetchone()
        audit(db, user, f"contact.{data.kind}_created", table, {"id": str(row["id"]), "name": name, "source": "contacts"})
    return _dto(data.kind, row, account)


def _contact_code(db, tenant_id, kind, name, wanted, *, exclude_id=None):
    """Plan M4: the short code in document names. Unique per company (per list); typed codes are upper-cased and
    checked, an empty one is suggested from the name. Employees keep whatever they had."""
    if kind not in {"supplier", "customer"}:
        return wanted
    from services.naming import suggest_contact_code
    table = TABLES[kind][0]
    db.execute("SELECT pg_advisory_xact_lock(hashtext('contact_code:' || %s::text))", (str(tenant_id),))
    taken = {r["code"] for r in db.execute(f"""SELECT upper(btrim(contact_code)) AS code FROM {table}
        WHERE tenant_id=%s AND contact_code IS NOT NULL AND (%s::uuid IS NULL OR id<>%s::uuid)""",
                                             (tenant_id, exclude_id, exclude_id)).fetchall()}
    if not wanted or not str(wanted).strip():
        return suggest_contact_code(name, taken)
    code = re.sub(r"\s+", "", str(wanted)).upper()
    if not re.fullmatch(r"[A-Z0-9-]{1,12}", code):
        raise HTTPException(422, "A contact code is 1-12 letters, digits or dashes.")
    if code in taken:
        raise HTTPException(409, f"Another {kind} already uses the code {code}.")
    return code


def get_contact(contact_id: str, request: Request):
    user = erp_user(request, READ_ROLES)
    with connect() as db:
        kind, row = find_contact(db, user["tenant_id"], contact_id)
        if not row:
            raise HTTPException(404, "Contact not found.")
        account = _expense_account(db, user["tenant_id"], row.get("default_expense_account_id"))
    return _dto(kind, row, account)


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
        values["contact_code"] = _contact_code(db, user["tenant_id"], kind, name, values["contact_code"] or row.get("contact_code"),
                                               exclude_id=row["id"])
        columns = [column, *DETAIL_COLUMNS, "fields"]
        params = [name, *(values[c] for c in DETAIL_COLUMNS), Jsonb(data.fields)]
        account = _requested_expense_account(db, user["tenant_id"], data)
        if kind == "supplier":
            if "defaultExpenseAccountId" in data.model_fields_set:
                columns.append("default_expense_account_id")
                params.append(account["id"] if account else None)
            else:
                account = _expense_account(db, user["tenant_id"], row["default_expense_account_id"])
        assignments = ", ".join(f"{c}=%s" for c in columns)
        updated = db.execute(f"UPDATE {table} SET {assignments}, updated_at=now() WHERE id=%s AND tenant_id=%s RETURNING *",
                             (*params, row["id"], user["tenant_id"])).fetchone()
        audit(db, user, f"contact.{kind}_updated", table, {"id": str(row["id"]), "name": name})
    return _dto(kind, updated, account)


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
