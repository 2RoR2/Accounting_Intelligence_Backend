"""One-command demo company from an A1 company pack (plan v2, task A3).

    python services/admin/manage.py seed-demo-company --pack trading [--month 2026-08]

Local databases only. It creates the company and three users, then goes through the real API
in this process (FastAPI TestClient, so the in-process extraction queue runs as it does under
uvicorn): accounting periods, chart of accounts, contacts and opening balances through the A2
imports, every pack document through the normal upload, and every bank statement through the
existing bank import. It waits for extraction, then prints how many documents reached each
state and how many bank lines the existing reconciliation suggestions match correctly against
the pack's expected_matches.csv. Re-running it skips what is already there.

The seed signs its two working users in by writing a short-lived web session directly (no
password or authenticator step) and deletes those sessions when it finishes. Passwords are
printed once to the console and never written to a file.
"""
from __future__ import annotations

import calendar
import csv
import getpass
import ipaddress
import json
import os
import secrets
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PACKS = ("trading", "services", "fnb")
PACK_ROOT = ROOT / "ai" / "synthetic" / "output" / "packs"
WAITING_STATUSES = ("uploaded", "queued", "processing", "validating")
HEADERS = {"X-Requested-With": "AccountingIntelligence", "Origin": "http://localhost:3000"}


class SeedError(RuntimeError):
    """A step failed; the message says which one and why."""


# --- guards ----------------------------------------------------------------------------------------

def refusal_reason(environ=None):
    """Why this environment must not be seeded, or None when it is a local development database."""
    environ = os.environ if environ is None else environ
    if (environ.get("APP_ENV") or "").strip().lower() != "development":
        return "APP_ENV must be development."
    url = (environ.get("DATABASE_URL") or "").strip()
    if not url:
        return "DATABASE_URL is not set."
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return "DATABASE_URL is not a valid URL."
    if scheme not in {"postgresql", "postgres"}:
        return "DATABASE_URL must be a postgresql:// URL."
    reason = _remote_destination(url, environ)
    if reason:
        return reason.rstrip(".") + "; only a database on this machine (127.0.0.1 or localhost) can be seeded."
    if (environ.get("S3_BUCKET") or "").strip():
        return "S3_BUCKET is set; clear it so demo uploads stay in local storage and never reach a shared bucket."
    return None


def _is_loopback(value):
    value = value.strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _remote_destination(url, environ):
    """Why the address libpq will actually use is not this machine, or None.

    Checks the parsed connection parameters, not just the URL's host: query parameters can override
    `host`, add `hostaddr` (which libpq connects to instead of resolving `host`) or name a `service`
    from pg_service.conf, and libpq falls back to PGHOST / PGHOSTADDR / PGSERVICE when a value is absent.
    """
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict
    try:
        params = conninfo_to_dict(url)
    except ProgrammingError:
        return "DATABASE_URL is not a valid connection URL."
    if params.get("service") or (environ.get("PGSERVICE") or "").strip():
        return "DATABASE_URL (or PGSERVICE) names a connection service, which can point anywhere."
    hosts = params.get("host") or (environ.get("PGHOST") or "")
    if not hosts.strip():
        return "DATABASE_URL names no host."
    for host in hosts.split(","):
        if not _is_loopback(host):
            return f"DATABASE_URL points at {host.strip() or 'an empty host'}."
    hostaddrs = params.get("hostaddr") or (environ.get("PGHOSTADDR") or "")
    for address in filter(None, (part.strip() for part in hostaddrs.split(","))):
        if not _is_loopback(address):
            return f"DATABASE_URL (or PGHOSTADDR) connects to {address}."
    return None


# --- pack files ------------------------------------------------------------------------------------

def pack_folder(key, root=PACK_ROOT):
    folder = Path(root) / key
    if not (folder / "pack.json").is_file():
        print(f"Generating the {key} company pack (ai/synthetic/company_pack.py)...")
        from ai.synthetic.company_pack import generate_pack
        folder = Path(generate_pack(key, root))
    return folder


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def upload_kind(document):
    """(kind, category) for POST /api/documents from a documents.csv row."""
    kind = document["kind"] if document["kind"] in {"credit_note", "debit_note"} else document["manifest"]
    if kind not in {"invoice", "bill", "receipt", "credit_note", "debit_note"}:
        raise SeedError(f"Unknown document kind for {document['document_id']}: {document['kind']}")
    category = "accounts_receivable" if document["direction"] == "sales" else "accounts_payable"
    return kind, category


