"""Company books for team testing: one interface over KNT and the three fictional SMEs in `sme_profiles`.

    python -m ai.synthetic.sme_books --company TKK [--out DIRECTORY]

`books_for(code)` returns an object the generic seed (`services/admin/company_seed.py`) and tally
(`services/admin/company_tally.py`) read: identity, contacts, catalogue, the fixed Sep-Nov 2026 scenario
(`books()` and `extras()`, same shapes as `knt_pack`) and `generate(folder)`, which writes the Inbox source files,
chart of accounts, contacts and bank statements. KNT keeps its own fixture (`KntBooks` only adapts it); TKK, EHB and
CDA are generated from their profile with a fixed seed, so the same code always gives the same books and bytes.

Tax is never calculated by the application: the scenario fixes each printed tax amount once (net x the printed
rate, rounded to the sen) and the seed types that amount.
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cached_property
from pathlib import Path

from ai.synthetic.company_pack import COA, COA_COLUMNS, CONTACT_COLUMNS, DEFAULT_OUT, NOTICE, ZERO
from ai.synthetic.knt_extra import ContactDetail, Quote
from ai.synthetic.knt_pack import MONTHS, TITLES, BankLine, Document, money, write_csv
from ai.synthetic.sme_profiles import PROFILES, Item, Profile

CENT = Decimal("0.01")
END = list(MONTHS.values())[-1]
SIGNED_MAPPING = {"posted_date": "Date", "description": "Description", "reference": "Reference",
                  "debit": "Amount", "credit": "Amount"}
DEBIT_CREDIT_MAPPING = {"posted_date": "Transaction Date", "description": "Description", "reference": "Reference",
                        "debit": "Debit", "credit": "Credit"}


def cents(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PLine:
    """A document line with the same attributes as `knt_extra.Line`, for any company's catalogue."""
    item: Item | None
    quantity: Decimal
    price: Decimal
    rate: Decimal
    discount: Decimal = ZERO
    account: str | None = None
    note: str | None = None
    code: str | None = None

    @property
    def sku(self):
        return self.item.sku if self.item else None

    @property
    def product(self):
        return self.item

    @property
    def net(self):
        return cents(self.quantity * self.price) - self.discount

    @property
    def tax_code(self):
        return self.code or self.item.tax_code

    @property
    def tax(self):
        return cents(self.net * self.rate)

    @property
    def account_code(self):
        return self.account or self.item.income

    @property
    def description(self):
        return self.note or self.item.name


# --------------------------------------------------------------------------- KNT, unchanged
class KntBooks:
    """The existing KNT fixture behind the common interface. Nothing here changes what KNT generates."""

    def __init__(self):
        from ai.synthetic import knt_extra, knt_pack

        self._pack, self._extra = knt_pack, knt_extra
        self.code = "KNT"
        self.company = knt_pack.COMPANY
        self.contacts = knt_pack.CONTACTS
        self.months = knt_pack.MONTHS
        self.opening_bank, self.opening_cash = knt_pack.OPENING_BANK, knt_pack.OPENING_CASH
        self.tax_codes, self.payment_terms = knt_extra.TAX_CODES, knt_extra.PAYMENT_TERMS
        self.cash_account, self.products = knt_extra.CASH_ACCOUNT, knt_extra.PRODUCTS
        self.bank_mapping, self.bank_date_format = knt_pack.BANK_MAPPING, "iso"
        self.bank_label = "Public Bank Current Account ••" + self.company["bank_account_number"][-4:]
        self.bank_name = "Public Bank Berhad"
        self.emails = {"local_admin": "admin@knt.demo.test", "accountant": "accountant@knt.demo.test"}
        self.key_prefix = "knt"
        self.lock_key = 2026112305
        self.coa = COA
        self.legacy_admin_email = "admin@trading.demo.test"
        self.close_months = ()  # KNT's periods stay open, as on uat today
        self.name = self.company["name"]

    def contact_header(self, code):
        return self._pack.contact_header(code)

    def contact_state(self, code):
        return "Sarawak"

    def contact_email(self, code):
        return f"accounts@{code.lower()}.knt.test"

    def books(self, include_extension=True):
        return self._pack.books(include_extension)

    def extras(self):
        return self._pack.extras()

    def generate(self, folder):
        return self._pack.generate(folder)


