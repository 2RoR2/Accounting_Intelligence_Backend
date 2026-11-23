"""Contacts import: suppliers into `vendors` (+ `supplier_contacts`), customers into `customers`.

Columns are the company pack's `contacts.csv` (docs/COMPANY_PACKS.md). The schema keeps only
name, registration number (SSM) and address for both kinds of contact. For suppliers the
other columns (TIN, SST, e-mail, phone, terms, default account, category, currency, the
file's contact id) go into one `supplier_contacts` row: e-mail and phone in their columns,
the rest as JSON in `notes`. Customers have no such table, so those columns are reported
back as not stored (a schema gap, listed in the pull request). The file's contact id also
becomes the contact's code (both kinds) when no other contact uses that code, so imported
opening balances, which name the contact by that id, reach the contact's statement.

A contact that is already in the company (same name ignoring case and punctuation, or the
same SSM number, or for suppliers the same TIN) is reported as a duplicate and skipped,
never merged. Two rows of the same file that clash are errors: the file must be fixed.
"""
from __future__ import annotations

import json
import re

from validation.matching.vendor_matching import normalize_vendor_name

from .common import preview_token, read_rows, row_result, summary

KIND = "contacts"
COLUMNS = ["contact_id", "type", "name", "category", "ssm", "tin", "sst", "address", "email", "phone",
           "payment_terms_days", "default_account_code", "currency"]
REQUIRED = ["type", "name"]
NOT_STORED_FOR_CUSTOMERS = ["category", "tin", "sst", "email", "phone", "payment_terms_days",
                            "default_account_code", "currency"]
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./()\-]*$")
_LIMITS = {"contact_id": 60, "name": 200, "category": 60, "ssm": 40, "tin": 40, "sst": 40, "address": 500,
           "email": 200, "phone": 40, "default_account_code": 40}


def _identity(text: str) -> str:
    """Registration and tax numbers compare without spaces, dashes or case."""
    return re.sub(r"[\s\-]", "", text or "").upper()


def parse_contacts(content: bytes):
    """Check each row on its own. Returns [(source_row, values, errors)]."""
    _, rows = read_rows(content, REQUIRED, "contacts")
    parsed = []
    for source_row, raw in rows:
        values = {column: raw.get(column, "") for column in COLUMNS}
        errors = []
        values["type"] = values["type"].lower()
        if values["type"] not in {"supplier", "customer"}:
            errors.append("Type must be supplier or customer.")
        if not normalize_vendor_name(values["name"]):
            errors.append("Name is empty.")
        for column, limit in _LIMITS.items():
            if len(values[column]) > limit:
                errors.append(f"{column} is longer than {limit} characters.")
        for column in ("ssm", "tin", "sst"):
            if values[column] and not _IDENTIFIER.fullmatch(values[column]):
                errors.append(f"{column.upper()} number has characters that are not allowed.")
        if values["email"] and not _EMAIL.fullmatch(values["email"]):
            errors.append("E-mail address is not valid.")
        terms = values["payment_terms_days"]
        if terms and (not terms.isdecimal() or int(terms) > 365):
            errors.append("Payment terms must be a whole number of days from 0 to 365.")
        values["currency"] = (values["currency"] or "MYR").upper()
        if not re.fullmatch(r"[A-Z]{3}", values["currency"]):
            errors.append("Currency must be a 3-letter code such as MYR.")
        parsed.append((source_row, values, errors))
    return parsed


def plan_contacts(parsed, existing, account_codes=frozenset()):
    """Decide new / duplicate / error for every row.

    `existing` is {"supplier": [{"name", "ssm", "tin"}], "customer": [...]}; `account_codes` is the
    company's chart (an unknown default account is a warning, not an error: the chart may come later).
    """
    seen = {"supplier": {}, "customer": {}}
    for kind in seen:
        for contact in existing.get(kind, []):
            for key in _keys(contact["name"], contact.get("ssm"), contact.get("tin")):
                seen[kind].setdefault(key, ("existing", contact["name"]))
    in_file_ids = {}
    results = []
    for source_row, values, errors in parsed:
        errors = list(errors)
        warnings, duplicate_of = [], None
        if values["contact_id"]:
            first = in_file_ids.setdefault(values["contact_id"], source_row)
            if first != source_row:
                errors.append(f"Contact id {values['contact_id']} is also used on row {first}.")
        if not errors:
            kind = values["type"]
            for key in _keys(values["name"], values["ssm"], values["tin"]):
                where = seen[kind].get(key)
                if not where:
                    continue
                label = {"name": "name", "ssm": "SSM number", "tin": "TIN"}[key[0]]
                if where[0] == "existing":
                    duplicate_of = where[1]
                    warnings.append(f"Already in Contacts as \"{where[1]}\" (same {label}); this row is skipped.")
                else:
                    errors.append(f"Same {label} as row {where[1]} of this file.")
                break
            if not errors and duplicate_of is None:
                for key in _keys(values["name"], values["ssm"], values["tin"]):
                    seen[kind][key] = ("file", source_row)
        if values["default_account_code"] and values["default_account_code"] not in account_codes:
            warnings.append(f"Default account {values['default_account_code']} is not in the chart of accounts yet.")
        if values["type"] == "customer":
            dropped = [column for column in NOT_STORED_FOR_CUSTOMERS if values.get(column) and column != "currency"]
            if dropped:
                warnings.append("Not stored for customers yet: " + ", ".join(dropped) + ".")
        status = "error" if errors else ("duplicate" if duplicate_of else "new")
        results.append(row_result(source_row, status, values, errors, warnings, duplicate_of))
    return results


