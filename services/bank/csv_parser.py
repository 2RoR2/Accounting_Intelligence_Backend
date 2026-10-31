"""Strict, in-memory parser for explicitly mapped bank CSV statements."""

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


MAX_ROWS = 20_000
MAX_DESCRIPTION_LENGTH = 2_000
MAX_REFERENCE_LENGTH = 500
_DECIMAL = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _decimal(value):
    value = (value or "").strip()
    if not value:
        return None
    if not _DECIMAL.fullmatch(value):
        raise ValueError("Amounts must use decimal notation with at most two places.")
    try:
        result = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("Amount is not a valid decimal.") from exc
    if not result.is_finite():
        raise ValueError("Amount must be finite.")
    return result


def _date(value, date_format):
    value = (value or "").strip()
    if not value:
        return None
    formats = {
        "iso": "%Y-%m-%d",
        "dmy": "%d/%m/%Y",
        "mdy": "%m/%d/%Y",
    }
    try:
        return datetime.strptime(value, formats[date_format]).date()
    except ValueError:
        return None


def _cell(row, mapping, key):
    column = mapping.get(key)
    return (row.get(column) or "").strip() if column else ""


def _fingerprint(tenant_id, account_ref, row, duplicate_index):
    canonical = "\x1f".join((
        str(tenant_id), account_ref.casefold(),
        row["posted_date"].isoformat() if row["posted_date"] else "",
        row["value_date"].isoformat() if row["value_date"] else "",
        row["description"].casefold(), (row["reference"] or "").casefold(),
        str(row["debit_decimal"] or ""), str(row["credit_decimal"] or ""),
        row["currency"] or "", str(duplicate_index),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_bank_csv(content, mapping, date_format, tenant_id, account_ref):
    """Parse CSV rows into migration-007 fields. Invalid/ambiguous rows need review."""
    if date_format not in {"iso", "dmy", "mdy"}:
        raise ValueError("Choose an explicit date format: iso, dmy, or mdy.")
    if not isinstance(mapping, dict):
        raise ValueError("CSV column mapping must be an object.")
    required = {"posted_date", "description", "debit", "credit", "currency"}
    allowed = required | {"value_date", "reference"}
    if set(mapping) - allowed or not required.issubset(mapping):
        raise ValueError("Map posted_date, description, debit, credit and currency columns.")
    if any(not isinstance(v, str) or not v.strip() for v in mapping.values()):
        raise ValueError("Every mapped CSV column must have a header name.")
    mapping = {key: value.strip() for key, value in mapping.items()}

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Bank CSV must be UTF-8 encoded.") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = reader.fieldnames or []
    if not headers or len(headers) != len(set(headers)):
        raise ValueError("CSV must have one header row with unique column names.")
    missing = sorted({v for v in mapping.values()} - set(headers))
    if missing:
        raise ValueError("Mapped CSV header not found: " + ", ".join(missing))

    rows = []
    duplicate_counts = {}
    for index, source in enumerate(reader, start=1):
        if not any(isinstance(value, str) and value.strip() for value in source.values()):
            continue
        if index > MAX_ROWS:
            raise ValueError(f"CSV exceeds the {MAX_ROWS} row limit.")
        if None in source:
            raise ValueError(f"CSV row {index} has more columns than its header.")

        errors = []
        posted_date = _date(_cell(source, mapping, "posted_date"), date_format)
        value_date = _date(_cell(source, mapping, "value_date"), date_format)
        if not posted_date:
            errors.append("MISSING_OR_INVALID_POSTED_DATE")
        raw_description = _cell(source, mapping, "description")
        if len(raw_description) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(f"CSV row {index} description exceeds {MAX_DESCRIPTION_LENGTH} characters.")
        description = raw_description
        if not description:
            errors.append("MISSING_DESCRIPTION")
        reference = _cell(source, mapping, "reference") or None
        if reference and len(reference) > MAX_REFERENCE_LENGTH:
            raise ValueError(f"CSV row {index} reference exceeds {MAX_REFERENCE_LENGTH} characters.")

        currency = _cell(source, mapping, "currency").upper() or None
        if currency and not _CURRENCY.fullmatch(currency):
            currency = None
            errors.append("MISSING_OR_INVALID_CURRENCY")
        elif not currency:
            errors.append("MISSING_OR_INVALID_CURRENCY")

        try:
            debit = _decimal(_cell(source, mapping, "debit"))
            credit = _decimal(_cell(source, mapping, "credit"))
        except ValueError:
            debit = credit = None
            errors.append("INVALID_AMOUNT")
        if (debit is not None and debit < 0) or (credit is not None and credit < 0):
            debit = credit = None
            errors.append("NEGATIVE_DEBIT_OR_CREDIT")

        direction = None
        signed_amount = None
        if debit is not None and credit is None and debit > 0:
            direction, signed_amount = "money_out", -debit
        elif credit is not None and debit is None and credit > 0:
            direction, signed_amount = "money_in", credit
        else:
            errors.append("AMBIGUOUS_OR_MISSING_DEBIT_CREDIT")

        status = "needs_review" if errors else "imported"
        record = {
            "source_row": index,
            "posted_date": posted_date,
            "value_date": value_date,
            "description": description,
            "reference": reference,
            "debit_decimal": debit,
            "credit_decimal": credit,
            "signed_amount_decimal": signed_amount,
            "direction": direction,
            "currency": currency,
            "status": status,
            "review_reasons": errors,
        }
        duplicate_key = (
            posted_date.isoformat() if posted_date else "", description.casefold(),
            reference.casefold() if reference else "", str(debit or ""),
            str(credit or ""), currency or "",
        )
        duplicate_index = duplicate_counts.get(duplicate_key, 0)
        duplicate_counts[duplicate_key] = duplicate_index + 1
        record["fingerprint"] = _fingerprint(tenant_id, account_ref, record, duplicate_index)
        rows.append(record)

    if not rows:
        raise ValueError("CSV has no transaction rows.")
    mapping_version = "manual-v1:" + hashlib.sha256(
        json.dumps({"mapping": mapping, "date_format": date_format}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return rows, mapping_version
