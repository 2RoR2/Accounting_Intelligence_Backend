"""Plan L4/L5: ledger posting rules for typed documents and approved scanned documents.

One rule set, two sources:
- a typed document (erp_document_headers) posts when it becomes "ready"; its journal has
  source_type 'erp_document' and the key ``erp:<document id>``, so a repeated request posts once;
- an approved scanned invoice/bill (standardised_records) posts through the same rule with the key the
  existing record endpoints already use (``supplier-bill:<record>:revision:<n>`` /
  ``customer-invoice:...``), so posting it here or through POST /api/ledger/journals/from-supplier-bill
  gives one journal, never two.

Voiding a posted typed document adds a reversal journal (the original stays; the ledger is append-only).
A closed (locked) accounting period refuses both posting and voiding. MYR only, like supplier payments:
foreign-currency conversion is not configured, so a non-MYR document is refused with 409.

Everything runs inside the caller's transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException

CENT = Decimal("0.01")
DEFAULT_CODES = {"receivable": "1100", "revenue": "4000", "tax_output": "2030", "payable": "2000",
                 "expense": "5000", "tax_input": "1140", "bank": "1010"}
PURPOSE_TYPES = {"receivable": "asset", "revenue": "income", "tax_output": "liability", "payable": "liability",
                 "expense": "expense", "tax_input": "asset", "bank": "asset"}

SALES_TYPES = {"quotation", "sales-order", "delivery-order", "invoice", "credit-note", "debit-note", "payment", "refund",
               "store-order", "minipos-order"}
PURCHASE_TYPES = {"purchase-order", "goods-received", "bill", "purchase-credit-note", "purchase-debit-note", "purchase-payment",
                  "purchase-refund"}
# Types that post a journal when they become ready. Everything else is record-only and says so.
POSTING_TYPES = {"invoice", "credit-note", "debit-note", "payment", "refund", "bill", "purchase-credit-note",
                 "purchase-debit-note", "purchase-payment", "purchase-refund", "money-in", "money-out", "bank-transfer",
                 "journal-entry"}
# Open items: what a customer or supplier owes / is owed until payments and credit notes settle it.
OPEN_ITEM_TYPES = {"invoice", "debit-note", "bill", "purchase-debit-note"}
# Payment / credit note -> the open items it can settle (Plan M: a debit note is settled like an invoice).
ALLOCATING_TYPES = {"payment": ("invoice", "debit-note"), "credit-note": ("invoice", "debit-note"),
                    "purchase-payment": ("bill", "purchase-debit-note"), "purchase-credit-note": ("bill", "purchase-debit-note")}
BANK_TYPES = {"payment", "refund", "purchase-payment", "purchase-refund", "money-in", "money-out", "bank-transfer"}
CONTACT_REQUIRED = {"invoice", "credit-note", "debit-note", "payment", "refund", "bill", "purchase-credit-note",
                    "purchase-debit-note", "purchase-payment", "purchase-refund"}


def cents(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Line:
    account_id: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    description: str = ""
    source_field: str | None = None


@dataclass
class Plan:
    lines: list[Line] = field(default_factory=list)

    def debit(self, account_id, amount, description, source_field=None):
        amount = cents(amount)
        if amount > 0:
            self.lines.append(Line(str(account_id), debit=amount, description=description, source_field=source_field))
        elif amount < 0:
            self.credit(account_id, -amount, description, source_field)

    def credit(self, account_id, amount, description, source_field=None):
        amount = cents(amount)
        if amount > 0:
            self.lines.append(Line(str(account_id), credit=amount, description=description, source_field=source_field))
        elif amount < 0:
            self.debit(account_id, -amount, description, source_field)

    def totals(self):
        return (sum((l.debit for l in self.lines), Decimal("0")), sum((l.credit for l in self.lines), Decimal("0")))


# ------------------------------------------------------------------ accounts and periods

def ledger_account(db, tenant_id, account_id, *, expected: str | set[str] | None = None, label="account"):
    from .common import parse_uuid
    row = db.execute("SELECT id,code,name,account_type,is_active FROM ledger_accounts WHERE id=%s AND tenant_id=%s",
                     (parse_uuid(str(account_id), f"{label} id"), tenant_id)).fetchone()
    expected = {expected} if isinstance(expected, str) else expected
    if not row or not row["is_active"] or (expected and row["account_type"] not in expected):
        kinds = " or ".join(sorted(expected)) if expected else "ledger"
        raise HTTPException(422, f"Choose an active {kinds} account for the {label}.")
    return row


def default_account(db, tenant_id, purpose: str):
    """The company's posting account for a purpose: its setting, else the standard chart code."""
    row = db.execute("""SELECT a.id,a.code,a.name,a.account_type,a.is_active FROM erp_posting_accounts p
        JOIN ledger_accounts a ON a.id=p.account_id AND a.tenant_id=p.tenant_id
        WHERE p.tenant_id=%s AND p.purpose=%s""", (tenant_id, purpose)).fetchone()
    if not row:
        row = db.execute("SELECT id,code,name,account_type,is_active FROM ledger_accounts WHERE tenant_id=%s AND code=%s",
                         (tenant_id, DEFAULT_CODES[purpose])).fetchone()
    if not row or not row["is_active"] or row["account_type"] != PURPOSE_TYPES[purpose]:
        label = purpose.replace("_", " ")
        raise HTTPException(409, f"No {label} account is set up. Choose one under posting accounts "
                                 f"(or add ledger account {DEFAULT_CODES[purpose]}).")
    return row


