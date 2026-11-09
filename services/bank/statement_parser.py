"""Normalize CSV, Excel, and text-based PDF bank statements for preview/import."""

from __future__ import annotations

import csv
import io
import re
import tempfile
from datetime import date, datetime
from pathlib import Path

from services.bank.csv_parser import parse_bank_csv


SUPPORTED_STATEMENT_EXTENSIONS = {".csv", ".xlsx", ".pdf"}
_DATE = re.compile(r"(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})")
_MONEY = re.compile(
    r"(?P<currency>MYR|RM|USD|SGD|EUR|GBP|AUD|JPY|CNY)?\s*"
    r"(?P<value>\(?-?\d[\d,]*(?:\.\d{1,2})?\)?)"
)


def _extension(filename: str | None) -> str:
    return Path(str(filename or "")).suffix.lower()


def _clean_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value).strip()


def _unique_headers(values) -> list[str]:
    result = []
    seen: dict[str, int] = {}
    for index, value in enumerate(values, start=1):
        base = _clean_cell(value) or f"Column {index}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        result.append(base if count == 0 else f"{base} ({count + 1})")
    return result


def _looks_like_header(values: list[str]) -> bool:
    normalized = " ".join(value.casefold() for value in values)
    has_date = any(token in normalized for token in ("date", "posted", "transaction"))
    has_description = any(token in normalized for token in ("description", "details", "narrative", "particular"))
    has_amount = any(token in normalized for token in ("debit", "credit", "amount", "withdrawal", "deposit"))
    return has_date and has_description and has_amount


def _rows_to_csv(headers: list[str], rows: list[list[str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _excel_table(content: bytes) -> tuple[list[str], bytes]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - requirements installs this
        raise ValueError("Excel preview requires the openpyxl package.") from exc
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("The Excel bank statement could not be opened.") from exc
    try:
        sheet = workbook.active
        values = [[_clean_cell(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    values = [row for row in values if any(row)]
    header_index = next((i for i, row in enumerate(values[:30]) if _looks_like_header(row)), None)
    if header_index is None:
        raise ValueError("Excel must contain a header row with date, description, and debit/credit or amount columns.")
    headers = _unique_headers(values[header_index])
    width = len(headers)
    rows = [(row + [""] * width)[:width] for row in values[header_index + 1:]]
    return headers, _rows_to_csv(headers, rows)


def _pdf_text(content: bytes) -> str:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - requirements installs this
        raise ValueError("PDF preview requires the pypdfium2 package.") from exc
    source_name = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as source:
            source.write(content)
            source_name = source.name
        document = pdfium.PdfDocument(source_name)
        pages = []
        for page in document:
            text_page = page.get_textpage()
            pages.append(text_page.get_text_range())
        text = "\n".join(pages)
        if text.strip():
            return text
        return _pdf_ocr_text(document)
    except Exception as exc:
        raise ValueError("The PDF bank statement could not be read.") from exc
    finally:
        if source_name:
            try:
                Path(source_name).unlink(missing_ok=True)
            except OSError:
                pass


def _pdf_ocr_text(document) -> str:
    """Read scanned PDF pages when the document has no selectable text."""
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:  # pragma: no cover - requirements installs this
        raise ValueError("This PDF has no selectable text and OCR is unavailable.") from exc
    engine = RapidOCR()
    lines = []
    for page_number, page in enumerate(document):
        if page_number >= 50:
            break
        bitmap = page.render(scale=2.0)
        try:
            result = engine(bitmap.to_pil())
            boxes = result.boxes if result.boxes is not None else []
            texts = result.txts if result.txts is not None else []
            items = []
            for box, text in zip(boxes, texts):
                points = box.tolist()
                left = min(point[0] for point in points)
                top = min(point[1] for point in points)
                bottom = max(point[1] for point in points)
                items.append((top, bottom, left, str(text)))
            items.sort(key=lambda item: (item[0], item[2]))
            grouped: list[list[tuple[float, float, float, str]]] = []
            for item in items:
                if not grouped or abs(item[0] - sum(value[0] for value in grouped[-1]) / len(grouped[-1])) > max(10, (item[1] - item[0]) * 0.8):
                    grouped.append([])
                grouped[-1].append(item)
            lines.extend(" ".join(item[3] for item in group) for group in grouped)
        finally:
            bitmap.close()
    if not lines:
        raise ValueError("The PDF has no readable transaction rows. Use a bank statement with selectable text.")
    return "\n".join(lines)


def _money_value(match: re.Match) -> str:
    value = match.group("value").replace(",", "")
    if value.startswith("(") and value.endswith(")"):
        return "-" + value[1:-1]
    return value


def _pdf_table(content: bytes) -> tuple[list[str], bytes]:
    text = _pdf_text(content)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    parsed: list[list[str]] = []
    has_debit_credit = False
    currencies: list[str] = []
    for line in lines:
        date_match = _DATE.search(line)
        if not date_match:
            continue
        remainder = line[date_match.end():].strip(" -|\t")
        money_matches = list(_MONEY.finditer(remainder))
        if not money_matches:
            continue
        first_money = money_matches[0]
        description = remainder[:first_money.start()].strip(" -|\t") or "Bank transaction"
        values = [_money_value(match) for match in money_matches[-2:]]
        currency = next((match.group("currency") for match in reversed(money_matches) if match.group("currency")), "")
        currency = "MYR" if currency == "RM" else currency
        if currency:
            currencies.append(currency)
        direction_hint = remainder.casefold()
        if len(values) >= 2:
            debit, credit = values[-2], values[-1]
            has_debit_credit = True
        else:
            amount = values[-1]
            if any(token in direction_hint for token in (" debit", " dr", "withdrawal", "out")) and not amount.startswith("-"):
                amount = "-" + amount
            parsed.append([date_match.group("date"), description, amount, currency])
            continue
        parsed.append([date_match.group("date"), description, debit, credit, currency])
    if not parsed:
        raise ValueError("The PDF has no readable transaction rows. Use a bank statement PDF with clear transaction rows or an Excel/CSV export.")
    if has_debit_credit:
        headers = ["Date", "Description", "Debit", "Credit", "Currency"]
        rows = [row if len(row) == 5 else [row[0], row[1], "", row[2], row[3]] for row in parsed]
    else:
        headers = ["Date", "Description", "Amount", "Currency"]
        rows = parsed
    return headers, _rows_to_csv(headers, rows)


def inspect_statement(content: bytes, filename: str) -> dict:
    extension = _extension(filename)
    if extension == ".csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Bank CSV must be UTF-8 encoded.") from exc
        headers = next(csv.reader(io.StringIO(text)), [])
        if not headers:
            raise ValueError("The CSV has no header row.")
    elif extension == ".xlsx":
        headers, _ = _excel_table(content)
    elif extension == ".pdf":
        headers, _ = _pdf_table(content)
    else:
        raise ValueError("Bank statements must be CSV, Excel (.xlsx), or PDF files.")
    return {"format": extension[1:], "headers": _unique_headers(headers)}


def parse_bank_statement(content, filename, mapping, date_format, tenant_id, account_ref, default_currency="MYR"):
    extension = _extension(filename)
    if extension == ".csv":
        canonical = content
    elif extension == ".xlsx":
        _, canonical = _excel_table(content)
    elif extension == ".pdf":
        _, canonical = _pdf_table(content)
    else:
        raise ValueError("Bank statements must be CSV, Excel (.xlsx), or PDF files.")
    return parse_bank_csv(canonical, mapping, date_format, tenant_id, account_ref, default_currency)