def bank_rows_as_json(path, layout):
    """Rows for the JSON bank import, for layouts the CSV parser cannot read yet (dr_cr; task C5)."""
    from ai.synthetic.company_pack import read_bank_file
    rows = []
    for row in read_bank_file(path, layout):
        amount = row["amount"]
        rows.append({"postedDate": row["posted_date"].isoformat(), "description": row["description"],
                     "reference": row["reference"] or None, "currency": "MYR",
                     "debit": str(-amount) if amount < 0 else None, "credit": str(amount) if amount > 0 else None})
    return rows


def bank_file_month(name):
    """'bank_2026-08.csv' -> '2026-08'."""
    return name[len("bank_"):-len(".csv")]


# --- measuring reconciliation ----------------------------------------------------------------------

def score_proposals(expected_rows, proposals):
    """Compare proposed matches with the gold file.

    expected_rows: expected_matches.csv rows; proposals: {(bank_file, source_row): set of pack ids}.
    A bank line is `correct` when every proposed id is in its gold ids, `complete` when the sets are
    equal, `wrong` when any proposed id is not in its gold ids (including lines with no document),
    `missed` when it has gold documents and nothing was proposed.
    """
    result = Counter()
    by_type = {}
    for row in expected_rows:
        key = (row["bank_file"], int(row["source_row"]))
        gold = {item for item in row["document_ids"].split(";") if item}
        proposed = proposals.get(key, set())
        result["bank_rows"] += 1
        if gold:
            result["rows_with_documents"] += 1
        bucket = by_type.setdefault(row["match_type"] or row["category"], Counter())
        bucket["rows"] += 1
        if not proposed:
            if gold:
                result["missed"] += 1
            continue
        if proposed <= gold:
            result["correct"] += 1
            bucket["correct"] += 1
            if proposed == gold:
                result["complete"] += 1
        else:
            result["wrong"] += 1
            bucket["wrong"] += 1
    keys = ("bank_rows", "rows_with_documents", "correct", "complete", "wrong", "missed")
    return {**{k: result[k] for k in keys}, "by_match_type": {k: dict(v) for k, v in sorted(by_type.items())}}


def matcher_baseline(folder, months=None):
    """Run the existing matcher (validation/matching/bank_reconciliation.py) as if every pack document
    and opening item were an approved record carrying its printed number and total. This separates the
    matcher from extraction and approval: the ceiling of today's exact reference + amount rule."""
    from ai.synthetic.company_pack import read_bank_file
    from validation.matching.bank_reconciliation import find_bank_match_candidates
    pack = json.loads((Path(folder) / "pack.json").read_text(encoding="utf-8"))
    months = months or pack["period"]["months"]
    records = []
    for doc in read_csv(Path(folder) / "documents.csv"):
        if doc["month"] in months:
            records.append({"record_id": doc["document_id"], "total_amount": doc["total"], "currency": "MYR",
                            "reference_number": doc["document_number"],
                            "category": "accounts_receivable" if doc["direction"] == "sales" else "accounts_payable"})
    for item in read_csv(Path(folder) / "opening_balances.csv"):
        if item["row_type"] in {"receivable", "payable"}:
            records.append({"record_id": item["item_id"], "total_amount": item["debit"] or item["credit"],
                            "currency": "MYR", "reference_number": item["document_number"],
                            "category": "accounts_receivable" if item["row_type"] == "receivable" else "accounts_payable"})
    transactions = []
    for month in months:
        name = f"bank_{month}.csv"
        for number, row in enumerate(read_bank_file(Path(folder) / name, pack["bank"]["layout"]), 1):
            transactions.append({"transaction_id": (name, number), "amount": abs(row["amount"]), "currency": "MYR",
                                 "reference": row["reference"] or None,
                                 "direction": "money_in" if row["amount"] > 0 else "money_out"})
    proposals = {}
    for record in records:
        match = find_bank_match_candidates(record, transactions)
        if match["status"] == "MATCHED":
            proposals.setdefault(match["matched_bank_transaction_id"], set()).add(record["record_id"])
    expected = [row for row in read_csv(Path(folder) / "expected_matches.csv")
                if bank_file_month(row["bank_file"]) in months]
    return score_proposals(expected, proposals)


# --- seeding -----------------------------------------------------------------------------------------

