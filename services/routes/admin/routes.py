from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto

def list_tenants(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM tenants WHERE (%s IS NULL OR id=%s) ORDER BY name", (tenant_scope(user), tenant_scope(user))).fetchall()
    return [{"id": str(r["id"]), "name": r["name"], "registration": r["registration_number"], "adminEmail": r["admin_email"], "accountLimit": r["seat_limit"], "status": r["status"].upper(), "createdAt": str(r["created_at"])} for r in rows]

def change_tenant_status(tenant_id: str, request: Request):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("UPDATE tenants SET status=CASE WHEN status='active' THEN 'suspended' ELSE 'active' END WHERE id=%s RETURNING id,status", (tenant_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Tenant not found.")
    return {"id": str(row["id"]), "status": row["status"].upper()}

def change_tenant_limit(tenant_id: str, data: CompanyReviewInput, request: Request):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        active_count = db.execute("SELECT count(*) AS n FROM users WHERE tenant_id=%s AND status IN ('active','invited')", (tenant_id,)).fetchone()["n"]
        if data.limit < active_count:
            raise HTTPException(422, "Limit cannot be below active accounts.")
        row = db.execute("UPDATE tenants SET seat_limit=%s WHERE id=%s RETURNING id,seat_limit", (data.limit, tenant_id)).fetchone()
    if not row:
        raise HTTPException(404, "Tenant not found.")
    return {"id": str(row["id"]), "accountLimit": row["seat_limit"]}

def list_account_limit_requests(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM account_limit_requests WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC", (scope, scope)).fetchall()
    return [{"id": str(r["id"]), "tenantId": str(r["tenant_id"]), "current": r["current_limit"], "requested": r["requested_limit"], "reason": r["reason"], "status": r["status"].upper()} for r in rows]

def create_account_limit_request(data: LimitRequestInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        tenant = db.execute("SELECT seat_limit FROM tenants WHERE id=%s", (user["tenant_id"],)).fetchone()
        if not tenant or data.requested <= tenant["seat_limit"]:
            raise HTTPException(422, "Requested limit must be larger than the current limit.")
        row = db.execute("INSERT INTO account_limit_requests(tenant_id,current_limit,requested_limit,reason) VALUES (%s,%s,%s,%s) RETURNING *", (user["tenant_id"], tenant["seat_limit"], data.requested, data.reason)).fetchone()
    return {"id": str(row["id"]), "tenantId": str(row["tenant_id"]), "current": row["current_limit"], "requested": row["requested_limit"], "reason": row["reason"], "status": row["status"].upper()}

def review_account_limit_request(request_id: str, data: LimitReviewInput, request: Request):
    admin = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        item = db.execute("SELECT * FROM account_limit_requests WHERE id=%s AND status='pending' FOR UPDATE", (request_id,)).fetchone()
        if not item:
            raise HTTPException(404, "Request has already been reviewed.")
        if data.approve:
            db.execute("UPDATE tenants SET seat_limit=%s WHERE id=%s", (item["requested_limit"], item["tenant_id"]))
        row = db.execute("UPDATE account_limit_requests SET status=%s,review_reason=%s,reviewed_by=%s,reviewed_at=now() WHERE id=%s RETURNING *", ("approved" if data.approve else "rejected", data.reason, admin["id"], request_id)).fetchone()
    return {"id": str(row["id"]), "status": row["status"].upper()}

def list_users(request: Request, tenant_id: str | None = None):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    scope = tenant_id if user["role"] == "super_admin" and tenant_id else tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM users WHERE (%s IS NULL OR tenant_id=%s) ORDER BY name", (scope, scope)).fetchall()
    return [api_user(r) for r in rows]

def invite_user(data: InviteInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        tenant = db.execute("SELECT * FROM tenants WHERE id=%s AND status='active'", (user["tenant_id"],)).fetchone()
        count = db.execute("SELECT count(*) AS n FROM users WHERE tenant_id=%s AND status IN ('active','invited')", (user["tenant_id"],)).fetchone()["n"]
        if not tenant or count >= tenant["seat_limit"]:
            raise HTTPException(409, "Account limit reached.")
        if db.execute("SELECT id FROM users WHERE lower(email)=lower(%s)", (data.email,)).fetchone():
            raise HTTPException(409, "This email is already registered.")
        row = db.execute("""INSERT INTO users(tenant_id,name,email,role,status,invited_by,invited_at,invitation_expires_at)
            VALUES (%s,%s,%s,'accountant','invited',%s,now(),now()+interval '7 days') RETURNING *""",
            (user["tenant_id"], data.name, data.email, user["id"])).fetchone()
    return api_user(row)

def change_user_status(user_id: str, data: UserStatusInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("UPDATE users SET status=%s WHERE id=%s AND tenant_id=%s AND role='accountant' RETURNING *", (data.status, user_id, user["tenant_id"])).fetchone()
        if not row:
            raise HTTPException(404, "Account is not manageable in this workspace.")
    return api_user(row)

def update_user_credentials(user_id: str, data: AdminCredentialsInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        target = db.execute("SELECT * FROM users WHERE id=%s AND tenant_id=%s AND role='accountant'", (user_id, user["tenant_id"])).fetchone()
        if not target:
            raise HTTPException(404, "Account is not manageable in this workspace.")
        if data.password:
            if not strong_password(data.password):
                raise HTTPException(422, "Password does not meet the required strength.")
            row = db.execute("UPDATE users SET name=%s,password_hash=%s,must_change_password=true WHERE id=%s RETURNING *", (data.name, hash_password(data.password), user_id)).fetchone()
        else:
            row = db.execute("UPDATE users SET name=%s WHERE id=%s RETURNING *", (data.name, user_id)).fetchone()
    return api_user(row)

def resend_invitation(user_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("""UPDATE users SET status='invited', invited_at=now(), invitation_expires_at=now()+interval '7 days'
            WHERE id=%s AND tenant_id=%s AND role='accountant' RETURNING *""", (user_id, user["tenant_id"])).fetchone()
        if not row:
            raise HTTPException(404, "Account is not manageable in this workspace.")
    return api_user(row)

def company_request(data: CompanyInput):
    rate_limit("registration:" + data.email, 5, 3600)
    try:
        with connect() as db:
            if user_by_email(db, data.email):
                raise HTTPException(409, "This email already has an account or pending request.")
            from psycopg.types.json import Jsonb
            row = db.execute("""INSERT INTO company_requests
                (company_name,registration_number,contact_name,contact_email,phone,requested_accounts,employee_emails,notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,submitted_at""",
                (data.company, data.registration, data.contact, data.email, data.phone,
                 data.accounts, Jsonb(data.employees), data.notes)).fetchone()
    except UniqueViolation:
        raise HTTPException(409, "This email already has an account or pending request.")
    return {**data.model_dump(), "id": str(row["id"]), "submittedAt": row["submitted_at"], "status": "PENDING"}

def list_company_requests(request: Request):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        rows = db.execute("SELECT * FROM company_requests ORDER BY submitted_at DESC").fetchall()
    return [{"id": str(r["id"]), "company": r["company_name"], "registration": r["registration_number"], "contact": r["contact_name"], "email": r["contact_email"], "phone": r["phone"] or "", "accounts": r["requested_accounts"], "employees": r["employee_emails"], "notes": r["notes"] or "", "submittedAt": str(r["submitted_at"]), "status": r["status"].upper(), "rejectionReason": r["rejection_reason"] or ""} for r in rows]

def approve_company_request(request_id: str, data: CompanyReviewInput, request: Request):
    admin = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        item = db.execute("SELECT * FROM company_requests WHERE id=%s AND status='pending' FOR UPDATE", (request_id,)).fetchone()
        if not item:
            raise HTTPException(404, "Request has already been reviewed.")
        tenant = db.execute("""INSERT INTO tenants(name,registration_number,admin_email,seat_limit,status)
            VALUES (%s,%s,%s,%s,'active') RETURNING *""", (item["company_name"], item["registration_number"], item["contact_email"], data.limit)).fetchone()
        invited = db.execute("""INSERT INTO users(tenant_id,name,email,role,status,invited_by,invited_at,invitation_expires_at)
            VALUES (%s,%s,%s,'local_admin','invited',%s,now(),now()+interval '7 days') RETURNING *""", (tenant["id"], item["contact_name"], item["contact_email"], admin["id"])).fetchone()
        db.execute("UPDATE company_requests SET status='approved',tenant_id=%s,reviewed_by=%s,reviewed_at=now() WHERE id=%s", (tenant["id"], admin["id"], request_id))
    return {"tenantId": str(tenant["id"]), "userId": str(invited["id"]), "status": "APPROVED"}

def reject_company_request(request_id: str, data: CompanyReviewInput, request: Request):
    admin = require_workspace_user(request, {"super_admin"})
    if not data.reason.strip():
        raise HTTPException(422, "A rejection reason is required.")
    with connect() as db:
        row = db.execute("UPDATE company_requests SET status='rejected',rejection_reason=%s,reviewed_by=%s,reviewed_at=now() WHERE id=%s AND status='pending' RETURNING id", (data.reason, admin["id"], request_id)).fetchone()
        if not row:
            raise HTTPException(404, "Request has already been reviewed.")
    return {"id": str(row["id"]), "status": "REJECTED"}

def list_permissions(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    with connect() as db:
        rows = db.execute("SELECT permission_key,description FROM permission_catalog ORDER BY permission_key").fetchall()
    return [{"key": r["permission_key"], "description": r["description"]} for r in rows]

def set_user_permission(user_id: str, data: PermissionInput, request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    with connect() as db:
        target = db.execute("SELECT id FROM users WHERE id=%s AND (%s='super_admin' OR tenant_id=%s)", (user_id, user["role"], user["tenant_id"])).fetchone()
        if not target: raise HTTPException(404, "User not found.")
        db.execute("""INSERT INTO user_permissions(user_id,permission_key,granted,granted_by,updated_at)
            VALUES (%s,%s,%s,%s,now()) ON CONFLICT (user_id,permission_key) DO UPDATE SET granted=EXCLUDED.granted,granted_by=EXCLUDED.granted_by,updated_at=now()""", (user_id, data.permissionKey, data.granted, user["id"]))
    return {"userId": user_id, "permissionKey": data.permissionKey, "granted": data.granted}

def list_platform_notifications(request: Request):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        rows = db.execute("SELECT * FROM platform_notifications ORDER BY created_at DESC").fetchall()
    return [{"id": str(r["id"]), "type": r["notification_type"], "severity": r["severity"], "title": r["title"], "message": r["message"], "createdAt": str(r["created_at"])} for r in rows]

def create_platform_notification(data: PlatformNotificationInput, request: Request):
    user = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO platform_notifications(notification_type,severity,title,message,created_by) VALUES (%s,%s,%s,%s,%s) RETURNING *", (data.notificationType, data.severity, data.title, data.message, user["id"])).fetchone()
    return {"id": str(row["id"]), "severity": row["severity"], "title": row["title"], "message": row["message"]}
"""Admin API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/tenants', list_tenants, methods=['GET'])
    router.add_api_route('/api/tenants/{tenant_id}/status', change_tenant_status, methods=['PATCH'])
    router.add_api_route('/api/tenants/{tenant_id}/limit', change_tenant_limit, methods=['PATCH'])
    router.add_api_route('/api/account-limit-requests', list_account_limit_requests, methods=['GET'])
    router.add_api_route('/api/account-limit-requests', create_account_limit_request, methods=['POST'], status_code=201)
    router.add_api_route('/api/account-limit-requests/{request_id}/review', review_account_limit_request, methods=['POST'])
    router.add_api_route('/api/users', list_users, methods=['GET'])
    router.add_api_route('/api/users/invitations', invite_user, methods=['POST'], status_code=201)
    router.add_api_route('/api/users/{user_id}/status', change_user_status, methods=['PATCH'])
    router.add_api_route('/api/users/{user_id}/credentials', update_user_credentials, methods=['PATCH'])
    router.add_api_route('/api/users/{user_id}/resend-invitation', resend_invitation, methods=['POST'])
    router.add_api_route('/api/company-requests', company_request, methods=['POST'], status_code=201)
    router.add_api_route('/api/company-requests', list_company_requests, methods=['GET'])
    router.add_api_route('/api/company-requests/{request_id}/approve', approve_company_request, methods=['POST'])
    router.add_api_route('/api/company-requests/{request_id}/reject', reject_company_request, methods=['POST'])
    router.add_api_route('/api/permissions', list_permissions, methods=['GET'])
    router.add_api_route('/api/users/{user_id}/permissions', set_user_permission, methods=['PATCH'])
    router.add_api_route('/api/platform/notifications', list_platform_notifications, methods=['GET'])
    router.add_api_route('/api/platform/notifications', create_platform_notification, methods=['POST'], status_code=201)
