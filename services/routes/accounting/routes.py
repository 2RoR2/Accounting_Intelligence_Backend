from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto
from decimal import Decimal
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
    debit: Decimal = Decimal("0.00")
    credit: Decimal = Decimal("0.00")
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
    amount: Decimal = Field(gt=0)
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
    debits = sum((line.debit for line in data.lines), Decimal("0"))
    credits = sum((line.credit for line in data.lines), Decimal("0"))
    if debits <= 0 or debits != credits:
        raise HTTPException(422, "Journal must have equal non-zero debit and credit totals.")
    period_id = _uuid_or_422(data.periodId, "period id")
    source_id = _uuid_or_422(data.sourceId, "source id") if data.sourceId else None
    with connect() as db:
        existing = db.execute("SELECT id,status FROM journal_headers WHERE tenant_id=%s AND idempotency_key=%s", (user["tenant_id"], data.idempotencyKey)).fetchone()
        if existing:
            return {"id": str(existing["id"]), "status": existing["status"], "idempotent": True}
        period = db.execute("SELECT * FROM accounting_periods WHERE id=%s AND tenant_id=%s FOR UPDATE", (period_id, user["tenant_id"])).fetchone()
        if not period or period["status"] != "open" or not (period["starts_on"] <= data.journalDate <= period["ends_on"]):
            raise HTTPException(409, "Journal date must be inside an open accounting period.")
        header = db.execute("""INSERT INTO journal_headers(tenant_id,period_id,idempotency_key,source_type,source_id,journal_date,description,posted_by,status,posted_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'posted',now()) RETURNING id""", (user["tenant_id"], period_id, data.idempotencyKey, data.sourceType, source_id, data.journalDate, data.description, user["id"])).fetchone()
        for number, line in enumerate(data.lines, 1):
            account_id = _uuid_or_422(line.accountId, "account id")
            if (line.debit > 0) == (line.credit > 0):
                raise HTTPException(422, "Each journal line must contain either a debit or a credit.")
            db.execute("""INSERT INTO journal_lines(tenant_id,journal_id,account_id,line_number,description,debit,credit,source_field)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (user["tenant_id"], header["id"], account_id, number, line.description, line.debit, line.credit, line.sourceField))
    return {"id": str(header["id"]), "status": "posted", "idempotent": False}

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
            COALESCE((SELECT SUM(x.amount) FROM settlement_allocations x WHERE x.settlement_id=a.settlement_id AND x.status='active'),0) settlement_allocated
            FROM settlement_allocations a JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id
            WHERE a.tenant_id=%s ORDER BY a.created_at DESC""", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "settlementId": str(r["settlement_id"]), "settlementRef": r["settlement_ref"], "recordId": str(r["record_id"]), "amount": str(r["amount"]), "currency": r["currency"], "direction": r["direction"], "status": r["status"], "unmatchedSettlementAmount": str(r["settlement_amount"]-r["settlement_allocated"]), "createdAt": str(r["created_at"]), "reversedAt": str(r["reversed_at"]) if r["reversed_at"] else None, "reversalReason": r["reversal_reason"]} for r in rows]

def list_settlements(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("""SELECT s.*, COALESCE(SUM(a.amount) FILTER (WHERE a.status='active'),0) allocated
            FROM settlements s LEFT JOIN settlement_allocations a ON a.settlement_id=s.id AND a.tenant_id=s.tenant_id
            WHERE s.tenant_id=%s GROUP BY s.id ORDER BY s.settlement_date DESC, s.created_at DESC""", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "settlementRef": r["settlement_ref"], "date": str(r["settlement_date"]), "direction": r["direction"], "amount": str(r["amount"]), "allocated": str(r["allocated"]), "unmatchedAmount": str(r["amount"]-r["allocated"]), "currency": r["currency"], "status": r["status"]} for r in rows]

def create_allocation(data: AllocationInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    settlement_id = _uuid_or_422(data.settlementId, "settlement id")
    record_id = _uuid_or_422(data.recordId, "record id")
    try:
        with connect() as db:
            row = db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by)
                VALUES (%s,%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], settlement_id, record_id, data.amount, user["id"])).fetchone()
    except RaiseException as error:
        raise HTTPException(409, str(error).split('\n')[0])
    return {"id": str(row["id"]), "settlementId": str(row["settlement_id"]), "recordId": str(row["record_id"]), "amount": str(row["amount"]), "status": row["status"]}

def reverse_allocation(allocation_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    allocation_id = _uuid_or_422(allocation_id, "allocation id")
    with connect() as db:
        original = db.execute("SELECT * FROM settlement_allocations WHERE id=%s AND tenant_id=%s AND status='active' FOR UPDATE", (allocation_id, user["tenant_id"])).fetchone()
        if not original: raise HTTPException(404, "Active allocation not found.")
        row = db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,status,created_by,reversed_at,reversal_reason,reverses_allocation_id)
            VALUES (%s,%s,%s,%s,'reversed',%s,now(),'Allocation undone',%s) RETURNING *""", (user["tenant_id"], original["settlement_id"], original["record_id"], original["amount"], user["id"], original["id"])).fetchone()
    return {"id": str(row["id"]), "reversedAllocationId": allocation_id, "status": "reversed"}

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
