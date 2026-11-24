from __future__ import annotations
from decimal import Decimal
from services.routes.accounting.routes import (
    bank_payment_preview,
    bank_payment_state,
    get_or_create_bank_settlement,
    mark_bank_line_matched,
    post_allocated_bank_payment,
    record_settlement_allocation,
    require_no_posted_settlement_journal,
    settlement_allocated,
    unposted_bank_allocations,
)
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto
import hashlib
import json
import re
from pathlib import Path
from fastapi.concurrency import run_in_threadpool
from psycopg.errors import RaiseException, UniqueViolation
from pydantic import BaseModel, Field, ValidationError
from services.bank.statement_parser import inspect_statement, parse_bank_statement
from services.routes.erp.common import money
from validation.matching.bank_reconciliation import find_bank_match_candidates, direction_matches
from validation.transactions.transaction_anomaly import detect_transaction_anomalies


class BankExpenseInput(BaseModel):
    ledgerAccountId: str
    bankAccountId: str | None = None
    description: str | None = Field(default=None, max_length=4000)


class BankPaymentInput(BaseModel):
    journalDate: date | None = None


class BankMatchDecisionInput(ReconciliationDecisionInput):
    # Confirm and post in one step (local admins only): the match and its payment journal commit together.
    postPayment: bool = False
    journalDate: date | None = None


class BankMatchAllocationInput(BaseModel):
    recordId: str
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)


class BankMatchManyInput(BaseModel):
    expectedAllocated: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    allocations: list[BankMatchAllocationInput] = Field(min_length=1, max_length=50)


class BankReconcileDocumentInput(BaseModel):
    documentId: str


class BankReconcileUndoInput(BaseModel):
    reason: str = Field(default="Reconciliation undone by the reviewer", min_length=1, max_length=500)

BANK_ANOMALY_DETECTOR_VERSION = "modified-z-3.5"

def _ensure_reconciliation_period_tables(db):
    """Compatibility bootstrap for existing PoC databases before migrations are installed."""
    db.execute("""CREATE TABLE IF NOT EXISTS bank_reconciliation_periods (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        bank_account_ref TEXT NOT NULL, starts_on DATE NOT NULL, ends_on DATE NOT NULL,
        opening_balance NUMERIC(18,2) NOT NULL, closing_balance NUMERIC(18,2) NOT NULL, book_balance NUMERIC(18,2) NOT NULL,
        statement_adjustment NUMERIC(18,2) NOT NULL DEFAULT 0, book_adjustment NUMERIC(18,2) NOT NULL DEFAULT 0,
        adjustment_evidence TEXT, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','completed')),
        signed_off_by UUID REFERENCES users(id) ON DELETE RESTRICT, signed_off_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (starts_on <= ends_on), UNIQUE (tenant_id, bank_account_ref, starts_on, ends_on))""")
    db.execute("""CREATE TABLE IF NOT EXISTS bank_reconciliation_reopens (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        period_id UUID NOT NULL REFERENCES bank_reconciliation_periods(id) ON DELETE RESTRICT,
        reason TEXT NOT NULL, reopened_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT, reopened_at TIMESTAMPTZ NOT NULL DEFAULT now())""")

def _ensure_bank_anomaly_table(db):
    # Dashboard loads Money In and Money Out concurrently. Serialize this
    # compatibility bootstrap so two first requests cannot race in PostgreSQL
    # while creating the same table type.
    db.execute("SELECT pg_advisory_xact_lock(hashtext('accordai.bank_transaction_anomalies'))")
    # PostgreSQL can still report a duplicate type during compatibility
    # bootstrap when another worker began the same CREATE before the lock was
    # acquired. Keep the CREATE inside a savepoint so that harmless race does
    # not abort the request transaction.
    db.execute("SAVEPOINT ensure_bank_anomaly_table")
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS bank_transaction_anomalies (
        bank_transaction_id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        import_id UUID NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('anomaly', 'normal', 'insufficient_baseline')),
        is_anomaly BOOLEAN NOT NULL,
        label TEXT NOT NULL,
        reason TEXT NOT NULL,
        amount NUMERIC(14,2) NOT NULL,
        currency CHAR(3) NOT NULL,
        baseline_median NUMERIC(14,2),
        upper_bound NUMERIC(14,2),
        baseline_count INTEGER NOT NULL DEFAULT 0 CHECK (baseline_count >= 0),
        anomaly_score NUMERIC(18,6),
        detector_version TEXT NOT NULL DEFAULT 'modified-z-3.5',
        calculated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (bank_transaction_id, tenant_id),
        FOREIGN KEY (bank_transaction_id, tenant_id) REFERENCES bank_transactions(id, tenant_id) ON DELETE RESTRICT,
        FOREIGN KEY (import_id, tenant_id) REFERENCES bank_imports(id, tenant_id) ON DELETE RESTRICT
    )""")
    except UniqueViolation:
        db.execute("ROLLBACK TO SAVEPOINT ensure_bank_anomaly_table")
    finally:
        db.execute("RELEASE SAVEPOINT ensure_bank_anomaly_table")

def _period_result(db, period):
    rows = db.execute("""SELECT t.*, EXISTS(SELECT 1 FROM reconciliation_candidates c WHERE c.bank_transaction_id=t.id AND c.status='confirmed') AS matched,
        EXISTS(SELECT 1 FROM bank_coding_decisions cd WHERE cd.bank_transaction_id=t.id AND cd.decision <> 'cleared') AS coded
        FROM bank_transactions t JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
        WHERE t.tenant_id=%s AND i.status='confirmed' AND i.source_bank_account_ref=%s AND t.posted_date BETWEEN %s AND %s
        ORDER BY t.posted_date,t.source_row""", (period['tenant_id'], period['bank_account_ref'], period['starts_on'], period['ends_on'])).fetchall()
    money_in = sum((r['credit_decimal'] or Decimal('0') for r in rows), Decimal('0'))
    money_out = sum((r['debit_decimal'] or Decimal('0') for r in rows), Decimal('0'))
    expected = period['opening_balance'] + money_in - money_out
    statement_difference = period['closing_balance'] - expected
    adjusted_statement = period['closing_balance'] + period['statement_adjustment']
    adjusted_book = period['book_balance'] + period['book_adjustment']
    unexplained = adjusted_statement - adjusted_book
    unaccounted = sum(1 for r in rows if r['status'] not in {'ignored','matched'} and not r['matched'] and not r['coded'])
    return {"id": str(period['id']), "bankAccountRef": period['bank_account_ref'], "startsOn": str(period['starts_on']), "endsOn": str(period['ends_on']),
        "openingBalance": str(period['opening_balance']), "closingBalance": str(period['closing_balance']), "bookBalance": str(period['book_balance']),
        "statementAdjustment": str(period['statement_adjustment']), "bookAdjustment": str(period['book_adjustment']), "adjustmentEvidence": period['adjustment_evidence'],
        "moneyIn": str(money_in), "moneyOut": str(money_out), "expectedClosingBalance": str(expected), "statementDifference": str(statement_difference),
        "adjustedStatementBalance": str(adjusted_statement), "adjustedBookBalance": str(adjusted_book), "unexplainedDifference": str(unexplained),
        "unaccountedLines": unaccounted, "status": period['status'], "signedOffBy": str(period['signed_off_by']) if period['signed_off_by'] else None, "signedOffAt": str(period['signed_off_at']) if period['signed_off_at'] else None}

def create_reconciliation_period(data: BankReconciliationPeriodInput, request: Request):
    user = require_workspace_user(request, {'local_admin','accountant'})
    if data.endsOn < data.startsOn: raise HTTPException(422, 'Period end must be on or after the start date.')
    if data.adjustmentEvidence is None and (data.statementAdjustment != 0 or data.bookAdjustment != 0): raise HTTPException(422, 'Adjustment evidence is required for non-zero adjustments.')
    with connect() as db:
        _ensure_reconciliation_period_tables(db)
        try:
            period = db.execute("""INSERT INTO bank_reconciliation_periods(tenant_id,bank_account_ref,starts_on,ends_on,opening_balance,closing_balance,book_balance,statement_adjustment,book_adjustment,adjustment_evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (user['tenant_id'],data.bankAccountRef.strip(),data.startsOn,data.endsOn,data.openingBalance,data.closingBalance,data.bookBalance,data.statementAdjustment,data.bookAdjustment,data.adjustmentEvidence)).fetchone()
        except UniqueViolation: raise HTTPException(409, 'This bank account period already exists.')
        return _period_result(db, period)

def list_reconciliation_periods(request: Request):
    user = require_workspace_user(request, {'local_admin','accountant'})
    with connect() as db:
        _ensure_reconciliation_period_tables(db)
        rows = db.execute('SELECT * FROM bank_reconciliation_periods WHERE tenant_id=%s ORDER BY starts_on DESC', (user['tenant_id'],)).fetchall()
        return [_period_result(db, row) for row in rows]

def complete_reconciliation_period(period_id: str, request: Request):
    user = require_workspace_user(request, {'local_admin','accountant'})
    period_id = _uuid_or_422(period_id, 'period id')
    with connect() as db:
        _ensure_reconciliation_period_tables(db)
        period = db.execute('SELECT * FROM bank_reconciliation_periods WHERE id=%s AND tenant_id=%s FOR UPDATE', (period_id,user['tenant_id'])).fetchone()
        if not period: raise HTTPException(404, 'Reconciliation period not found.')
        result = _period_result(db, period)
        if result['status'] == 'completed': raise HTTPException(409, 'This reconciliation period is already completed.')
        if Decimal(result['statementDifference']) != 0 or Decimal(result['unexplainedDifference']) != 0 or result['unaccountedLines'] != 0:
            raise HTTPException(409, 'Reconciliation cannot be completed until both balance checks pass and every statement line is accounted for.')
        db.execute("UPDATE bank_reconciliation_periods SET status='completed',signed_off_by=%s,signed_off_at=now(),updated_at=now() WHERE id=%s", (user['id'],period_id))
        return {**result, 'status':'completed', 'signedOffBy': str(user['id'])}

def reopen_reconciliation_period(period_id: str, data: BankReconciliationReopenInput, request: Request):
    user = require_workspace_user(request, {'local_admin'})
    period_id = _uuid_or_422(period_id, 'period id')
    with connect() as db:
        _ensure_reconciliation_period_tables(db)
        period = db.execute('SELECT * FROM bank_reconciliation_periods WHERE id=%s AND tenant_id=%s FOR UPDATE', (period_id,user['tenant_id'])).fetchone()
        if not period: raise HTTPException(404, 'Reconciliation period not found.')
        if period['status'] != 'completed': raise HTTPException(409, 'Only a completed period can be reopened.')
        db.execute('INSERT INTO bank_reconciliation_reopens(tenant_id,period_id,reason,reopened_by) VALUES (%s,%s,%s,%s)', (user['tenant_id'],period_id,data.reason.strip(),user['id']))
        db.execute("UPDATE bank_reconciliation_periods SET status='open',signed_off_by=NULL,signed_off_at=NULL,updated_at=now() WHERE id=%s", (period_id,))
        return {'id': str(period_id), 'status':'open', 'reopenedBy': str(user['id']), 'reason': data.reason.strip()}

def _calculate_bank_transaction_anomaly(db, row, bank_account_ref):
    """Calculate one amount anomaly using only imports that predate this import.

    Matching and anomaly detection answer different questions. Matching asks whether
    this row can be linked to an accounting record; anomaly detection asks whether
    its amount is unusual compared with the tenant's earlier rows for the same bank
    account, direction, and currency.
    """
    amount = row['signed_amount_decimal']
    direction = row['direction']
    currency = str(row['currency'] or '').strip().upper()
    posted_date = row['posted_date']

    if amount is None or not direction or not currency or not posted_date or not bank_account_ref:
        return None

    references = db.execute("""
        SELECT b.id, abs(b.signed_amount_decimal) AS amount, b.currency
        FROM bank_transactions b
        JOIN bank_imports i ON i.id=b.import_id AND i.tenant_id=b.tenant_id
        WHERE b.tenant_id=%s
          AND i.status='confirmed'
          AND b.status<>'ignored'
          AND b.direction=%s
          AND b.currency=%s
          AND i.source_bank_account_ref=%s
          AND b.posted_date < %s
        ORDER BY b.posted_date, b.source_row
    """, (row['tenant_id'], direction, currency, bank_account_ref, posted_date)).fetchall()

    result = detect_transaction_anomalies(
        [{
            'transaction_id': str(row['id']),
            'amount': abs(amount),
            'currency': currency,
        }],
        reference_transactions=[{
            'transaction_id': str(reference['id']),
            'amount': reference['amount'],
            'currency': reference['currency'],
        } for reference in references],
    )[0]

    direction_label = 'Money In' if direction == 'money_in' else 'Money Out'
    formatted_amount = f"{currency} {Decimal(str(abs(amount))):,.2f}"
    if result['status'] == 'anomaly':
        label = 'Unusually high amount'
        reason = f"{formatted_amount} is above the usual range for {direction_label} transactions."
    elif result['status'] == 'normal':
        label = 'No unusual amount detected'
        reason = f"{formatted_amount} is within the usual range for {direction_label} transactions."
    elif result['baseline_count'] > 0:
        label = 'Baseline dispersion unavailable'
        reason = result['reason']
    else:
        label = 'Insufficient history'
        reason = f"No prior {direction_label} transactions are available to establish a range."

    return {
        'status': result['status'],
        'isAnomaly': result['is_anomaly'],
        'label': label,
        'reason': reason,
        'amount': result['amount'],
        'currency': result['currency'],
        'baselineMedian': result['baseline_median'],
        'upperBound': result['upper_bound'],
        'baselineCount': result['baseline_count'],
        'anomalyScore': result['anomaly_score'],
    }


