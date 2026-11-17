"""Opening balances import: one balanced journal dated `as_at`, posted through the ledger code.

Rows with row_type `account` are the trial balance on the first day (debits equal credits).
Rows with `receivable` or `payable` are the unpaid invoices behind a control account (1100,
2000 in the packs): they must add up to that account's balance, and the journal then carries
one line per invoice instead of one line for the account, so the ledger keeps which invoice
makes up the balance (document number, contact, dates in the line description; the pack item
id in `source_field`). Open items are not yet stored as payables or receivables of their own
(no table for items without a document; task C4).

The journal must fall inside an open accounting period, every account must already be in the
ledger list, and an opening date has at most one journal that is not reversed (each import gets its own
idempotency key, so a corrected file can replace a reversed opening journal).
"""
from __future__ import annotations

from decimal import Decimal

from .common import parse_amount, parse_iso_date, preview_token, read_rows, row_result, summary

KIND = "opening_balances"
COLUMNS = ["row_type", "as_at", "account_code", "account_name", "contact_id", "item_id", "document_number",
           "document_date", "due_date", "debit", "credit"]
REQUIRED = ["row_type", "as_at", "account_code", "debit", "credit"]
ROW_TYPES = {"account", "receivable", "payable"}
ZERO = Decimal("0.00")


def idempotency_key(as_at, used_keys=frozenset()) -> str:
    """`opening-balances:<date>` for the first import of a date. Once that journal is reversed, the replacement
    takes the next free `:2`, `:3`, ..., otherwise the posting code would return the reversed journal as done."""
    base = f"opening-balances:{as_at.isoformat()}"
    key, number = base, 1
    while key in used_keys:
        number += 1
        key = f"{base}:{number}"
    return key


def parse_opening(content: bytes):
    _, rows = read_rows(content, REQUIRED, "opening balances")
    parsed = []
    for source_row, raw in rows:
        values = {column: raw.get(column, "") for column in COLUMNS}
        values["row_type"] = values["row_type"].lower()
        errors = []
        if values["row_type"] not in ROW_TYPES:
            errors.append("Row type must be account, receivable or payable.")
        if not values["account_code"]:
            errors.append("Account code is empty.")
        parsed_values = {}
        for column in ("as_at", "document_date", "due_date"):
            try:
                parsed_values[column] = parse_iso_date(values[column])
            except ValueError as error:
                errors.append(f"{column} {error}.")
        if not parsed_values.get("as_at") and "as_at must" not in " ".join(errors):
            errors.append("as_at (the opening date) is empty.")
        for column in ("debit", "credit"):
            try:
                parsed_values[column] = parse_amount(values[column]) or ZERO
            except ValueError as error:
                errors.append(f"{column.capitalize()} {error}.")
                parsed_values[column] = ZERO
        if not errors and (parsed_values["debit"] > 0) == (parsed_values["credit"] > 0):
            errors.append("Enter either a debit or a credit on each row, not both and not neither.")
        if values["row_type"] in {"receivable", "payable"} and not values["item_id"] and not values["document_number"]:
            errors.append("An open receivable or payable needs its document number or item id.")
        parsed.append((source_row, values, parsed_values, errors))
    return parsed


