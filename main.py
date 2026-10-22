"""Public account API. Run: uvicorn main:app --host 127.0.0.1 --port 8000."""
import hmac
import logging
import os
import re
import secrets
import uuid
import hashlib
import io
from decimal import Decimal
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
import jwt
from pydantic import BaseModel, Field, StringConstraints, field_validator
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from services.extraction.accounting_contract import (
    approved_snapshot, decimal_text, field_value, line_dto, prediction_dto, record_values, CURRENCY_MINOR_UNITS,
)
from validation.document_validation import evaluate_invoice, evaluate_document, load_current, validate_document

from auth.security import (
    ACCESS_TOKEN_SECONDS,
    SESSION_SECONDS,
    create_access_token,
    decode_access_token,
    decrypt_totp_secret,
    digest,
    encrypt_totp_secret,
    hash_password,
    jwt_secret,
    needs_rehash,
    new_totp_secret,
    otp_hash,
    strong_password,
    verify_password,
    verify_totp,
)
from database.connection import connect
from notifications.mailer import send_login_code, send_reset_code


def auth_cookie_samesite():
    """Allow the Vercel frontend to send Render auth cookies in production."""
    configured = os.getenv("COOKIE_SAMESITE", "").strip().lower()
    if configured in {"strict", "lax", "none"}:
        return configured
    return "none" if os.getenv("COOKIE_SECURE", "false").lower() == "true" else "lax"

def apply_compatibility_migrations():
    """Apply additive compatibility changes for existing development databases."""
    with connect() as db:
        db.execute("ALTER TABLE review_submissions ADD COLUMN IF NOT EXISTS content_revision INTEGER NOT NULL DEFAULT 1")
        # Seed data is inserted with explicit document codes, so older
        # databases can have sequences behind the highest existing code.
        # Align each sequence before accepting new uploads to avoid a unique
        # document_code collision being returned as a generic 503.
        for kind, sequence in (
            ("invoice", "document_invoice_seq"),
            ("bill", "document_bill_seq"),
            ("receipt", "document_receipt_seq"),
            ("credit_note", "document_credit_note_seq"),
            ("debit_note", "document_debit_note_seq"),
        ):
            db.execute(
                f"""SELECT setval('{sequence}', GREATEST(
                    COALESCE((SELECT max(NULLIF(regexp_replace(document_code, '[^0-9]', '', 'g'), '')::bigint)
                              FROM documents WHERE kind=%s), 100000),
                    (SELECT last_value FROM {sequence})
                ))""",
                (kind,),
            )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run startup migrations using FastAPI's supported lifespan interface."""
    apply_compatibility_migrations()
    yield


app = FastAPI(
    title="SAIC - Accounting Intelligence API",
    description="Secure accounting document ingestion, AI extraction, validation, review, approval, standardisation and platform administration.",
    version="0.1.0",
    lifespan=lifespan,
)
log = logging.getLogger(__name__)
_frontend_origins = [value.strip().rstrip("/") for value in (os.getenv("FRONTEND_ORIGINS") or os.getenv("FRONTEND_ORIGIN", "http://localhost:3000,http://127.0.0.1:3000,https://accounting-intelligence-frontend.vercel.app")).split(",") if value.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_frontend_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+):(3000|3001|3002)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
SESSION = "ai_session"
ACCESS = "ai_access"
REFRESH = "ai_refresh"
MFA = "ai_mfa"
RESET = "ai_reset"
PRIVILEGED_ROLES = frozenset({"super_admin", "local_admin"})
WORKSPACE_PERMISSIONS = {
    "admin": ("super_admin", frozenset({"dashboard", "approvals", "tenants", "users", "processing", "performance", "records", "audit", "settings", "profile"})),
    "company": ("local_admin", frozenset({"dashboard", "documents", "upload", "exceptions", "records", "users", "validation-rules", "reports", "audit", "settings", "profile"})),
    "workspace": ("accountant", frozenset({"dashboard", "documents", "upload", "exceptions", "records", "reports", "profile"})),
}
DETAIL_PAGES = frozenset({"tenants", "documents", "records"})
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=254,
                                         pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Password = Annotated[str, StringConstraints(min_length=1, max_length=256)]
DUMMY_PASSWORD = hash_password(secrets.token_urlsafe(32))


def utcnow():
    return datetime.now(timezone.utc)


def validate_uploaded_bytes(filename: str, data: bytes):
    """Reject unsupported or obviously corrupt uploads before they enter storage/queues."""
    suffix = Path(filename or "").suffix.lower()
    accepted = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".xlsx", ".csv"}
    if suffix not in accepted:
        raise HTTPException(422, "Unsupported file type. Accepted formats: PDF, PNG, JPG, TIFF, XLSX, or CSV.")
    if not data:
        raise HTTPException(422, "The uploaded file is empty.")
    signatures = {
        ".pdf": data.startswith(b"%PDF-"),
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": data.startswith(b"\xff\xd8\xff"),
        ".jpeg": data.startswith(b"\xff\xd8\xff"),
        ".tif": data.startswith((b"II*\x00", b"MM\x00*")),
        ".tiff": data.startswith((b"II*\x00", b"MM\x00*")),
        ".xlsx": data.startswith(b"PK\x03\x04"),
        ".csv": True,
    }
    if not signatures[suffix]:
        raise HTTPException(422, "The document could not be opened. Replace it with a valid, readable file.")


def secret():
    try:
        return jwt_secret()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.middleware("http")
async def protect_requests(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        configured_origins = os.getenv("FRONTEND_ORIGINS") or os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
        allowed_origins = {value.strip().rstrip("/") for value in configured_origins.split(",") if value.strip()}
        # Keep local development ports consistent with the CORS middleware and
        # the Playwright server (3000 is the normal app, 3001/3002 are test and
        # review servers). Production origins remain explicitly configured.
        allowed_origins.update({
            f"http://{host}:{port}"
            for host in ("localhost", "127.0.0.1", "0.0.0.0")
            for port in (3000, 3001, 3002)
        })
        if request.headers.get("x-requested-with") != "AccountingIntelligence" or (origin and origin.rstrip("/") not in allowed_origins):
            return JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, exc: Exception):
    # Never return connection strings, credentials or database details to the browser.
    log.error("Account API failure at %s: %s", _request.url.path, type(exc).__name__)
    return JSONResponse({"detail": "Account service is unavailable. Check the backend configuration."}, status_code=503)


def rate_limit(key: str, maximum: int, seconds: int):
    # A separate transaction ensures failures cannot roll back the attempt counter.
    with connect() as db:
        row = db.execute("""INSERT INTO auth_rate_limits(key) VALUES (%s)
            ON CONFLICT(key) DO UPDATE SET
              hits = CASE WHEN auth_rate_limits.window_start < now() - %s * interval '1 second'
                          THEN 1 ELSE auth_rate_limits.hits + 1 END,
              window_start = CASE WHEN auth_rate_limits.window_start < now() - %s * interval '1 second'
                                  THEN now() ELSE auth_rate_limits.window_start END
            RETURNING hits""", (digest(key), seconds, seconds)).fetchone()
    if row["hits"] > maximum:
        raise HTTPException(429, "Too many attempts. Please wait and try again.")


def issue_session(db, user):
    session_id = str(uuid.uuid4())
    refresh_token = secrets.token_urlsafe(48)
    expires_at = utcnow() + timedelta(seconds=SESSION_SECONDS)
    db.execute("INSERT INTO web_sessions(session_id,token_hash,user_id,expires_at) VALUES (%s,%s,%s,%s)",
               (session_id, digest(refresh_token), user["id"], expires_at))
    return session_id, refresh_token, expires_at


def set_user_session_cookies(response: Response, user, session_id: str, refresh_token: str, expires_at: datetime):
    response.set_cookie(ACCESS, create_access_token(str(user["id"]), session_id),
                        max_age=ACCESS_TOKEN_SECONDS, httponly=True,
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        samesite=auth_cookie_samesite(), path="/api")
    response.delete_cookie(SESSION, path="/api")
    response.set_cookie(REFRESH, refresh_token, max_age=max(1, int((expires_at - utcnow()).total_seconds())),
                        httponly=True, secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        samesite=auth_cookie_samesite(), path="/api/auth")


def clear_auth_cookies(response: Response):
    response.delete_cookie(ACCESS, path="/api")
    response.delete_cookie(REFRESH, path="/api/auth")
    response.delete_cookie(MFA, path="/api/auth")
    response.delete_cookie(SESSION, path="/api")


def user_by_email(db, email):
    return db.execute("""SELECT u.*, t.tenant_code, t.name AS tenant_name, t.status AS tenant_status
        FROM users u LEFT JOIN tenants t ON t.id=u.tenant_id WHERE lower(u.email)=%s""", (email,)).fetchone()


def active(user):
    return user and user["status"] == "active" and (not user["tenant_id"] or user["tenant_status"] == "active")


def public_user(user):
    return {"id": str(user["id"]), "name": user["name"], "email": user["email"],
            "role": user["role"].upper(), "tenantId": user["tenant_code"],
            "tenantName": user.get("tenant_name") or "",
            "status": user["status"].upper(), "mustChangePassword": user["must_change_password"],
            "invitedBy": str(user["invited_by"] or ""),
            "invitedAt": str(user["invited_at"] or ""), "lastLogin": str(user["last_login_at"] or "")}


def session_user(db, request):
    token = request.cookies.get(ACCESS, "")
    try:
        claims = decode_access_token(token)
    except (jwt.InvalidTokenError, RuntimeError):
        raise HTTPException(401, "Sign in with an active company account.")
    user = db.execute("""SELECT u.*, t.tenant_code, t.name AS tenant_name, t.status AS tenant_status
        FROM web_sessions s JOIN users u ON u.id=s.user_id
        LEFT JOIN tenants t ON t.id=u.tenant_id
        WHERE s.session_id=%s AND u.id=%s AND s.expires_at>now()""",
        (claims["jti"], claims["sub"])).fetchone()
    if not active(user):
        raise HTTPException(401, "Sign in with an active company account.")
    return user


class Credentials(BaseModel):
    email: Email
    password: Password


class EmailInput(BaseModel):
    email: Email


class VerifyInput(EmailInput):
    code: Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class MfaInput(BaseModel):
    code: Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class NewPassword(BaseModel):
    password: Password
    confirmation: Password

    def validate_password(self):
        if self.password != self.confirmation or not strong_password(self.password):
            raise HTTPException(422, "Use 8-256 characters with uppercase, lowercase, number and symbol; match both passwords.")


class ActivationInput(NewPassword):
    token: Annotated[str, StringConstraints(min_length=32, max_length=256)]


class CompanyInput(BaseModel):
    company: Text
    registration: Text
    contact: Text
    email: Email
    phone: str = Field(default="", max_length=50)
    accounts: int = Field(ge=1, le=1000, strict=True)
    employees: list[Email] = Field(default_factory=list, max_length=1000)
    notes: str = Field(default="", max_length=4000)


class SettingsInput(BaseModel):
    timezone: str = Field(min_length=1, max_length=80)
    notifications: str = Field(min_length=1, max_length=40)
    sessionTimeoutMinutes: int = Field(default=30, ge=15, le=60)
    defaultDocumentView: str = Field(default="all", pattern="^(all|exceptions|review)$")
    emailAssignmentNotifications: bool = True
    processingStatusNotifications: bool = True
    uploadMaxSizeMb: int = Field(default=20, ge=1, le=20)
    allowCameraUploads: bool = True


class ProfileInput(BaseModel):
    name: Text
    avatar: str = Field(default="", max_length=2_000_000)


