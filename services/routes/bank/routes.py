from __future__ import annotations
from decimal import Decimal
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto
import hashlib
import json
from pathlib import Path
from fastapi.concurrency import run_in_threadpool
from psycopg.errors import RaiseException, UniqueViolation
from pydantic import ValidationError
from services.bank.statement_parser import inspect_statement, parse_bank_statement
from validation.matching.bank_reconciliation import find_bank_match_candidates, direction_matches
from validation.transactions.transaction_anomaly import detect_transaction_anomalies

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
        detector_version TEXT NOT NULL DEFAULT 'amount-v1',
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
          AND b.import_id<>%s
          AND i.status='confirmed'
          AND b.status<>'ignored'
          AND b.direction=%s
          AND b.currency=%s
          AND i.source_bank_account_ref=%s
          AND b.posted_date IS NOT NULL
        ORDER BY b.posted_date, b.source_row
    """, (row['tenant_id'], row['import_id'], direction, currency, bank_account_ref)).fetchall()

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
    else:
        label = 'Insufficient history'
        reason = f"There are not enough prior {direction_label} transactions to establish a reliable range."

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
         baseline_median,upper_bound,baseline_count,anomaly_score)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (bank_transaction_id,tenant_id) DO UPDATE SET
          status=EXCLUDED.status,is_anomaly=EXCLUDED.is_anomaly,label=EXCLUDED.label,
          reason=EXCLUDED.reason,amount=EXCLUDED.amount,currency=EXCLUDED.currency,
          baseline_median=EXCLUDED.baseline_median,upper_bound=EXCLUDED.upper_bound,
          baseline_count=EXCLUDED.baseline_count,anomaly_score=EXCLUDED.anomaly_score,
          detector_version=EXCLUDED.detector_version,calculated_at=EXCLUDED.calculated_at""",
        (row['id'], row['tenant_id'], row['import_id'], result['status'], result['isAnomaly'],
         result['label'], result['reason'], result['amount'], result['currency'],
         result['baselineMedian'], result['upperBound'], result['baselineCount'], result['anomalyScore']))
    return result


def _bank_transaction_anomaly(db, row, bank_account_ref):
    """Return the saved, explainable amount anomaly for one bank row.

    New imports are saved during confirmation. The calculate-and-save fallback
    backfills legacy rows once, so list loads never recalculate an existing result.
    """
    _ensure_bank_anomaly_table(db)
    saved = db.execute("""SELECT status,is_anomaly,label,reason,amount,currency,
        baseline_median,upper_bound,baseline_count,anomaly_score
        FROM bank_transaction_anomalies
        WHERE bank_transaction_id=%s AND tenant_id=%s""", (row['id'], row['tenant_id'])).fetchone()
    if saved:
        return {
            'status': saved['status'], 'isAnomaly': saved['is_anomaly'], 'label': saved['label'],
            'reason': saved['reason'], 'amount': str(saved['amount']), 'currency': saved['currency'],
            'baselineMedian': str(saved['baseline_median']) if saved['baseline_median'] is not None else None,
            'upperBound': str(saved['upper_bound']) if saved['upper_bound'] is not None else None,
            'baselineCount': saved['baseline_count'],
            'anomalyScore': str(saved['anomaly_score']) if saved['anomaly_score'] is not None else None,
        }
    return _save_bank_transaction_anomaly(db, row, _calculate_bank_transaction_anomaly(db, row, bank_account_ref))


