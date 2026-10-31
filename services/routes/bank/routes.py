from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto
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
        rows = db.execute("""SELECT * FROM bank_transactions
            WHERE (%s::uuid IS NULL OR tenant_id=%s::uuid) AND (%s::text IS NULL OR direction=%s::text) AND (%s::text IS NULL OR status=%s::text)
            ORDER BY posted_date DESC NULLS LAST, source_row DESC""", (scope, scope, direction, direction, status, status)).fetchall()
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

def create_bank_import(data: BankImportInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if not user.get("tenant_id"):
        raise HTTPException(403, "A company workspace is required.")
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
        records = db.execute("""SELECT id AS record_id, document_id, category, reference_number,
                total_amount, currency, revision_number
            FROM standardised_records WHERE tenant_id=%s AND is_current=true
              AND validation_status='passed'""", (user["tenant_id"],)).fetchall()
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
                transaction = {'transaction_id': str(transaction_id), 'amount': amount,
                    'signed_amount_decimal': signed, 'direction': direction,
                    'currency': currency, 'reference': row.reference}
                for record in records:
                    match = find_bank_match_candidates(dict(record), [transaction])
                    if match['status'] != 'MATCHED':
                        continue
                    candidate = db.execute("""INSERT INTO reconciliation_candidates
                        (tenant_id,bank_transaction_id,record_id,reason_codes,proposed_by)
                        VALUES (%s,%s,%s,%s,'rules') ON CONFLICT (bank_transaction_id,record_id) DO NOTHING
                        RETURNING id""", (user['tenant_id'], transaction_id, record['record_id'],
                        Jsonb(['EXACT_AMOUNT', 'CURRENCY_MATCH', 'REFERENCE_MATCH', 'DIRECTION_MATCH']))).fetchone()
                    if candidate:
                        candidates_proposed += 1
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
        records = db.execute("""SELECT id AS record_id, document_id, category, reference_number,
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
        row = db.execute("""INSERT INTO reconciliation_decisions
            (tenant_id,candidate_id,decision,reviewer_id,reason,expected_revision)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            (user["tenant_id"], candidate_id, data.decision, user["id"], data.reason, data.expectedRevision)).fetchone()
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
    router.add_api_route('/api/bank/reconciliation/candidates', create_reconciliation_candidate, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/reconciliation/suggestions', list_reconciliation_suggestions, methods=['GET'])
    router.add_api_route('/api/bank/reconciliation/decisions', decide_reconciliation, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/reconciliation/candidates/{candidate_id}/decision', decide_reconciliation_candidate, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}', get_bank_transaction, methods=['GET'])
    router.add_api_route('/api/bank/transactions/{transaction_id}', ignore_bank_transaction, methods=['DELETE'])
    router.add_api_route('/api/bank/transactions/{transaction_id}/coding', code_bank_transaction, methods=['POST'], status_code=201)
    router.add_api_route('/api/bank/transactions/{transaction_id}/coding', clear_bank_transaction_coding, methods=['DELETE'])
