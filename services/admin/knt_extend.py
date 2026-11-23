"""Add the richer KNT demo data to a KNT company that already has the original books, without touching them.

    python services/admin/manage.py extend-knt --as-user local-admin@example.test            # dry run: prints the plan
    python services/admin/manage.py extend-knt --as-user local-admin@example.test --apply

Additive and idempotent: it only creates what is missing (contacts, tax codes, payment terms, a cash-in-hand account,
products, quotations, orders, invoices, notes, bills and payments) and a second run finds nothing to do. Existing rows
are never edited or deleted. The only optional edit is --fill-blank-contacts, which gives the five original contacts the
header values (SSM, TIN, SST number, phone, terms) they lack and never overwrites a value they already hold.
Users are not changed: the app is called in-process as the named company admin, through two temporary web sessions that
are deleted afterwards (the documents' audit rows name that admin). Typed documents keep their PDFs in the database and
nothing is uploaded to document storage.

A remote database (hosted UAT) needs both --allow-remote and KNT_EXTEND_REMOTE_HOST set to that database's host name,
so it cannot be reached by accident. Without --apply nothing is written.
"""
from __future__ import annotations

import os
from contextlib import closing
from urllib.parse import urlsplit

from ai.synthetic.knt_extra import CASH_ACCOUNT, PAYMENT_TERMS, PRODUCTS, TAX_CODES, extension
from ai.synthetic.knt_pack import CONTACTS, original_books
from services.admin.demo_company import Api, SeedError, _is_loopback, issue_tokens
from services.admin.knt_seed import (
    LOCK_KEY, create_contacts, find_company, setup_catalogue, typed_bills, typed_cash, typed_quotes, typed_sales,
)


def require_target(allow_remote: bool, environ=None):
    """The connect function for the database in DATABASE_URL, refusing a remote one unless explicitly confirmed."""
    environ = os.environ if environ is None else environ
    from database.connection import connect

    try:
        host = urlsplit(environ.get("DATABASE_URL", "")).hostname or ""
    except ValueError:
        host = ""
    if not host:
        raise SeedError("DATABASE_URL is not set to a PostgreSQL URL.")
    if not _is_loopback(host):
        if not allow_remote or (environ.get("KNT_EXTEND_REMOTE_HOST") or "").strip().lower() != host.lower():
            raise SeedError("This database is not on this machine. Pass --allow-remote and set KNT_EXTEND_REMOTE_HOST "
                            "to its host name to extend it.")
    return connect


def read_state(db, tenant):
    """What the company already holds, so only what is missing is created."""
    contacts = {}
    for kind, table in (("customer", "customers"), ("supplier", "vendors")):
        for row in db.execute(f"SELECT id,contact_code FROM {table} WHERE tenant_id=%s AND contact_code IS NOT NULL",
                              (tenant,)).fetchall():
            contacts[(kind, row["contact_code"])] = str(row["id"])
    headers = db.execute("""SELECT id,document_type,document_number,customer_id,vendor_id,
        payload->'fields'->>'fixtureNumber' AS fixture FROM erp_document_headers WHERE tenant_id=%s""", (tenant,)).fetchall()
    return {
        "contacts": contacts,
        "fixtures": {row["fixture"]: row for row in headers if row["fixture"]},
        "quotations": {row["document_number"] for row in headers if row["document_type"] == "quotation"},
        "payments": {row["document_number"] for row in headers if row["document_type"] in {"payment", "purchase-payment"}},
        "catalogue": {(row["kind"], row["code"]) for row in db.execute(
            "SELECT kind,code FROM erp_catalog_entries WHERE tenant_id=%s", (tenant,)).fetchall()},
        "skus": {row["sku"] for row in db.execute("SELECT sku FROM erp_products WHERE tenant_id=%s", (tenant,)).fetchall()},
        "records": {row["invoice_number"]: str(row["id"]) for row in db.execute(
            """SELECT r.id,r.invoice_number FROM standardised_records r JOIN documents d ON d.id=r.document_id
            AND d.tenant_id=r.tenant_id WHERE r.tenant_id=%s AND r.is_current AND d.excluded_at IS NULL
            AND r.invoice_number LIKE 'PI-%%'""", (tenant,)).fetchall()},
    }


def plan(state, found):
    """What a run would create, by kind (the numbers an operator checks before --apply)."""
    held = state["contacts"]
    return {
        "contacts": [code for code, (kind, *_rest) in CONTACTS.items() if (kind, code) not in held],
        "tax codes": [c for c in TAX_CODES if ("tax-code", c) not in state["catalogue"]],
        "payment terms": [c for c in PAYMENT_TERMS if ("payment-term", c) not in state["catalogue"]],
        "cash account": [] if ("bank-account", CASH_ACCOUNT["code"]) in state["catalogue"] else [CASH_ACCOUNT["code"]],
        "products": [p.sku for p in PRODUCTS if p.sku not in state["skus"]],
        "quotations": [q.number for q in found.quotes if q.number not in state["quotations"]],
        "sales documents": [d.number for d in found.sales if d.number not in state["fixtures"]],
        "supplier bills": [b.number for b in found.bills if b.number not in state["fixtures"]],
        "payments": [p.reference for p in found.cash if p.reference not in state["payments"]],
    }


