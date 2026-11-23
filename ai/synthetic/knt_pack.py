"""Generate the fictional KNT books through 23 November 2026.

Run python -m ai.synthetic.knt_pack [--out DIRECTORY].
The typed Sales data stays in Python and is never rendered as source files.
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from ai.synthetic.company_pack import COA, COA_COLUMNS, CONTACT_COLUMNS, DEFAULT_OUT, NOTICE, ZERO
from ai.synthetic.generate import Generator, Page, write_manifests
from ai.synthetic.knt_extra import EXTRA_CONTACTS, contact_details

COMPANY = {
    "code": "KNT", "name": "Ketapang Nibong Trading Sdn. Bhd.", "ssm": "2021034769157",
    "tin": "C9990001901", "sst": "Q10-2611-99900001", "bank_account_number": "000026119901",
    "address": "Lot 99, Jalan Nibong Contoh, 93050 Kuching, Sarawak", "fictional": True,
}
MONTHS = {"2026-09": date(2026, 9, 30), "2026-10": date(2026, 10, 31), "2026-11": date(2026, 11, 23)}
OPENING_BANK = Decimal("18000.00")
OPENING_CASH = Decimal("500.00")
CONTACTS = {
    "SEN": ("customer", "Senduduk Fit-Out Sdn. Bhd.", "4000", "Lot 12, Jalan Senduduk Contoh"),
    "KU": ("customer", "Kuini Office Works Sdn. Bhd.", "4000", "Lot 23, Jalan Kuini Contoh"),
    "PAU": ("supplier", "Pauh Builders Supply Sdn. Bhd.", "5000", "Lot 34, Jalan Pauh Contoh"),
    "TUA": ("supplier", "Tualang Power Services Sdn. Bhd.", "6110", "Lot 45, Jalan Tualang Contoh"),
    "REN": ("supplier", "Rengas Stationers", "6140", "Lot 56, Jalan Rengas Contoh"),
}
BASE_CONTACTS = tuple(CONTACTS)
CONTACTS.update(EXTRA_CONTACTS)  # the original five keep their codes; the extension adds the rest


def contact_header(code):
    """Header data for a contact: SSM, TIN, SST number, phone, terms, address parts. Customers and suppliers are
    numbered separately in the order they appear in CONTACTS."""
    kind = CONTACTS[code][0]
    position = [c for c, row in CONTACTS.items() if row[0] == kind].index(code) + 1
    return contact_details(code, kind, position)
TITLES = {"invoice": "Invoice", "bill": "Electricity Bill", "receipt": "Cash Receipt",
          "credit_note": "Credit Note", "debit_note": "Debit Note"}
FILE_TYPES = {"invoice": "INV", "bill": "BILL", "receipt": "REC", "credit_note": "CN", "debit_note": "DN"}
BANK_MAPPING = {"posted_date": "Date", "description": "Description", "reference": "Reference",
                "debit": "Amount", "credit": "Amount"}


@dataclass(frozen=True)
class Document:
    number: str
    kind: str
    contact: str
    issued: date
    due: date | None
    description: str
    subtotal: Decimal
    tax: Decimal
    account: str
    related: str | None = None
    state: str = "posted"
    quote: str | None = None
    # Product lines (knt_extra.Line); empty for the original one-line documents. quoted/valid_until are the
    # quotation's own dates and flow the sales order / delivery order it passed through before this invoice.
    lines: tuple = ()
    flow: tuple = ()
    quoted: date | None = None
    valid_until: date | None = None

    @property
    def total(self):
        return self.subtotal + self.tax

    @property
    def amounts(self):
        """Net amount per ledger account: one account for the original documents, the lines' accounts otherwise."""
        if not self.lines:
            return {self.account: self.subtotal}
        result = {}
        for line in self.lines:
            result[line.account_code] = result.get(line.account_code, ZERO) + line.net
        return result

    @property
    def filename(self):
        extension = "jpg" if self.kind == "receipt" else "pdf"
        return f"{self.issued}_{self.contact}_{FILE_TYPES[self.kind]}_{self.number}.{extension}"


@dataclass(frozen=True)
class BankLine:
    posted: date
    reference: str
    description: str
    amount: Decimal
    kind: str
    targets: tuple[tuple[str, Decimal], ...] = ()


def books(include_extension=True):
    """The fixed scenario is also the independent source for tally expectations.

    The original fixture comes first; with include_extension the richer typed sales from knt_extra follow it.
    The bank statements never change: the extension's payments go to cash in hand (see extras())."""
    sales, purchases, bank = original_books()
    if include_extension:
        from ai.synthetic.knt_extra import extension

        sales = sales + extension().sales
    return sales, purchases, bank


def extras():
    """The extension's quotations, cash payments (received and made) and typed supplier bills.
    The payments have no bank statement lines; the bills are typed, so they are not Inbox files."""
    from ai.synthetic.knt_extra import extension

    found = extension()
    return found.quotes, found.cash, found.bills


