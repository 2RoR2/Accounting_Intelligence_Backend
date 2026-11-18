"""Team test accounts in the local demo company (plan v3, task E1).

    python services/admin/manage.py create-test-users [--pack trading | --company "Exact name"]

Reads `UAT_TEST_EMAILS` from the environment (the backend .env; never the repository): a comma
separated list of `email` or `email:role`, role one of accountant (default), local_admin or
super_admin. At least one entry must be a local admin. Each address becomes an active user in the
demo company (super admins belong to no company). Running it again changes nothing unless the list
or `--reset-passwords` asks for it. Emails are printed masked (`f***@e***.com`), never in full.

Everyone signs in with email and password; accountants then get a one-time code by email (Brevo or
SMTP), local admins and super admins enter an authenticator (TOTP) code. New users get the password
from `UAT_TEST_PASSWORD`, or one typed at the prompt; existing users keep theirs unless
`--reset-passwords` is given. The password is never printed.

Local databases only: it applies seed-demo-company's guard (APP_ENV=development, every database
address on 127.0.0.1 or localhost, S3_BUCKET empty).
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = ROOT / "ai" / "synthetic" / "output" / "packs"
PACKS = ("trading", "services", "fnb")
ROLES = ("accountant", "local_admin", "super_admin")
EMAIL = re.compile(r"^[^@\s,:]+@[^@\s,:]+\.[^@\s,:.]+$")
PASSWORD_RULE = "8-256 characters with upper and lower case, a number and a symbol"


class TestUsersError(RuntimeError):
    """The command cannot continue; the message never contains a full email address."""
    __test__ = False  # not a pytest test class


@dataclass(frozen=True)
class TestUser:
    __test__ = False  # not a pytest test class
    email: str
    role: str = "accountant"


@dataclass(frozen=True)
class Change:
    action: str  # create, update, unchanged
    user: TestUser
    existing_id: object = None
    role_changed: bool = False
    # The account gets the test password: --reset-passwords, or an invited account that has none yet.
    set_password: bool = False
    # A local or super admin demoted to accountant: drop the authenticator so sign-in uses the emailed code.
    clear_totp: bool = False


def mask_email(email: str) -> str:
    """'first.last@example.com' -> 'f***@e***.com'. Keeps only first letters and the last label."""
    local, _, domain = (email or "").partition("@")
    host, dot, tld = domain.rpartition(".")
    if not host:
        host, dot, tld = domain, "", ""
    return f"{local[:1]}***@{host[:1]}***{dot}{tld}"


def parse_test_emails(raw: str | None) -> list[TestUser]:
    """Parse UAT_TEST_EMAILS. Errors name the entry by position, never by its text."""
    users, seen = [], set()
    for position, item in enumerate((raw or "").split(","), start=1):
        item = item.strip()
        if not item:
            continue
        email, _, role = item.partition(":")
        email, role = email.strip().lower(), (role.strip().lower() or "accountant")
        if not EMAIL.match(email):
            raise TestUsersError(f"UAT_TEST_EMAILS entry {position} is not a valid email address.")
        if role not in ROLES:
            raise TestUsersError(f"UAT_TEST_EMAILS entry {position} ({mask_email(email)}) has role '{role}'; "
                                 f"use one of {', '.join(ROLES)}.")
        if email in seen:
            raise TestUsersError(f"UAT_TEST_EMAILS lists {mask_email(email)} more than once.")
        seen.add(email)
        users.append(TestUser(email, role))
    if not users:
        raise TestUsersError("UAT_TEST_EMAILS is empty. Add it to the backend .env, for example "
                             "UAT_TEST_EMAILS=you@example.com:local_admin,teammate@example.com")
    if not any(user.role == "local_admin" for user in users):
        raise TestUsersError("UAT_TEST_EMAILS needs at least one local admin: add ':local_admin' after one email.")
    return users


def refusal_reason(environ=None) -> str | None:
    """Why this environment must not get test accounts, or None for a local development database."""
    """Same rule as seed-demo-company (one guard for both local-only commands): APP_ENV=development and
    every address libpq would use (URL host, ?host=, ?hostaddr=, PGHOST, PGHOSTADDR) is loopback, no
    connection service, and S3_BUCKET empty."""
    from services.admin.demo_company import refusal_reason as local_only_reason
    return local_only_reason(os.environ if environ is None else environ)


def display_name(email: str) -> str:
    """'first.last+uat@x.com' -> 'First Last'."""
    words = [word for word in re.split(r"[._\-]+", email.partition("@")[0].partition("+")[0]) if word]
    return " ".join(word.capitalize() for word in words) or "Test User"


def pack_company(pack: str, root=PACK_ROOT) -> dict:
    """The demo company's name and SSM number from the generated pack (written by seed-demo-company)."""
    path = Path(root) / pack / "pack.json"
    if not path.is_file():
        raise TestUsersError(f"No {pack} company pack yet. Run: manage.py seed-demo-company --pack {pack}")
    company = json.loads(path.read_text(encoding="utf-8"))["company"]
    return {"name": company["name"], "ssm": company["ssm"]}


