"""Plan L9: typed sales and purchases for a seeded demo company, all posted, so the dashboard shows real numbers.

    python services/admin/manage.py seed-erp-demo --pack trading

Local development databases only (same guard as seed-demo-company, which must have run first: it makes the
company, periods, chart of accounts, contacts and scanned documents). For each pack month this adds typed
sales invoices with customer payments (older months mostly paid, the last month mostly open), bills with
supplier payments, one quotation turned into an invoice and one credit note, then posts every approved
scanned invoice, bill and credit note through the same rule. Everything goes through the real routes as the
demo users. Fixed document numbers make a second run skip what is already there. Synthetic data only.
"""
from __future__ import annotations

import calendar
import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from services.admin.demo_company import PACK_ROOT, PACKS, Api, SeedError, bank_account_label, issue_tokens, pack_folder, refusal_reason

PRODUCTS = {  # trading pack: building and office supplies (synthetic prices, RM)
    "trading": [("Simen Portland 50kg / Portland cement 50kg", "CEM-50", "28.50", "21.00"),
                ("Pasir halus (tan) / Fine sand (tonne)", "SND-T", "95.00", "70.00"),
                ("Bata merah / Red clay bricks (per 1,000)", "BRK-1K", "420.00", "330.00"),
                ("Paip PVC 4 inci / PVC pipe 4 in (6 m)", "PVC-4", "38.00", "27.50"),
                ("Cat dinding 18L / Wall paint 18 L", "PNT-18", "185.00", "140.00"),
                ("Kertas A4 80gsm (kotak) / A4 paper 80 gsm (box)", "A4-BOX", "115.00", "88.00")],
}
SST = Decimal("0.10")  # sales tax on goods in the demo
KUALA_LUMPUR = timezone(timedelta(hours=8), "Asia/Kuala_Lumpur")


def _money(value) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def _month_days(code):
    year, month = (int(part) for part in code.split("-"))
    return year, month, calendar.monthrange(year, month)[1]