def _save_bank_transaction_anomaly(db, row, result):
    if not result:
        return None
    db.execute("""INSERT INTO bank_transaction_anomalies
        (bank_transaction_id,tenant_id,import_id,status,is_anomaly,label,reason,amount,currency,
         baseline_median,upper_bound,baseline_count,anomaly_score,detector_version)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (bank_transaction_id,tenant_id) DO UPDATE SET
          status=EXCLUDED.status,is_anomaly=EXCLUDED.is_anomaly,label=EXCLUDED.label,
          reason=EXCLUDED.reason,amount=EXCLUDED.amount,currency=EXCLUDED.currency,
          baseline_median=EXCLUDED.baseline_median,upper_bound=EXCLUDED.upper_bound,
          baseline_count=EXCLUDED.baseline_count,anomaly_score=EXCLUDED.anomaly_score,
          detector_version=EXCLUDED.detector_version,calculated_at=EXCLUDED.calculated_at""",
        (row['id'], row['tenant_id'], row['import_id'], result['status'], result['isAnomaly'],
         result['label'], result['reason'], result['amount'], result['currency'],
         result['baselineMedian'], result['upperBound'], result['baselineCount'], result['anomalyScore'],
         BANK_ANOMALY_DETECTOR_VERSION))
    return result


def _bank_transaction_anomaly(db, row, bank_account_ref):
    """Return the saved, explainable amount anomaly for one bank row.

    New imports are saved during confirmation. The calculate-and-save fallback
    backfills legacy rows once, so list loads never recalculate an existing result.
    """
    saved = db.execute("""SELECT status,is_anomaly,label,reason,amount,currency,
        baseline_median,upper_bound,baseline_count,anomaly_score,detector_version
        FROM bank_transaction_anomalies
        WHERE bank_transaction_id=%s AND tenant_id=%s""", (row['id'], row['tenant_id'])).fetchone()
    if saved and saved['detector_version'] == BANK_ANOMALY_DETECTOR_VERSION:
        return {
            'status': saved['status'], 'isAnomaly': saved['is_anomaly'], 'label': saved['label'],
            'reason': saved['reason'], 'amount': str(saved['amount']), 'currency': saved['currency'],
            'baselineMedian': str(saved['baseline_median']) if saved['baseline_median'] is not None else None,
            'upperBound': str(saved['upper_bound']) if saved['upper_bound'] is not None else None,
            'baselineCount': saved['baseline_count'],
            'anomalyScore': str(saved['anomaly_score']) if saved['anomaly_score'] is not None else None,
        }
    return _save_bank_transaction_anomaly(db, row, _calculate_bank_transaction_anomaly(db, row, bank_account_ref))


BANK_TRANSACTION_READ = """SELECT t.*,i.source_bank_account_ref,s.id AS settlement_id,COALESCE(allocation.allocated,0) AS allocated,
        h.id AS recorded_id,h.document_number AS recorded_number,h.document_type AS recorded_type,
        rec.id AS reconciliation_id,rec.match_label AS reconciliation_label,rec.match_reason AS reconciliation_reason
    FROM bank_transactions t JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
    LEFT JOIN erp_document_headers h ON h.id=t.recorded_document_id AND h.tenant_id=t.tenant_id
        AND h.status<>'void' AND h.document_type IN ('money-in','money-out','payment','refund','purchase-payment','purchase-refund')
    LEFT JOIN bank_document_reconciliations rec ON rec.tenant_id=t.tenant_id AND rec.bank_transaction_id=t.id
        AND rec.undone_at IS NULL AND rec.document_id=t.recorded_document_id
    LEFT JOIN settlements s ON s.tenant_id=t.tenant_id AND s.source_type='bank_import'
        AND s.source_id=t.id AND s.status='recorded'
    LEFT JOIN LATERAL (
        SELECT SUM(a.amount) AS allocated FROM settlement_allocations a
        WHERE a.settlement_id=s.id AND a.tenant_id=s.tenant_id AND a.status='active'
            AND NOT EXISTS (SELECT 1 FROM settlement_allocations undo
                WHERE undo.tenant_id=a.tenant_id AND undo.reverses_allocation_id=a.id)
    ) allocation ON TRUE"""


def _bank_candidates(db, row, records_by_tenant):
    tenant_id = row["tenant_id"]
    can_review = row["status"] in {"imported", "proposed"} and not row["allocated"]
    if can_review and row["posted_date"] and row["signed_amount_decimal"] and row["reference"]:
        if tenant_id not in records_by_tenant:
            by_amount = {}
            for record in _eligible_records(db, tenant_id):
                by_amount.setdefault(record["total_amount"], []).append(record)
            records_by_tenant[tenant_id] = by_amount
        transaction = {
            "amount": abs(row["signed_amount_decimal"]), "signed": row["signed_amount_decimal"],
            "direction": row["direction"], "currency": row["currency"], "reference": row["reference"],
        }
        matches = _matching_records(row["id"], transaction, records_by_tenant[tenant_id].get(transaction["amount"], []))
        if matches:
            current = db.execute("""SELECT status FROM bank_transactions
                WHERE tenant_id=%s AND id=%s FOR NO KEY UPDATE""", (tenant_id, row["id"])).fetchone()
            can_review = current and current["status"] in {"imported", "proposed"}
            if can_review:
                settlement = _existing_bank_settlement(db, row)
                can_review = not settlement or not settlement_allocated(db, tenant_id, settlement["id"])
            if can_review:
                row["status"] = "proposed" if _save_candidates(db, tenant_id, row["id"], matches) else current["status"]
    return db.execute("""SELECT c.id, c.record_id, c.status, c.reason_codes, c.proposed_by,
            c.amount_delta_decimal, c.date_delta_days, c.score_components,
            r.record_code, r.document_id, r.revision_number, r.category,
            COALESCE(r.reference_number,r.invoice_number) AS reference_number, r.invoice_number,
            r.supplier_name, r.buyer_name, r.vendor_id, r.invoice_date, r.currency, r.total_amount,
            d.document_code
        FROM reconciliation_candidates c
        JOIN standardised_records r ON r.id=c.record_id AND r.tenant_id=c.tenant_id
        LEFT JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
        WHERE c.bank_transaction_id=%s AND c.tenant_id=%s
            AND (c.status<>'proposed' OR (%s AND r.is_current AND r.validation_status='passed'))
        ORDER BY c.created_at DESC, c.id DESC""", (row["id"], tenant_id, bool(can_review))).fetchall()


def _bank_transaction_response(db, row, records_by_tenant, user, memory=None):
    coding = db.execute("""SELECT c.account_id, c.cost_centre_id, c.decision, c.reason
        FROM bank_coding_decisions c
        WHERE c.bank_transaction_id=%s AND c.tenant_id=%s
        ORDER BY c.created_at DESC, c.id DESC LIMIT 1""", (row['id'], row['tenant_id'])).fetchone()
    candidates = _bank_candidates(db, row, records_by_tenant)
    open_candidates = [c for c in candidates if c['status'] == 'proposed']
    payers = _record_payers(db, row['tenant_id'], [c['record_id'] for c in open_candidates]) if open_candidates else {}
    result = _bank_transaction_dto(row)
    mapped = []
    for c in candidates:
        payer = payers.get(c['record_id'])
        label, reason = _candidate_explanation(row, c, payer, memory)
        mapped.append({"id": str(c['id']), "recordId": str(c['record_id']),
            "recordCode": c['record_code'], "documentId": str(c['document_id']) if c['document_id'] else None,
            "documentCode": c['document_code'], "revision": c['revision_number'],
            "category": c['category'], "reference": c['reference_number'],
            "party": c['supplier_name'] or c['buyer_name'], "currency": c['currency'],
            "amount": str(c['total_amount']) if c['total_amount'] is not None else None,
            "amountDelta": str(c['amount_delta_decimal']) if c['amount_delta_decimal'] is not None else '0.00',
            "dateDeltaDays": c['date_delta_days'], "reasons": c['reason_codes'] or [],
            "evidence": c['score_components'] or {}, "status": c['status'],
            "matchLabel": label, "matchReason": reason, "settledBy": _payer_dto(payer)})
    confirmed = next((c for c in mapped if c['status'] == 'confirmed'), None)
    proposed = next((c for c in mapped if c['status'] == 'proposed'), None)
    amount = abs(row['signed_amount_decimal'] or Decimal(0))
    allocated = row['allocated']
    payment_posted, can_post = bank_payment_state(db, row["tenant_id"], row["settlement_id"]) if allocated else (False, False)
    result.update({"matchStatus": {"matched": "matched", "proposed": "proposed", "ignored": "ignored"}.get(row['status'], "unmatched"),
        "anomaly": _bank_transaction_anomaly(db, row, row['source_bank_account_ref']),
        "accountId": str(coding['account_id']) if coding and coding['account_id'] else None,
        "costCentreId": str(coding['cost_centre_id']) if coding and coding['cost_centre_id'] else None,
        "codingDecision": coding['decision'] if coding else None,
        "codingReason": coding['reason'] if coding else None,
        "bankAccountRef": row['source_bank_account_ref'],
        "relatedRecordId": confirmed['recordId'] if confirmed else None,
        "relatedRecordCode": confirmed['recordCode'] if confirmed else None,
        "relatedDocumentId": confirmed['documentId'] if confirmed else None,
        "relatedDocumentCode": confirmed['documentCode'] if confirmed else None,
        "amountDelta": confirmed['amountDelta'] if confirmed else None,
        "candidateId": proposed['id'] if proposed else None,
        "expectedRevision": proposed['revision'] if proposed else None,
        "candidates": mapped,
        "allocatedAmount": money(allocated),
        "remainingAmount": money(max(amount - allocated, Decimal(0))),
        "paymentPosted": payment_posted,
        # Matched records whose payment journal is not posted yet, whoever is looking (accountants wait for an admin).
        "paymentPending": bool(can_post) and row["status"] != "ignored" and row["recorded_id"] is None,
        "canPostPayment": can_post and user["role"] == "local_admin" and row["status"] != "ignored"
                          and row["recorded_id"] is None,
        "recordedDocumentId": str(row['recorded_id']) if row['recorded_id'] else None,
        "recordedDocumentNumber": row['recorded_number'],
        "recordedDocumentType": row['recorded_type'],
        # Reconciled against a payment that had already posted the cash: no allocation or journal of its own.
        "reconciliation": {"id": str(row['reconciliation_id']), "documentId": str(row['recorded_id']),
                           "documentNumber": row['recorded_number'], "documentType": row['recorded_type'],
                           "documentLabel": DOCUMENT_LABELS.get(row['recorded_type'], row['recorded_type']),
                           "matchLabel": row['reconciliation_label'], "matchReason": row['reconciliation_reason'],
                           } if row['reconciliation_id'] and row['recorded_id'] else None})
    if row['status'] in {"imported", "needs_review", "proposed"} and 0 < allocated < amount:
        result.update(status="partially_matched", matchStatus="partially_matched")
    return result

def list_bank_transactions(request: Request, direction: str | None = None, status: str | None = None):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_scope(user)
    if direction and direction not in {"money_in", "money_out"}:
        raise HTTPException(422, "direction must be money_in or money_out.")
    if status and status not in {"imported", "needs_review", "proposed", "matched", "ignored", "partially_matched"}:
        raise HTTPException(422, "Invalid bank transaction status.")
    with connect() as db:
        # Ensure the compatibility table once per request, not once per row.
        _ensure_bank_anomaly_table(db)
        # A statement still in preview is not on the Money pages until someone confirms it.
        rows = db.execute(BANK_TRANSACTION_READ + """
            WHERE i.status='confirmed' AND (%s::uuid IS NULL OR t.tenant_id=%s::uuid)
                AND (%s::text IS NULL OR t.direction=%s::text)
                AND (%s::text IS NULL OR t.status=%s::text OR (%s='partially_matched'
                    AND t.status IN ('imported','needs_review','proposed')
                    AND allocation.allocated>0 AND allocation.allocated<ABS(t.signed_amount_decimal)))
            ORDER BY t.posted_date DESC NULLS LAST, t.source_row DESC, t.id""",
            (scope, scope, direction, direction, status, status, status)).fetchall()
        records_by_tenant = {}
        return [_bank_transaction_response(db, row, records_by_tenant, user) for row in rows]