def find_tenant(db, *, pack: str | None = None, company: str | None = None, pack_root=PACK_ROOT):
    if company:
        tenant = db.execute("SELECT * FROM tenants WHERE name=%s FOR UPDATE", (company,)).fetchone()
        hint = "Check the exact company name."
    else:
        details = pack_company(pack or "trading", pack_root)
        tenant = db.execute("SELECT * FROM tenants WHERE registration_number=%s AND name=%s FOR UPDATE",
                            (details["ssm"], details["name"])).fetchone()
        hint = f"Run: manage.py seed-demo-company --pack {pack or 'trading'}"
    if not tenant:
        raise TestUsersError(f"The demo company was not found in this database. {hint}")
    if tenant["status"] != "active":
        raise TestUsersError("The demo company is not active.")
    return tenant


def plan_changes(existing: dict, tenant_id, wanted: list[TestUser], reset_passwords=False) -> list[Change]:
    """What to do for each wanted user, given existing users by lower-case email.

    Refuses (before anything is written) a user who already belongs to another company, or a
    super admin asked to become a company user and the reverse: the env list never moves accounts.
    """
    changes = []
    for user in wanted:
        target_tenant = None if user.role == "super_admin" else tenant_id
        row = existing.get(user.email)
        if row is None:
            changes.append(Change("create", user))
            continue
        if str(row["tenant_id"] or "") != str(target_tenant or ""):
            raise TestUsersError(f"{mask_email(user.email)} already has an account outside the demo company "
                                 "(or a different kind of account); remove it from UAT_TEST_EMAILS or use another address.")
        role_changed = row["role"] != user.role
        # An invited account (invite_user, approve-company) has no password yet: activating it without
        # one would leave a user who can never sign in.
        set_password = reset_passwords or not row.get("password_hash")
        clear_totp = (role_changed and user.role == "accountant"
                      and bool(row.get("totp_enabled") or row.get("totp_secret_enc")))
        needs_update = (role_changed or row["status"] != "active" or row["must_change_password"]
                        or set_password or clear_totp)
        changes.append(Change("update" if needs_update else "unchanged", user, row["id"], role_changed,
                              set_password, clear_totp))
    return changes


def read_password(environ, prompt=getpass.getpass) -> str:
    from security.authentication import strong_password
    password = environ.get("UAT_TEST_PASSWORD") or ""
    if password:
        if not strong_password(password):
            raise TestUsersError(f"UAT_TEST_PASSWORD must have {PASSWORD_RULE}.")
        return password
    password = prompt("Password for new test users: ")
    if not strong_password(password) or password != prompt("Confirm password: "):
        raise TestUsersError(f"Passwords must match and have {PASSWORD_RULE}.")
    return password


