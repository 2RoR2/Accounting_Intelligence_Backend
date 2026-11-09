from __future__ import annotations
import json
from main import *

def _notify_tenant_users(db, tenant_id, actor_id, title, message, resource, roles=('local_admin', 'accountant')):
    users = db.execute("SELECT id FROM users WHERE tenant_id=%s AND role=ANY(%s::text[]) AND id<>%s", (tenant_id, list(roles), actor_id)).fetchall()
    for recipient in users:
        db.execute("INSERT INTO notifications(tenant_id,user_id,type,title,message,resource) VALUES (%s,%s,%s,%s,%s,%s)", (tenant_id, recipient['id'], 'document_workflow', title, message, resource))

def _raw_scalar(value):
    """Unwrap the list/object shape used by XML-to-JSON extraction payloads."""
    while isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        for key in ('_', 'value', 'text'):
            if value.get(key) is not None:
                return value[key]
    return value


def _find_raw_nodes(value, key):
    if isinstance(value, dict):
        for name, child in value.items():
            if name.casefold() == key.casefold():
                yield child
            yield from _find_raw_nodes(child, key)
    elif isinstance(value, list):
        for child in value:
            yield from _find_raw_nodes(child, key)


def _ubl_line_items(raw_json):
    """Recover real line items when an older extraction saved no line_items rows."""
    if isinstance(raw_json, str):
        try:
            raw_json = json.loads(raw_json)
        except Exception:
            return []
    if not isinstance(raw_json, dict):
        return []
    line_groups = list(_find_raw_nodes(raw_json, 'InvoiceLine'))
    lines = []
    for group in line_groups:
        if isinstance(group, list):
            lines.extend(group)
        elif isinstance(group, dict):
            lines.append(group)
    output = []
    for number, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            continue
        item = _raw_scalar(line.get('Item')) or {}
        price = _raw_scalar(line.get('Price')) or {}
        quantity = _raw_scalar(line.get('InvoicedQuantity'))
        amount = _raw_scalar(line.get('LineExtensionAmount'))
        description = _raw_scalar(item.get('Description')) if isinstance(item, dict) else None
        identifier = _raw_scalar(line.get('ID'))
        unit_price = _raw_scalar(price.get('PriceAmount')) if isinstance(price, dict) else None
        output.append({
            'id': str(identifier or number),
            'description': str(description) if description is not None else None,
            'quantity': str(quantity) if quantity is not None else None,
            'unit_price': str(unit_price) if unit_price is not None else None,
            'amount': str(amount) if amount is not None else None,
        })
    return output


def _display_filename(original_filename, detected_identifier=None):
    """Use the detected document number in the UI while preserving the upload extension."""
    original = Path(str(original_filename or "document")).name
    identifier = str(detected_identifier or "").strip()
    if not identifier or identifier.casefold() in {"invoice", "invoice number", "reference", "n/a", "na", "none", "null", "unknown", "-"}:
        return original
    safe = safe_component(identifier, "document")
    suffix = Path(original).suffix.lower()
    return f"{safe}{suffix}" if suffix else safe


from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto
from validation.matching.vendor_matching import find_vendor_match
from database.object_storage.object_store import local_storage_root, document_key, safe_component
from validation.documents.document_validation import validation_outcome
from validation.documents.references import accepted_reviews, REVIEW_ACTION
from services.extraction.contracts.accounting_contract import effective_value, json_safe
from services.extraction.contracts.currency_review import build_currency_review, decision_details
from starlette.background import BackgroundTask

# Every key the extractor persists must be correctable, under the key it is stored as.
CORRECTABLE_MONEY_FIELDS = frozenset({
    'subtotal', 'tax', 'discount', 'total', 'tax_amount', 'total_amount',
    'tendered_amount', 'change_given', 'amount_paid', 'outstanding_amount',
    'document_allowances', 'document_charges', 'prepaid_amount', 'rounding_amount',
    'total_excluding_tax', 'total_including_tax', 'amount_due',
    'allowances', 'charges', 'prepaid', 'rounding', 'payment_amount',
    'previous_balance', 'arrears', 'adjustment_total', 'credit_total', 'late_fee',
})
CORRECTABLE_FIELDS = CORRECTABLE_MONEY_FIELDS | frozenset({
    'supplier', 'seller', 'merchant', 'customer', 'buyer', 'invoice', 'reference', 'receipt',
    'date', 'due', 'currency',
    'supplier_name', 'buyer_name', 'invoice_number', 'reference_number',
    'supplier_address', 'customer_address', 'supplier_tax_id', 'customer_tax_id',
    'merchant_address', 'provider_address',
    'invoice_date', 'due_date',
    'account_number', 'billing_period', 'transaction_time', 'payment_method',
    'payment_terms', 'received_date', 'expense_category', 'payment_reference',
    'purchase_order_number', 'goods_receipt_reference',
    'bill_reference', 'receipt_number',
})

def list_documents(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    date: str | None = None,
    amount: float | None = None,
    business: str | None = None,
):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    clauses = ["(%s IS NULL OR d.tenant_id=%s)"]
    params: list[object] = [scope, scope]
    if user["role"] == "accountant":
        clauses.append("(d.owner_id=%s OR d.assigned_to=%s OR EXISTS (SELECT 1 FROM benchmark_demo_documents bd JOIN benchmark_demo_tenants bt ON bt.tenant_id=bd.tenant_id AND bt.enabled=true WHERE bd.tenant_id=d.tenant_id AND bd.document_id=d.id))")
        params.extend([user["id"], user["id"]])
    if q:
        pattern = f"%{q.strip()}%"
        clauses.append("""(
            lower(d.original_filename) LIKE lower(%s)
            OR lower(d.document_code) LIKE lower(%s)
            OR d.id::text LIKE lower(%s)
            OR lower(coalesce(sr.supplier_name, '')) LIKE lower(%s)
            OR lower(coalesce(sr.buyer_name, '')) LIKE lower(%s)
            OR lower(coalesce(sr.invoice_number, '')) LIKE lower(%s)
            OR EXISTS (
                SELECT 1 FROM field_predictions fp
                WHERE fp.document_id=d.id
                  AND lower(coalesce(fp.current_value, fp.normalized_value, fp.raw_value, '')) LIKE lower(%s)
            )
        )""")
        params.extend([pattern] * 7)
    if business:
        pattern = f"%{business.strip()}%"
        clauses.append("""(
            lower(coalesce(sr.supplier_name, '')) LIKE lower(%s)
            OR lower(coalesce(sr.buyer_name, '')) LIKE lower(%s)
            OR EXISTS (
                SELECT 1 FROM field_predictions fp
                WHERE fp.document_id=d.id
                  AND fp.field_key IN ('supplier', 'supplier_name', 'customer', 'buyer', 'buyer_name')
                  AND lower(coalesce(fp.current_value, fp.normalized_value, fp.raw_value, '')) LIKE lower(%s)
            )
        )""")
        params.extend([pattern] * 3)
    if status:
        clauses.append("d.status=%s")
        params.append(status.lower())
    if date:
        clauses.append("d.uploaded_at::date=%s")
        params.append(date)
    if amount is not None:
        clauses.append("""(
            sr.total_amount=%s
            OR EXISTS (
                SELECT 1 FROM field_predictions fp
                WHERE fp.document_id=d.id AND fp.field_key IN ('total', 'total_amount')
                  AND coalesce(fp.current_value, fp.normalized_value, fp.raw_value, '') ~ '^[^0-9-]*-?[0-9][0-9, .]*$'
                  AND NULLIF(regexp_replace(coalesce(fp.current_value, fp.normalized_value, fp.raw_value, ''), '[^0-9.-]', '', 'g'), '')::numeric=%s
            )
        )""")
        params.extend([amount, amount])
    with connect() as db:
        rows = db.execute(f"""
            SELECT d.*, sr.total_amount,
                   -- Before a record exists, the party is the extraction's current value. A contact's related documents depend on this.
                   COALESCE(sr.supplier_name, supplier_field.value) AS supplier_name,
                   COALESCE(sr.buyer_name, customer_field.value) AS buyer_name,
                   COALESCE(sr.due_date::text, due_field.current_value, due_field.normalized_value, due_field.raw_value) AS due_date_value,
                   COALESCE(sr.invoice_number, sr.reference_number, identifier_field.value) AS detected_identifier
            FROM documents d
            LEFT JOIN LATERAL (
                SELECT total_amount, supplier_name, buyer_name, due_date, invoice_number, reference_number
                FROM standardised_records
                WHERE document_id=d.id
                ORDER BY revision_number DESC
                LIMIT 1
            ) sr ON true
            LEFT JOIN LATERAL (
                SELECT current_value, normalized_value, raw_value
                FROM field_predictions
                WHERE document_id=d.id AND field_key IN ('due', 'due_date')
                ORDER BY created_at DESC
                LIMIT 1
            ) due_field ON true
            LEFT JOIN LATERAL (
                SELECT COALESCE(current_value, normalized_value) AS value FROM field_predictions
                WHERE document_id=d.id AND field_key='supplier' ORDER BY created_at DESC LIMIT 1
            ) supplier_field ON true
            LEFT JOIN LATERAL (
                SELECT COALESCE(current_value, normalized_value) AS value FROM field_predictions
                WHERE document_id=d.id AND field_key='customer' ORDER BY created_at DESC LIMIT 1
            ) customer_field ON true
            LEFT JOIN LATERAL (
                SELECT COALESCE(current_value, normalized_value, raw_value) AS value
                FROM field_predictions
                WHERE document_id=d.id
                  AND field_key IN ('invoice', 'invoice_number', 'reference', 'reference_number', 'bill_reference', 'receipt', 'receipt_number')
                ORDER BY created_at DESC
                LIMIT 1
            ) identifier_field ON true
            WHERE {' AND '.join(clauses)}
            ORDER BY d.uploaded_at DESC
        """, params).fetchall()
    return [{"id": str(r["id"]), "code": r["document_code"], "name": _display_filename(r["original_filename"], r["detected_identifier"]), "tenantId": str(r["tenant_id"]), "ownerId": str(r["owner_id"]), "assignedTo": str(r["assigned_to"]) if r["assigned_to"] else None, "kind": r["kind"].upper(), "category": r["category"].upper(), "status": r["status"].upper(), "mimeType": r["mime_type"], "fileSize": r["file_size_bytes"], "seconds": float(r["processing_seconds"]) if r["processing_seconds"] is not None else 0, "totalAmount": float(r["total_amount"]) if r["total_amount"] is not None else None, "supplier": r["supplier_name"], "customer": r["buyer_name"], "dueDate": r["due_date_value"], "createdAt": str(r["uploaded_at"]), "updatedAt": str(r["updated_at"])} for r in rows]