def _bank_transaction_response(db, row):
    result = _bank_transaction_dto(row)
    coding = db.execute("""SELECT c.account_id, c.cost_centre_id, c.decision, c.reason
        FROM bank_coding_decisions c
        WHERE c.bank_transaction_id=%s AND c.tenant_id=%s
        ORDER BY c.created_at DESC, c.id DESC LIMIT 1""", (row['id'], row['tenant_id'])).fetchone()
    candidates = db.execute("""SELECT c.id, c.record_id, c.status, c.reason_codes,
            c.amount_delta_decimal, c.date_delta_days, c.score_components,
            r.record_code, r.document_id, r.revision_number, r.category,
            r.reference_number, r.supplier_name, r.buyer_name, r.currency, r.total_amount,
            d.document_code
        FROM reconciliation_candidates c
        JOIN standardised_records r ON r.id=c.record_id AND r.tenant_id=c.tenant_id
        LEFT JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
        WHERE c.bank_transaction_id=%s AND c.tenant_id=%s
        ORDER BY c.created_at DESC, c.id DESC""", (row['id'], row['tenant_id'])).fetchall()
    mapped = [{"id": str(c['id']), "recordId": str(c['record_id']),
        "recordCode": c['record_code'], "documentId": str(c['document_id']) if c['document_id'] else None,
        "documentCode": c['document_code'], "revision": c['revision_number'],
        "category": c['category'], "reference": c['reference_number'],
        "party": c['supplier_name'] or c['buyer_name'], "currency": c['currency'],
        "amount": str(c['total_amount']) if c['total_amount'] is not None else None,
        "amountDelta": str(c['amount_delta_decimal']) if c['amount_delta_decimal'] is not None else '0.00',
        "dateDeltaDays": c['date_delta_days'], "reasons": c['reason_codes'] or [],
        "evidence": c['score_components'] or {}, "status": c['status']} for c in candidates]
    confirmed = next((c for c in mapped if c['status'] == 'confirmed'), None)
    proposed = next((c for c in mapped if c['status'] == 'proposed'), None)
    import_row = db.execute("SELECT source_bank_account_ref FROM bank_imports WHERE id=%s AND tenant_id=%s",
                            (row['import_id'], row['tenant_id'])).fetchone()
    result.update({"matchStatus": {"matched": "matched", "proposed": "proposed", "ignored": "ignored"}.get(row['status'], "unmatched"),
        "anomaly": _bank_transaction_anomaly(db, row, import_row['source_bank_account_ref'] if import_row else None),
        "accountId": str(coding['account_id']) if coding and coding['account_id'] else None,
        "costCentreId": str(coding['cost_centre_id']) if coding and coding['cost_centre_id'] else None,
        "codingDecision": coding['decision'] if coding else None,
        "codingReason": coding['reason'] if coding else None,
        "bankAccountRef": import_row['source_bank_account_ref'] if import_row else None,
        "relatedRecordId": confirmed['recordId'] if confirmed else None,
        "relatedRecordCode": confirmed['recordCode'] if confirmed else None,
        "relatedDocumentId": confirmed['documentId'] if confirmed else None,
        "relatedDocumentCode": confirmed['documentCode'] if confirmed else None,
        "amountDelta": confirmed['amountDelta'] if confirmed else None,
        "candidateId": proposed['id'] if proposed else None,
        "expectedRevision": proposed['revision'] if proposed else None,
        "candidates": mapped})
    return result

def list_bank_transactions(request: Request, direction: str | None = None, status: str | None = None):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_scope(user)
    if direction and direction not in {"money_in", "money_out"}:
        raise HTTPException(422, "direction must be money_in or money_out.")
    if status and status not in {"imported", "needs_review", "proposed", "matched", "ignored"}:
        raise HTTPException(422, "Invalid bank transaction status.")
    with connect() as db:
        # A statement still in preview is not on the Money pages until someone confirms it.
        rows = db.execute("""SELECT t.* FROM bank_transactions t JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
            WHERE i.status='confirmed' AND (%s::uuid IS NULL OR t.tenant_id=%s::uuid) AND (%s::text IS NULL OR t.direction=%s::text) AND (%s::text IS NULL OR t.status=%s::text)
            ORDER BY t.posted_date DESC NULLS LAST, t.source_row DESC""", (scope, scope, direction, direction, status, status)).fetchall()
    with connect() as db:
        return [_bank_transaction_response(db, row) for row in rows]

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