def open_period(db, tenant_id, on: date):
    row = db.execute("""SELECT id,period_code,status FROM accounting_periods
        WHERE tenant_id=%s AND starts_on<=%s AND ends_on>=%s ORDER BY starts_on DESC LIMIT 1""",
                     (tenant_id, on, on)).fetchone()
    if not row:
        raise HTTPException(409, f"No accounting period covers {on.isoformat()}. Add the period before posting.")
    if row["status"] != "open":
        raise HTTPException(409, f"The accounting period {row['period_code']} is closed; nothing can be posted or voided in it.")
    return row


def bank_ledger_account(db, tenant_id, catalog_id):
    """The ledger account behind a bank-account catalog entry, or the default bank account."""
    if not catalog_id:
        return default_account(db, tenant_id, "bank")
    row = db.execute("""SELECT a.id,a.code,a.name,a.account_type,a.is_active,c.is_active AS entry_active
        FROM erp_catalog_entries c JOIN ledger_accounts a ON a.id=c.ledger_account_id AND a.tenant_id=c.tenant_id
        WHERE c.id=%s AND c.tenant_id=%s AND c.kind='bank-account'""", (catalog_id, tenant_id)).fetchone()
    if not row or not row["entry_active"] or not row["is_active"] or row["account_type"] != "asset":
        raise HTTPException(422, "Choose an active bank account that is linked to a ledger cash or bank account.")
    return row


# ------------------------------------------------------------------ rules

def _line_account(db, tenant_id, line: dict, purpose: str):
    """A document line's account: its own choice, else its product's, else the default for the purpose."""
    expected = PURPOSE_TYPES[purpose]
    allowed = {expected, "asset", "liability", "equity"}
    if line.get("accountId"):
        return ledger_account(db, tenant_id, line["accountId"], expected=allowed, label="line account")["id"]
    if line.get("itemId"):
        column = "income_account_id" if purpose == "revenue" else "expense_account_id"
        product = db.execute(f"SELECT {column} AS account_id FROM erp_products WHERE id=%s AND tenant_id=%s",
                             (line["itemId"], tenant_id)).fetchone()
        if product and product["account_id"]:
            return product["account_id"]
    return default_account(db, tenant_id, purpose)["id"]


def line_net(line: dict) -> Decimal:
    return cents(Decimal(str(line.get("quantity", 1))) * Decimal(str(line.get("unitPrice", 0)))) - cents(line.get("discount", 0))


def scanned_control_account(db, tenant_id, document_id, purpose):
    """The receivable or payable account an invoice or bill posted to."""
    side = "debit" if purpose == "receivable" else "credit"
    row = db.execute(f"""SELECT l.account_id FROM journal_headers j
        JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id
        JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
        WHERE j.tenant_id=%s AND j.source_type='document' AND j.source_id=%s AND j.status='posted'
            AND l.{side}>0 AND a.account_type=%s
        ORDER BY j.posted_at DESC,j.id DESC,l.line_number LIMIT 1""",
        (tenant_id, document_id, PURPOSE_TYPES[purpose])).fetchone()
    return row["account_id"] if row else None


