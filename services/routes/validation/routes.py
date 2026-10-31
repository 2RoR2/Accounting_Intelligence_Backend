from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto

def list_exceptions(request: Request):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    with connect() as db:
        owner_filter = " AND EXISTS (SELECT 1 FROM documents d WHERE d.id=exceptions.document_id AND (d.owner_id=%s OR d.assigned_to=%s))" if user["role"] == "accountant" else ""
        params = [scope, scope]
        if user["role"] == "accountant":
            params.extend([user["id"], user["id"]])
        rows = db.execute(f"SELECT * FROM exceptions WHERE (%s IS NULL OR tenant_id=%s){owner_filter} ORDER BY created_at DESC", params).fetchall()
    return [{"id": str(r["id"]), "documentId": str(r["document_id"]), "tenantId": str(r["tenant_id"]), "type": r["type"], "severity": r["severity"].upper(), "assignedTo": str(r["assigned_to"]) if r["assigned_to"] else None, "status": r["status"].upper(), "message": r["message"], "note": r["note"] or "", "createdAt": str(r["created_at"]), "resolvedAt": str(r["resolved_at"] or "")} for r in rows]

def get_validation_rules(request: Request):
    # Accountants need to read the active validation policy to render the
    # review results; only local admins can change it (see the PUT endpoint).
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("SELECT require_supplier, require_invoice_number, validate_date, validate_total, check_duplicate, confidence_threshold, require_invoice_number_for_invoice, require_reference_for_bill, require_receipt_identifier FROM validation_rules WHERE tenant_id=%s", (user["tenant_id"],)).fetchone()
    if not row:
        return {"supplier": True, "invoice": True, "date": True, "total": True, "duplicate": True, "threshold": 80, "invoiceByType": True, "billReferenceByType": True, "receiptIdentifierByType": False}
    return {"supplier": row["require_supplier"], "invoice": row["require_invoice_number"], "date": row["validate_date"], "total": row["validate_total"], "duplicate": row["check_duplicate"], "threshold": float(row["confidence_threshold"]) * 100, "invoiceByType": row["require_invoice_number_for_invoice"], "billReferenceByType": row["require_reference_for_bill"], "receiptIdentifierByType": row["require_receipt_identifier"]}

def save_validation_rules(data: ValidationRulesInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        db.execute("""INSERT INTO validation_rules(tenant_id,require_supplier,require_invoice_number,validate_date,validate_total,check_duplicate,confidence_threshold,require_invoice_number_for_invoice,require_reference_for_bill,require_receipt_identifier)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id) DO UPDATE SET require_supplier=EXCLUDED.require_supplier, require_invoice_number=EXCLUDED.require_invoice_number, validate_date=EXCLUDED.validate_date, validate_total=EXCLUDED.validate_total, check_duplicate=EXCLUDED.check_duplicate, confidence_threshold=EXCLUDED.confidence_threshold, require_invoice_number_for_invoice=EXCLUDED.require_invoice_number_for_invoice, require_reference_for_bill=EXCLUDED.require_reference_for_bill, require_receipt_identifier=EXCLUDED.require_receipt_identifier, updated_at=now()""",
            (user["tenant_id"], data.supplier, data.invoice, data.date, data.total, data.duplicate, data.threshold / 100, data.invoiceByType, data.billReferenceByType, data.receiptIdentifierByType))
    return data.model_dump()

def assign_exception(exception_id: str, data: ExceptionAssignment, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        target = db.execute("SELECT id, name FROM users WHERE id=%s AND tenant_id=%s AND status='active'", (data.userId, user["tenant_id"])).fetchone()
        if not target:
            raise HTTPException(422, "Invalid assignment.")
        row = db.execute("""UPDATE exceptions SET assigned_to=%s, status='assigned'
            WHERE id=%s AND tenant_id=%s RETURNING *""", (data.userId, exception_id, user["tenant_id"])).fetchone()
        if not row:
            raise HTTPException(404, "Exception not found.")
    return {"id": str(row["id"]), "status": row["status"].upper(), "assignedTo": str(row["assigned_to"])}

def resolve_exception(exception_id: str, data: ExceptionUpdate, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if not data.note.strip():
        raise HTTPException(422, "A resolution note is required.")
    with connect() as db:
        row = db.execute("""UPDATE exceptions SET status='resolved', note=%s, resolved_at=now()
            WHERE id=%s AND tenant_id=%s AND (assigned_to=%s OR %s='local_admin') RETURNING *""",
            (data.note.strip(), exception_id, user["tenant_id"], user["id"], user["role"])).fetchone()
        if not row:
            raise HTTPException(404, "Exception not found or not assigned to you.")
    return {"id": str(row["id"]), "status": row["status"].upper(), "note": row["note"], "resolvedAt": str(row["resolved_at"])}
"""Validation API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/exceptions', list_exceptions, methods=['GET'])
    router.add_api_route('/api/validation-rules', get_validation_rules, methods=['GET'])
    router.add_api_route('/api/validation-rules', save_validation_rules, methods=['PUT'])
    router.add_api_route('/api/exceptions/{exception_id}/assign', assign_exception, methods=['PATCH'])
    router.add_api_route('/api/exceptions/{exception_id}/resolve', resolve_exception, methods=['PATCH'])
