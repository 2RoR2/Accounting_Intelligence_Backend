from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto

def workspace_navigation(request: Request):
    """Return the enabled navigation modules persisted for the signed-in role."""
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("""
            SELECT module_key, section, label, route, icon_key, sort_order
            FROM workspace_modules
            WHERE role=%s AND enabled=TRUE
            ORDER BY sort_order, label
        """, (user["role"],)).fetchall()
    return [{
        "key": str(row["module_key"]),
        "section": row["section"],
        "label": row["label"],
        "route": row["route"],
        "icon": row["icon_key"],
        "order": row["sort_order"],
    } for row in rows]

def list_audit(request: Request):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM audit_logs WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC LIMIT 500", (scope, scope)).fetchall()
    return [{"id": str(r["id"]), "tenantId": str(r["tenant_id"]) if r["tenant_id"] else None, "user": r["user_name"], "role": r["role"].upper(), "action": r["action"], "resource": r["resource"], "details": r["details"], "timestamp": str(r["created_at"])} for r in rows]

def get_workspace_settings(request: Request):
    user = require_workspace_user(request)
    tenant_id = None if user["role"] == "super_admin" else user["tenant_id"]
    with connect() as db:
        row = db.execute("""SELECT timezone, notifications, session_timeout_minutes,
            default_document_view, email_assignment_notifications,
            processing_status_notifications, upload_max_size_mb, allow_camera_uploads
            FROM workspace_settings WHERE tenant_id IS NOT DISTINCT FROM %s""", (tenant_id,)).fetchone()
    return row or {"timezone": "Asia/Kuala_Lumpur", "notifications": "enabled"}