def _target_control_account(db, tenant_id, allocation, purpose):
    if allocation.get("source") == "scanned":
        record = db.execute("SELECT document_id FROM standardised_records WHERE id=%s AND tenant_id=%s",
                            (allocation["targetId"], tenant_id)).fetchone()
        return scanned_control_account(db, tenant_id, record["document_id"], purpose) if record else None
    target = db.execute("SELECT journal_id FROM erp_document_headers WHERE id=%s AND tenant_id=%s",
                        (allocation["targetId"], tenant_id)).fetchone()
    if not target or not target["journal_id"]:
        return None
    side = "debit" if purpose == "receivable" else "credit"
    row = db.execute(f"""SELECT l.account_id FROM journal_lines l JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
        WHERE l.journal_id=%s AND l.tenant_id=%s AND l.{side}>0 AND a.account_type=%s ORDER BY l.line_number LIMIT 1""",
                     (target["journal_id"], tenant_id, PURPOSE_TYPES[purpose])).fetchone()
    return row["account_id"] if row else None


def control_split(db, tenant_id, doc, purpose):
    """[(account, amount)] for the receivable/payable side of a document: each allocated amount on the account its
    invoice or bill was posted to, the rest on the default account."""
    total = cents(doc["total_amount"])
    parts: dict = {}
    allocated = Decimal("0")
    if doc["document_type"] in ALLOCATING_TYPES:
        for allocation in (doc["payload"] or {}).get("allocations", []):
            account = _target_control_account(db, tenant_id, allocation, purpose)
            if account:
                amount = min(cents(allocation["amount"]), total - allocated)
                if amount > 0:
                    parts[account] = parts.get(account, Decimal("0")) + amount
                    allocated += amount
    if total - allocated > 0:
        default = default_account(db, tenant_id, purpose)["id"]
        parts[default] = parts.get(default, Decimal("0")) + total - allocated
    return list(parts.items())