def plan_opening(parsed, ledger_accounts, open_periods, already_posted=frozenset(), used_keys=frozenset()):
    """ledger_accounts: code -> {"id", "name"}; open_periods: [{"id", "period_code", "starts_on", "ends_on"}];
    already_posted: as_at dates with an opening journal that is not reversed; used_keys: every opening-balances
    idempotency key already taken (reversed journals included). Returns (rows, file_errors, journal or None)."""
    rows, file_errors = [], []
    seen_accounts, seen_items = {}, {}
    for source_row, values, amounts, errors in parsed:
        errors = list(errors)
        code = values["account_code"]
        if code and code not in ledger_accounts:
            errors.append(f"Account {code} is not in the chart of accounts. Import the chart first.")
        if values["row_type"] == "account" and code:
            first = seen_accounts.setdefault(code, source_row)
            if first != source_row:
                errors.append(f"Account {code} has a balance on row {first} already.")
        item = values["item_id"] or values["document_number"]
        if values["row_type"] != "account" and item:
            first = seen_items.setdefault((code, item), source_row)
            if first != source_row:
                errors.append(f"Open item {item} is also on row {first}.")
        rows.append(row_result(source_row, "error" if errors else "new",
                               {**values, "debit": str(amounts["debit"]), "credit": str(amounts["credit"])}, errors))
        rows[-1]["_amounts"] = amounts
    valid = [row for row in rows if row["status"] == "new"]
    dates = {row["_amounts"]["as_at"] for row in valid}
    if len(dates) > 1:
        file_errors.append("All rows must have the same opening date (as_at); this file has "
                           + ", ".join(sorted(d.isoformat() for d in dates)) + ".")
    account_rows = [row for row in valid if row["values"]["row_type"] == "account"]
    item_rows = [row for row in valid if row["values"]["row_type"] != "account"]
    debit = sum((row["_amounts"]["debit"] for row in account_rows), ZERO)
    credit = sum((row["_amounts"]["credit"] for row in account_rows), ZERO)
    if not account_rows:
        file_errors.append("There are no account rows: the file needs the trial balance (row_type account).")
    elif debit != credit:
        file_errors.append(f"The account rows do not balance: debits {debit:,.2f}, credits {credit:,.2f} "
                           f"(difference {abs(debit - credit):,.2f}).")
    balance = {row["values"]["account_code"]: row["_amounts"]["debit"] - row["_amounts"]["credit"] for row in account_rows}
    items_by_account = {}
    for row in item_rows:
        items_by_account.setdefault(row["values"]["account_code"], []).append(row)
    for code, items in sorted(items_by_account.items()):
        total = sum((r["_amounts"]["debit"] - r["_amounts"]["credit"] for r in items), ZERO)
        if code not in balance:
            file_errors.append(f"Open items are listed for account {code}, but the trial balance has no row for it.")
        elif total != balance[code]:
            file_errors.append(f"The open items for account {code} add up to {total:,.2f}, "
                               f"but its opening balance is {balance[code]:,.2f}.")
    as_at = next(iter(dates)) if len(dates) == 1 else None
    period = None
    if as_at:
        period = next((p for p in open_periods if p["starts_on"] <= as_at <= p["ends_on"]), None)
        if not period:
            file_errors.append(f"No open accounting period covers {as_at.isoformat()}. "
                               "Create (or reopen) that period first.")
        if as_at in already_posted:
            file_errors.append(f"Opening balances for {as_at.isoformat()} are already posted. "
                               "Reverse that journal before importing again.")
    journal = None
    if not file_errors and not any(row["status"] == "error" for row in rows):
        journal = _journal(account_rows, items_by_account, ledger_accounts, period, as_at, used_keys)
    for row in rows:
        row.pop("_amounts", None)
    return rows, file_errors, journal


def _journal(account_rows, items_by_account, ledger_accounts, period, as_at, used_keys=frozenset()):
    lines = []
    for row in account_rows:
        code = row["values"]["account_code"]
        detail = items_by_account.get(code)
        for line_row in detail or [row]:
            values, amount_debit, amount_credit = line_row["values"], Decimal(line_row["values"]["debit"]), Decimal(line_row["values"]["credit"])
            if detail:
                parts = [f"Opening {values['row_type']} {values['document_number'] or values['item_id']}"]
                if values["contact_id"]:
                    parts.append(f"contact {values['contact_id']}")
                if values["document_date"]:
                    parts.append(f"dated {values['document_date']}")
                if values["due_date"]:
                    parts.append(f"due {values['due_date']}")
                description = ", ".join(parts)
                source_field = f"opening_item:{values['item_id'] or values['document_number']}"
            else:
                description, source_field = "Opening balance", None
            lines.append({"accountId": str(ledger_accounts[code]["id"]), "debit": amount_debit, "credit": amount_credit,
                          "description": description[:500], "sourceField": source_field})
    return {"periodId": str(period["id"]), "idempotencyKey": idempotency_key(as_at, used_keys), "sourceType": "manual",
            "sourceId": None, "journalDate": as_at, "description": f"Opening balances at {as_at.isoformat()}",
            "lines": lines}


def load_context(db, tenant_id):
    accounts = {r["code"]: {"id": r["id"], "name": r["name"]} for r in db.execute(
        "SELECT id, code, name FROM ledger_accounts WHERE tenant_id=%s AND is_active", (tenant_id,)).fetchall()}
    periods = db.execute("""SELECT id, period_code, starts_on, ends_on FROM accounting_periods
        WHERE tenant_id=%s AND status='open' ORDER BY starts_on""", (tenant_id,)).fetchall()
    journals = db.execute("""SELECT journal_date, idempotency_key, status FROM journal_headers
        WHERE tenant_id=%s AND idempotency_key LIKE 'opening-balances:%%'""", (tenant_id,)).fetchall()
    # A reversed opening journal no longer counts as posted, but its key stays taken.
    posted = frozenset(row["journal_date"] for row in journals if row["status"] != "reversed")
    return accounts, periods, posted, frozenset(row["idempotency_key"] for row in journals)


def preview_plan(db, tenant_id, content: bytes):
    rows, file_errors, journal = plan_opening(parse_opening(content), *load_context(db, tenant_id))
    body = summary(KIND, preview_token(KIND, tenant_id, content), rows, file_errors)
    if journal:
        total = sum((line["debit"] for line in journal["lines"]), ZERO)
        body["journal"] = {"date": journal["journalDate"].isoformat(), "lineCount": len(journal["lines"]),
                           "totalDebit": str(total), "totalCredit": str(total)}
    return body, journal


def preview(db, tenant_id, content: bytes) -> dict:
    return preview_plan(db, tenant_id, content)[0]
