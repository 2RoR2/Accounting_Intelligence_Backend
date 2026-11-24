"""Dependency-free PDF rendering for every document an ERP workspace issues.

``PdfCanvas`` is a minimal PDF 1.4 writer that only uses the standard Type1
fonts Helvetica and Helvetica-Bold (WinAnsiEncoding), so no font embedding or
third-party library is needed. Output is deterministic: identical input gives
identical bytes (no timestamps or random ids).

Canvas coordinates are in points with the origin at the TOP-LEFT of the page
and y growing downwards; ``text`` takes the baseline position. The writer
converts to PDF user space (origin bottom-left) when emitting operators.

``render_document_pdf`` lays out a quotation, invoice, receipt, voucher,
delivery order or note on one shared template. It never computes amounts or
tax: every figure is printed exactly as supplied by the caller.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

PAGE_WIDTH = 595
PAGE_HEIGHT = 842
PRODUCER = "Accounting Intelligence"
NO_SIGNATURE = "This is a computer-generated document. No signature is required."
# A delivery order is the one printed document the customer signs: its signed copy is the proof of delivery.
SIGN_AND_RETURN = "Please sign and chop one copy and return it to us."
RECEIVED_HEADING = "Goods received in good order and condition"

# Adobe AFM advance widths (1/1000 em) for character codes 32..126.
_HELVETICA_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,  # space ... /
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556,  # 0-9
    278, 278, 584, 584, 584, 556, 1015,  # : ; < = > ? @
    667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833,  # A-M
    722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611,  # N-Z
    278, 278, 278, 469, 556, 333,  # [ \ ] ^ _ `
    556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833,  # a-m
    556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500,  # n-z
    334, 260, 334, 584,  # { | } ~
)
_HELVETICA_BOLD_WIDTHS = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556,
    333, 333, 584, 584, 584, 611, 975,
    722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833,
    722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611,
    333, 278, 333, 584, 556, 333,
    556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889,
    611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500,
    389, 280, 389, 584,
)
assert len(_HELVETICA_WIDTHS) == len(_HELVETICA_BOLD_WIDTHS) == 95
_DEFAULT_WIDTH = 556

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _encode(s: Any) -> bytes:
    """Return the cp1252 bytes that will be drawn for ``s`` (single line)."""
    if s is None:
        return b""
    text = str(s).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    text = _CONTROL_CHARS.sub("", text)
    return text.encode("cp1252", errors="replace")


def _escape(data: bytes) -> str:
    out = []
    for b in data:
        if b in (0x28, 0x29, 0x5C):  # ( ) backslash
            out.append("\\" + chr(b))
        elif 32 <= b <= 126:
            out.append(chr(b))
        else:
            out.append("\\%03o" % b)
    return "".join(out)


def _num(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


class PdfCanvas:
    """A small multi-page A4 portrait drawing surface that serializes to PDF 1.4."""

    def __init__(self, title: str = "", producer: str = PRODUCER):
        self.title = title
        self.producer = producer
        self._pages: list[list[str]] = [[]]
        self._current = 0
        self._underlay: list[str] = []

    # -- pages -------------------------------------------------------------
    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def page_index(self) -> int:
        return self._current

    def new_page(self) -> int:
        self._pages.append([])
        self._current = len(self._pages) - 1
        return self._current

    def set_page(self, index: int) -> None:
        if not 0 <= index < len(self._pages):
            raise IndexError("page index out of range")
        self._current = index

    # -- measuring ---------------------------------------------------------
    @staticmethod
    def text_width(s: Any, size: float = 9, bold: bool = False) -> float:
        table = _HELVETICA_BOLD_WIDTHS if bold else _HELVETICA_WIDTHS
        units = sum(table[b - 32] if 32 <= b <= 126 else _DEFAULT_WIDTH for b in _encode(s))
        return units * size / 1000.0

    def wrap(self, s: Any, width: float, size: float = 9, bold: bool = False) -> list[str]:
        """Greedy word wrap; honours newlines and hard-breaks over-long words."""
        if s is None:
            return []
        text = str(s).replace("\r\n", "\n").replace("\r", "\n")
        lines: list[str] = []
        for paragraph in text.split("\n"):
            words = paragraph.replace("\t", " ").split()
            if not words:
                lines.append("")
                continue
            current = ""
            for word in words:
                candidate = f"{current} {word}" if current else word
                if self.text_width(candidate, size, bold) <= width:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                while len(word) > 1 and self.text_width(word, size, bold) > width:
                    cut = len(word) - 1
                    while cut > 1 and self.text_width(word[:cut], size, bold) > width:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
                current = word
            if current:
                lines.append(current)
        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()
        return lines

    # -- drawing -----------------------------------------------------------
    def _emit(self, op: str) -> None:
        self._pages[self._current].append(op)

    def _text_op(self, x: float, y: float, data: bytes, size: float, bold: bool) -> str:
        font = "/F2" if bold else "/F1"
        return (f"BT {font} {_num(size)} Tf {_num(x)} {_num(PAGE_HEIGHT - y)} Td "
                f"({_escape(data)}) Tj ET")

    def text(self, x: float, y: float, s: Any, size: float = 9, bold: bool = False,
             align: str = "left") -> None:
        data = _encode(s)
        if not data:
            return
        width = self.text_width(s, size, bold)
        if align == "right":
            x -= width
        elif align == "center":
            x -= width / 2
        self._emit(self._text_op(x, y, data, size, bold))

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5) -> None:
        self._emit(f"{_num(width)} w {_num(x1)} {_num(PAGE_HEIGHT - y1)} m "
                   f"{_num(x2)} {_num(PAGE_HEIGHT - y2)} l S")

    def rect(self, x: float, y: float, w: float, h: float, fill_gray: float | None = None,
             stroke: bool = True) -> None:
        """Rectangle with top-left corner (x, y)."""
        if fill_gray is None and not stroke:
            return
        op = "B" if fill_gray is not None and stroke else ("f" if fill_gray is not None else "S")
        fill = f"{_num(fill_gray)} g " if fill_gray is not None else ""
        self._emit(f"q {fill}0.5 w {_num(x)} {_num(PAGE_HEIGHT - y - h)} {_num(w)} {_num(h)} "
                   f"re {op} Q")

    def set_watermark(self, s: Any, gray: float = 0.85, max_size: float = 64) -> None:
        """Large light-gray text drawn beneath the content of every page."""
        data = _encode(s)
        if not data:
            self._underlay = []
            return
        size = min(max_size, 500 * 1000.0 / max(1.0, self.text_width(s, 1000, True)))
        width = self.text_width(s, size, True)
        x = (PAGE_WIDTH - width) / 2
        y = PAGE_HEIGHT / 2 + size / 3
        self._underlay = [f"q {_num(gray)} g", self._text_op(x, y, data, size, True), "Q"]

    # -- serialization -----------------------------------------------------
    def to_bytes(self) -> bytes:
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}

        def add(num: int, body: bytes) -> None:
            offsets[num] = len(out)
            out.extend(f"{num} 0 obj\n".encode("ascii"))
            out.extend(body)
            out.extend(b"\nendobj\n")

        n_pages = len(self._pages)
        page_ids = [6 + 2 * i for i in range(n_pages)]
        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        add(2, f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode("ascii"))
        add(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        add(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
               b"/Encoding /WinAnsiEncoding >>")
        add(5, (f"<< /Title ({_escape(_encode(self.title))}) "
                f"/Producer ({_escape(_encode(self.producer))}) >>").encode("ascii"))
        for i, ops in enumerate(self._pages):
            page_id = page_ids[i]
            content = "\n".join(self._underlay + ops).encode("ascii")
            add(page_id, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                          f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                          f"/Contents {page_id + 1} 0 R >>").encode("ascii"))
            add(page_id + 1, f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
                + content + b"\nendstream")
        size = 6 + 2 * n_pages
        xref = len(out)
        out.extend(f"xref\n0 {size}\n0000000000 65535 f \n".encode("ascii"))
        for num in range(1, size):
            out.extend(f"{offsets[num]:010d} 00000 n \n".encode("ascii"))
        out.extend((f"trailer\n<< /Size {size} /Root 1 0 R /Info 5 0 R >>\n"
                    f"startxref\n{xref}\n%%EOF\n").encode("ascii"))
        return bytes(out)


# ---------------------------------------------------------------------------
# Document template
# ---------------------------------------------------------------------------
TITLES = {
    "quotation": "QUOTATION",
    "sales-order": "SALES ORDER",
    "invoice": "INVOICE",
    "credit-note": "CREDIT NOTE",
    "debit-note": "DEBIT NOTE",
    "payment": "OFFICIAL RECEIPT",
    "purchase-payment": "PAYMENT VOUCHER",
    "delivery-order": "DELIVERY ORDER",
    "purchase-credit-note": "SUPPLIER CREDIT NOTE",
    "purchase-debit-note": "SUPPLIER DEBIT NOTE",
}
ISSUED_TYPES = frozenset(TITLES)

_DEFAULT_CONTACT_LABELS = {
    "payment": "Received from",
    "purchase-payment": "Pay to",
    "delivery-order": "Deliver to",
    "sales-order": "Order for",
    "purchase-credit-note": "Supplier",
    "purchase-debit-note": "Supplier",
}
_PAYMENT_TYPES = frozenset({"payment", "purchase-payment"})
_BANK_TYPES = frozenset({"quotation", "invoice", "debit-note"})

LEFT = 40
RIGHT = 555
CONTENT_WIDTH = RIGHT - LEFT
TOP = 40
BOTTOM = 782  # content must end above the footer rule
ROW_LINE = 10.5
ROW_PAD = 6

_LINE_COLUMNS = (
    ("no", "No", 22, "center"),
    ("description", "Description", 183, "left"),
    ("quantity", "Qty", 45, "right"),
    ("unitPrice", "Unit price", 65, "right"),
    ("discount", "Disc.", 50, "right"),
    ("taxCode", "Tax code", 45, "center"),
    ("tax", "Tax", 45, "right"),
    ("amount", "Amount", 60, "right"),
)
_DELIVERY_COLUMNS = (
    ("no", "No", 22, "center"),
    ("description", "Description", 413, "left"),
    ("quantity", "Qty", 80, "right"),
)
_ALLOCATION_COLUMNS = (
    ("no", "No", 22, "center"),
    ("number", "Document no.", 253, "left"),
    ("date", "Date", 120, "left"),
    ("amount", "Amount", 120, "right"),
)


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _date(value: Any) -> str:
    text = _s(value)
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", text)
    if not match:
        return text
    year, month, day, hour, minute = match.groups()
    out = f"{day}/{month}/{year}"
    return f"{out} {hour}:{minute}" if hour else out


def _nonzero(value: Any) -> bool:
    text = _s(value)
    if not text:
        return False
    try:
        return Decimal(re.sub(r"[^0-9.\-]", "", text)) != 0
    except InvalidOperation:
        return True


class _Renderer:
    def __init__(self, canvas: PdfCanvas, doc: dict, company: dict, doc_type: str, title: str):
        self.c = canvas
        self.doc = doc
        self.company = company
        self.type = doc_type
        self.title = title
        self.number = _s(doc.get("number"))
        self.currency = _s(doc.get("currency")) or "MYR"
        self.y = TOP
        self._columns: tuple = ()

    # -- helpers -----------------------------------------------------------
    def page_break(self, repeat_header: bool = False) -> None:
        self.c.new_page()
        self.y = TOP + 12
        self.c.text(LEFT, self.y, f"{self.title} {self.number} (continued)".strip(), 10, True)
        self.c.text(RIGHT, self.y, _s(self.company.get("name")), 8, align="right")
        self.y += 12
        if repeat_header and self._columns:
            self.table_header(self._columns)

    def ensure(self, height: float) -> None:
        if self.y + height > BOTTOM:
            self.page_break()

    def cell(self, x: float, baseline: float, width: float, value: str, size: float,
             bold: bool, align: str) -> None:
        pad = 3
        while size > 6 and self.c.text_width(value, size, bold) > width - 2 * pad:
            size -= 0.5
        if align == "right":
            self.c.text(x + width - pad, baseline, value, size, bold, "right")
        elif align == "center":
            self.c.text(x + width / 2, baseline, value, size, bold, "center")
        else:
            self.c.text(x + pad, baseline, value, size, bold)

    def segments(self, parts: list[str], width: float, size: float) -> list[str]:
        """Join "Label: value" parts with " | ", breaking lines only between parts."""
        lines: list[str] = []
        current = ""
        for part in parts:
            candidate = f"{current} | {part}" if current else part
            if self.c.text_width(candidate, size) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            wrapped = self.c.wrap(part, width, size)
            lines.extend(wrapped[:-1])
            current = wrapped[-1] if wrapped else ""
        if current:
            lines.append(current)
        return lines

    # -- blocks ------------------------------------------------------------
    def header_rows(self) -> list[tuple[str, str]]:
        doc = self.doc
        revision = doc.get("revision")
        return [
            ("No.", self.number),
            ("Date", _date(doc.get("documentDate"))),
            ("Due date", _date(doc.get("dueDate"))),
            ("Valid until", _date(doc.get("validUntil"))),
            ("Reference", _s(doc.get("referenceNo"))),
            ("Terms", _s(doc.get("paymentTerms"))),
            (_s(doc.get("relatedLabel")) or "Related document", _s(doc.get("relatedNumber"))),
            ("Payment method", _s(doc.get("paymentMethod"))),
            ("Revision", "" if revision is None else str(revision)),
            ("Issued", _date(doc.get("issuedAt"))),
        ]

    def header(self) -> None:
        c, company = self.c, self.company
        left_width = 290
        yl = TOP
        for ln in c.wrap(company.get("name"), left_width, 14, True):
            yl += 15
            c.text(LEFT, yl, ln, 14, True)
        yl += 2
        reg = self.segments([f"{label}: {value}" for label, value in (
            ("SSM", _s(company.get("registrationNumber"))),
            ("TIN", _s(company.get("tin"))),
            ("SST No", _s(company.get("sstNumber"))),
        ) if value], left_width, 8)
        reach = self.segments([f"{label}: {value}" for label, value in (
            ("Tel", _s(company.get("phone"))), ("Email", _s(company.get("email"))),
        ) if value], left_width, 8)
        small = reg + c.wrap(company.get("address"), left_width, 8) + reach
        for ln in small:
            yl += 10
            c.text(LEFT, yl, ln, 8)

        yr = TOP + 15
        c.text(RIGHT, yr, self.title, 16, True, "right")
        yr += 6
        label_x, value_x, value_width = 372, 452, RIGHT - 452
        for label, value in self.header_rows():
            if not value:
                continue
            labels = c.wrap(label, value_x - label_x - 4, 8)
            values = c.wrap(value, value_width, 8.5)
            for i in range(max(len(labels), len(values))):
                yr += 11
                if i < len(labels):
                    c.text(label_x, yr, labels[i], 8)
                if i < len(values):
                    c.text(value_x, yr, values[i], 8.5, i == 0 and label == "No.")
        self.y = max(yl, yr) + 10
        c.line(LEFT, self.y, RIGHT, self.y, 0.75)
        self.y += 4

    def contact(self) -> None:
        """The customer or supplier block. A delivery order prints the delivery address under "Deliver to" and the
        billing address beside it; other documents print the billing address and, when one is set, "Deliver to"."""
        c, doc = self.c, self.doc
        contact = doc.get("contact") or {}
        if not any(_s(v) for v in contact.values()):
            return
        label = _s(doc.get("contactLabel")) or _DEFAULT_CONTACT_LABELS.get(self.type, "Bill to")
        delivery = _s(doc.get("deliveryAddress"))
        is_delivery_order = self.type == "delivery-order"
        width = 300
        top = self.y
        self.y += 12
        c.text(LEFT, self.y, label, 8, True)
        for ln in c.wrap(contact.get("name"), width, 10, True):
            self.y += 12
            c.text(LEFT, self.y, ln, 10, True)
        ids = [] if is_delivery_order else self.segments([f"{k}: {v}" for k, v in (
            ("Code", _s(contact.get("code"))),
            ("SSM", _s(contact.get("registrationNumber"))),
            ("TIN", _s(contact.get("tin"))),
            ("SST No", _s(contact.get("sstNumber"))),
        ) if v], width, 8)
        reach = self.segments([f"{k}: {v}" for k, v in (
            ("Tel", _s(contact.get("phone"))), ("Email", "" if is_delivery_order else _s(contact.get("email"))),
        ) if v], width, 8)
        address = delivery if is_delivery_order and delivery else contact.get("address")
        attention = _s(doc.get("attention"))
        rows = ids + c.wrap(address, width, 8) + reach + (c.wrap(f"Attn: {attention}", width, 8) if attention else [])
        for ln in rows:
            self.y += 10
            c.text(LEFT, self.y, ln, 8)
        # Right column: the billing address on a delivery order, the delivery address on anything else.
        if is_delivery_order and delivery:
            side_label, side_parts = "Bill to", [_s(contact.get("name")), *_s(contact.get("address")).splitlines()]
        elif delivery and not is_delivery_order:
            side_label, side_parts = "Deliver to", delivery.splitlines()
        else:
            side_label, side_parts = "", []
        if side_label:
            x, side_width = LEFT + width + 25, RIGHT - (LEFT + width + 25)
            ys = top + 12
            c.text(x, ys, side_label, 8, True)
            for part in side_parts:
                for ln in c.wrap(part, side_width, 8):
                    ys += 10
                    c.text(x, ys, ln, 8)
            self.y = max(self.y, ys)
        self.y += 10

    def table_header(self, columns: tuple) -> None:
        height = 16
        self.c.rect(LEFT, self.y, CONTENT_WIDTH, height, fill_gray=0.9, stroke=True)
        x = LEFT
        for _key, head, width, align in columns:
            self.cell(x, self.y + 11, width, head, 8, True, align)
            x += width
        self.y += height

    def table(self, columns: tuple, rows: list[dict], wrap_key: str) -> None:
        self._columns = columns
        wrap_width = next(w for k, _h, w, _a in columns if k == wrap_key) - 6
        self.ensure(16 + ROW_LINE + ROW_PAD)
        self.table_header(columns)
        fresh_capacity = int((BOTTOM - (TOP + 24 + 16) - ROW_PAD) // ROW_LINE)
        if not rows:
            rows = [{wrap_key: "No items."}]
        for row in rows:
            remaining = self.c.wrap(row.get(wrap_key), wrap_width, 8.5) or [""]
            first = True
            while remaining:
                capacity = int((BOTTOM - self.y - ROW_PAD) // ROW_LINE)
                if capacity < len(remaining) and (capacity < 1 or (
                        first and len(remaining) <= fresh_capacity)):
                    self.page_break(repeat_header=True)
                    continue
                chunk, remaining = remaining[:capacity], remaining[capacity:]
                baseline = self.y + 10
                x = LEFT
                for key, _head, width, align in columns:
                    if key == wrap_key:
                        for j, ln in enumerate(chunk):
                            self.c.text(x + 3, baseline + j * ROW_LINE, ln, 8.5)
                    elif first:
                        self.cell(x, baseline, width, _s(row.get(key)), 8.5, False, align)
                    x += width
                self.y += len(chunk) * ROW_LINE + ROW_PAD
                self.c.line(LEFT, self.y, RIGHT, self.y, 0.25)
                first = False
                if remaining:
                    self.page_break(repeat_header=True)
        self._columns = ()

    def line_items(self) -> None:
        lines = self.doc.get("lines") or []
        columns = _DELIVERY_COLUMNS if self.type == "delivery-order" else _LINE_COLUMNS
        rows = []
        for i, line in enumerate(lines, 1):
            line = line or {}
            row = {key: _s(line.get(key)) for key, *_ in columns if key != "no"}
            row["no"] = str(i)
            rows.append(row)
        self.y += 6
        self.table(columns, rows, "description")

    def allocations(self) -> None:
        allocations = self.doc.get("allocations") or []
        if not allocations:
            return
        rows = [{"no": str(i), "number": _s(a.get("number")), "date": _date(a.get("date")),
                 "amount": _s(a.get("amount"))} for i, a in enumerate(allocations, 1)]
        self.ensure(14 + 16 + ROW_LINE + ROW_PAD)
        self.y += 14
        self.c.text(LEFT, self.y, "Being payment for", 9, True)
        self.y += 4
        self.table(_ALLOCATION_COLUMNS, rows, "number")

    def totals_and_bank(self, has_lines: bool) -> None:
        if self.type == "delivery-order":
            return
        doc, company, c = self.doc, self.company, self.c
        currency = self.currency
        rows: list[tuple[str, str, bool]] = []
        if self.type in _PAYMENT_TYPES and not has_lines:
            label = "Amount received" if self.type == "payment" else "Amount paid"
            if _s(doc.get("total")):
                rows.append((f"{label} ({currency})", _s(doc.get("total")), True))
        else:
            if _s(doc.get("subtotal")):
                rows.append(("Subtotal", _s(doc.get("subtotal")), False))
            if _nonzero(doc.get("discountTotal")):
                rows.append(("Discount", _s(doc.get("discountTotal")), False))
            if _s(doc.get("taxAmount")):
                rows.append(("Tax (as recorded)", _s(doc.get("taxAmount")), False))
            if _s(doc.get("total")):
                rows.append((f"Total ({currency})", _s(doc.get("total")), True))

        bank: list[tuple[str, bool]] = []
        if self.type in _BANK_TYPES:
            details = [(label, _s(company.get(key))) for label, key in (
                ("Bank", "bankName"), ("Account name", "bankAccountName"),
                ("Account no.", "bankAccountNumber"))]
            details = [(label, value) for label, value in details if value]
            if details:
                bank.append(("Payment to:", True))
                for label, value in details:
                    for ln in c.wrap(f"{label}: {value}", 300, 8.5):
                        bank.append((ln, False))
        if not rows and not bank:
            return
        height = max(len(rows) * 15 + 6, len(bank) * 11) + 14
        self.ensure(height)
        top = self.y + 8
        y = top
        for label, value, bold in rows:
            size = 10 if bold else 9
            y += 15 if bold else 13
            if bold:
                c.line(360, y - 11, RIGHT, y - 11, 0.75)
            c.text(468, y, label, size, bold, "right")
            self.cell(472, y, RIGHT - 472, value, size, bold, "right")
        yb = top
        for text, bold in bank:
            yb += 11
            c.text(LEFT, yb, text, 9 if bold else 8.5, bold)
        self.y = max(y, yb) + 8

    def text_section(self, heading: str, body: Any) -> None:
        lines = self.c.wrap(body, CONTENT_WIDTH, 8.5)
        if not lines:
            return
        self.ensure(16 + min(len(lines), 3) * 11)
        self.y += 16
        self.c.text(LEFT, self.y, heading, 9, True)
        for ln in lines:
            self.ensure(11)
            self.y += 11
            self.c.text(LEFT, self.y, ln, 8.5)
        self.y += 4

    def received_by(self) -> None:
        """Delivery order only: where the customer signs, names, dates and chops on receipt."""
        c = self.c
        height = 92
        self.ensure(height + 14)
        self.y += 14
        top = self.y
        c.rect(LEFT, top, CONTENT_WIDTH, height, stroke=True)
        c.text(LEFT + 8, top + 14, RECEIVED_HEADING, 9, True)
        for i, label in enumerate(("Signature", "Name / IC no.", "Date")):
            base = top + 38 + i * 22
            c.text(LEFT + 8, base, label, 8)
            c.line(LEFT + 80, base + 1, LEFT + 300, base + 1, 0.5)
        chop_x = LEFT + 330
        c.rect(chop_x, top + 22, RIGHT - chop_x - 8, height - 30, stroke=True)
        c.text((chop_x + RIGHT - 8) / 2, top + 22 + (height - 30) / 2 + 3, "Company chop", 8, align="center")
        self.y = top + height + 4

    def footers(self) -> None:
        c = self.c
        revision = self.doc.get("revision")
        left = self.number + (f" rev {revision}" if _s(revision) else "")
        note = SIGN_AND_RETURN if self.type == "delivery-order" else NO_SIGNATURE
        total = c.page_count
        for i in range(total):
            c.set_page(i)
            c.line(LEFT, 796, RIGHT, 796, 0.5)
            c.text(PAGE_WIDTH / 2, 808, note, 7.5, align="center")
            c.text(LEFT, 820, left, 7.5)
            c.text(RIGHT, 820, f"Page {i + 1} of {total}", 7.5, align="right")

    def render(self) -> None:
        self.header()
        self.contact()
        has_lines = bool(self.doc.get("lines"))
        if self.type == "delivery-order" or has_lines or self.type not in _PAYMENT_TYPES:
            self.line_items()
        if self.type in _PAYMENT_TYPES:
            self.allocations()
        self.totals_and_bank(has_lines)
        self.text_section("Notes", self.doc.get("notes"))
        self.text_section("Terms and conditions", self.doc.get("terms"))
        if self.type == "delivery-order":
            self.received_by()
        self.footers()


def render_document_pdf(doc: dict, company: dict, *, watermark: str | None = None) -> bytes:
    """Render an issued (or draft/void, via ``watermark``) document as PDF bytes.

    All values are printed as given (already formatted strings); missing keys
    simply omit their row. Tax and totals are never recomputed here.
    """
    doc = doc or {}
    company = company or {}
    doc_type = _s(doc.get("type")).lower()
    title = TITLES.get(doc_type) or (_s(doc.get("type")).replace("-", " ").upper() or "DOCUMENT")
    number = _s(doc.get("number"))
    canvas = PdfCanvas(title=f"{title} {number}".strip())
    if watermark:
        canvas.set_watermark(watermark)
    _Renderer(canvas, doc, company, doc_type, title).render()
    return canvas.to_bytes()


# ---------------------------------------------------------------------------
# Statement of account (same header, contact block and footer as issued documents)
# ---------------------------------------------------------------------------
STATEMENT_TITLE = "STATEMENT OF ACCOUNT"
_STATEMENT_COLUMNS = (
    ("date", "Date", 55, "left"),
    ("label", "Transaction", 85, "left"),
    ("number", "No.", 80, "left"),
    ("description", "Description", 125, "left"),
    ("debit", "Debit", 55, "right"),
    ("credit", "Credit", 55, "right"),
    ("balance", "Balance", 60, "right"),
)
_AGEING_LABELS = {"current": "Current", "1-30": "1-30 days", "31-60": "31-60 days", "61-90": "61-90 days",
                  "90+": "Over 90 days"}


def _blank_zero(value: Any) -> str:
    return _s(value) if _nonzero(value) else ""


class _StatementRenderer(_Renderer):
    def _fit(self, value: Any, width: float, size: float = 6.5) -> str:
        """Shorten a reference that would not fit its column even at the smallest size, ending in "..."."""
        text = _s(value)
        if self.c.text_width(text, size) <= width:
            return text
        while text and self.c.text_width(text + "...", size) > width:
            text = text[:-1]
        return text + "..."

    def header_rows(self) -> list[tuple[str, str]]:
        doc = self.doc
        return [
            ("Period", f"{_date(doc.get('fromDate'))} to {_date(doc.get('toDate'))}"),
            ("Currency", self.currency),
            ("Account", "Receivable" if doc.get("kind") == "customer" else "Payable"),
        ]

    def transactions(self) -> None:
        doc = self.doc
        rows = [{"date": _date(doc.get("fromDate")), "label": "Opening balance", "number": "", "description": "",
                 "debit": "", "credit": "", "balance": _s(doc.get("openingBalance"))}]
        for line in doc.get("lines") or []:
            rows.append({"date": _date(line.get("date")), "label": _s(line.get("label")), "number": self._fit(line.get("number"), 74),
                         "description": _s(line.get("description")), "debit": _blank_zero(line.get("debit")),
                         "credit": _blank_zero(line.get("credit")), "balance": _s(line.get("balance"))})
        rows.append({"date": _date(doc.get("toDate")), "label": "Closing balance", "number": "", "description": "",
                     "debit": _s(doc.get("totalDebit")), "credit": _s(doc.get("totalCredit")),
                     "balance": _s(doc.get("closingBalance"))})
        self.y += 6
        self.table(_STATEMENT_COLUMNS, rows, "description")

    def summary(self) -> None:
        doc, c = self.doc, self.c
        label = _s(doc.get("balanceLabel")) or "Balance"
        self.ensure(40)
        self.y += 20
        c.line(300, self.y - 13, RIGHT, self.y - 13, 0.75)
        c.text(468, self.y, f"{label} ({self.currency})", 10, True, "right")
        self.cell(472, self.y, RIGHT - 472, _s(doc.get("closingBalance")), 10, True, "right")
        self.y += 8

    def ageing(self) -> None:
        ageing = self.doc.get("ageing") or {}
        buckets = ageing.get("buckets") or list(_AGEING_LABELS)
        totals = ageing.get("totals") or {}
        width = CONTENT_WIDTH // (len(buckets) + 1)
        columns = tuple((key, _AGEING_LABELS.get(key, key), width, "right") for key in buckets)
        columns += (("total", "Open items", CONTENT_WIDTH - width * len(buckets), "right"),)
        self.ensure(14 + 16 + ROW_LINE + ROW_PAD + 30)
        self.y += 16
        self.c.text(LEFT, self.y, f"Ageing at {_date(ageing.get('asOf'))} (days past due)", 9, True)
        self.y += 4
        self._ageing_table(columns, {**{k: _s(totals.get(k)) for k in buckets}, "total": _s(ageing.get("openItems"))})
        extra = []
        unallocated = _s(ageing.get("unallocatedCredits"))
        if _nonzero(unallocated):
            negated = unallocated[1:] if unallocated.startswith("-") else f"-{unallocated}"
            extra.append(("Payments and credits not allocated to an item", negated))
        if _nonzero(ageing.get("unpostedPayments")):
            extra.append(("Matched payments not yet posted (still owed)", _s(ageing.get("unpostedPayments"))))
        if _nonzero(ageing.get("amountDueBelowTotal")):
            extra.append(("Scanned documents: total above the amount due shown", _s(ageing.get("amountDueBelowTotal"))))
        if extra:
            extra.append(("Balance", _s(ageing.get("balance"))))
        if ageing.get("reconciles") is False:
            extra.append(("Difference to the closing balance (review)", _s(ageing.get("difference"))))
        for text, value in extra:
            self.ensure(13)
            self.y += 13
            self.c.text(468, self.y, text, 8.5, False, "right")
            self.cell(472, self.y, RIGHT - 472, value, 8.5, False, "right")

    def _ageing_table(self, columns: tuple, values: dict) -> None:
        self.table_header(columns)
        x = LEFT
        for key, _head, width, align in columns:
            self.cell(x, self.y + 10, width, values.get(key, ""), 8.5, key == "total", align)
            x += width
        self.y += ROW_LINE + ROW_PAD
        self.c.line(LEFT, self.y, RIGHT, self.y, 0.25)

    def render(self) -> None:
        self.header()
        self.contact()
        self.transactions()
        self.summary()
        self.ageing()
        self.text_section("Notes", "Amounts are as recorded in the books (tax as recorded on each document). "
                                   "Please tell us about any difference.")
        self.footers()


def render_statement_pdf(statement: dict, company: dict) -> bytes:
    """Render a statement of account (already formatted values, as the JSON statement returns them)."""
    statement = dict(statement or {})
    company = company or {}
    contact = statement.get("contact") or {}
    statement.setdefault("contactLabel", "Statement for")
    statement["contact"] = {k: contact.get(k) for k in ("name", "code", "registrationNumber", "tin", "address",
                                                         "phone", "email")}
    statement["number"] = f"{_date(statement.get('fromDate'))} - {_date(statement.get('toDate'))}"
    canvas = PdfCanvas(title=f"{STATEMENT_TITLE} {_s(contact.get('name'))}".strip())
    _StatementRenderer(canvas, statement, company, "statement", STATEMENT_TITLE).render()
    return canvas.to_bytes()
