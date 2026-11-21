from __future__ import annotations
import json
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto
from decimal import Decimal
from psycopg.errors import RaiseException
from pydantic import BaseModel, Field

class LedgerAccountInput(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    accountType: str

class PeriodInput(BaseModel):
    periodCode: str = Field(min_length=1, max_length=40)
    startsOn: date
    endsOn: date

class JournalLineInput(BaseModel):
    accountId: str
    debit: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=18, decimal_places=2)
    credit: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=18, decimal_places=2)
    description: str = ""
    sourceField: str | None = None

class JournalInput(BaseModel):
    periodId: str
    idempotencyKey: str = Field(min_length=1, max_length=160)
    sourceType: str
    sourceId: str | None = None
    journalDate: date
    description: str = Field(min_length=1, max_length=500)
    lines: list[JournalLineInput] = Field(min_length=2)

class SettlementInput(BaseModel):
    settlementRef: str = Field(min_length=1, max_length=160)
    settlementDate: date
    direction: str
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    sourceType: str
    sourceId: str | None = None

class AllocationInput(BaseModel):
    settlementId: str
    recordId: str
    amount: Decimal = Field(gt=0)

class SupplierBillPostingInput(BaseModel):
    periodId: str
    expenseAccountId: str
    payableAccountId: str
    journalDate: date
    description: str = "Supplier bill posting"

class SupplierPaymentPostingInput(BaseModel):
    periodId: str | None = None
    payableAccountId: str | None = None
    bankAccountId: str | None = None
    journalDate: date | None = None
    description: str = Field(default="Supplier payment settlement", min_length=1, max_length=500)
    # Only an explicit correction posts again after a reversal: name the reversed payment journal it replaces.
    replacesJournalId: str | None = None

class CustomerInvoicePostingInput(BaseModel):
    periodId: str
    receivableAccountId: str
    revenueAccountId: str
    journalDate: date
    description: str = "Customer invoice posting"

class AdjustmentPostingInput(BaseModel):
    periodId: str
    debitAccountId: str
    creditAccountId: str
    # Same precision as a journal line, so a bad amount is a 422 here rather than an error building the lines.
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    journalDate: date
    adjustmentKind: str
    description: str = "Approved document adjustment"

def list_ledger_accounts(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("SELECT * FROM ledger_accounts WHERE tenant_id=%s AND is_active ORDER BY code", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "code": r["code"], "name": r["name"], "accountType": r["account_type"]} for r in rows]