class Seeder:
    def __init__(self, api, key, months, rng):
        self.api, self.key, self.months, self.rng = api, key, months, rng
        self.counts = {"created": 0, "skipped": 0, "posted": 0, "scanned_posted": 0, "scanned_skipped": 0}

    # -------------------------------------------------------------- setup
    def ensure_current_period(self, today):
        code = today.strftime("%Y-%m")
        year, month, last_day = _month_days(code)
        start = date(year, month, 1).isoformat()
        end = date(year, month, last_day).isoformat()
        periods = self.api.call("local_admin", "GET", "/api/ledger/periods").json()
        if any(p["periodCode"] == code or (p["startsOn"] <= end and p["endsOn"] >= start) for p in periods):
            return
        self.api.call("local_admin", "POST", "/api/ledger/periods", json={
            "periodCode": code, "startsOn": start, "endsOn": end,
        })

    def accounts(self):
        rows = self.api.call("local_admin", "GET", "/api/ledger/accounts").json()
        return {row["code"]: row for row in rows}

    def ensure_bank_account(self, ledger, account_number, label=None, bank_name="Demo Bank"):
        """The demo bank account. `label` defaults to the statement-import label, which older packs match by name;
        a pack whose statements use the account number (KNT) may pass a realistic label and bank name."""
        if "1010" not in ledger:
            raise SeedError("Ledger account 1010 (bank) is missing; run seed-demo-company first.")
        path = "/api/erp/catalog/bank-account"
        entries = self.api.call("local_admin", "GET", path, params={"includeInactive": "true"}).json()
        name = label or bank_account_label(account_number)
        by_name = {entry["name"]: entry for entry in entries}
        existing = by_name.get(name) or by_name.get(bank_account_label(account_number)) or by_name.get("Current account")
        if existing:
            values = existing.get("data") or {}
            if (existing["name"] != name or values.get("accountNumber") != account_number
                    or (label and values.get("bankName") != bank_name)):
                self.api.call("local_admin", "PUT", f"{path}/{existing['id']}", json={
                    "name": name, "code": existing["code"], "description": existing.get("description"),
                    "ledgerAccountId": existing["ledgerAccountId"], "active": existing["isActive"],
                    "data": {**values, **({"bankName": bank_name} if label else {}), "accountNumber": account_number},
                })
            return existing["id"]
        return self.api.call("local_admin", "POST", path, json={
            "name": name, "code": "CA-1010", "ledgerAccountId": ledger["1010"]["id"],
            "data": {"bankName": bank_name, "accountNumber": account_number}}).json()["id"]

    def ensure_products(self):
        existing = {p["sku"]: p for p in self.api.call("accountant", "GET", "/api/erp/products",
                                                       params={"includeInactive": "true"}).json()}
        products = []
        for name, sku, sale, cost in PRODUCTS.get(self.key, []):
            products.append(existing.get(sku) or self.api.call("accountant", "POST", "/api/erp/products", json={
                "name": name, "sku": sku, "kind": "inventory", "unit": "unit", "salePrice": sale, "purchasePrice": cost}).json())
        if not products:
            raise SeedError(f"No demo products are defined for the {self.key} pack.")
        return products

    def contacts(self, kind):
        rows = self.api.call("accountant", "GET", "/api/erp/contacts", params={"kind": kind}).json()
        if not rows:
            raise SeedError(f"No {kind}s in this company; run seed-demo-company first (it imports the pack's contacts).")
        return rows

    # -------------------------------------------------------------- documents
    def document(self, doc_type, number, body, *, post=True):
        """Create (status ready posts it) unless the fixed number already exists. Returns the document or None."""
        response = self.api.call("local_admin", "POST", f"/api/erp/documents/{doc_type}", expected=(201, 409),
                                 json={**body, "number": number, "status": "ready" if post else "draft",
                                       "fields": {**body.get("fields", {}), "seed": "erp-demo"}})
        if response.status_code == 409:
            detail = response.json().get("detail", "")
            if "already exists" not in detail:
                raise SeedError(f"{doc_type} {number}: {detail}")
            self.counts["skipped"] += 1
            return None
        doc = response.json()
        self.counts["created"] += 1
        self.counts["posted"] += 1 if doc.get("posted") else 0
        return doc

    def lines(self, products, sale=True, count=None):
        chosen = self.rng.sample(products, count or self.rng.randint(1, 3))
        result = []
        for product in chosen:
            quantity = self.rng.randint(2, 40)
            price = Decimal(product["salePrice"] if sale else product["purchasePrice"])
            tax = (price * quantity * SST).quantize(Decimal("0.01"))
            result.append({"itemId": product["id"], "description": product["name"], "quantity": str(quantity),
                           "unitPrice": _money(price), "tax": _money(tax)})
        return result

    def month(self, index, code, customers, suppliers, products, bank):
        year, month, last = _month_days(code)
        tag = f"{year % 100:02d}{month:02d}"
        is_last = index == len(self.months) - 1
        for n in range(1, 6):  # five sales invoices a month
            customer = customers[(index * 5 + n) % len(customers)]
            day = min(last, 2 + n * 5)
            invoice = self.document("invoice", f"IV-{tag}-{n:02d}", {
                "contactId": customer["id"], "documentDate": f"{code}-{day:02d}",
                "dueDate": f"{code}-{min(last, day + 14):02d}", "lines": self.lines(products)})
            if not invoice:
                continue
            if n <= (2 if is_last else 4):  # the last month stays mostly open, so the ageing shows balances
                amount = Decimal(invoice["amount"]) if n != 3 else (Decimal(invoice["amount"]) / 2).quantize(Decimal("0.01"))
                self.document("payment", f"OR-{tag}-{n:02d}", {
                    "contactId": customer["id"], "documentDate": f"{code}-{min(last, day + 7):02d}", "amount": _money(amount),
                    "bankAccountId": bank, "allocations": [{"targetId": invoice["id"], "source": "typed", "amount": _money(amount)}]})
        for n in range(1, 3):  # two typed bills a month (supplier invoices received on paper)
            supplier = suppliers[(index * 2 + n) % len(suppliers)]
            day = min(last, 4 + n * 9)
            bill = self.document("bill", f"BL-{tag}-{n:02d}", {
                "contactId": supplier["id"], "documentDate": f"{code}-{day:02d}", "dueDate": f"{code}-{min(last, day + 20):02d}",
                "referenceNo": f"SUP-{tag}-{n:02d}", "lines": self.lines(products, sale=False, count=2)})
            if bill and not (is_last and n == 2):
                self.document("purchase-payment", f"PV-{tag}-{n:02d}", {
                    "contactId": supplier["id"], "documentDate": f"{code}-{min(last, day + 10):02d}", "amount": bill["amount"],
                    "bankAccountId": bank, "allocations": [{"targetId": bill["id"], "source": "typed", "amount": bill["amount"]}]})
        if index == 0:
            self.quotation_and_credit_note(code, tag, customers[0], products)

    def quotation_and_credit_note(self, code, tag, customer, products):
        quote = self.document("quotation", f"QT-{tag}-01", {
            "contactId": customer["id"], "documentDate": f"{code}-05", "lines": self.lines(products, count=2)})
        if not quote:
            return
        invoice = self.api.call("local_admin", "POST", f"/api/erp/documents/quotation/{quote['id']}/transfer",
                                json={"targetType": "invoice"}).json()
        self.api.call("local_admin", "PATCH", f"/api/erp/documents/invoice/{invoice['id']}",
                      json={"documentDate": f"{code}-08", "dueDate": f"{code}-22", "fields": {"seed": "erp-demo"}})
        posted = self.api.call("local_admin", "POST", f"/api/erp/documents/invoice/{invoice['id']}/transition",
                               json={"status": "ready", "reason": "Quotation accepted"}).json()
        self.counts["created"] += 1
        self.counts["posted"] += 1 if posted.get("posted") else 0
        first = posted["lines"][0]
        quantity = Decimal(first["quantity"])
        credit_line = {**first, "quantity": "1", "tax": _money(Decimal(first.get("tax") or 0) / quantity)}
        credit = self.document("credit-note", f"CN-{tag}-01", {
            "contactId": customer["id"], "documentDate": f"{code}-25", "referenceNo": posted["number"], "lines": [credit_line]},
            post=False)
        if credit:  # allocate the credit to the invoice it corrects, then post
            self.api.call("local_admin", "PATCH", f"/api/erp/documents/credit-note/{credit['id']}", json={
                "allocations": [{"targetId": posted["id"], "source": "typed", "amount": credit["amount"]}]})
            done = self.api.call("local_admin", "POST", f"/api/erp/documents/credit-note/{credit['id']}/transition",
                                 json={"status": "ready", "reason": "Returned goods"}).json()
            self.counts["posted"] += 1 if done.get("posted") else 0

    def post_scanned(self):
        """Post every approved scanned invoice, bill and credit note (collected first, so paging is stable)."""
        for doc_type in ("invoice", "bill", "credit-note", "purchase-credit-note"):
            rows, page = [], 1
            while page:
                listing = self.api.call("local_admin", "GET", f"/api/erp/documents/{doc_type}",
                                        params={"source": "scanned", "status": "approved", "page": page, "pageSize": 100}).json()
                rows.extend(listing["items"])
                page = listing["nextPage"]
            for row in rows:
                response = self.api.call("local_admin", "POST", f"/api/erp/scanned/{row['id']}/post",
                                         expected=(200, 409, 422), json={})
                if response.status_code == 200:
                    self.counts["scanned_posted"] += 0 if response.json().get("idempotent") else 1
                else:
                    self.counts["scanned_skipped"] += 1
                    print(f"  skipped scanned {row['number']}: {response.json().get('detail')}")


