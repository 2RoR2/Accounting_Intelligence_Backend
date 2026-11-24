"""Seed a company's demo books through the real app, for any company in `ai.synthetic.sme_books` (TKK, EHB, CDA).

    python services/admin/manage.py seed-company --company TKK          # local: create the books from empty
    python services/admin/manage.py reset-company --company TKK         # local: wipe that company and rebuild it
    python services/admin/manage.py apply-company --company TKK         # dry run: what would be created
    python services/admin/manage.py apply-company --company TKK --apply [--allow-remote]

The steps are KNT's (`services/admin/knt_seed.py`) with the company passed in: accounting periods, chart of accounts,
bank and cash accounts, tax codes, payment terms, products, contacts, an opening-funds journal, typed Sales
(quotation chains, invoices, credit and debit notes), quotations, typed supplier bills, Inbox purchases stored as
already-read records (no AI is called), bank statements imported and matched, reconciliation periods, cash payments,
and finally the months listed in `close_months` closed. KNT itself keeps `seed-knt` / `reset-knt`.

Users: the seed works as two fixture logins of the company (`admin@<code>.demo.test` local admin and
`accountant@<code>.demo.test` accountant) through short-lived web sessions it deletes afterwards. Locally they get a
generated password printed once. On a remote database they get no password at all and are disabled at the end, so
nobody can sign in with them; real people get their own accounts from `team-accounts`.

A company that already has books is never touched: seed-company and apply-company stop and say so. A remote
database needs --allow-remote and COMPANY_PACK_REMOTE_HOST equal to its host (see `require_target`).
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from ai.synthetic.knt_pack import money
from services.admin.demo_company import (
    Api, SeedError, _create_periods, _is_loopback, _remote_destination, generated_password, import_csv, issue_tokens,
    refusal_reason,
)
from services.admin.erp_demo import Seeder
from services.admin.knt_seed import admin_call, line_body, pass_through, typed_bills, typed_cash, typed_quotes

ROOT = Path(__file__).resolve().parents[2]
ERP = "/api/erp/documents"
PERIODS = "/api/bank/reconciliation/periods"
PACK_ROOT = ROOT / "ai" / "synthetic" / "output" / "packs"
REMOTE_HOST_VARIABLE = "COMPANY_PACK_REMOTE_HOST"


# --------------------------------------------------------------------------- where it may run
def database_host(environ=None):
    environ = os.environ if environ is None else environ
    try:
        return (urlsplit(environ.get("DATABASE_URL", "")).hostname or "").lower()
    except ValueError:
        return ""


def is_local(environ=None):
    """True when every address libpq would use for DATABASE_URL is this machine."""
    environ = os.environ if environ is None else environ
    host = database_host(environ)
    return bool(host) and _is_loopback(host) and not _remote_destination(environ.get("DATABASE_URL", ""), environ)


def require_target(allow_remote: bool, environ=None, *, need_local_storage=True):
    """The connect function for this run, or a SeedError saying why this database may not be used.

    Local: APP_ENV=development, every address libpq would use is loopback, and (when the command writes Inbox
    files) S3_BUCKET is empty. Remote: --allow-remote AND COMPANY_PACK_REMOTE_HOST equal to the URL's host."""
    from database.connection import connect

    environ = os.environ if environ is None else environ
    if is_local(environ):
        reason = refusal_reason(environ)
        if reason and (need_local_storage or "S3_BUCKET" not in reason):
            raise SeedError(reason)
        return connect
    host = database_host(environ)
    if not allow_remote:
        raise SeedError("This database is not on this machine. Pass --allow-remote and set "
                        f"{REMOTE_HOST_VARIABLE} to its host name to confirm.")
    if not host or (environ.get(REMOTE_HOST_VARIABLE) or "").strip().lower() != host:
        raise SeedError(f"{REMOTE_HOST_VARIABLE} does not name this database's host; nothing was done.")
    if "host=" in (urlsplit(environ.get("DATABASE_URL", "")).query or "") or environ.get("PGHOST") \
            or environ.get("PGHOSTADDR") or environ.get("PGSERVICE"):
        raise SeedError("The database address is overridden (query string or PGHOST/PGHOSTADDR/PGSERVICE); "
                        "use a plain URL.")
    return connect