def plan_for_document(db, tenant_id, doc) -> Plan:
    """The journal lines a typed document posts, by type. Raises 409/422 when it cannot post."""
    kind = doc["document_type"]
    payload = doc["payload"] or {}
    lines = payload.get("lines", [])
    total = cents(doc["total_amount"])
    plan = Plan()
    if kind in {"invoice", "debit-note", "credit-note"}:
        sign = -1 if kind == "credit-note" else 1
        for account, amount in control_split(db, tenant_id, doc, "receivable"):
            plan.debit(account, sign * amount, "Accounts receivable", "total")
        for line in lines:
            plan.credit(_line_account(db, tenant_id, line, "revenue"), sign * line_net(line), line.get("description") or "Sales", "line")
        tax = sum((cents(l.get("tax", 0)) for l in lines), Decimal("0"))
        if tax:
            plan.credit(default_account(db, tenant_id, "tax_output")["id"], sign * tax, "SST output tax", "tax")
    elif kind in {"bill", "purchase-debit-note", "purchase-credit-note"}:
        sign = -1 if kind == "purchase-credit-note" else 1
        for line in lines:
            plan.debit(_line_account(db, tenant_id, line, "expense"), sign * line_net(line), line.get("description") or "Purchases", "line")
        tax = sum((cents(l.get("tax", 0)) for l in lines), Decimal("0"))
        if tax:
            plan.debit(default_account(db, tenant_id, "tax_input")["id"], sign * tax, "SST input tax", "tax")
        for account, amount in control_split(db, tenant_id, doc, "payable"):
            plan.credit(account, sign * amount, "Accounts payable", "total")
    elif kind in {"payment", "refund", "purchase-payment", "purchase-refund"}:
        bank = bank_ledger_account(db, tenant_id, doc["bank_account_id"])["id"]
        purpose = "receivable" if kind in {"payment", "refund"} else "payable"
        label = "Accounts receivable" if purpose == "receivable" else "Accounts payable"
        split = control_split(db, tenant_id, doc, purpose)
        if kind in {"payment", "purchase-refund"}:  # money comes in
            plan.debit(bank, total, "Bank", "total")
            for account, amount in split:
                plan.credit(account, amount, label, "total")
        else:
            for account, amount in split:
                plan.debit(account, amount, label, "total")
            plan.credit(bank, total, "Bank", "total")
    elif kind in {"money-in", "money-out"}:
        bank = bank_ledger_account(db, tenant_id, doc["bank_account_id"])["id"]
        if not lines:
            raise HTTPException(422, "Add at least one line with the account the money came from or went to.")
        for line in lines:
            if not line.get("accountId"):
                raise HTTPException(422, "Choose an account on every line of a money-in or money-out entry.")
            account = ledger_account(db, tenant_id, line["accountId"], label="line account")["id"]
            amount = line_net(line) + cents(line.get("tax", 0))
            if kind == "money-in":
                plan.credit(account, amount, line.get("description") or "Money in", "line")
            else:
                plan.debit(account, amount, line.get("description") or "Money out", "line")
        if kind == "money-in":
            plan.debit(bank, total, "Bank", "total")
        else:
            plan.credit(bank, total, "Bank", "total")
    elif kind == "bank-transfer":
        target_id = (payload.get("fields") or {}).get("toBankAccountId")
        if not doc["bank_account_id"] or not target_id:
            raise HTTPException(422, "Choose the bank account the money leaves and the one it goes to.")
        source = bank_ledger_account(db, tenant_id, doc["bank_account_id"])
        target = bank_ledger_account(db, tenant_id, target_id)
        if target["id"] == source["id"]:
            raise HTTPException(422, "A transfer needs two different bank accounts.")
        plan.debit(target["id"], total, f"Transfer from {source['code']}", "total")
        plan.credit(source["id"], total, f"Transfer to {target['code']}", "total")
    elif kind == "journal-entry":
        for line in lines:
            if not line.get("accountId"):
                raise HTTPException(422, "Choose an account on every journal line.")
            account = ledger_account(db, tenant_id, line["accountId"], label="journal line account")["id"]
            debit, credit = cents(line.get("debit", 0)), cents(line.get("credit", 0))
            if (debit > 0) == (credit > 0):
                raise HTTPException(422, "Each journal line needs either a debit or a credit.")
            plan.debit(account, debit, line.get("description") or "Journal entry", "line")
            plan.credit(account, credit, line.get("description") or "Journal entry", "line")
    else:
        raise HTTPException(500, "Posting was asked for a record-only document type.")
    debits, credits = plan.totals()
    if debits <= 0 or debits != credits:
        raise HTTPException(422, f"The document does not balance (debits {debits}, credits {credits}). Check its lines and total.")
    return plan


def post_plan(db, user, plan: Plan, *, key: str, source_type: str, source_id, journal_date: date, description: str):
    """Record the journal through the existing ledger code (period check, append-only, idempotency key)."""
    from services.routes.accounting.routes import JournalInput, JournalLineInput, record_journal
    period = open_period(db, user["tenant_id"], journal_date)
    return record_journal(db, user, JournalInput(
        periodId=str(period["id"]), idempotencyKey=key, sourceType=source_type, sourceId=str(source_id),
        journalDate=journal_date, description=description[:500],
        lines=[JournalLineInput(accountId=str(l.account_id), debit=l.debit, credit=l.credit,
                                description=l.description[:500], sourceField=l.source_field) for l in plan.lines]))


