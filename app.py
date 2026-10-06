"""Public account API. Run: uvicorn app:app --host 127.0.0.1 --port 8000."""
import hmac
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import jwt
from pydantic import BaseModel, Field, StringConstraints
from psycopg.errors import UniqueViolation

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

app = FastAPI(title="Accounting Intelligence account API")
log = logging.getLogger(__name__)
SESSION = "ai_session"
ACCESS = "ai_access"
REFRESH = "ai_refresh"
MFA = "ai_mfa"
RESET = "ai_reset"
PRIVILEGED_ROLES = frozenset({"super_admin", "local_admin"})
WORKSPACE_PERMISSIONS = {
    "admin": ("super_admin", frozenset({"dashboard", "approvals", "tenants", "users", "processing", "performance", "records", "audit", "settings", "profile"})),
    "company": ("local_admin", frozenset({"dashboard", "documents", "upload", "exceptions", "records", "users", "validation-rules", "reports", "audit", "settings", "profile"})),
    "workspace": ("accountant", frozenset({"dashboard", "documents", "upload", "exceptions", "records", "profile"})),
}
DETAIL_PAGES = frozenset({"tenants", "documents", "records"})
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=254,
                                         pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Password = Annotated[str, StringConstraints(min_length=1, max_length=256)]
DUMMY_PASSWORD = hash_password(secrets.token_urlsafe(32))


def utcnow():
    return datetime.now(timezone.utc)


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
                        samesite="strict", path="/api")
    response.delete_cookie(SESSION, path="/api")
    response.set_cookie(REFRESH, refresh_token, max_age=max(1, int((expires_at - utcnow()).total_seconds())),
                        httponly=True, secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        samesite="strict", path="/api/auth")


def clear_auth_cookies(response: Response):
    response.delete_cookie(ACCESS, path="/api")
    response.delete_cookie(REFRESH, path="/api/auth")
    response.delete_cookie(MFA, path="/api/auth")
    response.delete_cookie(SESSION, path="/api")


def user_by_email(db, email):
    return db.execute("""SELECT u.*, t.tenant_code, t.status AS tenant_status
        FROM users u LEFT JOIN tenants t ON t.id=u.tenant_id WHERE lower(u.email)=%s""", (email,)).fetchone()


def active(user):
    return user and user["status"] == "active" and (not user["tenant_id"] or user["tenant_status"] == "active")


def public_user(user):
    return {"id": str(user["id"]), "name": user["name"], "email": user["email"],
            "role": user["role"].upper(), "tenantId": user["tenant_code"],
            "status": user["status"].upper(), "mustChangePassword": user["must_change_password"],
            "invitedBy": str(user["invited_by"] or ""),
            "invitedAt": str(user["invited_at"] or ""), "lastLogin": str(user["last_login_at"] or "")}


def session_user(db, request):
    token = request.cookies.get(ACCESS, "")
    try:
        claims = decode_access_token(token)
    except (jwt.InvalidTokenError, RuntimeError):
        raise HTTPException(401, "Sign in with an active company account.")
    user = db.execute("""SELECT u.*, t.tenant_code, t.status AS tenant_status
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
            raise HTTPException(422, "Use 8–256 characters with uppercase, lowercase, number and symbol; match both passwords.")


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
                log.error("Login OTP delivery failed: %s", type(exc).__name__)
                raise HTTPException(503, "Unable to send sign-in code. Please try again later.")
        else:
            db.execute("DELETE FROM auth_challenges WHERE challenge_hash=%s",
                       (digest(request.cookies.get(MFA, "")),))
            session_id, refresh_token, expires_at = issue_session(db, user)
    if user["role"] in PRIVILEGED_ROLES:
        clear_auth_cookies(response)
        response.set_cookie(MFA, challenge, max_age=600, httponly=True,
                            secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                            samesite="strict", path="/api/auth")
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
        response.set_cookie(MFA, challenge, max_age=600, httponly=True, secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", samesite="strict", path="/api/auth")
        return {"mfaRequired": True, "enrollmentRequired": False, "mfaMethod": "email"}
    set_user_session_cookies(response, user, session_id, refresh_token, expires_at)
    response.delete_cookie(MFA, path="/api/auth")
    return public_user(user)


@app.get("/api/auth/me")
def me(request: Request):
    with connect() as db:
        return public_user(session_user(db, request))


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


@app.post("/api/auth/forgot-password")
def forgot_password(data: EmailInput, response: Response):
    signing_secret = secret()
    rate_limit("reset-minute:" + data.email, 1, 60)
    rate_limit("reset-window:" + data.email, 5, 900)
    response.delete_cookie(RESET, path="/api/auth")
    with connect() as db:
        user = user_by_email(db, data.email)
        if not active(user):
            raise HTTPException(404, "You do not have an active account yet. Return to the home screen or request company access.")
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
                        samesite="strict", path="/api/auth")
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