@contextmanager
def company_lock(connect, books):
    with connect() as lock:
        lock.execute("SELECT pg_advisory_xact_lock(%s)", (books.lock_key,))
        yield connect


# --------------------------------------------------------------------------- company and fixture users
def find_company(db, books):
    company = books.company
    rows = db.execute("SELECT * FROM tenants WHERE tenant_code=%s OR registration_number=%s OR name=%s",
                      (company["code"], company["ssm"], company["name"])).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or (rows[0]["name"], rows[0]["registration_number"]) != (company["name"], company["ssm"]):
        raise SeedError(f"The {books.code} identity conflicts with an existing company.")
    if rows[0]["tenant_code"] not in (company["code"], None):
        raise SeedError(f"The existing company has a different code than {books.code}.")
    return rows[0]


def has_books(db, tenant_id):
    return bool(db.execute("SELECT 1 FROM accounting_periods WHERE tenant_id=%s LIMIT 1", (tenant_id,)).fetchone()
                or db.execute("SELECT 1 FROM documents WHERE tenant_id=%s LIMIT 1", (tenant_id,)).fetchone())


def setup_company(db, books, tenant, *, local: bool, password=None):
    """The company row and its two fixture logins. Locally they get a password (returned to print once);
    remotely they get none (they cannot sign in) and are disabled when the seed finishes."""
    from security.authentication import hash_password

    company = books.company
    if tenant:
        tenant = db.execute("UPDATE tenants SET tenant_code=%s WHERE id=%s RETURNING *",
                            (company["code"], tenant["id"])).fetchone()
    else:
        tenant = db.execute("""INSERT INTO tenants(tenant_code,name,registration_number,admin_email,seat_limit)
            VALUES (%s,%s,%s,%s,10) RETURNING *""",
                            (company["code"], company["name"], company["ssm"], books.emails["local_admin"])).fetchone()
    users, credentials = {}, {}
    for role, email in books.emails.items():
        user = db.execute("SELECT * FROM users WHERE lower(email)=%s", (email,)).fetchone()
        if user and (user["tenant_id"] != tenant["id"] or user["role"] != role):
            raise SeedError(f"The demo account {email} conflicts with an existing account.")
        if user:
            user = db.execute("UPDATE users SET status='active' WHERE id=%s RETURNING *", (user["id"],)).fetchone()
        else:
            chosen = (password or generated_password()) if local else None
            user = db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                VALUES (%s,%s,%s,%s,%s,'active') RETURNING *""",
                              (tenant["id"], f"{books.code} Demo {role.replace('_', ' ').title()}", email,
                               hash_password(chosen) if chosen else None, role)).fetchone()
            if local and password is None:
                credentials[email] = chosen
        users[role] = user
    return tenant, users, credentials


# --------------------------------------------------------------------------- catalogue and contacts
def contact_body(books, code, accounts):
    kind, name, account, address = books.contacts[code]
    head = books.contact_header(code)
    return {"kind": kind, "code": code, "legalName": name, "registrationNumber": head.ssm, "address": address,
            "city": head.city, "state": books.contact_state(code), "postcode": head.postcode, "phone": head.phone,
            "email": books.contact_email(code),
            "defaultExpenseAccountId": accounts[account]["id"] if kind == "supplier" else None,
            "fields": {"tin": head.tin, "sstNumber": head.sst, "paymentTermDays": head.terms,
                       "defaultAccountCode": account, "currency": "MYR", "category": head.category}}


def create_contacts(api, books, accounts):
    return {code: admin_call(api, "POST", "/api/erp/contacts", contact_body(books, code, accounts))["id"]
            for code in books.contacts}


def setup_catalogue(api, books, accounts):
    """Tax-code labels, payment terms, a cash-in-hand account and the products."""
    for kind, rows in (("tax-code", {code: (desc, {"ratePercent": f"{rate * 100:.0f}"})
                                     for code, (desc, rate) in books.tax_codes.items()}),
                       ("payment-term", {code: (name, {"days": days})
                                         for code, (name, days) in books.payment_terms.items()})):
        for code, (name, data) in rows.items():
            admin_call(api, "POST", f"/api/erp/catalog/{kind}", {"code": code, "name": name, "data": data})
    cash = books.cash_account
    cash_row = admin_call(api, "POST", "/api/erp/catalog/bank-account", {
        "code": cash["code"], "name": cash["name"], "ledgerAccountId": accounts[cash["ledger"]]["id"],
        "data": {"bankName": "Cash"}})
    products = {}
    for product in books.products:
        products[product.sku] = admin_call(api, "POST", "/api/erp/products", {
            "name": product.name, "sku": product.sku, "kind": product.kind, "unit": product.unit,
            "salePrice": money(product.sale), "purchasePrice": money(product.cost), "quantity": str(product.stock),
            "groupName": product.group, "incomeAccountId": accounts[product.income]["id"],
            "expenseAccountId": accounts[product.expense]["id"], "defaultTaxCode": product.tax_code,
        })
    return cash_row["id"], products


# --------------------------------------------------------------------------- typed Sales
def typed_sales(api, books, documents, contacts, accounts, products):
    actual = {}
    for doc in documents:
        fields = {"demo": books.code, "fixtureNumber": doc.number}
        lines = [line_body(line, accounts, products) for line in doc.lines]
        params = {"contactId": contacts[doc.contact], "documentDate": str(doc.issued),
                  "dueDate": str(doc.due) if doc.due else None, "lines": lines, "fields": fields,
                  "description": doc.description}
        if doc.quote:
            if not doc.quoted:
                raise SeedError(f"{doc.number}: a quoted sale needs its quotation date.")
            quote = admin_call(api, "POST", f"{ERP}/quotation", {
                **params, "fields": {"demo": books.code, "quoteOutcome": "accepted"}, "number": doc.quote,
                "status": "ready", "documentDate": str(doc.quoted), "dueDate": str(doc.valid_until),
                "description": "Accepted by the customer",
            })
            kind_now, node = pass_through(api, "quotation", quote, doc.flow, doc.quoted, doc.issued,
                                          f"Delivery for {doc.number}")
            invoice = admin_call(api, "POST", f"{ERP}/{kind_now}/{node['id']}/transfer", {"targetType": "invoice"})
            path = f"{ERP}/invoice/{invoice['id']}"
            admin_call(api, "PATCH", path, params)
            actual[doc.number] = admin_call(api, "POST", path + "/transition",
                                            {"status": "ready", "reason": "Quotation accepted"})
            continue
        if doc.related:
            target = actual[doc.related]
            params["referenceNo"] = target["number"]
            if doc.kind == "credit-note":
                params["allocations"] = [{"targetId": target["id"], "source": "typed", "amount": money(doc.total)}]
            else:
                params["relatedDocumentId"] = target["id"]
                params["description"] = f"Debit note for {target['number']} - {doc.description}"
        actual[doc.number] = admin_call(api, "POST", f"{ERP}/{doc.kind}",
                                        {**params, "number": doc.number, "status": "ready"})
    return actual


# --------------------------------------------------------------------------- Inbox purchases (already read)
def store_file(tenant_id, owner, document_id, filename, data):
    """Write an Inbox source file where the app reads it: the S3 bucket when configured, else local storage.
    Returns (storage_path, local path or None)."""
    from database.object_storage import object_store

    key = object_store.document_key(tenant_id, owner["id"], owner["name"], document_id, filename)
    mime = "image/jpeg" if filename.lower().endswith(".jpg") else "application/pdf"
    if object_store.enabled():
        return object_store.put_bytes(key, data, mime), None
    root = object_store.local_storage_root()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise SeedError("The capture path is outside local storage.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path), path


def capture_document(connect, owner, values, number_field, source, *, kind, description, subtotal,
                     review_note=None, mode="company_pack_fixture", extra=None):
    """Store one received document as an already-read record (status extracted) and run the app's validation.

    No extraction job and no AI call: the field values are the document's printed values."""
    from psycopg.types.json import Jsonb
    from main import validate_uploaded_bytes
    from validation.documents.document_validation import validate_document

    document_id = uuid4()
    data = source.read_bytes()
    validate_uploaded_bytes(source.name, data)
    tenant = owner["tenant_id"]
    stored, local_path = store_file(tenant, owner, document_id, source.name, data)
    mime = "image/jpeg" if source.suffix == ".jpg" else "application/pdf"
    try:
        with connect() as db:
            db.execute("""INSERT INTO documents(id,tenant_id,owner_id,original_filename,kind,category,storage_path,
                    mime_type,file_size_bytes,file_hash_sha256,status,page_count)
                VALUES (%s,%s,%s,%s,%s,'accounts_payable',%s,%s,%s,%s,'extracted',1)""",
                       (document_id, tenant, owner["id"], source.name, kind, stored, mime, len(data),
                        hashlib.sha256(data).hexdigest()))
            extraction = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
                VALUES (%s,1,true,'human_review',%s) RETURNING id""",
                                    (document_id, Jsonb({"mode": mode, "source": source.name, **(extra or {})}))).fetchone()["id"]
            for key, value in values.items():
                state = "resolved" if value is not None else "unreadable" if key == number_field else "missing"
                db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,
                        extraction_state,raw_value,normalized_value,current_value)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                           (extraction, document_id, key, key.replace("_", " ").title(), state, value, value, value))
            db.execute("""INSERT INTO line_items(extraction_id,document_id,line_number,description,quantity,unit_price,amount)
                VALUES (%s,%s,1,%s,1,%s,%s)""", (extraction, document_id, description, subtotal, subtotal))
            validate_document(db, str(document_id), owner)
            if review_note:
                rule, message = review_note
                db.execute("""INSERT INTO validation_results(document_id,tenant_id,content_revision,rule_id,document_type,
                        status,severity,requires_review,message)
                    VALUES (%s,%s,1,%s,'invoice','not_checked','medium',true,%s)""",
                           (document_id, tenant, rule, message))
    except Exception:
        if local_path:
            local_path.unlink(missing_ok=True)
        raise
    return str(document_id)


def purchase_values(books, doc, *, supplier_name=None):
    values = {
        "supplier": supplier_name or books.contacts[doc.contact][1], "customer": books.company["name"],
        "date": str(doc.issued), "due": str(doc.due) if doc.due else None,
        "currency": "MYR", "subtotal": money(doc.subtotal), "tax": money(doc.tax), "total": money(doc.total),
    }
    number_field = "invoice" if doc.kind == "invoice" else "reference"
    values[number_field] = doc.number if doc.state != "exception" else None
    if doc.related:
        values["original_invoice_number"] = doc.related
    if doc.kind == "receipt":
        values.update(payment_method="cash", amount_paid=money(doc.total), receipt_number=doc.number)
    if doc.kind == "bill":
        values.update(bill_number=doc.number, amount_due=money(doc.total))
    return values, number_field


DELIVERY_CHECK = ("PACK_DELIVERY_CONFIRMATION", "Confirm the goods were received before submitting this invoice.")


def capture_purchases(api, connect, books, owner, purchases, folder, accounts, periods):
    records, ids, bank_journals = {}, {}, {}
    for doc in purchases:
        source = folder / f"documents/{doc.issued:%Y-%m}" / doc.filename
        values, number_field = purchase_values(books, doc)
        document_id = capture_document(connect, owner, values, number_field, source, kind=doc.kind,
                                       description=doc.description, subtotal=doc.subtotal,
                                       review_note=DELIVERY_CHECK if doc.state == "to_review" else None)
        ids[doc.number] = document_id
        if doc.state != "posted":
            continue
        path = f"/api/documents/{document_id}/review"
        review = {"expectedContentRevision": 1, "note": f"Checked against the fictional {books.code} source"}
        api.call("accountant", "POST", path, json=review)
        admin_call(api, "POST", path + "/decision", {**review, "decision": "approved"})
        record = admin_call(api, "POST", f"/api/records/from-document/{document_id}", {})
        records[doc.number] = record["id"]
        if doc.kind in {"bill", "invoice"}:
            admin_call(api, "POST", f"/api/erp/scanned/{record['id']}/post", {"accountId": accounts[doc.account]["id"]})
            continue
        if doc.related:
            admin_call(api, "POST", f"/api/documents/{ids[doc.related]}/reconciliations", {
                "relatedDocumentId": document_id, "relationType": f"{doc.kind.split('_')[0]}_for",
                "amount": money(doc.total), "note": doc.description,
            })
        credit = doc.kind == "credit_note"
        cash_account = "1000" if doc.kind == "receipt" else "1010"
        lines = [(doc.account, doc.subtotal, credit), (cash_account, doc.total, not credit)]
        if doc.tax:
            lines.append(("1140", doc.tax, credit))
        journal = admin_call(api, "POST", "/api/ledger/journals", {
            "periodId": periods[f"{doc.issued:%Y-%m}"], "journalDate": str(doc.issued),
            "sourceType": "adjustment" if doc.related else "document", "sourceId": document_id,
            "idempotencyKey": f"{books.key_prefix}:purchase:{doc.number}",
            "description": f"{doc.number} {doc.description}",
            "lines": [{"accountId": accounts[account]["id"], "credit" if is_credit else "debit": money(amount)}
                      for account, amount, is_credit in lines],
        })
        if doc.related:
            bank_journals[doc.number] = journal["id"]
    return records, bank_journals


# --------------------------------------------------------------------------- bank
def import_and_match_bank(api, connect, books, tenant, folder, pack, bank_lines, bank_account, accounts, sales,
                          purchases, note_journals, bills):
    from services.routes.accounting.routes import mark_bank_line_matched

    bank_ref = books.company["bank_account_number"]
    for month, statement in pack["bank"].items():
        file = folder / statement["file"]
        imported = api.call("accountant", "POST", "/api/bank/imports",
                            files={"file": (file.name, file.read_bytes(), "text/csv")},
                            data={"source_bank_account_ref": bank_ref, "mapping": json.dumps(books.bank_mapping),
                                  "date_format": books.bank_date_format, "default_currency": "MYR"}).json()
        api.call("accountant", "POST", f"/api/bank-imports/{imported['importId']}/confirm")
    with connect() as db:
        rows = {row["reference"]: row for row in db.execute(
            "SELECT * FROM bank_transactions WHERE tenant_id=%s", (tenant,)).fetchall()}
    suspense = 0
    for month, cutoff in books.months.items():
        for line in (line for line in bank_lines if line.posted.strftime("%Y-%m") == month):
            row = rows[line.reference]
            path = f"/api/bank/transactions/{row['id']}"
            if line.kind == "unmatched":
                suspense += line.amount
            elif line.kind == "fee":
                admin_call(api, "POST", path + "/record-expense", {"ledgerAccountId": accounts["6230"]["id"]})
            elif line.kind == "supplier":
                admin_call(api, "POST", path + "/match-many", {"expectedAllocated": "0", "allocations": [
                    {"recordId": purchases[number], "amount": money(amount)} for number, amount in line.targets]})
                admin_call(api, "POST", path + "/post-payment", {})
            else:
                document_id = None
                if line.kind in {"customer", "bill_payment"}:
                    incoming = line.kind == "customer"
                    typed = sales if incoming else bills
                    payment = admin_call(api, "POST", f"{ERP}/{'payment' if incoming else 'purchase-payment'}", {
                        "number": line.reference, "contactId": typed[line.targets[0][0]]["contactId"],
                        "documentDate": str(line.posted), "status": "ready", "referenceNo": line.reference,
                        "amount": money(abs(line.amount)), "description": line.description,
                        "bankAccountId": bank_account, "allocations": [
                            {"targetId": typed[number]["id"], "source": "typed", "amount": money(amount)}
                            for number, amount in line.targets],
                    })
                    document_id, journal_id = payment["id"], payment["journalId"]
                else:
                    journal_id = note_journals[line.reference]
                # Link the posted evidence to the statement line without recording a second payment.
                with connect() as db:
                    evidence = db.execute("""SELECT j.journal_date, sum(l.debit-l.credit) AS amount
                        FROM journal_headers j JOIN journal_lines l ON l.journal_id=j.id AND l.tenant_id=j.tenant_id
                        WHERE j.tenant_id=%s AND j.id=%s AND j.status='posted' AND l.account_id=%s
                        GROUP BY j.journal_date""", (tenant, journal_id, accounts["1010"]["id"])).fetchone()
                    if (not evidence or evidence["journal_date"] != row["posted_date"]
                            or evidence["amount"] != row["signed_amount_decimal"]):
                        raise SeedError(f"Bank transfer evidence differs for {line.reference}.")
                    mark_bank_line_matched(db, row, document_id=document_id)
        with connect() as db:
            balance = db.execute("""SELECT COALESCE(sum(l.debit-l.credit),0) AS amount
                FROM journal_lines l JOIN journal_headers j ON j.id=l.journal_id AND j.tenant_id=l.tenant_id
                WHERE l.tenant_id=%s AND l.account_id=%s AND j.status='posted' AND j.journal_date<=%s""",
                                 (tenant, accounts["1010"]["id"], cutoff)).fetchone()["amount"]
        admin_call(api, "POST", PERIODS, {
            "bankAccountRef": bank_ref, "startsOn": str(cutoff.replace(day=1)), "endsOn": str(cutoff),
            "openingBalance": pack["bank"][month]["opening"], "closingBalance": pack["bank"][month]["closing"],
            "bookBalance": money(balance), "statementAdjustment": money(-suspense),
            "adjustmentEvidence": "Unidentified statement transfers awaiting remittance details",
        })


def close_periods(api, books, periods):
    for code in books.close_months:
        admin_call(api, "POST", f"/api/ledger/periods/{periods[code]}/close",
                   {"reason": "Month-end review completed (demo data)"})


def relabel(db, tenant_id, books):
    """typed_quotes / typed_bills (shared with KNT) mark documents "demo": "KNT"; give them this company's code."""
    db.execute("""UPDATE erp_document_headers SET payload=jsonb_set(payload,'{fields,demo}',to_jsonb(%s::text))
        WHERE tenant_id=%s AND payload->'fields'->>'demo'='KNT'""", (books.code, tenant_id))


