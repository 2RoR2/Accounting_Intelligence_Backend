"""Load the KNT fixture into the local app. Purchases use labelled capture snapshots."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from ai.synthetic.knt_extra import CASH_ACCOUNT, PAYMENT_TERMS, PRODUCTS, TAX_CODES
from ai.synthetic.knt_pack import (
    BANK_MAPPING, COMPANY, CONTACTS, MONTHS, OPENING_BANK, OPENING_CASH, books, contact_header, extras, generate, money,
)
from services.admin.demo_company import (
    Api, SeedError, _create_periods, _is_loopback, generated_password, import_csv, issue_tokens, refusal_reason,
)
from services.admin.erp_demo import Seeder

LOCK_KEY = 2026112305
EMAILS = {"local_admin": "admin@knt.demo.test", "accountant": "accountant@knt.demo.test"}
ERP = "/api/erp/documents"
PERIODS = "/api/bank/reconciliation/periods"


# Statements match the catalog entry by account number, so the display name can read like a real account.
KNT_BANK_LABEL = "Public Bank Current Account ••" + COMPANY["bank_account_number"][-4:]


def require_local():
    # Load normal local configuration before checking it. No connection is made on import.
    import os
    from database.connection import connect

    try:
        authority_host = urlsplit(os.environ.get("DATABASE_URL", "")).hostname or ""
    except ValueError:
        authority_host = ""
    if not _is_loopback(authority_host) or refusal_reason():
        raise SeedError("KNT commands require development mode, a loopback PostgreSQL URL and local document storage.")
    return connect


@contextmanager
def locked_database():
    connect = require_local()
    with connect() as lock:
        lock.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        yield connect


def find_company(db):
    rows = db.execute("""SELECT * FROM tenants WHERE tenant_code=%s OR registration_number=%s OR name=%s""",
                      (COMPANY["code"], COMPANY["ssm"], COMPANY["name"])).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or (rows[0]["name"], rows[0]["registration_number"]) != (COMPANY["name"], COMPANY["ssm"]):
        raise SeedError("The KNT identity conflicts with an existing company.")
    row = rows[0]
    if row["tenant_code"] != COMPANY["code"] and row["admin_email"] != "admin@trading.demo.test":
        raise SeedError("The existing company does not match the legacy KNT demo identity.")
    return row


def setup_company(db, tenant, password):
    from security.authentication import hash_password

    if tenant:
        tenant = db.execute("UPDATE tenants SET tenant_code=%s WHERE id=%s RETURNING *",
                            (COMPANY["code"], tenant["id"])).fetchone()
    else:
        tenant = db.execute("""INSERT INTO tenants(tenant_code,name,registration_number,admin_email)
            VALUES (%s,%s,%s,%s) RETURNING *""",
                            (COMPANY["code"], COMPANY["name"], COMPANY["ssm"], EMAILS["local_admin"])).fetchone()
    users, credentials = {}, {}
    for role, email in EMAILS.items():
        user = db.execute("SELECT * FROM users WHERE lower(email)=%s", (email,)).fetchone()
        if user and (user["tenant_id"] != tenant["id"] or user["role"] != role or user["status"] != "active"):
            raise SeedError(f"The demo account {email} conflicts with an existing account.")
        if not user:
            chosen = password or generated_password()
            user = db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                VALUES (%s,%s,%s,%s,%s,'active') RETURNING *""",
                              (tenant["id"], f"KNT Demo {role.replace('_', ' ').title()}", email,
                               hash_password(chosen), role)).fetchone()
            if password is None:
                credentials[email] = chosen
        users[role] = user
    return tenant, users, credentials


def admin_call(api, method, path, body):
    return api.call("local_admin", method, path, json=body).json()


def line_body(line, accounts, products):
    """One document line: the product, the typed tax label and amount, and the account the sale is posted to."""
    return {"itemId": products[line.sku]["id"] if line.sku else None, "description": line.description,
            "quantity": str(line.quantity),
            "unitPrice": money(line.price), "discount": money(line.discount), "tax": money(line.tax),
            "taxCode": line.tax_code, "accountId": accounts[line.account_code]["id"]}


