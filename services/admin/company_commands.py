"""manage.py commands for the team demo companies (see docs/TEAM_DEMO_DATA_PLAN.md and docs/COMPANY_PACKS.md).

Every command that writes is a dry run unless --apply is given (seed-company and reset-company are local-only and
write directly, like seed-knt). A database that is not on this machine also needs --allow-remote and
COMPANY_PACK_REMOTE_HOST set to its host name.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

from ai.synthetic.sme_books import COMPANY_CODES, books_for
from services.admin.demo_company import SeedError

ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = ROOT / "ai" / "synthetic" / "output" / "packs"
COMMANDS = {"build-pack", "seed-company", "reset-company", "apply-company", "team-accounts", "team-inbox",
            "team-upload-pack"}
NEW_COMPANIES = tuple(code for code in COMPANY_CODES if code != "KNT")


def add_arguments(commands):
    build = commands.add_parser("build-pack", help="write a demo company's files (Inbox sources, chart, statements)")
    build.add_argument("--company", required=True, choices=COMPANY_CODES)
    build.add_argument("--out", type=Path)
    for name, text in (("seed-company", "local: create TKK, EHB or CDA with its demo books"),
                       ("reset-company", "local: wipe TKK, EHB or CDA and rebuild its demo books")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--company", required=True, choices=NEW_COMPANIES)
        command.add_argument("--out", type=Path)
        command.add_argument("--prompt-password", action="store_true", help="choose the fixture logins' password")
    apply = commands.add_parser("apply-company", help="create TKK, EHB or CDA in an existing database (dry run by default)")
    apply.add_argument("--company", required=True, choices=NEW_COMPANIES)
    apply.add_argument("--out", type=Path)
    apply.add_argument("--apply", action="store_true")
    apply.add_argument("--allow-remote", action="store_true")
    accounts = commands.add_parser("team-accounts", help="create the roster's accounts with no password (dry run by default)")
    accounts.add_argument("--roster", type=Path, default=os.getenv("TEAM_ROSTER_FILE"),
                          help="private roster JSON outside the repository (or TEAM_ROSTER_FILE)")
    accounts.add_argument("--apply", action="store_true")
    accounts.add_argument("--allow-remote", action="store_true")
    accounts.add_argument("--password-env", help="local only: give new users the password in this environment variable")
    inbox = commands.add_parser("team-inbox", help="give each roster accountant their Inbox documents (dry run by default)")
    inbox.add_argument("--company", required=True, choices=COMPANY_CODES)
    inbox.add_argument("--roster", type=Path, default=os.getenv("TEAM_ROSTER_FILE"))
    inbox.add_argument("--out", type=Path)
    inbox.add_argument("--apply", action="store_true")
    inbox.add_argument("--allow-remote", action="store_true")
    upload = commands.add_parser("team-upload-pack", help="fresh files with expected values for live upload tests")
    upload.add_argument("--roster", type=Path, default=os.getenv("TEAM_ROSTER_FILE"))
    upload.add_argument("--out", type=Path, default=PACK_ROOT / "team-upload")


def _print(lines):
    for line in lines:
        print(line, flush=True)


def _warn_storage(local):
    from database.object_storage import object_store

    if not local and not object_store.enabled():
        print("  Warning: S3_BUCKET is not set in this shell, so Inbox files are written to this machine only and the "
              "hosted app will not show their previews.", flush=True)


def _tally_one(db, books):
    from services.admin.company_tally import print_tally, tally

    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    print_tally(tally(db, books), f"{books.code} {books.company['name']}")


def run(args, environ=None):
    environ = os.environ if environ is None else environ
    try:
        return _run(args, environ)
    except SeedError as error:
        print(str(error), flush=True)
        return 2


def _run(args, environ):
    from services.admin import company_seed as seed_module

    command = args.command
    if command == "build-pack":
        books = books_for(args.company)
        pack = books.generate(args.out or PACK_ROOT / books.code.lower())
        print(f"{books.code}: wrote {books.company['name']} to {args.out or PACK_ROOT / books.code.lower()}"
              f" ({len(pack['bank'])} statements).")
        return 0
    if command == "team-upload-pack":
        from services.admin.team_upload import write_packs
        from services.admin.team_setup import read_roster

        if not args.roster:
            raise SeedError("Give --roster (or TEAM_ROSTER_FILE).")
        written = write_packs(read_roster(args.roster), args.out)
        print(f"Wrote {written} upload packs under {args.out} (files plus expected.json and README.md each).")
        return 0
    if command == "tally":
        connect = seed_module.require_target(args.allow_remote, environ, need_local_storage=False)
        codes = COMPANY_CODES if args.all else (args.company.upper() if args.company else "KNT",)
        from services.admin.company_tally import find_company

        with connect() as db:
            present = [code for code in codes if find_company(db, books_for(code))]
        if args.company and not present:
            raise SeedError(f"{codes[0]} is not in this database.")
        for code in present:
            with connect() as db:
                _tally_one(db, books_for(code))
        return 0
    if command in {"seed-company", "reset-company"}:
        connect = seed_module.require_target(False, environ)
        books = books_for(args.company)
        password = None
        if args.prompt_password:
            from security.authentication import strong_password

            password = getpass.getpass(f"Password for new {books.code} fixture logins: ")
            if not strong_password(password) or password != getpass.getpass("Confirm password: "):
                raise SeedError("Passwords must match and meet local password requirements.")
        with seed_module.company_lock(connect, books):
            (seed_module.reset if command == "reset-company" else seed_module.seed)(connect, books, args.out, password)
        with connect() as db:
            _tally_one(db, books)
        return 0
    if command == "apply-company":
        connect = seed_module.require_target(args.allow_remote, environ)
        local = seed_module.is_local(environ)
        books = books_for(args.company)
        with connect() as db:
            found = seed_module.plan(db, books)
        print(f"{'Applying' if args.apply else 'Dry run (nothing written)'}: {books.code} on "
              f"{'this machine' if local else seed_module.database_host(environ)}")
        _warn_storage(local)
        _print(f"  {key}: {value}" for key, value in found.items())
        if not args.apply:
            print("Run again with --apply to create it.")
            return 0
        with seed_module.company_lock(connect, books):
            seed_module.seed(connect, books, args.out, local=local)
        with connect() as db:
            _tally_one(db, books)
        return 0
    from services.admin import team_setup

    if not args.roster:
        raise SeedError("Give --roster (or TEAM_ROSTER_FILE): the private roster JSON outside the repository.")
    people = team_setup.read_roster(args.roster)
    if command == "team-accounts":
        connect = seed_module.require_target(args.allow_remote, environ, need_local_storage=False)
        local = seed_module.is_local(environ)
        password = team_setup.local_password(environ, args.password_env, local)
        with connect() as db:
            rows = team_setup.plan_accounts(db, people)
            print(f"{'Applying' if args.apply else 'Dry run (nothing written)'}: {len(rows)} roster accounts")
            _print(team_setup.account_lines(rows))
            if args.apply:
                created = team_setup.apply_accounts(db, rows, password)
                print(f"Created {created} accounts. Each person now uses Forgot password on the sign-in page"
                      + (" (local: the password from " + args.password_env + ")." if password else "."))
        return 0
    if command == "team-inbox":
        connect = seed_module.require_target(args.allow_remote, environ)
        books = books_for(args.company)
        with connect() as db:
            _tenant, work = team_setup.plan_inbox(db, books, people)
        print(f"{'Applying' if args.apply else 'Dry run (nothing written)'}: {books.code} team Inbox")
        _warn_storage(seed_module.is_local(environ))
        _print(team_setup.inbox_lines(work))
        if not args.apply:
            return 0
        folder = args.out or PACK_ROOT / books.code.lower() / "team_inbox"
        with seed_module.company_lock(connect, books):
            made = team_setup.apply_inbox(connect, books, people, folder)
        print(f"Created and assigned {made} documents.")
        with connect() as db:
            _tally_one(db, books)
        return 0
    raise SeedError(f"Unknown command {command}.")
