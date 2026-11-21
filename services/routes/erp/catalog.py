"""Product and catalog CRUD for a company workspace."""

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

from main import HTTPException, Request, connect
from psycopg import DataError
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from .common import DOCUMENT_TYPES, audit, erp_user, money, parse_uuid

VISIBLE_CATALOG_KINDS = (
    "payment-term", "payment-method", "currency", "bank-account", "tag", "category",
    "location", "custom-field", "number-format",
)
CATALOG_KINDS = VISIBLE_CATALOG_KINDS + (
    "product-group", "coupon", "content-page", "price-level", "asset-type",
    "form-design", "fixed-asset", "store-setting",
)
CATALOG_WRITE_ROLES = frozenset({"local_admin"})


def _dump_catalog_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class ERPProductInput(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    sku: str | None = Field(default=None, max_length=120)
    kind: str = Field(default="inventory", pattern=r"^(inventory|non-inventory|service|bundle)$")
    unit: str = Field(default="unit", min_length=1, max_length=40)
    salePrice: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=4)
    purchasePrice: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=4)
    quantity: Decimal = Field(default=Decimal(0), max_digits=18, decimal_places=3)
    groupName: str | None = Field(default=None, max_length=200)
    incomeAccountId: str | None = None
    expenseAccountId: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class ERPCatalogInput(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    code: str | None = None
    description: str | None = Field(default=None, max_length=4000)
    active: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    ledgerAccountId: str | None = None

    @field_validator("code", mode="before")
    @classmethod
    def validate_code(cls, value):
        if value is not None and (not isinstance(value, str) or len(value) > 100):
            raise HTTPException(422, "Code must be a string of at most 100 characters.")
        return value

    @field_validator("ledgerAccountId", mode="before")
    @classmethod
    def validate_account_id(cls, value):
        if value is not None and not isinstance(value, str):
            raise HTTPException(422, "Invalid ledger account id.")
        return value

    @field_validator("data", mode="before")
    @classmethod
    def validate_data(cls, value):
        if not isinstance(value, dict):
            raise HTTPException(422, "Data must be a JSON object.")
        try:
            encoded = _dump_catalog_json(value).encode("utf-8")
        except (ValueError, TypeError, RecursionError) as exc:
            raise HTTPException(422, "Data must be a valid JSON object.") from exc
        if len(encoded) > 16 * 1024:
            raise HTTPException(422, "Data must be at most 16 KB.")
        return value


def _name(value):
    name = value.strip()
    if not name:
        raise HTTPException(422, "Name is required.")
    return name


def _ledger_account(db, tenant_id, value, label, account_type):
    account_id = parse_uuid(value, label)
    row = db.execute(
        """SELECT id, code FROM ledger_accounts
           WHERE tenant_id=%s AND id=%s AND account_type=%s AND is_active
           FOR SHARE""",
        (tenant_id, account_id, account_type),
    ).fetchone()
    if not row:
        raise HTTPException(422, f"The {label} must reference an active {account_type} ledger account in this workspace.")
    return row


def _product_dto(row):
    return {
        "id": str(row["id"]), "name": row["name"], "sku": row["sku"], "kind": row["kind"],
        "unit": row["unit"], "salePrice": money(row["sale_price"]), "purchasePrice": money(row["purchase_price"]),
        "quantity": str(row["quantity"]), "groupName": row["group_name"], "fields": row["fields"] or {},
        "incomeAccountId": str(row["income_account_id"]) if row["income_account_id"] else None,
        "expenseAccountId": str(row["expense_account_id"]) if row["expense_account_id"] else None,
        "isActive": row["is_active"], "createdAt": str(row["created_at"]), "updatedAt": str(row["updated_at"]),
    }


def _audit_change(db, user, resource_type, change, row):
    audit(db, user, f"erp.{resource_type}_{change}", row["id"], {"kind": row["kind"], "name": row["name"]})


def list_products(request: Request, search: str | None = None, kind: str | None = None, includeInactive: bool = False):
    user = erp_user(request)
    pattern = f"%{search}%" if search else None
    kind = kind or None
    with connect() as db:
        rows = db.execute(
            """SELECT * FROM erp_products WHERE tenant_id=%s
               AND (%s::text IS NULL OR kind=%s) AND (%s OR is_active)
               AND (%s::text IS NULL OR name ILIKE %s OR sku ILIKE %s)
               ORDER BY name, id""",
            (user["tenant_id"], kind, kind, includeInactive, pattern, pattern, pattern),
        ).fetchall()
    return [_product_dto(row) for row in rows]


def _get_product(db, tenant_id, product_id):
    row = db.execute("SELECT * FROM erp_products WHERE tenant_id=%s AND id=%s", (tenant_id, product_id)).fetchone()
    if not row:
        raise HTTPException(404, "Product not found.")
    return row


def get_product(product_id: str, request: Request):
    user = erp_user(request)
    product_id = parse_uuid(product_id, "product id")
    with connect() as db:
        return _product_dto(_get_product(db, user["tenant_id"], product_id))


def _save_product(db, user, data, product_id=None):
    tenant_id = user["tenant_id"]
    if product_id is not None:
        _get_product(db, tenant_id, product_id)
    income = expense = None
    if data.incomeAccountId is not None:
        income = _ledger_account(db, tenant_id, data.incomeAccountId, "income account id", "income")["id"]
    if data.expenseAccountId is not None:
        expense = _ledger_account(db, tenant_id, data.expenseAccountId, "expense account id", "expense")["id"]
    values = (_name(data.name), data.sku, data.kind, data.unit, data.salePrice, data.purchasePrice,
              data.quantity, data.groupName, income, expense, Jsonb(data.fields))
    try:
        if product_id is None:
            row = db.execute(
                """INSERT INTO erp_products
                   (name,sku,kind,unit,sale_price,purchase_price,quantity,group_name,
                    income_account_id,expense_account_id,fields,tenant_id,created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (*values, tenant_id, user["id"]),
            ).fetchone()
        else:
            row = db.execute(
                """UPDATE erp_products SET name=%s,sku=%s,kind=%s,unit=%s,sale_price=%s,purchase_price=%s,
                   quantity=%s,group_name=%s,income_account_id=%s,expense_account_id=%s,fields=%s,updated_at=now()
                   WHERE tenant_id=%s AND id=%s RETURNING *""",
                (*values, tenant_id, product_id),
            ).fetchone()
    except UniqueViolation as exc:
        raise HTTPException(409, "This product SKU already exists.") from exc
    if not row:
        raise HTTPException(404, "Product not found.")
    _audit_change(db, user, "product", "created" if product_id is None else "updated", row)
    return _product_dto(row)


def create_product(data: ERPProductInput, request: Request):
    user = erp_user(request)
    with connect() as db:
        return _save_product(db, user, data)


def update_product(product_id: str, data: ERPProductInput, request: Request):
    user = erp_user(request)
    product_id = parse_uuid(product_id, "product id")
    with connect() as db:
        return _save_product(db, user, data, product_id)


def delete_product(product_id: str, request: Request):
    user = erp_user(request)
    product_id = parse_uuid(product_id, "product id")
    with connect() as db:
        row = db.execute(
            """UPDATE erp_products SET is_active=false,updated_at=now()
               WHERE tenant_id=%s AND id=%s RETURNING id,kind,name""",
            (user["tenant_id"], product_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Product not found.")
        _audit_change(db, user, "product", "deactivated", row)
    return {"id": str(product_id), "isActive": False}


def catalog_kinds(request: Request):
    erp_user(request)
    return {"visible": list(VISIBLE_CATALOG_KINDS), "all": list(CATALOG_KINDS)}


def _require_catalog_kind(kind):
    if kind not in CATALOG_KINDS:
        raise HTTPException(422, "Invalid catalog kind.")


def _catalog_dto(kind, row):
    if kind == "tag":
        code, name, values = None, row["name"], {"color": row["color"]}
    elif kind == "custom-field":
        code, name = row["field_key"], row["label"]
        values = {"fieldType": row["field_type"], "options": row["options"], "required": row["required"]}
    else:
        code, name, values = row["code"], row["name"], row["data"] or {}
    result = {
        "id": str(row["id"]), "kind": kind, "code": code, "name": name,
        "description": row.get("description"), "data": values, "isActive": row.get("is_active", True),
        "createdAt": str(row["created_at"]), "updatedAt": str(row.get("updated_at", row["created_at"])),
    }
    if kind == "bank-account":
        account_id = row["ledger_account_id"]
        result["ledgerAccountId"] = str(account_id) if account_id else None
        result["ledgerAccountCode"] = row["ledger_account_code"]
    return result


def _read_catalog(db, tenant_id, kind, *, entry_id=None, include_inactive=False):
    if kind == "tag":
        query = "SELECT e.* FROM document_tags e WHERE e.tenant_id=%s"
        params = (tenant_id,)
        order = "e.name"
    elif kind == "custom-field":
        query = "SELECT e.* FROM custom_fields e WHERE e.tenant_id=%s"
        params = (tenant_id,)
        order = "e.label"
    else:
        query = """SELECT e.*, a.code AS ledger_account_code FROM erp_catalog_entries e
                   LEFT JOIN ledger_accounts a ON a.id=e.ledger_account_id AND a.tenant_id=e.tenant_id
                   WHERE e.tenant_id=%s AND e.kind=%s AND (%s OR e.is_active)"""
        params = (tenant_id, kind, include_inactive or entry_id is not None)
        order = "e.name"
    if entry_id is not None:
        row = db.execute(query + " AND e.id=%s", (*params, entry_id)).fetchone()
        if not row:
            raise HTTPException(404, "Catalog entry not found.")
        return row
    return db.execute(query + f" ORDER BY {order}, e.id", params).fetchall()


def list_catalog(kind: str, request: Request, includeInactive: bool = False):
    user = erp_user(request)
    _require_catalog_kind(kind)
    with connect() as db:
        rows = _read_catalog(db, user["tenant_id"], kind, include_inactive=includeInactive)
    return [_catalog_dto(kind, row) for row in rows]


def get_catalog(kind: str, entry_id: str, request: Request):
    user = erp_user(request)
    _require_catalog_kind(kind)
    entry_id = parse_uuid(entry_id, "catalog entry id")
    with connect() as db:
        row = _read_catalog(db, user["tenant_id"], kind, entry_id=entry_id)
    return _catalog_dto(kind, row)


def _optional_string(values, key):
    if values.get(key) is not None and not isinstance(values[key], str):
        raise HTTPException(422, f"data.{key} must be a string.")


def _integer_in_range(values, key, minimum, maximum):
    value = values.get(key)
    if type(value) is not int or not minimum <= value <= maximum:
        raise HTTPException(422, f"data.{key} must be an integer from {minimum} to {maximum}.")


def _currency_code(code, values):
    code = (code or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise HTTPException(422, "Currency code must contain three letters (A-Z).")
    _optional_string(values, "symbol")
    rate = values.get("rate")
    rate_date = values.get("rateDate")
    if rate is not None:
        invalid_rate = "data.rate must be a positive decimal string with up to six decimal places."
        if not isinstance(rate, str) or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]{1,6})?|\.[0-9]{1,6})", rate):
            raise HTTPException(422, invalid_rate)
        amount = Decimal(rate)
        if amount <= 0:
            raise HTTPException(422, invalid_rate)
        if code == "MYR" and amount != 1:
            raise HTTPException(422, "The MYR rate must be 1 when provided.")
        if rate_date is None:
            raise HTTPException(422, "data.rateDate is required when data.rate is set.")
    if rate_date is not None:
        try:
            if not isinstance(rate_date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", rate_date):
                raise ValueError
            date.fromisoformat(rate_date)
        except ValueError as exc:
            raise HTTPException(422, "data.rateDate must be an ISO date (YYYY-MM-DD).") from exc
    return code


def _validate_catalog_values(kind, data):
    values = data.data
    if kind != "bank-account" and data.ledgerAccountId is not None:
        raise HTTPException(422, "Only bank-account entries may have a ledgerAccountId.")
    if kind == "currency":
        return _currency_code(data.code, values)
    if kind == "payment-term":
        _integer_in_range(values, "days", 0, 365)
    if kind == "payment-method" and values.get("type") not in (None, "cash", "bank_transfer", "cheque", "card", "ewallet", "other"):
        raise HTTPException(422, "Invalid payment method type.")
    if kind == "bank-account":
        if data.ledgerAccountId is None:
            raise HTTPException(422, "ledgerAccountId is required for bank-account entries.")
        _optional_string(values, "accountNumber")
        _optional_string(values, "bankName")
    if kind == "number-format":
        document_type = values.get("documentType")
        if not isinstance(document_type, str) or document_type not in DOCUMENT_TYPES:
            raise HTTPException(422, "data.documentType must be a supported ERP document type.")
        prefix = values.get("prefix")
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Z0-9-]{1,10}", prefix):
            raise HTTPException(422, "data.prefix must contain 1 to 10 uppercase letters, digits or hyphens.")
        _integer_in_range(values, "padding", 3, 8)
    return data.code


def _check_catalog_conflict(db, tenant_id, kind, entry_id, name, code, data):
    if kind == "currency":
        query = "SELECT 1 FROM erp_catalog_entries WHERE tenant_id=%s AND kind='currency' AND upper(code)=%s"
        value, message = code, "This currency code already exists."
    elif kind == "number-format" and data.active:
        query = """SELECT 1 FROM erp_catalog_entries
                   WHERE tenant_id=%s AND kind='number-format' AND is_active AND data->>'documentType'=%s"""
        value, message = data.data["documentType"], "An active number format already exists for this document type."
    elif kind == "custom-field":
        query = "SELECT 1 FROM custom_fields WHERE tenant_id=%s AND label=%s"
        value, message = name, "This custom field name already exists."
    else:
        return
    if db.execute(query + " AND id IS DISTINCT FROM %s::uuid", (tenant_id, value, entry_id)).fetchone():
        raise HTTPException(409, message)


def _write_tag(db, tenant_id, entry_id, name, values):
    color = values.get("color", "#64748b")
    if not isinstance(color, str):
        raise HTTPException(422, "data.color must be a string.")
    if entry_id is None:
        return db.execute(
            "INSERT INTO document_tags(tenant_id,name,color) VALUES (%s,%s,%s) RETURNING *",
            (tenant_id, name, color),
        ).fetchone()
    return db.execute(
        "UPDATE document_tags SET name=%s,color=%s WHERE tenant_id=%s AND id=%s RETURNING *",
        (name, color, tenant_id, entry_id),
    ).fetchone()


def _write_custom_field(db, tenant_id, entry_id, name, code, values):
    if not code or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", code):
        raise HTTPException(422, "Custom field code must start with a lowercase letter and contain up to 40 lowercase letters, digits or underscores.")
    field_type = values.get("fieldType", "text")
    options = values.get("options", [])
    required = values.get("required", False)
    if field_type not in ("text", "number", "date", "boolean", "select"):
        raise HTTPException(422, "Invalid custom field type.")
    if not isinstance(options, list) or not all(isinstance(option, str) for option in options):
        raise HTTPException(422, "data.options must be a list of strings.")
    if not isinstance(required, bool):
        raise HTTPException(422, "data.required must be a boolean.")
    params = (code, name, field_type, Jsonb(options, dumps=_dump_catalog_json), required, tenant_id)
    if entry_id is None:
        return db.execute(
            """INSERT INTO custom_fields(field_key,label,field_type,options,required,tenant_id)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            params,
        ).fetchone()
    return db.execute(
        """UPDATE custom_fields SET field_key=%s,label=%s,field_type=%s,options=%s,required=%s
           WHERE tenant_id=%s AND id=%s RETURNING *""",
        (*params, entry_id),
    ).fetchone()


def _write_catalog_entry(db, user, kind, entry_id, name, code, data, account):
    params = (code, name, data.description, Jsonb(data.data, dumps=_dump_catalog_json), data.active,
              account["id"] if account else None, user["tenant_id"], kind)
    if entry_id is None:
        return db.execute(
            """INSERT INTO erp_catalog_entries(code,name,description,data,is_active,ledger_account_id,tenant_id,kind,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (*params, user["id"]),
        ).fetchone()
    return db.execute(
        """UPDATE erp_catalog_entries SET code=%s,name=%s,description=%s,data=%s,is_active=%s,
           ledger_account_id=%s,updated_at=now() WHERE tenant_id=%s AND kind=%s AND id=%s RETURNING *""",
        (*params, entry_id),
    ).fetchone()


def _save_catalog(db, user, kind, data, entry_id=None):
    tenant_id = user["tenant_id"]
    if entry_id is not None:
        _read_catalog(db, tenant_id, kind, entry_id=entry_id)
    name = _name(data.name)
    code = _validate_catalog_values(kind, data)
    account = None
    if kind == "bank-account":
        account = _ledger_account(db, tenant_id, data.ledgerAccountId, "ledger account id", "asset")
    if kind in ("currency", "number-format", "custom-field"):
        # These uniqueness rules have no matching database constraint.
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"erp.catalog:{tenant_id}:{kind}",))
        _check_catalog_conflict(db, tenant_id, kind, entry_id, name, code, data)
    try:
        if kind == "tag":
            row = _write_tag(db, tenant_id, entry_id, name, data.data)
        elif kind == "custom-field":
            row = _write_custom_field(db, tenant_id, entry_id, name, code, data.data)
        else:
            row = _write_catalog_entry(db, user, kind, entry_id, name, code, data, account)
    except UniqueViolation as exc:
        raise HTTPException(409, "This catalog entry already exists.") from exc
    except DataError as exc:
        raise HTTPException(422, "This catalog entry contains invalid data.") from exc
    if not row:
        raise HTTPException(404, "Catalog entry not found.")
    if kind == "bank-account":
        row["ledger_account_code"] = account["code"]
    result = _catalog_dto(kind, row)
    _audit_change(db, user, "catalog", "created" if entry_id is None else "updated", result)
    return result


def create_catalog(kind: str, data: ERPCatalogInput, request: Request):
    user = erp_user(request, CATALOG_WRITE_ROLES)
    _require_catalog_kind(kind)
    with connect() as db:
        return _save_catalog(db, user, kind, data)


def update_catalog(kind: str, entry_id: str, data: ERPCatalogInput, request: Request):
    user = erp_user(request, CATALOG_WRITE_ROLES)
    _require_catalog_kind(kind)
    entry_id = parse_uuid(entry_id, "catalog entry id")
    with connect() as db:
        return _save_catalog(db, user, kind, data, entry_id)


def delete_catalog(kind: str, entry_id: str, request: Request):
    user = erp_user(request, CATALOG_WRITE_ROLES)
    _require_catalog_kind(kind)
    entry_id = parse_uuid(entry_id, "catalog entry id")
    params = (user["tenant_id"], entry_id)
    with connect() as db:
        if kind == "tag":
            row = db.execute(
                "DELETE FROM document_tags WHERE tenant_id=%s AND id=%s RETURNING id, name", params,
            ).fetchone()
        elif kind == "custom-field":
            row = db.execute(
                "DELETE FROM custom_fields WHERE tenant_id=%s AND id=%s RETURNING id, label AS name", params,
            ).fetchone()
        else:
            row = db.execute(
                """UPDATE erp_catalog_entries SET is_active=false,updated_at=now()
                   WHERE tenant_id=%s AND id=%s AND kind=%s RETURNING id,name""",
                (*params, kind),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Catalog entry not found.")
        _audit_change(db, user, "catalog", "deactivated", {**row, "kind": kind})
    return {"id": str(entry_id), "isActive": False}
