from __future__ import annotations
import json
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto
from security.authentication import revoke_user_sessions
from services.notifications.mailer import send_invitation_email

INVITATION_DAYS = 7
invite_log = logging.getLogger(__name__)


def activation_origin():
    """Public frontend origin for activation links (first configured origin wins)."""
    for name in ("FRONTEND_ORIGIN", "APP_PUBLIC_URL", "FRONTEND_URL"):
        value = os.getenv(name, "").strip()
        if value:
            return value.rstrip("/")
    origins = [v.strip() for v in os.getenv("FRONTEND_ORIGINS", "").split(",") if v.strip()]
    return (origins[0] if origins else "http://localhost:3000").rstrip("/")


def _lock_tenant_with_seat(db, tenant_id):
    """Lock the company row so concurrent invites cannot overrun the seat limit."""
    tenant = db.execute("SELECT * FROM tenants WHERE id=%s FOR UPDATE", (tenant_id,)).fetchone()
    if not tenant:
        raise HTTPException(404, "Company not found.")
    if tenant["status"] != "active":
        raise HTTPException(409, "Company is not active.")
    used = db.execute("SELECT count(*) AS n FROM users WHERE tenant_id=%s AND status IN ('active','invited')",
                      (tenant_id,)).fetchone()["n"]
    if used >= tenant["seat_limit"]:
        raise HTTPException(409, "Account limit reached.")
    return tenant


def _issue_activation_token(db, user_id):
    """Replace any unused activation link with a new single-use one; only the hash is stored."""
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM auth_tokens WHERE user_id=%s AND purpose='activation' AND used_at IS NULL", (user_id,))
    row = db.execute("""INSERT INTO auth_tokens(user_id,purpose,token_hash,expires_at)
        VALUES (%s,'activation',%s,now()+make_interval(days => %s)) RETURNING expires_at""",
        (user_id, digest(token), INVITATION_DAYS)).fetchone()
    db.execute("UPDATE users SET invited_at=now(), invitation_expires_at=%s WHERE id=%s", (row["expires_at"], user_id))
    return token, row["expires_at"]


def _create_invited_user(db, tenant_id, name, email, role, invited_by):
    if db.execute("SELECT id FROM users WHERE lower(email)=lower(%s)", (email,)).fetchone():
        raise HTTPException(409, "This email is already registered.")
    try:
        with db.transaction():
            row = db.execute("""INSERT INTO users(tenant_id,name,email,role,status,invited_by,invited_at)
                VALUES (%s,%s,%s,%s,'invited',%s,now()) RETURNING *""",
                (tenant_id, name.strip(), email.strip().lower(), role, invited_by)).fetchone()
    except UniqueViolation:
        raise HTTPException(409, "This email is already registered.")
    token, expires_at = _issue_activation_token(db, row["id"])
    return dict(row, invitation_expires_at=expires_at), token