def original_books():
    sales, purchases, bank = [], [], []
    for index, cutoff in enumerate(MONTHS.values()):
        tag = cutoff.strftime("%y%m")
        on = cutoff.replace
        future_due = date(2026, 12, 4) if index == 2 else on(day=25)
        sale_net, sale_tax = (("2600", "260"), ("3250", "325"), ("2850", "285"))[index]
        goods_net, goods_tax = (("1100", "110"), ("1450", "145"), ("1370", "137"))[index]
        power_net, power_tax = (("180", "14.40"), ("206", "16.48"), ("192", "15.36"))[index]
        paid_sale = Document(f"SI-{tag}-01", "invoice", "SEN", on(day=5), on(day=19),
                             "Fittings and electrical supplies", Decimal(sale_net), Decimal(sale_tax),
                             "4000", quote=f"QT-{tag}-01")
        other_sale = Document(f"SI-{tag}-02", "invoice", "KU", on(day=8), on(day=17),
                              "Office fit-out supplies", Decimal("1320"), Decimal("132"), "4000")
        goods = Document(f"PI-{tag}-01", "invoice", "PAU", on(day=6), future_due,
                         "Wholesale fittings and fixings", Decimal(goods_net), Decimal(goods_tax), "5000")
        power = Document(f"UB-{tag}-01", "bill", "TUA", on(day=9), on(day=19),
                         "Warehouse electricity", Decimal(power_net), Decimal(power_tax), "6110")
        cash = Document(f"RC-{tag}-01", "receipt", "REN", on(day=12), None,
                        "Packing labels and stationery", Decimal("48.50"), ZERO, "6140")
        sales.extend((paid_sale, other_sale))
        purchases.extend((goods, power, cash))
        if index == 0:
            credit = Document("CN-2609-01", "credit-note", "SEN", on(day=10), None,
                              "Returned fittings", Decimal("180"), Decimal("18"), "4090", paid_sale.number)
            sales.append(credit)
        if index == 1:
            sales.append(Document("DN-2610-01", "debit-note", "KU", on(day=10), on(day=17),
                                  "Additional cable reels delivered", Decimal("160"), Decimal("16"), "4000",
                                  other_sale.number))
            for kind, number, day, description, net, tax, account, bank_kind, sign in (
                ("credit_note", "SC-2610-01", 11, "Returned damaged fittings", "90", "9", "5010", "refund", 1),
                ("debit_note", "SD-2610-01", 13, "Additional cartons supplied", "40", "4", "5000", "debit", -1),
            ):
                note = Document(number, kind, "PAU", on(day=day), None, description, Decimal(net), Decimal(tax),
                                account, goods.number)
                purchases.append(note)
                bank.append(BankLine(note.issued, note.number, description, sign * note.total, bank_kind))
            bank.append(BankLine(on(day=16), f"OR-{tag}-02", "Kuini instalment", Decimal("500"),
                                 "customer", ((other_sale.number, Decimal("500")),)))
        sale_amount = paid_sale.total - (credit.total if index == 0 else ZERO)
        bank.append(BankLine(on(day=15), f"OR-{tag}-01", "Senduduk bank transfer", sale_amount, "customer",
                             ((paid_sale.number, sale_amount),)))
        bank.append(BankLine(on(day=19), f"PV-{tag}-01", "Tualang electricity payment", -power.total, "supplier",
                             ((power.number, power.total),)))
        if index < 2:
            amount = goods.total if index == 0 else Decimal("700")
            bank.append(BankLine(on(day=21), f"PV-{tag}-02", "Pauh supplier transfer", -amount, "supplier",
                                 ((goods.number, amount),)))
        bank.append(BankLine(on(day=22), f"FEE-{tag}", "Bank service charge", Decimal("-8.00"), "fee"))
        other_amount = (Decimal("125"), Decimal("-35"), Decimal("80"))[index]
        bank.append(BankLine(on(day=23), f"UNK-{tag}", "Transfer pending identification", other_amount, "unmatched"))
    purchases.extend((
        Document("PI-2611-02", "invoice", "PAU", date(2026, 11, 18), date(2026, 12, 2),
                 "Fixings pending delivery confirmation", Decimal("390"), Decimal("39"), "5000", state="to_review"),
        Document("PI-2611-03", "invoice", "PAU", date(2026, 11, 19), date(2026, 12, 3),
                 "Warehouse shelf brackets", Decimal("420"), Decimal("42"), "5000", state="ready"),
        Document("PI-2611-04", "invoice", "PAU", date(2026, 11, 20), date(2026, 12, 4),
                 "Fasteners ordered for the warehouse", Decimal("360"), Decimal("36"), "5000", state="exception"),
    ))
    return sales, sorted(purchases, key=lambda doc: doc.issued), sorted(bank, key=lambda line: line.posted)


def money(value):
    return f"{value:.2f}"


def write_csv(path, headings, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headings)
        writer.writerows(rows)


