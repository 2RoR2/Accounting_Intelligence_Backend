"""Build the KNT demo pack: fictional RECEIVED documents, a bank statement and a transfer slip.

Run from the repository root (needs Pillow, numpy and opencv, which requirements.txt already installs):

    python docs/demo/build_demo_pack.py            # writes docs/demo/pack/
    python docs/demo/build_demo_pack.py --check    # only verifies the arithmetic and the supplier identities

Everything here is invented. Supplier names, addresses, SSM / TIN / SST numbers come from the KNT seed
(ai/synthetic/knt_pack.py), so the Inbox can match the supplier. Tax is never computed by the application:
the SST amounts below are typed once, exactly as "printed", and `check()` only proves each document adds up.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai.synthetic.knt_pack import COMPANY, CONTACTS, books, contact_header  # noqa: E402

NOTICE = "Synthetic sample: fictional company, contacts and transactions. Not real records."
OUT = Path(__file__).resolve().parent / "pack"
D = Decimal


def money(value):
    return f"{D(value):,.2f}"


# --------------------------------------------------------------------------- drawing model
FONT_FILES = {
    ("sans", False): ("arial.ttf", "DejaVuSans.ttf"), ("sans", True): ("arialbd.ttf", "DejaVuSans-Bold.ttf"),
    ("mono", False): ("cour.ttf", "DejaVuSansMono.ttf"), ("mono", True): ("courbd.ttf", "DejaVuSansMono-Bold.ttf"),
}
_cache = {}


def font(kind, bold, size):
    from PIL import ImageFont

    key = (kind, bold, round(size * 4))
    if key not in _cache:
        for name in FONT_FILES[(kind, bold)]:
            try:
                _cache[key] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _cache[key] = ImageFont.load_default()
    return _cache[key]


def text_width(value, size, bold=False, kind="sans"):
    return font(kind, bold, 100).getlength(value) * size / 100


class Canvas:
    """Text, lines and boxes in PDF points (y from the top). Renders to a text PDF or to a PIL image."""

    def __init__(self, width, height):
        self.width, self.height, self.ops = width, height, []

    def text(self, x, y, value, size=10, bold=False, align="left", kind="sans", grey=0.0):
        width = text_width(value, size, bold, kind)
        x = x - width if align == "right" else x - width / 2 if align == "center" else x
        self.ops.append(("text", x, y, str(value), size, bold, kind, grey))

    def line(self, x1, y1, x2, y2, width=0.6, grey=0.0):
        self.ops.append(("line", x1, y1, x2, y2, width, grey))

    def box(self, x, y, w, h, fill=None, stroke=0.0, width=0.6):
        self.ops.append(("box", x, y, w, h, fill, stroke, width))

    # -- PDF (real text layer, Helvetica / Courier) -------------------------------------------
    def pdf(self):
        def esc(value):
            return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

        stream = ""
        for op in self.ops:
            if op[0] == "text":
                _, x, y, value, size, bold, kind, grey = op
                face = {("sans", False): "F1", ("sans", True): "F2", ("mono", False): "F3", ("mono", True): "F4"}[(kind, bold)]
                stream += f"{grey:.2f} g BT /{face} {size} Tf {x:.2f} {self.height - y - size * 0.8:.2f} Td ({esc(value)}) Tj ET\n"
            elif op[0] == "line":
                _, x1, y1, x2, y2, width, grey = op
                stream += f"{grey:.2f} G {width} w {x1:.2f} {self.height - y1:.2f} m {x2:.2f} {self.height - y2:.2f} l S\n"
            else:
                _, x, y, w, h, fill, stroke, width = op
                if fill is not None:
                    stream += f"{fill:.2f} g {x:.2f} {self.height - y - h:.2f} {w:.2f} {h:.2f} re f\n"
                if stroke is not None:
                    stream += f"{stroke:.2f} G {width} w {x:.2f} {self.height - y - h:.2f} {w:.2f} {h:.2f} re S\n"
        data = stream.encode("cp1252", "replace")
        fonts = ["Helvetica", "Helvetica-Bold", "Courier", "Courier-Bold"]
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.width:g} {self.height:g}] /Resources << /Font << "
                    + " ".join(f"/F{i + 1} {4 + i} 0 R" for i in range(4)) + f" >> >> /Contents {4 + len(fonts)} 0 R >>").encode()]
        objects += [f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>".encode() for name in fonts]
        objects.append(f"<< /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream")
        out, offsets = b"%PDF-1.4\n", []
        for number, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
        out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return out

    # -- raster ---------------------------------------------------------------------------------
    def image(self, scale=2.4, paper=(252, 252, 250)):
        from PIL import Image, ImageDraw

        picture = Image.new("RGB", (round(self.width * scale), round(self.height * scale)), paper)
        draw = ImageDraw.Draw(picture)
        gray = lambda g: (round(g * 255),) * 3  # noqa: E731
        for op in self.ops:
            if op[0] == "text":
                _, x, y, value, size, bold, kind, grey = op
                draw.text((x * scale, y * scale), value, font=font(kind, bold, size * scale), fill=gray(grey))
            elif op[0] == "line":
                _, x1, y1, x2, y2, width, grey = op
                draw.line((x1 * scale, y1 * scale, x2 * scale, y2 * scale), fill=gray(grey), width=max(1, round(width * scale)))
            else:
                _, x, y, w, h, fill, stroke, width = op
                draw.rectangle((x * scale, y * scale, (x + w) * scale, (y + h) * scale),
                               fill=None if fill is None else gray(fill), outline=None if stroke is None else gray(stroke),
                               width=max(1, round(width * scale)))
        return picture


# --------------------------------------------------------------------------- the documents
def party(code):
    kind, name, _, address = CONTACTS[code]
    head = contact_header(code)
    return {"name": name, "address_line": address, "city_line": f"{head.postcode} {head.city}, Sarawak", "phone": head.phone,
            "ssm": head.ssm, "tin": head.tin, "sst": head.sst, "terms": head.terms}


@dataclass
class Row:
    code: str
    description: str
    quantity: D
    unit: str
    price: D

    @property
    def amount(self):
        return (self.quantity * self.price).quantize(D("0.01"))


@dataclass
class Doc:
    key: str
    kind: str  # invoice | bill | receipt | credit_note
    supplier: str
    number: str
    issued: date
    rows: list
    tax_label: str
    tax: D  # typed exactly as printed
    due: date | None = None
    reference: str | None = None  # original invoice for a credit note
    extra: dict = field(default_factory=dict)

    @property
    def subtotal(self):
        return sum((row.amount for row in self.rows), D("0"))

    @property
    def total(self):
        return self.subtotal + self.tax


DOCS = [
    Doc("invoice", "invoice", "SEL", "SCC-2611-0412", date(2026, 11, 19),
        [Row("CEM-50", "Portland cement 50 kg (OPC)", D(200), "bag", D("19.80")),
         Row("MRT-25", "Ready-mix mortar 25 kg", D(80), "bag", D("12.20")),
         Row("SND-FN", "Fine river sand (delivered to site)", D(20), "tonne", D("52.00"))],
        "SST 5%", D("298.80"), due=date(2026, 12, 19),
        extra={"do": "DO-88417", "terms": "30 days"}),
    Doc("bill", "bill", "KEN", "KT-BL-2611-3358", date(2026, 11, 20),
        [Row("", "Business Fibre 100 Mbps - monthly fee", D(1), "month", D("299.00")),
         Row("", "Static IP address add-on", D(1), "month", D("40.00")),
         Row("", "Fixed line rental (2 lines)", D(2), "line", D("25.00")),
         Row("", "Call charges (itemised overleaf)", D(1), "", D("38.60"))],
        "Service tax 8%", D("34.21"), due=date(2026, 12, 20),
        extra={"account": "KT-4471-0092", "period": "21 Oct 2026 - 20 Nov 2026"}),
    Doc("receipt", "receipt", "REN", "RS-088412", date(2026, 11, 21),
        [Row("", "A4 copier paper 80gsm (ream)", D(5), "", D("13.90")),
         Row("", "Flip file A4 (plastic)", D(10), "", D("2.30"))],
        "SST 10%", D("9.25"), extra={"time": "10:42", "tendered": D("110.00"), "cashier": "Siti"}),
    Doc("credit_note", "credit_note", "PAU", "PBS-CN-2611-017", date(2026, 11, 20),
        [Row("BLT-M10", "Hex bolts M10 x 60 mm, zinc (box 100) - returned damaged", D(8), "box", D("18.75"))],
        "SST 10%", D("15.00"), reference="PI-2611-01", extra={"original_date": date(2026, 11, 6)}),
]
SLIP = {"when": "24 Nov 2026  10:42:17", "reference": "DN2411260048213", "to": "SEL", "memo": "SCC-2611-0412"}
STATEMENT_DAY = date(2026, 11, 24)


def words(amount):
    ones = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    tens = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()

    def below_thousand(n):
        parts = []
        if n >= 100:
            parts.append(ones[n // 100] + " hundred")
            n %= 100
        if n >= 20:
            parts.append(tens[n // 10] + ("-" + ones[n % 10] if n % 10 else ""))
        elif n or not parts:
            parts.append(ones[n])
        return " ".join(parts)

    ringgit, sen = int(amount), int((amount - int(amount)) * 100)
    text = below_thousand(ringgit % 1000)
    if ringgit >= 1000:
        text = below_thousand(ringgit // 1000) + " thousand" + (" " + text if ringgit % 1000 else "")
    return f"Ringgit Malaysia {text} and {below_thousand(sen)} sen only" if sen else f"Ringgit Malaysia {text} only"


def fmt(day):
    return f"{day.day} {day:%b %Y}"


def kuching_head(page, who, y=36, accent=0.18):
    s = party(who)
    page.text(40, y, s["name"].upper(), 15, bold=True)
    page.text(40, y + 20, f"{s['address_line']}, {s['city_line']}", 9)
    page.text(40, y + 32, f"Tel: {s['phone']}   Co. Reg. No. (SSM): {s['ssm']}", 9)
    page.text(40, y + 44, f"SST Reg. No.: {s['sst']}   TIN: {s['tin']}", 9)
    page.line(40, y + 60, 555, y + 60, 1.2, accent)
    return s


def bill_to(page, y):
    page.text(40, y, "BILL TO", 8, bold=True, grey=0.35)
    page.text(40, y + 12, COMPANY["name"], 10, bold=True)
    page.text(40, y + 25, COMPANY["address"], 9)
    page.text(40, y + 37, f"Co. Reg. No.: {COMPANY['ssm']}   TIN: {COMPANY['tin']}", 9)


def lines_table(page, doc, y, with_code=True):
    columns = [("No.", 44, "left"), ("Description", 70, "left"), ("Qty", 392, "right"), ("UOM", 402, "left"),
               ("Unit price", 490, "right"), ("Amount (RM)", 555, "right")]
    if with_code:
        columns[1] = ("Item code / Description", 70, "left")
    page.box(40, y, 515, 18, fill=0.9, stroke=None)
    for label, x, align in columns:
        page.text(x, y + 4, label, 9, bold=True, align=align)
    y += 22
    for number, row in enumerate(doc.rows, 1):
        page.text(44, y, str(number), 9)
        label = (row.code + "  " if row.code and with_code else "") + row.description
        page.text(70, y, label, 9)
        page.text(392, y, f"{row.quantity:g}", 9, align="right")
        page.text(402, y, row.unit, 9)
        page.text(490, y, money(row.price), 9, align="right")
        page.text(555, y, money(row.amount), 9, align="right")
        y += 17
    page.line(40, y + 2, 555, y + 2, 0.6)
    return y + 8


def totals(page, doc, y):
    page.text(440, y, "Subtotal", 10, align="right")
    page.text(555, y, money(doc.subtotal), 10, align="right")
    page.text(440, y + 16, doc.tax_label, 10, align="right")
    page.text(555, y + 16, money(doc.tax), 10, align="right")
    page.line(350, y + 33, 555, y + 33, 0.6)
    page.text(440, y + 40, "TOTAL (RM)", 11, bold=True, align="right")
    page.text(555, y + 40, money(doc.total), 11, bold=True, align="right")
    return y + 66


def page_invoice(doc):
    page = Canvas(595, 842)
    kuching_head(page, doc.supplier)
    page.text(555, 38, "TAX INVOICE", 18, bold=True, align="right")
    bill_to(page, 112)
    labels = [("Invoice No.", doc.number), ("Invoice date", fmt(doc.issued)), ("Terms", doc.extra["terms"]),
              ("Due date", fmt(doc.due)), ("D.O. No.", doc.extra["do"])]
    for index, (label, value) in enumerate(labels):
        page.text(350, 112 + index * 14, label, 9, grey=0.35)
        page.text(440, 112 + index * 14, value, 9, bold=index in (0, 3))
    y = lines_table(page, doc, 214)
    y = totals(page, doc, y + 6)
    page.text(40, y - 52, "Amount in words:", 9, bold=True)
    page.text(40, y - 40, words(doc.total), 9)
    page.text(40, y + 10, "Payment: bank transfer to Seladah Cement & Concrete Sdn. Bhd., Contoh Bank Berhad A/C 1234-5678-9004.", 8.5)
    page.text(40, y + 22, "Please quote the invoice number. Goods sold are not returnable unless damaged on delivery.", 8.5)
    page.text(40, y + 34, "This is a computer-generated invoice. No signature is required.", 8.5)
    page.text(40, 800, NOTICE, 7.5, grey=0.45)
    return page


def page_credit_note(doc):
    page = Canvas(595, 842)
    kuching_head(page, doc.supplier)
    page.text(555, 38, "CREDIT NOTE", 18, bold=True, align="right")
    bill_to(page, 112)
    labels = [("Credit note No.", doc.number), ("Date", fmt(doc.issued)),
              ("Original invoice", doc.reference), ("Invoice date", fmt(doc.extra["original_date"])),
              ("Reason", "Goods returned - damaged")]
    for index, (label, value) in enumerate(labels):
        page.text(350, 112 + index * 14, label, 9, grey=0.35)
        page.text(440, 112 + index * 14, value, 9, bold=index in (0, 2))
    y = lines_table(page, doc, 204)
    y = totals(page, doc, y + 6)
    page.text(40, y - 52, "Amount in words:", 9, bold=True)
    page.text(40, y - 40, words(doc.total), 9)
    page.text(40, y + 10, f"This credit note reduces invoice {doc.reference} and will be set off against your next payment.", 8.5)
    page.text(40, y + 22, "This is a computer-generated credit note. No signature is required.", 8.5)
    page.text(40, 800, NOTICE, 7.5, grey=0.45)
    return page


def page_bill(doc):
    """A utility-style bill, drawn narrow-margined so a photocopy / phone scan stays legible."""
    page = Canvas(595, 842)
    kuching_head(page, doc.supplier, accent=0.1)
    page.text(555, 38, "BILL", 22, bold=True, align="right")
    page.text(555, 62, "Business Services Statement", 9, align="right")
    page.box(40, 110, 515, 78, fill=0.95, stroke=0.6)
    page.text(52, 118, "Customer", 8, bold=True, grey=0.35)
    page.text(52, 130, COMPANY["name"], 10, bold=True)
    page.text(52, 144, COMPANY["address"], 9)
    page.text(52, 158, f"Co. Reg. No.: {COMPANY['ssm']}   TIN: {COMPANY['tin']}", 9)
    labels = [("Account No.", doc.extra["account"]), ("Bill No.", doc.number), ("Bill date", fmt(doc.issued)),
              ("Billing period", doc.extra["period"]), ("Payment due", fmt(doc.due))]
    for index, (label, value) in enumerate(labels):
        page.text(52, 204 + index * 15, label, 9, grey=0.35)
        page.text(150, 204 + index * 15, value, 9, bold=index in (1, 4))
    y = lines_table(page, doc, 296, with_code=False)
    y = totals(page, doc, y + 6)
    page.box(40, y + 6, 515, 40, fill=0.88, stroke=0.0)
    page.text(52, y + 18, "AMOUNT DUE (RM)", 12, bold=True)
    page.text(543, y + 14, money(doc.total), 18, bold=True, align="right")
    page.text(40, y + 62, "Pay at any branch, online banking (biller: Kenyalang Telecom) or by bank transfer quoting the Bill No.", 8.5)
    page.text(40, y + 74, "Late payment may attract a reconnection fee. Queries: billing@kenyalang-telecom.test", 8.5)
    page.text(40, 800, NOTICE, 7.5, grey=0.45)
    return page


def page_receipt(doc):
    """A narrow thermal-printer receipt, 227 pt wide (80 mm)."""
    page = Canvas(227, 330)
    s = party(doc.supplier)
    centre = 113
    y = 14
    page.text(centre, y, s["name"].upper(), 11, bold=True, align="center", kind="mono"); y += 15
    page.text(centre, y, s["address_line"], 8, align="center", kind="mono"); y += 10
    page.text(centre, y, s["city_line"], 8, align="center", kind="mono"); y += 10
    page.text(centre, y, f"Tel {s['phone']}", 8, align="center", kind="mono"); y += 10
    page.text(centre, y, f"SSM {s['ssm']}", 8, align="center", kind="mono"); y += 10
    page.text(centre, y, f"SST ID {s['sst']}", 8, align="center", kind="mono"); y += 14
    page.text(centre, y, "TAX INVOICE / CASH RECEIPT", 9, bold=True, align="center", kind="mono"); y += 14
    for label, value in (("Receipt", doc.number), ("Date", f"{doc.issued:%d/%m/%Y} {doc.extra['time']}"),
                         ("Cashier", doc.extra["cashier"]), ("Bill to", "KNT SDN BHD")):
        page.text(10, y, f"{label:<9}{value}", 8.5, kind="mono"); y += 10
    y += 3
    page.text(10, y, "-" * 38, 8, kind="mono"); y += 9
    page.text(10, y, "ITEM", 8, bold=True, kind="mono")
    page.text(217, y, "AMOUNT", 8, bold=True, align="right", kind="mono"); y += 11
    for row in doc.rows:
        page.text(10, y, row.description[:30], 8.5, kind="mono"); y += 10
        page.text(18, y, f"{row.quantity:g} x {money(row.price)}", 8.5, kind="mono")
        page.text(217, y, money(row.amount), 8.5, align="right", kind="mono"); y += 12
    page.text(10, y, "-" * 38, 8, kind="mono"); y += 10
    for label, value, bold in (("SUBTOTAL", doc.subtotal, False), (doc.tax_label.upper(), doc.tax, False),
                               ("TOTAL RM", doc.total, True)):
        page.text(10, y, label, 9, bold=bold, kind="mono")
        page.text(217, y, money(value), 9, bold=bold, align="right", kind="mono"); y += 11
    y += 2
    page.text(10, y, "CASH", 8.5, kind="mono")
    page.text(217, y, money(doc.extra["tendered"]), 8.5, align="right", kind="mono"); y += 10
    page.text(10, y, "CHANGE", 8.5, kind="mono")
    page.text(217, y, money(doc.extra["tendered"] - doc.total), 8.5, align="right", kind="mono"); y += 16
    page.text(centre, y, "THANK YOU - GOODS SOLD NOT RETURNABLE", 7, align="center", kind="mono"); y += 10
    page.text(centre, y, "Sample receipt - fictional business", 7, align="center", kind="mono", grey=0.4)
    page.height = y + 22
    return page


def page_counter_receipt(doc):
    """Proof of a counter payment for the telecom bill (cash at the supplier's customer-care counter)."""
    page = Canvas(227, 260)
    s = party(doc.supplier)
    centre, y = 113, 14
    page.text(centre, y, s["name"].upper(), 11, bold=True, align="center", kind="mono"); y += 15
    page.text(centre, y, "Customer Care Counter, Kuching", 8, align="center", kind="mono"); y += 10
    page.text(centre, y, f"Tel {s['phone']}", 8, align="center", kind="mono"); y += 16
    page.text(centre, y, "PAYMENT RECEIPT", 10, bold=True, align="center", kind="mono"); y += 16
    for label, value in (("Receipt", "CC-2411-77310"), ("Date", "24/11/2026 11:05"), ("Account", doc.extra["account"]),
                         ("Bill no.", doc.number), ("Paid by", "KNT SDN BHD"), ("Method", "CASH")):
        page.text(10, y, f"{label:<9}{value}", 8.5, kind="mono"); y += 10
    y += 4
    page.text(10, y, "-" * 38, 8, kind="mono"); y += 10
    page.text(10, y, "AMOUNT RECEIVED RM", 9, bold=True, kind="mono")
    page.text(217, y, money(doc.total), 9, bold=True, align="right", kind="mono"); y += 16
    page.text(centre, y, "RECEIVED WITH THANKS", 8, align="center", kind="mono"); y += 10
    page.text(centre, y, "Sample receipt - fictional business", 7, align="center", kind="mono", grey=0.4)
    page.height = y + 22
    return page


# --------------------------------------------------------------------------- degrade to a "scan" / "photo"
def scan(picture, seed, angle=1.1, noise=7, blur=0.6):
    import numpy as np
    from PIL import Image, ImageFilter

    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)
    desk = Image.new("RGB", (picture.width + 120, picture.height + 120), (205, 205, 200))
    desk.paste(picture, (60, 60))
    desk = desk.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(205, 205, 200))
    array = np.asarray(desk).astype("float32")
    array = array * np.linspace(1.0, 0.86, array.shape[1])[None, :, None]  # uneven lighting
    array = array * 0.94 + 6
    array += np_rng.normal(0, noise, array.shape)
    for _ in range(40):  # a few specks of dust
        cx, cy = rng.randrange(array.shape[1]), rng.randrange(array.shape[0])
        array[cy:cy + 2, cx:cx + 2] -= 70
    return Image.fromarray(np.clip(array, 0, 255).astype("uint8")).filter(ImageFilter.GaussianBlur(blur))