def reverse_in_transaction(db, user, journal_id, *, reason: str):
    """Reverse one posted journal in the caller's transaction (same shape as POST /api/ledger/journals/{id}/reverse),
    refusing a closed period. Returns the reversal journal id; a second call returns the same reversal."""
    tenant_id = user["tenant_id"]
    row = db.execute("""SELECT j.*,p.status AS period_status,p.period_code FROM journal_headers j
        JOIN accounting_periods p ON p.id=j.period_id AND p.tenant_id=j.tenant_id
        WHERE j.id=%s AND j.tenant_id=%s FOR UPDATE OF j, p""", (journal_id, tenant_id)).fetchone()
    if not row:
        raise HTTPException(409, "The posted journal for this document was not found; review the ledger.")
    existing = db.execute("SELECT id FROM journal_headers WHERE reversed_journal_id=%s AND tenant_id=%s",
                          (journal_id, tenant_id)).fetchone()
    if existing:
        return existing["id"]
    if row["status"] != "posted" or row["source_type"] == "reversal":
        raise HTTPException(409, "Only a posted journal can be reversed.")
    if row["period_status"] != "open":
        raise HTTPException(409, f"The accounting period {row['period_code']} is closed; reopen it before voiding.")
    lines = db.execute("SELECT * FROM journal_lines WHERE journal_id=%s AND tenant_id=%s ORDER BY line_number",
                       (journal_id, tenant_id)).fetchall()
    new = db.execute("""INSERT INTO journal_headers(tenant_id,period_id,idempotency_key,source_type,source_id,journal_date,
            description,posted_by,status,posted_at,reversed_journal_id)
        VALUES (%s,%s,%s,'reversal',%s,%s,%s,%s,'posted',now(),%s) RETURNING id""",
                     (tenant_id, row["period_id"], f"reversal:{journal_id}", journal_id, row["journal_date"],
                      f"Reversal: {reason}"[:500], user["id"], journal_id)).fetchone()
    for number, line in enumerate(lines, 1):
        db.execute("""INSERT INTO journal_lines(tenant_id,journal_id,account_id,line_number,description,debit,credit,source_field)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (tenant_id, new["id"], line["account_id"], number,
                                                  f"Reversal: {line['description']}"[:500], line["credit"], line["debit"],
                                                  line["source_field"]))
    db.execute("UPDATE journal_headers SET status='reversed' WHERE id=%s AND tenant_id=%s", (journal_id, tenant_id))
    return new["id"]


# ------------------------------------------------------------------ scanned documents (L5)

# List type -> (record category, document kinds) for approved scanned documents shown in the same list.
SCANNED_TYPES = {"invoice": ("accounts_receivable", ("invoice",)), "bill": ("accounts_payable", ("invoice", "bill")),
                 "credit-note": ("accounts_receivable", ("credit_note",)),
                 "purchase-credit-note": ("accounts_payable", ("credit_note",))}


def scanned_key(record) -> str:
    """The base key shared with the ledger's scanned record posting routes."""
    rid, rev = record["id"], record["revision_number"]
    if record["kind"] == "credit_note":
        return f"adjustment:credit_note:{rid}:revision:{rev}"
    prefix = "customer-invoice" if record["category"] == "accounts_receivable" else "supplier-bill"
    return f"{prefix}:{rid}:revision:{rev}"


def scanned_posted_journal(db, tenant_id, record):
    # Use the document source, as the waiting-to-post list does, including journals with legacy keys.
    return db.execute("""SELECT id,status,journal_date,
            bool_or(status='reversed') OVER () AS previously_reversed
        FROM journal_headers WHERE tenant_id=%s AND source_type IN ('document','adjustment') AND source_id=%s
        ORDER BY (status='posted') DESC, (status='reversed') DESC, posted_at DESC NULLS LAST, id DESC LIMIT 1""",
        (tenant_id, record["document_id"])).fetchone()


def _supplier_expense_account(db, tenant_id, record):
    if record.get("vendor_id"):
        supplier = db.execute("SELECT default_expense_account_id FROM vendors WHERE tenant_id=%s AND id=%s",
                              (tenant_id, record["vendor_id"])).fetchone()
        return supplier["default_expense_account_id"] if supplier else None
    if not record.get("supplier_name"):
        return None
    # Records standardised before a supplier was linked can still identify one by its exact normalised name.
    vendors = db.execute("SELECT vendor_name,default_expense_account_id FROM vendors WHERE tenant_id=%s AND is_active",
                         (tenant_id,)).fetchall()
    matches = [row for row in vendors if _same_party(record["supplier_name"], row["vendor_name"])]
    return matches[0]["default_expense_account_id"] if len(matches) == 1 else None


def plan_for_scanned(db, tenant_id, record, account_id=None) -> Plan:
    """An approved scanned invoice/bill/credit note as a one-line document: net to revenue/expense, tax to SST."""
    from services.company_identity import refuse_own_sales_document
    refuse_own_sales_document(db, tenant_id, record)
    total = cents(record["total_amount"])
    if total <= 0:
        raise HTTPException(422, "The approved document must have a positive total amount.")
    tax = cents(record["tax_amount"] or 0)
    if tax < 0 or tax >= total:
        tax = Decimal("0")
    receivable_side = record["category"] == "accounts_receivable"
    purpose = "revenue" if receivable_side else "expense"
    purchases_description = "Purchases"
    if account_id:
        allowed = {PURPOSE_TYPES[purpose], "asset", "liability", "equity"}
        line_account = ledger_account(db, tenant_id, account_id, expected=allowed, label="posting account")["id"]
    else:
        # The latest coding decision wins; if its code has no active ledger account, ask for one (Codex review P2).
        coded = db.execute("""SELECT c.code,la.id AS ledger_id FROM (SELECT account_id FROM coding_decisions
                WHERE tenant_id=%s AND record_id=%s AND line_item_id IS NULL ORDER BY created_at DESC, id DESC LIMIT 1) d
            JOIN coa_accounts c ON c.id=d.account_id AND c.tenant_id=%s
            LEFT JOIN ledger_accounts la ON la.tenant_id=c.tenant_id AND la.code=c.code AND la.is_active""",
                           (tenant_id, record["id"], tenant_id)).fetchone()
        if coded and not coded["ledger_id"]:
            raise HTTPException(422, f"The document is coded to {coded['code']}, which has no active ledger account. "
                                     "Choose the posting account.")
        supplier_default = None
        if not coded and not receivable_side and record["kind"] in {"bill", "invoice"}:
            supplier_default = _supplier_expense_account(db, tenant_id, record)
        if supplier_default:
            line_account = ledger_account(db, tenant_id, supplier_default, expected="expense",
                                          label="supplier default")["id"]
            purchases_description = "Purchases (supplier default)"
        else:
            line_account = coded["ledger_id"] if coded else default_account(db, tenant_id, purpose)["id"]
    sign = -1 if record["kind"] == "credit_note" else 1
    plan = Plan()
    if receivable_side:
        plan.debit(default_account(db, tenant_id, "receivable")["id"], sign * total, "Accounts receivable", "total_amount")
        plan.credit(line_account, sign * (total - tax), "Sales", "subtotal")
        if tax:
            plan.credit(default_account(db, tenant_id, "tax_output")["id"], sign * tax, "SST output tax", "tax_amount")
    else:
        plan.debit(line_account, sign * (total - tax), purchases_description, "subtotal")
        if tax:
            plan.debit(default_account(db, tenant_id, "tax_input")["id"], sign * tax, "SST input tax", "tax_amount")
        plan.credit(default_account(db, tenant_id, "payable")["id"], sign * total, "Accounts payable", "total_amount")
    return plan


def _same_party(name_a, name_b) -> bool:
    from validation.matching.vendor_matching import normalize_vendor_name
    a, b = normalize_vendor_name(name_a or ""), normalize_vendor_name(name_b or "")
    return bool(a) and a == b


def scanned_number(record) -> str:
    """The supplier's / our invoice number on a scanned record (some bills only carry a reference number)."""
    return (record.get("invoice_number") or record.get("reference_number") or "").strip()


def typed_copy_of_scanned(db, tenant_id, record):
    """A ready typed bill/invoice repeating this approved scanned one (same party and number), or None."""
    number = scanned_number(record)
    if not number or record.get("kind") == "credit_note":
        return None
    if record["category"] == "accounts_payable":
        rows = db.execute("""SELECT document_number,vendor_id,contact_name FROM erp_document_headers
            WHERE tenant_id=%s AND document_type='bill' AND status='ready' AND lower(btrim(reference_no))=lower(%s)""",
                          (tenant_id, number)).fetchall()
        party = record.get("supplier_name")
        return next((r for r in rows if (record.get("vendor_id") and r["vendor_id"] == record["vendor_id"])
                     or _same_party(r["contact_name"], party)), None)
    rows = db.execute("""SELECT document_number,contact_name FROM erp_document_headers
        WHERE tenant_id=%s AND document_type='invoice' AND status='ready' AND lower(btrim(document_number))=lower(%s)""",
                      (tenant_id, number)).fetchall()
    return next((r for r in rows if _same_party(r["contact_name"], record.get("buyer_name"))), None)


def refuse_if_typed_copy(db, tenant_id, record):
    copy = typed_copy_of_scanned(db, tenant_id, record)
    if copy:
        raise HTTPException(409, f"This document was already entered and posted as {copy['document_number']}; "
                                 "posting the scanned copy too would count it twice.")