def conflicts(db, tenant, state, found):
    """Things already in the company that would make a create fail or clash (rows someone typed by hand, or a run that
    stopped half way). Empty means the plan can run. Reported by the dry run too."""
    from validation.matching.vendor_matching import normalize_vendor_name

    problems = []
    for kind, table, column in (("customer", "customers", "customer_name"), ("supplier", "vendors", "vendor_name")):
        names = {normalize_vendor_name(row["name"]) for row in db.execute(
            f"SELECT {column} AS name FROM {table} WHERE tenant_id=%s", (tenant,)).fetchall()}
        for code, (each, name, *_rest) in CONTACTS.items():
            if each == kind and (kind, code) not in state["contacts"] and normalize_vendor_name(name) in names:
                problems.append(f"The {kind} name '{name}' already exists under another code.")
    held = {(row["document_type"], row["document_number"]) for row in db.execute(
        """SELECT document_type,document_number FROM erp_document_headers WHERE tenant_id=%s
        AND payload->'fields'->>'fixtureNumber' IS NULL""", (tenant,)).fetchall()}
    for doc in found.sales:
        if doc.number not in state["fixtures"] and (doc.kind, doc.number) in held:
            problems.append(f"{doc.kind} {doc.number} exists but is not a KNT fixture document.")
        if doc.quote and doc.number not in state["fixtures"] and doc.quote in state["quotations"]:
            problems.append(f"Quotation {doc.quote} exists without its invoice {doc.number}: a half-finished run? "
                            "Restore the pre-run snapshot instead of continuing.")
    for bill in found.bills:
        if bill.number not in state["fixtures"] and ("bill", bill.number) in held:
            problems.append(f"bill {bill.number} exists but is not a KNT fixture document.")
    return problems


def extend(connect, admin_email, *, apply=False, fill_blank_contacts=False):
    """Print the plan; with apply, create what is missing and return the tally summaries (None for a dry run)."""
    from fastapi.testclient import TestClient
    import main
    from services.admin.knt_tally import print_tally, tally

    main.check_schema_compatibility()
    found = extension()
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        tenant = find_company(db)
        if not tenant:
            raise SeedError("KNT is not in this database; seed it first.")
        if not db.execute("SELECT 1 FROM accounting_periods WHERE tenant_id=%s LIMIT 1", (tenant["id"],)).fetchone():
            raise SeedError("KNT has no accounting periods; this extends existing books, it does not create them.")
        admin = db.execute("SELECT * FROM users WHERE lower(email)=lower(%s)", (admin_email,)).fetchone()
        if not admin or admin["tenant_id"] != tenant["id"] or admin["role"] != "local_admin" or admin["status"] != "active":
            raise SeedError("--as-user must be an active company admin (local_admin) of KNT.")
        state = read_state(db, tenant["id"])
        base_numbers = {doc.number for doc in original_books()[0]}
        if not base_numbers <= set(state["fixtures"]):
            raise SeedError("The original KNT sales documents are not all here; refusing to extend a different company.")
        problems = conflicts(db, tenant["id"], state, found)
        if problems:
            raise SeedError("Cannot extend KNT safely:\n  " + "\n  ".join(problems))
    todo = plan(state, found)
    for kind, items in todo.items():
        shown = f" ({', '.join(items[:4])}{', ...' if len(items) > 4 else ''})" if items else ""
        print(f"{kind:>16}: {len(items)} to create{shown}")
    if not apply:
        print("Dry run: nothing was written. Add --apply to create these.")
        return None
    if not any(todo.values()) and not fill_blank_contacts:
        print("Nothing to add: the extension is already complete.")
    tokens, sessions = issue_tokens({"accountant": admin, "local_admin": admin})
    try:
        with closing(TestClient(main.app, base_url="http://127.0.0.1")) as client:
            api = Api(client, tokens)
            accounts = {row["code"]: row for row in api.call("local_admin", "GET", "/api/ledger/accounts").json()}
            cash_account, products = setup_catalogue(api, accounts)
            listed = api.call("local_admin", "GET", "/api/erp/contacts", params={"includeInactive": "true"}).json()
            wanted = {(kind, code) for code, (kind, *_rest) in CONTACTS.items()}
            existing = {row["code"]: row for row in listed if (row["kind"], row.get("code")) in wanted}
            contacts = create_contacts(api, accounts, existing, fill_blanks=fill_blank_contacts)
            held = state["fixtures"]
            actual = {number: {"id": str(row["id"]), "number": row["document_number"],
                               "contactId": str(row["customer_id"] or row["vendor_id"])} for number, row in held.items()}
            typed = typed_sales(api, [doc for doc in found.sales if doc.number not in held], contacts, accounts,
                                products, actual)
            typed_quotes(api, [q for q in found.quotes if q.number not in state["quotations"]],
                         contacts, accounts, products)
            made = typed_bills(api, [bill for bill in found.bills if bill.number not in held], contacts, accounts,
                               products)
            bills = {**{b.number: actual[b.number] for b in found.bills if b.number in held}, **made}
            typed_cash(api, [p for p in found.cash if p.reference not in state["payments"]],
                       [*original_books()[1], *found.bills], contacts, typed, state["records"], bills, cash_account)
    finally:
        with connect() as db:
            db.execute("DELETE FROM web_sessions WHERE session_id=ANY(%s)", (sessions,))
    with connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        summaries = tally(db)
    print_tally(summaries)
    return summaries
