from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto

def update_profile(data: ProfileInput, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        db.execute("UPDATE users SET name=%s, avatar=%s WHERE id=%s", (data.name.strip(), data.avatar or None, user["id"]))
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,%s,%s,'PROFILE_UPDATED',%s,'Profile updated.')""",
            (user["tenant_id"], user["id"], data.name.strip(), user["role"], user["email"]))
        updated = db.execute("SELECT * FROM users WHERE id=%s", (user["id"],)).fetchone()
    return api_user(updated)

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
                       (digest(challenge), user["id"], pending_secret, utcnow() + timedelta(seconds=60)))
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
        response.set_cookie(MFA, challenge, max_age=60, httponly=True,
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
        response.set_cookie(MFA, challenge, max_age=60, httponly=True, secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", samesite=auth_cookie_samesite(), path="/api/auth")
        return {"mfaRequired": True, "enrollmentRequired": False, "mfaMethod": "email"}
    set_user_session_cookies(response, user, session_id, refresh_token, expires_at)
    response.delete_cookie(MFA, path="/api/auth")
    return public_user(user)

def me(request: Request):
    with connect() as db:
        return public_user(session_user(db, request))

def generate_backup_codes(request: Request):
    user = require_workspace_user(request, roles=PRIVILEGED_ROLES)
    codes = [f"{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}" for _ in range(10)]
    with connect() as db:
        db.execute("DELETE FROM mfa_backup_codes WHERE user_id=%s", (user["id"],))
        for code in codes:
            db.execute("INSERT INTO mfa_backup_codes(user_id,code_hash) VALUES (%s,%s)", (user["id"], backup_code_hash(code)))
    return {"codes": codes, "generatedAt": utcnow().isoformat()}

def recover_lost_authenticator(data: dict, request: Request, response: Response):
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not email or not password:
        raise HTTPException(400, "Email and password are required.")
    with connect() as db:
        user = user_by_email(db, email)
        if not user or user["role"] not in PRIVILEGED_ROLES or not active(user) or not verify_password(password, user["password_hash"] or ""):
            raise HTTPException(401, "The email or password is invalid.")
        db.execute("DELETE FROM auth_challenges WHERE user_id=%s", (user["id"],))
        challenge = secrets.token_urlsafe(32)
        pending = encrypt_totp_secret(new_totp_secret())
        db.execute("INSERT INTO auth_challenges(challenge_hash,user_id,pending_totp_secret_enc,expires_at) VALUES (%s,%s,%s,%s)", (digest(challenge), user["id"], pending, utcnow() + timedelta(seconds=60)))
    response.set_cookie(MFA, challenge, max_age=60, httponly=True, secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", samesite=auth_cookie_samesite(), path="/api/auth")
    totp_secret = decrypt_totp_secret(pending)
    label = quote(email, safe="")
    return {"mfaRequired": True, "enrollmentRequired": True, "totpSecret": totp_secret, "otpauthUrl": f"otpauth://totp/Accounting%20Intelligence:{label}?secret={totp_secret}&issuer=Accounting%20Intelligence&algorithm=SHA1&digits=6&period=30"}

def list_auth_sessions(request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        rows = db.execute("SELECT session_id,expires_at FROM web_sessions WHERE user_id=%s AND expires_at>now() ORDER BY expires_at DESC", (user["id"],)).fetchall()
    return [{"id": str(row["session_id"]), "expiresAt": str(row["expires_at"]), "current": False} for row in rows]

def resend_mfa(request: Request, response: Response):
    """Issue a fresh email MFA code for the current login challenge."""
    challenge = request.cookies.get(MFA, "")
    if not challenge:
        raise HTTPException(400, "Your sign-in challenge has expired. Sign in again.")
    rate_limit("resend-mfa:" + digest(challenge), 3, 60)
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
    response.set_cookie(MFA, new_challenge, max_age=60, httponly=True,
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        samesite=auth_cookie_samesite(), path="/api/auth")
    return {"message": "A new sign-in code has been sent.", "expiresIn": 60}

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

def logout(request: Request, response: Response):
    with connect() as db:
        db.execute("DELETE FROM web_sessions WHERE token_hash=%s",
                   (digest(request.cookies.get(REFRESH, "")),))
    clear_auth_cookies(response)
    response.delete_cookie(RESET, path="/api/auth")
    return {"success": True}

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
            (data.email, user["id"], otp_hash(signing_secret, data.email, code), utcnow() + timedelta(seconds=60)))
        try:
            send_reset_code(data.email, code)
        except Exception as exc:
            log.error("Reset email delivery failed: %s", type(exc).__name__)
            raise HTTPException(503, "Unable to send email. Please try again later or contact support.")
    return {"message": "If this email belongs to an active account, a reset code has been sent."}

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
                           (digest(token), utcnow() + timedelta(seconds=60), data.email))
    # Raise after committing so wrong-code attempts cannot be rolled back.
    if not valid:
        raise HTTPException(400, "Code is invalid, expired or already used. Request a new code.")
    response.set_cookie(RESET, token, max_age=60, httponly=True,
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        samesite=auth_cookie_samesite(), path="/api/auth")
    return {"success": True}

def reset_status(request: Request):
    with connect() as db:
        row = db.execute("SELECT 1 FROM password_challenges WHERE grant_hash=%s AND grant_expires_at>now()",
                         (digest(request.cookies.get(RESET, "")),)).fetchone()
    if not row:
        raise HTTPException(401, "Verify your email code before resetting your password.")
    return {"valid": True}

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

def request_lost_authenticator_recovery(data: dict, request: Request):
    """Create a pending recovery request from the password-verified MFA challenge.

    This does not reset MFA or grant access. A Super Admin must still approve it.
    """
    reason = str(data.get("reason", "")).strip()[:500]
    if not reason:
        raise HTTPException(400, "A recovery reason is required.")
    challenge = request.cookies.get(MFA, "")
    if not challenge:
        raise HTTPException(401, "Your sign-in challenge has expired. Sign in again.")
    with connect() as db:
        row = db.execute("""SELECT c.user_id,u.email,u.role
            FROM auth_challenges c JOIN users u ON u.id=c.user_id
            WHERE c.challenge_hash=%s AND c.expires_at>now() AND c.attempts<5""", (digest(challenge),)).fetchone()
        if not row or row["role"] not in PRIVILEGED_ROLES:
            raise HTTPException(401, "Your sign-in challenge has expired. Sign in again.")
        recovery = db.execute("""INSERT INTO mfa_recovery_requests(requester_id,target_user_id,reason)
            VALUES (%s,%s,%s) ON CONFLICT (target_user_id,status) DO UPDATE SET reason=EXCLUDED.reason,created_at=now()
            RETURNING id,status,created_at""", (row["user_id"], row["user_id"], reason)).fetchone()
    return {"id": str(recovery["id"]), "status": recovery["status"], "createdAt": str(recovery["created_at"])}

def list_mfa_recovery_requests(request: Request):
    with connect() as db:
        admin = session_user(db, request)
        if admin["role"] != "super_admin": raise HTTPException(403, "Super Admin approval required.")
        rows = db.execute("""SELECT r.*,u.email AS target_email,u.name AS target_name
            FROM mfa_recovery_requests r JOIN users u ON u.id=r.target_user_id
            ORDER BY r.created_at DESC""").fetchall()
    return [{**dict(row), "id": str(row["id"]), "requester_id": str(row["requester_id"]), "target_user_id": str(row["target_user_id"])} for row in rows]

def approve_mfa_recovery(request_id: str, request: Request):
    with connect() as db:
        admin = session_user(db, request)
        if admin["role"] != "super_admin": raise HTTPException(403, "Super Admin approval required.")
        row = db.execute("SELECT * FROM mfa_recovery_requests WHERE id=%s AND status='pending' FOR UPDATE", (request_id,)).fetchone()
        if not row: raise HTTPException(404, "Pending recovery request not found.")
        db.execute("UPDATE users SET totp_enabled=false,totp_secret_enc=NULL WHERE id=%s", (row["target_user_id"],))
        db.execute("UPDATE mfa_recovery_requests SET status='approved',reviewed_at=now(),reviewed_by=%s WHERE id=%s", (admin["id"], request_id))
    return {"success": True}

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
"""Security API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/auth/profile', update_profile, methods=['PATCH'])
    router.add_api_route('/api/auth/login', login, methods=['POST'])
    router.add_api_route('/api/auth/me', me, methods=['GET'])
    router.add_api_route('/api/auth/sessions', list_auth_sessions, methods=['GET'])
    router.add_api_route('/api/auth/resend-mfa', resend_mfa, methods=['POST'])
    router.add_api_route('/api/auth/verify-mfa', verify_mfa, methods=['POST'])
    router.add_api_route('/api/auth/backup-codes', generate_backup_codes, methods=['POST'])
    router.add_api_route('/api/auth/recover-authenticator', recover_lost_authenticator, methods=['POST'])
    router.add_api_route('/api/auth/refresh', refresh_session, methods=['POST'])
    router.add_api_route('/api/auth/logout', logout, methods=['POST'])
    router.add_api_route('/api/auth/authorize', authorize_workspace, methods=['GET'])
    router.add_api_route('/api/auth/forgot-password', forgot_password, methods=['POST'])
    router.add_api_route('/api/auth/verify-reset-code', verify_reset_code, methods=['POST'])
    router.add_api_route('/api/auth/reset-status', reset_status, methods=['GET'])
    router.add_api_route('/api/auth/reset-password', reset_password, methods=['POST'])
    router.add_api_route('/api/auth/change-password', change_password, methods=['POST'])
    router.add_api_route('/api/auth/mfa-recovery-requests', request_mfa_recovery, methods=['POST'])
    router.add_api_route('/api/auth/mfa-recovery-requests/lost-authenticator', request_lost_authenticator_recovery, methods=['POST'])
    router.add_api_route('/api/admin/mfa-recovery-requests', list_mfa_recovery_requests, methods=['GET'])
    router.add_api_route('/api/admin/mfa-recovery-requests/{request_id}/approve', approve_mfa_recovery, methods=['POST'])
    router.add_api_route('/api/auth/activate', activate, methods=['POST'])