def generated_password():
    from security.authentication import strong_password
    while True:
        candidate = secrets.token_urlsafe(12) + "Aa1!"
        if strong_password(candidate):
            return candidate


def demo_users(key, company_name):
    return [("accountant", f"Demo Accountant ({company_name})", f"accountant@{key}.demo.test"),
            ("local_admin", f"Demo Local Admin ({company_name})", f"admin@{key}.demo.test"),
            ("super_admin", "Demo Super Admin", "superadmin@demo.test")]


def ensure_company(db, key, pack, prompt_password=False, reset_passwords=False):
    """Create (or find) the tenant and its three users. Returns (tenant, {role: user}, printable passwords)."""
    from security.authentication import hash_password, strong_password
    company = pack["company"]
    tenant = db.execute("SELECT * FROM tenants WHERE registration_number=%s", (company["ssm"],)).fetchone()
    if tenant and tenant["name"] != company["name"]:
        raise SeedError("The pack's SSM number is already used by another company in this database.")
    if not tenant:
        tenant = db.execute("""INSERT INTO tenants(name, registration_number, admin_email, seat_limit)
            VALUES (%s,%s,%s,10) RETURNING *""", (company["name"], company["ssm"], f"admin@{key}.demo.test")).fetchone()
    shared_password = None
    if prompt_password:
        shared_password = getpass.getpass("Password for the three demo users: ")
        if not strong_password(shared_password) or shared_password != getpass.getpass("Confirm password: "):
            raise SeedError("Passwords must match and have 8-256 characters with upper and lower case, a number and a symbol.")
    users, passwords = {}, []
    for role, name, email in demo_users(key, company["name"]):
        tenant_id = None if role == "super_admin" else tenant["id"]
        existing = db.execute("SELECT * FROM users WHERE lower(email)=%s", (email,)).fetchone()
        if existing and (existing["role"] != role or existing["tenant_id"] != tenant_id):
            raise SeedError(f"{email} already belongs to a different account.")
        if existing and not reset_passwords:
            users[role] = existing
            passwords.append((role, email, "(unchanged)"))
            continue
        password = shared_password or generated_password()
        if existing:
            users[role] = db.execute("""UPDATE users SET password_hash=%s, status='active', must_change_password=false
                WHERE id=%s RETURNING *""", (hash_password(password), existing["id"])).fetchone()
        else:
            users[role] = db.execute("""INSERT INTO users(tenant_id, name, email, password_hash, role, status)
                VALUES (%s,%s,%s,%s,%s,'active') RETURNING *""",
                (tenant_id, name, email, hash_password(password), role)).fetchone()
        passwords.append((role, email, "(the password you typed)" if shared_password else password))
    return tenant, users, passwords


class Api:
    """Signed-in calls to the real app, one cookie per role."""

    def __init__(self, client, tokens):
        self.client, self.tokens = client, tokens

    def call(self, role, method, path, expected=(200, 201), **kwargs):
        from main import ACCESS
        headers = {**HEADERS, "Cookie": f"{ACCESS}={self.tokens[role]}"}
        response = self.client.request(method, path, headers=headers, **kwargs)
        if response.status_code not in expected:
            raise SeedError(f"{method} {path} returned {response.status_code}: {response.text[:600]}")
        return response


def import_csv(api, path, file_path, label):
    """Preview, then confirm when there is something new. Returns a one-line result."""
    content = file_path.read_bytes()
    preview = api.call("local_admin", "POST", path, files={"file": (file_path.name, content, "text/csv")}).json()
    if not preview["canConfirm"]:
        already = preview["errorCount"] == 0 and (preview["newCount"] == 0 or any(
            "already posted" in error for error in preview["fileErrors"]))
        if already:
            return f"{label}: already imported ({preview['duplicateCount']} duplicate rows skipped)"
        problems = preview["fileErrors"] + [f"row {r['sourceRow']}: {'; '.join(r['errors'])}"
                                            for r in preview["rows"] if r["errors"]][:10]
        raise SeedError(f"{label} import has errors: " + " | ".join(problems))
    result = api.call("local_admin", "POST", path + "/confirm",
                      files={"file": (file_path.name, content, "text/csv")},
                      data={"previewToken": preview["previewToken"]}).json()
    return f"{label}: {result['imported']} imported, {len(result['skippedDuplicates'])} duplicates skipped"


