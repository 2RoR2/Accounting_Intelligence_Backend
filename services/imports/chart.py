"""Chart-of-accounts import into both account lists the backend keeps.

`coa_accounts` (account_class: asset, liability, equity, revenue, cogs, expense) is used for
coding documents and bank lines; `ledger_accounts` (account_type: asset, liability, equity,
income, expense) is what journals post to. One CSV row creates the account in both, with the
same code and name. The pack's `statement`, `report_section` and `normal_balance` columns are
checked but not stored: neither table has a column for them (a schema gap for D2 reports).

A code that already exists in both lists is a duplicate and is skipped. A code that exists in
only one list with the same name is added to the other one; with a different name it is a
duplicate that needs a person to decide.
"""
from __future__ import annotations

import re

from .common import preview_token, read_rows, row_result, summary

KIND = "chart_of_accounts"
COLUMNS = ["code", "name", "type", "account_class", "statement", "report_section", "normal_balance"]
REQUIRED = ["code", "name", "type", "account_class"]
NOT_STORED = ["statement", "report_section", "normal_balance"]
LEDGER_TYPES = {"asset", "liability", "equity", "income", "expense"}
CLASS_TYPE = {"asset": "asset", "liability": "liability", "equity": "equity", "revenue": "income",
              "cogs": "expense", "expense": "expense"}
STATEMENT = {"asset": "balance_sheet", "liability": "balance_sheet", "equity": "balance_sheet",
             "income": "profit_and_loss", "expense": "profit_and_loss"}
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,39}$")


def parse_chart(content: bytes):
    _, rows = read_rows(content, REQUIRED, "chart of accounts")
    parsed = []
    for source_row, raw in rows:
        values = {column: raw.get(column, "") for column in COLUMNS}
        for column in ("type", "account_class", "statement", "normal_balance"):
            values[column] = values[column].lower()
        errors = []
        if not _CODE.fullmatch(values["code"]):
            errors.append("Account code must be 1-40 letters or digits (dots and dashes allowed).")
        if not values["name"]:
            errors.append("Account name is empty.")
        elif len(values["name"]) > 200:
            errors.append("Account name is longer than 200 characters.")
        if values["type"] not in LEDGER_TYPES:
            errors.append("Type must be asset, liability, equity, income or expense.")
        if values["account_class"] not in CLASS_TYPE:
            errors.append("Account class must be asset, liability, equity, revenue, cogs or expense.")
        elif values["type"] in LEDGER_TYPES and CLASS_TYPE[values["account_class"]] != values["type"]:
            errors.append(f"Account class {values['account_class']} does not fit type {values['type']}.")
        if values["statement"]:
            if values["statement"] not in {"balance_sheet", "profit_and_loss"}:
                errors.append("Statement must be balance_sheet or profit_and_loss.")
            elif values["type"] in STATEMENT and STATEMENT[values["type"]] != values["statement"]:
                errors.append(f"A {values['type']} account belongs on the "
                              f"{STATEMENT[values['type']].replace('_', ' ')}, not the {values['statement'].replace('_', ' ')}.")
        if values["normal_balance"] and values["normal_balance"] not in {"debit", "credit"}:
            errors.append("Normal balance must be debit or credit.")
        parsed.append((source_row, values, errors))
    return parsed


def plan_chart(parsed, existing_coa, existing_ledger):
    """existing_* map code -> name. Each new row says which list(s) it is added to."""
    first_row = {}
    results = []
    for source_row, values, errors in parsed:
        errors, warnings, duplicate_of = list(errors), [], None
        code = values["code"]
        if code:
            first = first_row.setdefault(code, source_row)
            if first != source_row:
                errors.append(f"Account code {code} is also on row {first} of this file.")
        values = {**values, "addTo": []}
        if not errors:
            in_coa, in_ledger = existing_coa.get(code), existing_ledger.get(code)
            if in_coa and in_ledger:
                duplicate_of = in_coa
                warnings.append(f"Account {code} already exists (\"{in_coa}\"); this row is skipped.")
            elif in_coa or in_ledger:
                current = in_coa or in_ledger
                if current.casefold() == values["name"].casefold():
                    values["addTo"] = ["ledger"] if in_coa else ["chart"]
                    warnings.append(f"Account {code} is already in the "
                                    f"{'chart' if in_coa else 'ledger'} list; it is added to the other list only.")
                else:
                    duplicate_of = current
                    warnings.append(f"Account code {code} is already used for \"{current}\"; this row is skipped.")
            else:
                values["addTo"] = ["chart", "ledger"]
        status = "error" if errors else ("duplicate" if duplicate_of else "new")
        results.append(row_result(source_row, status, values, errors, warnings, duplicate_of))
    return results


def _existing(db, tenant_id):
    coa = {r["code"]: r["name"] for r in db.execute("SELECT code, name FROM coa_accounts WHERE tenant_id=%s",
                                                     (tenant_id,)).fetchall()}
    ledger = {r["code"]: r["name"] for r in db.execute("SELECT code, name FROM ledger_accounts WHERE tenant_id=%s",
                                                        (tenant_id,)).fetchall()}
    return coa, ledger


def preview(db, tenant_id, content: bytes) -> dict:
    rows = plan_chart(parse_chart(content), *_existing(db, tenant_id))
    body = summary(KIND, preview_token(KIND, tenant_id, content), rows)
    body["notStored"] = NOT_STORED
    return body


def apply(db, user, rows) -> dict:
    created = {"chart": 0, "ledger": 0}
    for row in rows:
        if row["status"] != "new":
            continue
        values = row["values"]
        if "chart" in values["addTo"]:
            db.execute("""INSERT INTO coa_accounts(tenant_id, code, name, account_class, created_by)
                VALUES (%s,%s,%s,%s,%s)""", (user["tenant_id"], values["code"], values["name"],
                                            values["account_class"], user["id"]))
            created["chart"] += 1
        if "ledger" in values["addTo"]:
            db.execute("""INSERT INTO ledger_accounts(tenant_id, code, name, account_type)
                VALUES (%s,%s,%s,%s)""", (user["tenant_id"], values["code"], values["name"], values["type"]))
            created["ledger"] += 1
    return {"chartAccountsCreated": created["chart"], "ledgerAccountsCreated": created["ledger"]}