class ValidationRulesInput(BaseModel):
    supplier: bool
    invoice: bool
    date: bool
    total: bool
    duplicate: bool
    invoiceByType: bool = True
    billReferenceByType: bool = True
    receiptIdentifierByType: bool = False
    threshold: float = Field(ge=0, le=100)


class ExceptionUpdate(BaseModel):
    note: str = Field(default="", max_length=4000)


class ExceptionAssignment(BaseModel):
    userId: str


class InviteInput(BaseModel):
    name: Text
    email: Email


class UserStatusInput(BaseModel):
    status: Annotated[str, StringConstraints(pattern=r"^(active|disabled|invited)$")]


class AdminCredentialsInput(BaseModel):
    name: Text
    password: Password = ""


class FieldCorrectionInput(BaseModel):
    value: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4000)]
    expectedContentRevision: int = Field(strict=True, ge=1)

    @field_validator('value')
    @classmethod
    def reject_control_characters(cls, value: str):
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError('Correction values must not contain control characters.')
        return value


class CompanyReviewInput(BaseModel):
    limit: int = Field(default=10, ge=1, le=10000)
    reason: str = Field(default="", max_length=4000)


class LimitRequestInput(BaseModel):
    requested: int = Field(ge=1, le=10000)
    reason: Text


class LimitReviewInput(BaseModel):
    approve: bool
    reason: Text


class BatchInput(BaseModel):
    name: Text
    documentIds: list[str] = Field(default_factory=list, max_length=500)


class ReviewInput(BaseModel):
    decision: Annotated[str, StringConstraints(pattern=r"^submitted$")] = "submitted"
    note: str = Field(default="", max_length=4000)
    expectedContentRevision: int = Field(ge=1)


class ReviewDecisionInput(BaseModel):
    decision: Annotated[str, StringConstraints(pattern=r"^(approved|rejected|returned)$")]
    note: str = Field(default="", max_length=4000)
    expectedContentRevision: int = Field(ge=1)


class LineItemCorrectionInput(BaseModel):
    value: str = Field(max_length=2000)


class RecordAmendmentInput(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class ReconciliationInput(BaseModel):
    relatedDocumentId: str
    relationType: Annotated[str, StringConstraints(pattern=r"^(credit_for|debit_for|adjustment_of|related_to)$")]
    amount: str | None = Field(default=None, pattern=r"^(0|[1-9]\d*)(\.\d{1,2})?$")
    note: str = Field(default="", max_length=2000)


class NamedInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    color: str = "#64748b"


class FilterInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    resource: Annotated[str, StringConstraints(pattern=r"^(documents|exceptions|records)$")]
    definition: dict = Field(default_factory=dict)
    isShared: bool = False


class ReminderInput(BaseModel):
    documentId: str
    assignedTo: str
    remindAt: datetime


class NotificationInput(BaseModel):
    userId: str
    type: str
    title: str
    message: str
    resource: str | None = None


class BackupInput(BaseModel):
    backupType: Annotated[str, StringConstraints(pattern=r"^(database|documents|full)$")]


class VersionInput(BaseModel):
    storagePath: str
    fileHashSha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]
    fileSizeBytes: int = Field(gt=0)


class ExportInput(BaseModel):
    resource: Annotated[str, StringConstraints(pattern=r"^(records|documents|exceptions)$")]
    filters: dict = Field(default_factory=dict)
    format: Annotated[str, StringConstraints(pattern=r"^(csv|json|xlsx)$")] = "csv"


class PermissionInput(BaseModel):
    permissionKey: str
    granted: bool = True


class PlatformNotificationInput(BaseModel):
    notificationType: str
    severity: Annotated[str, StringConstraints(pattern=r"^(info|warning|critical)$")]
    title: str
    message: str


class AmendmentDecisionInput(BaseModel):
    decision: Annotated[str, StringConstraints(pattern=r"^(approved|rejected)$")]


class BulkActionInput(BaseModel):
    documentIds: list[str] = Field(min_length=1, max_length=500)
    action: Annotated[str, StringConstraints(pattern=r"^(assign|tag|export)$")]
    userId: str | None = None
    tagId: str | None = None


class CustomFieldInput(BaseModel):
    fieldKey: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    fieldType: Annotated[str, StringConstraints(pattern=r"^(text|number|date|boolean|select)$")]
    options: list[str] = Field(default_factory=list)
    required: bool = False