def _propose_candidates(db, tenant_id, transaction_id, row, records):
    """Exact candidates for one imported row. Each still needs a person to confirm it."""
    proposed = 0
    transaction = {'transaction_id': str(transaction_id), 'amount': row['amount'], 'signed_amount_decimal': row['signed'],
                   'direction': row['direction'], 'currency': row['currency'], 'reference': row['reference']}
    for record in records:
        match = find_bank_match_candidates(dict(record), [transaction])
        if match['status'] != 'MATCHED':
            continue
        candidate = db.execute("""INSERT INTO reconciliation_candidates
            (tenant_id,bank_transaction_id,record_id,reason_codes,proposed_by)
            VALUES (%s,%s,%s,%s,'rules') ON CONFLICT (bank_transaction_id,record_id) DO NOTHING
            RETURNING id""", (tenant_id, transaction_id, record['record_id'],
            Jsonb(['EXACT_AMOUNT', 'CURRENCY_MATCH', 'REFERENCE_MATCH', 'DIRECTION_MATCH']))).fetchone()
        if candidate:
            proposed += 1
    return proposed


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
        content = await upload.read()
        if not content or len(content) > MAX_STATEMENT_BYTES:
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
            if status == 'imported':
                stored_row = db.execute("SELECT * FROM bank_transactions WHERE id=%s AND tenant_id=%s", (transaction_id, user['tenant_id'])).fetchone()
                _save_bank_transaction_anomaly(db, stored_row, _calculate_bank_transaction_anomaly(db, stored_row, data.sourceBankAccountRef.strip()))
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

def search_bank_match_records(transaction_id: str, request: Request, q: str = ""):
    """Return approved records eligible for a manually selected bank match."""
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    with connect() as db:
        bank = db.execute("SELECT * FROM bank_transactions WHERE id=%s AND tenant_id=%s", (transaction_id, user["tenant_id"])).fetchone()
        if not bank: raise HTTPException(404, "Bank transaction not found.")
        term = f"%{q.strip()}%"
        rows = db.execute("""SELECT r.id,r.record_code,r.document_id,r.category,r.supplier_name,r.buyer_name,
            COALESCE(r.reference_number,r.invoice_number) reference_number,r.invoice_number,r.currency,r.total_amount,
            COALESCE((SELECT SUM(a.amount) FROM settlement_allocations a JOIN settlements s ON s.id=a.settlement_id
                WHERE a.record_id=r.id AND a.tenant_id=r.tenant_id AND a.status='active'),0) allocated_amount
            FROM standardised_records r WHERE r.tenant_id=%s AND r.is_current=true AND r.validation_status='passed'
              AND (%s='' OR r.record_code ILIKE %s OR COALESCE(r.reference_number,'') ILIKE %s OR COALESCE(r.invoice_number,'') ILIKE %s
                   OR COALESCE(r.supplier_name,r.buyer_name,'') ILIKE %s)
            ORDER BY r.created_at DESC LIMIT 50""", (user["tenant_id"], q.strip(), term, term, term, term)).fetchall()
    return [{"recordId": str(r["id"]), "recordCode": r["record_code"], "documentId": str(r["document_id"]), "category": r["category"], "party": r["supplier_name"] or r["buyer_name"], "reference": r["reference_number"], "invoiceNumber": r["invoice_number"], "currency": r["currency"], "totalAmount": str(r["total_amount"]) if r["total_amount"] is not None else None, "allocatedAmount": str(r["allocated_amount"]), "remainingAmount": str((r["total_amount"] or Decimal('0')) - r["allocated_amount"])} for r in rows]

