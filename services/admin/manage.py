"""Administrative setup. Credentials are read from backend .env, never from the browser."""
import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security.authentication import hash_password, strong_password, digest
from database.connection import connect

DEMO_TENANT_NAME = "ABC Sdn Bhd (Demo)"
DEMO_TENANT_REGISTRATION = "DEMO-ABC-001"
DEMO_USERS = (
    ("SAIC Administrator", "admin@saic.example", "super_admin"),
    ("Alice Tan", "alice@abc.example", "local_admin"),
    ("Rachel Chong", "rachel@abc.example", "accountant"),
)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("seed-demo-accounts")
    create = commands.add_parser("create-admin")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    account = commands.add_parser("create-account")
    account.add_argument("--email", required=True)
    account.add_argument("--name", required=True)
    account.add_argument("--tenant", required=True, help="Exact company name")
    account.add_argument("--role", choices=("local_admin", "accountant"), default="accountant")
    reset_mfa = commands.add_parser("reset-mfa")
    reset_mfa.add_argument("--email", required=True)
    commands.add_parser("requests")
    approve = commands.add_parser("approve-company")
    approve.add_argument("request_id")
    from services.admin import demo_company
    demo_company.add_arguments(commands.add_parser(
        "seed-demo-company", help="local only: a demo company from an A1 company pack (see docs/LOCALHOST.md)"))
    from services.admin import reread, test_users
    reread.add_arguments(commands.add_parser(
        "reread-documents", help="local only: read a company's stored documents again with the current settings"))
    test_users.add_arguments(commands.add_parser(
        "create-test-users", help="local only: team test accounts from UAT_TEST_EMAILS (see docs/LOCALHOST.md)"))
    from services.admin import browser_session, erp_demo
    erp_demo.add_arguments(commands.add_parser(
        "seed-erp-demo", help="local only: typed sales/purchases for a seeded demo company, all posted (Plan L9)"))
    browser_session.add_arguments(commands.add_parser(
        "browser-session", help="local only: Playwright cookie file for a synthetic *.demo.test user"))
    for command, help_text in (("seed-knt", "load the KNT demo books into a local database"),
                               ("reset-knt", "delete and rebuild the local KNT demo books"),
                               ("tally", "verify the local KNT books and print a reconciliation summary")):
        subparser = commands.add_parser(command, help=help_text)
        if command != "tally":
            subparser.add_argument("--out", type=Path, default=ROOT / "ai/synthetic/output/packs/knt",
                                   help="directory for the generated KNT pack")
            subparser.add_argument("--prompt-password", action="store_true", help="choose new demo users' password")
    args = parser.parse_args()
    if args.command in {"seed-knt", "reset-knt", "tally"}:
        from services.admin.knt_seed import locked_database, seed
        from services.admin.knt_tally import print_tally, tally
        from services.admin.knt_reset import reset

        try:
            with locked_database() as local_connect:
                if args.command != "tally":
                    password = None
                    if args.prompt_password:
                        password = getpass.getpass("Password for new KNT demo users: ")
                        if not strong_password(password) or password != getpass.getpass("Confirm password: "):
                            raise demo_company.SeedError("Passwords must match and meet local password requirements.")
                    (reset if args.command == "reset-knt" else seed)(local_connect, args.out, password)
                with local_connect() as db:
                    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    print_tally(tally(db))
        except demo_company.SeedError as error:
            parser.exit(2, f"{error}\n")
        return
    if args.command == "browser-session":
        sys.exit(browser_session.run(args))
    if args.command == "seed-demo-company":
        sys.exit(demo_company.run(args))
    if args.command == "seed-erp-demo":
        sys.exit(erp_demo.run(args))
    if args.command == "reread-documents":
        sys.exit(reread.run(args))
    if args.command == "create-test-users":
        sys.exit(test_users.run(args))
    demo_password = None
    if args.command == "seed-demo-accounts":
        if (os.getenv("APP_ENV", "").lower() != "development"
                or os.getenv("ENABLE_DEMO_ACCOUNTS", "").lower() != "true"):
            parser.error(
                "Demo seeding is disabled. Set APP_ENV=development and "
                "ENABLE_DEMO_ACCOUNTS=true in the local backend .env."
            )
        demo_password = getpass.getpass("Demo password for all three accounts: ")
        if (not strong_password(demo_password)
                or demo_password != getpass.getpass("Confirm demo password: ")):
            parser.error(
                "Passwords must match and include 8–256 characters, uppercase, "
                "lowercase, number and symbol."
            )
    with connect() as db:
        if args.command == "migrate":
            from database.schema_compatibility import apply_migrations
            root=ROOT
            applied=apply_migrations(db,root/"database/PostgreSQL Database schema.sql",root/"database/migrations")
        elif args.command == "create-admin":
            password = getpass.getpass("New administrator password: ")
            if not strong_password(password) or password != getpass.getpass("Confirm password: "):
                parser.error("Passwords must match and include 8–256 characters, uppercase, lowercase, number and symbol.")
            db.execute("""INSERT INTO users(name,email,password_hash,role,status)
                VALUES (%s,%s,%s,'super_admin','active')""",
                (args.name, args.email.strip().lower(), hash_password(password)))
            print("Administrator created.")
        elif args.command == "create-account":
            password = getpass.getpass("New account password: ")
            if not strong_password(password) or password != getpass.getpass("Confirm password: "):
                parser.error("Passwords must match and include 8–256 characters, uppercase, lowercase, number and symbol.")
            tenant = db.execute("SELECT id,status FROM tenants WHERE name=%s", (args.tenant,)).fetchone()
            if not tenant or tenant["status"] != "active":
                parser.error("An active company with that exact name was not found.")
            if db.execute("SELECT 1 FROM users WHERE lower(email)=lower(%s)", (args.email.strip(),)).fetchone():
                parser.error("That email already has an account.")
            db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status,must_change_password,invited_at)
                VALUES (%s,%s,%s,%s,%s,'active',%s,now())""",
                (tenant["id"], args.name.strip(), args.email.strip().lower(), hash_password(password), args.role, args.role == "accountant"))
            print("Account created securely.")
        elif args.command == "reset-mfa":
            email = args.email.strip().lower()
            user = db.execute("SELECT id,role FROM users WHERE lower(email)=%s AND status='active' FOR UPDATE",
                              (email,)).fetchone()
            if not user or user["role"] not in ("super_admin", "local_admin"):
                parser.error("An active privileged account with that email was not found.")
            db.execute("UPDATE users SET totp_secret_enc=NULL,totp_enabled=false WHERE id=%s", (user["id"],))
            db.execute("DELETE FROM web_sessions WHERE user_id=%s", (user["id"],))
            db.execute("DELETE FROM auth_challenges WHERE user_id=%s", (user["id"],))
            print("Authenticator reset. The user must enroll again at next sign-in.")
        elif args.command == "requests":
            for row in db.execute("SELECT id,company_name,contact_email FROM company_requests WHERE status='pending' ORDER BY submitted_at"):
                print(row["id"], row["company_name"], row["contact_email"])
        elif args.command == "approve-company":
            row = db.execute("SELECT * FROM company_requests WHERE id=%s AND status='pending' FOR UPDATE", (args.request_id,)).fetchone()
            if not row:
                parser.error("Pending request was not found.")
            tenant = db.execute("""INSERT INTO tenants(name,registration_number,admin_email,seat_limit)
                VALUES (%s,%s,%s,%s) RETURNING id""",
                (row["company_name"], row["registration_number"], row["contact_email"], row["requested_accounts"])).fetchone()
            user = db.execute("""INSERT INTO users(tenant_id,name,email,role,status,invited_at,invitation_expires_at)
                VALUES (%s,%s,%s,'local_admin','invited',now(),now()+interval '24 hours') RETURNING id""",
                (tenant["id"], row["contact_name"], row["contact_email"])).fetchone()
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO auth_tokens(user_id,purpose,token_hash,expires_at) VALUES (%s,'activation',%s,%s)",
                       (user["id"], digest(token), datetime.now(timezone.utc)+timedelta(hours=24)))
            db.execute("UPDATE company_requests SET status='approved',tenant_id=%s,reviewed_at=now() WHERE id=%s", (tenant["id"], row["id"]))
            print("Provide this private, single-use link to the company contact (expires in 24 hours):")
            print(os.getenv("FRONTEND_ORIGIN", "http://localhost:3000") + "/activate?token=" + token)
        elif args.command == "seed-demo-accounts":
            tenant = db.execute(
                "SELECT id,name FROM tenants WHERE registration_number=%s FOR UPDATE",
                (DEMO_TENANT_REGISTRATION,),
            ).fetchone()
            if tenant and tenant["name"] != DEMO_TENANT_NAME:
                parser.error("The demo tenant registration number is already used by another company.")
            if not tenant:
                tenant = db.execute(
                    """INSERT INTO tenants(name,registration_number,admin_email,seat_limit)
                    VALUES (%s,%s,%s,10) RETURNING id,name""",
                    (DEMO_TENANT_NAME, DEMO_TENANT_REGISTRATION, "alice@abc.example"),
                ).fetchone()
            for name, email, role in DEMO_USERS:
                tenant_id = tenant["id"] if role != "super_admin" else None
                existing = db.execute(
                    "SELECT id,role,tenant_id FROM users WHERE lower(email)=%s FOR UPDATE",
                    (email,),
                ).fetchone()
                if existing and (
                    existing["role"] != role or existing["tenant_id"] != tenant_id
                ):
                    parser.error(f"Demo email {email} is already assigned to a different account.")
                if existing:
                    db.execute(
                        """UPDATE users SET name=%s,password_hash=%s,status='active',
                        must_change_password=false WHERE id=%s""",
                        (name, hash_password(demo_password), existing["id"]),
                    )
                else:
                    db.execute(
                        """INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                        VALUES (%s,%s,%s,%s,%s,'active')""",
                        (tenant_id, name, email, hash_password(demo_password), role),
                    )
            print("Demo accounts are ready. Admin and Local Admin sign-in require TOTP enrollment.")
    # Report success only after the connection context committed the complete upgrade.
    if args.command == "migrate":
        for name in applied:
            print(f"Applied {name}.")
        print("Schema prerequisites verified.")


if __name__ == "__main__":
    main()
