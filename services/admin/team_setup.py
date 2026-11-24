"""Team accounts and per-accountant Inbox work for the demo companies (docs/TEAM_DEMO_DATA_PLAN.md).

    python services/admin/manage.py team-accounts --roster PRIVATE.json            # dry run
    python services/admin/manage.py team-accounts --roster PRIVATE.json --apply
    python services/admin/manage.py team-inbox --company TKK --roster PRIVATE.json [--apply]

The roster is a private JSON file kept OUTSIDE the repository (real addresses never enter git):

    {"people": [{"slot": "TKK.admin.1", "name": "Rachel", "email": "name+tkk@example.com"},
                {"slot": "TKK.accountant.2", "name": "Jonathan", "email": "...", "inbox": "full"},
                {"slot": "KNT.accountant.6", "name": "SAIC accountant", "email": "...", "inbox": "starter"}]}

A slot is COMPANY.ROLE.N: ROLE is `admin` (local admin) or `accountant`, N a number that never changes for that
person (it keeps their document numbers apart). `inbox` (accountants only): full (12 documents, the default),
starter (6) or none. Emails are printed masked.

team-accounts creates missing users as active with NO password: each person sets their own with "Forgot password"
(email code), and a local admin enrols an authenticator at first sign-in. It never changes an existing user; an email
that already belongs to another company or role stops the run before anything is written. The seat limit is
checked first. Locally only, --password-env NAME gives the new users the password held in that environment variable
(for the local proof; never used on a hosted database).

team-inbox gives each roster accountant their own received documents, stored as already-read records (no AI call):
three to review, three ready, two waiting for approval, three needing a fix (unknown supplier, duplicate number,
missing number) and one approved and posted, including a supplier credit note and debit note. Documents are made
by the company's two fixture logins through the app (submit, approve, post) and handed to the accountant with the
app's own assignment (workload row, audit entry, notification). Running it again creates only what is missing.
"""
from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ai.synthetic.knt_pack import TITLES, Document, money
from ai.synthetic.sme_books import books_for
from services.admin.demo_company import Api, SeedError, issue_tokens
from services.admin.test_users import mask_email

SLOT = re.compile(r"^(KNT|TKK|EHB|CDA)\.(admin|accountant)\.([1-9][0-9]?)$")
EMAIL = re.compile(r"^[^@\s,:]+@[^@\s,:]+\.[^@\s,:.]+$")
ROLES = {"admin": "local_admin", "accountant": "accountant"}
TEAM_MODE = "team_inbox_fixture"
UNKNOWN_SUPPLIER = ("Syarikat Contoh Baharu Enterprise", "No. 99, Jalan Baharu Contoh")


@dataclass(frozen=True)
class Person:
    slot: str
    company: str
    role: str
    number: int
    name: str
    email: str
    inbox: str


def read_roster(path) -> list[Person]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SeedError(f"The roster file could not be read: {type(error).__name__}.") from None
    people, slots, emails = [], set(), set()
    for index, entry in enumerate(raw.get("people", []), 1):
        slot = str(entry.get("slot", "")).strip()
        match = SLOT.match(slot)
        email = str(entry.get("email", "")).strip().lower()
        name = str(entry.get("name", "")).strip()
        inbox = str(entry.get("inbox", "full" if match and match[2] == "accountant" else "none")).strip().lower()
        if not match:
            raise SeedError(f"Roster entry {index}: slot must look like TKK.accountant.1.")
        if not EMAIL.match(email):
            raise SeedError(f"Roster entry {index} ({slot}): the email is not valid.")
        if not name or len(name) > 120:
            raise SeedError(f"Roster entry {index} ({slot}): a display name is required.")
        if inbox not in {"full", "starter", "none"} or (match[2] == "admin" and inbox != "none"):
            raise SeedError(f"Roster entry {index} ({slot}): inbox must be full, starter or none (admins: none).")
        if slot in slots or email in emails:
            raise SeedError(f"Roster entry {index} ({slot}): slot or email repeated.")
        slots.add(slot)
        emails.add(email)
        people.append(Person(slot, match[1], ROLES[match[2]], int(match[3]), name, email, inbox))
    if not people:
        raise SeedError("The roster lists nobody.")
    return people