def apply_changes(db, tenant, changes: list[Change], password: str | None) -> int | None:
    """Write the planned changes. Returns the new seat limit if it had to be raised, else None."""
    from security.authentication import hash_password
    password_hash = hash_password(password) if password else None
    for change in changes:
        user = change.user
        tenant_id = None if user.role == "super_admin" else tenant["id"]
        if change.action == "create":
            db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status,must_change_password)
                VALUES (%s,%s,%s,%s,%s,'active',false)""",
                       (tenant_id, display_name(user.email), user.email, password_hash, user.role))
        elif change.action == "update":
            db.execute("""UPDATE users SET role=%s,status='active',must_change_password=false,
                password_hash=CASE WHEN %s THEN %s ELSE password_hash END,
                totp_secret_enc=CASE WHEN %s THEN NULL ELSE totp_secret_enc END,
                totp_enabled=CASE WHEN %s THEN false ELSE totp_enabled END WHERE id=%s""",
                       (user.role, change.set_password, password_hash, change.clear_totp, change.clear_totp,
                        change.existing_id))
            if change.clear_totp:
                # verify_mfa accepts a stored authenticator code even on an email challenge, so a demoted
                # admin must lose the enrolment; backup codes belong to it.
                db.execute("DELETE FROM mfa_backup_codes WHERE user_id=%s", (change.existing_id,))
            if change.role_changed or change.set_password:
                # Sign out existing sessions so the new role or password applies at the next sign-in.
                db.execute("DELETE FROM web_sessions WHERE user_id=%s", (change.existing_id,))
                db.execute("DELETE FROM auth_challenges WHERE user_id=%s", (change.existing_id,))
    seats = db.execute("SELECT count(*) AS n FROM users WHERE tenant_id=%s AND status<>'disabled'",
                       (tenant["id"],)).fetchone()["n"]
    if seats > tenant["seat_limit"]:
        db.execute("UPDATE tenants SET seat_limit=%s WHERE id=%s", (seats, tenant["id"]))
        return seats
    return None


def share_documents(db, tenant_id, wanted: list[TestUser]) -> int:
    """Assign the company's unassigned documents to the test accountants, in turn (oldest first).

    An accountant sees only documents they own or are assigned, and the seed's documents belong to
    the demo accountant, so without this a teammate's Today and Documents pages would be empty.
    Documents someone already holds are left alone, so running it again assigns only new ones.

    Each assignment is recorded the way the local admin's assign endpoint records it (a
    `workload_assignments` row, assigned by a test local admin); it also sets `documents.assigned_to`,
    which is what the visibility rule reads. Each accountant gets one notification, and the run
    leaves one audit entry (counts only, no addresses)."""
    emails = [user.email for user in wanted if user.role == "accountant"]
    if not emails:
        return 0
    ids = [row["id"] for row in db.execute(
        "SELECT id FROM users WHERE tenant_id=%s AND role='accountant' AND status='active' "
        "AND lower(email) = ANY(%s) ORDER BY lower(email)", (tenant_id, emails)).fetchall()]
    admin = db.execute(
        "SELECT id FROM users WHERE tenant_id=%s AND role='local_admin' AND status='active' "
        "ORDER BY (lower(email) = ANY(%s)) DESC, created_at LIMIT 1",
        (tenant_id, [user.email for user in wanted if user.role == "local_admin"])).fetchone()
    documents = [row["id"] for row in db.execute(
        "SELECT id FROM documents WHERE tenant_id=%s AND assigned_to IS NULL ORDER BY uploaded_at, id FOR UPDATE",
        (tenant_id,)).fetchall()]
    if not ids or not documents:
        return 0
    given = {user_id: 0 for user_id in ids}
    for index, document_id in enumerate(documents):
        accountant = ids[index % len(ids)]
        given[accountant] += 1
        db.execute("UPDATE documents SET assigned_to=%s WHERE id=%s AND tenant_id=%s", (accountant, document_id, tenant_id))
        if admin:
            db.execute("""INSERT INTO workload_assignments(tenant_id,document_id,assigned_to,assigned_by)
                VALUES (%s,%s,%s,%s) ON CONFLICT (document_id) DO UPDATE SET assigned_to=EXCLUDED.assigned_to,
                assigned_by=EXCLUDED.assigned_by,completed_at=NULL""", (tenant_id, document_id, accountant, admin["id"]))
    for accountant, count in given.items():
        if count:
            db.execute("""INSERT INTO notifications(tenant_id,user_id,type,title,message,resource)
                VALUES (%s,%s,'document_workflow',%s,%s,'documents')""",
                       (tenant_id, accountant, "Documents assigned to you",
                        f"{count} demo company document{'s' if count != 1 else ''} assigned to you for review."))
    db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
        VALUES (%s,%s,'create-test-users','system','DOCUMENTS_ASSIGNED','documents',%s)""",
               (tenant_id, admin["id"] if admin else None,
                json.dumps({"documents": len(documents), "accountants": len(ids)})))
    return len(documents)