def issue_tokens(users):
    from database.connection import connect
    from main import issue_session
    from security.authentication import create_access_token
    tokens, sessions = {}, []
    with connect() as db:
        for role in ("accountant", "local_admin"):
            session_id, _refresh, _expires = issue_session(db, users[role])
            sessions.append(session_id)
            tokens[role] = create_access_token(str(users[role]["id"]), session_id)
    return tokens, sessions


def wait_for_processing(tenant_id, timeout_seconds, poll=3.0):
    from database.connection import connect
    started, last = time.monotonic(), None
    while True:
        with connect() as db:
            waiting = db.execute("SELECT count(*) n FROM documents WHERE tenant_id=%s AND status = ANY(%s)",
                                 (tenant_id, list(WAITING_STATUSES))).fetchone()["n"]
            total = db.execute("SELECT count(*) n FROM documents WHERE tenant_id=%s", (tenant_id,)).fetchone()["n"]
        if waiting != last:
            print(f"  extraction: {total - waiting}/{total} documents done")
            last = waiting
        if not waiting:
            return time.monotonic() - started
        if time.monotonic() - started > timeout_seconds:
            raise SeedError(f"{waiting} documents were still waiting for extraction after {timeout_seconds // 60} minutes.")
        time.sleep(poll)


def document_states(db, tenant_id):
    """Counts the demo needs: document status, and the accountant's three piles.

    needs fixing = an open exception (or status exception); to review = a current validation finding
    that blocks approval or asks for review; ready to approve = extracted with neither. Failed and
    still-waiting documents are counted on their own.
    """
    statuses = {r["status"]: r["n"] for r in db.execute(
        "SELECT status, count(*) n FROM documents WHERE tenant_id=%s GROUP BY status ORDER BY status",
        (tenant_id,)).fetchall()}
    rows = db.execute("""SELECT d.id, d.status,
            EXISTS(SELECT 1 FROM exceptions e WHERE e.document_id=d.id AND e.tenant_id=d.tenant_id
                   AND e.status='open') AS has_exception,
            EXISTS(SELECT 1 FROM validation_results v WHERE v.document_id=d.id AND v.tenant_id=d.tenant_id
                   AND v.content_revision=d.content_revision AND (v.prevents_approval OR v.requires_review)) AS flagged
        FROM documents d WHERE d.tenant_id=%s""", (tenant_id,)).fetchall()
    piles = Counter()
    for row in rows:
        if row["status"] == "failed":
            piles["failed"] += 1
        elif row["status"] in WAITING_STATUSES:
            piles["still_processing"] += 1
        elif row["has_exception"] or row["status"] == "exception":
            piles["needs_fixing"] += 1
        elif row["flagged"]:
            piles["to_review"] += 1
        else:
            piles["ready_to_approve"] += 1
    exceptions = {r["type"]: r["n"] for r in db.execute("""SELECT type, count(*) n FROM exceptions
        WHERE tenant_id=%s AND status='open' GROUP BY type ORDER BY n DESC, type""", (tenant_id,)).fetchall()}
    return {"by_status": statuses, "piles": dict(sorted(piles.items())), "open_exceptions_by_type": exceptions}


def bank_import_files(db, tenant_id):
    """{import id: pack bank file} for every confirmed import of the company, this run's or an earlier one's.
    A pack bank file holds the rows posted in one month, so the month of an import's first row names its file."""
    rows = db.execute("""SELECT i.id, min(t.posted_date) AS first_posted
        FROM bank_imports i JOIN bank_transactions t ON t.import_id=i.id AND t.tenant_id=i.tenant_id
        WHERE i.tenant_id=%s AND i.status='confirmed' GROUP BY i.id""", (tenant_id,)).fetchall()
    return {str(row["id"]): f"bank_{row['first_posted']:%Y-%m}.csv" for row in rows if row["first_posted"]}


def database_proposals(db, tenant_id, import_files):
    """{(bank_file, source_row): pack ids} from the reconciliation candidates the bank import proposed."""
    rows = db.execute("""SELECT t.import_id, t.source_row, d.original_filename
        FROM reconciliation_candidates c
        JOIN bank_transactions t ON t.id=c.bank_transaction_id AND t.tenant_id=c.tenant_id
        JOIN standardised_records r ON r.id=c.record_id AND r.tenant_id=c.tenant_id
        JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
        WHERE c.tenant_id=%s""", (tenant_id,)).fetchall()
    proposals = {}
    for row in rows:
        name = import_files.get(str(row["import_id"]))
        if name:
            proposals.setdefault((name, row["source_row"]), set()).add(Path(row["original_filename"]).stem)
    return proposals