# --------------------------------------------------------------------------- accounts
def find_tenant(db, code):
    from services.admin.company_tally import find_company

    return find_company(db, books_for(code))


def plan_accounts(db, people):
    """[(person, tenant, 'create'|'exists')]. Raises before any write when something conflicts."""
    rows, wanted = [], {}
    for person in people:
        tenant = find_tenant(db, person.company)
        if not tenant:
            raise SeedError(f"{person.company} does not exist in this database yet; seed or apply it first.")
        user = db.execute("SELECT id,tenant_id,role,status FROM users WHERE lower(email)=%s", (person.email,)).fetchone()
        if user and (user["tenant_id"] != tenant["id"] or user["role"] != person.role):
            raise SeedError(f"{person.slot}: {mask_email(person.email)} already has an account in another company or "
                            "role. Use a different address (for example a +company address) for this slot.")
        rows.append((person, tenant, "exists" if user else "create"))
        if not user:
            wanted[tenant["id"]] = wanted.get(tenant["id"], 0) + 1
    for tenant_id, count in wanted.items():
        tenant = db.execute("SELECT name,seat_limit FROM tenants WHERE id=%s", (tenant_id,)).fetchone()
        used = db.execute("SELECT count(*) AS n FROM users WHERE tenant_id=%s AND status IN ('active','invited')",
                          (tenant_id,)).fetchone()["n"]
        if used + count > tenant["seat_limit"]:
            raise SeedError(f"{tenant['name']}: {used} seats used of {tenant['seat_limit']}; {count} more do not fit.")
    return rows


def apply_accounts(db, rows, password=None):
    from security.authentication import hash_password

    created = {}
    for person, tenant, action in rows:
        if action != "create":
            continue
        db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status,invited_at)
            VALUES (%s,%s,%s,%s,%s,'active',now())""",
                   (tenant["id"], person.name, person.email, hash_password(password) if password else None,
                    person.role))
        created[tenant["id"]] = created.get(tenant["id"], 0) + 1
    for tenant_id, count in created.items():
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,NULL,'team-accounts','system','TEAM_ACCOUNTS_CREATED','users',%s)""",
                   (tenant_id, json.dumps({"created": count})))
    return sum(created.values())


def account_lines(rows):
    lines = []
    for person, tenant, action in rows:
        role = "local admin" if person.role == "local_admin" else "accountant"
        what = "create, no password (Forgot password sets one)" if action == "create" else "exists, unchanged"
        lines.append(f"  {person.slot:<18} {mask_email(person.email):<22} {role:<11} {what}")
    return lines


# --------------------------------------------------------------------------- the per-accountant document set
@dataclass(frozen=True)
class TeamDoc:
    n: int
    kind: str  # invoice | bill | receipt | credit_note | debit_note
    lane: str  # to_review | ready | in_review | exception | posted
    source: str  # trade | utility | receipt | unknown
    description: str
    net: Decimal
    related: int | None = None  # the n of the invoice a note refers to
    duplicate_of: int | None = None
    missing_number: bool = False
    review: tuple[str, str] | None = None
    day: int = 10
    fixed: bool = False  # net is an amount in ringgit, not a multiple of the supplier's usual amount


