"""Shared pieces of the CSV imports (plan v2, task A2): reading the file, row results, templates.

Every import works the same way: upload a CSV, get a preview with a result for each row
(new, duplicate or error), then confirm with the same file. Nothing is stored at preview
time, so there is nothing to discard: an unconfirmed preview simply expires with the page.
The preview token is a hash of the file, so confirm refuses a file that changed since the
preview.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
_AMOUNT = re.compile(r"^\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?$|^\d+(?:\.\d{1,2})?$")


class ImportFileError(ValueError):
    """The file as a whole cannot be read (wrong encoding, missing columns, too many rows)."""


def template_csv(columns) -> str:
    """The downloadable template: the header row only, in the company pack's column order."""
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerow(columns)
    return buffer.getvalue()


def preview_token(kind: str, tenant_id, content: bytes) -> str:
    return hashlib.sha256(f"{kind}:{tenant_id}:".encode("utf-8") + content).hexdigest()


def read_rows(content: bytes, required, label: str):
    """Return (header, [(source_row, {column: stripped text})]). source_row is the 1-based data row."""
    if not content:
        raise ImportFileError("The file is empty.")
    if len(content) > MAX_IMPORT_BYTES:
        raise ImportFileError("The file is larger than 2 MB. Split it into smaller files.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFileError("Save the file as CSV UTF-8 and try again.") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        header = [name.strip() for name in (reader.fieldnames or [])]
    except csv.Error as exc:
        raise ImportFileError(f"The column-name row is not valid CSV ({exc}).") from exc
    if not header or len(header) != len(set(header)):
        raise ImportFileError("The first row must hold the column names, each used once.")
    missing = [column for column in required if column not in header]
    if missing:
        raise ImportFileError(f"This is not a {label} file: the column(s) {', '.join(missing)} are missing. "
                              "Download the template to see the expected columns.")
    rows, number = [], 0
    try:
        for number, source in enumerate(reader, start=1):
            if None in source:
                raise ImportFileError(f"Row {number} has more cells than there are column names.")
            values = {key.strip(): (value or "").strip() for key, value in source.items() if key is not None}
            if not any(values.values()):
                continue
            if len(rows) >= MAX_IMPORT_ROWS:
                raise ImportFileError(f"The file has more than {MAX_IMPORT_ROWS} rows. Split it into smaller files.")
            rows.append((number, values))
    except csv.Error as exc:
        # The csv module raises on an unclosed quote or a cell over its field size limit (131072 characters).
        raise ImportFileError(f"Row {number + 1} is not valid CSV ({exc}). Check for an unclosed quote "
                              "or a cell that is far too long.") from exc
    if not rows:
        raise ImportFileError("The file has column names but no rows.")
    return header, rows


def parse_amount(text: str):
    """A money amount with at most two decimals (thousands separators allowed). None when empty."""
    if not text:
        return None
    if not _AMOUNT.fullmatch(text):
        raise ValueError("must be a positive amount with at most two decimals, such as 1250.00")
    try:
        return Decimal(text.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation as exc:  # pragma: no cover - the pattern already rules this out
        raise ValueError("is not a valid amount") from exc


def parse_iso_date(text: str):
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("must be a date written as YYYY-MM-DD") from exc


def summary(kind: str, token: str, rows: list[dict], file_errors=None) -> dict:
    """The preview body. A file-level error or any row error blocks confirm; duplicates are skipped."""
    file_errors = list(file_errors or [])
    counts = {status: sum(1 for row in rows if row["status"] == status) for status in ("new", "duplicate", "error")}
    return {"kind": kind, "previewToken": token, "rowCount": len(rows), "newCount": counts["new"],
            "duplicateCount": counts["duplicate"], "errorCount": counts["error"], "fileErrors": file_errors,
            "canConfirm": not file_errors and counts["error"] == 0 and counts["new"] > 0, "rows": rows}


def row_result(source_row: int, status: str, values: dict, errors=(), warnings=(), duplicate_of=None) -> dict:
    return {"sourceRow": source_row, "status": status, "values": values, "errors": list(errors),
            "warnings": list(warnings), "duplicateOf": duplicate_of}