def pass_through(api, source_type, source, steps, start, end, description):
    """Turn an accepted quotation into sales orders / delivery orders (record-only, issued) and return the last one."""
    node_type, node = source_type, source
    for number, step in enumerate(steps, 1):
        node = admin_call(api, "POST", f"{ERP}/{node_type}/{node['id']}/transfer", {"targetType": step})
        on = start + timedelta(days=(end - start).days * number // (len(steps) + 1))
        admin_call(api, "PATCH", f"{ERP}/{step}/{node['id']}", {"documentDate": str(on), "description": description})
        node = admin_call(api, "POST", f"{ERP}/{step}/{node['id']}/transition",
                          {"status": "ready", "reason": "Issued to the customer"})
        node_type = step
    return node_type, node


def typed_sales(api, documents, contacts, accounts, products, actual=None):
    # `actual` may already hold documents made earlier (the extension of an existing company): number -> document.
    actual = {} if actual is None else actual
    for doc in documents:
        fields = {"demo": "KNT", "fixtureNumber": doc.number}
        if doc.lines:
            lines = [line_body(line, accounts, products) for line in doc.lines]
        else:
            lines = [{"description": doc.description, "quantity": "1", "unitPrice": money(doc.subtotal),
                      "tax": money(doc.tax), "accountId": accounts[doc.account]["id"]}]
        params = {"contactId": contacts[doc.contact], "documentDate": str(doc.issued),
                  "dueDate": str(doc.due) if doc.due else None, "lines": lines, "fields": fields,
                  "description": doc.description}
        if doc.quote and doc.quoted:
            quote = admin_call(api, "POST", f"{ERP}/quotation", {
                **params, "fields": {"demo": "KNT", "quoteOutcome": "accepted"}, "number": doc.quote,
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
        if doc.quote:
            quote = admin_call(api, "POST", f"{ERP}/quotation", {
                **params, "fields": {"demo": "KNT", "quoteOutcome": "accepted"},
                "number": doc.quote, "status": "ready",
                "documentDate": str(doc.issued - timedelta(days=3)), "dueDate": str(doc.issued + timedelta(days=7)),
                "description": "Accepted by the customer",
            })
            expired_on = doc.issued + timedelta(days=5)
            admin_call(api, "POST", f"{ERP}/quotation", {
                **params, "contactId": contacts["KU"], "number": doc.quote.replace("-01", "-02"),
                "status": "ready", "documentDate": str(doc.issued - timedelta(days=2)),
                "dueDate": str(expired_on), "fields": {"demo": "KNT", "quoteOutcome": "expired"},
                "description": f"Expired on {expired_on} without acceptance",
            })
            invoice = admin_call(api, "POST", f"{ERP}/quotation/{quote['id']}/transfer", {"targetType": "invoice"})
            path = f"{ERP}/invoice/{invoice['id']}"
            admin_call(api, "PATCH", path, params)
            actual[doc.number] = admin_call(api, "POST", path + "/transition",
                                            {"status": "ready", "reason": "Quotation accepted"})
            continue
        kind = doc.kind
        if doc.related:
            target = actual[doc.related]
            params["referenceNo"] = target["number"]
            if kind == "credit-note":
                params["allocations"] = [{"targetId": target["id"], "source": "typed", "amount": money(doc.total)}]
            else:
                # Plan M1: a real sales debit note that references the invoice it adds to.
                params["relatedDocumentId"] = target["id"]
                params["description"] = f"Debit note for {target['number']} - {doc.description}"
        actual[doc.number] = admin_call(api, "POST", f"{ERP}/{kind}",
                                        {**params, "number": doc.number, "status": "ready"})
    return actual


def typed_quotes(api, quotes, contacts, accounts, products):
    """Quotations that were not turned into an invoice: still open, expired, or accepted into a sales order."""
    for quote in quotes:
        made = admin_call(api, "POST", f"{ERP}/quotation", {
            "contactId": contacts[quote.contact], "number": quote.number, "status": "ready",
            "documentDate": str(quote.issued), "dueDate": str(quote.valid_until), "description": quote.description,
            "lines": [line_body(line, accounts, products) for line in quote.lines],
            "fields": {"demo": "KNT", "quoteOutcome": quote.outcome},
        })
        if quote.outcome == "expired":
            admin_call(api, "POST", f"{ERP}/quotation/{made['id']}/transition",
                       {"status": "expired", "reason": "Validity lapsed without acceptance"})
        if quote.via:
            pass_through(api, "quotation", made, quote.via, quote.issued, quote.issued + timedelta(days=3),
                         f"For {quote.number}")


def typed_bills(api, bills, contacts, accounts, products):
    """Typed supplier bills, posted: the stock bought for the sales above, telephone and one repair."""
    made = {}
    for bill in bills:
        made[bill.number] = admin_call(api, "POST", f"{ERP}/bill", {
            "number": bill.number, "contactId": contacts[bill.contact], "documentDate": str(bill.issued),
            "dueDate": str(bill.due), "description": bill.description, "status": "ready",
            "lines": [line_body(line, accounts, products) for line in bill.lines],
            "fields": {"demo": "KNT", "fixtureNumber": bill.number},
        })
    return made


def typed_cash(api, cash, purchases, contacts, sales, records, bills, cash_account):
    """Payments received from customers and made to suppliers, all through cash in hand (no bank statement line)."""
    suppliers = {doc.number: doc.contact for doc in purchases}
    for line in cash:
        incoming = line.kind == "cash_in"

        def target(number):
            if incoming:
                return {"targetId": sales[number]["id"], "source": "typed"}
            if number in bills:  # a typed bill; the original suppliers' bills are scanned
                return {"targetId": bills[number]["id"], "source": "typed"}
            return {"targetId": records[number], "source": "scanned"}

        targets = [{**target(number), "amount": money(amount)} for number, amount in line.targets]
        contact = sales[line.targets[0][0]]["contactId"] if incoming else contacts[suppliers[line.targets[0][0]]]
        admin_call(api, "POST", f"{ERP}/{'payment' if incoming else 'purchase-payment'}", {
            "number": line.reference, "contactId": contact, "documentDate": str(line.posted), "status": "ready",
            "referenceNo": line.reference, "description": line.description, "amount": money(abs(line.amount)),
            "bankAccountId": cash_account, "allocations": targets,
        })


def capture_purchase(connect, owner, doc, source):
    from psycopg.types.json import Jsonb
    from database.object_storage.object_store import document_key, local_storage_root
    from main import validate_uploaded_bytes
    from validation.documents.document_validation import validate_document

    document_id = uuid4()
    data = source.read_bytes()
    validate_uploaded_bytes(source.name, data)
    tenant = owner["tenant_id"]
    root = local_storage_root()
    path = (root / document_key(tenant, owner["id"], owner["name"], document_id, source.name)).resolve()
    if not path.is_relative_to(root):
        raise SeedError("The KNT capture path is outside local storage.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    mime = "image/jpeg" if source.suffix == ".jpg" else "application/pdf"
    values = {
        "supplier": CONTACTS[doc.contact][1], "customer": COMPANY["name"],
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
    try:
        with connect() as db:
            db.execute("""INSERT INTO documents(id,tenant_id,owner_id,original_filename,kind,category,storage_path,
                    mime_type,file_size_bytes,file_hash_sha256,status,page_count)
                VALUES (%s,%s,%s,%s,%s,'accounts_payable',%s,%s,%s,%s,'extracted',1)""",
                       (document_id, tenant, owner["id"], source.name, doc.kind, str(path), mime, len(data),
                        hashlib.sha256(data).hexdigest()))
            extraction = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
                VALUES (%s,1,true,'human_review',%s) RETURNING id""",
                                    (document_id, Jsonb({"mode": "knt_gold_fixture", "source": source.name}))).fetchone()["id"]
            for key, value in values.items():
                state = "resolved" if value is not None else "unreadable" if key == number_field else "missing"
                db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,
                        extraction_state,raw_value,normalized_value,current_value)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                           (extraction, document_id, key, key.replace("_", " ").title(), state, value, value, value))
            db.execute("""INSERT INTO line_items(extraction_id,document_id,line_number,description,quantity,unit_price,amount)
                VALUES (%s,%s,1,%s,1,%s,%s)""", (extraction, document_id, doc.description, doc.subtotal, doc.subtotal))
            validate_document(db, str(document_id), owner)
            if doc.state == "to_review":
                db.execute("""INSERT INTO validation_results(document_id,tenant_id,content_revision,rule_id,document_type,
                        status,severity,requires_review,message)
                    VALUES (%s,%s,1,'KNT_DELIVERY_CONFIRMATION','invoice','not_checked','medium',true,
                            'Confirm the warehouse received the goods before submitting this invoice.')""",
                           (document_id, tenant))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return str(document_id)


def capture_purchases(api, connect, owner, purchases, folder, accounts, periods):
    records, ids, bank_journals = {}, {}, {}
    for doc in purchases:
        source = folder / f"documents/{doc.issued:%Y-%m}" / doc.filename
        document_id = capture_purchase(connect, owner, doc, source)
        ids[doc.number] = document_id
        if doc.state != "posted":
            continue
        path = f"/api/documents/{document_id}/review"
        review = {"expectedContentRevision": 1, "note": "Checked against the fictional KNT source"}
        api.call("accountant", "POST", path, json=review)
        admin_call(api, "POST", path + "/decision", {**review, "decision": "approved"})
        record = admin_call(api, "POST", f"/api/records/from-document/{document_id}", {})
        records[doc.number] = record["id"]
        if doc.kind in {"bill", "invoice"}:
            admin_call(api, "POST", f"/api/erp/scanned/{record['id']}/post",
                       {"accountId": accounts[doc.account]["id"]})
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
            "idempotencyKey": f"knt:purchase:{doc.number}", "description": f"{doc.number} {doc.description}",
            "lines": [{"accountId": accounts[account]["id"], "credit" if is_credit else "debit": money(amount)}
                      for account, amount, is_credit in lines],
        })
        if doc.related:
            bank_journals[doc.number] = journal["id"]
    return records, bank_journals


def import_and_match_bank(api, connect, tenant, folder, pack, bank_lines, bank_account, accounts, sales, purchases, note_journals):
    from services.routes.accounting.routes import mark_bank_line_matched

    bank_ref = COMPANY["bank_account_number"]
    for month, statement in pack["bank"].items():
        file = folder / statement["file"]
        imported = api.call("accountant", "POST", "/api/bank/imports",
                            files={"file": (file.name, file.read_bytes(), "text/csv")},
                            data={"source_bank_account_ref": bank_ref, "mapping": json.dumps(BANK_MAPPING),
                                  "date_format": "iso", "default_currency": "MYR"}).json()
        api.call("accountant", "POST", f"/api/bank-imports/{imported['importId']}/confirm")
    with connect() as db:
        rows = {row["reference"]: row for row in db.execute(
            "SELECT * FROM bank_transactions WHERE tenant_id=%s", (tenant,)).fetchall()}
    suspense = 0
    for month, cutoff in MONTHS.items():
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
                if line.kind == "customer":
                    target = sales[line.targets[0][0]]
                    payment = admin_call(api, "POST", f"{ERP}/payment", {
                        "number": line.reference, "contactId": target["contactId"], "documentDate": str(line.posted),
                        "status": "ready", "referenceNo": line.reference, "amount": money(line.amount),
                        "bankAccountId": bank_account, "allocations": [
                            {"targetId": sales[number]["id"], "source": "typed", "amount": money(amount)}
                            for number, amount in line.targets],
                    })
                    document_id, journal_id = payment["id"], payment["journalId"]
                else:
                    journal_id = note_journals[line.reference]
                # The bank UI matches scanned invoices or creates a new money entry.
                # Link already-posted fixture evidence without recording a second payment.
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
        evidence = "Unidentified statement transfers awaiting remittance details"
        admin_call(api, "POST", PERIODS, {
            "bankAccountRef": bank_ref, "startsOn": str(cutoff.replace(day=1)), "endsOn": str(cutoff),
            "openingBalance": pack["bank"][month]["opening"], "closingBalance": pack["bank"][month]["closing"],
            "bookBalance": money(balance), "statementAdjustment": money(-suspense), "adjustmentEvidence": evidence,
        })


def contact_body(code, accounts):
    kind, name, account, address = CONTACTS[code]
    head = contact_header(code)
    return {"kind": kind, "code": code, "legalName": name, "registrationNumber": head.ssm, "address": address,
            "city": head.city, "state": "Sarawak", "postcode": head.postcode, "phone": head.phone,
            "email": f"accounts@{code.lower()}.knt.test",
            "defaultExpenseAccountId": accounts[account]["id"] if kind == "supplier" else None,
            "fields": {"tin": head.tin, "sstNumber": head.sst, "paymentTermDays": head.terms,
                       "defaultAccountCode": account, "currency": "MYR", "category": head.category}}


def create_contacts(api, accounts, existing=None, fill_blanks=False):
    """Add every KNT contact. `existing` ({code: contact}) are kept as they are; with fill_blanks a contact that is
    already there only gets the header values it does not have yet (nothing it already holds is overwritten)."""
    contacts = {}
    for code in CONTACTS:
        body = contact_body(code, accounts)
        current = (existing or {}).get(code)
        if current is None:
            contacts[code] = admin_call(api, "POST", "/api/erp/contacts", body)["id"]
            continue
        contacts[code] = current["id"]
        if not fill_blanks:
            continue
        merged = {key: current.get(key) or body[key] for key in
                  ("registrationNumber", "address", "city", "state", "postcode", "phone", "email")}
        fields = {**body["fields"], **(current.get("fields") or {})}
        extra = {"defaultExpenseAccountId": current.get("defaultExpenseAccountId") or body["defaultExpenseAccountId"]}
        if (merged, fields, extra["defaultExpenseAccountId"]) != (
                {key: current.get(key) for key in merged}, current.get("fields") or {},
                current.get("defaultExpenseAccountId")):
            admin_call(api, "PUT", f"/api/erp/contacts/{current['id']}", {
                "kind": body["kind"], "code": current.get("code") or code, "legalName": current["legalName"],
                **merged, "fields": fields, **(extra if body["kind"] == "supplier" else {})})
    return contacts


def setup_catalogue(api, accounts):
    """Tax-code labels, payment terms, a cash-in-hand account and the product catalogue (idempotent by label/SKU)."""
    def entries(kind):
        return {row["code"]: row for row in api.call("local_admin", "GET", f"/api/erp/catalog/{kind}",
                                                     params={"includeInactive": "true"}).json()}

    for kind, rows in (("tax-code", {code: (desc, {"ratePercent": f"{rate * 100:.0f}"})
                                     for code, (desc, rate) in TAX_CODES.items()}),
                       ("payment-term", {code: (name, {"days": days}) for code, (name, days) in PAYMENT_TERMS.items()})):
        have = entries(kind)
        for code, (name, data) in rows.items():
            if code not in have:
                admin_call(api, "POST", f"/api/erp/catalog/{kind}", {"code": code, "name": name, "data": data})
    have = entries("bank-account")
    cash = have.get(CASH_ACCOUNT["code"]) or admin_call(api, "POST", "/api/erp/catalog/bank-account", {
        "code": CASH_ACCOUNT["code"], "name": CASH_ACCOUNT["name"],
        "ledgerAccountId": accounts[CASH_ACCOUNT["ledger"]]["id"], "data": {"bankName": "Cash"}})
    existing = {row["sku"]: row for row in api.call("local_admin", "GET", "/api/erp/products",
                                                    params={"includeInactive": "true"}).json()}
    products = {}
    for product in PRODUCTS:
        products[product.sku] = existing.get(product.sku) or admin_call(api, "POST", "/api/erp/products", {
            "name": product.name, "sku": product.sku, "kind": product.kind, "unit": product.unit,
            "salePrice": money(product.sale), "purchasePrice": money(product.cost), "quantity": str(product.stock),
            "groupName": product.group, "incomeAccountId": accounts[product.income]["id"],
            "expenseAccountId": accounts[product.expense]["id"], "defaultTaxCode": product.tax_code,
        })
    return cash["id"], products


def seed(connect, folder, password=None):
    from fastapi.testclient import TestClient
    import main

    main.check_schema_compatibility()
    folder = Path(folder)
    pack = generate(folder)
    with connect() as db:
        tenant = find_company(db)
        if tenant and db.execute("SELECT 1 FROM accounting_periods WHERE tenant_id=%s LIMIT 1", (tenant["id"],)).fetchone():
            raise SeedError("KNT already has books. Run tally to check them or reset-knt to rebuild them.")
        if tenant and db.execute("SELECT 1 FROM documents WHERE tenant_id=%s LIMIT 1", (tenant["id"],)).fetchone():
            raise SeedError("KNT already has Inbox documents. Run reset-knt to rebuild its demo books.")
        tenant, users, credentials = setup_company(db, tenant, password)
    for email, chosen in credentials.items():
        print(f"New local demo login {email}  {chosen}", flush=True)
    tokens, sessions = issue_tokens(users)
    try:
        # Skip application lifespan so seeding cannot start a general-purpose extraction worker.
        with closing(TestClient(main.app, base_url="http://127.0.0.1")) as client:
            api = Api(client, tokens)
            _create_periods(api, MONTHS)
            import_csv(api, "/api/chart-of-accounts/import", folder / "coa.csv", "chart of accounts")
            # Reuse the existing bank catalogue setup and ledger account lookup.
            setup = Seeder(api, "trading", MONTHS, None)
            accounts = setup.accounts()
            bank_account = setup.ensure_bank_account(accounts, COMPANY["bank_account_number"],
                                                         label=KNT_BANK_LABEL, bank_name="Public Bank Berhad")
            periods = {p["periodCode"]: p["id"] for p in api.call("local_admin", "GET", "/api/ledger/periods").json()}
            cash_account, products = setup_catalogue(api, accounts)
            contacts = create_contacts(api, accounts)
            admin_call(api, "POST", "/api/ledger/journals", {
                "periodId": periods["2026-09"], "sourceType": "manual", "idempotencyKey": "knt:opening",
                "journalDate": "2026-09-01", "description": "KNT opening funds",
                "lines": [{"accountId": accounts[code]["id"], side: money(amount)} for code, side, amount in
                          (("1010", "debit", OPENING_BANK), ("1000", "debit", OPENING_CASH),
                           ("3000", "credit", OPENING_BANK + OPENING_CASH))],
            })
            sales, purchases, bank = books()
            typed = typed_sales(api, sales, contacts, accounts, products)
            quotes, cash, bills = extras()
            typed_quotes(api, quotes, contacts, accounts, products)
            typed_supplier_bills = typed_bills(api, bills, contacts, accounts, products)
            scanned, note_journals = capture_purchases(api, connect, users["accountant"], purchases, folder, accounts, periods)
            import_and_match_bank(api, connect, tenant["id"], folder, pack, bank, bank_account, accounts,
                                  typed, scanned, note_journals)
            typed_cash(api, cash, [*purchases, *bills], contacts, typed, scanned, typed_supplier_bills, cash_account)
    finally:
        with connect() as db:
            db.execute("DELETE FROM web_sessions WHERE session_id=ANY(%s)", (sessions,))