def photo_of_receipt(page, seed):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    rng = np.random.default_rng(seed)
    receipt = page.image(scale=4.0, paper=(250, 249, 244))
    arr = np.asarray(receipt).astype("float32")  # thermal paper curls: a slight sideways bow
    bow = (np.sin(np.linspace(0, np.pi, arr.shape[0])) * 7).astype(int)
    for row in range(arr.shape[0]):
        arr[row] = np.roll(arr[row], bow[row], axis=0)
    receipt = Image.fromarray(np.clip(arr * 0.9 + 12, 0, 255).astype("uint8"))
    desk = (96, 78, 62)
    canvas = Image.new("RGB", (receipt.width + 560, receipt.height + 420), desk)
    draw = ImageDraw.Draw(canvas)
    for stripe in range(0, canvas.height, 9):  # wood grain
        draw.line((0, stripe, canvas.width, stripe + int(rng.integers(-2, 3))),
                  fill=(88 + int(rng.integers(0, 16)), 72, 56), width=2)
    shadow = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(shadow).rectangle((300, 240, 300 + receipt.width, 240 + receipt.height), fill=120)
    canvas.paste((20, 14, 8), (28, 22), shadow.filter(ImageFilter.GaussianBlur(26)))
    canvas.paste(receipt, (280, 200))
    canvas = canvas.rotate(-1.8, resample=Image.BICUBIC, fillcolor=desk)
    array = np.asarray(canvas).astype("float32")
    array = array * np.linspace(1.0, 0.82, array.shape[0])[:, None, None]  # light falls off down the frame
    array += rng.normal(0, 6, array.shape)
    return Image.fromarray(np.clip(array, 0, 255).astype("uint8")).filter(ImageFilter.GaussianBlur(0.9))


