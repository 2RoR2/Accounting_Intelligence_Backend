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
from services.extraction.contracts.accounting_contract import (
    approved_snapshot, decimal_text, field_value, line_dto, prediction_dto, record_values, CURRENCY_MINOR_UNITS,
)
from validation.documents.document_validation import evaluate_invoice, evaluate_document, load_current, validate_document

from security.authentication import (
    ACCESS_TOKEN_SECONDS,
    SESSION_SECONDS,
    backup_code_hash,
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
from services.extraction.contracts.reliability import extraction_guidance
from database.connection import connect
from services.notifications.mailer import send_login_code, send_reset_code
from services.routes import router


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


def check_schema_compatibility():
    """Read-only prerequisites; owner migrations are an explicit services/admin/manage.py action."""
    from database.schema_compatibility import check_runtime_schema
    with connect() as db:
        check_runtime_schema(db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Check schema prerequisites without mutating the database at startup."""
    check_schema_compatibility()
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
            "role": user["role"].upper(), "tenantId": str(user['tenant_id']) if user['tenant_id'] else None,
            "tenantCode": user.get('tenant_code'),
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


class LineItemCorrectionInput(FieldCorrectionInput):
    value: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000)]



class RecordAmendmentInput(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class ReconciliationInput(BaseModel):
    relatedDocumentId: str
    relationType: Annotated[str, StringConstraints(pattern=r"^(credit_for|debit_for|adjustment_of|related_to)$")]
    amount: str | None = Field(default=None, pattern=r"^(0|[1-9]\d*)(\.\d{1,2})?$")
    note: str = Field(default="", max_length=2000)


class BankTransactionInput(BaseModel):
    postedDate: date | None = None
    valueDate: date | None = None
    description: str = Field(default="", max_length=2000)
    reference: str | None = Field(default=None, max_length=500)
    debit: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    credit: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")


class BankImportInput(BaseModel):
    sourceBankAccountRef: str = Field(min_length=1, max_length=200)
    mappingVersion: str = Field(default="v1", min_length=1, max_length=100)
    rows: list[BankTransactionInput] = Field(min_length=1, max_length=10000)


class ReconciliationCandidateInput(BaseModel):
    bankTransactionId: str
    recordId: str
    proposedBy: Annotated[str, StringConstraints(pattern=r"^(rules|semantic_match|accountant)$")] = "accountant"
    reasonCodes: list[str] = Field(default_factory=list, max_length=20)


class ReconciliationDecisionInput(BaseModel):
    candidateId: str
    decision: Annotated[str, StringConstraints(pattern=r"^(confirmed|rejected|reversed)$")]
    expectedRevision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class AccountInput(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    accountClass: Annotated[str, StringConstraints(pattern=r"^(asset|liability|equity|revenue|cogs|expense)$")]
    parentId: str | None = None


class CostCentreInput(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)


class BankCodingInput(BaseModel):
    accountId: str | None = None
    costCentreId: str | None = None
    decision: Annotated[str, StringConstraints(pattern=r"^(accepted|overridden|manual|cleared)$")] = "manual"
    reason: str | None = Field(default=None, max_length=2000)
    suggestionId: str | None = None


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


def _lock_upload_identity(db,tenant_id,file_hash):
    # Shared PostgreSQL lock across API processes, held until document/job commit.
    # A 64-bit collision only serializes unrelated uploads; lookup remains tenant/hash exact.
    key=int.from_bytes(hashlib.sha256(
        ("saic:upload:v1:"+str(tenant_id)+":"+file_hash).encode("ascii")).digest()[:8],
        byteorder="big",signed=True)
    row=db.execute(
        "SELECT pg_advisory_xact_lock(%s), current_setting('transaction_isolation') AS isolation",
        (key,)).fetchone()
    # READ COMMITTED takes a fresh duplicate-query snapshot AFTER a waiting lock.
    if not row or row.get('isolation')!='read committed':
        raise HTTPException(503,"Document upload requires a READ COMMITTED database transaction.")


def _processing_error(exc, document_id, job_id=None):
    detail = {'code':exc.code,'documentId':str(document_id),'dispatchAccepted':False,
              'message':'Processing could not be dispatched.' if exc.code=='DISPATCH_UNCONFIRMED'
                        else 'Processing is not eligible for retry in its current state.'}
    if job_id:
        detail['jobId']=str(job_id)
    if exc.job:
            detail.update(jobId=str(exc.job['id']),jobStatus=str(exc.job.get('status') or 'UNKNOWN').upper())
    raise HTTPException(503 if exc.code=='DISPATCH_UNCONFIRMED' else 409,detail)


def _lock_workspace_document(db, document_id, user, *, protect_record=True):
    """Authorize and lock within the caller's transaction; display codes never scope SQL."""
    if user['role'] not in {'accountant', 'local_admin'} or not user.get('tenant_id'):
        raise HTTPException(403, 'This document action requires a company accountant or administrator.')
    visibility = ' AND (d.owner_id=%s OR d.assigned_to=%s)' if user['role'] == 'accountant' else ''
    params = [document_id, user['tenant_id']] + ([user['id'], user['id']] if visibility else [])
    document = db.execute('SELECT d.* FROM documents d WHERE d.id=%s AND d.tenant_id=%s' + visibility + ' FOR UPDATE', params).fetchone()
    if not document:
        raise HTTPException(404, 'Document not found.')
    if protect_record and (document['status'] in {'completed', 'standardised'} or db.execute(
            'SELECT 1 FROM standardised_records WHERE document_id=%s LIMIT 1', (document_id,)).fetchone()):
        raise HTTPException(409, 'Standardised documents require an amendment.')
    return document


def _require_current_extraction(db, document_id):
    extraction = db.execute('SELECT * FROM raw_extractions WHERE document_id=%s AND is_current=true', (document_id,)).fetchone()
    if not extraction:
        raise HTTPException(409, 'The document has no current extraction to review.')
    return extraction






















































































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

































































# Accountant workflow endpoints backed by the workflow extension tables.






































































































def _bank_row_values(row: BankTransactionInput):
    debit = row.debit
    credit = row.credit
    if debit is not None and debit == 0:
        debit = None
    if credit is not None and credit == 0:
        credit = None
    if (debit is not None) == (credit is not None):
        return None, None, None, "A row must contain exactly one positive debit or credit amount."
    currency = row.currency.upper() if row.currency else None
    if not currency:
        return None, None, None, "A currency is required for a Money In/Money Out row."
    if debit is not None:
        return "money_out", debit, -debit, None
    return "money_in", credit, credit, None


def _bank_transaction_dto(row):
    return {
        "id": str(row["id"]), "importId": str(row["import_id"]), "sourceRow": row["source_row"],
        "postedDate": str(row["posted_date"]) if row["posted_date"] else None,
        "valueDate": str(row["value_date"]) if row["value_date"] else None,
        "description": row["description"], "reference": row["reference"],
        "debit": decimal_text(row["debit_decimal"]), "credit": decimal_text(row["credit_decimal"]),
        "amount": decimal_text(abs(row["signed_amount_decimal"])) if row["signed_amount_decimal"] is not None else None,
        "signedAmount": decimal_text(row["signed_amount_decimal"]), "direction": row["direction"],
        "currency": row["currency"], "status": row["status"], "createdAt": str(row["created_at"]),
    }














def _uuid_or_422(value, label):
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, f"Invalid {label}.")






def _account_dto(row):
    return {"id": str(row["id"]), "code": row["code"], "name": row["name"],
            "accountClass": row["account_class"], "parentId": str(row["parent_id"]) if row["parent_id"] else None,
            "isActive": row["is_active"]}










def _cost_centre_dto(row):
    return {"id": str(row["id"]), "code": row["code"], "name": row["name"], "isActive": row["is_active"]}














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
    ("/api/bank", "Money In & Money Out"),
    ("/api/audit", "Audit & Security"),
    ("/api/permissions", "Audit & Security"),
]
# Register component routes before applying the shared OpenAPI tag policy.
from services.routes.registry import register_component_routes
register_component_routes(router, globals())
app.include_router(router)

for _route in app.routes:
    if hasattr(_route, "path") and _route.path.startswith("/api/") and not getattr(_route, "tags", None):
        _path = _route.path
        _special = (
            "Review & Approval" if "/review" in _path or "/review/" in _path else
            "Contacts" if "-matches" in _path else
            next((tag for prefix, tag in _OPENAPI_TAGS if _path.startswith(prefix)), "Platform")
        )
        _route.tags = [_special]