def _create_periods(api, months):
    existing = {p["periodCode"] for p in api.call("local_admin", "GET", "/api/ledger/periods").json()}
    for code in months:
        if code not in existing:
            year, number = (int(part) for part in code.split("-"))
            last = calendar.monthrange(year, number)[1]
            api.call("local_admin", "POST", "/api/ledger/periods",
                     json={"periodCode": code, "startsOn": f"{code}-01", "endsOn": f"{code}-{last:02d}"})
    return f"accounting periods: {', '.join(months)}"


def _wait_for_free_worker(tenant_id, workers, poll=0.5, timeout_seconds=600):
    """Upload no faster than the in-process workers take documents.

    A burst of uploads can make two dispatchers hand the inline pool more jobs than it has threads;
    the extra job is then marked failed (DISPATCH_UNCONFIRMED) instead of waiting. Pacing the seed
    avoids that; the queue itself is not changed here (follow-up reported with task A3).
    """
    from database.connection import connect
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        with connect() as db:
            busy = db.execute("SELECT count(*) n FROM documents WHERE tenant_id=%s AND status = ANY(%s)",
                              (tenant_id, list(WAITING_STATUSES))).fetchone()["n"]
        if busy < workers:
            return
        time.sleep(poll)
    raise SeedError("Extraction stopped taking new documents for 10 minutes; check the API log.")


def _upload_documents(api, folder, chosen, tenant_id):
    from services.queue import inline
    from services.queue.backend import queue_backend
    workers = inline.max_workers() if queue_backend() == "inline" else 10 ** 6
    uploaded = skipped = 0
    for doc in read_csv(folder / "documents.csv"):
        if doc["month"] not in chosen:
            continue
        _wait_for_free_worker(tenant_id, workers)
        kind, category = upload_kind(doc)
        file_path = folder / doc["file"]
        mime = "application/pdf" if file_path.suffix.lower() == ".pdf" else "image/jpeg"
        response = api.call("accountant", "POST", "/api/documents", expected=(201, 409),
                            params={"kind": kind, "category": category},
                            files={"file": (file_path.name, file_path.read_bytes(), mime)})
        uploaded += response.status_code == 201
        skipped += response.status_code == 409
    return f"documents: {uploaded} uploaded, {skipped} already there"


def _import_bank(api, folder, pack, chosen, steps):
    account_ref = f"Current account {pack['company']['bank_account_number'][-4:]}"
    parser = pack["bank"].get("parser")
    for code in chosen:
        name = f"bank_{code}.csv"
        if parser:
            response = api.call("accountant", "POST", "/api/bank/imports", expected=(201, 409),
                                files={"file": (name, (folder / name).read_bytes(), "text/csv")},
                                data={"source_bank_account_ref": account_ref, "mapping": json.dumps(parser["mapping"]),
                                      "date_format": parser["date_format"], "default_currency": "MYR"})
            if response.status_code == 409:
                steps.append(f"{name}: already imported")
                print("  " + steps[-1])
                continue
            import_id = response.json()["importId"]
            confirmed = api.call("accountant", "POST", f"/api/bank-imports/{import_id}/confirm").json()
        else:
            response = api.call("accountant", "POST", "/api/bank/imports", expected=(201, 409), json={
                "sourceBankAccountRef": account_ref, "mappingVersion": f"company-pack-{pack['bank']['layout']}",
                "rows": bank_rows_as_json(folder / name, pack["bank"]["layout"])})
            if response.status_code == 409:
                steps.append(f"{name}: already imported")
                print("  " + steps[-1])
                continue
            confirmed = response.json()
        steps.append(f"{name}: {confirmed['rowCount']} rows imported ({'CSV file' if parser else 'JSON rows'}), "
                     f"{confirmed['candidatesProposed']} matches proposed")
        print("  " + steps[-1])