def _keys(name, ssm, tin):
    keys = [("name", normalize_vendor_name(name))]
    if _identity(ssm):
        keys.append(("ssm", _identity(ssm)))
    if _identity(tin):
        keys.append(("tin", _identity(tin)))
    return [key for key in keys if key[1]]


# --- database ------------------------------------------------------------------------------------

def load_existing(db, tenant_id):
    suppliers = []
    for row in db.execute("""SELECT v.id, v.vendor_name, v.registration_number,
            (SELECT array_agg(c.notes) FROM supplier_contacts c WHERE c.vendor_id=v.id AND c.tenant_id=v.tenant_id) notes
            FROM vendors v WHERE v.tenant_id=%s""", (tenant_id,)).fetchall():
        tin = None
        for note in row["notes"] or []:
            try:
                tin = tin or (json.loads(note) or {}).get("tin")
            except (TypeError, ValueError, AttributeError):
                continue
        suppliers.append({"name": row["vendor_name"], "ssm": row["registration_number"], "tin": tin})
    customers = [{"name": row["customer_name"], "ssm": row["registration_number"], "tin": None}
                 for row in db.execute("SELECT customer_name, registration_number FROM customers WHERE tenant_id=%s",
                                       (tenant_id,)).fetchall()]
    return {"supplier": suppliers, "customer": customers}


def _account_codes(db, tenant_id):
    rows = db.execute("""SELECT code FROM coa_accounts WHERE tenant_id=%s
        UNION SELECT code FROM ledger_accounts WHERE tenant_id=%s""", (tenant_id, tenant_id)).fetchall()
    return frozenset(row["code"] for row in rows)


def preview(db, tenant_id, content: bytes) -> dict:
    rows = plan_contacts(parse_contacts(content), load_existing(db, tenant_id), _account_codes(db, tenant_id))
    body = summary(KIND, preview_token(KIND, tenant_id, content), rows)
    body["notStoredForCustomers"] = NOT_STORED_FOR_CUSTOMERS
    return body


def _free_code(db, tenant_id, table, contact_id):
    """The file's contact id as the new contact's code, or None when it is blank or another contact has that code."""
    code = (contact_id or "").strip()
    if not code or db.execute(f"""SELECT 1 FROM {table} WHERE tenant_id=%s AND contact_code IS NOT NULL
            AND upper(btrim(contact_code))=upper(%s)""", (tenant_id, code)).fetchone():
        return None
    return code


def apply(db, user, rows) -> dict:
    """Insert the rows planned as new. The caller has already refused a plan with errors."""
    tenant_id = user["tenant_id"]
    created = {"supplier": 0, "customer": 0}
    # The contacts page takes the same lock before it saves a code, so two writers cannot give one code twice.
    db.execute("SELECT pg_advisory_xact_lock(hashtext('contact_code:' || %s::text))", (str(tenant_id),))
    for row in rows:
        if row["status"] != "new":
            continue
        values = row["values"]
        code = _free_code(db, tenant_id, "vendors" if values["type"] == "supplier" else "customers", values["contact_id"])
        if values["type"] == "supplier":
            # The pack's default account becomes the supplier's default expense account when it is an expense
            # account in this company's chart (scanned bills from the supplier then post there).
            default_account = db.execute("""SELECT id FROM ledger_accounts WHERE tenant_id=%s AND code=%s
                AND account_type='expense'""", (tenant_id, values["default_account_code"])).fetchone()                 if values["default_account_code"] else None
            vendor = db.execute("""INSERT INTO vendors(tenant_id, vendor_name, registration_number, address,
                    default_expense_account_id, contact_code)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""", (tenant_id, values["name"], values["ssm"] or None,
                                                            values["address"] or None,
                                                            default_account["id"] if default_account else None,
                                                            code)).fetchone()
            details = {key: values[key] for key in ("contact_id", "category", "tin", "sst", "default_account_code", "currency")
                       if values[key]}
            if values["payment_terms_days"]:
                details["payment_terms_days"] = int(values["payment_terms_days"])
            if details or values["email"] or values["phone"]:
                db.execute("""INSERT INTO supplier_contacts(tenant_id, vendor_id, contact_name, email, phone, role, notes)
                    VALUES (%s,%s,%s,%s,%s,'accounts',%s)""", (tenant_id, vendor["id"], values["name"],
                    values["email"] or None, values["phone"] or None,
                    json.dumps({"source": "contacts-import", **details}, sort_keys=True)))
        else:
            db.execute("""INSERT INTO customers(tenant_id, customer_name, registration_number, address, contact_code)
                VALUES (%s,%s,%s,%s,%s)""", (tenant_id, values["name"], values["ssm"] or None, values["address"] or None,
                                             code))
        created[values["type"]] += 1
    return {"suppliersCreated": created["supplier"], "customersCreated": created["customer"]}
