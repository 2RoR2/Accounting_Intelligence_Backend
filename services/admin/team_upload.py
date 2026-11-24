"""Fresh files for live upload tests, one pack per roster slot (these DO spend AI quota when uploaded).

    python services/admin/manage.py team-upload-pack --roster PRIVATE.json [--out DIR]

Each pack (`<out>/<slot>/`) holds five received documents from that company's own suppliers and a bank CSV in the
company's statement layout, plus `expected.json` (every printed value) and a README with what to upload and what to
check. Numbers carry the slot (for example PI-UA2-01 for TKK.accountant.2), so two people uploading into the same
company never trip each other's duplicate check. Nothing is written to any database. Files go under
`ai/synthetic/output/` by default, which git ignores.
"""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ai.synthetic.knt_pack import money
from ai.synthetic.sme_books import books_for
from services.admin.team_setup import render, sources, usual_amounts

DAY = date(2026, 11, 24)
TOKENS_PER_DOCUMENT = 2500


def _cents(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def tag_for(person):
    return f"{'M' if person.role == 'local_admin' else 'A'}{person.number}"


def documents_for(books, person):
    supply, usual = sources(books), usual_amounts(books)
    tag = tag_for(person)

    def doc(n, kind, source, factor, description, *, related=None, day=0, fixed=None):
        code, account, rate = supply[source]
        net = _cents(fixed if fixed is not None else usual[source] * Decimal(factor))
        tax = _cents(net * rate)
        issued = DAY + timedelta(days=day)
        due = issued + timedelta(days=30) if kind in {"invoice", "bill"} else None
        prefix = {"invoice": "PI", "bill": "UB", "receipt": "RC", "credit_note": "SC"}[kind]
        return {"n": n, "kind": kind, "supplier": books.contacts[code][1], "supplier_street": books.contacts[code][3],
                "contact": code, "number": f"{prefix}-U{tag}-{n:02d}", "related": related,
                "date": issued.isoformat(), "due": due.isoformat() if due else None, "description": description,
                "account": account, "subtotal": money(net), "tax": money(tax), "total": money(net + tax),
                "file": f"{n:02d}_{issued}_{code}_{prefix}-U{tag}-{n:02d}.{'jpg' if kind in {'bill', 'receipt'} else 'pdf'}"}

    invoice = doc(1, "invoice", "trade", "0.98", "Stock for the live upload test")
    bill = doc(2, "bill", "utility", "1.01", "Electricity, live upload test")
    receipt = doc(3, "receipt", "receipt", "0.95", "Small items paid in cash, live upload test")
    credit = doc(4, "credit_note", "trade", None, "Part of the stock returned", related=invoice["number"],
                 fixed=_cents(Decimal(invoice["subtotal"]) / 10))
    return invoice, bill, receipt, credit


def slip_page(books, person, invoice, credit, folder):
    """A bank transfer slip (photographed) for the invoice less the credit note."""
    from ai.synthetic.company_pack import NOTICE
    from ai.synthetic.generate import Generator, Page

    amount = Decimal(invoice["total"]) - Decimal(credit["total"])
    reference = f"TRF-U{tag_for(person)}-05"
    page = Page(420, 595)
    page.text(30, 36, "ONLINE TRANSFER - PAYMENT ADVICE", 13, bold=True)
    rows = (("Date", f"{DAY + timedelta(days=2)}  10:42:17"), ("From", books.company["name"]),
            ("From account", "ending " + books.company["bank_account_number"][-4:]), ("To", invoice["supplier"]),
            ("Amount", f"MYR {money(amount)}"), ("Reference", reference),
            ("Payment for", f"{invoice['number']} less {credit['number']}"), ("Status", "Successful"))
    for y, (label, value) in enumerate(rows):
        page.text(30, 80 + y * 22, label, 10)
        page.text(150, 80 + y * 22, value, 10, bold=label == "Amount")
    page.text(30, 290, NOTICE[:70], 7)
    data, _size, _variant = Generator("holdout").degrade(page.pdf(), True)
    path = folder / f"05_{DAY + timedelta(days=2)}_TRANSFER_{reference}.jpg"
    path.write_bytes(data)
    return {"n": 5, "kind": "payment_slip", "file": path.name, "date": str(DAY + timedelta(days=2)),
            "reference": reference, "to": invoice["supplier"], "amount": money(amount),
            "pays": [invoice["number"], credit["number"]]}


def bank_csv(books, person, slip, bill, folder):
    """Three statement lines in the company's own layout: the transfer, the electricity payment and a fee."""
    tag = tag_for(person)
    lines = [(DAY + timedelta(days=2), f"Transfer to {slip['to'].split()[0]}", slip["reference"], -Decimal(slip["amount"])),
             (DAY + timedelta(days=3), "Electricity payment", f"PV-U{tag}-06", -Decimal(bill["total"])),
             (DAY + timedelta(days=4), "Bank service charge", f"FEE-U{tag}", Decimal("-10.00"))]
    name = f"06_{books.code}_STATEMENT_U{tag}.csv"
    with (folder / name).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        balance = Decimal("20000.00")
        if books.bank_date_format == "dmy":
            writer.writerow(("Transaction Date", "Description", "Reference", "Debit", "Credit", "Balance"))
            for on, text, ref, amount in lines:
                balance += amount
                writer.writerow((f"{on:%d/%m/%Y}", text, ref, money(-amount) if amount < 0 else "",
                                 money(amount) if amount > 0 else "", money(balance)))
        else:
            writer.writerow(("Date", "Description", "Reference", "Amount", "Balance"))
            for on, text, ref, amount in lines:
                balance += amount
                writer.writerow((on, text, ref, money(amount), money(balance)))
    return {"file": name, "layout": "debit/credit columns, dd/mm/yyyy" if books.bank_date_format == "dmy"
            else "signed amount, yyyy-mm-dd", "mapping": books.bank_mapping, "date_format": books.bank_date_format,
            "account": books.bank_label, "lines": [{"date": str(on), "reference": ref, "amount": money(amount)}
                                                   for on, _text, ref, amount in lines]}


README = """# Live upload test pack: {slot} ({company})

Fictional files for {name}. Uploading them spends AI quota: about {tokens:,} tokens for the four documents
(about {per:,} each). Upload on your own day; the team shares one daily quota.

| File | Upload as | What to check after it is read |
|---|---|---|
{rows}

`expected.json` lists every printed value. Tax is printed on the document: check the app reads the printed amount and
never recalculates it.

## Bank (no AI)

Bank -> Import statement -> `{bank_file}` for {bank_account} ({layout}). Then match:
the transfer to the invoice less the credit note (attach the payment slip, file 05, as evidence); the electricity
payment to the bill; record the fee as a bank charge.
"""


def write_packs(people, out):
    out = Path(out)
    written = 0
    for person in people:
        books = books_for(person.company)
        folder = out / person.slot
        folder.mkdir(parents=True, exist_ok=True)
        docs = documents_for(books, person)
        for item in docs:
            render(books, item, folder)
        invoice, bill, receipt, credit = docs
        slip = slip_page(books, person, invoice, credit, folder)
        bank = bank_csv(books, person, slip, bill, folder)
        expected = {"slot": person.slot, "company": books.company["name"], "documents": list(docs),
                    "payment_slip": slip, "bank_statement": bank,
                    "lane_after_upload": "Ready when every value reads as printed and the supplier matches; "
                                         "otherwise To review with the differing field flagged. The credit note "
                                         "should be linked to invoice 01."}
        (folder / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        rows = "\n".join(f"| `{item['file']}` | {item['kind'].replace('_', ' ')} | {item['supplier']}, "
                         f"{item['number']}, {item['date']}, subtotal {item['subtotal']}, tax {item['tax']}, "
                         f"total {item['total']} |" for item in docs)
        rows += f"\n| `{slip['file']}` | evidence for the bank match | transfer {slip['reference']}, MYR {slip['amount']} |"
        (folder / "README.md").write_text(README.format(
            slot=person.slot, company=books.company["name"], name=person.name, tokens=TOKENS_PER_DOCUMENT * 4,
            per=TOKENS_PER_DOCUMENT, rows=rows, bank_file=bank["file"], bank_account=books.bank_label,
            layout=bank["layout"]), encoding="utf-8")
        written += 1
    return written