# --------------------------------------------------------------------------- TKK, EHB, CDA
class SmeBooks:
    """Books generated from a `sme_profiles.Profile`. See the module docstring."""

    def __init__(self, profile: Profile, lock_key: int):
        p = self.profile = profile
        self.code, self.name = p.code, p.name
        self.company = {"code": p.code, "name": p.name, "ssm": p.ssm, "tin": p.tin, "sst": p.sst,
                        "bank_account_number": p.bank_account_number, "address": p.address, "fictional": True}
        self.parties = {party.code: party for party in p.parties}
        self.contacts = {party.code: (party.kind, party.name, party.account, party.street) for party in p.parties}
        self.months = MONTHS
        self.opening_bank, self.opening_cash = p.opening_bank, p.opening_cash
        self.tax_codes, self.payment_terms = p.tax_codes, p.payment_terms
        self.cash_account = {"name": "Cash in hand (counter and petty cash)", "code": "CA-1000", "ledger": "1000"}
        self.products = p.items
        self.items = {item.sku: item for item in p.items}
        if p.bank_layout == "debit_credit":
            self.bank_mapping, self.bank_date_format = DEBIT_CREDIT_MAPPING, "dmy"
        else:
            self.bank_mapping, self.bank_date_format = SIGNED_MAPPING, "iso"
        self.bank_label, self.bank_name = p.bank_label, p.bank_name
        lower = p.code.lower()
        self.emails = {"local_admin": f"admin@{lower}.demo.test", "accountant": f"accountant@{lower}.demo.test"}
        self.key_prefix = lower
        self.lock_key = lock_key
        self.coa = COA
        self.legacy_admin_email = None
        self.close_months = ("2026-09",)  # September is closed (locked) so period closing can be tested

    # ---------------------------------------------------------------- contacts
    def contact_header(self, code):
        party = self.parties[code]
        customer = party.kind == "customer"
        position = [c for c, each in self.parties.items() if each.kind == party.kind].index(code) + 1
        side = "1" if customer else "2"
        tin, ssm, sst = self.profile.series
        return ContactDetail(ssm=f"{ssm}{side}{position:04d}"[:12], tin=f"C{tin}{side}{position:04d}",
                             sst=f"W10-2611-{sst}{side}{position:03d}",
                             phone=f"{self.profile.phone_area}-{5 if customer else 6}{position:05d}",
                             terms=party.terms, postcode=party.postcode, city=party.city, category=party.category)

    def contact_state(self, code):
        city = self.parties[code].city
        return "Sabah" if city in {"Kota Kinabalu", "Penampang", "Tuaran", "Putatan"} else "Sarawak"

    def contact_email(self, code):
        return f"accounts@{code.lower()}.{self.code.lower()}.test"

    # ---------------------------------------------------------------- scenario
    def line(self, sku, quantity, price=None, *, account=None, note=None, code=None):
        item = self.items[sku] if sku else None
        tax_code = code or item.tax_code
        return PLine(item, Decimal(str(quantity)), Decimal(str(price)) if price is not None else item.sale,
                     self.tax_codes[tax_code][1], account=account, note=note, code=code)

    def _quantity(self, item, rng):
        if item.unit in {"pax", "box"}:
            return rng.choice((20, 25, 30, 40, 50, 60))
        if item.unit in {"bag"}:
            return rng.choice((20, 40, 60, 80))
        if item.unit in {"pc", "sheet", "length"}:
            return rng.choice((10, 12, 20, 24, 30))
        if item.unit in {"month", "job", "run", "person", "item"}:
            return 1
        return rng.choice((1, 2, 3, 4))

    def _document(self, number, kind, contact, issued, description, lines, *, related=None, quote=None,
                  quoted=None, flow=(), state="posted"):
        terms = self.parties[contact].terms
        due = None if kind == "credit-note" else issued + timedelta(days=terms)
        subtotal = sum((line.net for line in lines), ZERO)
        tax = sum((line.tax for line in lines), ZERO)
        return Document(number, kind, contact, issued, due, description, subtotal, tax, lines[0].account_code,
                        related, state=state, quote=quote, lines=tuple(lines), flow=tuple(flow), quoted=quoted,
                        valid_until=(quoted + timedelta(days=14)) if quoted else None)

    def _purchase(self, number, kind, source, issued, net, *, related=None, state="posted", account=None,
                  description=None, due_days=None):
        party = self.parties[source.supplier]
        terms = party.terms or 30
        due = None if kind in {"receipt", "credit_note", "debit_note"} else issued + timedelta(days=due_days or terms)
        return Document(number, kind, source.supplier, issued, due, description or source.description, net,
                        cents(net * source.rate), account or source.account, related, state=state)

    @cached_property
    def _scenario(self):
        p = self.profile
        rng = random.Random(p.seed)
        customers = [party for party in p.parties if party.kind == "customer" and party.code not in p.cash_customers]
        items = list(p.items)
        cursor = [0]

        def take(count):
            chosen = []
            for _ in range(count):
                item = items[cursor[0] % len(items)]
                cursor[0] += 1
                chosen.append(self.line(item.sku, self._quantity(item, rng)))
            return chosen

        def describe(lines):
            groups = sorted({line.item.group for line in lines if line.item})
            return " and ".join(groups) if len(groups) < 3 else ", ".join(groups[:-1]) + " and " + groups[-1]

        sales, quotes = [], []
        per = p.invoices_per_month
        for index, (month, cutoff) in enumerate(self.months.items()):
            tag = cutoff.strftime("%y%m")
            span = cutoff.day - 6
            days = sorted({2 + k * span // per for k in range(per)})
            for n, day in enumerate(days, 1):
                customer = customers[(index * per + n - 1) % len(customers)]
                issued = cutoff.replace(day=day)
                lines = take(3 if n % 3 == 0 else 2)
                chain = n == 1
                sales.append(self._document(f"SI-{tag}-{n:02d}", "invoice", customer.code, issued, describe(lines),
                                            lines, quote=f"QT-{tag}-{n:02d}" if chain else None,
                                            quoted=issued - timedelta(days=7) if chain else None,
                                            flow=p.flow if chain else ()))
            for code in p.cash_customers:
                for n, day in enumerate((10, cutoff.day - 3), 81):
                    lines = take(1)
                    sales.append(self._document(f"CS-{tag}-{n:02d}", "invoice", code, cutoff.replace(day=day),
                                                "Counter order " + describe(lines), lines))
        by_number = {doc.number: doc for doc in sales}

        def credit(number, on, related, quarter=4):
            base = by_number[related].lines[0]
            quantity = max(Decimal(1), (base.quantity / quarter).to_integral_value())
            note = PLine(base.item, quantity, base.price, base.rate, account="4090",
                         note=f"Credit: {base.description} not taken up")
            return self._document(number, "credit-note", by_number[related].contact, on,
                                  f"Credit note for {related}", [note], related=related)

        notes = [credit("CN-2609-01", by_number["SI-2609-02"].issued + timedelta(days=3), "SI-2609-02"),
                 credit("CN-2611-01", by_number["SI-2611-03"].issued + timedelta(days=3), "SI-2611-03")]
        extra = take(1)
        target = by_number["SI-2610-03"]
        notes.append(self._document("DN-2610-01", "debit-note", target.contact, target.issued + timedelta(days=4),
                                    "Additional " + extra[0].description.lower(), extra, related=target.number))
        sales = sorted(sales + notes, key=lambda doc: (doc.issued, doc.number))

        # Quotations still open, expired or accepted into an order (not invoiced yet).
        open_customers = customers[1:4]
        quotes.append(Quote("QT-2610-90", open_customers[0].code, date(2026, 10, 8), date(2026, 10, 22),
                            "Quotation not taken up", tuple(take(2)), "expired"))
        quotes.append(Quote("QT-2611-90", open_customers[1].code, date(2026, 11, 17), date(2026, 12, 1),
                            "Quotation awaiting the customer's answer", tuple(take(2)), "open"))
        if p.flow:
            quotes.append(Quote("QT-2611-91", open_customers[2].code, date(2026, 11, 19), date(2026, 12, 3),
                                "Accepted; order confirmed, delivery next week", tuple(take(2)), "accepted",
                                via=p.flow[:1]))
        used = {line.sku for doc in sales for line in doc.lines} | {line.sku for q in quotes for line in q.lines}
        missing = sorted(set(self.items) - used)
        if missing:
            raise ValueError(f"{p.code}: products never used on a document: {missing}")

        # Inbox purchases: a supplier invoice, an electricity bill and a cash receipt each month,
        # the October credit and debit notes on that month's invoice, and three November invoices left in review.
        purchases = []
        for index, (month, cutoff) in enumerate(self.months.items()):
            tag = cutoff.strftime("%y%m")
            on = cutoff.replace
            purchases.append(self._purchase(f"PI-{tag}-01", "invoice", p.stock_inbox, on(day=6), p.stock_inbox.nets[index]))
            purchases.append(self._purchase(f"UB-{tag}-01", "bill", p.utility_inbox, on(day=9),
                                            p.utility_inbox.nets[index], due_days=14))
            purchases.append(self._purchase(f"RC-{tag}-01", "receipt", p.receipt_inbox, on(day=12),
                                            p.receipt_inbox.nets[index]))
        source = p.stock_inbox
        returns = "5010" if source.account.startswith("5") else source.account
        purchases.append(self._purchase("SC-2610-01", "credit_note", source, date(2026, 10, 11),
                                        cents(source.nets[1] / 10), related="PI-2610-01", account=returns,
                                        description="Returned items not as ordered"))
        purchases.append(self._purchase("SD-2610-01", "debit_note", source, date(2026, 10, 13), Decimal("40.00"),
                                        related="PI-2610-01", description="Additional delivery charge"))
        for number, day, state, text in (("PI-2611-02", 18, "to_review", "awaiting delivery confirmation"),
                                         ("PI-2611-03", 19, "ready", "for the store room"),
                                         ("PI-2611-04", 20, "exception", "order without a number")):
            purchases.append(self._purchase(number, "invoice", source, date(2026, 11, day),
                                            cents(source.nets[2] * Decimal("0.3")) + day, state=state,
                                            description=f"{source.description}, {text}"))
        purchases.sort(key=lambda doc: (doc.issued, doc.number))

        # Typed supplier bills: monthly overheads, and the stock sold each month bought from its usual supplier.
        bills, counters = [], {}

        def bill(supplier, tag, issued, description, lines):
            counters[(supplier, tag)] = counters.get((supplier, tag), 0) + 1
            terms = self.parties[supplier].terms
            return Document(f"{supplier}-{tag}-{counters[(supplier, tag)]:02d}", "bill", supplier, issued,
                            issued + timedelta(days=terms), description, sum((line.net for line in lines), ZERO),
                            sum((line.tax for line in lines), ZERO), lines[0].account_code, lines=tuple(lines))

        for index, (month, cutoff) in enumerate(self.months.items()):
            tag = cutoff.strftime("%y%m")
            for overhead in p.overheads:
                if overhead.day > cutoff.day:
                    continue
                lines = [self.line(None, 1, overhead.amount, account=overhead.account, note=overhead.note,
                                   code=overhead.tax_code)]
                bills.append(bill(overhead.supplier, tag, cutoff.replace(day=overhead.day), overhead.note, lines))
            wanted = {}
            for doc in sales:
                if doc.kind in {"invoice", "debit-note"} and f"{doc.issued:%Y-%m}" == month:
                    for line in doc.lines:
                        if line.item and line.item.supplier:
                            per_supplier = wanted.setdefault(line.item.supplier, {})
                            per_supplier[line.sku] = per_supplier.get(line.sku, ZERO) + line.quantity
            for supplier in sorted(wanted):
                lines = []
                for sku, quantity in sorted(wanted[supplier].items()):
                    item = self.items[sku]
                    code = item.tax_code if item.tax_code.startswith("SST") else "SST-0"
                    lines.append(self.line(sku, quantity, item.cost, account=item.expense, code=code))
                bills.append(bill(supplier, tag, cutoff.replace(day=3), f"Stock bought for {cutoff:%B} sales", lines))
        bills.sort(key=lambda doc: (doc.issued, doc.number))

        # Money: customer receipts and supplier payments through the bank, counter sales in cash, a monthly bank
        # charge and one transfer per month that stays unidentified (the reconciliation story).
        left = {doc.number: doc.total for doc in sales if doc.kind in {"invoice", "debit-note"}}
        for doc in sales:
            if doc.kind == "credit-note":
                left[doc.related] -= doc.total
        debit_notes = {doc.related: doc for doc in sales if doc.kind == "debit-note"}
        bank, cash_lines, sequence = [], [], {"OR": 0, "PV": 0}

        def reference(prefix, on):
            sequence[prefix] += 1
            return f"{prefix}-{on:%y%m}-{sequence[prefix]:02d}"

        invoices = [doc for doc in sales if doc.kind == "invoice"]
        for position, doc in enumerate(invoices):
            if doc.contact in p.cash_customers:
                cash_lines.append(BankLine(doc.issued, f"CR-{doc.number}", "Counter takings", doc.total, "cash_in",
                                           ((doc.number, doc.total),)))
                left[doc.number] = ZERO
                continue
            note = debit_notes.get(doc.number)
            pattern = 0 if note else position % 6  # the invoice with a debit note is paid in one transfer with it
            if pattern == 5:
                continue  # left unpaid
            on = doc.due - timedelta(days=rng.randint(0, 3)) if pattern != 4 else doc.due + timedelta(days=12)
            if note:
                on = min(on, END - timedelta(days=1))
            if on > END or on <= doc.issued:
                continue
            amount = left[doc.number] if pattern != 3 else cents(left[doc.number] / 2 / 10) * 10
            targets = [(doc.number, amount)]
            if note and note.issued < on:
                targets.append((note.number, note.total))  # one transfer for the invoice and its debit note
            for number, value in targets:
                left[number] -= value
            total = sum((value for _, value in targets), ZERO)
            short = self.parties[doc.contact].name.split()[0]
            bank.append(BankLine(on, reference("OR", on), f"{short} transfer", total, "customer", tuple(targets)))

        unpaid = {doc.number: doc.total for doc in purchases if doc.kind in {"invoice", "bill"} and doc.state == "posted"}
        for doc in purchases:
            if doc.state != "posted" or doc.kind not in {"invoice", "bill"} or doc.due > END:
                continue
            amount = doc.total if doc.number != "PI-2610-01" else cents(doc.total / 2)
            short = self.parties[doc.contact].name.split()[0]
            bank.append(BankLine(doc.due, reference("PV", doc.due), f"{short} payment", -amount, "supplier",
                                 ((doc.number, amount),)))
            unpaid[doc.number] -= amount
        for doc in purchases:
            if doc.kind in {"credit_note", "debit_note"}:
                sign = 1 if doc.kind == "credit_note" else -1
                bank.append(BankLine(doc.issued, doc.number, doc.description, sign * doc.total,
                                     "refund" if sign > 0 else "debit"))
        overhead_paid = {(o.supplier, o.account): o.paid for o in p.overheads}
        last_month = list(self.months)[-1]
        for doc in bills:
            paid = overhead_paid.get((doc.contact, doc.account), f"{doc.issued:%Y-%m}" != last_month)
            if not paid or doc.due > END:
                continue
            short = self.parties[doc.contact].name.split()[0]
            bank.append(BankLine(doc.due, reference("PV", doc.due), f"{short} payment", -doc.total, "bill_payment",
                                 ((doc.number, doc.total),)))
        for index, (month, cutoff) in enumerate(self.months.items()):
            tag = cutoff.strftime("%y%m")
            bank.append(BankLine(cutoff.replace(day=min(25, cutoff.day - 1)), f"FEE-{tag}", "Bank service charge",
                                 Decimal("-10.00"), "fee"))
            amount = (Decimal("150.00"), Decimal("-45.00"), Decimal("60.00"))[index]
            bank.append(BankLine(cutoff, f"UNK-{tag}", "Transfer pending identification", amount, "unmatched"))
        bank.sort(key=lambda line: (line.posted, line.reference))
        balance = self.opening_bank
        for line in bank:
            balance += line.amount
            if balance < 0:
                raise ValueError(f"{p.code}: the bank balance goes below zero at {line.reference}")
        cash = sorted(cash_lines, key=lambda line: (line.posted, line.reference))
        spent = sum((doc.total for doc in purchases if doc.kind == "receipt"), ZERO)
        if self.opening_cash + sum((line.amount for line in cash), ZERO) - spent < 0:
            raise ValueError(f"{p.code}: cash in hand goes below zero")
        return sales, purchases, bank, quotes, cash, bills

    def books(self, include_extension=True):
        sales, purchases, bank, *_rest = self._scenario
        return list(sales), list(purchases), list(bank)

    def extras(self):
        _s, _p, _b, quotes, cash, bills = self._scenario
        return list(quotes), list(cash), list(bills)

    # ---------------------------------------------------------------- files
    def purchase_page(self, doc):
        from ai.synthetic.generate import Page

        party = self.parties[doc.contact]
        supplier_address = f"{party.street}, {party.postcode} {party.city}, {self.contact_state(doc.contact)}"
        kind = doc.kind
        page = Page(595, 842)
        title = TITLES[kind]
        page.text(40, 40, title.upper(), 18, bold=True)
        for y, value in enumerate((party.name, supplier_address, "", "Bill to:", self.name, self.profile.address)):
            page.text(40, 80 + y * 16, value, 11)
        page.text(40, 190, f"Document no. {doc.number if doc.state != 'exception' else ''}", 11)
        page.text(40, 209, f"Issued {doc.issued}", 11)
        if doc.due:
            page.text(40, 228, f"Due {doc.due}", 11)
        if doc.related:
            page.text(40, 228, f"Original invoice {doc.related}", 11)
        if kind == "receipt":
            page.text(40, 228, "Paid in cash at 10:15:00", 11)
        page.line(40, 555, 256)
        page.text(40, 266, "Description", 11, bold=True)
        page.text(391, 266, "Qty       Unit (MYR)       Amount (MYR)", 9)
        page.text(40, 287, doc.description[:60], 11)
        page.text(391, 287, f"1          {money(doc.subtotal):>9}           {money(doc.subtotal):>9}", 10)
        page.line(40, 555, 310)
        for y, (label, value) in enumerate((("Subtotal", doc.subtotal), ("Tax", doc.tax), ("Total", doc.total))):
            page.text(350, 325 + y * 18, label, 11, bold=y == 2)
            page.text(555, 325 + y * 18, f"MYR {money(value)}", 11, right=True, bold=y == 2)
        if kind == "bill":
            page.text(350, 383, f"Amount due MYR {money(doc.total)}", 11)
        page.text(40, 435, NOTICE, 9)
        labels = {"customer_name": self.name, "currency": "MYR",
                  **{field: (money(value), f"MYR {money(value)}") for field, value in
                     (("subtotal", doc.subtotal), ("tax", doc.tax), ("total", doc.total))}}
        if kind == "bill":
            labels.update(bill_number=doc.number, provider_name=party.name, provider_address=supplier_address,
                          customer_address=self.profile.address, bill_date=doc.issued.isoformat(),
                          due_date=doc.due.isoformat(), amount_due=labels["total"])
        elif kind == "receipt":
            labels.update(receipt_number=doc.number, merchant_name=party.name, merchant_address=supplier_address,
                          transaction_date=doc.issued.isoformat(), transaction_time="10:15:00", payment_method="cash")
        else:
            labels.update(invoice_number=doc.number, supplier_name=party.name, supplier_address=supplier_address,
                          customer_address=self.profile.address, invoice_date=doc.issued.isoformat(),
                          invoice_type=title)
            if doc.due:
                labels["due_date"] = doc.due.isoformat()
            if doc.related:
                labels.update(original_invoice_number=doc.related, note_reason=doc.description,
                              **{f"{kind}_number": doc.number})
        return page, labels

    def statement_name(self, month, cutoff):
        return f"{cutoff}_{self.code}_{self.profile.bank_short.upper()}_STMT_{month}.csv"

    def generate(self, folder):
        from ai.synthetic.generate import Generator, write_manifests

        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        _, purchases, bank = self.books()
        renderer = Generator("holdout")
        renderer.dataset = f"{self.key_prefix}-demo-books"
        renderer.set_name = self.key_prefix
        renderer.split = "development"
        renderer.revision = f"{self.key_prefix}-1"
        renderer.source_file = "ai/synthetic/sme_books.py"
        manifests = {"invoice": [], "bill": [], "receipt": []}
        for doc in purchases:
            directory = f"documents/{doc.issued:%Y-%m}"
            (folder / directory).mkdir(parents=True, exist_ok=True)
            page, labels = self.purchase_page(doc)
            items = [{"description": doc.description, "quantity": Decimal(1),
                      "unit_price": doc.subtotal, "line_total": doc.subtotal}]
            kind, sample = renderer.package(Path(doc.filename).stem, doc.kind, page, labels, items,
                                            "Electricity" if doc.kind == "bill" else None,
                                            folder, doc.kind == "receipt", directory)
            manifests[kind].append(sample)
        write_manifests(folder, renderer.dataset, renderer.revision, manifests)
        write_csv(folder / "coa.csv", COA_COLUMNS, self.coa)
        rows = []
        for code, (kind, name, account, street) in self.contacts.items():
            head = self.contact_header(code)
            rows.append((code, kind, name, "customer" if kind == "customer" else head.category, head.ssm, head.tin,
                         head.sst, f"{street}, {head.postcode} {head.city}, {self.contact_state(code)}",
                         self.contact_email(code), head.phone, head.terms, account, "MYR"))
        write_csv(folder / "contacts.csv", CONTACT_COLUMNS, rows)
        opening, statements = self.opening_bank, {}
        for month, cutoff in self.months.items():
            closing, lines = opening, []
            for line in bank:
                if line.posted.strftime("%Y-%m") == month:
                    closing += line.amount
                    if self.bank_date_format == "dmy":
                        out, into = (money(-line.amount), "") if line.amount < 0 else ("", money(line.amount))
                        lines.append((f"{line.posted:%d/%m/%Y}", line.description, line.reference, out, into,
                                      money(closing)))
                    else:
                        lines.append((line.posted, line.description, line.reference, money(line.amount),
                                      money(closing)))
            name = self.statement_name(month, cutoff)
            headings = (("Transaction Date", "Description", "Reference", "Debit", "Credit", "Balance")
                        if self.bank_date_format == "dmy" else ("Date", "Description", "Reference", "Amount", "Balance"))
            write_csv(folder / name, headings, lines)
            statements[month] = {"file": name, "opening": money(opening), "closing": money(closing)}
            opening = closing
        pack = {"notice": NOTICE, "company": self.company, "bank_name": self.bank_name, "currency": "MYR",
                "as_of": END.isoformat(), "bank": statements}
        (folder / "pack.json").write_text(json.dumps(pack, indent=2) + "\n", encoding="utf-8")
        return pack


_LOCKS = {"TKK": 2026112401, "EHB": 2026112402, "CDA": 2026112403}
COMPANY_CODES = ("KNT", "TKK", "EHB", "CDA")
_CACHE = {}


def books_for(code: str):
    """The books object for a company code (KNT, TKK, EHB or CDA)."""
    code = code.strip().upper()
    if code not in COMPANY_CODES:
        raise KeyError(f"Unknown company {code!r}; choose one of {', '.join(COMPANY_CODES)}.")
    if code not in _CACHE:
        _CACHE[code] = KntBooks() if code == "KNT" else SmeBooks(PROFILES[code], _LOCKS[code])
    return _CACHE[code]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", required=True, choices=COMPANY_CODES)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    target = books_for(args.company)
    print(target.generate(args.out or DEFAULT_OUT / args.company.lower())["company"]["name"])