def seed(key, pack_root=PACK_ROOT, seed_value=20261121):
    reason = refusal_reason()
    if reason:
        raise SeedError("Refusing to seed: " + reason)
    from fastapi.testclient import TestClient
    import main
    from database.connection import connect
    folder = pack_folder(key, pack_root)
    pack = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
    from ai.synthetic.knt_pack import COMPANY
    if pack["company"]["name"] == COMPANY["name"]:
        raise SeedError("The KNT seed includes typed Sales. Use manage.py seed-knt or reset-knt.")
    months = pack["period"]["months"]
    main.check_schema_compatibility()
    with connect() as db:
        tenant = db.execute("SELECT * FROM tenants WHERE registration_number=%s", (pack["company"]["ssm"],)).fetchone()
        if not tenant:
            raise SeedError(f"{pack['company']['name']} is not in this database; run seed-demo-company --pack {key} first.")
        users = {}
        for role, email in (("local_admin", f"admin@{key}.demo.test"), ("accountant", f"accountant@{key}.demo.test")):
            users[role] = db.execute("SELECT * FROM users WHERE email=%s AND tenant_id=%s", (email, tenant["id"])).fetchone()
            if not users[role]:
                raise SeedError(f"Demo user {email} is missing; run seed-demo-company --pack {key} first.")
    tokens, sessions = issue_tokens(users)
    try:
        with TestClient(main.app, base_url="http://127.0.0.1") as client:
            api = Api(client, tokens)
            seeder = Seeder(api, key, months, random.Random(seed_value))
            periods = {p["periodCode"]: p for p in api.call("local_admin", "GET", "/api/ledger/periods").json()}
            closed = [code for code in months if periods.get(code, {}).get("status") != "open"]
            if closed:
                raise SeedError(f"These months need an open accounting period: {', '.join(closed)}.")
            bank = seeder.ensure_bank_account(seeder.accounts(), pack["company"]["bank_account_number"])
            products = seeder.ensure_products()
            customers, suppliers = seeder.contacts("customer"), seeder.contacts("supplier")
            for index, code in enumerate(months):
                seeder.month(index, code, customers, suppliers, products, bank)
                print(f"  {code}: typed documents done")
            if key == "trading":
                seeder.ensure_current_period(datetime.now(KUALA_LUMPUR).date())
            seeder.post_scanned()
            summary = api.call("local_admin", "GET", "/api/home/summary").json()
    finally:
        with connect() as db:
            db.execute("DELETE FROM web_sessions WHERE session_id = ANY(%s)", (sessions,))
    tiles = summary.get("tiles", {})
    return {"company": pack["company"]["name"], "months": months, **seeder.counts,
            "invoicesOverdue": tiles.get("invoicesOverdue", {}).get("amount"),
            "billsOverdue": tiles.get("billsOverdue", {}).get("amount")}


def add_arguments(parser):
    parser.add_argument("--pack", choices=PACKS, required=True)
    parser.add_argument("--pack-root", default=str(PACK_ROOT), help="folder holding <pack>/pack.json")


def run(args):
    try:
        report = seed(args.pack, args.pack_root)
    except SeedError as error:
        print(str(error), file=sys.stderr)
        return 2
    for key, value in report.items():
        print(f"{key}: {value}")
    return 0