class PartyInput(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    registrationNumber: str | None = None
    address: str | None = None


class CommentInput(BaseModel):
    comment: Text
    pageNumber: int | None = Field(default=None, ge=1)
    fieldKey: str | None = Field(default=None, max_length=100)


class WorkloadInput(BaseModel):
    userId: str
    priority: Annotated[str, StringConstraints(pattern=r"^(low|normal|high|urgent)$")] = "normal"
    dueAt: datetime | None = None


class SupplierMatchInput(BaseModel):
    vendorId: str | None = None
    decision: Annotated[str, StringConstraints(pattern=r"^(accepted|rejected)$")]


class CustomerMatchInput(BaseModel):
    customerId: str | None = None
    decision: Annotated[str, StringConstraints(pattern=r"^(accepted|rejected)$")]


def require_workspace_user(request: Request, roles=None):
    with connect() as db:
        user = session_user(db, request)
    if roles and user["role"] not in roles:
        raise HTTPException(403, "You do not have permission for this workspace.")
    return user


def api_user(row):
    return {"id": str(row["id"]), "name": row["name"], "email": row["email"],
            "role": row["role"].upper(), "tenantId": str(row["tenant_id"]) if row["tenant_id"] else None,
            "status": row["status"].upper(), "mustChangePassword": row["must_change_password"],
            "invitedBy": str(row["invited_by"]) if row.get("invited_by") else "",
            "invitedAt": str(row["invited_at"] or ""), "lastLogin": str(row["last_login_at"] or ""),
            "avatar": row.get("avatar") or ""}


def tenant_scope(user):
    return None if user["role"] == "super_admin" else user["tenant_id"]


@app.get("/api/workspace/navigation")
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


@app.get("/api/tenants")
def list_tenants(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM tenants WHERE (%s IS NULL OR id=%s) ORDER BY name", (tenant_scope(user), tenant_scope(user))).fetchall()
    return [{"id": str(r["id"]), "name": r["name"], "registration": r["registration_number"], "adminEmail": r["admin_email"], "accountLimit": r["seat_limit"], "status": r["status"].upper(), "createdAt": str(r["created_at"])} for r in rows]


@app.patch("/api/tenants/{tenant_id}/status")
def change_tenant_status(tenant_id: str, request: Request):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("UPDATE tenants SET status=CASE WHEN status='active' THEN 'suspended' ELSE 'active' END WHERE id=%s RETURNING id,status", (tenant_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Tenant not found.")
    return {"id": str(row["id"]), "status": row["status"].upper()}


@app.patch("/api/tenants/{tenant_id}/limit")
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


@app.get("/api/account-limit-requests")
def list_account_limit_requests(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM account_limit_requests WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC", (scope, scope)).fetchall()
    return [{"id": str(r["id"]), "tenantId": str(r["tenant_id"]), "current": r["current_limit"], "requested": r["requested_limit"], "reason": r["reason"], "status": r["status"].upper()} for r in rows]


@app.post("/api/account-limit-requests", status_code=201)
def create_account_limit_request(data: LimitRequestInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        tenant = db.execute("SELECT seat_limit FROM tenants WHERE id=%s", (user["tenant_id"],)).fetchone()
        if not tenant or data.requested <= tenant["seat_limit"]:
            raise HTTPException(422, "Requested limit must be larger than the current limit.")
        row = db.execute("INSERT INTO account_limit_requests(tenant_id,current_limit,requested_limit,reason) VALUES (%s,%s,%s,%s) RETURNING *", (user["tenant_id"], tenant["seat_limit"], data.requested, data.reason)).fetchone()
    return {"id": str(row["id"]), "tenantId": str(row["tenant_id"]), "current": row["current_limit"], "requested": row["requested_limit"], "reason": row["reason"], "status": row["status"].upper()}


@app.post("/api/account-limit-requests/{request_id}/review")
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


@app.get("/api/users")
def list_users(request: Request, tenant_id: str | None = None):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    scope = tenant_id if user["role"] == "super_admin" and tenant_id else tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM users WHERE (%s IS NULL OR tenant_id=%s) ORDER BY name", (scope, scope)).fetchall()
    return [api_user(r) for r in rows]


@app.post("/api/users/invitations", status_code=201)
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


@app.patch("/api/users/{user_id}/status")
def change_user_status(user_id: str, data: UserStatusInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("UPDATE users SET status=%s WHERE id=%s AND tenant_id=%s AND role='accountant' RETURNING *", (data.status, user_id, user["tenant_id"])).fetchone()
        if not row:
            raise HTTPException(404, "Account is not manageable in this workspace.")
    return api_user(row)


@app.patch("/api/users/{user_id}/credentials")
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


@app.post("/api/users/{user_id}/resend-invitation")
def resend_invitation(user_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("""UPDATE users SET status='invited', invited_at=now(), invitation_expires_at=now()+interval '7 days'
            WHERE id=%s AND tenant_id=%s AND role='accountant' RETURNING *""", (user_id, user["tenant_id"])).fetchone()
        if not row:
            raise HTTPException(404, "Account is not manageable in this workspace.")
    return api_user(row)


@app.get("/api/documents")
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
        clauses.append("(d.owner_id=%s OR d.assigned_to=%s)")
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
            SELECT d.*, sr.total_amount, sr.supplier_name, sr.buyer_name,
                   COALESCE(sr.due_date::text, due_field.current_value, due_field.normalized_value, due_field.raw_value) AS due_date_value
            FROM documents d
            LEFT JOIN LATERAL (
                SELECT total_amount, supplier_name, buyer_name, due_date
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
            WHERE {' AND '.join(clauses)}
            ORDER BY d.uploaded_at DESC
        """, params).fetchall()
    return [{"id": str(r["id"]), "code": r["document_code"], "name": r["original_filename"], "tenantId": str(r["tenant_id"]), "ownerId": str(r["owner_id"]), "kind": r["kind"].upper(), "category": r["category"].upper(), "status": r["status"].upper(), "mimeType": r["mime_type"], "fileSize": r["file_size_bytes"], "totalAmount": float(r["total_amount"]) if r["total_amount"] is not None else None, "supplier": r["supplier_name"], "customer": r["buyer_name"], "dueDate": r["due_date_value"], "createdAt": str(r["uploaded_at"]), "updatedAt": str(r["updated_at"])} for r in rows]


@app.get("/api/documents/{document_id}")
def get_document_detail(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        visibility = " AND (owner_id=%s OR assigned_to=%s)" if user["role"] == "accountant" else ""
        params = [document_id, tenant_scope(user), tenant_scope(user)]
        if user["role"] == "accountant":
            params.extend([user["id"], user["id"]])
        r = db.execute(f"SELECT * FROM documents WHERE id=%s AND (%s IS NULL OR tenant_id=%s){visibility}", params).fetchone()
    if not r:
        raise HTTPException(404, "Document not found.")
    return {"id": str(r["id"]), "code": r["document_code"], "name": r["original_filename"], "tenantId": str(r["tenant_id"]), "ownerId": str(r["owner_id"]), "kind": r["kind"].upper(), "category": r["category"].upper(), "status": r["status"].upper(), "mimeType": r["mime_type"], "fileSize": r["file_size_bytes"], "contentRevision": r["content_revision"], "createdAt": str(r["uploaded_at"]), "updatedAt": str(r["updated_at"])}


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: str, request: Request):
    """Return an authorised private source file for preview/download."""
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        visibility = " AND (owner_id=%s OR assigned_to=%s)" if user["role"] == "accountant" else ""
        params = [document_id, tenant_scope(user), tenant_scope(user)]
        if user["role"] == "accountant":
            params.extend([user["id"], user["id"]])
        row = db.execute(f"SELECT storage_path, original_filename, mime_type FROM documents WHERE id=%s AND (%s IS NULL OR tenant_id=%s){visibility}", params).fetchone()
    if not row:
        raise HTTPException(404, "Document not found.")
    path = Path(row["storage_path"]).resolve()
    storage_root = Path(os.getenv("DOCUMENT_STORAGE_PATH", "storage")).resolve()
    if not path.is_file() or storage_root not in path.parents:
        raise HTTPException(404, "The original document is unavailable.")
    return FileResponse(path, media_type=row["mime_type"] or "application/octet-stream", filename=row["original_filename"], content_disposition_type="inline")


@app.patch("/api/documents/{document_id}/status")
def update_document_status(document_id: str, status: str, request: Request):
    require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    raise HTTPException(405, "Direct document status changes are not permitted. Use the dedicated workflow action.")


@app.post("/api/documents/{document_id}/retry")
def retry_document_processing(document_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        document = db.execute("SELECT id FROM documents WHERE id=%s AND tenant_id=%s FOR UPDATE", (document_id, user["tenant_id"])).fetchone()
        if not document:
            raise HTTPException(404, "Document not found.")
        job = db.execute("INSERT INTO pending_jobs(tenant_id,document_id,status) VALUES (%s,%s,'pending') RETURNING id", (user["tenant_id"], document_id)).fetchone()
        db.execute("UPDATE documents SET status='queued',updated_at=now() WHERE id=%s AND tenant_id=%s", (document_id, user["tenant_id"]))
    try:
        from job_queue.durable import dispatch_due_jobs
        dispatch_due_jobs(limit=1)
        return {"documentId": document_id, "jobId": str(job["id"]), "queueJobId": None, "status": "QUEUED"}
    except Exception:
        return {"documentId": document_id, "jobId": str(job["id"]), "queueJobId": None, "status": "QUEUED", "message": "Retry queued; processing will resume when the worker is available."}


@app.get("/api/records")
def list_records(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM standardised_records WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC", (scope, scope)).fetchall()
    return [{"id": str(r["id"]), "code": r["record_code"], "documentId": str(r["document_id"]), "tenantId": str(r["tenant_id"]), "category": r["category"].upper(), "validation": r["validation_status"].upper(), "status": "COMPLETED", "snapshot": r["snapshot"], "createdAt": str(r["created_at"])} for r in rows]


@app.get("/api/records/metadata")
def record_metadata(request: Request, tenant_id: str | None = None):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        rows = db.execute("SELECT * FROM standardised_records WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC", (tenant_id, tenant_id)).fetchall()
    return [{"id": str(r["id"]), "documentId": str(r["document_id"]), "tenantId": str(r["tenant_id"]), "createdAt": str(r["created_at"]), "category": r["category"].upper(), "validation": r["validation_status"].upper(), "status": "COMPLETED"} for r in rows]


@app.get("/api/records/{record_id}")
def get_record(record_id: str, request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_scope(user)
    with connect() as db:
        row = db.execute("SELECT * FROM standardised_records WHERE (record_code=%s OR id::text=%s) AND (%s IS NULL OR tenant_id=%s)", (record_id, record_id, scope, scope)).fetchone()
    if not row:
        raise HTTPException(404, "Standardised record not found.")
    return {"id": str(row["id"]), "code": row["record_code"], "documentId": str(row["document_id"]), "tenantId": str(row["tenant_id"]), "revision": row["revision_number"], "category": row["category"].upper(), "supplier": row["supplier_name"], "customer": row["buyer_name"], "invoiceNumber": row["invoice_number"], "referenceNumber": row["reference_number"], "invoiceDate": str(row["invoice_date"]) if row["invoice_date"] else None, "dueDate": str(row["due_date"]) if row["due_date"] else None, "totalAmount": decimal_text(row["total_amount"]), "validation": row["validation_status"].upper(), "snapshot": row["snapshot"], "createdAt": str(row["created_at"])}


@app.get("/api/records/{record_id}/amendments")
def get_record_amendments(record_id: str, request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("""SELECT a.* FROM record_amendments a
            JOIN standardised_records r ON r.id=a.original_record_id
            WHERE (a.original_record_id=%s OR r.record_code=%s) AND (%s IS NULL OR a.tenant_id=%s)
            ORDER BY a.created_at DESC""", (record_id, record_id, scope, scope)).fetchall()
    return [{"id": str(r["id"]), "recordId": str(r["original_record_id"]), "replacementRecordId": str(r["replacement_record_id"]) if r["replacement_record_id"] else None, "status": r["status"].upper(), "reason": r["reason"], "createdAt": str(r["created_at"]), "approvedAt": str(r["approved_at"]) if r["approved_at"] else None} for r in rows]


@app.post("/api/records/{record_id}/support-view")
def support_view(record_id: str, data: CompanyReviewInput, request: Request):
    user = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("SELECT * FROM standardised_records WHERE id=%s", (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Record not found.")
        db.execute("INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details) VALUES (%s,%s,%s,%s,'SUPPORT_VIEW_ACCESSED',%s,%s)", (row["tenant_id"], user["id"], user["name"], user["role"], row["record_code"], data.reason))
    return {"id": str(row["id"]), "documentId": str(row["document_id"]), "tenantId": str(row["tenant_id"]), "snapshot": row["snapshot"], "status": "COMPLETED"}


@app.post("/api/documents", status_code=201)
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
    storage_dir = Path(os.getenv("DOCUMENT_STORAGE_PATH", "storage"))
    if not storage_dir.is_absolute():
        storage_dir = (Path(__file__).resolve().parent / storage_dir).resolve()
    else:
        storage_dir = storage_dir.resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    # Never allow a client filename to create directories or escape private storage.
    original_filename = Path(file.filename or "document").name
    object_key = f"documents/{tenant_id}/{uuid.uuid4()}_{original_filename}"
    storage_path = storage_dir / f"{uuid.uuid4()}_{original_filename}"
    if os.getenv("S3_BUCKET"):
        from storage.object_store import put_bytes
        stored_path = put_bytes(object_key, data, file.content_type or "application/octet-stream")
    else:
        storage_path.write_bytes(data)
        stored_path = str(storage_path)
    try:
        with connect() as db:
            duplicate = db.execute("SELECT id,document_code,original_filename FROM documents WHERE tenant_id=%s AND file_hash_sha256=%s", (tenant_id, file_hash)).fetchone()
            if duplicate:
                raise HTTPException(409, {"message": "This document is already uploaded.", "existingDocumentId": str(duplicate["id"]), "existingDocumentCode": duplicate["document_code"], "existingFilename": duplicate["original_filename"]})
            row = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,file_size_bytes,file_hash_sha256,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued') RETURNING *""",
                (tenant_id, user["id"], original_filename, kind, category, stored_path, file.content_type or "application/octet-stream", len(data), file_hash)).fetchone()
            job = db.execute("INSERT INTO pending_jobs(tenant_id,document_id) VALUES (%s,%s) RETURNING id", (tenant_id, row["id"])).fetchone()
    except Exception:
        if not os.getenv("S3_BUCKET"):
            storage_path.unlink(missing_ok=True)
        raise
    queue_job_id = None
    try:
        from job_queue.durable import dispatch_due_jobs
        dispatch_due_jobs(limit=1)
    except Exception as exc:
        log.warning("Document %s stored but could not be queued: %s", row["id"], type(exc).__name__)
    return {"id": str(row["id"]), "code": row["document_code"], "name": row["original_filename"], "tenantId": str(row["tenant_id"]), "ownerId": str(row["owner_id"]), "kind": kind.upper(), "category": category.upper(), "status": "QUEUED", "jobId": str(job["id"]), "queueJobId": queue_job_id}


@app.get("/api/exceptions")
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


@app.get("/api/audit")
def list_audit(request: Request):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM audit_logs WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC LIMIT 500", (scope, scope)).fetchall()
    return [{"id": str(r["id"]), "tenantId": str(r["tenant_id"]) if r["tenant_id"] else None, "user": r["user_name"], "role": r["role"].upper(), "action": r["action"], "resource": r["resource"], "details": r["details"], "timestamp": str(r["created_at"])} for r in rows]


@app.get("/api/reports/accounting")
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


@app.get("/api/settings")
def get_workspace_settings(request: Request):
    user = require_workspace_user(request)
    tenant_id = None if user["role"] == "super_admin" else user["tenant_id"]
    with connect() as db:
        row = db.execute("""SELECT timezone, notifications, session_timeout_minutes,
            default_document_view, email_assignment_notifications,
            processing_status_notifications, upload_max_size_mb, allow_camera_uploads
            FROM workspace_settings WHERE tenant_id IS NOT DISTINCT FROM %s""", (tenant_id,)).fetchone()
    return row or {"timezone": "Asia/Kuala_Lumpur", "notifications": "enabled"}


@app.put("/api/settings")
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


@app.patch("/api/auth/profile")
def update_profile(data: ProfileInput, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        db.execute("UPDATE users SET name=%s, avatar=%s WHERE id=%s", (data.name.strip(), data.avatar or None, user["id"]))
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,%s,%s,'PROFILE_UPDATED',%s,'Profile updated.')""",
            (user["tenant_id"], user["id"], data.name.strip(), user["role"], user["email"]))
        updated = db.execute("SELECT * FROM users WHERE id=%s", (user["id"],)).fetchone()
    return api_user(updated)


@app.get("/api/processing/jobs")
def list_processing_jobs(request: Request):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM pending_jobs WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC", (scope, scope)).fetchall()
    return [{"id": str(r["id"]), "documentId": str(r["document_id"]), "tenantId": str(r["tenant_id"]), "status": r["status"].upper(), "attempts": r["attempt_count"], "createdAt": str(r["created_at"]), "completedAt": str(r["finished_at"] or "")} for r in rows]


@app.get("/api/processing/jobs/{job_id}")
def get_processing_job(job_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("SELECT * FROM pending_jobs WHERE id=%s AND tenant_id=%s", (job_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Processing job not found.")
    return {"id": str(row["id"]), "documentId": str(row["document_id"]), "tenantId": str(row["tenant_id"]), "status": row["status"].upper(), "attempts": row["attempt_count"], "maxAttempts": row["max_attempts"], "lastError": row["last_error"], "createdAt": str(row["created_at"]), "finishedAt": str(row["finished_at"] or "")}


@app.get("/api/processing/health")
def processing_health(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    redis_status = "unavailable"
    try:
        from job_queue.config import get_queues
        cpu_queue, io_queue = get_queues()
        cpu_queue.connection.ping()
        io_queue.connection.ping()
        redis_status = "healthy"
    except Exception:
        pass
    with connect() as db:
        scope = tenant_scope(user)
        pending = db.execute("SELECT count(*) AS n FROM pending_jobs WHERE status IN ('pending','processing') AND (%s IS NULL OR tenant_id=%s)", (scope, scope)).fetchone()["n"]
    return {"status": "healthy" if redis_status == "healthy" else "degraded", "worker": redis_status == "healthy", "redis": redis_status, "pendingJobs": int(pending)}


@app.get("/api/processing/workers")
def processing_workers(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    health = processing_health(request)
    return [{"name": "default", "queue": "processing", "status": "healthy" if health["worker"] else "unavailable", "redis": health["redis"], "tenantScope": "platform" if user["role"] == "super_admin" else "tenant"}]


@app.get("/api/processing/metrics")
def processing_metrics(request: Request):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    with connect() as db:
        row = db.execute("""SELECT count(*) FILTER (WHERE j.status IN ('succeeded','failed')) AS processed,
            coalesce(avg(d.processing_seconds),0) AS average_seconds
            FROM pending_jobs j LEFT JOIN documents d ON d.id=j.document_id
            WHERE (%s IS NULL OR j.tenant_id=%s)""", (scope, scope)).fetchone()
        exception_filter = " AND EXISTS (SELECT 1 FROM documents d WHERE d.id=exceptions.document_id AND (d.owner_id=%s OR d.assigned_to=%s))" if user["role"] == "accountant" else ""
        exception_params = [scope, scope]
        if user["role"] == "accountant":
            exception_params.extend([user["id"], user["id"]])
        exceptions = db.execute(f"SELECT count(*) AS n FROM exceptions WHERE (%s IS NULL OR tenant_id=%s) AND status <> 'resolved'{exception_filter}", exception_params).fetchone()["n"]
    processed = int(row["processed"] or 0)
    avg_seconds = float(row["average_seconds"] or 0)
    return [{"label": "Processed documents", "value": str(processed), "target": "", "met": True, "change": ""}, {"label": "Avg. processing time", "value": f"{avg_seconds:.1f}s", "target": "= 20 sec", "met": avg_seconds <= 20 or not avg_seconds, "change": ""}, {"label": "Open exceptions", "value": str(exceptions), "target": "", "met": exceptions == 0, "change": ""}]


@app.get("/api/processing/trends")
def processing_trends(request: Request, period: str = "week"):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    days = 30 if period in {"month", "30"} else 7
    with connect() as db:
        rows = db.execute("""SELECT date_trunc('day', j.created_at) AS day, count(*) AS processed,
            count(*) FILTER (WHERE j.status='succeeded') AS succeeded,
            count(e.id) AS exceptions
            FROM pending_jobs j LEFT JOIN exceptions e ON e.document_id=j.document_id
            WHERE j.created_at >= now() - (%s || ' days')::interval AND (%s IS NULL OR j.tenant_id=%s)
            GROUP BY 1 ORDER BY 1""", (days, scope, scope)).fetchall()
    return [{"day": r["day"].strftime("%d %b"), "processed": int(r["processed"]), "accuracy": round((int(r["succeeded"]) / int(r["processed"]) * 100) if r["processed"] else 0, 1), "exceptions": int(r["exceptions"])} for r in rows]


@app.get("/api/validation-rules")
def get_validation_rules(request: Request):
    # Accountants need to read the active validation policy to render the
    # review results; only local admins can change it (see the PUT endpoint).
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("SELECT require_supplier, require_invoice_number, validate_date, validate_total, check_duplicate, confidence_threshold, require_invoice_number_for_invoice, require_reference_for_bill, require_receipt_identifier FROM validation_rules WHERE tenant_id=%s", (user["tenant_id"],)).fetchone()
    if not row:
        return {"supplier": True, "invoice": True, "date": True, "total": True, "duplicate": True, "threshold": 80, "invoiceByType": True, "billReferenceByType": True, "receiptIdentifierByType": False}
    return {"supplier": row["require_supplier"], "invoice": row["require_invoice_number"], "date": row["validate_date"], "total": row["validate_total"], "duplicate": row["check_duplicate"], "threshold": float(row["confidence_threshold"]) * 100, "invoiceByType": row["require_invoice_number_for_invoice"], "billReferenceByType": row["require_reference_for_bill"], "receiptIdentifierByType": row["require_receipt_identifier"]}


@app.put("/api/validation-rules")
def save_validation_rules(data: ValidationRulesInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        db.execute("""INSERT INTO validation_rules(tenant_id,require_supplier,require_invoice_number,validate_date,validate_total,check_duplicate,confidence_threshold,require_invoice_number_for_invoice,require_reference_for_bill,require_receipt_identifier)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id) DO UPDATE SET require_supplier=EXCLUDED.require_supplier, require_invoice_number=EXCLUDED.require_invoice_number, validate_date=EXCLUDED.validate_date, validate_total=EXCLUDED.validate_total, check_duplicate=EXCLUDED.check_duplicate, confidence_threshold=EXCLUDED.confidence_threshold, require_invoice_number_for_invoice=EXCLUDED.require_invoice_number_for_invoice, require_reference_for_bill=EXCLUDED.require_reference_for_bill, require_receipt_identifier=EXCLUDED.require_receipt_identifier, updated_at=now()""",
            (user["tenant_id"], data.supplier, data.invoice, data.date, data.total, data.duplicate, data.threshold / 100, data.invoiceByType, data.billReferenceByType, data.receiptIdentifierByType))
    return data.model_dump()


@app.patch("/api/exceptions/{exception_id}/assign")
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


@app.patch("/api/exceptions/{exception_id}/resolve")
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


@app.post("/api/records/from-document/{document_id}", status_code=201)
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
        invoice_number = values.get("invoice") or values.get("reference")
        row = db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,supplier_name,invoice_number,buyer_name,invoice_date,due_date,currency,total_amount,subtotal,tax_amount,discount,validation_status,snapshot,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s::date,%s::date,%s,%s::numeric,%s::numeric,%s::numeric,%s::numeric,'passed',%s,%s) RETURNING *""",
            (document_id, user["tenant_id"], doc["category"], values.get('supplier'), invoice_number,
             values.get("customer"), values.get("date"), values.get("due"), values.get("currency"),
             values.get("total"), values.get('subtotal'), values.get('tax'), values.get('discount'), Jsonb(snapshot), user["id"])).fetchone()
        db.execute("UPDATE documents SET status='completed', updated_at=now() WHERE id=%s", (document_id,))
    return {"id": str(row["id"]), "documentId": str(row["document_id"]), "tenantId": str(row["tenant_id"]), "category": row["category"].upper(), "validation": row["validation_status"].upper(), "status": "COMPLETED", "createdAt": str(row["created_at"])}


@app.get("/api/documents/{document_id}/extraction")
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
    return {
        "documentId": document_id,
        "contentRevision": doc['content_revision'],
        "classification": (extraction['raw_json'].get('document_type') or {}) if extraction else {},
        "raw": extraction["raw_json"] if extraction else {},
        "fields": [prediction_dto(row) for row in fields],
        "lineItems": [line_dto(row) for row in items],
    }


def _authorised_document(request: Request, document_id: str):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        visibility = " AND (owner_id=%s OR assigned_to=%s)" if user["role"] == "accountant" else ""
        params = [document_id, tenant_scope(user), tenant_scope(user)]
        if user["role"] == "accountant":
            params.extend([user["id"], user["id"]])
        row = db.execute(f"SELECT * FROM documents WHERE id=%s AND (%s IS NULL OR tenant_id=%s){visibility}", params).fetchone()
    if not row:
        raise HTTPException(404, "Document not found.")
    return user


@app.get("/api/documents/{document_id}/reviews")
def get_document_reviews(document_id: str, request: Request):
    user = _authorised_document(request, document_id)
    with connect() as db:
        submissions = db.execute("SELECT * FROM review_submissions WHERE document_id=%s AND tenant_id=%s ORDER BY created_at DESC", (document_id, user["tenant_id"])).fetchall()
        decisions = db.execute("SELECT * FROM review_decisions WHERE document_id=%s AND tenant_id=%s ORDER BY created_at DESC", (document_id, user["tenant_id"])).fetchall()
    return {"submissions": [{"id": str(r["id"]), "submittedBy": str(r["submitted_by"]), "decision": r["decision"].upper(), "note": r["note"] or "", "createdAt": str(r["created_at"])} for r in submissions], "decisions": [{"id": str(r["id"]), "reviewedBy": str(r["reviewed_by"]), "decision": r["decision"].upper(), "note": r["note"] or "", "approvedRevision": r["approved_content_revision"], "createdAt": str(r["created_at"])} for r in decisions]}


@app.get("/api/documents/{document_id}/validation")
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
    results = [{**e, 'rule': e['rule_id'], 'ruleSource': e['rule_source'], 'ruleVersion': e['rule_version'],
                'documentType': e['document_type'], 'result': e['status'].upper(), 'checkStatus': e['status'].upper(),
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
    blocking = any(r.get('result') in {'FAIL', 'FAILED'} or r.get('preventsApproval') is True for r in results)
    return {'documentId': document_id, 'contentRevision': doc['content_revision'], 'status': doc['status'].upper(),
            'validationStatus': 'REVIEW_REQUIRED' if validation['errors'] or external or blocking or not validation['coverage_complete'] else 'PASS', 'reviewRequired': True,
            'coverageComplete': validation['coverage_complete'],
            'accountingApprovalPerformed': False, 'passed': int(not validation['errors'] and validation['coverage_complete']),
            'warnings': sum(r['result'] in {'WARNING', 'NOT_CHECKED'} for r in results), 'failed': sum(r['result'] in {'FAIL', 'FAILED'} for r in results),
            'blocking': blocking, 'results': results, 'vendorMatch': validation.get('vendor_match')}


@app.get("/api/documents/{document_id}/processing-history")
def get_processing_history(document_id: str, request: Request):
    user = _authorised_document(request, document_id)
    with connect() as db:
        jobs = db.execute("SELECT id,status,attempt_count,max_attempts,last_error,created_at,finished_at FROM pending_jobs WHERE document_id=%s AND tenant_id=%s ORDER BY created_at DESC", (document_id, user["tenant_id"])).fetchall()
        history = db.execute("SELECT old_status,new_status,changed_by,reason,created_at FROM document_status_history WHERE document_id=%s AND tenant_id=%s ORDER BY created_at DESC", (document_id, user["tenant_id"])).fetchall()
    return {"jobs": [{"id": str(r["id"]), "status": r["status"].upper(), "attempts": r["attempt_count"], "maxAttempts": r["max_attempts"], "error": r["last_error"], "createdAt": str(r["created_at"]), "finishedAt": str(r["finished_at"]) if r["finished_at"] else None} for r in jobs], "statusHistory": [{"from": r["old_status"], "to": r["new_status"], "changedBy": str(r["changed_by"]) if r["changed_by"] else None, "reason": r["reason"], "createdAt": str(r["created_at"])} for r in history]}


@app.patch("/api/documents/{document_id}/extraction/{field_key}")
def correct_extracted_field(document_id: str, field_key: str, data: FieldCorrectionInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    # Accept existing API and M2 keys as stored; key translation belongs to the
    # extraction adapter. Never allow arbitrary prediction metadata to be edited.
    allowed_fields = {
        'supplier', 'seller', 'merchant', 'customer', 'buyer', 'invoice', 'reference', 'receipt',
        'date', 'due', 'currency', 'subtotal', 'tax', 'discount', 'total',
        'supplier_name', 'buyer_name', 'invoice_number', 'reference_number',
        'invoice_date', 'due_date', 'tax_amount', 'total_amount',
        'account_number', 'billing_period', 'transaction_time', 'payment_method',
        'payment_terms', 'received_date', 'expense_category', 'payment_reference',
        'tendered_amount', 'change_given', 'amount_paid', 'outstanding_amount',
        'document_allowances', 'document_charges', 'prepaid_amount', 'rounding_amount',
        'total_excluding_tax', 'total_including_tax', 'amount_due', 'purchase_order_number',
        'goods_receipt_reference',
    }
    if field_key not in allowed_fields:
        raise HTTPException(422, "Unsupported extraction field.")
    if field_key in {'subtotal', 'tax', 'discount', 'total', 'tax_amount', 'total_amount',
                     'tendered_amount', 'change_given', 'amount_paid', 'outstanding_amount',
                     'document_allowances', 'document_charges', 'prepaid_amount', 'rounding_amount',
                     'total_excluding_tax', 'total_including_tax', 'amount_due'}:
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
    return {"key": row["field_key"], "value": row["current_value"], "correctedBy": str(row["corrected_by"]),
            "contentRevision": revision['content_revision']}


@app.patch("/api/documents/{document_id}/line-items/{line_item_id}/{field_key}")
def correct_line_item(document_id: str, line_item_id: str, field_key: str, data: LineItemCorrectionInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    if field_key not in {"description", "quantity", "unit_price", "amount"}:
        raise HTTPException(422, "Unsupported line-item field.")
    with connect() as db:
        item = db.execute("""SELECT li.* FROM line_items li
            JOIN documents d ON d.id=li.document_id AND d.tenant_id=%s
            WHERE li.id=%s AND li.document_id=%s FOR UPDATE""", (user["tenant_id"], line_item_id, document_id)).fetchone()
        if not item:
            raise HTTPException(404, "Line item not found.")
        old_value = item[field_key]
        db.execute(f"UPDATE line_items SET {field_key}=%s WHERE id=%s", (data.value, line_item_id))
        db.execute("UPDATE documents SET content_revision=content_revision+1,updated_at=now() WHERE id=%s AND tenant_id=%s", (document_id, user["tenant_id"]))
        db.execute("""INSERT INTO line_item_corrections(line_item_id,document_id,field_key,old_value,new_value,corrected_by)
            VALUES (%s,%s,%s,%s,%s,%s)""", (line_item_id, document_id, field_key, str(old_value) if old_value is not None else None, data.value, user["id"]))
    return {"lineItemId": line_item_id, "fieldKey": field_key, "value": data.value, "correctedBy": str(user["id"])}


@app.post("/api/records/{record_id}/amendments", status_code=201)
def request_record_amendment(record_id: str, data: RecordAmendmentInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("""INSERT INTO record_amendments(original_record_id,document_id,tenant_id,requested_by,reason)
            SELECT id,document_id,tenant_id,%s,%s FROM standardised_records
            WHERE id=%s AND tenant_id=%s RETURNING *""", (user["id"], data.reason.strip(), record_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Standardised record not found.")
    return {"id": str(row["id"]), "recordId": record_id, "status": row["status"], "createdAt": str(row["created_at"])}


@app.post("/api/documents/{document_id}/revalidate")
def revalidate_document(document_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        return validate_document(db, document_id, user)


@app.post("/api/documents/{document_id}/draft")
def save_document_draft(document_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("""UPDATE documents SET updated_at=now()
            WHERE id=%s AND tenant_id=%s AND status NOT IN ('completed','standardised')
            RETURNING id,status,updated_at""", (document_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Document cannot be saved as a draft.")
    return {"documentId": str(row["id"]), "status": row["status"].upper(), "savedAt": str(row["updated_at"])}


@app.get("/api/health")
def health():
    with connect() as db:
        db.execute("SELECT 1 FROM web_sessions LIMIT 1")
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(data: Credentials, request: Request, response: Response):
    secret()
    rate_limit("login:" + data.email, 10, 900)
    with connect() as db:
        user = user_by_email(db, data.email)
        encoded = (user or {}).get("password_hash") or DUMMY_PASSWORD
        valid = verify_password(data.password, encoded)
        if not valid or not active(user):
            raise HTTPException(401, "Invalid email or password, or account is not active.")
        if needs_rehash(encoded):
            db.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                       (hash_password(data.password), user["id"]))
        db.execute("DELETE FROM web_sessions WHERE token_hash=%s OR expires_at<=now()",
                   (digest(request.cookies.get(REFRESH, "")),))
        db.execute("UPDATE users SET last_login_at=now() WHERE id=%s", (user["id"],))
        user["last_login_at"] = utcnow()
        if user["role"] in PRIVILEGED_ROLES:
            db.execute("DELETE FROM auth_challenges WHERE user_id=%s OR expires_at<=now()", (user["id"],))
            challenge = secrets.token_urlsafe(32)
            pending_secret = None
            setup = not user["totp_enabled"] or not user["totp_secret_enc"]
            if setup:
                pending_secret = encrypt_totp_secret(new_totp_secret())
            db.execute("INSERT INTO auth_challenges(challenge_hash,user_id,pending_totp_secret_enc,expires_at) VALUES (%s,%s,%s,%s)",
                       (digest(challenge), user["id"], pending_secret, utcnow() + timedelta(minutes=10)))
        elif user["role"] == "accountant":
            db.execute("DELETE FROM auth_challenges WHERE user_id=%s OR expires_at<=now()", (user["id"],))
            challenge = secrets.token_urlsafe(32)
            code = f"{secrets.randbelow(1000000):06d}"
            db.execute("INSERT INTO auth_challenges(challenge_hash,user_id,email_code_hash,method,expires_at) VALUES (%s,%s,%s,'email',%s)",
                       (digest(challenge), user["id"], otp_hash(jwt_secret(), data.email, code), utcnow() + timedelta(seconds=60)))
            try:
                send_login_code(data.email, code)
            except Exception as exc:
                log.error("Login OTP delivery failed: %s: %s", type(exc).__name__, str(exc)[:300])
                raise HTTPException(503, "Unable to send sign-in code. Please try again later.")
        else:
            db.execute("DELETE FROM auth_challenges WHERE challenge_hash=%s",
                       (digest(request.cookies.get(MFA, "")),))
            session_id, refresh_token, expires_at = issue_session(db, user)
    if user["role"] in PRIVILEGED_ROLES:
        clear_auth_cookies(response)
        response.set_cookie(MFA, challenge, max_age=600, httponly=True,
                            secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                            samesite=auth_cookie_samesite(), path="/api/auth")
        result = {"mfaRequired": True, "enrollmentRequired": setup}
        if setup:
            totp_secret = decrypt_totp_secret(pending_secret)
            label = quote(user["email"], safe="")
            result["totpSecret"] = totp_secret
            result["otpauthUrl"] = (
                f"otpauth://totp/Accounting%20Intelligence:{label}?secret={totp_secret}"
                f"&issuer=Accounting%20Intelligence&algorithm=SHA1&digits=6&period=30"
            )
        return result
    if user["role"] == "accountant":
        clear_auth_cookies(response)
        response.set_cookie(MFA, challenge, max_age=600, httponly=True, secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", samesite=auth_cookie_samesite(), path="/api/auth")
        return {"mfaRequired": True, "enrollmentRequired": False, "mfaMethod": "email"}
    set_user_session_cookies(response, user, session_id, refresh_token, expires_at)
    response.delete_cookie(MFA, path="/api/auth")
    return public_user(user)


@app.get("/api/auth/me")
def me(request: Request):
    with connect() as db:
        return public_user(session_user(db, request))


@app.get("/api/auth/sessions")
def list_auth_sessions(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT session_id,expires_at FROM web_sessions WHERE user_id=%s AND expires_at>now() ORDER BY expires_at DESC", (user["id"],)).fetchall()
    return [{"id": str(row["session_id"]), "expiresAt": str(row["expires_at"]), "current": False} for row in rows]


@app.post("/api/auth/resend-mfa")
def resend_mfa(request: Request, response: Response):
    """Issue a fresh email MFA code for the current login challenge."""
    challenge = request.cookies.get(MFA, "")
    if not challenge:
        raise HTTPException(400, "Your sign-in challenge has expired. Sign in again.")
    rate_limit("resend-mfa:" + digest(challenge).hex(), 3, 60)
    with connect() as db:
        row = db.execute("""SELECT c.user_id, c.method, u.email
            FROM auth_challenges c JOIN users u ON u.id=c.user_id
            WHERE c.challenge_hash=%s AND c.method='email' AND c.expires_at>now()
            FOR UPDATE OF c""", (digest(challenge),)).fetchone()
        if not row:
            raise HTTPException(400, "Your sign-in challenge has expired. Sign in again.")
        code = f"{secrets.randbelow(1000000):06d}"
        new_challenge = secrets.token_urlsafe(32)
        db.execute("DELETE FROM auth_challenges WHERE user_id=%s", (row["user_id"],))
        db.execute("""INSERT INTO auth_challenges
            (challenge_hash,user_id,email_code_hash,method,expires_at)
            VALUES (%s,%s,%s,'email',%s)""",
            (digest(new_challenge), row["user_id"],
             otp_hash(jwt_secret(), row["email"], code),
             utcnow() + timedelta(seconds=60)))
        try:
            send_login_code(row["email"], code)
        except Exception as exc:
            log.error("MFA resend delivery failed: %s: %s", type(exc).__name__, str(exc)[:300])
            raise HTTPException(503, "Unable to send a new sign-in code. Please try again.")
    response.set_cookie(MFA, new_challenge, max_age=600, httponly=True,
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        samesite=auth_cookie_samesite(), path="/api/auth")
    return {"message": "A new sign-in code has been sent.", "expiresIn": 60}


@app.post("/api/auth/verify-mfa")
def verify_mfa(data: MfaInput, request: Request, response: Response):
    challenge = request.cookies.get(MFA, "")
    valid = False
    user = None
    session_id = refresh_token = expires_at = None
    with connect() as db:
        row = db.execute("""SELECT c.*, u.*, t.tenant_code, t.status AS tenant_status
            FROM auth_challenges c JOIN users u ON u.id=c.user_id
            LEFT JOIN tenants t ON t.id=u.tenant_id
            WHERE c.challenge_hash=%s AND c.expires_at>now() AND c.attempts<5
            FOR UPDATE OF c,u""", (digest(challenge),)).fetchone()
        if row and active(row):
            db.execute("UPDATE auth_challenges SET attempts=attempts+1 WHERE challenge_hash=%s",
                       (digest(challenge),))
            encoded_totp = row["pending_totp_secret_enc"] or row["totp_secret_enc"]
            email_valid = row.get("method") == "email" and row.get("email_code_hash") and hmac.compare_digest(row["email_code_hash"], otp_hash(jwt_secret(), row["email"], data.code))
            if (encoded_totp and verify_totp(decrypt_totp_secret(encoded_totp), data.code)) or email_valid:
                valid = True
                user = row
                if row["pending_totp_secret_enc"]:
                    db.execute("UPDATE users SET totp_secret_enc=%s,totp_enabled=true WHERE id=%s",
                               (row["pending_totp_secret_enc"], row["id"]))
                db.execute("DELETE FROM auth_challenges WHERE challenge_hash=%s", (digest(challenge),))
                session_id, refresh_token, expires_at = issue_session(db, user)
    if not valid:
        raise HTTPException(400, "Authentication code is invalid or expired. Sign in again.")
    set_user_session_cookies(response, user, session_id, refresh_token, expires_at)
    response.delete_cookie(MFA, path="/api/auth")
    return public_user(user)


@app.post("/api/auth/refresh")
def refresh_session(request: Request, response: Response):
    token = request.cookies.get(REFRESH, "")
    replacement = secrets.token_urlsafe(48)
    with connect() as db:
        row = db.execute("""SELECT s.session_id,s.expires_at,u.*,t.tenant_code,t.status AS tenant_status
            FROM web_sessions s JOIN users u ON u.id=s.user_id
            LEFT JOIN tenants t ON t.id=u.tenant_id
            WHERE s.token_hash=%s AND s.expires_at>now()
            FOR UPDATE OF s,u""", (digest(token),)).fetchone()
        if not active(row):
            raise HTTPException(401, "Sign in with an active company account.")
        db.execute("UPDATE web_sessions SET token_hash=%s WHERE session_id=%s",
                   (digest(replacement), row["session_id"]))
        session_id = str(row["session_id"])
        expires_at = row["expires_at"]
    set_user_session_cookies(response, row, session_id, replacement, expires_at)
    return {"success": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    with connect() as db:
        db.execute("DELETE FROM web_sessions WHERE token_hash=%s",
                   (digest(request.cookies.get(REFRESH, "")),))
    clear_auth_cookies(response)
    response.delete_cookie(RESET, path="/api/auth")
    return {"success": True}


@app.get("/api/auth/authorize")
def authorize_workspace(path: str, request: Request):
    segments = path.split("/")
    if len(segments) not in (2, 3) or not all(segments):
        raise HTTPException(403, "This workspace route is not available.")
    area, page = segments[:2]
    allowed_role = WORKSPACE_PERMISSIONS.get(area)
    if not allowed_role or page not in allowed_role[1]:
        raise HTTPException(403, "This workspace route is not available.")
    if len(segments) == 3 and page not in DETAIL_PAGES:
        raise HTTPException(403, "This workspace route is not available.")
    with connect() as db:
        user = session_user(db, request)
    if user["role"] != allowed_role[0]:
        raise HTTPException(403, "Your account role cannot access this workspace route.")
    return {"allowed": True}


@app.post("/api/company-requests", status_code=201)
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


@app.get("/api/company-requests")
def list_company_requests(request: Request):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        rows = db.execute("SELECT * FROM company_requests ORDER BY submitted_at DESC").fetchall()
    return [{"id": str(r["id"]), "company": r["company_name"], "registration": r["registration_number"], "contact": r["contact_name"], "email": r["contact_email"], "phone": r["phone"] or "", "accounts": r["requested_accounts"], "employees": r["employee_emails"], "notes": r["notes"] or "", "submittedAt": str(r["submitted_at"]), "status": r["status"].upper(), "rejectionReason": r["rejection_reason"] or ""} for r in rows]


@app.post("/api/company-requests/{request_id}/approve")
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


@app.post("/api/company-requests/{request_id}/reject")
def reject_company_request(request_id: str, data: CompanyReviewInput, request: Request):
    admin = require_workspace_user(request, {"super_admin"})
    if not data.reason.strip():
        raise HTTPException(422, "A rejection reason is required.")
    with connect() as db:
        row = db.execute("UPDATE company_requests SET status='rejected',rejection_reason=%s,reviewed_by=%s,reviewed_at=now() WHERE id=%s AND status='pending' RETURNING id", (data.reason, admin["id"], request_id)).fetchone()
        if not row:
            raise HTTPException(404, "Request has already been reviewed.")
    return {"id": str(row["id"]), "status": "REJECTED"}


@app.post("/api/auth/forgot-password")
def forgot_password(data: EmailInput, response: Response):
    signing_secret = secret()
    rate_limit("reset-minute:" + data.email, 1, 60)
    rate_limit("reset-window:" + data.email, 5, 900)
    response.delete_cookie(RESET, path="/api/auth")
    with connect() as db:
        user = user_by_email(db, data.email)
        if not active(user):
            # Do not reveal whether an email belongs to an active account.
            return {"message": "If this email belongs to an active account, a reset code has been sent."}
        code = f"{secrets.randbelow(1000000):06d}"
        db.execute("""INSERT INTO password_challenges(email,user_id,code_hash,expires_at)
            VALUES (%s,%s,%s,%s) ON CONFLICT(email) DO UPDATE SET
            code_hash=excluded.code_hash, expires_at=excluded.expires_at, sent_at=now(),
            attempts=0, grant_hash=NULL, grant_expires_at=NULL""",
            (data.email, user["id"], otp_hash(signing_secret, data.email, code), utcnow() + timedelta(minutes=10)))
        try:
            send_reset_code(data.email, code)
        except Exception as exc:
            log.error("Reset email delivery failed: %s", type(exc).__name__)
            raise HTTPException(503, "Unable to send email. Please try again later or contact support.")
    return {"message": "If this email belongs to an active account, a reset code has been sent."}


@app.post("/api/auth/verify-reset-code")
def verify_reset_code(data: VerifyInput, response: Response):
    signing_secret = secret()
    rate_limit("verify:" + data.email, 10, 900)
    valid = False
    with connect() as db:
        row = db.execute("SELECT * FROM password_challenges WHERE email=%s FOR UPDATE", (data.email,)).fetchone()
        if row and row["expires_at"] > utcnow() and row["attempts"] < 5 and not row["grant_hash"]:
            db.execute("UPDATE password_challenges SET attempts=attempts+1 WHERE email=%s", (data.email,))
            valid = hmac.compare_digest(row["code_hash"], otp_hash(signing_secret, data.email, data.code))
            if valid:
                token = secrets.token_urlsafe(32)
                db.execute("UPDATE password_challenges SET grant_hash=%s,grant_expires_at=%s WHERE email=%s",
                           (digest(token), utcnow() + timedelta(minutes=10), data.email))
    # Raise after committing so wrong-code attempts cannot be rolled back.
    if not valid:
        raise HTTPException(400, "Code is invalid, expired or already used. Request a new code.")
    response.set_cookie(RESET, token, max_age=600, httponly=True,
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        samesite=auth_cookie_samesite(), path="/api/auth")
    return {"success": True}


@app.get("/api/auth/reset-status")
def reset_status(request: Request):
    with connect() as db:
        row = db.execute("SELECT 1 FROM password_challenges WHERE grant_hash=%s AND grant_expires_at>now()",
                         (digest(request.cookies.get(RESET, "")),)).fetchone()
    if not row:
        raise HTTPException(401, "Verify your email code before resetting your password.")
    return {"valid": True}


@app.post("/api/auth/reset-password")
def reset_password(data: NewPassword, request: Request, response: Response):
    data.validate_password()
    with connect() as db:
        row = db.execute("""SELECT * FROM password_challenges WHERE grant_hash=%s
            AND grant_expires_at>now() FOR UPDATE""", (digest(request.cookies.get(RESET, "")),)).fetchone()
        if not row or not active(user_by_email(db, row["email"])):
            raise HTTPException(400, "Reset authorization is invalid or expired. Request a new code.")
        db.execute("UPDATE users SET password_hash=%s,must_change_password=false WHERE id=%s",
                   (hash_password(data.password), row["user_id"]))
        db.execute("DELETE FROM web_sessions WHERE user_id=%s", (row["user_id"],))
        db.execute("DELETE FROM password_challenges WHERE email=%s", (row["email"],))
    response.delete_cookie(RESET, path="/api/auth")
    clear_auth_cookies(response)
    return {"success": True}


@app.post("/api/auth/change-password")
def change_password(data: NewPassword, request: Request):
    data.validate_password()
    with connect() as db:
        user = session_user(db, request)
        # This endpoint is only for the existing forced-first-login screen.
        if not user["must_change_password"]:
            raise HTTPException(403, "Use the email reset flow to change your password.")
        db.execute("UPDATE users SET password_hash=%s,must_change_password=false WHERE id=%s",
                   (hash_password(data.password), user["id"]))
        claims = decode_access_token(request.cookies.get(ACCESS, ""))
        db.execute("DELETE FROM web_sessions WHERE user_id=%s AND session_id<>%s",
                   (user["id"], claims["jti"]))
        db.execute("DELETE FROM password_challenges WHERE user_id=%s", (user["id"],))
        user["must_change_password"] = False
    return public_user(user)


@app.post("/api/auth/mfa-recovery-requests")
def request_mfa_recovery(data: dict, request: Request):
    reason = str(data.get("reason", "")).strip()[:500]
    if not reason:
        raise HTTPException(400, "A recovery reason is required.")
    with connect() as db:
        requester = session_user(db, request)
        target = user_by_email(db, str(data.get("email", "")).strip().lower())
        if not target or target["role"] not in PRIVILEGED_ROLES:
            raise HTTPException(404, "Privileged account not found.")
        if requester["id"] != target["id"] and requester["role"] != "super_admin":
            raise HTTPException(403, "Only the affected user or a Super Admin can request MFA recovery.")
        row = db.execute("""INSERT INTO mfa_recovery_requests(requester_id,target_user_id,reason)
            VALUES (%s,%s,%s) ON CONFLICT (target_user_id,status) DO UPDATE SET reason=EXCLUDED.reason,created_at=now()
            RETURNING id,status,created_at""", (requester["id"], target["id"], reason)).fetchone()
    return {"id": str(row["id"]), "status": row["status"], "createdAt": str(row["created_at"])}


@app.get("/api/admin/mfa-recovery-requests")
def list_mfa_recovery_requests(request: Request):
    with connect() as db:
        admin = session_user(db, request)
        if admin["role"] != "super_admin": raise HTTPException(403, "Super Admin approval required.")
        rows = db.execute("""SELECT r.*,u.email AS target_email,u.name AS target_name
            FROM mfa_recovery_requests r JOIN users u ON u.id=r.target_user_id
            ORDER BY r.created_at DESC""").fetchall()
    return [{**dict(row), "id": str(row["id"]), "requester_id": str(row["requester_id"]), "target_user_id": str(row["target_user_id"])} for row in rows]


@app.post("/api/admin/mfa-recovery-requests/{request_id}/approve")
def approve_mfa_recovery(request_id: str, request: Request):
    with connect() as db:
        admin = session_user(db, request)
        if admin["role"] != "super_admin": raise HTTPException(403, "Super Admin approval required.")
        row = db.execute("SELECT * FROM mfa_recovery_requests WHERE id=%s AND status='pending' FOR UPDATE", (request_id,)).fetchone()
        if not row: raise HTTPException(404, "Pending recovery request not found.")
        db.execute("UPDATE users SET totp_enabled=false,totp_secret_enc=NULL WHERE id=%s", (row["target_user_id"],))
        db.execute("UPDATE mfa_recovery_requests SET status='approved',reviewed_at=now(),reviewed_by=%s WHERE id=%s", (admin["id"], request_id))
    return {"success": True}


@app.post("/api/auth/activate")
def activate(data: ActivationInput):
    data.validate_password()
    with connect() as db:
        row = db.execute("""SELECT * FROM auth_tokens WHERE token_hash=%s AND purpose='activation'
            AND used_at IS NULL AND expires_at>now() FOR UPDATE""", (digest(data.token),)).fetchone()
        if not row:
            raise HTTPException(400, "Invitation is invalid or expired. Contact your administrator.")
        user = db.execute("SELECT email FROM users WHERE id=%s FOR UPDATE", (row["user_id"],)).fetchone()
        user = user_by_email(db, user["email"].lower())
        if user["status"] != "invited" or user["tenant_status"] != "active":
            raise HTTPException(400, "Invitation or company is not active.")
        db.execute("UPDATE users SET password_hash=%s,status='active' WHERE id=%s",
                   (hash_password(data.password), user["id"]))
        db.execute("UPDATE auth_tokens SET used_at=now() WHERE user_id=%s AND purpose='activation'", (user["id"],))
    return {"success": True}


# Accountant workflow endpoints backed by the workflow extension tables.
@app.get("/api/accountant/inbox")
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


@app.post("/api/accountant/batches", status_code=201)
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


@app.get("/api/accountant/batches/{batch_id}")
def get_accountant_batch(batch_id: str, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        batch = db.execute("SELECT * FROM document_batches WHERE id=%s AND tenant_id=%s", (batch_id, user["tenant_id"])).fetchone()
        if not batch: raise HTTPException(404, "Batch not found.")
        items = db.execute("SELECT bd.*,d.document_code,d.original_filename FROM batch_documents bd JOIN documents d ON d.id=bd.document_id AND d.tenant_id=bd.tenant_id WHERE bd.batch_id=%s AND bd.tenant_id=%s ORDER BY bd.position", (batch_id, user["tenant_id"])).fetchall()
    return {"id": str(batch["id"]), "name": batch["name"], "status": batch["status"].upper(), "totalDocuments": batch["total_documents"], "processedDocuments": batch["processed_documents"], "failedDocuments": batch["failed_documents"], "documents": [{"id": str(r["document_id"]), "code": r["document_code"], "name": r["original_filename"], "status": r["status"].upper()} for r in items]}


@app.post("/api/accountant/batches/{batch_id}/cancel")
def cancel_accountant_batch(batch_id: str, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        row = db.execute("UPDATE document_batches SET status='cancelled' WHERE id=%s AND tenant_id=%s AND status IN ('queued','processing') RETURNING id,status", (batch_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Pending batch not found or already completed.")
    return {"id": str(row["id"]), "status": row["status"].upper()}


@app.post("/api/documents/{document_id}/review")
def submit_document_review(document_id: str, data: ReviewInput, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        document = db.execute("SELECT id,tenant_id,status,content_revision FROM documents WHERE id=%s AND tenant_id=%s FOR UPDATE", (document_id, user["tenant_id"])).fetchone()
        if not document: raise HTTPException(404, "Document not found.")
        if data.expectedContentRevision is not None and data.expectedContentRevision != document["content_revision"]:
            raise HTTPException(409, "The document changed. Refresh it before submitting for approval.")
        if data.decision != "submitted":
            raise HTTPException(422, "Accountants submit documents for approval; only local administrators can decide them.")
        db.execute("INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,note,content_revision) VALUES (%s,%s,%s,%s,%s,(SELECT content_revision FROM documents WHERE id=%s))", (document_id, user["tenant_id"], user["id"], data.decision, data.note, document_id))
        next_status = "in_review"
        db.execute("UPDATE documents SET status=%s,updated_at=now() WHERE id=%s", (next_status, document_id))
    return {"documentId": document_id, "decision": data.decision, "status": next_status.upper()}


@app.post("/api/documents/{document_id}/review/decision")
def decide_document_review(document_id: str, data: ReviewDecisionInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        document = db.execute("SELECT id,tenant_id,status,content_revision FROM documents WHERE id=%s AND tenant_id=%s FOR UPDATE", (document_id, user["tenant_id"])).fetchone()
        if not document:
            raise HTTPException(404, "Document not found.")
        if document["status"] != "in_review":
            raise HTTPException(409, "Only documents currently in review can be approved or returned.")
        if data.expectedContentRevision is not None and data.expectedContentRevision != document["content_revision"]:
            raise HTTPException(409, "The document changed. Refresh it before deciding this review.")
        submission = db.execute("""SELECT id FROM review_submissions
            WHERE document_id=%s AND tenant_id=%s AND decision='submitted' AND content_revision=%s AND submitted_by<>%s
            ORDER BY created_at DESC LIMIT 1""", (document_id, user["tenant_id"], document["content_revision"], user["id"])).fetchone()
        if not submission:
            raise HTTPException(422, "The document has not been submitted for approval.")
        db.execute("""INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,note,approved_content_revision)
            SELECT %s,%s,%s,%s,%s,%s,content_revision FROM documents WHERE id=%s AND tenant_id=%s""", (submission["id"], document_id, user["tenant_id"], user["id"], data.decision, data.note, document_id, user["tenant_id"]))
        next_status = "validated" if data.decision == "approved" else ("exception" if data.decision == "rejected" else "in_review")
        db.execute("UPDATE documents SET status=%s,updated_at=now() WHERE id=%s", (next_status, document_id))
    return {"documentId": document_id, "decision": data.decision, "reviewedBy": str(user["id"]), "status": next_status.upper()}


@app.get("/api/documents/{document_id}/comments")
def list_document_comments(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT c.*,u.name AS author_name FROM document_comments c JOIN users u ON u.id=c.author_id WHERE c.document_id=%s AND c.tenant_id=%s ORDER BY c.created_at", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "comment": r["comment"], "author": r["author_name"], "pageNumber": r["page_number"], "fieldKey": r["field_key"], "resolved": bool(r["resolved_at"]), "createdAt": str(r["created_at"])} for r in rows]


@app.post("/api/documents/{document_id}/comments", status_code=201)
def add_document_comment(document_id: str, data: CommentInput, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO document_comments(document_id,tenant_id,author_id,comment,page_number,field_key) SELECT id,tenant_id,%s,%s,%s,%s FROM documents WHERE id=%s AND tenant_id=%s RETURNING *", (user["id"], data.comment, data.pageNumber, data.fieldKey, document_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Document not found.")
    return {"id": str(row["id"]), "comment": row["comment"], "createdAt": str(row["created_at"])}


@app.get("/api/documents/{document_id}/reconciliations")
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


@app.post("/api/documents/{document_id}/reconciliations", status_code=201)
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


@app.get("/api/suppliers")
def list_suppliers(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM vendors WHERE tenant_id=%s ORDER BY vendor_name", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "name": r["vendor_name"], "registrationNumber": r["registration_number"], "address": r["address"]} for r in rows]


@app.post("/api/suppliers", status_code=201)
def create_supplier(data: NamedInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,%s) RETURNING *", (user["tenant_id"], data.name)).fetchone()
    return {"id": str(row["id"]), "name": row["vendor_name"]}


@app.get("/api/document-tags")
def list_document_tags(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM document_tags WHERE tenant_id=%s ORDER BY name", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "name": r["name"], "color": r["color"]} for r in rows]


@app.post("/api/document-tags", status_code=201)
def create_document_tag(data: NamedInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO document_tags(tenant_id,name,color) VALUES (%s,%s,%s) RETURNING *", (user["tenant_id"], data.name, data.color)).fetchone()
    return {"id": str(row["id"]), "name": row["name"], "color": row["color"]}


@app.delete("/api/document-tags/{tag_id}")
def delete_document_tag(tag_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("DELETE FROM document_tags WHERE id=%s AND tenant_id=%s RETURNING id", (tag_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Document tag not found.")
    return {"deleted": True, "id": str(row["id"])}


@app.post("/api/documents/{document_id}/tags/{tag_id}", status_code=201)
def tag_document(document_id: str, tag_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        row = db.execute("""INSERT INTO document_tag_links(document_id,tag_id,created_by)
            SELECT d.id,t.id,%s FROM documents d JOIN document_tags t ON t.tenant_id=d.tenant_id
            WHERE d.id=%s AND d.tenant_id=%s AND t.id=%s RETURNING document_id,tag_id""", (user["id"], document_id, user["tenant_id"], tag_id)).fetchone()
    if not row: raise HTTPException(404, "Document or tag not found.")
    return {"documentId": str(row["document_id"]), "tagId": str(row["tag_id"])}


@app.get("/api/saved-filters")
def list_saved_filters(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM saved_filters WHERE tenant_id=%s AND (created_by=%s OR is_shared) ORDER BY name", (user["tenant_id"], user["id"])).fetchall()
    return [{"id": str(r["id"]), "name": r["name"], "resource": r["resource"], "definition": r["definition"], "isShared": r["is_shared"]} for r in rows]


@app.post("/api/saved-filters", status_code=201)
def create_saved_filter(data: FilterInput, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("INSERT INTO saved_filters(tenant_id,created_by,name,resource,definition,is_shared) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *", (user["tenant_id"], user["id"], data.name, data.resource, data.definition, data.isShared)).fetchone()
    return {"id": str(row["id"]), "name": row["name"], "resource": row["resource"], "definition": row["definition"], "isShared": row["is_shared"]}


@app.delete("/api/saved-filters/{filter_id}")
def delete_saved_filter(filter_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("DELETE FROM saved_filters WHERE id=%s AND tenant_id=%s AND created_by=%s RETURNING id", (filter_id, user["tenant_id"], user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Saved filter not found.")
    return {"deleted": True, "id": str(row["id"])}


@app.post("/api/review-reminders", status_code=201)
def create_review_reminder(data: ReminderInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO review_reminders(tenant_id,document_id,assigned_to,remind_at,created_by) SELECT tenant_id,id,%s,%s,%s FROM documents WHERE id=%s AND tenant_id=%s RETURNING *", (data.assignedTo, data.remindAt, user["id"], data.documentId, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Document not found.")
    return {"id": str(row["id"]), "documentId": str(row["document_id"]), "remindAt": str(row["remind_at"])}


@app.get("/api/notifications")
def list_notifications(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC", (user["id"],)).fetchall()
    return [{"id": str(r["id"]), "type": r["type"], "title": r["title"], "message": r["message"], "resource": r["resource"], "read": bool(r["read_at"]), "createdAt": str(r["created_at"])} for r in rows]


@app.patch("/api/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("UPDATE notifications SET read_at=COALESCE(read_at,now()) WHERE id=%s AND user_id=%s RETURNING id,read_at", (notification_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Notification not found.")
    return {"id": str(row["id"]), "read": True, "readAt": str(row["read_at"])}


@app.post("/api/backups", status_code=201)
def request_backup(data: BackupInput, request: Request):
    user = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO backup_runs(requested_by,backup_type) VALUES (%s,%s) RETURNING *", (user["id"], data.backupType)).fetchone()
    return {"id": str(row["id"]), "backupType": row["backup_type"], "status": row["status"]}


@app.get("/api/backups")
def list_backups(request: Request):
    user = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        rows = db.execute("SELECT * FROM backup_runs ORDER BY created_at DESC LIMIT 100").fetchall()
    return [{"id": str(r["id"]), "backupType": r["backup_type"], "status": r["status"], "requestedAt": str(r["created_at"]), "startedAt": str(r["started_at"]) if r["started_at"] else None, "finishedAt": str(r["completed_at"]) if r["completed_at"] else None, "error": r["error_message"]} for r in rows]


@app.get("/api/backups/{backup_id}")
def get_backup(backup_id: str, request: Request):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("SELECT * FROM backup_runs WHERE id=%s", (backup_id,)).fetchone()
    if not row: raise HTTPException(404, "Backup run not found.")
    return {"id": str(row["id"]), "backupType": row["backup_type"], "status": row["status"], "requestedAt": str(row["created_at"]), "startedAt": str(row["started_at"]) if row["started_at"] else None, "finishedAt": str(row["completed_at"]) if row["completed_at"] else None, "error": row["error_message"]}


@app.get("/api/documents/{document_id}/status-history")
def document_status_history(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM document_status_history WHERE document_id=%s AND tenant_id=%s ORDER BY created_at", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "oldStatus": r["old_status"], "newStatus": r["new_status"], "reason": r["reason"], "createdAt": str(r["created_at"])} for r in rows]


@app.post("/api/documents/{document_id}/workload", status_code=201)
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


@app.get("/api/workload")
def list_workload(request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("""SELECT w.*,d.document_code,d.original_filename,u.name AS assignee_name
            FROM workload_assignments w JOIN documents d ON d.id=w.document_id
            JOIN users u ON u.id=w.assigned_to
            WHERE w.tenant_id=%s AND w.completed_at IS NULL ORDER BY w.due_at NULLS LAST,w.priority DESC""", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "documentId": str(r["document_id"]), "documentCode": r["document_code"], "filename": r["original_filename"], "assignedTo": str(r["assigned_to"]), "assigneeName": r["assignee_name"], "priority": r["priority"], "dueAt": str(r["due_at"] or "")} for r in rows]


@app.get("/api/documents/{document_id}/supplier-matches")
def list_supplier_matches(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM supplier_matches WHERE document_id=%s AND tenant_id=%s ORDER BY confidence DESC NULLS LAST", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "candidateName": r["candidate_name"], "vendorId": str(r["vendor_id"]) if r["vendor_id"] else None, "confidence": float(r["confidence"]) if r["confidence"] is not None else None, "decision": r["decision"]} for r in rows]


@app.patch("/api/supplier-matches/{match_id}")
def decide_supplier_match(match_id: str, data: SupplierMatchInput, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        row = db.execute("""UPDATE supplier_matches SET decision=%s, vendor_id=%s, decided_by=%s, decided_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (data.decision, data.vendorId, user["id"], match_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Supplier match not found.")
    return {"id": str(row["id"]), "decision": row["decision"], "vendorId": str(row["vendor_id"]) if row["vendor_id"] else None}


@app.get("/api/documents/{document_id}/customer-matches")
def list_customer_matches(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM customer_matches WHERE document_id=%s AND tenant_id=%s ORDER BY confidence DESC NULLS LAST", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "candidateName": r["candidate_name"], "customerId": str(r["customer_id"]) if r["customer_id"] else None, "confidence": float(r["confidence"]) if r["confidence"] is not None else None, "decision": r["decision"]} for r in rows]


@app.patch("/api/customer-matches/{match_id}")
def decide_customer_match(match_id: str, data: CustomerMatchInput, request: Request):
    user = require_workspace_user(request, {"accountant", "local_admin"})
    with connect() as db:
        row = db.execute("""UPDATE customer_matches SET decision=%s, customer_id=%s, decided_by=%s, decided_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (data.decision, data.customerId, user["id"], match_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Customer match not found.")
    return {"id": str(row["id"]), "decision": row["decision"], "customerId": str(row["customer_id"]) if row["customer_id"] else None}


@app.post("/api/exports", status_code=201)
def create_export(data: ExportInput, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("INSERT INTO export_history(tenant_id,requested_by,resource,filters,format,status,completed_at) VALUES (%s,%s,%s,%s,%s,'completed',now()) RETURNING *", (user["tenant_id"], user["id"], data.resource, data.filters, data.format)).fetchone()
    return {"id": str(row["id"]), "resource": row["resource"], "format": row["format"], "status": row["status"], "rowCount": row["row_count"], "createdAt": str(row["created_at"])}


@app.get("/api/exports")
def list_exports(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM export_history WHERE tenant_id=%s ORDER BY created_at DESC", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "resource": r["resource"], "format": r["format"], "status": r["status"], "rowCount": r["row_count"], "createdAt": str(r["created_at"])} for r in rows]


@app.get("/api/exports/{export_id}/download")
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


@app.post("/api/documents/{document_id}/versions", status_code=201)
def create_document_version(document_id: str, data: VersionInput, request: Request):
    user = require_workspace_user(request, {"local_admin", "accountant"})
    with connect() as db:
        source = db.execute("SELECT storage_path,file_hash_sha256,file_size_bytes FROM documents WHERE id=%s AND tenant_id=%s", (document_id, user["tenant_id"])).fetchone()
        if not source: raise HTTPException(404, "Document not found.")
        supplied = Path(data.storagePath).resolve()
        storage_root = Path(os.getenv("DOCUMENT_STORAGE_PATH", "storage")).resolve()
        if storage_root not in supplied.parents or not supplied.is_file():
            raise HTTPException(422, "The version file must be an existing private stored file.")
        actual_hash = hashlib.sha256(supplied.read_bytes()).hexdigest()
        if actual_hash.lower() != data.fileHashSha256.lower() or supplied.stat().st_size != data.fileSizeBytes:
            raise HTTPException(422, "Version metadata does not match the stored file.")
        row = db.execute("""INSERT INTO document_versions(document_id,tenant_id,version_number,storage_path,file_hash_sha256,file_size_bytes,created_by)
            SELECT id,tenant_id,COALESCE((SELECT max(version_number)+1 FROM document_versions WHERE document_id=%s),1),%s,%s,%s,%s
            FROM documents WHERE id=%s AND tenant_id=%s RETURNING *""", (document_id, data.storagePath, data.fileHashSha256, data.fileSizeBytes, user["id"], document_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Document not found.")
    return {"id": str(row["id"]), "documentId": document_id, "versionNumber": row["version_number"], "storagePath": row["storage_path"], "createdAt": str(row["created_at"])}


@app.get("/api/documents/{document_id}/versions")
def list_document_versions(document_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM document_versions WHERE document_id=%s AND tenant_id=%s ORDER BY version_number DESC", (document_id, user["tenant_id"])).fetchall()
    return [{"id": str(r["id"]), "versionNumber": r["version_number"], "storagePath": r["storage_path"], "fileHashSha256": r["file_hash_sha256"], "createdAt": str(r["created_at"])} for r in rows]


@app.get("/api/documents/{document_id}/versions/{version_id}/file")
def download_document_version(document_id: str, version_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("SELECT storage_path FROM document_versions WHERE id=%s AND document_id=%s AND tenant_id=%s", (version_id, document_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Document version not found.")
    path = Path(row["storage_path"]).resolve()
    storage_root = Path(os.getenv("DOCUMENT_STORAGE_PATH", "storage")).resolve()
    if storage_root not in path.parents or not path.is_file():
        raise HTTPException(404, "Version file is unavailable.")
    return FileResponse(path, filename=path.name)


@app.get("/api/permissions")
def list_permissions(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    with connect() as db:
        rows = db.execute("SELECT permission_key,description FROM permission_catalog ORDER BY permission_key").fetchall()
    return [{"key": r["permission_key"], "description": r["description"]} for r in rows]


@app.patch("/api/users/{user_id}/permissions")
def set_user_permission(user_id: str, data: PermissionInput, request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    with connect() as db:
        target = db.execute("SELECT id FROM users WHERE id=%s AND (%s='super_admin' OR tenant_id=%s)", (user_id, user["role"], user["tenant_id"])).fetchone()
        if not target: raise HTTPException(404, "User not found.")
        db.execute("""INSERT INTO user_permissions(user_id,permission_key,granted,granted_by,updated_at)
            VALUES (%s,%s,%s,%s,now()) ON CONFLICT (user_id,permission_key) DO UPDATE SET granted=EXCLUDED.granted,granted_by=EXCLUDED.granted_by,updated_at=now()""", (user_id, data.permissionKey, data.granted, user["id"]))
    return {"userId": user_id, "permissionKey": data.permissionKey, "granted": data.granted}


@app.get("/api/platform/notifications")
def list_platform_notifications(request: Request):
    require_workspace_user(request, {"super_admin"})
    with connect() as db:
        rows = db.execute("SELECT * FROM platform_notifications ORDER BY created_at DESC").fetchall()
    return [{"id": str(r["id"]), "type": r["notification_type"], "severity": r["severity"], "title": r["title"], "message": r["message"], "createdAt": str(r["created_at"])} for r in rows]


@app.post("/api/platform/notifications", status_code=201)
def create_platform_notification(data: PlatformNotificationInput, request: Request):
    user = require_workspace_user(request, {"super_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO platform_notifications(notification_type,severity,title,message,created_by) VALUES (%s,%s,%s,%s,%s) RETURNING *", (data.notificationType, data.severity, data.title, data.message, user["id"])).fetchone()
    return {"id": str(row["id"]), "severity": row["severity"], "title": row["title"], "message": row["message"]}


@app.post("/api/record-amendments/{amendment_id}/decision")
def decide_record_amendment(amendment_id: str, data: AmendmentDecisionInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("""UPDATE record_amendments SET status=%s,approved_by=%s,approved_at=now()
            WHERE id=%s AND tenant_id=%s RETURNING *""", (data.decision, user["id"], amendment_id, user["tenant_id"])).fetchone()
    if not row: raise HTTPException(404, "Amendment request not found.")
    return {"id": str(row["id"]), "status": row["status"], "approvedBy": str(row["approved_by"])}


@app.get("/api/documents/{document_id}/kind-workflow")
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


@app.post("/api/customers", status_code=201)
def create_customer(data: PartyInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO customers(tenant_id,customer_name,registration_number,address) VALUES (%s,%s,%s,%s) RETURNING *", (user["tenant_id"], data.name, data.registrationNumber, data.address)).fetchone()
    return {"id": str(row["id"]), "name": row["customer_name"], "registrationNumber": row["registration_number"], "address": row["address"]}


@app.get("/api/customers")
def list_customers(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM customers WHERE tenant_id=%s ORDER BY customer_name", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "name": r["customer_name"], "registrationNumber": r["registration_number"], "address": r["address"]} for r in rows]


@app.get("/api/exports/xlsx")
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


@app.post("/api/documents/bulk-action")
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


@app.post("/api/custom-fields", status_code=201)
def create_custom_field(data: CustomFieldInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("INSERT INTO custom_fields(tenant_id,field_key,label,field_type,options,required) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *", (user["tenant_id"], data.fieldKey, data.label, data.fieldType, data.options, data.required)).fetchone()
    return {"id": str(row["id"]), "fieldKey": row["field_key"], "label": row["label"], "fieldType": row["field_type"], "options": row["options"], "required": row["required"]}


@app.get("/api/custom-fields")
def list_custom_fields(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT * FROM custom_fields WHERE tenant_id=%s ORDER BY label", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "fieldKey": r["field_key"], "label": r["label"], "fieldType": r["field_type"], "options": r["options"], "required": r["required"]} for r in rows]


# Keep the generated OpenAPI documentation usable as the API grows. Routes are
# grouped by capability without changing their paths or authorization rules.
_OPENAPI_TAGS = [
    ("/api/auth", "Authentication"),
    ("/api/workspace/navigation", "Workspace"),
    ("/api/tenants", "Tenant Management"),
    ("/api/company-requests", "Tenant Management"),
    ("/api/account-limit-requests", "Tenant Management"),
    ("/api/users", "User Management"),
    ("/api/documents", "Documents"),
    ("/api/exceptions", "Validation & Exceptions"),
    ("/api/validation-rules", "Validation & Exceptions"),
    ("/api/accountant/inbox", "Accountant Workspace"),
    ("/api/accountant/batches", "Accountant Workspace"),
    ("/api/document-tags", "Document Management"),
    ("/api/saved-filters", "Document Management"),
    ("/api/review-reminders", "Notifications"),
    ("/api/notifications", "Notifications"),
    ("/api/backups", "System Operations"),
    ("/api/admin/mfa-recovery-requests", "Security Administration"),
    ("/api/supplier-matches", "Contacts"),
    ("/api/customer-matches", "Contacts"),
    ("/api/platform/notifications", "Platform Administration"),
    ("/api/custom-fields", "Workspace Configuration"),
    ("/api/documents", "Documents"),
    ("/api/suppliers", "Contacts"),
    ("/api/customers", "Contacts"),
    ("/api/processing", "AI Processing"),
    ("/api/records", "Standardised Records"),
    ("/api/record-amendments", "Standardised Records"),
    ("/api/reports", "Reports & Exports"),
    ("/api/exports", "Reports & Exports"),
    ("/api/audit", "Audit & Security"),
    ("/api/permissions", "Audit & Security"),
]
for _route in app.routes:
    if hasattr(_route, "path") and _route.path.startswith("/api/") and not getattr(_route, "tags", None):
        _path = _route.path
        _special = (
            "Review & Approval" if "/review" in _path or "/review/" in _path else
            "Contacts" if "-matches" in _path else
            next((tag for prefix, tag in _OPENAPI_TAGS if _path.startswith(prefix)), "Platform")
        )
        _route.tags = [_special]