def get_document_detail(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        visibility = " AND (owner_id=%s OR assigned_to=%s OR EXISTS (SELECT 1 FROM benchmark_demo_documents bd JOIN benchmark_demo_tenants bt ON bt.tenant_id=bd.tenant_id AND bt.enabled=true WHERE bd.tenant_id=documents.tenant_id AND bd.document_id=documents.id))" if user["role"] == "accountant" else ""
        params = [document_id, tenant_scope(user), tenant_scope(user)]
        if user["role"] == "accountant":
            params.extend([user["id"], user["id"]])
        r = db.execute(f"SELECT * FROM documents WHERE id=%s AND (%s IS NULL OR tenant_id=%s){visibility}", params).fetchone()
    if not r:
        raise HTTPException(404, "Document not found.")
    return {"id": str(r["id"]), "code": r["document_code"], "name": r["original_filename"], "tenantId": str(r["tenant_id"]), "ownerId": str(r["owner_id"]), "assignedTo": str(r["assigned_to"]) if r["assigned_to"] else None, "kind": r["kind"].upper(), "category": r["category"].upper(), "status": r["status"].upper(), "mimeType": r["mime_type"], "fileSize": r["file_size_bytes"], "contentRevision": r["content_revision"], "createdAt": str(r["uploaded_at"]), "updatedAt": str(r["updated_at"])}

def get_document_file(document_id: str, request: Request):
    """Return an authorised private source file for preview/download."""
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        visibility = " AND (owner_id=%s OR assigned_to=%s OR EXISTS (SELECT 1 FROM benchmark_demo_documents bd JOIN benchmark_demo_tenants bt ON bt.tenant_id=bd.tenant_id AND bt.enabled=true WHERE bd.tenant_id=documents.tenant_id AND bd.document_id=documents.id))" if user["role"] == "accountant" else ""
        params = [document_id, tenant_scope(user), tenant_scope(user)]
        if user["role"] == "accountant":
            params.extend([user["id"], user["id"]])
        row = db.execute(f"SELECT storage_path, original_filename, mime_type FROM documents WHERE id=%s AND (%s IS NULL OR tenant_id=%s){visibility}", params).fetchone()
    if not row:
        raise HTTPException(404, "Document not found.")
    temporary = None
    if str(row["storage_path"]).startswith("s3://"):
        from database.object_storage.object_store import download
        temporary = download(str(row["storage_path"]))
        path = temporary
    else:
        path = Path(row["storage_path"]).resolve()
        if local_storage_root() not in path.parents:
            raise HTTPException(404, "The original document is unavailable.")
    if not path.is_file():
        raise HTTPException(404, "The original document is unavailable.")
    cleanup = BackgroundTask(temporary.unlink, missing_ok=True) if temporary else None
    return FileResponse(path, media_type=row["mime_type"] or "application/octet-stream", filename=row["original_filename"], content_disposition_type="inline", background=cleanup)

def update_document_status(document_id: str, status: str, request: Request):
    require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    raise HTTPException(405, "Direct document status changes are not permitted. Use the dedicated workflow action.")

def retry_document_processing(document_id: str, request: Request):
    from services.queue import attempts
    user = require_workspace_user(request, {'local_admin','accountant'})
    scheduled = None
    try:
        with connect() as db:
            _lock_workspace_document(db,document_id,user)
            db.execute("SELECT set_config('app.user_id',%s,true), set_config('app.status_reason',%s,true)",
                       (str(user['id']),'Explicit extraction retry'))
            scheduled = attempts.reserve_dispatch(db,document_id,str(user['tenant_id']),retry=True,defer=True)
            db.execute('''INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
                VALUES (%s,%s,%s,%s,'extraction.retry_requested',%s,%s)''',
                (user['tenant_id'],user['id'],user.get('name','Workspace user'),user['role'],
                 str(document_id),str(scheduled['id'])))
        # No SQL lock or uncommitted outbox reservation crosses the Redis call.
        queued = attempts.dispatch_scheduled(str(scheduled['id']))
        if queued['jobStatus']=='FAILED':
            raise attempts.AttemptRejected('DISPATCH_UNCONFIRMED')
    except attempts.AttemptRejected as exc:
        _processing_error(exc,document_id,str(scheduled['id']) if scheduled else None)
    return {'id':str(document_id),**queued}

def delete_document(document_id: str, request: Request):
    user = require_workspace_user(request, {'local_admin'})
    source = None
    with connect() as db:
        document = db.execute("SELECT * FROM documents WHERE id=%s AND tenant_id=%s FOR UPDATE", (document_id, user['tenant_id'])).fetchone()
        if not document:
            raise HTTPException(404, 'Document not found.')
        if document['status'] in {'validated','standardised','completed','in_review','corrected'}:
            raise HTTPException(409, 'Reviewed or standardised documents cannot be deleted.')
        if db.execute('SELECT 1 FROM raw_extractions WHERE document_id=%s LIMIT 1', (document_id,)).fetchone():
            raise HTTPException(409, 'Documents with extraction history cannot be deleted.')
        source = document.get('storage_path')
        db.execute("DELETE FROM pending_jobs WHERE document_id=%s", (document_id,))
        db.execute("DELETE FROM documents WHERE id=%s AND tenant_id=%s", (document_id, user['tenant_id']))
    if source:
        try:
            if str(source).startswith('s3://'):
                from database.object_storage.object_store import delete
                delete(str(source))
            else:
                Path(str(source)).unlink(missing_ok=True)
        except Exception:
            # Database deletion is complete; report cleanup failure without
            # exposing storage credentials or paths.
            raise HTTPException(503, 'Document deleted, but source cleanup failed.')
    return {'deleted': True, 'id': str(document_id)}

def list_records(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM standardised_records WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC", (scope, scope)).fetchall()
        output = []
        for r in rows:
            fields = db.execute("""SELECT fp.* FROM field_predictions fp
                JOIN raw_extractions re ON re.id=fp.extraction_id AND re.is_current=true
                WHERE fp.document_id=%s ORDER BY fp.field_key""", (r["document_id"],)).fetchall()
            items = db.execute("""SELECT li.* FROM line_items li
                JOIN raw_extractions re ON re.id=li.extraction_id AND re.is_current=true
                WHERE li.document_id=%s ORDER BY li.line_number""", (r["document_id"],)).fetchall()
            raw_extraction = db.execute("SELECT raw_json FROM raw_extractions WHERE document_id=%s AND is_current=true", (r["document_id"],)).fetchone()
            snapshot = r["snapshot"] or {}
            if isinstance(snapshot, str):
                try:
                    snapshot = json.loads(snapshot)
                except Exception:
                    snapshot = {}
            snapshot_items = snapshot.get("lineItems") or snapshot.get("line_items") or [] if isinstance(snapshot, dict) else []
            line_item_rows = [line_dto(item) for item in items] if items else (snapshot_items or _ubl_line_items(raw_extraction['raw_json'] if raw_extraction else {}))
            output.append({"id": str(r["id"]), "code": r["record_code"], "documentId": str(r["document_id"]), "tenantId": str(r["tenant_id"]), "category": r["category"].upper(), "validation": r["validation_status"].upper(), "status": "COMPLETED", "snapshot": snapshot,
                "supplier": r["supplier_name"], "customer": r["buyer_name"], "invoiceNumber": r["invoice_number"], "referenceNumber": r["reference_number"], "invoiceDate": str(r["invoice_date"]) if r["invoice_date"] else None, "dueDate": str(r["due_date"]) if r["due_date"] else None, "totalAmount": decimal_text(r["total_amount"]), "currency": r["currency"],
                # Keep the approved record columns in the list response. They are
                # the authoritative standardised values even when an older
                # extraction snapshot contains placeholders.
                "supplier": r["supplier_name"], "customer": r["buyer_name"],
                "invoiceNumber": r["invoice_number"], "referenceNumber": r["reference_number"],
                "invoiceDate": str(r["invoice_date"]) if r["invoice_date"] else None,
                "dueDate": str(r["due_date"]) if r["due_date"] else None,
                "totalAmount": decimal_text(r["total_amount"]), "currency": r["currency"],
                "extraction": {"documentId": str(r["document_id"]), "contentRevision": snapshot.get("contentRevision") if isinstance(snapshot, dict) else None,
                    "fields": [prediction_dto(field) for field in fields], "lineItems": line_item_rows, "raw": snapshot.get("extraction", {}) if isinstance(snapshot, dict) else {}}, "createdAt": str(r["created_at"])})
    return output

def record_metadata(request: Request, tenant_id: str | None = None):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        rows = db.execute("SELECT * FROM standardised_records WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC", (tenant_id, tenant_id)).fetchall()
    return [{"id": str(r["id"]), "documentId": str(r["document_id"]), "tenantId": str(r["tenant_id"]), "createdAt": str(r["created_at"]), "category": r["category"].upper(), "validation": r["validation_status"].upper(), "status": "COMPLETED"} for r in rows]

def get_record(record_id: str, request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_scope(user)
    with connect() as db:
        row = db.execute("SELECT * FROM standardised_records WHERE (record_code=%s OR id::text=%s) AND (%s IS NULL OR tenant_id=%s)", (record_id, record_id, scope, scope)).fetchone()
    if not row:
        raise HTTPException(404, "Standardised record not found.")
    return {"id": str(row["id"]), "code": row["record_code"], "documentId": str(row["document_id"]), "tenantId": str(row["tenant_id"]), "revision": row["revision_number"], "category": row["category"].upper(), "supplier": row["supplier_name"], "customer": row["buyer_name"], "invoiceNumber": row["invoice_number"], "referenceNumber": row["reference_number"], "invoiceDate": str(row["invoice_date"]) if row["invoice_date"] else None, "dueDate": str(row["due_date"]) if row["due_date"] else None, "totalAmount": decimal_text(row["total_amount"]), "validation": row["validation_status"].upper(), "snapshot": row["snapshot"], "createdAt": str(row["created_at"])}

def get_record_amendments(record_id: str, request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("""SELECT a.* FROM record_amendments a
            JOIN standardised_records r ON r.id=a.original_record_id
            WHERE (a.original_record_id=%s OR r.record_code=%s) AND (%s IS NULL OR a.tenant_id=%s)
            ORDER BY a.created_at DESC""", (record_id, record_id, scope, scope)).fetchall()
    return [{"id": str(r["id"]), "recordId": str(r["original_record_id"]), "replacementRecordId": str(r["replacement_record_id"]) if r["replacement_record_id"] else None, "status": r["status"].upper(), "reason": r["reason"], "createdAt": str(r["created_at"]), "approvedAt": str(r["approved_at"]) if r["approved_at"] else None} for r in rows]

def support_view(record_id: str, data: CompanyReviewInput, request: Request):
    user = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("SELECT * FROM standardised_records WHERE id=%s", (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Record not found.")
        db.execute("INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details) VALUES (%s,%s,%s,%s,'SUPPORT_VIEW_ACCESSED',%s,%s)", (row["tenant_id"], user["id"], user["name"], user["role"], row["record_code"], data.reason))
    return {"id": str(row["id"]), "documentId": str(row["document_id"]), "tenantId": str(row["tenant_id"]), "snapshot": row["snapshot"], "status": "COMPLETED"}

async def upload_document(request: Request, file: UploadFile = File(...), kind: str = "invoice", category: str = "accounts_payable"):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if kind not in {"invoice", "bill", "receipt", "credit_note", "debit_note"} or category not in {"accounts_payable", "accounts_receivable"}:
        raise HTTPException(422, "Invalid document kind or category.")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "This file exceeds the 20 MB limit. Choose a smaller file.")
    validate_uploaded_bytes(file.filename or "document", data)
    tenant_id = user["tenant_id"]
    if not tenant_id:
        raise HTTPException(422, "A tenant workspace is required.")
    file_hash = hashlib.sha256(data).hexdigest()
    # Never allow a client filename to create directories or escape private storage.
    original_filename = Path(file.filename or "document").name
    upload_id = uuid.uuid4()
    object_key = document_key(tenant_id, user["id"], user.get("name"), upload_id, original_filename)
    from database.object_storage.object_store import enabled as object_storage_enabled
    use_object_storage = object_storage_enabled()
    storage_path = None
    storage_uri = None
    if use_object_storage:
        from database.object_storage.object_store import put_bytes
        storage_uri = put_bytes(object_key, data, file.content_type or "application/octet-stream")
        storage_value = storage_uri
    else:
        storage_dir = local_storage_root()
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage_path = (storage_dir / Path(object_key)).resolve()
        if not storage_path.is_relative_to(storage_dir.resolve()):
            raise HTTPException(422, "Invalid storage destination.")
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_bytes(data)
        storage_value = str(storage_path)
    from services.queue import attempts
    try:
        with connect() as db:
            _lock_upload_identity(db,tenant_id,file_hash)
            duplicate = db.execute("SELECT id,document_code,original_filename FROM documents WHERE tenant_id=%s AND file_hash_sha256=%s ORDER BY uploaded_at,id LIMIT 1", (tenant_id, file_hash)).fetchone()
            if duplicate:
                raise HTTPException(409, {"message": "This document is already uploaded.", "existingDocumentId": str(duplicate["id"]), "existingDocumentCode": duplicate["document_code"], "existingFilename": duplicate["original_filename"]})
            row = db.execute("""INSERT INTO documents(id,tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,file_size_bytes,file_hash_sha256,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued') RETURNING *""",
                (upload_id, tenant_id, user["id"], original_filename, kind, category, storage_value, file.content_type or "application/octet-stream", len(data), file_hash)).fetchone()
            # Upload and durable scheduling commit together. Dispatch only when
            # worker capacity is available; waiting is not active extraction.
            job = attempts.schedule_document(db,str(row['id']),str(tenant_id))
    except Exception:
        if storage_path is not None:
            storage_path.unlink(missing_ok=True)
        if storage_uri:
            try:
                from database.object_storage.object_store import delete
                delete(storage_uri)
            except Exception:
                pass
        raise
    try:
        queued = attempts.dispatch_scheduled(str(job['id']))
    except attempts.AttemptRejected as exc:
        # The document and durable job remain stored. Include their IDs so the
        # caller can refresh/retry; do not encourage a duplicate upload.
        _processing_error(exc,row['id'],job['id'])
    return {"id":str(row["id"]),"code":row["document_code"],"name":row["original_filename"],
            "tenantId":str(row["tenant_id"]),"ownerId":str(row["owner_id"]),
            "kind":kind.upper(),"category":category.upper(),**queued}

def standardise_document(document_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        doc = db.execute("SELECT * FROM documents WHERE id=%s AND tenant_id=%s AND status='validated' FOR UPDATE", (document_id, user["tenant_id"])).fetchone()
        if not doc:
            raise HTTPException(422, "The document must be validated before standardising.")
        approved = db.execute("""SELECT 1 FROM review_decisions
            WHERE document_id=%s AND tenant_id=%s AND decision='approved'
              AND approved_content_revision=(SELECT content_revision FROM documents WHERE id=%s AND tenant_id=%s)
            LIMIT 1""", (document_id, user["tenant_id"], document_id, user["tenant_id"])).fetchone()
        if not approved:
            raise HTTPException(422, "A local administrator must approve the document before standardising.")
        current_validation = db.execute("""SELECT
            count(*) FILTER (WHERE status='failed' OR prevents_approval) AS failures,
            count(*) FILTER (WHERE status='not_checked' AND requires_review) AS unresolved_reviews,
            count(*) AS total
            FROM validation_results
            WHERE document_id=%s AND tenant_id=%s AND content_revision=(SELECT content_revision FROM documents WHERE id=%s AND tenant_id=%s)""",
            (document_id, user["tenant_id"], document_id, user["tenant_id"])).fetchone()
        if not current_validation or not current_validation["total"]:
            raise HTTPException(422, "The current document revision has not been validated.")
        if current_validation["failures"] or current_validation["unresolved_reviews"]:
            raise HTTPException(422, "The current document revision has failed or unresolved validation rules.")
        existing = db.execute("SELECT id FROM standardised_records WHERE document_id=%s", (document_id,)).fetchone()
        if existing:
            raise HTTPException(409, "This document already has a standardised record.")
        extraction = db.execute("SELECT * FROM raw_extractions WHERE document_id=%s AND is_current=true ORDER BY run_number DESC LIMIT 1", (document_id,)).fetchone()
        if not extraction:
            raise HTTPException(422, "The document has no current extraction.")
        fields = db.execute("SELECT * FROM field_predictions WHERE document_id=%s AND extraction_id=%s", (document_id, extraction['id'])).fetchall()
        items = db.execute("SELECT * FROM line_items WHERE document_id=%s AND extraction_id=%s ORDER BY line_number", (document_id, extraction['id'])).fetchall()
        try:
            values = record_values(fields)
            snapshot = approved_snapshot(doc, extraction, fields, items)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        snapshot['validationReviews'] = json_safe(list(accepted_reviews(db, doc).values()))
        if doc['kind'] == 'bill' and values.get('reference') is None:
            values['reference'] = values.get('bill_number') or values.get('bill_reference') or values.get('account_number')
        elif doc['kind'] == 'receipt' and values.get('reference') is None:
            values['reference'] = values.get('receipt_number') or values.get('receipt_reference')
        elif doc['kind'] == 'invoice' and values.get('reference') is None:
            values['reference'] = values.get('invoice_reference')
        if (extraction.get('raw_json') or {}).get('mode') == 'module2_public_result':
            coverage = evaluate_document(doc, extraction, fields, items, db)
            if coverage['errors']:
                raise HTTPException(422, {'message': 'Document validation requires correction.', 'errors': coverage['errors']})
            if not coverage['coverage_complete']:
                raise HTTPException(422, 'Type-specific validation is NOT_CHECKED; standardisation is unavailable.')
        if doc['kind'] == 'invoice':
            validation = evaluate_invoice(doc, extraction, fields, items, db)
            if validation['errors']:
                raise HTTPException(422, {'message': 'Invoice validation requires correction.', 'errors': validation['errors']})
        required_by_kind = {
            # AP buyer_name is nullable and absent from the M2 supplier-invoice
            # contract. AR retains the existing explicit customer requirement.
            "invoice": ["invoice", "supplier", "date", "currency", "total"]
                       + (["customer"] if doc['category'] == 'accounts_receivable' else []),
            "bill": ["supplier", "reference", "date", "currency", "total"],
            "receipt": ["supplier", "date", "currency", "total"],
            "credit_note": ["supplier", "reference", "date", "currency", "total"],
            "debit_note": ["supplier", "reference", "date", "currency", "total"],
        }
        rules = db.execute("""SELECT require_supplier, require_invoice_number_for_invoice,
            require_reference_for_bill, require_receipt_identifier
            FROM validation_rules WHERE tenant_id=%s""", (user["tenant_id"],)).fetchone()
        if rules:
            if not rules["require_supplier"]:
                required_by_kind["invoice"] = [k for k in required_by_kind["invoice"] if k != "supplier"]
                required_by_kind["bill"] = [k for k in required_by_kind["bill"] if k != "supplier"]
            if not rules["require_invoice_number_for_invoice"]:
                required_by_kind["invoice"].remove("invoice")
            if not rules["require_reference_for_bill"]:
                required_by_kind["bill"].remove("reference")
            if rules["require_receipt_identifier"]:
                required_by_kind["receipt"].append("reference")
        missing = [key for key in required_by_kind[doc["kind"]] if values.get(key) is None]
        if missing:
            raise HTTPException(422, f"Missing required {doc['kind'].replace('_', ' ')} fields: {', '.join(missing)}.")
        blocking = db.execute("SELECT 1 FROM exceptions WHERE document_id=%s AND tenant_id=%s AND status <> 'resolved' AND severity IN ('high','blocking') LIMIT 1", (document_id, user["tenant_id"])).fetchone()
        if blocking:
            raise HTTPException(422, "Blocking exceptions must be resolved before standardising this document.")
        vendor_id, vendor_confidence = None, None
        if values.get('supplier'):
            vendors = db.execute("SELECT id,vendor_name FROM vendors WHERE tenant_id=%s ORDER BY id",
                                 (user["tenant_id"],)).fetchall()
            matched_vendor = find_vendor_match(values['supplier'], vendors) if vendors else None
            if matched_vendor and matched_vendor['status'] == 'ACCEPT':
                vendor_id, vendor_confidence = matched_vendor['vendor_id'], matched_vendor['confidence']
        row = db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,vendor_id,vendor_match_confidence,supplier_name,invoice_number,reference_number,buyer_name,invoice_date,due_date,currency,total_amount,subtotal,tax_amount,discount,validation_status,snapshot,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::date,%s::date,%s,%s::numeric,%s::numeric,%s::numeric,%s::numeric,'passed',%s,%s) RETURNING *""",
            (document_id, user["tenant_id"], doc["category"], vendor_id, vendor_confidence,
             values.get('supplier'), values.get('invoice'), values.get('reference'),
             values.get("customer"), values.get("date"), values.get("due"), values.get("currency"),
             values.get("total"), values.get('subtotal'), values.get('tax'), values.get('discount'), Jsonb(snapshot), user["id"])).fetchone()
        db.execute("UPDATE documents SET status='completed', updated_at=now() WHERE id=%s", (document_id,))
    return {"id": str(row["id"]), "documentId": str(row["document_id"]), "tenantId": str(row["tenant_id"]), "category": row["category"].upper(), "validation": row["validation_status"].upper(), "status": "COMPLETED", "createdAt": str(row["created_at"])}

def get_document_extraction(document_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        ownership = ' AND (owner_id=%s OR assigned_to=%s)' if user['role'] == 'accountant' else ''
        params = [document_id, user['tenant_id']] + ([user['id'], user['id']] if ownership else [])
        doc = db.execute('SELECT id,content_revision FROM documents WHERE id=%s AND tenant_id=%s' + ownership, params).fetchone()
        if not doc:
            raise HTTPException(404, "Document not found.")
        extraction = db.execute("SELECT * FROM raw_extractions WHERE document_id=%s AND is_current=true ORDER BY run_number DESC LIMIT 1", (document_id,)).fetchone()
        fields = db.execute("SELECT * FROM field_predictions WHERE document_id=%s AND extraction_id=%s ORDER BY field_key", (document_id, extraction["id"] if extraction else None)).fetchall() if extraction else []
        items = db.execute("SELECT * FROM line_items WHERE document_id=%s AND extraction_id=%s ORDER BY line_number", (document_id, extraction["id"] if extraction else None)).fetchall() if extraction else []
        line_corrections = db.execute("""SELECT line_item_id,field_key,old_value,new_value,corrected_by,corrected_at
            FROM line_item_corrections WHERE document_id=%s ORDER BY corrected_at DESC""", (document_id,)).fetchall()
        currency_review = _currency_review(db, user['tenant_id'], document_id, fields)
    return {
        "documentId": document_id,
        "contentRevision": doc['content_revision'],
        "classification": ((extraction['raw_json'].get('document_type') or
            ({'value': extraction['raw_json']['predicted_document_type'], 'status': 'classified',
              'reason': 'STRUCTURED_SOURCE_TYPE_CODE', 'evidence': []}
             if extraction['raw_json'].get('predicted_document_type') in {'credit_note','debit_note'} else {})) if extraction else {}),
        "raw": extraction["raw_json"] if extraction else {},
        "warnings": extraction_guidance(extraction["raw_json"] if extraction else {}, items),
        "fields": [prediction_dto(row) for row in fields],
        "lineItems": [line_dto(row, corrections=line_corrections) for row in items],
        "lineCorrections": [{key: str(value) if value is not None else None for key, value in row.items()}
                            for row in line_corrections],
        "currencyReview": currency_review,
    }

def get_document_reviews(document_id: str, request: Request):
    user = _authorised_document(request, document_id)
    with connect() as db:
        submissions = db.execute("SELECT * FROM review_submissions WHERE document_id=%s AND tenant_id=%s ORDER BY created_at DESC", (document_id, user["tenant_id"])).fetchall()
        decisions = db.execute("SELECT * FROM review_decisions WHERE document_id=%s AND tenant_id=%s ORDER BY reviewed_at DESC", (document_id, user["tenant_id"])).fetchall()
    return {"submissions": [{"id": str(r["id"]), "submittedBy": str(r["submitted_by"]), "decision": r["decision"].upper(), "note": r["note"] or "", "createdAt": str(r["created_at"])} for r in submissions], "decisions": [{"id": str(r["id"]), "reviewedBy": str(r["reviewed_by"]), "decision": r["decision"].upper(), "note": r["note"] or "", "approvedRevision": r["approved_content_revision"], "createdAt": str(r["reviewed_at"])} for r in decisions]}

def get_document_validation(document_id: str, request: Request):
    user = _authorised_document(request, document_id)
    with connect() as db:
        doc, extraction, fields, items = load_current(db, document_id, user)
        validation = evaluate_document(doc, extraction, fields, items, db)
        stored_results = db.execute("""SELECT rule_id,rule_source,rule_version,document_type,status,severity,
            field_key,actual_value,expected_value,difference,prevents_approval,requires_review,message,created_at
            FROM validation_results WHERE document_id=%s AND tenant_id=%s AND content_revision=%s
            ORDER BY created_at, rule_id""", (document_id, user["tenant_id"], doc["content_revision"])).fetchall()
        exceptions = db.execute("SELECT id,type,severity,status,message,note,created_at,resolved_at FROM exceptions WHERE document_id=%s AND tenant_id=%s ORDER BY created_at", (document_id, user["tenant_id"])).fetchall()
    from validation.documents.document_validation import CAUSE_LABELS, exception_cause, read_status
    results = [{**e, 'cause': exception_cause(e['rule_id'], e['field_key'], e['message']) if e['status'] == 'failed' else None,
                'causeLabel': CAUSE_LABELS.get(exception_cause(e['rule_id'], e['field_key'], e['message'])) if e['status'] == 'failed' else None,
                'rule': e['rule_id'], 'ruleSource': e['rule_source'], 'ruleVersion': e['rule_version'],
                'documentType': e['document_type'], 'result': read_status(e), 'checkStatus': read_status(e),
                'field': e['field_key'], 'actualValue': e['actual_value'], 'expectedValue': e['expected_value'],
                'difference': e['difference'], 'preventsApproval': e['prevents_approval'],
                'requiresReview': e['requires_review'], 'message': e['message'], 'createdAt': str(e['created_at'])}
              for e in stored_results]
    if not results:
        results = [{**e, 'rule': e['code'], 'result': 'FAIL' if e.get('severity', 'high') == 'high' else 'WARNING',
                    'checkStatus': 'CHECKED'} for e in validation['errors']]
        results.extend(validation['checks'])
    external = [r for r in exceptions if not r['type'].startswith('VALIDATION:') and r['status'] != 'resolved']
    results.extend({'id': str(r['id']), 'rule': r['type'], 'result': 'FAIL' if r['severity'] == 'high' else 'WARNING', 'message': r['message']} for r in external)
    validation_status, blocking = validation_outcome(results, validation['errors'], external, validation['coverage_complete'])
    return {'documentId': document_id, 'contentRevision': doc['content_revision'], 'status': doc['status'].upper(),
            'validationStatus': validation_status, 'reviewRequired': True,
            'coverageComplete': validation['coverage_complete'],
            'validationPassed': validation.get('validation_passed', not validation['errors']),
            'notApplicable': sum(r['result'] == 'NOT_APPLICABLE' for r in results),
            'accountingApprovalPerformed': False, 'passed': int(not validation['errors'] and validation['coverage_complete']),
            'warnings': sum(r['result'] in {'WARNING', 'NOT_CHECKED'} for r in results), 'failed': sum(r['result'] in {'FAIL', 'FAILED'} for r in results),
            'blocking': blocking, 'results': results, 'vendorMatch': validation.get('vendor_match')}

def get_processing_history(document_id: str, request: Request):
    user = _authorised_document(request, document_id)
    with connect() as db:
        jobs = db.execute("SELECT id,status,attempt_count,max_attempts,last_error,created_at,finished_at FROM pending_jobs WHERE document_id=%s AND tenant_id=%s ORDER BY created_at DESC", (document_id, user["tenant_id"])).fetchall()
        history = db.execute("SELECT old_status,new_status,changed_by,reason,created_at FROM document_status_history WHERE document_id=%s AND tenant_id=%s ORDER BY created_at DESC", (document_id, user["tenant_id"])).fetchall()
    return {"jobs": [{"id": str(r["id"]), "status": r["status"].upper(), "attempts": r["attempt_count"], "maxAttempts": r["max_attempts"], "error": r["last_error"], "createdAt": str(r["created_at"]), "finishedAt": str(r["finished_at"]) if r["finished_at"] else None} for r in jobs], "statusHistory": [{"from": r["old_status"], "to": r["new_status"], "changedBy": str(r["changed_by"]) if r["changed_by"] else None, "reason": r["reason"], "createdAt": str(r["created_at"])} for r in history]}

CURRENCY_DECISION = 'currency.decision'


def _currency_review(db, tenant_id, document_id, fields):
    """Advice for the reviewer. Only a supplier already in this tenant's contacts counts as verified."""
    from validation.matching.vendor_matching import normalize_vendor_name
    if not any(row['field_key'] == 'currency' for row in fields):
        return None
    supplier = next((row for row in fields if row['field_key'] == 'supplier'), None)
    name = effective_value(supplier) if supplier else None
    wanted = normalize_vendor_name(name) if name else None
    supplier_record = None
    if wanted:
        vendors = db.execute("SELECT vendor_name,address FROM vendors WHERE tenant_id=%s", (tenant_id,)).fetchall()
        vendor = next((v for v in vendors if normalize_vendor_name(v['vendor_name']) == wanted), None)
        if vendor:
            approved = db.execute("""SELECT supplier_name,currency FROM standardised_records
                WHERE tenant_id=%s AND is_current AND document_id<>%s""", (tenant_id, document_id)).fetchall()
            counts = {}
            for record in approved:
                if record['supplier_name'] and normalize_vendor_name(record['supplier_name']) == wanted:
                    counts[record['currency']] = counts.get(record['currency'], 0) + 1
            supplier_record = {'name': vendor['vendor_name'], 'address': vendor['address'],
                               'records': [{'currency': code, 'count': count} for code, count in sorted(counts.items())]}
    review = build_currency_review(fields, supplier_record=supplier_record)
    row = db.execute("""SELECT details,created_at,user_name FROM audit_logs WHERE tenant_id=%s AND resource=%s AND action=%s
        ORDER BY created_at DESC LIMIT 1""", (tenant_id, str(document_id), CURRENCY_DECISION)).fetchone()
    if row:
        details = json.loads(row['details'])
        review['decision'] = {**details, 'by': row['user_name'], 'at': row['created_at'].isoformat()}
    return review


def correct_extracted_field(document_id: str, field_key: str, data: FieldCorrectionInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    # Accept existing API and M2 keys as stored; key translation belongs to the
    # extraction adapter. Never allow arbitrary prediction metadata to be edited.
    if field_key not in CORRECTABLE_FIELDS:
        raise HTTPException(422, "Unsupported extraction field.")
    if field_key in CORRECTABLE_MONEY_FIELDS:
        if not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', data.value):
            raise HTTPException(422, "Amounts must be finite decimal strings without currency prefixes.")
    elif field_key in {'date', 'due', 'invoice_date', 'due_date'}:
        try:
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', data.value):
                raise ValueError
            date.fromisoformat(data.value)
        except ValueError:
            raise HTTPException(422, "Dates must be valid YYYY-MM-DD values.")
    elif field_key == 'currency' and data.value not in CURRENCY_MINOR_UNITS:
        raise HTTPException(422, "Currency must be an assigned supported ISO 4217 code.")
    elif field_key == 'transaction_time':
        try:
            field_value('transaction_time', data.value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    ownership = " AND (d.owner_id=%s OR d.assigned_to=%s)" if user['role'] == 'accountant' else ""
    owner_params = [user['id'], user['id']] if ownership else []
    with connect() as db:
        document = db.execute(f"""SELECT d.id,d.content_revision,d.status FROM documents d
            WHERE d.id=%s AND d.tenant_id=%s{ownership} FOR UPDATE""",
            [document_id, user['tenant_id'], *owner_params]).fetchone()
        if not document:
            raise HTTPException(404, "Document not found.")
        if document['content_revision'] != data.expectedContentRevision:
            raise HTTPException(409, "The document changed. Refresh it before correcting this field.")
        if document['status'] in {'completed', 'standardised'}:
            raise HTTPException(409, "Standardised documents require an amendment.")
        decision = None
        if field_key == 'currency':
            # Saved in this transaction, so the value cannot change without its audit row.
            stored = db.execute("""SELECT fp.* FROM field_predictions fp JOIN raw_extractions re ON re.id=fp.extraction_id AND re.is_current=true
                WHERE fp.document_id=%s""", (document_id,)).fetchall()
            current = next((item for item in stored if item['field_key'] == 'currency'), None)
            decision = decision_details(_currency_review(db, user['tenant_id'], document_id, stored), data.value, current.get('current_value') if current else None)
        db.execute("SELECT set_config('app.user_id',%s,true), set_config('app.status_reason',%s,true)",
                   (str(user['id']), 'Extracted field corrected'))
        # Repeat tenant, visibility and revision guards in the write itself.
        # Preserve resolution/raw/provenance; the existing trigger appends history.
        row = db.execute(f"""UPDATE field_predictions AS fp
            SET current_value=%s, corrected_by=%s, corrected_at=now()
            FROM documents d JOIN raw_extractions re ON re.document_id=d.id AND re.is_current=true
            WHERE fp.document_id=d.id AND fp.extraction_id=re.id
              AND d.id=%s AND d.tenant_id=%s AND d.content_revision=%s
              AND d.status NOT IN ('completed','standardised'){ownership}
              AND fp.field_key=%s RETURNING fp.*""",
            [data.value, user['id'], document_id, user['tenant_id'], data.expectedContentRevision,
             *owner_params, field_key]).fetchone()
        if not row:
            raise HTTPException(404, "Extraction field not found.")
        revision = db.execute(f"""UPDATE documents AS d
            SET content_revision=content_revision+1,status='corrected',updated_at=now()
            WHERE d.id=%s AND d.tenant_id=%s AND d.content_revision=%s{ownership}
            RETURNING content_revision""",
            [document_id, user['tenant_id'], data.expectedContentRevision, *owner_params]).fetchone()
        if not revision:
            raise HTTPException(409, "The document changed. Refresh it before correcting this field.")
        if decision:
            db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (user['tenant_id'], user['id'], user.get('name', 'Workspace user'), user['role'],
                CURRENCY_DECISION, str(document_id), json.dumps(decision, default=str)))
        _notify_tenant_users(db, user['tenant_id'], user['id'], 'Document field corrected',
                             f"{document.get('document_code', 'A document')} was corrected and needs review.", f"documents/{document_id}")
    return {"key": row["field_key"], "value": row["current_value"], "correctedBy": str(row["corrected_by"]),
            "contentRevision": revision['content_revision']}

AI_ASSIST_ACTION = 'ai_assist.result'
AI_ASSIST_DECISION = 'ai_assist.decision'
AI_ASSIST_LIMIT_PER_10_MIN = 6


def _assist_document(db, document_id, user):
    ownership = " AND (d.owner_id=%s OR d.assigned_to=%s)" if user['role'] == 'accountant' else ""
    params = [document_id, user['tenant_id'], *([user['id'], user['id']] if ownership else [])]
    document = db.execute(f"SELECT d.id,d.status,d.kind,d.content_revision,d.tenant_id FROM documents d WHERE d.id=%s AND d.tenant_id=%s{ownership}", params).fetchone()
    if not document:
        raise HTTPException(404, "Document not found.")
    return document


def request_ai_assist(document_id: str, data: AIAssistInput, request: Request):
    """Local-model second pass. Advice only: applying a value uses the normal correction route."""
    from services.extraction.ai.ai_assist import AssistUnavailable, run_ai_assist
    from services.extraction.ai.qwen_provider import qwen_mode
    from services.queue.jobs import BACKEND_ROOT, resolve_document_from_database
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if qwen_mode() not in {'manual', 'both'}:
        raise HTTPException(409, "AI Assist is turned off on this installation. Continue reviewing manually.")
    with connect() as db:
        document = _assist_document(db, document_id, user)
        if document['status'] in {'completed', 'standardised', 'queued', 'processing'}:
            raise HTTPException(409, "AI Assist is available only while a document is in review.")
        recent = db.execute("""SELECT count(*) AS n FROM audit_logs WHERE tenant_id=%s AND resource=%s AND action=%s
            AND created_at > now() - interval '10 minutes'""", (user['tenant_id'], str(document_id), AI_ASSIST_ACTION)).fetchone()['n']
        if recent >= AI_ASSIST_LIMIT_PER_10_MIN:
            raise HTTPException(429, "AI Assist was used several times on this document just now. Try again in a few minutes.")
        extraction = db.execute("SELECT service_run_id FROM raw_extractions WHERE document_id=%s AND is_current=true", (document_id,)).fetchone()
        if not extraction or not extraction['service_run_id']:
            raise HTTPException(409, "This document has no current extraction to examine.")
        saved = db.execute("""SELECT fp.field_key,fp.current_value FROM field_predictions fp JOIN raw_extractions re ON re.id=fp.extraction_id
            WHERE fp.document_id=%s AND re.is_current=true AND fp.corrected_at IS NOT NULL""", (document_id,)).fetchall()
    corrected = {row['field_key']: row['current_value'] for row in saved}
    # Advice is computed with no database transaction held open during inference.
    try:
        source = resolve_document_from_database(document_id)
        if not source or not Path(source).is_file():
            raise AssistUnavailable('SOURCE_UNAVAILABLE', 'The original file is not available on this machine.')
        result = run_ai_assist(extraction['service_run_id'], source, data.fields, root=BACKEND_ROOT, current_values=_corrected_by_source_key(corrected))
    except AssistUnavailable as unavailable:
        result = {'version': 'ai-assist/1', 'status': unavailable.code, 'fields': [], 'message': unavailable.message}
    stored = {**result, 'content_revision': document['content_revision'], 'requested_fields': data.fields or []}
    with connect() as db:
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (user['tenant_id'], user['id'], user.get('name', 'Workspace user'), user['role'],
            AI_ASSIST_ACTION, str(document_id), json.dumps(stored, default=str)))
    return stored


def _corrected_by_source_key(corrected):
    from services.extraction.classification.route_extraction import RECOVERY_FIELDS
    from services.extraction.contracts.accounting_contract import FIELD_KEYS
    return {name: corrected[FIELD_KEYS.get(name, name)] for names in RECOVERY_FIELDS.values() for name in names
            if FIELD_KEYS.get(name, name) in corrected}


def get_ai_assist(document_id: str, request: Request):
    from services.extraction.ai.qwen_provider import qwen_mode
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        kind = _assist_document(db, document_id, user)['kind']
        row = db.execute("""SELECT details,created_at,user_name FROM audit_logs WHERE tenant_id=%s AND resource=%s AND action=%s
            ORDER BY created_at DESC LIMIT 1""", (user['tenant_id'], str(document_id), AI_ASSIST_ACTION)).fetchone()
        decisions = db.execute("""SELECT details,created_at,user_name FROM audit_logs WHERE tenant_id=%s AND resource=%s AND action=%s
            ORDER BY created_at""", (user['tenant_id'], str(document_id), AI_ASSIST_DECISION)).fetchall()
    from services.extraction.classification.route_extraction import RECOVERY_FIELDS
    from services.extraction.contracts.accounting_contract import FIELD_KEYS
    return {'available': qwen_mode() in {'manual', 'both'},
            'supportedFields': [{'field': name, 'storedKey': FIELD_KEYS.get(name, name)} for name in RECOVERY_FIELDS.get(kind, ())],
            'latest': (_assist_result(row, FIELD_KEYS) if row else None),
            'decisions': [{**json.loads(item['details']), 'by': item['user_name'], 'at': item['created_at'].isoformat()} for item in decisions]}


def _assist_result(row, field_keys):
    """Audit rows are permanent, so a result stored by an older version must still read."""
    result = json.loads(row['details'])
    for item in result.get('fields') or []:
        item.setdefault('stored_key', field_keys.get(item.get('field'), item.get('field')))
    return {**result, 'requestedBy': row['user_name'], 'requestedAt': row['created_at'].isoformat()}


def decide_ai_assist(document_id: str, data: AIAssistDecisionInput, request: Request):
    """Record the reviewer's decision for audit. A value changes only through the correction route."""
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        _assist_document(db, document_id, user)
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (user['tenant_id'], user['id'], user.get('name', 'Workspace user'), user['role'],
            AI_ASSIST_DECISION, str(document_id), json.dumps({'field': data.field, 'decision': data.decision, 'attempt_id': data.attemptId})))
    return {'recorded': True}


def correct_line_item(document_id: str, line_item_id: str, field_key: str, data: LineItemCorrectionInput, request: Request):
    user = require_workspace_user(request, {'local_admin', 'accountant'})
    if field_key not in {'description', 'quantity', 'unit_price', 'amount'}:
        raise HTTPException(422, 'Unsupported line-item field.')
    value = data.value
    if field_key != 'description':
        try:
            if not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', value):
                raise ValueError('Use a plain decimal string.')
            value = decimal_text(value, precision=14, scale={'quantity': 3, 'unit_price': 4, 'amount': 2}[field_key])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    with connect() as db:
        document = _lock_workspace_document(db, document_id, user)
        if document['content_revision'] != data.expectedContentRevision:
            raise HTTPException(409, 'The document changed. Refresh before correcting this line.')
        item = db.execute("""SELECT li.* FROM line_items li JOIN raw_extractions re
            ON re.id=li.extraction_id AND re.document_id=li.document_id AND re.is_current=true
            WHERE li.id=%s AND li.document_id=%s FOR UPDATE""", (line_item_id, document_id)).fetchone()
        if not item:
            raise HTTPException(404, 'Current extraction line item not found.')
        old_value = item[field_key]
        visibility = ' AND (d.owner_id=%s OR d.assigned_to=%s)' if user['role'] == 'accountant' else ''
        owners = [user['id'], user['id']] if visibility else []
        changed = db.execute(f"""UPDATE line_items AS li SET {field_key}=%s
            FROM documents d JOIN raw_extractions re ON re.document_id=d.id AND re.is_current=true
            WHERE li.document_id=d.id AND li.extraction_id=re.id AND li.id=%s
              AND d.id=%s AND d.tenant_id=%s AND d.content_revision=%s
              AND d.status NOT IN ('completed','standardised'){visibility} RETURNING li.*""",
            [value, line_item_id, document_id, user['tenant_id'], data.expectedContentRevision, *owners]).fetchone()
        if not changed:
            raise HTTPException(409, 'The document changed while correcting this line.')
        db.execute("SELECT set_config('app.user_id',%s,true), set_config('app.status_reason',%s,true)",
                   (str(user['id']), 'Extracted line item corrected'))
        revision = db.execute(f"""UPDATE documents AS d SET content_revision=content_revision+1,status='corrected',updated_at=now()
            WHERE d.id=%s AND d.tenant_id=%s AND d.content_revision=%s{visibility} RETURNING content_revision""",
            [document_id, user['tenant_id'], data.expectedContentRevision, *owners]).fetchone()
        if not revision:
            raise HTTPException(409, 'The document changed while correcting this line.')
        db.execute("""INSERT INTO line_item_corrections(line_item_id,document_id,field_key,old_value,new_value,corrected_by)
            VALUES (%s,%s,%s,%s,%s,%s)""", (line_item_id, document_id, field_key,
                str(old_value) if old_value is not None else None, value, user['id']))
        _notify_tenant_users(db, user['tenant_id'], user['id'], 'Line item corrected',
                             f"{document.get('document_code', 'A document')} has a corrected line item and needs review.", f"documents/{document_id}")
    return {'lineItemId': line_item_id, 'fieldKey': field_key, 'value': value,
            'correctedBy': str(user['id']), 'contentRevision': revision['content_revision']}

def request_record_amendment(record_id: str, data: RecordAmendmentInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("""INSERT INTO record_amendments(original_record_id,document_id,tenant_id,requested_by,reason)
            SELECT id,document_id,tenant_id,%s,%s FROM standardised_records
            WHERE id=%s AND tenant_id=%s RETURNING *""", (user["id"], data.reason.strip(), record_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Standardised record not found.")
    return {"id": str(row["id"]), "recordId": record_id, "status": row["status"], "createdAt": str(row["created_at"])}

class ValidationReviewInput(BaseModel):
    rule: str = Field(max_length=32)
    note: str = Field(max_length=4000)
    expectedContentRevision: int = Field(ge=1)


def accept_validation_review(document_id: str, data: ValidationReviewInput, request: Request):
    """An accountant settles a finding that only a person can settle, with a reason on the audit log.

    It applies to this exact revision. A later correction starts a new revision, so the finding returns.
    Local-admin approval is still required afterwards, and nothing here approves or standardises.
    """
    from validation.documents.document_validation import REVIEWABLE_RULES, evaluate_document, load_current
    user = require_workspace_user(request, {"local_admin", "accountant"})
    note = data.note.strip()
    if data.rule not in REVIEWABLE_RULES:
        raise HTTPException(422, "This finding cannot be settled by review.")
    if len(note) < 10:
        raise HTTPException(422, "Say why this is acceptable, in at least 10 characters.")
    with connect() as db:
        document = _lock_workspace_document(db, document_id, user)
        if document['status'] not in {'extracted', 'corrected', 'exception'}:
            raise HTTPException(409, "Only a document that has not been submitted can be reviewed here.")
        if data.expectedContentRevision != document['content_revision']:
            raise HTTPException(409, "The document changed. Refresh it before reviewing this finding.")
        doc, extraction, fields, items = load_current(db, document_id, user)
        live = evaluate_document(doc, extraction, fields, items, db)
        error = next((e for e in live['errors'] if e.get('code') == data.rule), None)
        open_check = next((c for c in live['checks'] if c.get('rule') == data.rule and c.get('checkStatus') == 'NOT_CHECKED' and c.get('required', True)), None)
        if not error and not open_check:
            raise HTTPException(409, "There is no open finding for this rule at this revision.")
        anomaly = (error or {}).get('transaction_anomaly') or {}
        details = {'rule': data.rule, 'note': note, 'content_revision': document['content_revision'],
                   **({key: anomaly.get(key) for key in ('amount', 'currency', 'upper_bound', 'baseline_median', 'baseline_count')} if anomaly else {})}
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (user['tenant_id'], user['id'], user.get('name', 'Workspace user'), user['role'],
            REVIEW_ACTION, str(document_id), json.dumps(details, default=str)))
        return validate_document(db, document_id, user)


def revalidate_document(document_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        return validate_document(db, document_id, user)

def save_document_draft(document_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("""UPDATE documents SET updated_at=now()
            WHERE id=%s AND tenant_id=%s AND status NOT IN ('completed','standardised')
            RETURNING id,status,updated_at""", (document_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Document cannot be saved as a draft.")
    return {"documentId": str(row["id"]), "status": row["status"].upper(), "savedAt": str(row["updated_at"])}

def submit_document_review(document_id: str, data: ReviewInput, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        document = _lock_workspace_document(db, document_id, user)
        _require_current_extraction(db, document_id)
        if document['status'] not in {'extracted','corrected','exception','in_review','validated'}:
            raise HTTPException(409, 'Only extracted documents can be submitted for review.')
        if data.expectedContentRevision is not None and data.expectedContentRevision != document["content_revision"]:
            raise HTTPException(409, "The document changed. Refresh it before submitting for approval.")
        if data.decision != "submitted":
            raise HTTPException(422, "Accountants submit documents for approval; only local administrators can decide them.")
        db.execute("INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,note,content_revision) VALUES (%s,%s,%s,%s,%s,(SELECT content_revision FROM documents WHERE id=%s))", (document_id, user["tenant_id"], user["id"], data.decision, data.note, document_id))
        _notify_tenant_users(db, user['tenant_id'], user['id'], 'Document submitted for approval',
                             f"{document.get('document_code', 'A document')} is ready for approval.", f"documents/{document_id}", roles=('local_admin',))
        next_status = "in_review"
        db.execute("UPDATE documents SET status=%s,updated_at=now() WHERE id=%s", (next_status, document_id))
    return {"documentId": document_id, "decision": data.decision, "status": next_status.upper()}

def decide_document_review(document_id: str, data: ReviewDecisionInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        document = _lock_workspace_document(db, document_id, user)
        _require_current_extraction(db, document_id)
        if document["status"] != "in_review":
            raise HTTPException(409, "Only documents currently in review can be approved or returned.")
        if data.expectedContentRevision is not None and data.expectedContentRevision != document["content_revision"]:
            raise HTTPException(409, "The document changed. Refresh it before deciding this review.")
        submission = db.execute("""SELECT id,submitted_by FROM review_submissions
            WHERE document_id=%s AND tenant_id=%s AND decision='submitted' AND content_revision=%s AND submitted_by<>%s
            ORDER BY created_at DESC LIMIT 1""", (document_id, user["tenant_id"], document["content_revision"], user["id"])).fetchone()
        if not submission:
            raise HTTPException(422, "The document has not been submitted for approval.")
        db.execute("""INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,note,approved_content_revision)
            SELECT %s,%s,%s,%s,%s,%s,content_revision FROM documents WHERE id=%s AND tenant_id=%s""", (submission["id"], document_id, user["tenant_id"], user["id"], data.decision, data.note, document_id, user["tenant_id"]))
        next_status = "validated" if data.decision == "approved" else ("exception" if data.decision == "rejected" else "in_review")
        db.execute("UPDATE documents SET status=%s,updated_at=now() WHERE id=%s", (next_status, document_id))
        title = 'Document approved' if data.decision == 'approved' else 'Document review updated'
        message = f"{document.get('document_code', 'A document')} was approved." if data.decision == 'approved' else f"{document.get('document_code', 'A document')} review status is {next_status}."
        db.execute("INSERT INTO notifications(tenant_id,user_id,type,title,message,resource) VALUES (%s,%s,%s,%s,%s,%s)", (user['tenant_id'], submission['submitted_by'], 'document_workflow', title, message, f"documents/{document_id}"))
    return {"documentId": document_id, "decision": data.decision, "reviewedBy": str(user["id"]), "status": next_status.upper()}

def list_document_comments(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT c.*,u.name AS author_name FROM document_comments c JOIN users u ON u.id=c.author_id WHERE c.document_id=%s AND c.tenant_id=%s ORDER BY c.created_at", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "comment": r["comment"], "author": r["author_name"], "pageNumber": r["page_number"], "fieldKey": r["field_key"], "resolved": bool(r["resolved_at"]), "createdAt": str(r["created_at"])} for r in rows]

def add_document_comment(document_id: str, data: CommentInput, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO document_comments(document_id,tenant_id,author_id,comment,page_number,field_key) SELECT id,tenant_id,%s,%s,%s,%s FROM documents WHERE id=%s AND tenant_id=%s RETURNING *", (user["id"], data.comment, data.pageNumber, data.fieldKey, document_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Document not found.")
    return {"id": str(row["id"]), "comment": row["comment"], "createdAt": str(row["created_at"])}

def list_document_reconciliations(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("""SELECT r.*, s.document_code AS source_code, d.document_code AS related_code
            FROM document_reconciliations r
            JOIN documents s ON s.id=r.source_document_id
            JOIN documents d ON d.id=r.related_document_id
            WHERE r.tenant_id=%s AND (r.source_document_id=%s OR r.related_document_id=%s)
            ORDER BY r.created_at DESC""", (user["tenant_id"], document_id, document_id)).fetchall()
    return [{"id": str(r["id"]), "sourceDocumentId": str(r["source_document_id"]), "sourceCode": r["source_code"], "relatedDocumentId": str(r["related_document_id"]), "relatedCode": r["related_code"], "relationType": r["relation_type"], "amount": float(r["amount"]) if r["amount"] is not None else None, "note": r["note"], "createdAt": str(r["created_at"])} for r in rows]

def reconcile_document(document_id: str, data: ReconciliationInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        source = db.execute("SELECT id,kind FROM documents WHERE id=%s AND tenant_id=%s", (document_id, user["tenant_id"])).fetchone()
        related = db.execute("SELECT id,kind FROM documents WHERE id=%s AND tenant_id=%s", (data.relatedDocumentId, user["tenant_id"])).fetchone()
        if not source or not related:
            raise HTTPException(404, "Both documents must belong to this workspace.")
        if data.relationType == "credit_for" and related["kind"] != "credit_note":
            raise HTTPException(422, "credit_for must point to a credit note.")
        if data.relationType == "debit_for" and related["kind"] != "debit_note":
            raise HTTPException(422, "debit_for must point to a debit note.")
        row = db.execute("""INSERT INTO document_reconciliations(tenant_id,source_document_id,related_document_id,relation_type,amount,note,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], document_id, data.relatedDocumentId, data.relationType, data.amount, data.note, user["id"])).fetchone()
    return {"id": str(row["id"]), "sourceDocumentId": document_id, "relatedDocumentId": data.relatedDocumentId, "relationType": row["relation_type"], "amount": row["amount"], "note": row["note"]}

def tag_document(document_id: str, tag_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("""INSERT INTO document_tag_links(document_id,tag_id,created_by)
            SELECT d.id,t.id,%s FROM documents d JOIN document_tags t ON t.tenant_id=d.tenant_id
            WHERE d.id=%s AND d.tenant_id=%s AND t.id=%s RETURNING document_id,tag_id""", (user["id"], document_id, user["tenant_id"], tag_id)).fetchone()
    if not row: raise HTTPException(404, "Document or tag not found.")
    return {"documentId": str(row["document_id"]), "tagId": str(row["tag_id"])}

def document_status_history(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM document_status_history WHERE document_id=%s AND tenant_id=%s ORDER BY created_at", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "oldStatus": r["old_status"], "newStatus": r["new_status"], "reason": r["reason"], "createdAt": str(r["created_at"])} for r in rows]

def assign_workload(document_id: str, data: WorkloadInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        target = db.execute("SELECT id FROM users WHERE id=%s AND tenant_id=%s AND status='active'", (data.userId, user["tenant_id"])).fetchone()
        if not target: raise HTTPException(422, "Invalid team member.")
        row = db.execute("""INSERT INTO workload_assignments(tenant_id,document_id,assigned_to,assigned_by,priority,due_at)
            SELECT tenant_id,id,%s,%s,%s,%s FROM documents WHERE id=%s AND tenant_id=%s
            ON CONFLICT (document_id) DO UPDATE SET assigned_to=EXCLUDED.assigned_to,assigned_by=EXCLUDED.assigned_by,priority=EXCLUDED.priority,due_at=EXCLUDED.due_at,completed_at=NULL RETURNING *""", (data.userId, user["id"], data.priority, data.dueAt, document_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Document not found.")
    return {"id": str(row["id"]), "documentId": str(row["document_id"]), "assignedTo": str(row["assigned_to"]), "priority": row["priority"], "dueAt": str(row["due_at"] or "")}

def list_supplier_matches(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM supplier_matches WHERE document_id=%s AND tenant_id=%s ORDER BY confidence DESC NULLS LAST", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "candidateName": r["candidate_name"], "vendorId": str(r["vendor_id"]) if r["vendor_id"] else None, "confidence": float(r["confidence"]) if r["confidence"] is not None else None, "decision": r["decision"]} for r in rows]

def list_customer_matches(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM customer_matches WHERE document_id=%s AND tenant_id=%s ORDER BY confidence DESC NULLS LAST", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "candidateName": r["candidate_name"], "customerId": str(r["customer_id"]) if r["customer_id"] else None, "confidence": float(r["confidence"]) if r["confidence"] is not None else None, "decision": r["decision"]} for r in rows]

def create_document_version(document_id: str, data: VersionInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        source = db.execute("SELECT storage_path,file_hash_sha256,file_size_bytes FROM documents WHERE id=%s AND tenant_id=%s", (document_id, user["tenant_id"])).fetchone()
        if not source: raise HTTPException(404, "Document not found.")
        supplied = Path(data.storagePath).resolve()
        if local_storage_root() not in supplied.parents or not supplied.is_file():
            raise HTTPException(422, "The version file must be an existing private stored file.")
        actual_hash = hashlib.sha256(supplied.read_bytes()).hexdigest()
        if actual_hash.lower() != data.fileHashSha256.lower() or supplied.stat().st_size != data.fileSizeBytes:
            raise HTTPException(422, "Version metadata does not match the stored file.")
        row = db.execute("""INSERT INTO document_versions(document_id,tenant_id,version_number,storage_path,file_hash_sha256,file_size_bytes,created_by)
            SELECT id,tenant_id,COALESCE((SELECT max(version_number)+1 FROM document_versions WHERE document_id=%s),1),%s,%s,%s,%s
            FROM documents WHERE id=%s AND tenant_id=%s RETURNING *""", (document_id, data.storagePath, data.fileHashSha256, data.fileSizeBytes, user["id"], document_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Document not found.")
    return {"id": str(row["id"]), "documentId": document_id, "versionNumber": row["version_number"], "storagePath": row["storage_path"], "createdAt": str(row["created_at"])}

def list_document_versions(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM document_versions WHERE document_id=%s AND tenant_id=%s ORDER BY version_number DESC", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "versionNumber": r["version_number"], "storagePath": r["storage_path"], "fileHashSha256": r["file_hash_sha256"], "createdAt": str(r["created_at"])} for r in rows]

def download_document_version(document_id: str, version_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("SELECT storage_path FROM document_versions WHERE id=%s AND document_id=%s AND tenant_id=%s", (version_id, document_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Document version not found.")
    temporary = None
    if str(row["storage_path"]).startswith("s3://"):
        from database.object_storage.object_store import download
        temporary = download(str(row["storage_path"]))
        path = temporary
    else:
        path = Path(row["storage_path"]).resolve()
        if local_storage_root() not in path.parents or not path.is_file():
            raise HTTPException(404, "Version file is unavailable.")
    if not path.is_file():
        raise HTTPException(404, "Version file is unavailable.")
    cleanup = BackgroundTask(temporary.unlink, missing_ok=True) if temporary else None
    return FileResponse(path, filename=path.name, background=cleanup)

def document_kind_workflow(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        doc = db.execute("SELECT id,kind,status,updated_at FROM documents WHERE id=%s AND tenant_id=%s", (document_id, user["tenant_id"])).fetchone()
        if not doc: raise HTTPException(404, "Document not found.")
        fields = db.execute("""SELECT field_key,current_value,normalized_value FROM field_predictions
            WHERE document_id=%s ORDER BY created_at DESC""", (document_id,)).fetchall()
        values = {}
        for row in fields:
            values.setdefault(row["field_key"], row["current_value"] or row["normalized_value"])
        duplicates = db.execute("""SELECT d.id,d.document_code,d.original_filename,d.kind FROM documents d
            JOIN field_predictions f ON f.document_id=d.id
            WHERE d.tenant_id=%s AND d.id<>%s AND f.field_key='invoice' AND f.current_value=%s
            LIMIT 10""", (user["tenant_id"], document_id, values.get("invoice"))).fetchall() if values.get("invoice") else []
        links = db.execute("""SELECT r.id,r.relation_type,r.amount,d.document_code,d.kind
            FROM document_reconciliations r JOIN documents d ON d.id=r.related_document_id
            WHERE r.tenant_id=%s AND r.source_document_id=%s""", (user["tenant_id"], document_id)).fetchall()
    due = values.get("due") or values.get("due_date")
    return {"kind": doc["kind"].upper(), "fields": values, "overdue": bool(due and str(due) < datetime.now(timezone.utc).date().isoformat()), "duplicates": [{"id": str(r["id"]), "code": r["document_code"], "name": r["original_filename"], "kind": r["kind"]} for r in duplicates], "relatedNotes": [{"id": str(r["id"]), "relationType": r["relation_type"], "amount": r["amount"], "code": r["document_code"], "kind": r["kind"]} for r in links]}

def bulk_document_action(data: BulkActionInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    ids = list(dict.fromkeys(data.documentIds))
    with connect() as db:
        count = db.execute("SELECT count(*) AS n FROM documents WHERE tenant_id=%s AND id=ANY(%s::uuid[])", (user["tenant_id"], ids)).fetchone()["n"]
        if count != len(ids): raise HTTPException(404, "One or more documents are outside this workspace.")
        if data.action == "assign":
            if not data.userId: raise HTTPException(422, "userId is required.")
            for document_id in ids:
                db.execute("""INSERT INTO workload_assignments(tenant_id,document_id,assigned_to,assigned_by)
                    VALUES (%s,%s,%s,%s) ON CONFLICT (document_id) DO UPDATE SET assigned_to=EXCLUDED.assigned_to,assigned_by=EXCLUDED.assigned_by,completed_at=NULL""", (user["tenant_id"], document_id, data.userId, user["id"]))
        elif data.action == "tag":
            if not data.tagId: raise HTTPException(422, "tagId is required.")
            for document_id in ids:
                db.execute("INSERT INTO document_tag_links(document_id,tag_id,created_by) SELECT %s,id,%s FROM document_tags WHERE id=%s AND tenant_id=%s ON CONFLICT DO NOTHING", (document_id, user["id"], data.tagId, user["tenant_id"]))
    return {"updated": len(ids), "action": data.action}
"""Documents API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/documents', list_documents, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}', get_document_detail, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/file', get_document_file, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/status', update_document_status, methods=['PATCH'])
    router.add_api_route('/api/documents/{document_id}/retry', retry_document_processing, methods=['POST'])
    router.add_api_route('/api/documents/{document_id}', delete_document, methods=['DELETE'])
    router.add_api_route('/api/records', list_records, methods=['GET'])
    router.add_api_route('/api/records/metadata', record_metadata, methods=['GET'])
    router.add_api_route('/api/records/{record_id}', get_record, methods=['GET'])
    router.add_api_route('/api/records/{record_id}/amendments', get_record_amendments, methods=['GET'])
    router.add_api_route('/api/records/{record_id}/support-view', support_view, methods=['POST'])
    router.add_api_route('/api/documents', upload_document, methods=['POST'], status_code=201)
    router.add_api_route('/api/records/from-document/{document_id}', standardise_document, methods=['POST'], status_code=201)
    router.add_api_route('/api/documents/{document_id}/extraction', get_document_extraction, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/reviews', get_document_reviews, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/validation', get_document_validation, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/processing-history', get_processing_history, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/ai-assist', get_ai_assist, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/ai-assist', request_ai_assist, methods=['POST'])
    router.add_api_route('/api/documents/{document_id}/ai-assist/decision', decide_ai_assist, methods=['POST'])
    router.add_api_route('/api/documents/{document_id}/extraction/{field_key}', correct_extracted_field, methods=['PATCH'])
    router.add_api_route('/api/documents/{document_id}/line-items/{line_item_id}/{field_key}', correct_line_item, methods=['PATCH'])
    router.add_api_route('/api/records/{record_id}/amendments', request_record_amendment, methods=['POST'], status_code=201)
    router.add_api_route('/api/documents/{document_id}/revalidate', revalidate_document, methods=['POST'])
    router.add_api_route('/api/documents/{document_id}/validation-review', accept_validation_review, methods=['POST'])
    router.add_api_route('/api/documents/{document_id}/draft', save_document_draft, methods=['POST'])
    router.add_api_route('/api/documents/{document_id}/review', submit_document_review, methods=['POST'])
    router.add_api_route('/api/documents/{document_id}/review/decision', decide_document_review, methods=['POST'])
    router.add_api_route('/api/documents/{document_id}/comments', list_document_comments, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/comments', add_document_comment, methods=['POST'], status_code=201)
    router.add_api_route('/api/documents/{document_id}/reconciliations', list_document_reconciliations, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/reconciliations', reconcile_document, methods=['POST'], status_code=201)
    router.add_api_route('/api/documents/{document_id}/tags/{tag_id}', tag_document, methods=['POST'], status_code=201)
    router.add_api_route('/api/documents/{document_id}/status-history', document_status_history, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/workload', assign_workload, methods=['POST'], status_code=201)
    router.add_api_route('/api/documents/{document_id}/supplier-matches', list_supplier_matches, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/customer-matches', list_customer_matches, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/versions', create_document_version, methods=['POST'], status_code=201)
    router.add_api_route('/api/documents/{document_id}/versions', list_document_versions, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/versions/{version_id}/file', download_document_version, methods=['GET'])
    router.add_api_route('/api/documents/{document_id}/kind-workflow', document_kind_workflow, methods=['GET'])
    router.add_api_route('/api/documents/bulk-action', bulk_document_action, methods=['POST'])