def create_test_users(db, environ, *, pack=None, company=None, reset_passwords=False,
                      prompt=getpass.getpass, pack_root=PACK_ROOT, assign_documents=True):
    """Create or update the UAT_TEST_EMAILS users in one transaction.
    Returns (tenant, changes, seat_limit, documents assigned)."""
    wanted = parse_test_emails(environ.get("UAT_TEST_EMAILS"))
    tenant = find_tenant(db, pack=pack, company=company, pack_root=pack_root)
    rows = db.execute("SELECT id,lower(email) AS email,tenant_id,role,status,must_change_password,password_hash,"
                      "totp_enabled,totp_secret_enc FROM users WHERE lower(email) = ANY(%s) FOR UPDATE",
                      ([user.email for user in wanted],)).fetchall()
    changes = plan_changes({row["email"]: row for row in rows}, tenant["id"], wanted, reset_passwords)
    password = None
    if any(change.action == "create" or change.set_password for change in changes):
        password = read_password(environ, prompt)
    seat_limit = apply_changes(db, tenant, changes, password)
    assigned = share_documents(db, tenant["id"], wanted) if assign_documents else 0
    return tenant, changes, seat_limit, assigned


def report_lines(tenant, changes, seat_limit, assigned=0) -> list[str]:
    def note(change):
        notes = [text for flag, text in ((change.action == "update" and change.set_password, "password set"),
                                         (change.clear_totp, "authenticator cleared")) if flag]
        return f" ({', '.join(notes)})" if notes else ""
    lines = [f"{change.action:<9} {change.user.role:<12} {mask_email(change.user.email)}{note(change)}"
             for change in changes]
    if seat_limit:
        lines.append(f"Seat limit of {tenant['name']} raised to {seat_limit}.")
    if assigned:
        lines.append(f"{assigned} unassigned documents shared among the test accountants.")
    lines.append(f"{len(changes)} test users ready (company: {tenant['name']})")
    lines.append("Sign in at the frontend with email and password. Accountants then get a code by email; "
                 "local admins and super admins scan the QR code with an authenticator app at first sign-in.")
    return lines


def add_arguments(parser):
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--pack", choices=PACKS, default=None,
                       help="demo company created by seed-demo-company (default trading)")
    where.add_argument("--company", help="exact company name instead of a demo pack")
    parser.add_argument("--reset-passwords", action="store_true",
                        help="also give existing test users the new password (signs them out)")
    parser.add_argument("--pack-root", default=str(PACK_ROOT), help="folder holding <pack>/pack.json")
    parser.add_argument("--no-assign", action="store_true",
                        help="leave unassigned documents alone (by default the test accountants share them, "
                             "so their Today and Documents pages have work)")


def run(args, environ=None, connect=None, prompt=getpass.getpass, out=print) -> int:
    environ = os.environ if environ is None else environ
    reason = refusal_reason(environ)
    if reason:
        print(f"Refusing to create test users: {reason}", file=sys.stderr)
        return 2
    if connect is None:
        from database.connection import connect
    try:
        with connect() as db:
            tenant, changes, seat_limit, assigned = create_test_users(
                db, environ, pack=args.pack, company=args.company,
                reset_passwords=args.reset_passwords, prompt=prompt,
                pack_root=getattr(args, "pack_root", None) or PACK_ROOT,
                assign_documents=not getattr(args, "no_assign", False))
    except TestUsersError as error:
        print(str(error), file=sys.stderr)
        return 2
    for line in report_lines(tenant, changes, seat_limit, assigned):
        out(line)
    return 0
