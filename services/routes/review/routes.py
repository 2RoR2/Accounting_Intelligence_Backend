from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto

def accountant_inbox(request: Request, status: str | None = None, assignedTo: str | None = None):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    scope = user["tenant_id"]
    with connect() as db:
        rows = db.execute("""SELECT d.*, w.priority, w.due_at
            FROM documents d LEFT JOIN workload_assignments w ON w.document_id=d.id
            WHERE d.tenant_id=%s AND (%s IS NULL OR d.status=%s) AND (%s IS NULL OR d.assigned_to=%s)
            ORDER BY CASE WHEN w.priority='urgent' THEN 0 WHEN w.priority='high' THEN 1 ELSE 2 END, d.uploaded_at DESC""",
            (scope, status, status, assignedTo, assignedTo)).fetchall()
    return [{"id": str(r["id"]), "code": r["document_code"], "name": r["original_filename"], "status": r["status"].upper(), "assignedTo": str(r["assigned_to"]) if r["assigned_to"] else None, "priority": r["priority"] or "normal", "dueAt": str(r["due_at"] or "")} for r in rows]

def create_accountant_batch(data: BatchInput, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    if not data.documentIds:
        raise HTTPException(422, "Select at least one document.")
    with connect() as db:
        valid = db.execute("SELECT id FROM documents WHERE tenant_id=%s AND id=ANY(%s::uuid[])", (user["tenant_id"], data.documentIds)).fetchall()
        if len(valid) != len(set(data.documentIds)):
            raise HTTPException(404, "One or more documents were not found in this workspace.")
        batch = db.execute("INSERT INTO document_batches(tenant_id,created_by,name,total_documents,status) VALUES (%s,%s,%s,%s,'queued') RETURNING *", (user["tenant_id"], user["id"], data.name, len(data.documentIds))).fetchone()
        for position, document_id in enumerate(data.documentIds, 1):
            db.execute("INSERT INTO batch_documents(batch_id,document_id,tenant_id,position) VALUES (%s,%s,%s,%s)", (batch["id"], document_id, user["tenant_id"], position))
    return {"id": str(batch["id"]), "name": batch["name"], "status": batch["status"].upper(), "totalDocuments": batch["total_documents"], "processedDocuments": 0}

def get_accountant_batch(batch_id: str, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        batch = db.execute("SELECT * FROM document_batches WHERE id=%s AND tenant_id=%s", (batch_id, user["tenant_id"])).fetchone()
        if not batch: raise HTTPException(404, "Batch not found.")
        items = db.execute("SELECT bd.*,d.document_code,d.original_filename FROM batch_documents bd JOIN documents d ON d.id=bd.document_id AND d.tenant_id=bd.tenant_id WHERE bd.batch_id=%s AND bd.tenant_id=%s ORDER BY bd.position", (batch_id, user["tenant_id"])).fetchall()
    return {"id": str(batch["id"]), "name": batch["name"], "status": batch["status"].upper(), "totalDocuments": batch["total_documents"], "processedDocuments": batch["processed_documents"], "failedDocuments": batch["failed_documents"], "documents": [{"id": str(r["document_id"]), "code": r["document_code"], "name": r["original_filename"], "status": r["status"].upper()} for r in items]}

def cancel_accountant_batch(batch_id: str, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        row = db.execute("UPDATE document_batches SET status='cancelled' WHERE id=%s AND tenant_id=%s AND status IN ('queued','processing') RETURNING id,status", (batch_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Pending batch not found or already completed.")
    return {"id": str(row["id"]), "status": row["status"].upper()}

def create_review_reminder(data: ReminderInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO review_reminders(tenant_id,document_id,assigned_to,remind_at,created_by) SELECT tenant_id,id,%s,%s,%s FROM documents WHERE id=%s AND tenant_id=%s RETURNING *", (data.assignedTo, data.remindAt, user["id"], data.documentId, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Document not found.")
    return {"id": str(row["id"]), "documentId": str(row["document_id"]), "remindAt": str(row["remind_at"])}

def list_notifications(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC", (user["id"],)).fetchall()
    return [{"id": str(r["id"]), "type": r["type"], "title": r["title"], "message": r["message"], "resource": r["resource"], "read": bool(r["read_at"]), "createdAt": str(r["created_at"])} for r in rows]

def mark_notification_read(notification_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("UPDATE notifications SET read_at=COALESCE(read_at,now()) WHERE id=%s AND user_id=%s RETURNING id,read_at", (notification_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Notification not found.")
    return {"id": str(row["id"]), "read": True, "readAt": str(row["read_at"])}

def list_workload(request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("""SELECT w.*,d.document_code,d.original_filename,u.name AS assignee_name
            FROM workload_assignments w JOIN documents d ON d.id=w.document_id
            JOIN users u ON u.id=w.assigned_to
            WHERE w.tenant_id=%s AND w.completed_at IS NULL ORDER BY w.due_at NULLS LAST,w.priority DESC""", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "documentId": str(r["document_id"]), "documentCode": r["document_code"], "filename": r["original_filename"], "assignedTo": str(r["assigned_to"]), "assigneeName": r["assignee_name"], "priority": r["priority"], "dueAt": str(r["due_at"] or "")} for r in rows]
"""Review API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/accountant/inbox', accountant_inbox, methods=['GET'])
    router.add_api_route('/api/accountant/batches', create_accountant_batch, methods=['POST'], status_code=201)
    router.add_api_route('/api/accountant/batches/{batch_id}', get_accountant_batch, methods=['GET'])
    router.add_api_route('/api/accountant/batches/{batch_id}/cancel', cancel_accountant_batch, methods=['POST'])
    router.add_api_route('/api/review-reminders', create_review_reminder, methods=['POST'], status_code=201)
    router.add_api_route('/api/notifications', list_notifications, methods=['GET'])
    router.add_api_route('/api/notifications/{notification_id}/read', mark_notification_read, methods=['PATCH'])
    router.add_api_route('/api/workload', list_workload, methods=['GET'])