def list_reconciliation_suggestions(request: Request):
    """Suggest exact reference/amount/currency matches for human review."""
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        records = db.execute("""SELECT id AS record_id, document_id, category, COALESCE(reference_number, invoice_number) AS reference_number,
                total_amount, currency, revision_number
            FROM standardised_records
            WHERE tenant_id=%s AND is_current=true AND validation_status='passed'
            ORDER BY created_at, id""", (user["tenant_id"],)).fetchall()
        transactions = db.execute("""SELECT id AS transaction_id, abs(signed_amount_decimal) AS amount,
                signed_amount_decimal, reference, currency, direction, status
            FROM bank_transactions WHERE tenant_id=%s AND status='imported'
            ORDER BY posted_date, source_row""", (user["tenant_id"],)).fetchall()
    output = []
    for record in records:
        match = find_bank_match_candidates(dict(record), [dict(row) for row in transactions])
        if match['status'] == 'UNMATCHED':
            continue
        output.append({"recordId": str(record['record_id']), "documentId": str(record['document_id']),
            "revision": record['revision_number'], "category": record['category'],
            "referenceNumber": record['reference_number'],
            "totalAmount": str(record['total_amount']) if record['total_amount'] is not None else None,
            "currency": record['currency'], "status": match['status'],
            "bankTransactionIds": [str(value) for value in match['candidates']],
            "reason": match['reason']})
    return {"suggestions": output, "humanConfirmationRequired": True}