def save_workspace_settings(data: SettingsInput, request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    tenant_id = None if user["role"] == "super_admin" else user["tenant_id"]
    with connect() as db:
        db.execute("""INSERT INTO workspace_settings(tenant_id, timezone, notifications,
            session_timeout_minutes, default_document_view, email_assignment_notifications,
            processing_status_notifications, upload_max_size_mb, allow_camera_uploads)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT ((COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)))
            DO UPDATE SET timezone=EXCLUDED.timezone, notifications=EXCLUDED.notifications,
              session_timeout_minutes=EXCLUDED.session_timeout_minutes,
              default_document_view=EXCLUDED.default_document_view,
              email_assignment_notifications=EXCLUDED.email_assignment_notifications,
              processing_status_notifications=EXCLUDED.processing_status_notifications,
              upload_max_size_mb=EXCLUDED.upload_max_size_mb,
              allow_camera_uploads=EXCLUDED.allow_camera_uploads, updated_at=now()""",
            (tenant_id, data.timezone, data.notifications, data.sessionTimeoutMinutes,
             data.defaultDocumentView, data.emailAssignmentNotifications,
             data.processingStatusNotifications, data.uploadMaxSizeMb, data.allowCameraUploads))
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,%s,%s,'SETTINGS_UPDATED','workspace','Workspace preferences saved.')""",
            (tenant_id, user["id"], user["name"], user["role"]))
    return data.model_dump()

def health():
    with connect() as db:
        db.execute("SELECT 1 FROM web_sessions LIMIT 1")
    return {"status": "ok"}

def list_suppliers(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM vendors WHERE tenant_id=%s ORDER BY vendor_name", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "name": r["vendor_name"], "registrationNumber": r["registration_number"], "address": r["address"]} for r in rows]

def create_supplier(data: NamedInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,%s) RETURNING *", (user["tenant_id"], data.name)).fetchone()
    return {"id": str(row["id"]), "name": row["vendor_name"]}

def list_document_tags(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM document_tags WHERE tenant_id=%s ORDER BY name", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "name": r["name"], "color": r["color"]} for r in rows]

def create_document_tag(data: NamedInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO document_tags(tenant_id,name,color) VALUES (%s,%s,%s) RETURNING *", (user["tenant_id"], data.name, data.color)).fetchone()
    return {"id": str(row["id"]), "name": row["name"], "color": row["color"]}

def delete_document_tag(tag_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("DELETE FROM document_tags WHERE id=%s AND tenant_id=%s RETURNING id", (tag_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Document tag not found.")
    return {"deleted": True, "id": str(row["id"])}

def list_saved_filters(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM saved_filters WHERE tenant_id=%s AND (created_by=%s OR is_shared) ORDER BY name", (user["tenant_id"], user["id"])).fetchall()
    return [{"id": str(r["id"]), "name": r["name"], "resource": r["resource"], "definition": r["definition"], "isShared": r["is_shared"]} for r in rows]

def create_saved_filter(data: FilterInput, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("INSERT INTO saved_filters(tenant_id,created_by,name,resource,definition,is_shared) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *", (user["tenant_id"], user["id"], data.name, data.resource, data.definition, data.isShared)).fetchone()
    return {"id": str(row["id"]), "name": row["name"], "resource": row["resource"], "definition": row["definition"], "isShared": row["is_shared"]}

def delete_saved_filter(filter_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("DELETE FROM saved_filters WHERE id=%s AND tenant_id=%s AND created_by=%s RETURNING id", (filter_id, user["tenant_id"], user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Saved filter not found.")
    return {"deleted": True, "id": str(row["id"])}

def decide_supplier_match(match_id: str, data: SupplierMatchInput, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        row = db.execute("""UPDATE supplier_matches SET decision=%s, vendor_id=%s, decided_by=%s, decided_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (data.decision, data.vendorId, user["id"], match_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Supplier match not found.")
    return {"id": str(row["id"]), "decision": row["decision"], "vendorId": str(row["vendor_id"]) if row["vendor_id"] else None}

def decide_customer_match(match_id: str, data: CustomerMatchInput, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        row = db.execute("""UPDATE customer_matches SET decision=%s, customer_id=%s, decided_by=%s, decided_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (data.decision, data.customerId, user["id"], match_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Customer match not found.")
    return {"id": str(row["id"]), "decision": row["decision"], "customerId": str(row["customer_id"]) if row["customer_id"] else None}

def decide_record_amendment(amendment_id: str, data: AmendmentDecisionInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("""UPDATE record_amendments SET status=%s,approved_by=%s,approved_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (data.decision, user["id"], amendment_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Amendment request not found.")
    return {"id": str(row["id"]), "status": row["status"], "approvedBy": str(row["approved_by"])}

def create_customer(data: PartyInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO customers(tenant_id,customer_name,registration_number,address) VALUES (%s,%s,%s,%s) RETURNING *", (user["tenant_id"], data.name, data.registrationNumber, data.address)).fetchone()
    return {"id": str(row["id"]), "name": row["customer_name"], "registrationNumber": row["registration_number"], "address": row["address"]}

def list_customers(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM customers WHERE tenant_id=%s ORDER BY customer_name", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "name": r["customer_name"], "registrationNumber": r["registration_number"], "address": r["address"]} for r in rows]

def create_custom_field(data: CustomFieldInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO custom_fields(tenant_id,field_key,label,field_type,options,required) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *", (user["tenant_id"], data.fieldKey, data.label, data.fieldType, data.options, data.required)).fetchone()
    return {"id": str(row["id"]), "fieldKey": row["field_key"], "label": row["label"], "fieldType": row["field_type"], "options": row["options"], "required": row["required"]}

def list_custom_fields(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM custom_fields WHERE tenant_id=%s ORDER BY label", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "fieldKey": r["field_key"], "label": r["label"], "fieldType": r["field_type"], "options": r["options"], "required": r["required"]} for r in rows]
"""Workspace API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/workspace/navigation', workspace_navigation, methods=['GET'])
    router.add_api_route('/api/audit', list_audit, methods=['GET'])
    router.add_api_route('/api/settings', get_workspace_settings, methods=['GET'])
    router.add_api_route('/api/settings', save_workspace_settings, methods=['PUT'])
    router.add_api_route('/api/health', health, methods=['GET'])
    router.add_api_route('/api/suppliers', list_suppliers, methods=['GET'])
    router.add_api_route('/api/suppliers', create_supplier, methods=['POST'], status_code=201)
    router.add_api_route('/api/document-tags', list_document_tags, methods=['GET'])
    router.add_api_route('/api/document-tags', create_document_tag, methods=['POST'], status_code=201)
    router.add_api_route('/api/document-tags/{tag_id}', delete_document_tag, methods=['DELETE'])
    router.add_api_route('/api/saved-filters', list_saved_filters, methods=['GET'])
    router.add_api_route('/api/saved-filters', create_saved_filter, methods=['POST'], status_code=201)
    router.add_api_route('/api/saved-filters/{filter_id}', delete_saved_filter, methods=['DELETE'])
    router.add_api_route('/api/supplier-matches/{match_id}', decide_supplier_match, methods=['PATCH'])
    router.add_api_route('/api/customer-matches/{match_id}', decide_customer_match, methods=['PATCH'])
    router.add_api_route('/api/record-amendments/{amendment_id}/decision', decide_record_amendment, methods=['POST'])
    router.add_api_route('/api/customers', create_customer, methods=['POST'], status_code=201)
    router.add_api_route('/api/customers', list_customers, methods=['GET'])
    router.add_api_route('/api/custom-fields', create_custom_field, methods=['POST'], status_code=201)
    router.add_api_route('/api/custom-fields', list_custom_fields, methods=['GET'])
