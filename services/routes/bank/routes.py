from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto
import hashlib
import json
from fastapi.concurrency import run_in_threadpool
from psycopg.errors import RaiseException, UniqueViolation
from pydantic import ValidationError
from services.bank.csv_parser import parse_bank_csv
from validation.matching.bank_reconciliation import find_bank_match_candidates, direction_matches

def _bank_transaction_response(db, row):
    result = _bank_transaction_dto(row)
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


def _preview_bank_statement(user, content, account_ref, mapping, date_format):
    """Rows wait as ignored evidence, unseen on the Money pages, until a person confirms the import."""
    try:
        rows, mapping_version = parse_bank_csv(content, mapping, date_format, user["tenant_id"], account_ref)
    except ValueError as error:
        raise HTTPException(422, str(error))
    source_hash = hashlib.sha256(content).hexdigest()
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
                VALUES (%s,%s,%s,%s,'csv',%s,%s,0,'previewed')""",
                (import_id, user["tenant_id"], account_ref, source_hash, mapping_version, user["id"]))
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
    """A CSV file from the Money pages starts a preview. Rows sent as JSON are imported directly."""
    user = await run_in_threadpool(require_workspace_user, request, {"local_admin", "accountant"})
    if not user.get("tenant_id"):
        raise HTTPException(403, "A company workspace is required.")
    if request.headers.get('content-type', '').startswith('multipart/form-data'):
        form = await request.form()
        upload = form.get('file')
        account_ref = str(form.get('source_bank_account_ref') or '').strip()
        if upload is None or not hasattr(upload, 'read'):
            raise HTTPException(422, "Choose a CSV bank statement.")
        if not account_ref or len(account_ref) > 200:
            raise HTTPException(422, "Give the bank account a label.")
        content = await upload.read()
        if not content or len(content) > MAX_STATEMENT_BYTES:
            raise HTTPException(422, "The statement must be a non-empty CSV of at most 5 MB.")
        try:
            mapping = json.loads(str(form.get('mapping') or ''))
        except ValueError:
            raise HTTPException(422, "CSV column mapping must be valid JSON.")
        return await run_in_threadpool(_preview_bank_statement, user, content, account_ref, mapping, str(form.get('date_format') or ''))
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
        candidate = db.execute("""SELECT b.direction, r.category
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
    return {"id": str(row["id"]), "candidateId": str(row["candidate_id"]),
            "decision": row["decision"], "createdAt": str(row["confirmed_at"])}

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
    router.add_api_route('/api/bank/reconciliation/suggestions', list_reconciliation_suggestions, methods=['GET'])
    router.add_api_route('/api/bank/reconciliation/decisions', decide_reconciliation, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/reconciliation/candidates/{candidate_id}/decision', decide_reconciliation_candidate, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}', get_bank_transaction, methods=['GET'])
    router.add_api_route('/api/bank/transactions/{transaction_id}', ignore_bank_transaction, methods=['DELETE'])
    router.add_api_route('/api/bank/transactions/{transaction_id}/coding', code_bank_transaction, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/coding', clear_bank_transaction_coding, methods=['DELETE'])