def list_bank_imports(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("""SELECT i.*, count(t.id) AS transaction_count
            FROM bank_imports i LEFT JOIN bank_transactions t ON t.import_id=i.id
            WHERE (%s IS NULL OR i.tenant_id=%s)
            GROUP BY i.id ORDER BY i.imported_at DESC""", (tenant_scope(user), tenant_scope(user))).fetchall()
    return [{"id": str(r["id"]), "sourceBankAccountRef": r["source_bank_account_ref"],
             "sourceHash": r["source_hash"], "mappingVersion": r["mapping_version"],
             "rowCount": r["row_count"], "transactionCount": r["transaction_count"],
             "status": r["status"], "importedAt": str(r["imported_at"])} for r in rows]

MAX_STATEMENT_BYTES = 5 * 1024 * 1024


def _matching_records(transaction_id, row, records):
    transaction = {'transaction_id': str(transaction_id), 'amount': row['amount'], 'signed_amount_decimal': row['signed'],
                   'direction': row['direction'], 'currency': row['currency'], 'reference': row['reference']}
    return [record['record_id'] for record in records if find_bank_match_candidates(record, [transaction])['status'] == 'MATCHED']


def _save_candidates(db, tenant_id, transaction_id, record_ids):
    if not record_ids:
        return 0
    rows = db.execute("""INSERT INTO reconciliation_candidates
        (tenant_id,bank_transaction_id,record_id,reason_codes,proposed_by)
        SELECT r.tenant_id,%s,r.id,%s,'rules' FROM standardised_records r
        WHERE r.tenant_id=%s AND r.id=ANY(%s) AND r.is_current AND r.validation_status='passed'
          AND NOT EXISTS (SELECT 1 FROM reconciliation_candidates c WHERE c.tenant_id=r.tenant_id
            AND c.bank_transaction_id=%s AND c.record_id=r.id)
        ORDER BY r.id FOR SHARE OF r
        ON CONFLICT (bank_transaction_id,record_id) DO NOTHING RETURNING id""",
        (transaction_id, Jsonb(['EXACT_AMOUNT', 'CURRENCY_MATCH', 'REFERENCE_MATCH', 'DIRECTION_MATCH']),
         tenant_id, record_ids, transaction_id)).fetchall()
    return len(rows)


def _propose_candidates(db, tenant_id, transaction_id, row, records):
    """Exact candidates for one imported row. Each still needs a person to confirm it."""
    return _save_candidates(db, tenant_id, transaction_id, _matching_records(transaction_id, row, records))


def _eligible_records(db, tenant_id):
    return db.execute("""SELECT id AS record_id, document_id, category, COALESCE(reference_number, invoice_number) AS reference_number,
            total_amount, currency, revision_number
        FROM standardised_records WHERE tenant_id=%s AND is_current=true
          AND validation_status='passed'""", (tenant_id,)).fetchall()


def _bank_audit(db, user, action, resource, details):
    db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""", (user["tenant_id"], user["id"], user.get("name", "Workspace user"), user["role"],
        action, str(resource), json.dumps(details, default=str)))


def _preview_bank_statement(user, content, filename, account_ref, mapping, date_format, default_currency="MYR"):
    """Rows wait as ignored evidence, unseen on the Money pages, until a person confirms the import."""
    try:
        rows, mapping_version = parse_bank_statement(content, filename, mapping, date_format, user["tenant_id"], account_ref, default_currency)
    except ValueError as error:
        raise HTTPException(422, str(error))
    source_hash = hashlib.sha256(content).hexdigest()
    input_format = Path(filename).suffix.lower().lstrip(".")
    import_id = uuid.uuid4()
    samples, duplicates, needs_review = [], 0, 0
    with connect() as db:
        # An abandoned preview of the same file gives way to this one. A confirmed statement does not.
        stale = db.execute("SELECT id FROM bank_imports WHERE tenant_id=%s AND source_hash=%s AND status='previewed'",
                           (user["tenant_id"], source_hash)).fetchone()
        if stale:
            db.execute("DELETE FROM bank_transactions WHERE import_id=%s AND tenant_id=%s", (stale['id'], user["tenant_id"]))
            db.execute("UPDATE bank_imports SET status='reverted' WHERE id=%s AND tenant_id=%s", (stale['id'], user["tenant_id"]))
        try:
            db.execute("""INSERT INTO bank_imports
                (id,tenant_id,source_bank_account_ref,source_hash,input_format,mapping_version,imported_by,row_count,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,0,'previewed')""",
                (import_id, user["tenant_id"], account_ref, source_hash, input_format, mapping_version, user["id"]))
        except UniqueViolation:
            raise HTTPException(409, "This bank statement has already been imported.")
        count = 0
        for row in rows:
            inserted = db.execute("""INSERT INTO bank_transactions
                (id,tenant_id,import_id,source_row,posted_date,value_date,description,reference,
                 debit_decimal,credit_decimal,signed_amount_decimal,direction,currency,fingerprint,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ignored')
                ON CONFLICT (tenant_id,fingerprint) DO NOTHING RETURNING id""",
                (uuid.uuid4(), user["tenant_id"], import_id, row['source_row'], row['posted_date'], row['value_date'],
                 row['description'] or '(no description)', row['reference'], row['debit_decimal'], row['credit_decimal'],
                 row['signed_amount_decimal'], row['direction'], row['currency'], row['fingerprint'])).fetchone()
            if not inserted:
                duplicates += 1
                continue
            count += 1
            needs_review += row['status'] == 'needs_review'
            if len(samples) < 20:
                samples.append({'sourceRow': row['source_row'], 'postedDate': row['posted_date'].isoformat() if row['posted_date'] else None,
                                'description': row['description'], 'reference': row['reference'],
                                'debit': str(row['debit_decimal']) if row['debit_decimal'] is not None else None,
                                'credit': str(row['credit_decimal']) if row['credit_decimal'] is not None else None,
                                'direction': row['direction'], 'currency': row['currency'], 'status': row['status'],
                                'reviewReasons': row['review_reasons']})
        db.execute("UPDATE bank_imports SET row_count=%s WHERE id=%s AND tenant_id=%s", (count, import_id, user["tenant_id"]))
    return {'importId': str(import_id), 'rowCount': count, 'duplicateRowsSkipped': duplicates, 'needsReviewCount': needs_review,
            'canConfirm': count > 0, 'status': 'previewed', 'sampleRows': samples}


async def create_bank_import(request: Request):
    """A bank statement file starts a preview. Rows sent as JSON are imported directly."""
    user = await run_in_threadpool(require_workspace_user, request, {"local_admin", "accountant"})
    if not user.get("tenant_id"):
        raise HTTPException(403, "A company workspace is required.")
    if request.headers.get('content-type', '').startswith('multipart/form-data'):
        form = await request.form()
        upload = form.get('file')
        account_ref = str(form.get('source_bank_account_ref') or '').strip()
        if upload is None or not hasattr(upload, 'read'):
            raise HTTPException(422, "Choose a CSV, Excel, or PDF bank statement.")
        filename = str(getattr(upload, 'filename', '') or '')
        if Path(filename).suffix.lower() not in {'.csv', '.xlsx', '.pdf'}:
            raise HTTPException(422, "Bank statements must be CSV, Excel (.xlsx), or PDF files.")
        content = await upload.read(MAX_STATEMENT_BYTES + 1)
        if len(content) > MAX_STATEMENT_BYTES:
            raise HTTPException(413, "The statement exceeds the 5 MB limit. Choose a smaller file.")
        if not content:
            raise HTTPException(422, "The statement must be a non-empty CSV, Excel, or PDF file of at most 5 MB.")
        if str(form.get('inspect_only') or '').lower() == 'true':
            try:
                return await run_in_threadpool(inspect_statement, content, filename)
            except ValueError as error:
                raise HTTPException(422, str(error))
        if not account_ref or len(account_ref) > 200:
            raise HTTPException(422, "Give the bank account a label.")
        try:
            mapping = json.loads(str(form.get('mapping') or ''))
        except ValueError:
            raise HTTPException(422, "Statement column mapping must be valid JSON.")
        return await run_in_threadpool(_preview_bank_statement, user, content, filename, account_ref, mapping, str(form.get('date_format') or ''), str(form.get('default_currency') or 'MYR'))
    try:
        data = BankImportInput(**await request.json())
    except ValidationError as error:
        raise HTTPException(422, json.loads(error.json(include_url=False, include_context=False, include_input=False)))
    except ValueError:
        raise HTTPException(422, "Expected a JSON bank import.")
    return await run_in_threadpool(_import_bank_rows, data, user)


def confirm_bank_import(import_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    import_id = _uuid_or_422(import_id, "import id")
    with connect() as db:
        _ensure_bank_anomaly_table(db)
        batch = db.execute("SELECT * FROM bank_imports WHERE id=%s AND tenant_id=%s FOR UPDATE", (import_id, user["tenant_id"])).fetchone()
        if not batch:
            raise HTTPException(404, "Bank import not found.")
        if batch['status'] != 'previewed':
            raise HTTPException(409, "Only a previewed import can be confirmed.")
        rows = db.execute("SELECT * FROM bank_transactions WHERE import_id=%s AND tenant_id=%s ORDER BY source_row", (import_id, user["tenant_id"])).fetchall()
        records = _eligible_records(db, user["tenant_id"])
        proposed = 0
        for row in rows:
            usable = row['direction'] and row['currency'] and row['posted_date'] and row['description'] != '(no description)'
            db.execute("UPDATE bank_transactions SET status=%s WHERE id=%s", ('imported' if usable else 'needs_review', row['id']))
            if usable:
                proposed += _propose_candidates(db, user["tenant_id"], row['id'], {
                    'amount': row['debit_decimal'] if row['direction'] == 'money_out' else row['credit_decimal'],
                    'signed': row['signed_amount_decimal'], 'direction': row['direction'], 'currency': row['currency'],
                    'reference': row['reference']}, records)
        db.execute("UPDATE bank_imports SET status='confirmed' WHERE id=%s AND tenant_id=%s", (import_id, user["tenant_id"]))
        for row in rows:
            if row['direction'] and row['currency'] and row['posted_date'] and row['signed_amount_decimal'] is not None:
                _save_bank_transaction_anomaly(db, row, _calculate_bank_transaction_anomaly(db, row, batch['source_bank_account_ref']))
        _bank_audit(db, user, 'bank.import_confirmed', import_id, {'rows': len(rows), 'candidates': proposed, 'account': batch['source_bank_account_ref']})
    return {'importId': str(import_id), 'status': 'confirmed', 'rowCount': len(rows), 'candidatesProposed': proposed}


def discard_bank_import(import_id: str, request: Request):
    """Removes rows nobody confirmed. The discarded preview stays on record as reverted (the existing schema state), so the file can be previewed again."""
    user = require_workspace_user(request, {"local_admin", "accountant"})
    import_id = _uuid_or_422(import_id, "import id")
    with connect() as db:
        batch = db.execute("SELECT status FROM bank_imports WHERE id=%s AND tenant_id=%s FOR UPDATE", (import_id, user["tenant_id"])).fetchone()
        if not batch:
            raise HTTPException(404, "Bank import not found.")
        if batch['status'] != 'previewed':
            raise HTTPException(409, "Only a previewed import can be discarded.")
        db.execute("DELETE FROM bank_transactions WHERE import_id=%s AND tenant_id=%s", (import_id, user["tenant_id"]))
        db.execute("UPDATE bank_imports SET status='reverted' WHERE id=%s AND tenant_id=%s", (import_id, user["tenant_id"]))
        _bank_audit(db, user, 'bank.import_discarded', import_id, {})
    return {'importId': str(import_id), 'status': 'discarded'}


def _import_bank_rows(data: BankImportInput, user):
    canonical = [{"postedDate": r.postedDate.isoformat() if r.postedDate else None,
                  "valueDate": r.valueDate.isoformat() if r.valueDate else None,
                  "description": r.description, "reference": r.reference,
                  "debit": str(r.debit) if r.debit is not None else None,
                  "credit": str(r.credit) if r.credit is not None else None,
                  "currency": r.currency.upper() if r.currency else None} for r in data.rows]
    source_hash = hashlib.sha256(repr(canonical).encode("utf-8")).hexdigest()
    import_id = uuid.uuid4()
    candidates_proposed = 0
    with connect() as db:
        _ensure_bank_anomaly_table(db)
        try:
            db.execute("""INSERT INTO bank_imports
                (id,tenant_id,source_bank_account_ref,source_hash,input_format,mapping_version,imported_by,row_count,status)
                VALUES (%s,%s,%s,%s,'csv',%s,%s,%s,'confirmed')""",
                (import_id, user["tenant_id"], data.sourceBankAccountRef.strip(), source_hash,
                 data.mappingVersion.strip(), user["id"], len(data.rows)))
        except UniqueViolation:
            raise HTTPException(409, "This bank statement has already been imported.")
        records = _eligible_records(db, user["tenant_id"])
        for number, row in enumerate(data.rows, 1):
            direction, amount, signed, error = _bank_row_values(row)
            status = "imported" if not error else "needs_review"
            currency = row.currency.upper() if row.currency else None
            fingerprint = hashlib.sha256(repr([str(user["tenant_id"]), row.reference, row.postedDate,
                                               row.description, str(row.debit), str(row.credit), number]).encode("utf-8")).hexdigest()
            transaction_id = uuid.uuid4()
            db.execute("""INSERT INTO bank_transactions
                (id,tenant_id,import_id,source_row,posted_date,value_date,description,reference,
                 debit_decimal,credit_decimal,signed_amount_decimal,direction,currency,fingerprint,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                 (transaction_id, user["tenant_id"], import_id, number, row.postedDate, row.valueDate, row.description.strip(),
                 row.reference.strip() if row.reference else None, row.debit, row.credit,
                 signed, direction, currency, fingerprint, status))
            if status == 'imported':
                candidates_proposed += _propose_candidates(db, user['tenant_id'], transaction_id, {
                    'amount': amount, 'signed': signed, 'direction': direction, 'currency': currency, 'reference': row.reference}, records)
        anomaly_rows = db.execute("""SELECT * FROM bank_transactions
            WHERE import_id=%s AND tenant_id=%s AND status='imported'
            ORDER BY posted_date, source_row""", (import_id, user['tenant_id'])).fetchall()
        for stored_row in anomaly_rows:
            _save_bank_transaction_anomaly(
                db, stored_row,
                _calculate_bank_transaction_anomaly(db, stored_row, data.sourceBankAccountRef.strip()),
            )
    return {"id": str(import_id), "importId": str(import_id), "status": "CONFIRMED",
            "rowCount": len(data.rows), "candidatesProposed": candidates_proposed,
            "sourceHash": source_hash}

def create_reconciliation_candidate(data: ReconciliationCandidateInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if not user.get("tenant_id"):
        raise HTTPException(403, "A company workspace is required.")
    try:
        bank_id, record_id = uuid.UUID(data.bankTransactionId), uuid.UUID(data.recordId)
    except ValueError:
        raise HTTPException(422, "Invalid bank transaction or record id.")
    with connect() as db:
        row = db.execute("""INSERT INTO reconciliation_candidates
            (tenant_id,bank_transaction_id,record_id,reason_codes,proposed_by)
            VALUES (%s,%s,%s,%s,%s) RETURNING *""",
            (user["tenant_id"], bank_id, record_id, Jsonb(data.reasonCodes), data.proposedBy)).fetchone()
    return {"id": str(row["id"]), "bankTransactionId": str(row["bank_transaction_id"]),
            "recordId": str(row["record_id"]), "status": row["status"], "proposedBy": row["proposed_by"]}

# ---------------------------------------------------------------------------------------------------------------
# P3-lite: match targets beyond scanned records, and why each match is suggested.
#
# A bank line can be explained by a typed document that already posted its cash (a payment voucher, payment
# received, refund or money in/out). Reconciling against it links the line to the document and marks both
# reconciled: no allocation, no journal. Open typed invoices and bills are listed so the accountant can see them,
# but their payment still goes through the existing payment documents (nothing here posts).
# ---------------------------------------------------------------------------------------------------------------

# Typed documents that move cash through a bank account, by the bank direction they explain.
CASH_DOCUMENT_TYPES = {"money_out": ("purchase-payment", "refund", "money-out"),
                       "money_in": ("payment", "purchase-refund", "money-in")}
# Typed open items (cash not posted yet), by the bank direction that would settle them.
OPEN_ITEM_TYPES = {"money_out": ("bill", "purchase-debit-note"), "money_in": ("invoice", "debit-note")}
DOCUMENT_LABELS = {"purchase-payment": "Payment voucher", "payment": "Payment received", "refund": "Customer refund",
                   "purchase-refund": "Supplier refund", "money-in": "Money in", "money-out": "Money out",
                   "bill": "Bill", "purchase-debit-note": "Supplier debit note", "invoice": "Invoice",
                   "debit-note": "Debit note"}
MATCH_LABEL_RANK = {"exact": 0, "reference": 1, "memory": 2, "amount+date": 3}
# Words that say how the money moved, not who it was for; ignored when remembering earlier matches.
_GENERIC_WORDS = {"IBG", "GIRO", "TRANSFER", "TRF", "PAYMENT", "PYMT", "DUITNOW", "FPX", "JOMPAY", "INSTANT",
                  "FUND", "FUNDS", "INTERBANK", "ONLINE", "BANKING", "DEBIT", "CREDIT", "CHQ", "CHEQUE", "DEPOSIT",
                  "CASH", "SDN", "BHD", "THE", "AND", "FOR", "REF", "INV", "MYR"}
DOCUMENT_TARGET_READ = """SELECT h.id,h.tenant_id,h.document_type,h.document_number,h.status,h.document_date,h.due_date,
        h.currency,h.reference_no,h.total_amount,h.vendor_id,h.customer_id,h.bank_account_id,h.journal_id,
        COALESCE(NULLIF(btrim(h.contact_name),''),v.vendor_name,c.customer_name) AS party,
        j.status AS journal_status,ba.name AS bank_account_name,
        (SELECT b.id FROM bank_transactions b WHERE b.tenant_id=h.tenant_id AND b.recorded_document_id=h.id
         ORDER BY b.id LIMIT 1) AS linked_bank_transaction_id,
        h.total_amount - COALESCE((SELECT SUM(a.amount) FROM erp_document_allocations a WHERE a.tenant_id=h.tenant_id
            AND a.target_document_id=h.id AND a.status='active'),0) AS outstanding
    FROM erp_document_headers h
    LEFT JOIN journal_headers j ON j.id=h.journal_id AND j.tenant_id=h.tenant_id
    LEFT JOIN vendors v ON v.id=h.vendor_id AND v.tenant_id=h.tenant_id
    LEFT JOIN customers c ON c.id=h.customer_id AND c.tenant_id=h.tenant_id
    LEFT JOIN erp_catalog_entries ba ON ba.id=h.bank_account_id AND ba.tenant_id=h.tenant_id"""


def _compact(value):
    return re.sub(r"[^0-9A-Z]", "", (value or "").upper())


def _reference_hit(bank, references):
    """The first (reference, paid-by note) whose text appears on the bank line, or (None, None)."""
    reference = _compact(bank.get("reference"))
    text = reference + "|" + _compact(bank.get("description"))
    for value, via in references:
        wanted = _compact(value)
        if len(wanted) >= 4 and wanted in text:
            return value, via
        if len(wanted) >= 3 and wanted == reference:
            return value, via
    return None, None


def _description_key(description):
    """What a bank description says about who was paid: its first three distinctive words."""
    words = [w for w in re.findall(r"[A-Z]{3,}", (description or "").upper()) if w not in _GENERIC_WORDS]
    return " ".join(words[:3]) or None


def _match_memory(db, tenant_id):
    """Earlier reconciled bank lines: description key -> the contacts they were matched to."""
    rows = db.execute("""SELECT t.description,COALESCE(h.vendor_id,h.customer_id) AS party_id,
            COALESCE(NULLIF(btrim(h.contact_name),''),v.vendor_name,c.customer_name) AS party
        FROM bank_transactions t
        JOIN erp_document_headers h ON h.id=t.recorded_document_id AND h.tenant_id=t.tenant_id AND h.status<>'void'
        LEFT JOIN vendors v ON v.id=h.vendor_id AND v.tenant_id=h.tenant_id
        LEFT JOIN customers c ON c.id=h.customer_id AND c.tenant_id=h.tenant_id
        WHERE t.tenant_id=%s AND t.status='matched'
        UNION ALL
        SELECT t.description,r.vendor_id,COALESCE(r.supplier_name,r.buyer_name)
        FROM reconciliation_candidates k
        JOIN bank_transactions t ON t.id=k.bank_transaction_id AND t.tenant_id=k.tenant_id
        JOIN standardised_records r ON r.id=k.record_id AND r.tenant_id=k.tenant_id
        WHERE k.tenant_id=%s AND k.status='confirmed'
        LIMIT 5000""", (tenant_id, tenant_id)).fetchall()
    memory = {}
    for row in rows:
        key = _description_key(row["description"])
        if not key:
            continue
        entry = memory.setdefault(key, {"ids": set(), "names": set(), "party": None})
        if row["party_id"]:
            entry["ids"].add(row["party_id"])
        if row["party"] and _compact(row["party"]):
            entry["names"].add(_compact(row["party"]))
            entry["party"] = entry["party"] or row["party"]
    return memory


def _explain(bank, target, memory=None):
    """(label, reason) saying why a target explains this bank line, or (None, None) when nothing does.

    exact: amount and a reference agree · reference: a reference agrees, the amount does not ·
    memory: amount agrees and earlier lines with this description went to the same contact ·
    amount+date: amount agrees and the dates are within a week."""
    amount = abs(bank.get("signed_amount_decimal") or Decimal(0))
    shown = money(amount)
    reference, via = _reference_hit(bank, target["references"])
    paid = f" ({reference} was paid by {via})" if via else ""
    same_amount = target["amount"] is not None and Decimal(target["amount"]) == amount
    if same_amount and reference:
        return "exact", f"Amount {shown} and reference {reference} match{paid}"
    if reference:
        return "reference", (f"Reference {reference} matches{paid}; the amounts differ "
                             f"(bank {shown}, {target['number']} {money(target['amount'] or 0)})")
    if not same_amount:
        return None, None
    key = _description_key(bank.get("description"))
    entry = (memory or {}).get(key) if key else None
    if entry and ((target.get("party_ids") or set()) & entry["ids"]
                  or (_compact(target.get("party")) and _compact(target.get("party")) in entry["names"])):
        return "memory", (f"Amount {shown} matches, and earlier bank lines like \"{key}\" were matched to "
                          f"{target.get('party') or entry['party']}")
    posted, dated = bank.get("posted_date"), target.get("date")
    if posted and dated:
        days = abs((posted - dated).days)
        if days <= 7:
            when = "on the same day" if days == 0 else f"{days} day{'s' if days != 1 else ''} apart"
            return "amount+date", f"Amount {shown} matches and the dates are {when}"
    return None, None


def _record_payers(db, tenant_id, record_ids):
    """Scanned records already settled by a ready typed payment: record id -> that payment document."""
    if not record_ids:
        return {}
    rows = db.execute("""SELECT DISTINCT ON (a.record_id) a.record_id,h.id,h.document_number,h.document_type,h.total_amount
        FROM settlement_allocations a
        JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id
            AND s.source_type='manual_record' AND s.status='recorded'
        JOIN erp_document_headers h ON h.id=s.source_id AND h.tenant_id=s.tenant_id AND h.status='ready'
            AND h.document_type IN ('payment','purchase-payment')
        WHERE a.tenant_id=%s AND a.record_id=ANY(%s) AND a.status='active'
          AND NOT EXISTS (SELECT 1 FROM settlement_allocations u WHERE u.tenant_id=a.tenant_id AND u.reverses_allocation_id=a.id)
        ORDER BY a.record_id,a.created_at DESC""", (tenant_id, list(record_ids))).fetchall()
    return {row["record_id"]: row for row in rows}


def _payer_dto(payer):
    return {"documentId": str(payer["id"]), "documentNumber": payer["document_number"],
            "documentType": payer["document_type"],
            "documentLabel": DOCUMENT_LABELS.get(payer["document_type"], payer["document_type"])} if payer else None


def _settled_note(payer):
    number = payer["document_number"]
    return (f"; already paid by {number}, so confirming links this line to {number} "
            f"(no new allocation or journal)")


def _record_target(record, *, use_remaining=False):
    total = record.get("total_amount")
    amount = record.get("remaining") if use_remaining else total
    return {"references": [(record.get("reference_number"), None), (record.get("invoice_number"), None),
                           (record.get("record_code"), None)],
            "amount": amount, "number": record.get("record_code"), "date": record.get("invoice_date"),
            "party": record.get("supplier_name") or record.get("buyer_name"),
            "party_ids": {record["vendor_id"]} if record.get("vendor_id") else set()}


def _candidate_explanation(bank, candidate, payer, memory):
    label, reason = _explain(bank, _record_target(candidate), memory)
    if not label:
        label, reason = None, ("Suggested by the matching rules" if candidate["proposed_by"] == "rules"
                               else "Suggested by a reviewer")
    if payer and candidate["status"] == "proposed":
        reason += _settled_note(payer)
    return label, reason


def _record_remaining(db, tenant_id, record_ids):
    rows = db.execute("""SELECT r.id,r.record_code,COALESCE(r.amount_due,r.total_amount,0) - COALESCE((
            SELECT SUM(a.amount) FROM settlement_allocations a JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id
            WHERE a.record_id=r.id AND a.tenant_id=r.tenant_id AND a.status='active'
              AND NOT EXISTS (SELECT 1 FROM settlement_allocations undo
                WHERE undo.tenant_id=a.tenant_id AND undo.reverses_allocation_id=a.id)),0) AS remaining
        FROM standardised_records r WHERE r.tenant_id=%s AND r.id=ANY(%s)""", (tenant_id, list(record_ids))).fetchall()
    return {row["id"]: row for row in rows}


def _statement_account_id(db, bank):
    """The bank-account catalog entry this statement belongs to, or None when it cannot be told."""
    rows = db.execute("""SELECT id FROM erp_catalog_entries
        WHERE tenant_id=%s AND kind='bank-account' AND is_active
          AND lower(%s) IN (lower(btrim(data->>'accountNumber')), lower(btrim(code)), lower(btrim(name)))
        LIMIT 2""", (bank["tenant_id"], (bank.get("source_bank_account_ref") or "").strip())).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def _link_problem(bank, doc, statement_account_id):
    """Why this bank line cannot be reconciled against the document, or None when it can."""
    number = doc["document_number"]
    label = DOCUMENT_LABELS.get(doc["document_type"], doc["document_type"])
    direction = bank["direction"]
    if doc["document_type"] not in CASH_DOCUMENT_TYPES.get(direction, ()):
        flow = "out" if direction == "money_out" else "in"
        return f"{label} {number} cannot explain a money-{flow} bank line."
    if doc["status"] != "ready":
        return f"{number} is {'void' if doc['status'] == 'void' else 'not issued yet'}. Only a posted payment can be reconciled."
    if not doc["journal_id"] or doc["journal_status"] != "posted":
        return f"{number} has no posted journal, so it has not recorded the cash yet."
    if (doc["currency"] or "").upper() != (bank["currency"] or "").upper():
        return f"{number} is in {doc['currency']} but the bank line is in {bank['currency']}."
    amount = abs(bank["signed_amount_decimal"] or Decimal(0))
    if Decimal(doc["total_amount"]) != amount:
        return (f"{number} is {money(doc['total_amount'])} but the bank line is {money(amount)}. "
                "Only a full-amount reconciliation is possible.")
    if doc["linked_bank_transaction_id"] and doc["linked_bank_transaction_id"] != bank["id"]:
        return f"{number} is already reconciled with another bank line."
    if statement_account_id and doc["bank_account_id"] and doc["bank_account_id"] != statement_account_id:
        return (f"{number} was paid {'from' if direction == 'money_out' else 'into'} {doc['bank_account_name'] or 'another account'}, "
                "not the account on this statement.")
    return None


def _document_references(db, tenant_id, document_ids):
    """Each payment's own number and reference, plus the numbers of the bills/invoices it paid."""
    refs = {}
    rows = db.execute("""SELECT a.source_document_id AS id,t.document_number AS value FROM erp_document_allocations a
            JOIN erp_document_headers t ON t.id=a.target_document_id AND t.tenant_id=a.tenant_id
            WHERE a.tenant_id=%s AND a.source_document_id=ANY(%s) AND a.status='active'
        UNION ALL
        SELECT a.source_document_id,t.reference_no FROM erp_document_allocations a
            JOIN erp_document_headers t ON t.id=a.target_document_id AND t.tenant_id=a.tenant_id
            WHERE a.tenant_id=%s AND a.source_document_id=ANY(%s) AND a.status='active' AND t.reference_no IS NOT NULL
        UNION ALL
        SELECT s.source_id,unnest(ARRAY[r.reference_number,r.invoice_number,r.record_code]) FROM settlements s
            JOIN settlement_allocations a ON a.settlement_id=s.id AND a.tenant_id=s.tenant_id AND a.status='active'
            JOIN standardised_records r ON r.id=a.record_id AND r.tenant_id=a.tenant_id
            WHERE s.tenant_id=%s AND s.source_type='manual_record' AND s.source_id=ANY(%s)
              AND NOT EXISTS (SELECT 1 FROM settlement_allocations u WHERE u.tenant_id=a.tenant_id AND u.reverses_allocation_id=a.id)""",
        (tenant_id, document_ids, tenant_id, document_ids, tenant_id, document_ids)).fetchall()
    for row in rows:
        if row["value"]:
            refs.setdefault(row["id"], []).append(row["value"])
    return refs


def _document_target(doc, paid_refs=()):
    open_item = doc["document_type"] in OPEN_ITEM_TYPES["money_out"] + OPEN_ITEM_TYPES["money_in"]
    references = [(doc["document_number"], None), (doc["reference_no"], None)]
    references += [(value, doc["document_number"]) for value in paid_refs]
    return {"references": references, "amount": doc["outstanding"] if open_item else doc["total_amount"],
            "number": doc["document_number"], "date": doc["document_date"], "party": doc["party"],
            "party_ids": {i for i in (doc["vendor_id"], doc["customer_id"]) if i}}


def _document_target_dto(bank, doc, statement_account_id, memory, paid_refs=()):
    open_item = doc["document_type"] in OPEN_ITEM_TYPES.get(bank["direction"], ())
    label, reason = _explain(bank, _document_target(doc, paid_refs), memory)
    if open_item:
        payment = "payment voucher" if bank["direction"] == "money_out" else "payment received"
        problem = (f"{doc['document_number']} is still open ({money(doc['outstanding'])} outstanding). Record its "
                   f"{payment} first, then reconcile this line against that payment.")
    else:
        problem = _link_problem(bank, doc, statement_account_id)
    return {"targetType": "document", "documentId": str(doc["id"]), "documentType": doc["document_type"],
            "documentLabel": DOCUMENT_LABELS.get(doc["document_type"], doc["document_type"]),
            "documentNumber": doc["document_number"], "documentDate": str(doc["document_date"]),
            "party": doc["party"], "reference": doc["reference_no"], "currency": doc["currency"],
            "amount": money(doc["total_amount"]),
            "outstandingAmount": money(max(Decimal(doc["outstanding"]), Decimal(0))) if open_item else None,
            "bankAccountName": doc["bank_account_name"],
            "action": "pay" if open_item else "link", "linkable": problem is None, "blockedReason": problem,
            "matchLabel": label, "matchReason": reason}


def _rank(item):
    return MATCH_LABEL_RANK.get(item["matchLabel"], 9)


def _document_targets(db, bank, q, memory, statement_account_id):
    """Typed payments (and open bills/invoices) for this line's direction and currency that fit the search."""
    direction = bank["direction"]
    if direction not in CASH_DOCUMENT_TYPES:
        return []
    types = list(CASH_DOCUMENT_TYPES[direction] + OPEN_ITEM_TYPES[direction])
    term = f"%{q}%"
    rows = db.execute("SELECT * FROM (" + DOCUMENT_TARGET_READ + """
        WHERE h.tenant_id=%s AND h.document_type=ANY(%s) AND h.status='ready' AND h.currency=%s
          AND (%s='' OR h.document_number ILIKE %s OR COALESCE(h.reference_no,'') ILIKE %s
               OR COALESCE(h.contact_name,v.vendor_name,c.customer_name,'') ILIKE %s)) d
        WHERE d.document_type NOT IN ('bill','purchase-debit-note','invoice','debit-note') OR d.outstanding>0
        ORDER BY d.document_date DESC,d.document_number DESC LIMIT 50""",
        (bank["tenant_id"], types, bank["currency"], q, term, term, term)).fetchall()
    refs = _document_references(db, bank["tenant_id"], [r["id"] for r in rows]) if rows else {}
    items = [_document_target_dto(bank, r, statement_account_id, memory, refs.get(r["id"], ())) for r in rows]
    return sorted(items, key=lambda item: (_rank(item), not item["linkable"]))


def _document_suggestions(db, bank, memory, statement_account_id, limit=5):
    """Posted payments that explain this unmatched line, best first, each with its reason."""
    direction = bank["direction"]
    if direction not in CASH_DOCUMENT_TYPES or not bank["posted_date"] or bank["signed_amount_decimal"] is None:
        return []
    amount = abs(bank["signed_amount_decimal"])
    rows = db.execute(DOCUMENT_TARGET_READ + """
        WHERE h.tenant_id=%s AND h.document_type=ANY(%s) AND h.status='ready' AND h.currency=%s
          AND (h.total_amount=%s OR h.document_date BETWEEN %s::date - 60 AND %s::date + 60)
          AND NOT EXISTS (SELECT 1 FROM bank_transactions b WHERE b.tenant_id=h.tenant_id AND b.recorded_document_id=h.id)
        ORDER BY h.document_date DESC LIMIT 300""",
        (bank["tenant_id"], list(CASH_DOCUMENT_TYPES[direction]), bank["currency"], amount,
         bank["posted_date"], bank["posted_date"])).fetchall()
    refs = _document_references(db, bank["tenant_id"], [r["id"] for r in rows]) if rows else {}
    items = [_document_target_dto(bank, r, statement_account_id, memory, refs.get(r["id"], ())) for r in rows]
    items = [item for item in items if item["matchLabel"]]
    return sorted(items, key=lambda item: (_rank(item), not item["linkable"]))[:limit]


def _record_rows(db, tenant_id, q):
    term = f"%{q}%"
    return db.execute("""SELECT r.id,r.record_code,r.document_id,r.category,r.supplier_name,r.buyer_name,r.vendor_id,r.invoice_date,
        COALESCE(r.reference_number,r.invoice_number) reference_number,r.invoice_number,r.currency,r.total_amount,r.amount_due,
        COALESCE((SELECT SUM(a.amount) FROM settlement_allocations a JOIN settlements s ON s.id=a.settlement_id
            WHERE a.record_id=r.id AND a.tenant_id=r.tenant_id AND a.status='active'
              AND NOT EXISTS (SELECT 1 FROM settlement_allocations undo
                WHERE undo.tenant_id=a.tenant_id AND undo.reverses_allocation_id=a.id)),0) allocated_amount
        FROM standardised_records r WHERE r.tenant_id=%s AND r.is_current=true AND r.validation_status='passed'
          AND (%s='' OR r.record_code ILIKE %s OR COALESCE(r.reference_number,'') ILIKE %s OR COALESCE(r.invoice_number,'') ILIKE %s
               OR COALESCE(r.supplier_name,r.buyer_name,'') ILIKE %s)
        ORDER BY r.created_at DESC LIMIT 50""", (tenant_id, q, term, term, term, term)).fetchall()


def _record_targets(db, bank, q, memory):
    rows = _record_rows(db, bank["tenant_id"], q)
    payers = _record_payers(db, bank["tenant_id"], [r["id"] for r in rows])
    items = []
    for r in rows:
        due = r["amount_due"] if r["amount_due"] is not None else r["total_amount"] or Decimal("0")
        remaining = due - r["allocated_amount"]
        payer = payers.get(r["id"]) if remaining <= 0 else None
        label, reason = _explain(bank, _record_target({**r, "remaining": remaining}, use_remaining=not payer), memory)
        if payer:
            reason = (reason or f"{r['record_code']} is fully paid") + _settled_note(payer).replace(
                "confirming links", "matching links")
        items.append({"targetType": "record", "recordId": str(r["id"]), "recordCode": r["record_code"],
                      "documentId": str(r["document_id"]), "category": r["category"],
                      "party": r["supplier_name"] or r["buyer_name"], "reference": r["reference_number"],
                      "invoiceNumber": r["invoice_number"], "currency": r["currency"],
                      "totalAmount": str(r["total_amount"]) if r["total_amount"] is not None else None,
                      "allocatedAmount": str(r["allocated_amount"]), "remainingAmount": str(remaining),
                      "action": "link" if payer else "allocate", "settledBy": _payer_dto(payer),
                      "matchLabel": label, "matchReason": reason})
    return sorted(items, key=_rank)


def _read_statement_line(db, tenant_id, transaction_id):
    bank = db.execute("""SELECT t.*,i.source_bank_account_ref FROM bank_transactions t
        JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
        WHERE t.id=%s AND t.tenant_id=%s""", (transaction_id, tenant_id)).fetchone()
    if not bank:
        raise HTTPException(404, "Bank transaction not found.")
    return bank


def search_bank_match_records(transaction_id: str, request: Request, q: str = ""):
    """Return approved records eligible for a manually selected bank match, each with why it fits."""
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    with connect() as db:
        bank = _read_statement_line(db, user["tenant_id"], transaction_id)
        return _record_targets(db, bank, q.strip(), _match_memory(db, user["tenant_id"]))


def search_bank_match_targets(transaction_id: str, request: Request, q: str = ""):
    """Everything this bank line could be matched to: scanned records to allocate, posted typed payments to
    reconcile against, and open typed bills/invoices (shown, but paid through their payment documents)."""
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    query = q.strip()
    with connect() as db:
        bank = _read_statement_line(db, user["tenant_id"], transaction_id)
        memory = _match_memory(db, user["tenant_id"])
        statement_account_id = _statement_account_id(db, bank)
        return {"records": _record_targets(db, bank, query, memory),
                "documents": _document_targets(db, bank, query, memory, statement_account_id)}


def _reconciliation_result(link, doc, *, idempotent=False):
    return {"reconciliationId": str(link["id"]), "bankTransactionId": str(link["bank_transaction_id"]),
            "documentId": str(doc["id"]), "documentNumber": doc["document_number"], "documentType": doc["document_type"],
            "documentLabel": DOCUMENT_LABELS.get(doc["document_type"], doc["document_type"]),
            "journalId": str(doc["journal_id"]) if doc["journal_id"] else None,
            "matchLabel": link["match_label"], "matchReason": link["match_reason"],
            "bankTransactionStatus": "matched", "newJournal": False, "idempotent": idempotent}


def _reconcile_with_document(db, user, bank, document_id):
    """Reconcile a locked bank line against a typed payment that already posted its cash.

    Links the line to the document and marks both reconciled: no allocation and no journal are created."""
    tenant_id = user["tenant_id"]
    existing = db.execute("""SELECT * FROM bank_document_reconciliations
        WHERE tenant_id=%s AND bank_transaction_id=%s AND undone_at IS NULL""", (tenant_id, bank["id"])).fetchone()
    if existing:
        doc = db.execute("SELECT id,document_number,document_type,journal_id FROM erp_document_headers WHERE id=%s AND tenant_id=%s",
                         (existing["document_id"], tenant_id)).fetchone()
        if existing["document_id"] == document_id:
            return _reconciliation_result(existing, doc, idempotent=True)
        raise HTTPException(409, f"This bank line is already reconciled with {doc['document_number']}. Undo that first.")
    if bank["recorded_document_id"]:
        raise HTTPException(409, "This bank line already has its own money document.")
    _require_unmatched_bank_line(bank)
    if db.execute("SELECT 1 FROM current_bank_coding WHERE tenant_id=%s AND bank_transaction_id=%s AND decision<>'cleared'",
                  (tenant_id, bank["id"])).fetchone():
        raise HTTPException(409, "Clear the bank transaction's coding before reconciling it against a payment.")
    settlement = _existing_bank_settlement(db, bank, lock=True)
    if settlement and settlement_allocated(db, tenant_id, settlement["id"]):
        raise HTTPException(409, "Undo the allocations on this bank line before reconciling it against a payment.")
    doc = db.execute(DOCUMENT_TARGET_READ + " WHERE h.id=%s AND h.tenant_id=%s FOR SHARE OF h",
                     (document_id, tenant_id)).fetchone()
    if not doc:
        raise HTTPException(404, "Payment document not found.")
    problem = _link_problem(bank, doc, _statement_account_id(db, bank))
    if problem:
        raise HTTPException(409, problem)
    refs = _document_references(db, tenant_id, [doc["id"]]).get(doc["id"], ())
    label, reason = _explain(bank, _document_target(doc, refs), _match_memory(db, tenant_id))
    if not label:
        label, reason = "manual", f"Chosen by the reviewer: {DOCUMENT_LABELS.get(doc['document_type'])} {doc['document_number']}"
    try:
        db.execute("SAVEPOINT bank_reconcile_document")
        link = db.execute("""INSERT INTO bank_document_reconciliations
            (tenant_id,bank_transaction_id,document_id,match_label,match_reason,created_by)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            (tenant_id, bank["id"], doc["id"], label, reason, user["id"])).fetchone()
        db.execute("RELEASE SAVEPOINT bank_reconcile_document")
    except UniqueViolation as error:
        db.execute("ROLLBACK TO SAVEPOINT bank_reconcile_document")
        raise HTTPException(409, f"{doc['document_number']} is already reconciled with another bank line.") from error
    mark_bank_line_matched(db, bank, document_id=doc["id"])
    _bank_audit(db, user, "bank.document_reconciled", bank["id"],
                {"reconciliationId": link["id"], "documentId": doc["id"], "documentNumber": doc["document_number"],
                 "matchLabel": label, "matchReason": reason})
    return _reconciliation_result(link, doc)


def reconcile_bank_document(transaction_id: str, data: BankReconcileDocumentInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    document_id = _uuid_or_422(data.documentId, "document id")
    try:
        with connect() as db:
            bank = _lock_statement_line(db, user["tenant_id"], transaction_id)
            return _reconcile_with_document(db, user, bank, document_id)
    except RaiseException as error:
        raise HTTPException(409, error.diag.message_primary) from error


def undo_bank_document_reconciliation(transaction_id: str, request: Request, data: BankReconcileUndoInput | None = None):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    reason = (data.reason if data else BankReconcileUndoInput().reason).strip() or BankReconcileUndoInput().reason
    try:
        with connect() as db:
            bank = _lock_statement_line(db, user["tenant_id"], transaction_id)
            link = db.execute("""SELECT r.*,h.document_number FROM bank_document_reconciliations r
                JOIN erp_document_headers h ON h.id=r.document_id AND h.tenant_id=r.tenant_id
                WHERE r.tenant_id=%s AND r.bank_transaction_id=%s AND r.undone_at IS NULL FOR UPDATE OF r""",
                (user["tenant_id"], transaction_id)).fetchone()
            if not link:
                if bank["recorded_document_id"]:
                    raise HTTPException(409, "This line's money document was recorded from the bank line. "
                                             "Void that document to reopen the line.")
                raise HTTPException(409, "This bank line is not reconciled against a payment document.")
            db.execute("""UPDATE bank_transactions SET status='imported',recorded_document_id=NULL
                WHERE id=%s AND tenant_id=%s""", (transaction_id, user["tenant_id"]))
            db.execute("""UPDATE bank_document_reconciliations SET undone_at=now(),undone_by=%s,undo_reason=%s
                WHERE id=%s AND tenant_id=%s""", (user["id"], reason, link["id"], user["tenant_id"]))
            _bank_audit(db, user, "bank.document_reconciliation_undone", transaction_id,
                        {"reconciliationId": link["id"], "documentId": link["document_id"], "reason": reason})
    except RaiseException as error:
        raise HTTPException(409, error.diag.message_primary) from error
    return {"bankTransactionId": str(transaction_id), "documentId": str(link["document_id"]),
            "documentNumber": link["document_number"], "bankTransactionStatus": "imported"}


def _existing_bank_settlement(db, bank, *, lock=False):
    return db.execute("""SELECT id,amount,status,direction,currency FROM settlements WHERE tenant_id=%s
        AND source_type='bank_import' AND source_id=%s""" + (" FOR UPDATE" if lock else ""),
        (bank["tenant_id"], bank["id"])).fetchone()


def _lock_statement_line(db, tenant_id, transaction_id):
    bank = db.execute("""SELECT t.*,i.source_bank_account_ref FROM bank_transactions t
        JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
        WHERE t.id=%s AND t.tenant_id=%s AND i.status='confirmed' FOR UPDATE OF t""",
        (transaction_id, tenant_id)).fetchone()
    if not bank:
        raise HTTPException(404, "Bank transaction not found.")
    return bank


def _require_unmatched_bank_line(bank):
    if bank["status"] in {"matched", "ignored"}:
        raise HTTPException(409, "Only an unmatched bank transaction can be used.")
    if not all(bank[k] for k in ("direction", "posted_date", "currency", "signed_amount_decimal")):
        raise HTTPException(422, "The bank transaction needs a direction, date, currency and amount.")


def _statement_catalog_account(db, bank, catalog_id=None):
    tenant_id = bank["tenant_id"]
    if catalog_id is not None:
        catalog_id = _uuid_or_422(catalog_id, "bank account id")
        if not db.execute("""SELECT id FROM erp_catalog_entries
            WHERE tenant_id=%s AND id=%s AND kind='bank-account' AND is_active FOR SHARE""",
                          (tenant_id, catalog_id)).fetchone():
            raise HTTPException(422, "Choose an active bank account for this company.")
        return catalog_id
    matches = db.execute("""SELECT id FROM erp_catalog_entries
        WHERE tenant_id=%s AND kind='bank-account' AND is_active
          AND lower(%s) IN (lower(btrim(data->>'accountNumber')), lower(btrim(code)), lower(btrim(name)))
        LIMIT 2 FOR SHARE""", (tenant_id, bank["source_bank_account_ref"].strip())).fetchall()
    if len(matches) != 1:
        raise HTTPException(422, "Choose the bank account for this statement line.")
    return matches[0]["id"]


def record_bank_expense(transaction_id: str, data: BankExpenseInput, request: Request):
    from services.routes.erp import documents, posting

    user = require_workspace_user(request, {"local_admin", "accountant"})
    tenant_id = user["tenant_id"]
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    with connect() as db:
        bank = _lock_statement_line(db, tenant_id, transaction_id)
        catalog_id = (_statement_catalog_account(db, bank, data.bankAccountId)
                      if data.bankAccountId is not None else None)
        if bank["recorded_document_id"]:
            document = db.execute("""SELECT id,document_number,journal_id FROM erp_document_headers
                WHERE id=%s AND tenant_id=%s""", (bank["recorded_document_id"], tenant_id)).fetchone()
        else:
            _require_unmatched_bank_line(bank)
            settlement = _existing_bank_settlement(db, bank, lock=True)
            if settlement:
                if settlement_allocated(db, tenant_id, settlement["id"]):
                    raise HTTPException(409, "Undo the existing allocations before recording this bank transaction.")
                require_no_posted_settlement_journal(db, tenant_id, settlement["id"])
            catalog_id = catalog_id or _statement_catalog_account(db, bank)
            expected = "expense" if bank["direction"] == "money_out" else "income"
            account = posting.ledger_account(db, tenant_id, data.ledgerAccountId, expected=expected, label=expected)
            description = (data.description or "").strip() or bank["description"][:4000] or (
                "Bank expense" if expected == "expense" else "Bank interest")
            document = documents.create_document_from_bank(db, user, bank, catalog_id, account["id"], description)
            mark_bank_line_matched(db, bank, document_id=document["id"])
            _bank_audit(db, user, "bank.expense_recorded", transaction_id,
                        {"documentId": document["id"], "journalId": document["journal_id"],
                         "ledgerAccountId": account["id"], "direction": bank["direction"]})
    return {"documentId": str(document["id"]), "documentNumber": document["document_number"],
            "journalId": str(document["journal_id"]), "bankTransactionStatus": "matched"}


def post_bank_payment(transaction_id: str, request: Request, data: BankPaymentInput | None = None):
    user = require_workspace_user(request, {"local_admin"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    with connect() as db:
        bank = _lock_statement_line(db, user["tenant_id"], transaction_id)
        settlement = _existing_bank_settlement(db, bank, lock=True)
        return post_allocated_bank_payment(
            db, user, bank, settlement, data.journalDate if data else None,
            lambda: _statement_catalog_account(db, bank),
        )


def match_bank_transaction_many(transaction_id: str, data: BankMatchManyInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    tenant_id = user["tenant_id"]
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    allocations = {_uuid_or_422(item.recordId, "record id"): item.amount for item in data.allocations}
    if len(allocations) != len(data.allocations):
        raise HTTPException(422, "Include each record only once in the request.")
    try:
        with connect() as db:
            bank = _lock_statement_line(db, tenant_id, transaction_id)
            settlement = _existing_bank_settlement(db, bank, lock=True)
            allocated = settlement_allocated(db, tenant_id, settlement["id"]) if settlement else Decimal(0)
            if allocated != data.expectedAllocated:
                raise HTTPException(409, "This bank line changed since you opened it. Refresh and check what is already matched.")
            _require_unmatched_bank_line(bank)
            if db.execute("""SELECT 1 FROM current_bank_coding WHERE tenant_id=%s AND bank_transaction_id=%s""",
                          (tenant_id, transaction_id)).fetchone():
                raise HTTPException(409, "Clear the bank transaction's coding before matching it to records.")
            # Bug f: a record a typed payment already settled cannot take this line's cash again. When it is the
            # whole match, reconcile the line against that payment instead (no allocation, no journal).
            payers = _record_payers(db, tenant_id, list(allocations))
            if payers:
                remaining_by_record = _record_remaining(db, tenant_id, list(payers))
                settled = [(record_id, payer) for record_id, payer in payers.items()
                           if remaining_by_record[record_id]["remaining"] < allocations[record_id]]
                if settled:
                    record_id, payer = settled[0]
                    code, number = remaining_by_record[record_id]["record_code"], payer["document_number"]
                    if len(allocations) != 1 or allocated != 0:
                        raise HTTPException(409, f"{code} is already paid by {number}. Reconcile the bank line "
                                                 f"against {number} instead of allocating {code} again.")
                    reconciliation = _reconcile_with_document(db, user, bank, payer["id"])
                    return {"settlementId": None, "allocated": "0.00", "remaining": "0.00",
                            "bankTransactionStatus": "matched", "reconciliation": reconciliation}
            if not settlement:
                settlement, _ = get_or_create_bank_settlement(db, user, bank)
            settlement_id = settlement["id"]
            if (settlement["amount"] != abs(bank["signed_amount_decimal"]) or settlement["direction"] != bank["direction"]
                    or settlement["currency"] != bank["currency"]):
                raise HTTPException(409, "The bank transaction and settlement details do not agree.")
            require_no_posted_settlement_journal(db, tenant_id, settlement_id, allow_payment_batches=True)
            total = allocated + sum(allocations.values(), Decimal(0))
            remaining = settlement["amount"] - total
            if remaining < 0:
                raise HTTPException(409, "The allocations exceed the remaining amount on this bank transaction.")
            records = db.execute("""SELECT id,category FROM standardised_records
                WHERE tenant_id=%s AND id=ANY(%s) AND is_current AND validation_status='passed'
                ORDER BY id FOR UPDATE""", (tenant_id, list(allocations))).fetchall()
            if len(records) != len(allocations):
                raise HTTPException(404, "A current approved record was not found.")
            for record in records:
                if not direction_matches(record["category"], bank["direction"]):
                    raise HTTPException(422, "Money-in transactions must match receivables; money-out transactions must match payables.")
                record_settlement_allocation(db, user, settlement_id, record["id"], allocations[record["id"]])
            status = bank["status"]
            if remaining == 0:
                status = "matched"
                mark_bank_line_matched(db, bank)
            _bank_audit(db, user, "bank.match_many", transaction_id,
                        {"settlementId": settlement_id, "allocated": money(total), "remaining": money(remaining),
                         "allocations": [{"recordId": record_id, "amount": money(amount)}
                                         for record_id, amount in allocations.items()]})
    except RaiseException as error:
        raise HTTPException(409, error.diag.message_primary) from error
    return {"settlementId": str(settlement_id), "allocated": money(total), "remaining": money(remaining),
            "bankTransactionStatus": status}

def list_reconciliation_suggestions(request: Request):
    """Suggest exact reference/amount/currency matches for human review."""
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_scope(user)
    grouped = {}
    records_by_tenant = {}
    with connect() as db:
        transactions = db.execute(BANK_TRANSACTION_READ + """ WHERE i.status='confirmed'
            AND (%s::uuid IS NULL OR t.tenant_id=%s) AND t.status IN ('imported','proposed')
            ORDER BY t.posted_date DESC NULLS LAST, t.source_row DESC, t.id""", (scope, scope)).fetchall()
        for transaction in transactions:
            for record in _bank_candidates(db, transaction, records_by_tenant):
                if record["status"] != "proposed":
                    continue
                item = grouped.setdefault(record["record_id"], {
                    "recordId": str(record["record_id"]), "documentId": str(record["document_id"]),
                    "revision": record["revision_number"], "category": record["category"],
                    "referenceNumber": record["reference_number"], "currency": record["currency"],
                    "totalAmount": str(record["total_amount"]) if record["total_amount"] is not None else None,
                    "bankTransactionIds": [],
                })
                item["bankTransactionIds"].append(str(transaction["id"]))
    for item in grouped.values():
        unique = len(item["bankTransactionIds"]) == 1
        item.update(status="MATCHED" if unique else "REVIEW_REQUIRED",
                    reason=None if unique else "MULTIPLE_MATCH_CANDIDATES")
    return {"suggestions": list(grouped.values()), "humanConfirmationRequired": True}

def _post_matched_payment(db, user, bank_transaction_id, journal_date):
    """Post the payment for a just-confirmed match with the existing posting rules (period lock, accounts, once)."""
    bank = _lock_statement_line(db, user["tenant_id"], bank_transaction_id)
    settlement = _existing_bank_settlement(db, bank, lock=True)
    return post_allocated_bank_payment(db, user, bank, settlement, journal_date,
                                       lambda: _statement_catalog_account(db, bank))


def decide_reconciliation(data: BankMatchDecisionInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if not user.get("tenant_id"):
        raise HTTPException(403, "A company workspace is required.")
    try:
        candidate_id = uuid.UUID(data.candidateId)
    except ValueError:
        raise HTTPException(422, "Invalid candidate id.")
    if data.decision != "confirmed" and not data.reason:
        raise HTTPException(422, "A reason is required when rejecting or reversing a match.")
    if data.postPayment and data.decision != "confirmed":
        raise HTTPException(422, "Only a confirmed match can post its payment.")
    if data.postPayment and user["role"] != "local_admin":
        raise HTTPException(403, "Only a local admin can post payments to the ledger. Confirm the match only.")
    with connect() as db:
        candidate = db.execute("""SELECT b.id bank_transaction_id,c.record_id,b.direction,b.posted_date,b.signed_amount_decimal,b.currency,
                   b.status AS bank_status,r.category,c.status AS candidate_status
            FROM reconciliation_candidates c
            JOIN bank_transactions b ON b.id=c.bank_transaction_id AND b.tenant_id=c.tenant_id
            JOIN standardised_records r ON r.id=c.record_id AND r.tenant_id=c.tenant_id
            WHERE c.id=%s AND c.tenant_id=%s FOR UPDATE OF b""", (candidate_id, user["tenant_id"])).fetchone()
        if not candidate:
            raise HTTPException(404, "Reconciliation candidate not found.")
        if data.postPayment:
            # Read after the bank-line lock: a concurrent confirm may have committed while this request waited.
            current = db.execute("SELECT status FROM reconciliation_candidates WHERE id=%s AND tenant_id=%s",
                                 (candidate_id, user["tenant_id"])).fetchone()
            if current["status"] == "confirmed":
                # A retry or double click: the match stands, so post (or return) its payment exactly once.
                # A payment journal for this match that was later reversed means the retry came too late: posting
                # again would pay twice. Re-posting after a reversal is an explicit action ("Post payment to ledger").
                if db.execute("""SELECT 1 FROM journal_headers j JOIN settlements s
                        ON s.id=j.source_id AND s.tenant_id=j.tenant_id AND s.source_type='bank_import'
                    WHERE j.tenant_id=%s AND j.source_type='settlement' AND s.source_id=%s AND j.status='reversed'
                    LIMIT 1""", (user["tenant_id"], candidate["bank_transaction_id"])).fetchone():
                    raise HTTPException(409, "The payment for this match was posted and then reversed, so this retry "
                                             "posts nothing. Post the payment again from the bank line if you mean to.")
                try:
                    payment = _post_matched_payment(db, user, candidate["bank_transaction_id"], data.journalDate)
                except RaiseException as error:
                    raise HTTPException(409, error.diag.message_primary) from error
                settlement = _existing_bank_settlement(db, {"id": candidate["bank_transaction_id"],
                                                            "tenant_id": user["tenant_id"]})
                return {"id": None, "candidateId": str(candidate_id), "decision": "confirmed", "createdAt": None,
                        "settlementId": str(settlement["id"]) if settlement else None,
                        "alreadyConfirmed": True, "payment": payment}
        if data.decision == "confirmed" and not direction_matches(candidate['category'], candidate['direction']):
            raise HTTPException(422, "Money-in transactions must match receivables; money-out transactions must match payables.")
        if data.decision == "confirmed" and candidate["candidate_status"] in {"proposed", "superseded"}:
            # Bug f: the record was already paid by a typed payment (for example a payment voucher), so its cash is
            # in the ledger. Confirming reconciles the line against that payment: no allocation, no new journal.
            # A retry finds the same reconciliation and returns it.
            record_id = candidate["record_id"]
            payer = _record_payers(db, user["tenant_id"], [record_id]).get(record_id)
            remaining = _record_remaining(db, user["tenant_id"], [record_id])[record_id]["remaining"] if payer else None
            if payer and remaining < abs(candidate["signed_amount_decimal"]):
                bank = _lock_statement_line(db, user["tenant_id"], candidate["bank_transaction_id"])
                try:
                    reconciliation = _reconcile_with_document(db, user, bank, payer["id"])
                except RaiseException as error:
                    raise HTTPException(409, error.diag.message_primary) from error
                if not reconciliation["idempotent"]:
                    _bank_audit(db, user, "bank.match_confirmed_as_reconciliation", candidate_id,
                                {"recordId": record_id, "documentId": payer["id"]})
                linked = {"id": None, "candidateId": str(candidate_id), "decision": "confirmed", "createdAt": None,
                          "settlementId": None, "reconciliation": reconciliation}
                if data.postPayment:
                    linked.update(alreadyConfirmed=reconciliation["idempotent"], payment=None)
                return linked
        if data.decision == "confirmed" and candidate["bank_status"] in {"imported", "proposed"}:
            bank = {"id": candidate["bank_transaction_id"], "tenant_id": user["tenant_id"]}
            settlement = _existing_bank_settlement(db, bank, lock=True)
            if settlement and 0 < settlement_allocated(db, user["tenant_id"], settlement["id"]) < settlement["amount"]:
                raise HTTPException(409, "Finish the partial match by allocating the remaining bank amount.")
        try:
            row = db.execute("""INSERT INTO reconciliation_decisions
                (tenant_id,candidate_id,decision,reviewer_id,reason,expected_revision)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                (user["tenant_id"], candidate_id, data.decision, user["id"], data.reason, data.expectedRevision)).fetchone()
        except RaiseException as error:
            # The database guard's own sentence, such as a currency or amount mismatch, tells the accountant why.
            raise HTTPException(409, str(error).split('\n')[0])
        _bank_audit(db, user, 'bank.match_' + data.decision, candidate_id, {'decision': data.decision, 'reason': data.reason})
        settlement_id = None
        if data.decision == 'confirmed':
            settlement = db.execute("""INSERT INTO settlements
                (tenant_id,settlement_ref,settlement_date,direction,amount,currency,source_type,source_id,created_by)
                VALUES (%s,%s,%s,%s,%s,%s,'bank_import',%s,%s)
                ON CONFLICT (tenant_id,source_type,source_id) WHERE source_type='bank_import' AND source_id IS NOT NULL
                DO UPDATE SET source_id=EXCLUDED.source_id
                RETURNING id""", (user["tenant_id"], f"BANK-{candidate['bank_transaction_id']}", candidate["posted_date"], candidate["direction"], abs(candidate["signed_amount_decimal"]), candidate["currency"], candidate["bank_transaction_id"], user["id"])).fetchone()
            settlement_id = str(settlement["id"])
            existing_allocation = db.execute("""SELECT a.id FROM settlement_allocations a
                WHERE a.tenant_id=%s AND a.settlement_id=%s AND a.record_id=%s AND a.status='active'
                  AND NOT EXISTS (SELECT 1 FROM settlement_allocations reversal
                    WHERE reversal.tenant_id=a.tenant_id AND reversal.reverses_allocation_id=a.id)""",
                (user["tenant_id"], settlement["id"], candidate["record_id"])).fetchone()
            if not existing_allocation:
                try:
                    db.execute("""INSERT INTO settlement_allocations
                        (tenant_id,settlement_id,record_id,amount,created_by)
                        VALUES (%s,%s,%s,%s,%s)""", (user["tenant_id"], settlement["id"], candidate["record_id"], abs(candidate["signed_amount_decimal"]), user["id"]))
                except RaiseException as error:
                    # A database rule refused the allocation. Say which, instead of the generic "account service
                    # unavailable" a database error would produce; the common case is a record already settled
                    # (for example by a payment voucher issued earlier).
                    reason = error.diag.message_primary or ""
                    if reason.startswith("Record allocation exceeds"):
                        raise HTTPException(409, "This bill is already settled by an earlier payment, so this bank "
                                                 "line cannot be matched to it again. Reject the match, or match the "
                                                 "line to another record.") from error
                    raise HTTPException(409, f"This bank line cannot be matched to that record: {reason}") from error
        elif data.decision == 'reversed':
            settlement = db.execute("""SELECT id FROM settlements
                WHERE tenant_id=%s AND source_type='bank_import' AND source_id=%s FOR UPDATE""", (user["tenant_id"], candidate["bank_transaction_id"])).fetchone()
            if settlement:
                require_no_posted_settlement_journal(db, user["tenant_id"], settlement["id"])
                settlement_id = str(settlement["id"])
                allocation = db.execute("""SELECT a.id,a.amount FROM settlement_allocations a
                    WHERE a.tenant_id=%s AND a.settlement_id=%s AND a.record_id=%s AND a.status='active'
                      AND NOT EXISTS (SELECT 1 FROM settlement_allocations reversal
                        WHERE reversal.tenant_id=a.tenant_id AND reversal.reverses_allocation_id=a.id)
                    ORDER BY a.created_at DESC FOR UPDATE OF a""",
                    (user["tenant_id"], settlement["id"], candidate["record_id"])).fetchone()
                if allocation:
                    db.execute("""INSERT INTO settlement_allocations
                        (tenant_id,settlement_id,record_id,amount,status,created_by,reversed_at,reversal_reason,reverses_allocation_id)
                        VALUES (%s,%s,%s,%s,'reversed',%s,now(),%s,%s)""", (user["tenant_id"], settlement["id"], candidate["record_id"], allocation["amount"], user["id"], data.reason, allocation["id"]))
        payment = None
        if data.postPayment:
            # Same transaction as the match: a refusal (closed period, missing account) leaves nothing confirmed.
            try:
                payment = _post_matched_payment(db, user, candidate["bank_transaction_id"], data.journalDate)
            except RaiseException as error:
                raise HTTPException(409, error.diag.message_primary) from error
    result = {"id": str(row["id"]), "candidateId": str(row["candidate_id"]),
              "decision": row["decision"], "createdAt": str(row["confirmed_at"]), "settlementId": settlement_id}
    if data.postPayment:
        result.update(alreadyConfirmed=False, payment=payment)
    return result

def decide_reconciliation_candidate(candidate_id: str, data: BankMatchDecisionInput, request: Request):
    data = data.model_copy(update={"candidateId": candidate_id})
    return decide_reconciliation(data, request)


def preview_bank_payment(transaction_id: str, request: Request, candidateId: str | None = None,
                         journalDate: date | None = None):
    """The payment journal a local admin would post for this bank line, without writing anything.

    With candidateId (a proposed full match), it previews "Confirm and post payment"; without it, the line's
    matched records that are not posted yet. canPost=false carries the refusal posting would give (closed period)."""
    user = require_workspace_user(request, {"local_admin", "accountant"})
    tenant_id = user["tenant_id"]
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    with connect() as db:
        bank = db.execute("""SELECT t.*,i.source_bank_account_ref FROM bank_transactions t
            JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
            WHERE t.id=%s AND t.tenant_id=%s AND i.status='confirmed'""", (transaction_id, tenant_id)).fetchone()
        if not bank:
            raise HTTPException(404, "Bank transaction not found.")
        if candidateId:
            candidate = db.execute("""SELECT c.status,c.record_id,r.record_code,r.document_id,r.category FROM reconciliation_candidates c
                JOIN standardised_records r ON r.id=c.record_id AND r.tenant_id=c.tenant_id
                WHERE c.id=%s AND c.tenant_id=%s AND c.bank_transaction_id=%s""",
                (_uuid_or_422(candidateId, "candidate id"), tenant_id, transaction_id)).fetchone()
            if not candidate:
                raise HTTPException(404, "Reconciliation candidate not found.")
            payer = _record_payers(db, tenant_id, [candidate["record_id"]]).get(candidate["record_id"])
            if payer and _record_remaining(db, tenant_id, [candidate["record_id"]])[candidate["record_id"]]["remaining"] \
                    < abs(bank["signed_amount_decimal"] or Decimal(0)):
                number = payer["document_number"]
                return {"canPost": False, "blockedReason": f"{candidate['record_code']} is already paid by {number}, "
                        f"which posted this payment. Confirming the match reconciles this line against {number}; "
                        "no new journal is posted.", "journalDate": None, "periodCode": None, "lines": [],
                        "settledBy": _payer_dto(payer)}
            settlement = _existing_bank_settlement(db, bank)
            if (candidate["status"] != "proposed" or bank["status"] not in {"imported", "proposed"}
                    or (settlement and settlement_allocated(db, tenant_id, settlement["id"]))):
                # Confirm and post would refuse (decided candidate, matched or partially allocated line).
                return {"canPost": False, "blockedReason": "This suggested match can no longer be confirmed as a "
                        "full match. Finish or undo the existing allocations first.",
                        "journalDate": None, "periodCode": None, "lines": []}
            pending = [{"id": None, "amount": abs(bank["signed_amount_decimal"] or Decimal(0)),
                        "document_id": candidate["document_id"], "category": candidate["category"]}]
        else:
            settlement = _existing_bank_settlement(db, bank)
            pending = unposted_bank_allocations(db, tenant_id, settlement["id"] if settlement else None)
        if not pending or bank["status"] == "ignored" or bank["recorded_document_id"]:
            return {"canPost": False, "blockedReason": "Nothing on this bank line is waiting to be posted.",
                    "journalDate": None, "periodCode": None, "lines": []}
        preview = bank_payment_preview(db, tenant_id, bank, pending, journalDate,
                                       lambda: _statement_catalog_account(db, bank))
    if preview["canPost"] and user["role"] != "local_admin":
        preview.update(canPost=False, blockedReason="Only a local admin can post payments to the ledger.")
    return preview

def get_bank_transaction(transaction_id: str, request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    with connect() as db:
        _ensure_bank_anomaly_table(db)
        row = db.execute(BANK_TRANSACTION_READ + " WHERE t.id=%s AND (%s::uuid IS NULL OR t.tenant_id=%s)",
            (transaction_id, tenant_scope(user), tenant_scope(user))).fetchone()
        if not row:
            raise HTTPException(404, "Bank transaction not found.")
        memory = _match_memory(db, row["tenant_id"])
        result = _bank_transaction_response(db, row, {}, user, memory)
        # Posted typed payments that already recorded this line's cash, each with why it is suggested.
        open_line = (row["status"] in {"imported", "proposed"} and not row["allocated"] and not row["recorded_document_id"])
        result["documentSuggestions"] = (_document_suggestions(db, row, memory, _statement_account_id(db, row))
                                         if open_line else [])
        # The payable account the matched bill was posted to: the supplier payment must debit it.
        payable = db.execute("""SELECT l.account_id FROM reconciliation_candidates c
            JOIN standardised_records r ON r.id=c.record_id AND r.tenant_id=c.tenant_id
            JOIN journal_headers j ON j.tenant_id=r.tenant_id AND j.source_type='document' AND j.source_id=r.document_id AND j.status='posted'
            JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id AND l.credit>0
            JOIN ledger_accounts a ON a.id=l.account_id AND a.tenant_id=l.tenant_id AND a.account_type='liability'
            WHERE c.bank_transaction_id=%s AND c.tenant_id=%s AND c.status='confirmed'
            ORDER BY j.posted_at DESC, l.line_number LIMIT 1""", (row["id"], row["tenant_id"])).fetchone()
    result.update({"billPayableAccountId": str(payable["account_id"]) if payable else None})
    return result

def ignore_bank_transaction(transaction_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    with connect() as db:
        bank = db.execute("""SELECT * FROM bank_transactions
            WHERE id=%s AND tenant_id=%s AND status IN ('imported','needs_review') FOR UPDATE""",
            (transaction_id, user["tenant_id"])).fetchone()
        if not bank:
            raise HTTPException(409, "Only an unmatched imported or review-required transaction can be ignored.")
        settlement = _existing_bank_settlement(db, bank, lock=True)
        if settlement and settlement_allocated(db, user["tenant_id"], settlement["id"]):
            raise HTTPException(409, "Undo the existing allocations before ignoring this bank transaction.")
        db.execute("UPDATE bank_transactions SET status='ignored' WHERE id=%s AND tenant_id=%s",
                   (transaction_id, user["tenant_id"]))
    return {"id": str(transaction_id), "status": "ignored"}

def code_bank_transaction(transaction_id: str, data: BankCodingInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    account_id = _uuid_or_422(data.accountId, "account id") if data.accountId else None
    cost_centre_id = _uuid_or_422(data.costCentreId, "cost centre id") if data.costCentreId else None
    suggestion_id = _uuid_or_422(data.suggestionId, "suggestion id") if data.suggestionId else None
    if data.decision != "cleared" and not account_id:
        raise HTTPException(422, "accountId is required unless the coding is being cleared.")
    decision, reason = data.decision, data.reason
    if decision in ("accepted", "overridden") and not suggestion_id:
        # The review screen sends "accepted" when the reviewer picks an account with no suggestion; without a
        # suggestion that is a manual choice (the table requires a suggestion for accepted/overridden rows).
        decision, reason = "manual", reason or "Account chosen by the reviewer"
    if decision != "accepted" and not reason:
        raise HTTPException(422, "A reason is required for overridden, manual, or cleared coding.")
    with connect() as db:
        transaction = db.execute("SELECT * FROM bank_transactions WHERE id=%s AND tenant_id=%s FOR UPDATE",
                                 (transaction_id, user["tenant_id"])).fetchone()
        if not transaction:
            raise HTTPException(404, "Bank transaction not found in this workspace.")
        if decision != "cleared":
            if transaction["recorded_document_id"]:
                raise HTTPException(409, "This bank transaction has already been recorded as a money document.")
            if transaction["status"] in {"imported", "needs_review", "proposed"}:
                settlement = _existing_bank_settlement(db, transaction, lock=True)
                if settlement and 0 < settlement_allocated(db, user["tenant_id"], settlement["id"]) < settlement["amount"]:
                    raise HTTPException(409, "Undo the partial match before coding this bank transaction.")
        if account_id:
            account = db.execute("SELECT id FROM coa_accounts WHERE id=%s AND tenant_id=%s AND is_active", (account_id, user["tenant_id"])).fetchone()
            if not account:
                raise HTTPException(422, "The selected account is inactive or outside this workspace.")
        if cost_centre_id:
            centre = db.execute("SELECT id FROM cost_centres WHERE id=%s AND tenant_id=%s AND is_active", (cost_centre_id, user["tenant_id"])).fetchone()
            if not centre:
                raise HTTPException(422, "The selected cost centre is inactive or outside this workspace.")
        row = db.execute("""INSERT INTO bank_coding_decisions
            (tenant_id,bank_transaction_id,suggestion_id,account_id,cost_centre_id,user_id,decision,reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (user["tenant_id"], transaction_id, suggestion_id, account_id, cost_centre_id,
             user["id"], decision, reason)).fetchone()
    return {"id": str(row["id"]), "bankTransactionId": str(row["bank_transaction_id"]),
            "accountId": str(row["account_id"]) if row["account_id"] else None,
            "costCentreId": str(row["cost_centre_id"]) if row["cost_centre_id"] else None,
            "decision": row["decision"], "reason": row["reason"]}

def clear_bank_transaction_coding(transaction_id: str, request: Request):
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    return code_bank_transaction(transaction_id, BankCodingInput(decision="cleared", reason="Coding cleared by reviewer"), request)
"""Bank API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/bank/transactions', list_bank_transactions, methods=['GET'])
    router.add_api_route('/api/bank/imports', list_bank_imports, methods=['GET'])
    router.add_api_route('/api/bank/imports', create_bank_import, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank-imports/{import_id}/confirm', confirm_bank_import, methods=['POST'])
    router.add_api_route('/api/bank-imports/{import_id}/discard', discard_bank_import, methods=['POST'])
    router.add_api_route('/api/bank/reconciliation/candidates', create_reconciliation_candidate, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/match-search', search_bank_match_records, methods=['GET'])
    router.add_api_route('/api/bank/transactions/{transaction_id}/match-targets', search_bank_match_targets, methods=['GET'])
    router.add_api_route('/api/bank/transactions/{transaction_id}/match-many', match_bank_transaction_many, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/reconcile-document', reconcile_bank_document, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/reconcile-document/undo', undo_bank_document_reconciliation, methods=['POST'])
    router.add_api_route('/api/bank/reconciliation/suggestions', list_reconciliation_suggestions, methods=['GET'])
    router.add_api_route('/api/bank/reconciliation/decisions', decide_reconciliation, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/reconciliation/candidates/{candidate_id}/decision', decide_reconciliation_candidate, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}', get_bank_transaction, methods=['GET'])
    router.add_api_route('/api/bank/transactions/{transaction_id}', ignore_bank_transaction, methods=['DELETE'])
    router.add_api_route('/api/bank/transactions/{transaction_id}/coding', code_bank_transaction, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/record-expense', record_bank_expense, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/post-payment', post_bank_payment, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/payment-preview', preview_bank_payment, methods=['GET'])
    router.add_api_route('/api/bank-transactions/{transaction_id}/coding', code_bank_transaction, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/coding', clear_bank_transaction_coding, methods=['DELETE'])
    router.add_api_route('/api/bank-transactions/{transaction_id}/coding', clear_bank_transaction_coding, methods=['DELETE'])
    router.add_api_route('/api/bank/reconciliation/periods', list_reconciliation_periods, methods=['GET'])
    router.add_api_route('/api/bank/reconciliation/periods', create_reconciliation_period, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/reconciliation/periods/{period_id}/complete', complete_reconciliation_period, methods=['POST'])
    router.add_api_route('/api/bank/reconciliation/periods/{period_id}/reopen', reopen_reconciliation_period, methods=['POST'])