FULL_SET = (
    # net: a multiple of the supplier's usual (median) amount, so the app's unusual-amount check stays quiet except
    # where a finding is intended; notes carry a fixed small amount; the unknown supplier has no history.
    TeamDoc(12, "invoice", "posted", "trade", "Stock received and checked", Decimal("1.00"), day=10),
    TeamDoc(1, "invoice", "to_review", "trade", "Stock awaiting delivery confirmation", Decimal("0.95"), day=17,
            review=("PACK_DELIVERY_CONFIRMATION", "Confirm the goods were received before submitting this invoice.")),
    TeamDoc(2, "bill", "to_review", "utility", "Electricity, meter reading to confirm", Decimal("1.02"), day=12,
            review=("PACK_METER_CHECK", "The meter reading is higher than usual; confirm it before submitting.")),
    TeamDoc(3, "debit_note", "to_review", "trade", "Extra delivery charge", Decimal("45.00"), related=12, day=14,
            review=("PACK_NOTE_CHECK", "Match this note to the original invoice before submitting."), fixed=True),
    TeamDoc(4, "invoice", "ready", "trade", "Store-room supplies", Decimal("1.05"), day=15),
    TeamDoc(5, "bill", "ready", "utility", "Electricity, branch counter", Decimal("0.97"), day=13),
    TeamDoc(6, "credit_note", "ready", "trade", "Items returned, wrong size", Decimal("60.00"), related=12, day=16,
            fixed=True),
    TeamDoc(7, "invoice", "in_review", "trade", "Monthly restock", Decimal("1.08"), day=11),
    TeamDoc(8, "receipt", "in_review", "receipt", "Small items paid in cash", Decimal("0.90"), day=18),
    TeamDoc(9, "invoice", "exception", "unknown", "Items from a new supplier", Decimal("410.00"), day=19, fixed=True),
    TeamDoc(10, "invoice", "exception", "trade", "Second copy of an earlier invoice", Decimal("1.00"),
            duplicate_of=12, day=20),
    TeamDoc(11, "invoice", "exception", "trade", "Invoice without a number", Decimal("0.92"), missing_number=True,
            day=21),
)
STARTER_NUMBERS = (12, 1, 4, 7, 9, 11)


def team_set(kind):
    if kind == "none":
        return ()
    if kind == "starter":
        return tuple(doc for doc in FULL_SET if doc.n in STARTER_NUMBERS)
    return FULL_SET


def sources(books):
    """(contact code, account, printed tax rate) for the trade supplier, the utility and the cash receipts."""
    if books.code == "KNT":
        return {"trade": ("PAU", "5000", Decimal("0.10")), "utility": ("TUA", "6110", Decimal("0.08")),
                "receipt": ("REN", "6140", Decimal("0"))}
    p = books.profile
    return {name: (source.supplier, source.account, source.rate)
            for name, source in (("trade", p.stock_inbox), ("utility", p.utility_inbox), ("receipt", p.receipt_inbox))}