def purchase_page(doc):
    _, supplier, _, address = CONTACTS[doc.contact]
    kind = doc.kind
    page = Page(595, 842)
    title = TITLES[kind]
    page.text(40, 40, title.upper(), 18, bold=True)
    for y, value in enumerate((supplier, address + ", Kuching, Sarawak", "", "Bill to:",
                               COMPANY["name"], COMPANY["address"])):
        page.text(40, 80 + y * 16, value, 11)
    page.text(40, 190, f"Document no. {doc.number}", 11)
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
    page.text(40, 287, doc.description, 11)
    page.text(391, 287, f"1          {money(doc.subtotal):>9}           {money(doc.subtotal):>9}", 10)
    page.line(40, 555, 310)
    for y, (label, value) in enumerate((("Subtotal", doc.subtotal), ("Tax", doc.tax), ("Total", doc.total))):
        page.text(350, 325 + y * 18, label, 11, bold=y == 2)
        page.text(555, 325 + y * 18, f"MYR {money(value)}", 11, right=True, bold=y == 2)
    if kind == "bill":
        page.text(350, 383, f"Amount due MYR {money(doc.total)}", 11)
    page.text(40, 435, NOTICE, 9)
    labels = {"customer_name": COMPANY["name"], "currency": "MYR",
              **{field: (money(value), f"MYR {money(value)}") for field, value in
                 (("subtotal", doc.subtotal), ("tax", doc.tax), ("total", doc.total))}}
    if kind == "bill":
        labels.update(bill_number=doc.number, provider_name=supplier, provider_address=address + ", Kuching, Sarawak",
                      customer_address=COMPANY["address"], bill_date=doc.issued.isoformat(),
                      due_date=doc.due.isoformat(), amount_due=labels["total"])
    elif kind == "receipt":
        labels.update(receipt_number=doc.number, merchant_name=supplier, merchant_address=address + ", Kuching, Sarawak",
                      transaction_date=doc.issued.isoformat(), transaction_time="10:15:00", payment_method="cash")
    else:
        labels.update(invoice_number=doc.number, supplier_name=supplier, supplier_address=address + ", Kuching, Sarawak",
                      customer_address=COMPANY["address"], invoice_date=doc.issued.isoformat(), invoice_type=title)
        if doc.due:
            labels["due_date"] = doc.due.isoformat()
        if doc.related:
            labels.update(original_invoice_number=doc.related, note_reason=doc.description,
                          **{f"{kind}_number": doc.number})
    return page, labels


def generate(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _, purchases, bank = books()
    renderer = Generator("holdout")
    renderer.dataset = "knt-demo-books"
    renderer.set_name = "knt"
    renderer.split = "development"
    renderer.revision = "knt-1"
    renderer.source_file = "ai/synthetic/knt_pack.py"
    manifests = {"invoice": [], "bill": [], "receipt": []}
    for doc in purchases:
        directory = f"documents/{doc.issued:%Y-%m}"
        (folder / directory).mkdir(parents=True, exist_ok=True)
        page, labels = purchase_page(doc)
        items = [{"description": doc.description, "quantity": Decimal(1),
                  "unit_price": doc.subtotal, "line_total": doc.subtotal}]
        kind, sample = renderer.package(Path(doc.filename).stem, doc.kind, page, labels, items,
                                        "Electricity" if doc.kind == "bill" else None,
                                        folder, doc.kind == "receipt", directory)
        manifests[kind].append(sample)
    write_manifests(folder, renderer.dataset, renderer.revision, manifests)
    write_csv(folder / "coa.csv", COA_COLUMNS, COA)
    write_csv(folder / "contacts.csv", CONTACT_COLUMNS, [
        (code, kind, name, "customer" if kind == "customer" else "trade", contact_header(code).ssm, contact_header(code).tin,
         contact_header(code).sst, f"{address}, {contact_header(code).postcode} {contact_header(code).city}, Sarawak",
         f"accounts@{code.lower()}.knt.test", contact_header(code).phone, contact_header(code).terms, account, "MYR")
        for code, (kind, name, account, address) in CONTACTS.items()
    ])
    opening, statements = OPENING_BANK, {}
    for month, cutoff in MONTHS.items():
        closing, rows = opening, []
        for line in bank:
            if line.posted.strftime("%Y-%m") == month:
                closing += line.amount
                rows.append((line.posted, line.description, line.reference, money(line.amount), money(closing)))
        name = f"{cutoff}_KNT_STMT_{month}.csv"
        write_csv(folder / name, ("Date", "Description", "Reference", "Amount", "Balance"), rows)
        statements[month] = {"file": name, "opening": money(opening), "closing": money(closing)}
        opening = closing
    pack = {"notice": NOTICE, "company": COMPANY, "currency": "MYR", "as_of": list(MONTHS.values())[-1].isoformat(),
            "bank": statements}
    (folder / "pack.json").write_text(json.dumps(pack, indent=2) + "\n", encoding="utf-8")
    return pack


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT / "knt")
    print(generate(parser.parse_args().out)["company"]["name"])
