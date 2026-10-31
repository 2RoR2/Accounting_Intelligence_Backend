from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto

def accounting_reports(request: Request):
    """Tenant-scoped accounting report summaries for accountants and local admins."""
    user = require_workspace_user(request, {"local_admin", "accountant"})
    tenant_id = user["tenant_id"]
    with connect() as db:
        totals = db.execute("""SELECT count(*) AS documents,
            coalesce(sum(total_amount),0) AS total_amount
            FROM standardised_records WHERE tenant_id=%s""", (tenant_id,)).fetchone()
        lines = db.execute("""SELECT count(*) AS items, coalesce(sum(amount),0) AS amount
            FROM line_items li JOIN documents d ON d.id=li.document_id
            WHERE d.tenant_id=%s""", (tenant_id,)).fetchone()
        reconciled = db.execute("SELECT count(*) AS n FROM document_reconciliations WHERE tenant_id=%s", (tenant_id,)).fetchone()["n"]
        exceptions = db.execute("SELECT count(*) AS n FROM exceptions WHERE tenant_id=%s AND status <> 'resolved'", (tenant_id,)).fetchone()["n"]
        audit = db.execute("SELECT count(*) AS n FROM audit_logs WHERE tenant_id=%s", (tenant_id,)).fetchone()["n"]
        documents = db.execute("""SELECT document_code AS reference, kind, status,
            uploaded_at AS date FROM documents WHERE tenant_id=%s ORDER BY uploaded_at DESC LIMIT 100""", (tenant_id,)).fetchall()
    amount = float(totals["total_amount"] or 0)
    item_amount = float(lines["amount"] or 0)
    # Only expose reports backed by data that this workspace currently owns.
    # Ledger posting, banking, currency conversion, and chart-of-accounts
    # reports are intentionally not presented until those capabilities exist.
    cards = [
        {"key": "transactions", "title": "Document Transaction Register", "count": int(lines["items"])},
        {"key": "audit", "title": "Audit Trail", "count": int(audit)},
    ]
    return {"cards": cards, "summary": {"documentCount": int(totals["documents"]), "totalAmount": amount, "lineAmount": item_amount, "openExceptions": int(exceptions)}, "documents": [{"reference": r["reference"], "kind": r["kind"].upper(), "status": r["status"].upper(), "date": str(r["date"])} for r in documents]}

def request_backup(data: BackupInput, request: Request):
    user = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO backup_runs(requested_by,backup_type) VALUES (%s,%s) RETURNING *", (user["id"], data.backupType)).fetchone()
    return {"id": str(row["id"]), "backupType": row["backup_type"], "status": row["status"]}

def list_backups(request: Request):
    user = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        rows = db.execute("SELECT * FROM backup_runs ORDER BY created_at DESC LIMIT 100").fetchall()
    return [{"id": str(r["id"]), "backupType": r["backup_type"], "status": r["status"], "requestedAt": str(r["created_at"]), "startedAt": str(r["started_at"]) if r["started_at"] else None, "finishedAt": str(r["completed_at"]) if r["completed_at"] else None, "error": r["error_message"]} for r in rows]

def get_backup(backup_id: str, request: Request):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("SELECT * FROM backup_runs WHERE id=%s", (backup_id,)).fetchone()
    if not row: raise HTTPException(404, "Backup run not found.")
    return {"id": str(row["id"]), "backupType": row["backup_type"], "status": row["status"], "requestedAt": str(row["created_at"]), "startedAt": str(row["started_at"]) if row["started_at"] else None, "finishedAt": str(row["completed_at"]) if row["completed_at"] else None, "error": row["error_message"]}