def decide_reconciliation(data: ReconciliationDecisionInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if not user.get("tenant_id"):
        raise HTTPException(403, "A company workspace is required.")
    try:
        candidate_id = uuid.UUID(data.candidateId)
    except ValueError:
        raise HTTPException(422, "Invalid candidate id.")
    if data.decision != "confirmed" and not data.reason:
        raise HTTPException(422, "A reason is required when rejecting or reversing a match.")
    with connect() as db:
        candidate = db.execute("""SELECT b.id bank_transaction_id,c.record_id,b.direction,b.posted_date,b.signed_amount_decimal,b.currency, r.category
            FROM reconciliation_candidates c
            JOIN bank_transactions b ON b.id=c.bank_transaction_id AND b.tenant_id=c.tenant_id
            JOIN standardised_records r ON r.id=c.record_id AND r.tenant_id=c.tenant_id
            WHERE c.id=%s AND c.tenant_id=%s""", (candidate_id, user["tenant_id"])).fetchone()
        if not candidate:
            raise HTTPException(404, "Reconciliation candidate not found.")
        if data.decision == "confirmed" and not direction_matches(candidate['category'], candidate['direction']):
            raise HTTPException(422, "Money-in transactions must match receivables; money-out transactions must match payables.")
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
            existing_allocation = db.execute("""SELECT id FROM settlement_allocations
                WHERE tenant_id=%s AND settlement_id=%s AND record_id=%s AND status='active'""", (user["tenant_id"], settlement["id"], candidate["record_id"])).fetchone()
            if not existing_allocation:
                db.execute("""INSERT INTO settlement_allocations
                    (tenant_id,settlement_id,record_id,amount,created_by)
                    VALUES (%s,%s,%s,%s,%s)""", (user["tenant_id"], settlement["id"], candidate["record_id"], abs(candidate["signed_amount_decimal"]), user["id"]))
        elif data.decision == 'reversed':
            settlement = db.execute("""SELECT id FROM settlements
                WHERE tenant_id=%s AND source_type='bank_import' AND source_id=%s""", (user["tenant_id"], candidate["bank_transaction_id"])).fetchone()
            if settlement:
                settlement_id = str(settlement["id"])
                allocation = db.execute("""SELECT id,amount FROM settlement_allocations
                    WHERE tenant_id=%s AND settlement_id=%s AND record_id=%s AND status='active'
                    ORDER BY created_at DESC FOR UPDATE""", (user["tenant_id"], settlement["id"], candidate["record_id"])).fetchone()
                if allocation:
                    db.execute("""INSERT INTO settlement_allocations
                        (tenant_id,settlement_id,record_id,amount,status,created_by,reversed_at,reversal_reason,reverses_allocation_id)
                        VALUES (%s,%s,%s,%s,'reversed',%s,now(),%s,%s)""", (user["tenant_id"], settlement["id"], candidate["record_id"], allocation["amount"], user["id"], data.reason, allocation["id"]))
    return {"id": str(row["id"]), "candidateId": str(row["candidate_id"]),
            "decision": row["decision"], "createdAt": str(row["confirmed_at"]), "settlementId": settlement_id}

def decide_reconciliation_candidate(candidate_id: str, data: ReconciliationDecisionInput, request: Request):
    data = data.model_copy(update={"candidateId": candidate_id})
    return decide_reconciliation(data, request)

def get_bank_transaction(transaction_id: str, request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    with connect() as db:
        row = db.execute("""SELECT b.*, c.account_id, c.cost_centre_id, c.decision AS coding_decision,
                   c.reason AS coding_reason
            FROM bank_transactions b
            LEFT JOIN LATERAL (
                SELECT account_id, cost_centre_id, decision, reason
                FROM bank_coding_decisions
                WHERE bank_transaction_id=b.id AND tenant_id=b.tenant_id
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) c ON TRUE
            WHERE b.id=%s AND (%s IS NULL OR b.tenant_id=%s)""",
            (transaction_id, tenant_scope(user), tenant_scope(user))).fetchone()
    if not row:
        raise HTTPException(404, "Bank transaction not found.")
    with connect() as db:
        result = _bank_transaction_response(db, row)
    result.update({"accountId": str(row["account_id"]) if row["account_id"] else None,
                   "costCentreId": str(row["cost_centre_id"]) if row["cost_centre_id"] else None,
                   "codingDecision": row["coding_decision"], "codingReason": row["coding_reason"]})
    return result

def ignore_bank_transaction(transaction_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    with connect() as db:
        row = db.execute("""UPDATE bank_transactions SET status='ignored'
            WHERE id=%s AND tenant_id=%s AND status IN ('imported','needs_review') RETURNING *""",
            (transaction_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(409, "Only an unmatched imported or review-required transaction can be ignored.")
    return {"id": str(row["id"]), "status": "ignored"}

def code_bank_transaction(transaction_id: str, data: BankCodingInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    transaction_id = _uuid_or_422(transaction_id, "transaction id")
    account_id = _uuid_or_422(data.accountId, "account id") if data.accountId else None
    cost_centre_id = _uuid_or_422(data.costCentreId, "cost centre id") if data.costCentreId else None
    suggestion_id = _uuid_or_422(data.suggestionId, "suggestion id") if data.suggestionId else None
    if data.decision != "cleared" and not account_id:
        raise HTTPException(422, "accountId is required unless the coding is being cleared.")
    if data.decision != "accepted" and not data.reason:
        raise HTTPException(422, "A reason is required for overridden, manual, or cleared coding.")
    with connect() as db:
        transaction = db.execute("SELECT id FROM bank_transactions WHERE id=%s AND tenant_id=%s", (transaction_id, user["tenant_id"])).fetchone()
        if not transaction:
            raise HTTPException(404, "Bank transaction not found in this workspace.")
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
             user["id"], data.decision, data.reason)).fetchone()
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
    router.add_api_route('/api/bank/reconciliation/suggestions', list_reconciliation_suggestions, methods=['GET'])
    router.add_api_route('/api/bank/reconciliation/decisions', decide_reconciliation, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/reconciliation/candidates/{candidate_id}/decision', decide_reconciliation_candidate, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}', get_bank_transaction, methods=['GET'])
    router.add_api_route('/api/bank/transactions/{transaction_id}', ignore_bank_transaction, methods=['DELETE'])
    router.add_api_route('/api/bank/transactions/{transaction_id}/coding', code_bank_transaction, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank-transactions/{transaction_id}/coding', code_bank_transaction, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/coding', clear_bank_transaction_coding, methods=['DELETE'])
    router.add_api_route('/api/bank-transactions/{transaction_id}/coding', clear_bank_transaction_coding, methods=['DELETE'])
    router.add_api_route('/api/bank/reconciliation/periods', list_reconciliation_periods, methods=['GET'])
    router.add_api_route('/api/bank/reconciliation/periods', create_reconciliation_period, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/reconciliation/periods/{period_id}/complete', complete_reconciliation_period, methods=['POST'])
    router.add_api_route('/api/bank/reconciliation/periods/{period_id}/reopen', reopen_reconciliation_period, methods=['POST'])
