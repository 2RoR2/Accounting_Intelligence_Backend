"""Chart-of-accounts import into both account lists the backend keeps.

`coa_accounts` (account_class: asset, liability, equity, revenue, cogs, expense) is used for
coding documents and bank lines; `ledger_accounts` (account_type: asset, liability, equity,
income, expense) is what journals post to. One CSV row creates the account in both, with the
same code and name. The ledger also stores the pack's `statement`, `report_section` and
`normal_balance` columns.

A matching existing ledger account can have a missing report section filled. A code that
exists in only one list with the same name is added to the other one. Other existing codes
are skipped as duplicates.
"""
from __future__ import annotations

import re

from .common import preview_token, read_rows, row_result, summary

KIND = "chart_of_accounts"
COLUMNS = ["code", "name", "type", "account_class", "statement", "report_section", "normal_balance"]
REQUIRED = ["code", "name", "type", "account_class"]
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
    """Plan additions to each list and fill missing report sections on matching ledger accounts."""
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
            fill_section = (
                in_ledger and in_ledger.get("report_section") is None and values["report_section"]
                and in_ledger["name"].casefold() == values["name"].casefold()
                and in_ledger["account_type"] == values["type"]
                and (not in_coa or (in_coa["name"].casefold() == values["name"].casefold()
                                    and in_coa["account_class"] == values["account_class"]))
            )
            if fill_section:
                values["fillReportSection"] = True
                warnings.append(f"The missing report section for ledger account {code} will be filled.")
            if in_coa and in_ledger:
                if not fill_section:
                    duplicate_of = in_coa["name"]
                    warnings.append(f"Account {code} already exists (\"{duplicate_of}\"); this row is skipped.")
            elif in_coa or in_ledger:
                current = (in_coa or in_ledger)["name"]
                if current.casefold() != values["name"].casefold():
                    duplicate_of = current
                    warnings.append(f"Account code {code} is already used for \"{current}\"; this row is skipped.")
                elif in_coa and in_coa["account_class"] != values["account_class"]:
                    errors.append(f"Account {code} is already in the chart as {in_coa['account_class']}, but this row "
                                  f"says {values['account_class']}. Make them agree before importing.")
                elif in_ledger and in_ledger["account_type"] != values["type"]:
                    errors.append(f"Account {code} is already in the ledger as {in_ledger['account_type']}, but this row "
                                  f"says {values['type']}. Make them agree before importing.")
                else:
                    values["addTo"] = ["ledger"] if in_coa else ["chart"]
                    warnings.append(f"Account {code} is already in the "
                                    f"{'chart' if in_coa else 'ledger'} list; it is added to the other list only.")
            else:
                values["addTo"] = ["chart", "ledger"]
        status = "error" if errors else ("duplicate" if duplicate_of else "new")
        results.append(row_result(source_row, status, values, errors, warnings, duplicate_of))
    return results


def _existing(db, tenant_id):
    coa = {r["code"]: {"name": r["name"], "account_class": r["account_class"]} for r in db.execute(
        "SELECT code, name, account_class FROM coa_accounts WHERE tenant_id=%s", (tenant_id,)).fetchall()}
    ledger = {r["code"]: r for r in db.execute(
        "SELECT code, name, account_type, report_section FROM ledger_accounts WHERE tenant_id=%s", (tenant_id,)).fetchall()}
    return coa, ledger


def preview(db, tenant_id, content: bytes) -> dict:
    rows = plan_chart(parse_chart(content), *_existing(db, tenant_id))
    body = summary(KIND, preview_token(KIND, tenant_id, content), rows)
    body["notStored"] = []
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
            db.execute("""INSERT INTO ledger_accounts(tenant_id, code, name, account_type,
                    statement, report_section, normal_balance)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (user["tenant_id"], values["code"], values["name"], values["type"],
                values["statement"] or None, values["report_section"] or None, values["normal_balance"] or None))
            created["ledger"] += 1
        if values.get("fillReportSection"):
            db.execute("""UPDATE ledger_accounts SET report_section=%s
                WHERE tenant_id=%s AND code=%s AND report_section IS NULL""",
                (values["report_section"], user["tenant_id"], values["code"]))
    return {"chartAccountsCreated": created["chart"], "ledgerAccountsCreated": created["ledger"]}