def _user_audit(db, actor, tenant_id, action, target, details=None):
    """One audit row per user-management action (25 Nov admin audit: none were recorded). Never a password or link."""
    db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""", (tenant_id, actor["id"], actor.get("name", "Workspace user"), actor["role"], action,
                                          str(target["id"]), json.dumps({"name": target.get("name"), "email": target.get("email"),
                                                                         "role": target.get("role"), **(details or {})}, default=str)))


def _deliver_invitation(user_row, token, expires_at, company):
    """Email the link after the invite has committed. On failure, hand it back once to the admin."""
    link = activation_origin() + "/activate?token=" + token
    expires = expires_at.astimezone(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    try:
        send_invitation_email(user_row["email"], name=user_row["name"], company=company,
                              role=user_row["role"], link=link, expires=expires)
    except Exception as exc:
        # The exception text never contains the link; log only its type.
        invite_log.warning("Invitation email for user %s failed: %s", user_row["id"], type(exc).__name__)
        return {"emailSent": False, "activationLink": link, "expiresAt": str(expires_at)}
    return {"emailSent": True, "activationLink": None, "expiresAt": str(expires_at)}

def list_tenants(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM tenants WHERE (%s::uuid IS NULL OR id=%s) ORDER BY name", (tenant_scope(user), tenant_scope(user))).fetchall()
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
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_id if user["role"] == "super_admin" and tenant_id else tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM users WHERE (%s IS NULL OR tenant_id=%s) ORDER BY name", (scope, scope)).fetchall()
    return [api_user(r) for r in rows]

def invite_user(data: InviteInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        tenant = _lock_tenant_with_seat(db, user["tenant_id"])
        row, token = _create_invited_user(db, user["tenant_id"], data.name, data.email, "accountant", user["id"])
        _user_audit(db, user, user["tenant_id"], "user.invited", row)
    return {**api_user(row), "invitation": _deliver_invitation(row, token, row["invitation_expires_at"], tenant["name"])}

def add_company_user(tenant_id: str, data: CompanyUserInput, request: Request):
    """Super Admin adds a Company Admin or Accountant to an existing company by invitation."""
    admin = require_workspace_user(request, {"super_admin"})
    tenant_id = str(_uuid_or_422(tenant_id, "Company"))
    with connect() as db:
        tenant = _lock_tenant_with_seat(db, tenant_id)
        row, token = _create_invited_user(db, tenant_id, data.name, data.email, data.role, admin["id"])
        _user_audit(db, admin, tenant_id, "user.invited", row)
    return {**api_user(row), "invitation": _deliver_invitation(row, token, row["invitation_expires_at"], tenant["name"])}

def change_user_status(user_id: str, data: UserStatusInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        before = db.execute("SELECT status FROM users WHERE id=%s AND tenant_id=%s AND role='accountant' FOR UPDATE",
                            (user_id, user["tenant_id"])).fetchone()
        row = db.execute("UPDATE users SET status=%s WHERE id=%s AND tenant_id=%s AND role='accountant' RETURNING *", (data.status, user_id, user["tenant_id"])).fetchone()
        if not row:
            raise HTTPException(404, "Account is not manageable in this workspace.")
        _user_audit(db, user, user["tenant_id"], "user.status_changed", row, {"from": before["status"], "to": row["status"]})
        if data.status == "invitation_expired":
            # Cancelling an invitation kills its pending link too.
            db.execute("DELETE FROM auth_tokens WHERE user_id=%s AND purpose='activation' AND used_at IS NULL", (row["id"],))
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
            revoke_user_sessions(db, user_id)
        else:
            row = db.execute("UPDATE users SET name=%s WHERE id=%s RETURNING *", (data.name, user_id)).fetchone()
        _user_audit(db, user, user["tenant_id"], "user.updated", row,
                    {"previousName": target.get("name"), "temporaryPasswordSet": bool(data.password)})
    return api_user(row)

def resend_invitation(user_id: str, request: Request):
    """Issue a fresh activation link (old ones stop working) and email it again."""
    user = require_workspace_user(request, {"local_admin", "super_admin"})
    user_id = str(_uuid_or_422(user_id, "User"))
    with connect() as db:
        target = db.execute("""SELECT u.*, t.name AS tenant_name, t.status AS tenant_status FROM users u
            JOIN tenants t ON t.id=u.tenant_id WHERE u.id=%s FOR UPDATE OF u""", (user_id,)).fetchone()
        manageable = target and (
            (user["role"] == "super_admin" and target["role"] in ("local_admin", "accountant"))
            or (target["tenant_id"] == user["tenant_id"] and target["role"] == "accountant"))
        if not manageable:
            raise HTTPException(404, "Account is not manageable in this workspace.")
        if target["status"] not in ("invited", "invitation_expired"):
            raise HTTPException(409, "This account has already been activated.")
        if target["tenant_status"] != "active":
            raise HTTPException(409, "Company is not active.")
        if target["status"] == "invitation_expired":
            _lock_tenant_with_seat(db, target["tenant_id"])  # re-inviting takes a seat back
        token, expires_at = _issue_activation_token(db, user_id)
        row = db.execute("UPDATE users SET status='invited' WHERE id=%s RETURNING *", (user_id,)).fetchone()
        _user_audit(db, user, target["tenant_id"], "user.invitation_resent", row)
    return {**api_user(row), "invitation": _deliver_invitation(row, token, expires_at, target["tenant_name"])}

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
        invited, token = _create_invited_user(db, tenant["id"], item["contact_name"], item["contact_email"], "local_admin", admin["id"])
        db.execute("UPDATE company_requests SET status='approved',tenant_id=%s,reviewed_by=%s,reviewed_at=now() WHERE id=%s", (tenant["id"], admin["id"], request_id))
    invitation = _deliver_invitation(invited, token, invited["invitation_expires_at"], tenant["name"])
    return {"tenantId": str(tenant["id"]), "userId": str(invited["id"]), "status": "APPROVED", "invitation": invitation}

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
        target = db.execute("SELECT id,tenant_id,name,email,role FROM users WHERE id=%s AND (%s='super_admin' OR tenant_id=%s)", (user_id, user["role"], user["tenant_id"])).fetchone()
        if not target: raise HTTPException(404, "User not found.")
        db.execute("""INSERT INTO user_permissions(user_id,permission_key,granted,granted_by,updated_at)
            VALUES (%s,%s,%s,%s,now()) ON CONFLICT (user_id,permission_key) DO UPDATE SET granted=EXCLUDED.granted,granted_by=EXCLUDED.granted_by,updated_at=now()""", (user_id, data.permissionKey, data.granted, user["id"]))
        _user_audit(db, user, target["tenant_id"], "user.permission_changed", target,
                    {"permission": data.permissionKey, "granted": data.granted})
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
    router.add_api_route('/api/tenants/{tenant_id}/users', add_company_user, methods=['POST'], status_code=201)
    router.add_api_route('/api/company-requests', company_request, methods=['POST'], status_code=201)
    router.add_api_route('/api/company-requests', list_company_requests, methods=['GET'])
    router.add_api_route('/api/company-requests/{request_id}/approve', approve_company_request, methods=['POST'])
    router.add_api_route('/api/company-requests/{request_id}/reject', reject_company_request, methods=['POST'])
    router.add_api_route('/api/permissions', list_permissions, methods=['GET'])
    router.add_api_route('/api/users/{user_id}/permissions', set_user_permission, methods=['PATCH'])
    router.add_api_route('/api/platform/notifications', list_platform_notifications, methods=['GET'])
    router.add_api_route('/api/platform/notifications', create_platform_notification, methods=['POST'], status_code=201)