def create_ledger_account(data: LedgerAccountInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    if data.accountType not in {"asset", "liability", "equity", "income", "expense"}:
        raise HTTPException(422, "Invalid ledger account type.")
    with connect() as db:
        row = db.execute("INSERT INTO ledger_accounts(tenant_id,code,name,account_type) VALUES (%s,%s,%s,%s) RETURNING *", (user["tenant_id"], data.code.strip(), data.name.strip(), data.accountType)).fetchone()
    return {"id": str(row["id"]), "code": row["code"], "name": row["name"], "accountType": row["account_type"]}

def list_accounting_periods(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("SELECT * FROM accounting_periods WHERE tenant_id=%s ORDER BY starts_on", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "periodCode": r["period_code"], "startsOn": str(r["starts_on"]), "endsOn": str(r["ends_on"]), "status": r["status"]} for r in rows]

def create_accounting_period(data: PeriodInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    if data.endsOn < data.startsOn:
        raise HTTPException(422, "Period end must be on or after period start.")
    with connect() as db:
        row = db.execute("INSERT INTO accounting_periods(tenant_id,period_code,starts_on,ends_on) VALUES (%s,%s,%s,%s) RETURNING *", (user["tenant_id"], data.periodCode.strip(), data.startsOn, data.endsOn)).fetchone()
    return {"id": str(row["id"]), "periodCode": row["period_code"], "startsOn": str(row["starts_on"]), "endsOn": str(row["ends_on"]), "status": row["status"]}

def post_journal(data: JournalInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        return record_journal(db, user, data)

def record_journal(db, user, data: JournalInput):
    """Post one balanced journal inside the caller's transaction (also used by the opening-balances import)."""
    debits = sum((line.debit for line in data.lines), Decimal("0"))
    credits = sum((line.credit for line in data.lines), Decimal("0"))
    if debits <= 0 or debits != credits:
        raise HTTPException(422, "Journal must have equal non-zero debit and credit totals.")
    period_id = _uuid_or_422(data.periodId, "period id")
    source_id = _uuid_or_422(data.sourceId, "source id") if data.sourceId else None
    if data.sourceType == "settlement":
        settlement = db.execute("SELECT id FROM settlements WHERE id=%s AND tenant_id=%s FOR UPDATE",
                                (source_id, user["tenant_id"])).fetchone()
        if not settlement:
            raise HTTPException(404, "Settlement not found.")
        linked = db.execute("""SELECT id,idempotency_key FROM journal_headers
            WHERE tenant_id=%s AND source_type='settlement' AND source_id=%s AND status='posted'""",
            (user["tenant_id"], source_id)).fetchall()
        if any(journal["idempotency_key"] != data.idempotencyKey for journal in linked):
            raise HTTPException(409, "A journal already records this settlement.")
    key_query = "SELECT id,status,source_type,source_id FROM journal_headers WHERE tenant_id=%s AND idempotency_key=%s"
    key_params = (user["tenant_id"], data.idempotencyKey)
    existing = db.execute(key_query, key_params).fetchone()
    if existing:
        if data.sourceType == "settlement" and (existing["source_type"] != "settlement" or existing["source_id"] != source_id):
            raise HTTPException(409, "The journal idempotency key belongs to a different source.")
        return {"id": str(existing["id"]), "status": existing["status"], "idempotent": True}
    period = db.execute("SELECT * FROM accounting_periods WHERE id=%s AND tenant_id=%s FOR UPDATE", (period_id, user["tenant_id"])).fetchone()
    if not period or period["status"] != "open" or not (period["starts_on"] <= data.journalDate <= period["ends_on"]):
        raise HTTPException(409, "Journal date must be inside an open accounting period.")
    header = db.execute("""INSERT INTO journal_headers(tenant_id,period_id,idempotency_key,source_type,source_id,journal_date,description,posted_by,status,posted_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'posted',now())
        ON CONFLICT (tenant_id,idempotency_key) DO NOTHING RETURNING id""",
        (user["tenant_id"], period_id, data.idempotencyKey, data.sourceType, source_id, data.journalDate, data.description, user["id"])).fetchone()
    if header is None:
        existing = db.execute(key_query, key_params).fetchone()
        if data.sourceType == "settlement" and (existing["source_type"] != "settlement" or existing["source_id"] != source_id):
            raise HTTPException(409, "The journal idempotency key belongs to a different source.")
        return {"id": str(existing["id"]), "status": existing["status"], "idempotent": True}
    for number, line in enumerate(data.lines, 1):
        account_id = _uuid_or_422(line.accountId, "account id")
        if (line.debit > 0) == (line.credit > 0):
            raise HTTPException(422, "Each journal line must contain either a debit or a credit.")
        db.execute("""INSERT INTO journal_lines(tenant_id,journal_id,account_id,line_number,description,debit,credit,source_field)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (user["tenant_id"], header["id"], account_id, number, line.description, line.debit, line.credit, line.sourceField))
    return {"id": str(header["id"]), "status": "posted", "idempotent": False}

def _refuse_typed_copy(user, record_id):
    """Plan L5: a scanned invoice/bill already entered and posted as a typed document must not post again."""
    from services.routes.erp.posting import refuse_if_typed_copy
    with connect() as db:
        record = db.execute("""SELECT r.*,d.kind FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
            WHERE r.id=%s AND r.tenant_id=%s""", (record_id, user["tenant_id"])).fetchone()
        if record:
            refuse_if_typed_copy(db, user["tenant_id"], record)


def post_supplier_bill(record_id: str, data: SupplierBillPostingInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    record_id = _uuid_or_422(record_id, "record id")
    with connect() as db:
        record = db.execute("""SELECT id,document_id,category,total_amount,currency,revision_number,is_current,validation_status
            FROM standardised_records WHERE id=%s AND tenant_id=%s FOR UPDATE""", (record_id, user["tenant_id"])).fetchone()
    if not record or not record["is_current"] or record["validation_status"] != "passed":
        raise HTTPException(409, "Only the current validated standardised record can be posted.")
    if record["category"] != "accounts_payable":
        raise HTTPException(422, "Supplier posting requires a validated supplier bill record.")
    _refuse_typed_copy(user, record_id)
    if record["total_amount"] is None or record["total_amount"] <= 0:
        raise HTTPException(422, "The supplier bill must have a positive total amount.")
    payload = JournalInput(
        periodId=data.periodId,
        idempotencyKey=f"supplier-bill:{record_id}:revision:{record['revision_number']}",
        sourceType="document",
        sourceId=record["document_id"].__str__(),
        journalDate=data.journalDate,
        description=data.description,
        lines=[
            JournalLineInput(accountId=data.expenseAccountId, debit=record["total_amount"], description="Supplier bill expense"),
            JournalLineInput(accountId=data.payableAccountId, credit=record["total_amount"], description="Accounts payable"),
        ],
    )
    result = post_journal(payload, request)
    result.update({"recordId": record_id, "documentId": str(record["document_id"]), "revision": record["revision_number"], "sourceCurrency": record["currency"]})
    return result

def post_supplier_payment(transaction_id: str, data: SupplierPaymentPostingInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    transaction_id = _uuid_or_422(transaction_id, "bank transaction id")

    with connect() as db:
        bank = db.execute("""SELECT id,direction,status,signed_amount_decimal,currency
            FROM bank_transactions WHERE id=%s AND tenant_id=%s FOR UPDATE""",
            (transaction_id, user["tenant_id"])).fetchone()
        if not bank:
            raise HTTPException(404, "Bank transaction not found.")
        if bank["status"] == "ignored" or bank["direction"] != "money_out" or bank["signed_amount_decimal"] is None:
            raise HTTPException(409, "Only a money-out bank transaction can post a supplier payment.")

        settlement = db.execute("""SELECT id,direction,amount,currency,status
            FROM settlements
            WHERE tenant_id=%s AND source_type='bank_import' AND source_id=%s
            FOR UPDATE""", (user["tenant_id"], transaction_id)).fetchone()
        if not settlement or settlement["status"] != "recorded":
            raise HTTPException(409, "A recorded settlement is required before posting the supplier payment.")
        if settlement["direction"] != "money_out":
            raise HTTPException(409, "The linked settlement is not a money-out payment.")

        payment_amount = abs(bank["signed_amount_decimal"])
        if payment_amount <= 0 or settlement["amount"] != payment_amount:
            raise HTTPException(409, "The bank transaction and settlement amounts do not agree.")
        currency = str(settlement["currency"] or "").strip().upper()
        bank_currency = str(bank["currency"] or "").strip().upper()
        if not currency or currency != bank_currency:
            raise HTTPException(409, "The bank transaction and settlement currencies do not agree.")

        # One effective (posted) journal per settlement. A reversed one no longer counts, so a corrected payment
        # can be posted after a reversal; its key names the reversed journal, so retries stay idempotent.
        settlement_journals = db.execute("""SELECT id,status,idempotency_key FROM journal_headers
            WHERE tenant_id=%s AND source_type='settlement' AND source_id=%s
            ORDER BY posted_at, id FOR UPDATE""", (user["tenant_id"], settlement["id"])).fetchall()
        effective = [journal for journal in settlement_journals if journal["status"] == "posted"]
        if len(effective) > 1:
            raise HTTPException(409, "Multiple journals are linked to this supplier payment; review the ledger before continuing.")
        idempotency_key = f"supplier-payment:{settlement['id']}"
        # Earlier supplier payments only: a manual settlement journal that was posted and reversed is not a payment
        # this endpoint made, so it must not make the first payment request look already done.
        reversed_journals = [journal for journal in settlement_journals if journal["status"] == "reversed"
                             and str(journal["idempotency_key"] or "").startswith(idempotency_key)]
        replaces = str(_uuid_or_422(data.replacesJournalId, "replaced journal id")) if data.replacesJournalId else None
        repeated_correction = bool(replaces and effective
                                   and effective[0]["idempotency_key"] == f"{idempotency_key}:after:{replaces}")
        if repeated_correction:
            # A retry of a correction whose response was lost: report the correction it already posted (checked below).
            idempotency_key += f":after:{replaces}"
        elif replaces and (effective or not reversed_journals or str(reversed_journals[-1]["id"]) != replaces):
            raise HTTPException(409, "Only the latest reversed payment journal for this settlement can be replaced.")
        if not effective and reversed_journals:
            if not replaces:
                # A plain retry of the original request after a reversal: report the reversed journal, post nothing.
                latest = reversed_journals[-1]
                return {"id": str(latest["id"]), "status": latest["status"], "idempotent": True,
                        "recordId": None, "settlementId": str(settlement["id"]),
                        "amount": str(payment_amount), "currency": currency}
            idempotency_key += f":after:{replaces}"
        taken = db.execute("""SELECT source_type,source_id FROM journal_headers WHERE tenant_id=%s AND idempotency_key=%s""",
                           (user["tenant_id"], idempotency_key)).fetchone()
        if taken and (taken["source_type"] != "settlement" or taken["source_id"] != settlement["id"]):
            raise HTTPException(409, "A journal already uses this supplier payment idempotency key.")
        if effective:
            existing = effective[0]
            # Report success only for a journal that really is this payment: payable debit and bank credit of the amount.
            lines = db.execute("""SELECT a.account_type,l.debit,l.credit FROM journal_lines l
                JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
                WHERE l.journal_id=%s AND l.tenant_id=%s""", (existing["id"], user["tenant_id"])).fetchall()
            shape = sorted((line["account_type"], Decimal(line["debit"] or 0), Decimal(line["credit"] or 0)) for line in lines)
            if shape != sorted([("liability", payment_amount, Decimal(0)), ("asset", Decimal(0), payment_amount)]):
                raise HTTPException(409, "The journal linked to this payment does not debit payables and credit the bank for its amount; review the ledger.")
            return {
                "id": str(existing["id"]), "status": existing["status"], "idempotent": True,
                "recordId": None, "settlementId": str(settlement["id"]),
                "amount": str(payment_amount), "currency": currency,
            }

        if currency != "MYR":
            raise HTTPException(409, "Supplier payment journal posting currently supports MYR only; foreign-currency conversion is not configured.")

        match = db.execute("""SELECT c.record_id,r.document_id,r.category,COALESCE(r.amount_due,r.total_amount) AS amount_due,r.currency,
                r.is_current,r.validation_status,d.kind
            FROM reconciliation_candidates c
            JOIN standardised_records r ON r.id=c.record_id AND r.tenant_id=c.tenant_id
            JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
            WHERE c.tenant_id=%s AND c.bank_transaction_id=%s AND c.status='confirmed'
            FOR UPDATE OF r""", (user["tenant_id"], transaction_id)).fetchone()
        if not match:
            raise HTTPException(409, "Confirm a reconciliation match to an approved supplier bill first.")
        if match["category"] != "accounts_payable":
            raise HTTPException(422, "Supplier payment posting requires an accounts-payable record.")
        from services.routes.home.lanes import OPEN_ITEM_KINDS
        if match["kind"] not in OPEN_ITEM_KINDS["accounts_payable"]:
            # A receipt is already paid and a credit or debit note adjusts another document.
            raise HTTPException(422, "A supplier payment can be posted only against a supplier invoice or bill.")
        if not match["is_current"] or match["validation_status"] != "passed":
            raise HTTPException(409, "Only the current approved supplier bill can receive a payment.")
        if match["amount_due"] is None or match["amount_due"] <= 0:
            raise HTTPException(409, "The approved supplier bill must have a positive amount due.")
        if str(match["currency"] or "").strip().upper() != currency:
            raise HTTPException(409, "The supplier bill and settlement currencies do not agree.")
        if payment_amount > match["amount_due"]:
            raise HTTPException(409, "The payment cannot exceed the approved supplier bill amount due.")

        allocations = db.execute("""SELECT COUNT(DISTINCT record_id) FILTER (WHERE status='active') record_count,
                COALESCE(SUM(amount) FILTER (WHERE status='active'),0) allocated_amount,
                COALESCE(SUM(amount) FILTER (WHERE status='active' AND record_id=%s),0) record_amount
            FROM settlement_allocations a
            WHERE a.tenant_id=%s AND a.settlement_id=%s
                AND NOT EXISTS (
                    SELECT 1 FROM settlement_allocations reversal
                    WHERE reversal.tenant_id=a.tenant_id AND reversal.reverses_allocation_id=a.id
                )""",
            (match["record_id"], user["tenant_id"], settlement["id"])).fetchone()
        if (allocations["record_count"] != 1
                or allocations["allocated_amount"] != payment_amount
                or allocations["record_amount"] != payment_amount):
            raise HTTPException(409, "The settlement must be fully allocated to this supplier bill before posting.")

        # When the bill was posted, the payment must clear the payable account that bill credited.
        bill_payable = db.execute("""SELECT l.account_id FROM journal_headers j
            JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id
            JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
            WHERE j.tenant_id=%s AND j.source_type='document' AND j.source_id=%s AND j.status='posted'
                AND l.credit>0 AND a.account_type='liability'
            ORDER BY j.posted_at DESC, l.line_number LIMIT 1""", (user["tenant_id"], match["document_id"])).fetchone()
        payable_account_text = data.payableAccountId or (str(bill_payable["account_id"]) if bill_payable else None)
        if not data.periodId or not payable_account_text or not data.bankAccountId or not data.journalDate:
            raise HTTPException(422, "Choose an open accounting period, payable account, bank account, and journal date.")
        payable_account_id = _uuid_or_422(payable_account_text, "payable account id")
        if bill_payable and str(payable_account_id) != str(bill_payable["account_id"]):
            raise HTTPException(422, "Use the payable account this supplier bill was posted to.")
        bank_account_id = _uuid_or_422(data.bankAccountId, "bank account id")
        payable_account = db.execute("""SELECT account_type,is_active FROM ledger_accounts
            WHERE id=%s AND tenant_id=%s FOR SHARE""", (payable_account_id, user["tenant_id"])).fetchone()
        bank_account = db.execute("""SELECT account_type,is_active FROM ledger_accounts
            WHERE id=%s AND tenant_id=%s FOR SHARE""", (bank_account_id, user["tenant_id"])).fetchone()
        if not payable_account or not payable_account["is_active"] or payable_account["account_type"] != "liability":
            raise HTTPException(422, "Choose an active liability account for accounts payable.")
        if not bank_account or not bank_account["is_active"] or bank_account["account_type"] != "asset":
            raise HTTPException(422, "Choose an active asset account for the bank or cash account.")

        result = record_journal(db, user, JournalInput(
            periodId=data.periodId,
            idempotencyKey=idempotency_key,
            sourceType="settlement",
            sourceId=str(settlement["id"]),
            journalDate=data.journalDate,
            description=data.description,
            lines=[
                JournalLineInput(accountId=str(payable_account_id), debit=payment_amount,
                                 description="Accounts payable settlement", sourceField="supplier_payment"),
                JournalLineInput(accountId=str(bank_account_id), credit=payment_amount,
                                 description="Bank payment", sourceField="supplier_payment"),
            ],
        ))
        if not result["idempotent"]:
            db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
                VALUES (%s,%s,%s,%s,'ledger.supplier_payment_posted',%s,%s)""",
                (user["tenant_id"], user["id"], user["name"], user["role"], result["id"],
                 json.dumps({"transactionId": str(transaction_id), "settlementId": str(settlement["id"]),
                             "recordId": str(match["record_id"])})))

    return {
        **result,
        "recordId": str(match["record_id"]), "settlementId": str(settlement["id"]),
        "amount": str(payment_amount), "currency": currency,
    }

def post_customer_invoice(record_id: str, data: CustomerInvoicePostingInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    record_id = _uuid_or_422(record_id, "record id")
    with connect() as db:
        record = db.execute("""SELECT id,document_id,category,total_amount,currency,revision_number,is_current,validation_status
            FROM standardised_records WHERE id=%s AND tenant_id=%s FOR UPDATE""", (record_id, user["tenant_id"])).fetchone()
    if not record or not record["is_current"] or record["validation_status"] != "passed":
        raise HTTPException(409, "Only the current validated standardised record can be posted.")
    if record["category"] != "accounts_receivable":
        raise HTTPException(422, "Customer posting requires an accounts-receivable record.")
    _refuse_typed_copy(user, record_id)
    if record["total_amount"] is None or record["total_amount"] <= 0:
        raise HTTPException(422, "The customer invoice must have a positive total amount.")
    payload = JournalInput(
        periodId=data.periodId,
        idempotencyKey=f"customer-invoice:{record_id}:revision:{record['revision_number']}",
        sourceType="document", sourceId=str(record["document_id"]), journalDate=data.journalDate,
        description=data.description,
        lines=[
            JournalLineInput(accountId=data.receivableAccountId, debit=record["total_amount"], description="Accounts receivable"),
            JournalLineInput(accountId=data.revenueAccountId, credit=record["total_amount"], description="Customer revenue"),
        ],
    )
    result = post_journal(payload, request)
    result.update({"recordId": record_id, "documentId": str(record["document_id"]), "revision": record["revision_number"], "sourceCurrency": record["currency"]})
    return result

def post_document_adjustment(record_id: str, data: AdjustmentPostingInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    record_id = _uuid_or_422(record_id, "record id")
    if data.adjustmentKind not in {"credit_note", "debit_note"}:
        raise HTTPException(422, "Adjustment kind must be credit_note or debit_note.")
    with connect() as db:
        record = db.execute("""SELECT id,document_id,currency,revision_number,is_current,validation_status
            FROM standardised_records WHERE id=%s AND tenant_id=%s FOR UPDATE""", (record_id, user["tenant_id"])).fetchone()
    if not record or not record["is_current"] or record["validation_status"] != "passed":
        raise HTTPException(409, "Only the current validated standardised record can be adjusted.")
    payload = JournalInput(
        periodId=data.periodId,
        idempotencyKey=f"adjustment:{data.adjustmentKind}:{record_id}:revision:{record['revision_number']}",
        sourceType="adjustment", sourceId=str(record["document_id"]), journalDate=data.journalDate,
        description=data.description,
        lines=[
            JournalLineInput(accountId=data.debitAccountId, debit=data.amount, description=f"{data.adjustmentKind} debit"),
            JournalLineInput(accountId=data.creditAccountId, credit=data.amount, description=f"{data.adjustmentKind} credit"),
        ],
    )
    result = post_journal(payload, request)
    result.update({"recordId": record_id, "documentId": str(record["document_id"]), "revision": record["revision_number"], "adjustmentKind": data.adjustmentKind, "sourceCurrency": record["currency"]})
    return result

def reverse_journal(journal_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    journal_id = _uuid_or_422(journal_id, "journal id")
    with connect() as db:
        row = db.execute("SELECT * FROM journal_headers WHERE id=%s AND tenant_id=%s FOR UPDATE", (journal_id, user["tenant_id"])).fetchone()
        if not row or row["status"] != "posted": raise HTTPException(409, "Only a posted journal can be reversed.")
        if row["source_type"] == "erp_document":
            # Voiding keeps the document, its allocations and the open-item balances in step with the ledger.
            raise HTTPException(409, "This journal belongs to a sales or purchase document. Void the document instead.")
        if row["source_type"] == "reversal" or row["reversed_journal_id"]:
            # Keeps one level of reversal: the report's currency, the settlement lock and corrections all rely on it.
            raise HTTPException(409, "A reversal cannot be reversed. Post the corrected entry again instead.")
        if db.execute("SELECT 1 FROM journal_headers WHERE reversed_journal_id=%s AND tenant_id=%s", (journal_id, user["tenant_id"])).fetchone():
            raise HTTPException(409, "This journal has already been reversed.")
        lines = db.execute("SELECT * FROM journal_lines WHERE journal_id=%s AND tenant_id=%s ORDER BY line_number", (journal_id, user["tenant_id"])).fetchall()
        new = db.execute("""INSERT INTO journal_headers(tenant_id,period_id,idempotency_key,source_type,source_id,journal_date,description,posted_by,status,posted_at,reversed_journal_id)
            VALUES (%s,%s,%s,'reversal',%s,%s,%s,%s,'posted',now(),%s) RETURNING id""", (user["tenant_id"], row["period_id"], f"reversal:{journal_id}", journal_id, row["journal_date"], f"Reversal of {journal_id}", user["id"], journal_id)).fetchone()
        for n, line in enumerate(lines, 1):
            db.execute("INSERT INTO journal_lines(tenant_id,journal_id,account_id,line_number,description,debit,credit,source_field) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (user["tenant_id"], new["id"], line["account_id"], n, f"Reversal: {line['description']}", line["credit"], line["debit"], line["source_field"]))
        db.execute("UPDATE journal_headers SET status='reversed' WHERE id=%s AND tenant_id=%s", (journal_id, user["tenant_id"]))
    return {"id": str(new["id"]), "status": "posted", "reversedJournalId": journal_id}

def create_settlement(data: SettlementInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if data.direction not in {"money_in", "money_out"} or data.sourceType not in {"bank_import", "manual_record", "document_evidence"}:
        raise HTTPException(422, "Invalid settlement direction or source type.")
    source_id = _uuid_or_422(data.sourceId, "source id") if data.sourceId else None
    with connect() as db:
        row = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,source_type,source_id,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], data.settlementRef.strip(), data.settlementDate, data.direction, data.amount, data.currency.upper(), data.sourceType, source_id, user["id"])).fetchone()
    return {"id": str(row["id"]), "settlementRef": row["settlement_ref"], "direction": row["direction"], "amount": str(row["amount"]), "currency": row["currency"], "status": row["status"]}

def create_settlement_from_bank(transaction_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "bank transaction id")
    with connect() as db:
        bank = db.execute("SELECT * FROM bank_transactions WHERE id=%s AND tenant_id=%s FOR UPDATE", (transaction_id, user["tenant_id"])).fetchone()
        if not bank: raise HTTPException(404, "Bank transaction not found.")
        existing = db.execute("SELECT * FROM settlements WHERE tenant_id=%s AND source_type='bank_import' AND source_id=%s", (user["tenant_id"], transaction_id)).fetchone()
        if existing:
            return {"id": str(existing["id"]), "status": existing["status"], "idempotent": True}
        if bank["status"] == 'ignored' or not bank["direction"] or not bank["currency"] or not bank["signed_amount_decimal"]:
            raise HTTPException(409, "Only a classified bank transaction can become a settlement.")
        row = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,source_type,source_id,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,'bank_import',%s,%s) RETURNING *""", (user["tenant_id"], f"BANK-{transaction_id}", bank["posted_date"], bank["direction"], abs(bank["signed_amount_decimal"]), bank["currency"], transaction_id, user["id"])).fetchone()
    return {"id": str(row["id"]), "settlementRef": row["settlement_ref"], "direction": row["direction"], "amount": str(row["amount"]), "currency": row["currency"], "status": row["status"], "idempotent": False}

def get_trial_balance(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("SELECT * FROM internal_trial_balance WHERE tenant_id=%s ORDER BY code", (user["tenant_id"],)).fetchall()
    return [{"accountId": str(r["account_id"]), "code": r["code"], "name": r["name"], "accountType": r["account_type"], "totalDebit": str(r["total_debit"]), "totalCredit": str(r["total_credit"]), "netBalance": str(r["net_balance"])} for r in rows]

def get_profit_and_loss_dashboard(request: Request, fromDate: date, toDate: date, basis: str = "accrual", granularity: str = "month"):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if toDate < fromDate:
        raise HTTPException(422, "The report end date must be on or after the start date.")
    if basis not in {"accrual", "cash"}:
        raise HTTPException(422, "Basis must be accrual or cash.")
    if granularity not in {"day", "week", "month"}:
        raise HTTPException(422, "Granularity must be day, week, or month.")

    if basis == "accrual":
        pnl_sql = """SELECT date_trunc(%s,j.journal_date)::date AS period,
            COALESCE(sr.currency,settlement.currency,typed.currency,'UNSPECIFIED') AS currency,
            a.account_type,a.code AS account_code,a.name AS account_name,
            SUM(CASE WHEN a.account_type='income' THEN l.credit-l.debit ELSE l.debit-l.credit END) AS amount
            FROM journal_headers j
            JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id
            JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id
            LEFT JOIN journal_headers original ON original.id=j.reversed_journal_id AND original.tenant_id=j.tenant_id
            LEFT JOIN LATERAL (
                SELECT record.currency
                FROM standardised_records record
                WHERE record.tenant_id=j.tenant_id
                    AND record.document_id=CASE WHEN j.source_type='reversal' THEN original.source_id ELSE j.source_id END
                    AND COALESCE(original.source_type,j.source_type) IN ('document','adjustment')
                    AND COALESCE(original.idempotency_key,j.idempotency_key) IN (
                        'supplier-bill:'||record.id::text||':revision:'||record.revision_number::text,
                        'customer-invoice:'||record.id::text||':revision:'||record.revision_number::text,
                        'adjustment:credit_note:'||record.id::text||':revision:'||record.revision_number::text,
                        'adjustment:debit_note:'||record.id::text||':revision:'||record.revision_number::text)
                LIMIT 1
            ) sr ON true
            LEFT JOIN settlements settlement ON settlement.tenant_id=j.tenant_id
                AND settlement.id=CASE WHEN j.source_type='reversal' AND original.source_type='settlement'
                    THEN original.source_id WHEN j.source_type='settlement' THEN j.source_id END
            -- Plan L4: a typed sales/purchase document (or its reversal) reports in the document's currency.
            LEFT JOIN erp_document_headers typed ON typed.tenant_id=j.tenant_id
                AND typed.id=CASE WHEN j.source_type='reversal' AND original.source_type='erp_document'
                    THEN original.source_id WHEN j.source_type='erp_document' THEN j.source_id END
            WHERE j.tenant_id=%s AND j.status IN ('posted','reversed')
                AND j.journal_date BETWEEN %s AND %s AND a.account_type IN ('income','expense')
                AND COALESCE(original.idempotency_key,j.idempotency_key) NOT LIKE 'opening-balances:%%'
            GROUP BY period,COALESCE(sr.currency,settlement.currency,typed.currency,'UNSPECIFIED'),a.account_type,a.code,a.name
            ORDER BY period,COALESCE(sr.currency,settlement.currency,typed.currency,'UNSPECIFIED'),a.account_type,a.code"""
        pnl_params = (granularity, user["tenant_id"], fromDate, toDate)
    else:
        pnl_sql = """WITH active_allocations AS (
                SELECT a.id,a.settlement_id,a.record_id,a.amount
                FROM settlement_allocations a
                WHERE a.tenant_id=%s AND a.status='active'
                    AND NOT EXISTS (SELECT 1 FROM settlement_allocations reversal
                        WHERE reversal.tenant_id=a.tenant_id AND reversal.reverses_allocation_id=a.id)
            ),
            allocation_rows AS (
                SELECT date_trunc(%s,COALESCE(bank.posted_date,s.settlement_date))::date AS period,s.currency,
                    CASE WHEN r.category='accounts_receivable' THEN 'income' ELSE 'expense' END AS account_type,
                    CASE WHEN r.category='accounts_receivable' THEN 'ALLOC-AR' ELSE 'ALLOC-AP' END AS account_code,
                    CASE WHEN r.category='accounts_receivable' THEN 'Allocated customer receipts' ELSE 'Allocated supplier payments' END AS account_name,
                    SUM(a.amount) AS amount
                FROM active_allocations a
                JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=%s AND s.status='recorded'
                JOIN standardised_records r ON r.id=a.record_id AND r.tenant_id=s.tenant_id
                -- A bank payment is recognised on its bank posting date, the same date coded_rows uses.
                LEFT JOIN bank_transactions bank ON s.source_type='bank_import' AND bank.id=s.source_id AND bank.tenant_id=s.tenant_id
                WHERE r.is_current AND r.validation_status='passed' AND COALESCE(bank.posted_date,s.settlement_date) BETWEEN %s AND %s
                    AND ((r.category='accounts_receivable' AND s.direction='money_in')
                        OR (r.category='accounts_payable' AND s.direction='money_out'))
                GROUP BY period,s.currency,account_type,account_code,account_name
            ),
            bank_allocated AS (
                -- Only allocations counted in allocation_rows (same record and direction rules) reduce the
                -- coded bank amount; anything else would make a coded refund vanish from the report.
                SELECT s.source_id AS bank_transaction_id,SUM(a.amount) AS allocated
                FROM active_allocations a
                JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=%s
                    AND s.source_type='bank_import' AND s.status='recorded'
                JOIN standardised_records r ON r.id=a.record_id AND r.tenant_id=s.tenant_id
                LEFT JOIN bank_transactions bank ON bank.id=s.source_id AND bank.tenant_id=s.tenant_id
                WHERE r.is_current AND r.validation_status='passed' AND COALESCE(bank.posted_date,s.settlement_date) BETWEEN %s AND %s
                    AND ((r.category='accounts_receivable' AND s.direction='money_in')
                        OR (r.category='accounts_payable' AND s.direction='money_out'))
                GROUP BY s.source_id
            ),
            latest_coding AS (
                SELECT DISTINCT ON (d.bank_transaction_id) d.bank_transaction_id,d.account_id,d.decision
                FROM bank_coding_decisions d
                WHERE d.tenant_id=%s
                ORDER BY d.bank_transaction_id,d.created_at DESC,d.id DESC
            ),
            coded_rows AS (
                SELECT date_trunc(%s,b.posted_date)::date AS period,COALESCE(b.currency,'UNSPECIFIED') AS currency,
                    CASE WHEN account.account_class='revenue' THEN 'income' ELSE 'expense' END AS account_type,
                    account.code AS account_code,account.name AS account_name,
                    SUM((CASE WHEN account.account_class='revenue' AND b.direction='money_out' THEN -1
                        WHEN account.account_class IN ('cogs','expense') AND b.direction='money_in' THEN -1
                        ELSE 1 END) *
                        GREATEST(ABS(b.signed_amount_decimal)-COALESCE(allocated.allocated,0),0)) AS amount
                FROM bank_transactions b
                JOIN latest_coding coding ON coding.bank_transaction_id=b.id AND coding.decision<>'cleared'
                JOIN coa_accounts account ON account.id=coding.account_id AND account.tenant_id=b.tenant_id
                LEFT JOIN bank_allocated allocated ON allocated.bank_transaction_id=b.id
                WHERE b.tenant_id=%s AND b.status<>'ignored' AND b.posted_date BETWEEN %s AND %s
                    AND account.account_class IN ('revenue','cogs','expense')
                    AND GREATEST(ABS(b.signed_amount_decimal)-COALESCE(allocated.allocated,0),0)>0
                GROUP BY period,COALESCE(b.currency,'UNSPECIFIED'),account_type,account.code,account.name
            ),
            typed_rows AS (
                -- Plan L4: payments allocated to typed invoices/bills (credit notes are not cash).
                SELECT date_trunc(%s,p.document_date)::date AS period,p.currency,
                    CASE WHEN p.document_type='payment' THEN 'income' ELSE 'expense' END AS account_type,
                    CASE WHEN p.document_type='payment' THEN 'ALLOC-AR' ELSE 'ALLOC-AP' END AS account_code,
                    CASE WHEN p.document_type='payment' THEN 'Allocated customer receipts' ELSE 'Allocated supplier payments' END AS account_name,
                    SUM(a.amount) AS amount
                FROM erp_document_allocations a
                JOIN erp_document_headers p ON p.id=a.source_document_id AND p.tenant_id=a.tenant_id
                WHERE a.tenant_id=%s AND a.status='active' AND p.status='ready'
                    AND p.document_type IN ('payment','purchase-payment') AND p.document_date BETWEEN %s AND %s
                GROUP BY period,p.currency,account_type,account_code,account_name
            )
            SELECT period,currency,account_type,account_code,account_name,amount FROM allocation_rows
            UNION ALL
            SELECT period,currency,account_type,account_code,account_name,amount FROM typed_rows
            UNION ALL
            SELECT period,currency,account_type,account_code,account_name,amount FROM coded_rows
            ORDER BY period,currency,account_type,account_code"""
        pnl_params = (user["tenant_id"], granularity, user["tenant_id"], fromDate, toDate,
            user["tenant_id"], fromDate, toDate, user["tenant_id"], granularity, user["tenant_id"], fromDate, toDate,
            granularity, user["tenant_id"], fromDate, toDate)

    from services.routes.home.lanes import OPEN_ITEM_KINDS, company_today
    with connect() as db:
        rows = db.execute(pnl_sql, pnl_params).fetchall()
        today = company_today(db, user["tenant_id"])
        aging_rows = db.execute("""WITH active_allocations AS (
                SELECT a.id,a.record_id,a.amount
                FROM settlement_allocations a
                JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id AND s.status='recorded'
                WHERE a.tenant_id=%s AND a.status='active'
                    AND NOT EXISTS (SELECT 1 FROM settlement_allocations reversal
                        WHERE reversal.tenant_id=a.tenant_id AND reversal.reverses_allocation_id=a.id)
            ), allocated AS (
                SELECT record_id,SUM(amount) AS amount FROM active_allocations GROUP BY record_id
            ), outstanding AS (
                SELECT r.id,r.category,r.currency,r.due_date,
                    GREATEST(COALESCE(r.amount_due,r.total_amount)-COALESCE(allocated.amount,0),0) AS balance
                FROM standardised_records r
                JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
                LEFT JOIN allocated ON allocated.record_id=r.id
                WHERE r.tenant_id=%s AND r.is_current AND r.validation_status='passed'
                    -- Open items only (as on Today): receipts are already paid, notes adjust another document.
                    AND ((r.category='accounts_payable' AND d.kind = ANY(%s))
                        OR (r.category='accounts_receivable' AND d.kind = ANY(%s)))
                    AND COALESCE(r.amount_due,r.total_amount)>0
                UNION ALL
                -- Plan L4: typed invoices and bills that are ready (posted), less payments and credit notes allocated to them.
                SELECT h.id,CASE h.document_type WHEN 'invoice' THEN 'accounts_receivable' ELSE 'accounts_payable' END,
                    h.currency,h.due_date,
                    GREATEST(h.total_amount-COALESCE((SELECT SUM(a.amount) FROM erp_document_allocations a
                        WHERE a.tenant_id=h.tenant_id AND a.target_document_id=h.id AND a.status='active'),0),0)
                FROM erp_document_headers h
                WHERE h.tenant_id=%s AND h.status='ready' AND h.document_type IN ('invoice','bill') AND h.total_amount>0
            )
            SELECT category,currency,
                CASE WHEN due_date IS NULL THEN 'no_due_date'
                    WHEN due_date >= %s::date THEN 'not_due'
                    WHEN due_date >= %s::date-30 THEN '1_30_days'
                    WHEN due_date >= %s::date-60 THEN '31_60_days'
                    WHEN due_date >= %s::date-90 THEN '61_90_days'
                    ELSE 'over_90_days' END AS age_bucket,
                SUM(balance) AS amount,COUNT(*) FILTER (WHERE balance>0) AS item_count
            FROM outstanding WHERE balance>0
            GROUP BY category,currency,age_bucket ORDER BY category,currency,age_bucket""",
            (user["tenant_id"], user["tenant_id"], list(OPEN_ITEM_KINDS["accounts_payable"]),
             list(OPEN_ITEM_KINDS["accounts_receivable"]), user["tenant_id"], today, today, today, today)).fetchall()

    period_totals = {}
    currency_totals = {}
    account_totals = {}
    for row in rows:
        currency = row["currency"]
        account_type = row["account_type"]
        amount = Decimal(row["amount"] or 0)
        period_key = (str(row["period"]), currency)
        period_total = period_totals.setdefault(period_key, {"period": str(row["period"]), "currency": currency, "income": Decimal(0), "expense": Decimal(0)})
        currency_total = currency_totals.setdefault(currency, {"currency": currency, "income": Decimal(0), "expense": Decimal(0)})
        account_key = (currency, account_type, row["account_code"], row["account_name"])
        account_total = account_totals.setdefault(account_key, {"currency": currency, "accountType": account_type, "accountCode": row["account_code"], "accountName": row["account_name"], "amount": Decimal(0)})
        period_total[account_type] += amount
        currency_total[account_type] += amount
        account_total["amount"] += amount

    def amount_text(value):
        return format(value.quantize(Decimal("0.01")), "f")

    periods = [{"period": row["period"], "currency": row["currency"], "income": amount_text(row["income"]),
        "expenses": amount_text(row["expense"]), "netProfit": amount_text(row["income"]-row["expense"])}
        for _, row in sorted(period_totals.items())]
    totals = [{"currency": row["currency"], "income": amount_text(row["income"]),
        "expenses": amount_text(row["expense"]), "netProfit": amount_text(row["income"]-row["expense"])}
        for _, row in sorted(currency_totals.items())]
    accounts = [{"currency": row["currency"], "accountType": row["accountType"], "accountCode": row["accountCode"],
        "accountName": row["accountName"], "amount": amount_text(row["amount"])}
        for _, row in sorted(account_totals.items())]
    aging = [{"category": row["category"], "currency": row["currency"], "bucket": row["age_bucket"],
        "amount": amount_text(Decimal(row["amount"] or 0)), "itemCount": int(row["item_count"])}
        for row in aging_rows]
    return {"basis": basis, "fromDate": fromDate.isoformat(), "toDate": toDate.isoformat(),
        "granularity": granularity, "periods": periods, "totals": totals, "accounts": accounts, "aging": aging,
        "cashBasisNote": "This cash estimate uses allocated settlements and classified bank transactions. Allocations appear as gross amounts." if basis == "cash" else None,
        "accrualNote": "Accrual results use posted journals and their reversals. Opening balances are excluded." if basis == "accrual" else None}

def list_ledger_journals(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("""SELECT j.id,j.period_id,j.source_type,j.source_id,j.status,j.journal_date,j.description,j.idempotency_key,j.created_at,
            COALESCE(SUM(l.debit),0) total_debit, COALESCE(SUM(l.credit),0) total_credit
            FROM journal_headers j LEFT JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id
            WHERE j.tenant_id=%s GROUP BY j.id ORDER BY j.created_at DESC""", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "periodId": str(r["period_id"]), "sourceType": r["source_type"], "sourceId": str(r["source_id"]) if r["source_id"] else None, "status": r["status"], "date": str(r["journal_date"]), "description": r["description"], "totalDebit": str(r["total_debit"]), "totalCredit": str(r["total_credit"]), "idempotencyKey": r["idempotency_key"]} for r in rows]

def list_allocations(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("""SELECT a.id,a.settlement_id,a.record_id,a.amount,a.status,a.created_at,a.reversed_at,a.reversal_reason,
            s.settlement_ref,s.amount settlement_amount,s.currency,s.direction,
            COALESCE((SELECT SUM(x.amount) FROM settlement_allocations x WHERE x.settlement_id=a.settlement_id AND x.status='active'
                AND NOT EXISTS (SELECT 1 FROM settlement_allocations undo WHERE undo.tenant_id=x.tenant_id AND undo.reverses_allocation_id=x.id)),0) settlement_allocated
            FROM settlement_allocations a JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id
            WHERE a.tenant_id=%s ORDER BY a.created_at DESC""", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "settlementId": str(r["settlement_id"]), "settlementRef": r["settlement_ref"], "recordId": str(r["record_id"]), "amount": str(r["amount"]), "currency": r["currency"], "direction": r["direction"], "status": r["status"], "unmatchedSettlementAmount": str(r["settlement_amount"]-r["settlement_allocated"]), "createdAt": str(r["created_at"]), "reversedAt": str(r["reversed_at"]) if r["reversed_at"] else None, "reversalReason": r["reversal_reason"]} for r in rows]

def list_settlements(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        # An undone allocation (an active row with a reversal row) no longer counts as allocated.
        rows = db.execute("""SELECT s.*, COALESCE(SUM(a.amount) FILTER (WHERE a.status='active' AND NOT EXISTS (
                SELECT 1 FROM settlement_allocations undo WHERE undo.tenant_id=a.tenant_id AND undo.reverses_allocation_id=a.id)),0) allocated
            FROM settlements s LEFT JOIN settlement_allocations a ON a.settlement_id=s.id AND a.tenant_id=s.tenant_id
            WHERE s.tenant_id=%s GROUP BY s.id ORDER BY s.settlement_date DESC, s.created_at DESC""", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "settlementRef": r["settlement_ref"], "date": str(r["settlement_date"]), "direction": r["direction"], "amount": str(r["amount"]), "allocated": str(r["allocated"]), "unmatchedAmount": str(r["amount"]-r["allocated"]), "currency": r["currency"], "status": r["status"]} for r in rows]

def create_allocation(data: AllocationInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    settlement_id = _uuid_or_422(data.settlementId, "settlement id")
    record_id = _uuid_or_422(data.recordId, "record id")
    try:
        with connect() as db:
            _refuse_document_settlement(db, user["tenant_id"], settlement_id)
            row = db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by)
                VALUES (%s,%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], settlement_id, record_id, data.amount, user["id"])).fetchone()
    except RaiseException as error:
        raise HTTPException(409, str(error).split('\n')[0])
    return {"id": str(row["id"]), "settlementId": str(row["settlement_id"]), "recordId": str(row["record_id"]), "amount": str(row["amount"]), "status": row["status"]}

def reverse_allocation(allocation_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    allocation_id = _uuid_or_422(allocation_id, "allocation id")
    with connect() as db:
        original = db.execute("""SELECT a.* FROM settlement_allocations a
            JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id
            WHERE a.id=%s AND a.tenant_id=%s AND a.status='active' FOR UPDATE OF s""",
            (allocation_id, user["tenant_id"])).fetchone()
        if not original: raise HTTPException(404, "Active allocation not found.")
        _refuse_document_settlement(db, user["tenant_id"], original["settlement_id"])
        require_no_posted_settlement_journal(db, user["tenant_id"], original["settlement_id"])
        row = db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,status,created_by,reversed_at,reversal_reason,reverses_allocation_id)
            VALUES (%s,%s,%s,%s,'reversed',%s,now(),'Allocation undone',%s) RETURNING *""", (user["tenant_id"], original["settlement_id"], original["record_id"], original["amount"], user["id"], original["id"])).fetchone()
    return {"id": str(row["id"]), "reversedAllocationId": allocation_id, "status": "reversed"}


def _refuse_document_settlement(db, tenant_id, settlement_id):
    """Plan L4: a settlement made by a typed payment is changed only by voiding that payment."""
    owner = db.execute("SELECT document_number FROM erp_document_headers WHERE tenant_id=%s AND settlement_id=%s",
                       (tenant_id, settlement_id)).fetchone()
    if owner:
        raise HTTPException(409, f"This settlement belongs to payment {owner['document_number']}; change it there.")


def require_no_posted_settlement_journal(db, tenant_id, settlement_id):
    journal = db.execute("""SELECT id FROM journal_headers
        WHERE tenant_id=%s AND source_type='settlement' AND source_id=%s AND status='posted'""",
        (tenant_id, settlement_id)).fetchone()
    if journal:
        raise HTTPException(409, "Reverse the posted settlement journal before undoing the allocation.")

def list_chart_of_accounts(request: Request, includeInactive: bool = False):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("""SELECT * FROM coa_accounts
            WHERE tenant_id=%s AND (%s OR is_active) ORDER BY code""",
            (user["tenant_id"], includeInactive)).fetchall()
    return [_account_dto(row) for row in rows]

def create_chart_account(data: AccountInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    parent_id = _uuid_or_422(data.parentId, "parent account id") if data.parentId else None
    with connect() as db:
        row = db.execute("""INSERT INTO coa_accounts(tenant_id,code,name,account_class,parent_id,created_by)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            (user["tenant_id"], data.code.strip(), data.name.strip(), data.accountClass, parent_id, user["id"])).fetchone()
    return _account_dto(row)

def update_chart_account(account_id: str, data: AccountInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    account_id = _uuid_or_422(account_id, "account id")
    parent_id = _uuid_or_422(data.parentId, "parent account id") if data.parentId else None
    with connect() as db:
        row = db.execute("""UPDATE coa_accounts SET code=%s,name=%s,account_class=%s,parent_id=%s
            WHERE id=%s AND tenant_id=%s RETURNING *""",
            (data.code.strip(), data.name.strip(), data.accountClass, parent_id, account_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Account not found.")
    return _account_dto(row)

def deactivate_chart_account(account_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    account_id = _uuid_or_422(account_id, "account id")
    with connect() as db:
        row = db.execute("UPDATE coa_accounts SET is_active=FALSE WHERE id=%s AND tenant_id=%s RETURNING id", (account_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Account not found.")
    return {"id": str(row["id"]), "isActive": False}

def list_cost_centres(request: Request, includeInactive: bool = False):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("SELECT * FROM cost_centres WHERE tenant_id=%s AND (%s OR is_active) ORDER BY code", (user["tenant_id"], includeInactive)).fetchall()
    return [_cost_centre_dto(row) for row in rows]

def create_cost_centre(data: CostCentreInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("""INSERT INTO cost_centres(tenant_id,code,name,created_by)
            VALUES (%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], data.code.strip(), data.name.strip(), user["id"])).fetchone()
    return _cost_centre_dto(row)

def update_cost_centre(cost_centre_id: str, data: CostCentreInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    cost_centre_id = _uuid_or_422(cost_centre_id, "cost centre id")
    with connect() as db:
        row = db.execute("UPDATE cost_centres SET code=%s,name=%s WHERE id=%s AND tenant_id=%s RETURNING *", (data.code.strip(), data.name.strip(), cost_centre_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Cost centre not found.")
    return _cost_centre_dto(row)

def deactivate_cost_centre(cost_centre_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    cost_centre_id = _uuid_or_422(cost_centre_id, "cost centre id")
    with connect() as db:
        row = db.execute("UPDATE cost_centres SET is_active=FALSE WHERE id=%s AND tenant_id=%s RETURNING id", (cost_centre_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Cost centre not found.")
    return {"id": str(row["id"]), "isActive": False}
"""Accounting API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/ledger/accounts', list_ledger_accounts, methods=['GET'])
    router.add_api_route('/api/ledger/accounts', create_ledger_account, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/periods', list_accounting_periods, methods=['GET'])
    router.add_api_route('/api/ledger/periods', create_accounting_period, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/journals', post_journal, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/journals/from-supplier-bill/{record_id}', post_supplier_bill, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/journals/from-customer-invoice/{record_id}', post_customer_invoice, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/journals/from-adjustment/{record_id}', post_document_adjustment, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/journals/{journal_id}/reverse', reverse_journal, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/settlements', list_settlements, methods=['GET'])
    router.add_api_route('/api/ledger/settlements', create_settlement, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/settlements/from-bank/{transaction_id}', create_settlement_from_bank, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/allocations', create_allocation, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/allocations/{allocation_id}/reverse', reverse_allocation, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/trial-balance', get_trial_balance, methods=['GET'])
    router.add_api_route('/api/ledger/journals/from-supplier-payment/{transaction_id}', post_supplier_payment, methods=['POST'], status_code=201)
    router.add_api_route('/api/ledger/profit-and-loss', get_profit_and_loss_dashboard, methods=['GET'])
    router.add_api_route('/api/ledger/journals', list_ledger_journals, methods=['GET'])
    router.add_api_route('/api/ledger/allocations', list_allocations, methods=['GET'])
    router.add_api_route('/api/chart-of-accounts', list_chart_of_accounts, methods=['GET'])
    router.add_api_route('/api/coa/accounts', list_chart_of_accounts, methods=['GET'])
    router.add_api_route('/api/chart-of-accounts', create_chart_account, methods=['POST'], status_code=201)
    router.add_api_route('/api/chart-of-accounts/{account_id}', update_chart_account, methods=['PATCH'])
    router.add_api_route('/api/chart-of-accounts/{account_id}', deactivate_chart_account, methods=['DELETE'])
    router.add_api_route('/api/cost-centres', list_cost_centres, methods=['GET'])
    router.add_api_route('/api/cost-centres', create_cost_centre, methods=['POST'], status_code=201)
    router.add_api_route('/api/cost-centres/{cost_centre_id}', update_cost_centre, methods=['PATCH'])
    router.add_api_route('/api/cost-centres/{cost_centre_id}', deactivate_cost_centre, methods=['DELETE'])
