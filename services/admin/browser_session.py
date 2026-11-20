"""Browser-test sign-in for a synthetic demo user (local only).

    python services/admin/manage.py browser-session --out C:/path/outside/repo/cookies.json
        [--email accountant@trading.demo.test]

Writes a Playwright cookie file ([{name, value, path}]) holding a fresh web session for a synthetic
demo user, so browser specs (B4A_COOKIES, B4B_COOKIES) can run without anyone pasting cookies. The
access cookie lasts 15 minutes; the refresh cookie lasts the normal 8-hour session and the frontend
renews the access cookie with it, exactly as for a person who signed in.

This is a command-line tool, not an endpoint: it adds nothing to the API or to the login flow. It
refuses unless the environment passes seed-demo-company's local-only guard (APP_ENV=development,
every database address loopback, S3_BUCKET empty), and it only signs in addresses ending in
`.demo.test` (the accounts seed-demo-company creates), never a real person's account. Cookie values
are written to the file only, never printed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_EMAIL = "accountant@trading.demo.test"
SYNTHETIC_SUFFIX = ".demo.test"
ALLOWED_ROLES = ("accountant", "local_admin")


class BrowserSessionError(RuntimeError):
    pass


def check_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not email.endswith(SYNTHETIC_SUFFIX) or "@" not in email:
        raise BrowserSessionError(f"Only synthetic demo accounts (*{SYNTHETIC_SUFFIX}) can get a browser session.")
    return email


def cookie_file(access_token: str, refresh_token: str) -> list[dict]:
    from main import ACCESS, REFRESH
    return [{"name": ACCESS, "value": access_token, "path": "/api"},
            {"name": REFRESH, "value": refresh_token, "path": "/api/auth"}]


def create_session(db, email: str):
    from main import issue_session
    from security.authentication import create_access_token
    user = db.execute("""SELECT u.*, t.status AS tenant_status FROM users u
        LEFT JOIN tenants t ON t.id=u.tenant_id WHERE lower(u.email)=%s""", (email,)).fetchone()
    if not user:
        raise BrowserSessionError("No such demo user. Run: manage.py seed-demo-company --pack trading")
    if user["role"] not in ALLOWED_ROLES or user["status"] != "active" or user.get("tenant_status") != "active":
        raise BrowserSessionError("The demo user must be an active accountant or local admin of an active company.")
    session_id, refresh_token, _expires = issue_session(db, user)
    return create_access_token(str(user["id"]), session_id), refresh_token


def add_arguments(parser):
    parser.add_argument("--email", default=DEFAULT_EMAIL, help=f"synthetic demo user (default {DEFAULT_EMAIL})")
    parser.add_argument("--out", required=True, help="cookie JSON file to write; keep it outside the repository")


def run(args, environ=None, connect=None) -> int:
    from services.admin.demo_company import refusal_reason
    environ = os.environ if environ is None else environ
    reason = refusal_reason(environ)
    if reason:
        print(f"Refusing to create a browser session: {reason}", file=sys.stderr)
        return 2
    try:
        email = check_email(args.email)
        if connect is None:
            from database.connection import connect
        with connect() as db:
            access, refresh = create_session(db, email)
    except BrowserSessionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cookie_file(access, refresh)), encoding="utf-8")
    print(f"Wrote a browser session for the demo user to {out} (valid up to 8 hours).")
    return 0