def create_export(data: ExportInput, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("INSERT INTO export_history(tenant_id,requested_by,resource,filters,format,status,completed_at) VALUES (%s,%s,%s,%s,%s,'completed',now()) RETURNING *", (user["tenant_id"], user["id"], data.resource, data.filters, data.format)).fetchone()
    return {"id": str(row["id"]), "resource": row["resource"], "format": row["format"], "status": row["status"], "rowCount": row["row_count"], "createdAt": str(row["created_at"])}

def list_exports(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM export_history WHERE tenant_id=%s ORDER BY created_at DESC", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "resource": r["resource"], "format": r["format"], "status": r["status"], "rowCount": r["row_count"], "createdAt": str(r["created_at"])} for r in rows]

def download_export(export_id: str, request: Request):
    # Keep the legacy XLSX route unambiguous if this dynamic route is reached
    # before it in FastAPI's route table.
    if export_id == "xlsx":
        return export_records_xlsx(request)
    user = require_workspace_user(request)
    with connect() as db:
        export = db.execute("SELECT * FROM export_history WHERE id=%s AND tenant_id=%s AND status='completed'", (export_id, user["tenant_id"])).fetchone()
        if not export:
            raise HTTPException(404, "Export not found.")
        if export["resource"] == "records":
            rows = db.execute("SELECT record_code,category,supplier_name,buyer_name,invoice_number,currency,total_amount,validation_status FROM standardised_records WHERE tenant_id=%s AND is_current ORDER BY created_at DESC", (user["tenant_id"],)).fetchall()
            headers = ["record_code", "category", "supplier_name", "buyer_name", "invoice_number", "currency", "total_amount", "validation_status"]
        elif export["resource"] == "documents":
            rows = db.execute("SELECT document_code,kind,original_filename,status FROM documents WHERE tenant_id=%s ORDER BY uploaded_at DESC", (user["tenant_id"],)).fetchall()
            headers = ["document_code", "kind", "original_filename", "status"]
        else:
            rows = db.execute("SELECT type,severity,status,message FROM exceptions WHERE tenant_id=%s ORDER BY created_at DESC", (user["tenant_id"],)).fetchall()
            headers = ["type", "severity", "status", "message"]
    import csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows([list(row) for row in rows])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="export-{export_id}.csv"'})

def export_records_xlsx(request: Request):
    user = require_workspace_user(request)
    try:
        from openpyxl import Workbook
        with connect() as db:
            rows = db.execute("""SELECT sr.record_code,sr.category,sr.supplier_name,sr.buyer_name,sr.invoice_number,sr.invoice_date,sr.due_date,sr.currency,sr.total_amount,sr.validation_status
                FROM standardised_records sr
                WHERE sr.tenant_id=%s AND sr.is_current
                ORDER BY sr.created_at DESC""", (user["tenant_id"],)).fetchall()
            workbook = Workbook(); sheet = workbook.active; sheet.title = "Approved records"
            headers = ["Record ID","Category","Supplier","Customer","Invoice/Reference","Issue date","Due date","Currency","Total","Validation"]
            sheet.append(headers)
            for row in rows: sheet.append(list(row))
            output = io.BytesIO(); workbook.save(output); output.seek(0)
            db.execute("INSERT INTO export_history(tenant_id,requested_by,resource,format,row_count,status,completed_at) VALUES (%s,%s,'records','xlsx',%s,'completed',now())", (user["tenant_id"], user["id"], len(rows)))
    except Exception:
        with connect() as db:
            db.execute("INSERT INTO export_history(tenant_id,requested_by,resource,format,row_count,status) VALUES (%s,%s,'records','xlsx',0,'failed')", (user["tenant_id"], user["id"]))
        raise HTTPException(503, "Excel export failed. Please retry the export.")
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=approved-records.xlsx"})
"""Reporting API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/reports/accounting', accounting_reports, methods=['GET'])
    router.add_api_route('/api/backups', request_backup, methods=['POST'], status_code=201)
    router.add_api_route('/api/backups', list_backups, methods=['GET'])
    router.add_api_route('/api/backups/{backup_id}', get_backup, methods=['GET'])
    router.add_api_route('/api/exports', create_export, methods=['POST'], status_code=201)
    router.add_api_route('/api/exports', list_exports, methods=['GET'])
    router.add_api_route('/api/exports/{export_id}/download', download_export, methods=['GET'])
    router.add_api_route('/api/exports/xlsx', export_records_xlsx, methods=['GET'])