def seed(key, month=None, pack_root=PACK_ROOT, prompt_password=False, reset_passwords=False, timeout_minutes=30):
    reason = refusal_reason()
    if reason:
        raise SeedError("Refusing to seed: " + reason)
    started = time.monotonic()
    from fastapi.testclient import TestClient
    import main
    from database.connection import connect
    folder = pack_folder(key, pack_root)
    pack = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
    months = pack["period"]["months"]
    if month and month not in months:
        raise SeedError(f"--month must be one of the pack's months: {', '.join(months)}.")
    chosen = [month] if month else months
    print(f"Seeding {pack['company']['name']} from {folder} (months {', '.join(chosen)}); "
          f"AI extraction: {os.getenv('AI_EXTRACTION') or 'off'}.")
    main.check_schema_compatibility()
    main.apply_compatibility_migrations()  # align document-code sequences before the first upload
    with connect() as db:
        tenant, users, passwords = ensure_company(db, key, pack, prompt_password, reset_passwords)
    # The users are committed now: show their passwords before any later step can fail and lose them.
    print_credentials(passwords)
    tokens, sessions = issue_tokens(users)
    report = {"pack": key, "company": pack["company"]["name"], "months": chosen, "steps": []}
    steps = report["steps"]
    try:
        with TestClient(main.app, base_url="http://127.0.0.1") as client:
            api = Api(client, tokens)
            steps.append(_create_periods(api, months))
            for path, name, label in (("/api/chart-of-accounts/import", "coa.csv", "chart of accounts"),
                                      ("/api/contacts/import", "contacts.csv", "contacts"),
                                      ("/api/opening-balances/import", "opening_balances.csv", "opening balances")):
                steps.append(import_csv(api, path, folder / name, label))
                print("  " + steps[-1])
            steps.append(_upload_documents(api, folder, chosen, tenant["id"]))
            print("  " + steps[-1])
            report["extraction_seconds"] = round(wait_for_processing(tenant["id"], timeout_minutes * 60), 1)
            _import_bank(api, folder, pack, chosen, steps)
            suggestions = api.call("accountant", "GET", "/api/bank/reconciliation/suggestions").json()["suggestions"]
    finally:
        with connect() as db:
            db.execute("DELETE FROM web_sessions WHERE session_id = ANY(%s)", (sessions,))
    expected = [row for row in read_csv(folder / "expected_matches.csv") if bank_file_month(row["bank_file"]) in chosen]
    with connect() as db:
        report["documents"] = document_states(db, tenant["id"])
        report["reconciliation_in_database"] = score_proposals(expected, database_proposals(db, tenant["id"], bank_import_files(db, tenant["id"])))
    report["reconciliation_in_database"]["suggestions_endpoint"] = len(suggestions)
    report["reconciliation_matcher_on_gold_records"] = matcher_baseline(folder, chosen)
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def print_credentials(passwords):
    print("Sign-in (shown once, not saved anywhere):")
    for role, email, password in passwords:
        print(f"  {role:12} {email:34} {password}")
    print("Admin and super admin sign-in asks for an authenticator app; accountants get an e-mailed code.", flush=True)


def print_report(report):
    print(f"\nDemo company ready in {report['seconds']} s:", report["company"])
    print("(The sign-in details were printed at the start of this run.)")
    docs = report["documents"]
    print("\nDocuments by status:", json.dumps(docs["by_status"]))
    print("Accountant piles:   ", json.dumps(docs["piles"]))
    print("Open exceptions:    ", json.dumps(docs["open_exceptions_by_type"]))
    for name, label in (("reconciliation_in_database", "Bank matches proposed in the database"),
                        ("reconciliation_matcher_on_gold_records", "Matcher on perfect records (ceiling)")):
        score = report[name]
        print(f"{label}: {score['correct']} correct ({score['complete']} complete), {score['wrong']} wrong, "
              f"{score['missed']} missed, of {score['rows_with_documents']} document lines / {score['bank_rows']} bank lines")


def add_arguments(parser):
    parser.add_argument("--pack", choices=PACKS, required=True)
    parser.add_argument("--month", help="only this month's documents and bank file (YYYY-MM)")
    parser.add_argument("--pack-root", default=str(PACK_ROOT), help="folder holding <pack>/pack.json")
    parser.add_argument("--prompt-password", action="store_true", help="type one password for the three users")
    parser.add_argument("--reset-passwords", action="store_true", help="give existing demo users new passwords")
    parser.add_argument("--timeout-minutes", type=int, default=30)
    parser.add_argument("--report", help="also write the measurements (never the passwords) to this JSON file")


def run(args):
    try:
        report = seed(args.pack, args.month, args.pack_root, args.prompt_password,
                      args.reset_passwords, args.timeout_minutes)
    except SeedError as error:
        print(str(error), file=sys.stderr)
        return 2
    print_report(report)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    return 0