# --------------------------------------------------------------------------- the whole seed
def plan(db, books):
    """What a seed would create in this database (the dry run). Raises when the company already has books."""
    tenant = find_company(db, books)
    if tenant and has_books(db, tenant["id"]):
        raise SeedError(f"{books.code} already has books; nothing to apply. Run tally --company {books.code}.")
    sales, purchases, bank = books.books()
    quotes, cash, bills = books.extras()
    taken = [email for email in books.emails.values()
             if db.execute("SELECT 1 FROM users WHERE lower(email)=%s AND tenant_id IS DISTINCT FROM %s",
                           (email, tenant["id"] if tenant else None)).fetchone()]
    if taken:
        raise SeedError(f"Fixture login(s) already used by another company: {', '.join(taken)}")
    return {"company": f"{books.company['name']} ({books.code})" + (" - exists, no books yet" if tenant else " - new"),
            "contacts": len(books.contacts), "products": len(books.products),
            "typed sales documents": len(sales), "quotations (open/expired/ordered)": len(quotes),
            "typed supplier bills": len(bills), "Inbox documents (already read)": len(purchases),
            "bank statement lines": len(bank), "cash payments": len(cash),
            "closed months": ", ".join(books.close_months) or "none"}


def seed(connect, books, folder=None, password=None, *, local=True):
    from fastapi.testclient import TestClient
    import main

    main.check_schema_compatibility()
    folder = Path(folder or PACK_ROOT / books.code.lower())
    pack = books.generate(folder)
    with connect() as db:
        plan(db, books)
        tenant, users, credentials = setup_company(db, books, find_company(db, books), local=local, password=password)
    for email, chosen in credentials.items():
        print(f"New local demo login {email}  {chosen}", flush=True)
    tokens, sessions = issue_tokens(users)
    try:
        with closing(TestClient(main.app, base_url="http://127.0.0.1")) as client:
            api = Api(client, tokens)
            _create_periods(api, books.months)
            import_csv(api, "/api/chart-of-accounts/import", folder / "coa.csv", "chart of accounts")
            setup = Seeder(api, books.code.lower(), books.months, None)
            accounts = setup.accounts()
            bank_account = setup.ensure_bank_account(accounts, books.company["bank_account_number"],
                                                     label=books.bank_label, bank_name=books.bank_name)
            periods = {p["periodCode"]: p["id"] for p in api.call("local_admin", "GET", "/api/ledger/periods").json()}
            cash_account, products = setup_catalogue(api, books, accounts)
            contacts = create_contacts(api, books, accounts)
            admin_call(api, "POST", "/api/ledger/journals", {
                "periodId": periods["2026-09"], "sourceType": "manual", "idempotencyKey": f"{books.key_prefix}:opening",
                "journalDate": "2026-09-01", "description": f"{books.code} opening funds",
                "lines": [{"accountId": accounts[code]["id"], side: money(amount)} for code, side, amount in
                          (("1010", "debit", books.opening_bank), ("1000", "debit", books.opening_cash),
                           ("3000", "credit", books.opening_bank + books.opening_cash))],
            })
            sales, purchases, bank = books.books()
            typed = typed_sales(api, books, sales, contacts, accounts, products)
            quotes, cash, bills = books.extras()
            typed_quotes(api, quotes, contacts, accounts, products)
            typed_supplier_bills = typed_bills(api, bills, contacts, accounts, products)
            scanned, note_journals = capture_purchases(api, connect, books, users["accountant"], purchases, folder,
                                                       accounts, periods)
            import_and_match_bank(api, connect, books, tenant["id"], folder, pack, bank, bank_account, accounts,
                                  typed, scanned, note_journals, typed_supplier_bills)
            typed_cash(api, cash, [*purchases, *bills], contacts, typed, scanned, typed_supplier_bills, cash_account)
            close_periods(api, books, periods)
        with connect() as db:
            relabel(db, tenant["id"], books)
    finally:
        with connect() as db:
            db.execute("DELETE FROM web_sessions WHERE session_id=ANY(%s)", (sessions,))
            if not local:
                db.execute("UPDATE users SET status='disabled' WHERE tenant_id=%s AND lower(email)=ANY(%s)",
                           (tenant["id"], list(books.emails.values())))
    return tenant


def reset(connect, books, folder=None, password=None):
    """Local only: delete the company's books and Inbox (users are kept) and seed them again."""
    import main
    from database.object_storage.object_store import local_storage_root
    from services.admin.knt_reset import wipe

    main.check_schema_compatibility()
    with connect() as db:
        tenant = find_company(db, books)
        stored = wipe(db, tenant["id"]) if tenant else []
    if tenant:
        tenant_root = local_storage_root() / "tenants" / str(tenant["id"])
        for value in stored:
            if "://" in value:
                continue
            path = Path(value).resolve()
            if path.is_relative_to(tenant_root) and path.is_file():
                path.unlink()
        print(f"Cleared {len(stored)} {books.code} Inbox documents and the {books.code} accounting data.")
    return seed(connect, books, folder, password)