def usual_amounts(books):
    """The median printed subtotal of each source's posted Inbox documents (the app compares new ones against them)."""
    supply = sources(books)
    _sales, purchases, _bank = books.books()
    result = {}
    for name, (code, _account, _rate) in supply.items():
        kinds = {"trade": {"invoice"}, "utility": {"bill"}, "receipt": {"receipt"}}[name]
        nets = sorted(doc.subtotal for doc in purchases if doc.contact == code and doc.kind in kinds
                      and doc.state == "posted")
        result[name] = nets[len(nets) // 2]
    return result


PREFIX = {"invoice": "PI", "bill": "UB", "receipt": "RC", "credit_note": "SC", "debit_note": "SD"}


def number_for(person, doc):
    target = doc.duplicate_of or doc.n
    return f"{PREFIX['invoice' if doc.duplicate_of else doc.kind]}-T{person.number}-{target:02d}"


def gold_for(books, person, doc, assignee_id):
    """Everything the document prints, its expected lane and who it belongs to."""
    supply = sources(books)
    code, account, rate = supply.get(doc.source, supply["trade"])
    if doc.source == "unknown":
        supplier, street = UNKNOWN_SUPPLIER
    else:
        supplier, street = books.contacts[code][1], books.contacts[code][3]
    if doc.kind == "credit_note" and account.startswith("5"):
        account = "5010"
    net = doc.net if doc.fixed else (usual_amounts(books)[doc.source] * doc.net).quantize(Decimal("0.01"))
    tax = (net * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    issued = date(2026, 11, doc.day)
    due = None if doc.kind in {"receipt", "credit_note", "debit_note"} else issued + timedelta(days=30)
    number = None if doc.missing_number else number_for(person, doc)
    related = number_for(person, TeamDoc(doc.related, "invoice", "", "", "", Decimal(0))) if doc.related else None
    return {"key": f"{books.code}:{person.slot}:{doc.n}", "slot": person.slot, "n": doc.n, "kind": doc.kind,
            "lane": doc.lane, "supplier": supplier, "supplier_street": street, "contact": code if doc.source != "unknown" else None,
            "number": number, "related": related, "date": issued.isoformat(), "due": due.isoformat() if due else None,
            "description": doc.description, "account": account, "subtotal": money(net), "tax": money(tax),
            "total": money(net + tax), "assignee": str(assignee_id),
            "file": f"{issued}_T{person.number}_{PREFIX[doc.kind]}_{doc.n:02d}.{'jpg' if doc.kind == 'receipt' else 'pdf'}"}


def render(books, gold, folder):
    """The document's page (PDF, or a photographed JPG for receipts) and where it was written."""
    from ai.synthetic.company_pack import NOTICE
    from ai.synthetic.generate import Generator, Page

    page = Page(595, 842)
    title = TITLES[gold["kind"]]
    page.text(40, 40, title.upper(), 18, bold=True)
    company_address = books.company["address"]
    for y, value in enumerate((gold["supplier"], gold["supplier_street"], "", "Bill to:", books.company["name"],
                               company_address)):
        page.text(40, 80 + y * 16, value, 11)
    page.text(40, 190, f"Document no. {gold['number'] or ''}", 11)
    page.text(40, 209, f"Issued {gold['date']}", 11)
    if gold["due"]:
        page.text(40, 228, f"Due {gold['due']}", 11)
    if gold["related"]:
        page.text(40, 228, f"Original invoice {gold['related']}", 11)
    if gold["kind"] == "receipt":
        page.text(40, 228, "Paid in cash at 11:40:00", 11)
    page.line(40, 555, 256)
    page.text(40, 266, "Description", 11, bold=True)
    page.text(391, 266, "Qty       Unit (MYR)       Amount (MYR)", 9)
    page.text(40, 287, gold["description"], 11)
    page.text(391, 287, f"1          {gold['subtotal']:>9}           {gold['subtotal']:>9}", 10)
    page.line(40, 555, 310)
    for y, (label, key) in enumerate((("Subtotal", "subtotal"), ("Tax", "tax"), ("Total", "total"))):
        page.text(350, 325 + y * 18, label, 11, bold=y == 2)
        page.text(555, 325 + y * 18, f"MYR {gold[key]}", 11, right=True, bold=y == 2)
    page.text(40, 435, NOTICE, 9)
    data = page.pdf()
    if gold["file"].endswith(".jpg"):  # receipts are photographed, scanned bills are JPG scans
        data, _size, _variant = Generator("holdout").degrade(data, gold["kind"] == "receipt")
    path = folder / gold["file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def values_for(books, gold):
    doc = Document(gold["number"] or "", gold["kind"], gold["contact"] or "", date.fromisoformat(gold["date"]),
                   date.fromisoformat(gold["due"]) if gold["due"] else None, gold["description"],
                   Decimal(gold["subtotal"]), Decimal(gold["tax"]), gold["account"], gold["related"],
                   state="exception" if not gold["number"] else "posted")
    from services.admin.company_seed import purchase_values

    return purchase_values(books, doc, supplier_name=gold["supplier"])


# --------------------------------------------------------------------------- applying the Inbox sets
def existing_keys(db, tenant_id):
    return {row["key"] for row in db.execute(
        """SELECT x.raw_json->'gold'->>'key' AS key FROM documents d
        JOIN raw_extractions x ON x.document_id=d.id AND x.is_current
        WHERE d.tenant_id=%s AND x.raw_json->>'mode'=%s""", (tenant_id, TEAM_MODE)).fetchall()}


def plan_inbox(db, books, people):
    """[(person, user row, [TeamDoc still missing])] for this company's roster accountants."""
    tenant = find_tenant(db, books.code)
    if not tenant:
        raise SeedError(f"{books.code} does not exist in this database yet.")
    have = existing_keys(db, tenant["id"])
    result = []
    for person in people:
        if person.company != books.code or person.role != "accountant" or person.inbox == "none":
            continue
        user = db.execute("SELECT * FROM users WHERE lower(email)=%s AND tenant_id=%s AND role='accountant'",
                          (person.email, tenant["id"])).fetchone()
        if not user:
            raise SeedError(f"{person.slot}: run team-accounts first ({mask_email(person.email)} has no account here).")
        missing = [doc for doc in team_set(person.inbox) if f"{books.code}:{person.slot}:{doc.n}" not in have]
        result.append((person, user, missing))
    return tenant, result


def _fixture_users(db, books, tenant):
    users, before = {}, {}
    for role, email in books.emails.items():
        user = db.execute("SELECT * FROM users WHERE lower(email)=%s AND tenant_id=%s AND role=%s",
                          (email, tenant["id"], role)).fetchone()
        if not user:
            raise SeedError(f"The {books.code} fixture login {email} is missing; it is created when the company is seeded.")
        before[user["id"]] = user["status"]
        users[role] = db.execute("UPDATE users SET status='active' WHERE id=%s RETURNING *", (user["id"],)).fetchone()
    return users, before


def apply_inbox(connect, books, people, folder):
    from fastapi.testclient import TestClient
    import main
    from services.admin.company_seed import capture_document
    from services.admin.knt_seed import admin_call
    from services.routes.documents.routes import _assign_documents

    with connect() as db:
        tenant, work = plan_inbox(db, books, people)
        users, before = _fixture_users(db, books, tenant)
    tokens, sessions = issue_tokens(users)
    made = 0
    try:
        with closing(TestClient(main.app, base_url="http://127.0.0.1")) as client:
            api = Api(client, tokens)
            accounts = {row["code"]: row for row in api.call("local_admin", "GET", "/api/ledger/accounts").json()}
            for person, assignee, missing in work:
                for doc in missing:
                    gold = gold_for(books, person, doc, assignee["id"])
                    source = render(books, gold, Path(folder) / person.slot)
                    values, number_field = values_for(books, gold)
                    document_id = capture_document(connect, users["accountant"], values, number_field, source,
                                                   kind=doc.kind, description=gold["description"],
                                                   subtotal=Decimal(gold["subtotal"]), review_note=doc.review,
                                                   mode=TEAM_MODE, extra={"gold": gold})
                    review = {"expectedContentRevision": 1, "note": "Prepared for the team demo"}
                    path = f"/api/documents/{document_id}/review"
                    if doc.lane in {"in_review", "posted"}:
                        api.call("accountant", "POST", path, json=review)
                    if doc.lane == "posted":
                        admin_call(api, "POST", path + "/decision", {**review, "decision": "approved"})
                        record = admin_call(api, "POST", f"/api/records/from-document/{document_id}", {})
                        admin_call(api, "POST", f"/api/erp/scanned/{record['id']}/post",
                                   {"accountId": accounts[gold["account"]]["id"]})
                    with connect() as db:
                        priority = "high" if doc.lane == "exception" else "normal"
                        due_at = datetime(2026, 11, 28, 10, tzinfo=timezone.utc) if doc.lane != "posted" else None
                        _assign_documents(db, dict(users["local_admin"]), [document_id], str(assignee["id"]),
                                          priority=priority, due_at=due_at)
                    made += 1
    finally:
        with connect() as db:
            db.execute("DELETE FROM web_sessions WHERE session_id=ANY(%s)", (sessions,))
            for user_id, status in before.items():
                db.execute("UPDATE users SET status=%s WHERE id=%s", (status, user_id))
    return made


def inbox_lines(work):
    lines = []
    for person, _user, missing in work:
        lanes = {}
        for doc in missing:
            lanes[doc.lane] = lanes.get(doc.lane, 0) + 1
        text = ", ".join(f"{count} {lane.replace('_', ' ')}" for lane, count in sorted(lanes.items())) or "nothing to add"
        lines.append(f"  {person.slot:<18} {mask_email(person.email):<22} {text}")
    return lines


def local_password(environ, name, local):
    if not name:
        return None
    if not local:
        raise SeedError("--password-env is for a local database only.")
    value = environ.get(name) or ""
    from security.authentication import strong_password

    if not strong_password(value):
        raise SeedError(f"{name} is not set to a strong password (8-256 characters, upper, lower, number, symbol).")
    return value