def transfer_slip():
    from PIL import Image, ImageDraw

    width, height = 1080, 1500
    page = Image.new("RGB", (width, height), (246, 247, 249))
    draw = ImageDraw.Draw(page)
    S = lambda size, bold=False: font("sans", bold, size)  # noqa: E731
    draw.rectangle((0, 0, width, 150), fill=(122, 20, 36))
    draw.text((60, 52), "Public Bank  |  Online Transfer", font=S(46, True), fill="white")
    draw.ellipse((440, 210, 640, 410), fill=(40, 160, 90))
    draw.line((490, 315, 530, 355), fill="white", width=18)
    draw.line((530, 355, 598, 270), fill="white", width=18)
    draw.text((width // 2, 450), "Transfer successful", font=S(54, True), fill=(30, 30, 30), anchor="mm")
    draw.text((width // 2, 530), SLIP["when"], font=S(34), fill=(100, 100, 100), anchor="mm")
    draw.text((width // 2, 640), f"RM {money(DOCS[0].total)}", font=S(96, True), fill=(30, 30, 30), anchor="mm")
    y = 760
    supplier = party(SLIP["to"])
    rows = [("From", f"{COMPANY['name']}\nPublic Bank Current Account ••{COMPANY['bank_account_number'][-4:]}"),
            ("To", f"{supplier['name']}\nContoh Bank Berhad ••9004"),
            ("Transfer type", "Interbank GIRO (IBG)"), ("Payment details", SLIP["memo"] + " invoice payment"),
            ("Reference no.", SLIP["reference"]), ("Status", "Completed")]
    draw.rounded_rectangle((40, y - 20, width - 40, y + 640), radius=24, fill="white", outline=(225, 227, 232), width=3)
    for label, value in rows:
        draw.text((80, y), label, font=S(30), fill=(120, 120, 120))
        for number, part in enumerate(value.split("\n")):
            draw.text((width - 80, y + number * 40), part, font=S(32, number == 0), fill=(30, 30, 30), anchor="ra")
        y += 100 if "\n" not in value else 120
    draw.text((width // 2, height - 90), NOTICE, font=S(24), fill=(140, 140, 140), anchor="mm")
    return page


# --------------------------------------------------------------------------- bank statement
def statement_rows():
    """Opening = closing balance of the seeded November statement; two lines on the demo day."""
    _, _, bank = books()
    opening = D("18000.00")
    for line in bank:
        opening += line.amount
    invoice = DOCS[0]
    lines = [(STATEMENT_DAY, "IBG TRANSFER SELADAH CEMENT & CONC", "SCC-2611-0412", -invoice.total),
             (STATEMENT_DAY, "SERVICE CHARGE", "FEE-2611-C", D("-8.00"))]
    rows, balance = [], opening
    for day, description, reference, amount in lines:
        balance += amount
        rows.append((day.isoformat(), description, reference, f"{amount:.2f}", f"{balance:.2f}"))
    return opening, rows, balance


# --------------------------------------------------------------------------- checks and output
def check():
    problems = []
    for doc in DOCS:
        s = party(doc.supplier)
        if doc.subtotal + doc.tax != doc.total:
            problems.append(f"{doc.key}: lines + tax != total")
        if not (date(2026, 11, 1) <= doc.issued <= date(2026, 11, 30)):
            problems.append(f"{doc.key}: date outside November 2026")
        if len(s["ssm"]) != 12 or not s["tin"].startswith("C"):
            problems.append(f"{doc.key}: supplier identity looks wrong")
        print(f"{doc.key:12} {s['name']:38} {doc.number:16} {doc.issued}  subtotal {money(doc.subtotal):>10}  "
              f"{doc.tax_label} {money(doc.tax):>8}  total {money(doc.total):>10}")
    opening, rows, closing = statement_rows()
    print(f"statement    opening {money(opening)}  closing {money(closing)}  lines {len(rows)}")
    _, purchases, _ = books()
    original = next((doc for doc in purchases if doc.number == DOCS[3].reference), None)
    if original is None or original.contact != DOCS[3].supplier or DOCS[3].total > original.total:
        problems.append("credit note does not refer to a seeded purchase invoice of the same supplier")
    return problems


def build():
    import csv

    OUT.mkdir(parents=True, exist_ok=True)
    invoice, bill, receipt, credit = DOCS
    (OUT / "1_invoice_Seladah_SCC-2611-0412.pdf").write_bytes(page_invoice(invoice).pdf())
    scan(page_bill(bill).image(2.2), seed=11).save(OUT / "2_scanned_bill_Kenyalang_KT-BL-2611-3358.jpg", quality=66, optimize=True)
    photo_of_receipt(page_receipt(receipt), seed=5).save(OUT / "3_receipt_photo_Rengas_RS-088412.jpg", quality=72, optimize=True)
    (OUT / "4_credit_note_Pauh_PBS-CN-2611-017.pdf").write_bytes(page_credit_note(credit).pdf())
    transfer_slip().save(OUT / "5_transfer_slip_Seladah_payment.jpg", quality=85)
    photo_of_receipt(page_counter_receipt(bill), seed=9).save(OUT / "7_counter_receipt_Kenyalang_payment.jpg", quality=72, optimize=True)
    opening, rows, closing = statement_rows()
    with (OUT / "6_bank_statement_PublicBank_9901_2026-11-24.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("Date", "Description", "Reference", "Amount", "Balance"))
        writer.writerows(rows)
    expected = {}
    for doc in DOCS:
        s = party(doc.supplier)
        expected[doc.key] = {"supplier": s["name"], "ssm": s["ssm"], "tin": s["tin"], "number": doc.number,
                             "date": doc.issued.isoformat(), "due": doc.due.isoformat() if doc.due else None,
                             "subtotal": f"{doc.subtotal:.2f}", "tax": f"{doc.tax:.2f}", "total": f"{doc.total:.2f}",
                             "reference": doc.reference,
                             "lines": [{"description": r.description, "quantity": f"{r.quantity:g}",
                                        "unit_price": f"{r.price:.2f}", "amount": f"{r.amount:.2f}"} for r in doc.rows]}
    expected["bank_statement"] = {"account": COMPANY["bank_account_number"], "opening": f"{opening:.2f}",
                                  "closing": f"{closing:.2f}", "lines": [list(r) for r in rows]}
    (OUT / "expected_values.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify the arithmetic and identities only")
    args = parser.parse_args()
    found = check()
    for item in found:
        print("PROBLEM:", item)
    if found:
        sys.exit(1)
    if not args.check:
        build()
