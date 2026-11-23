"""Plan M4: one name for every stored and displayed document.

    {YYYY-MM-DD}_{CONTACT}_{TYPE}_{NUMBER}.{ext}      e.g. 2026-11-03_KRM_INV_IV-2026-00012.pdf

Date first so a list sorted by name is sorted by date; CONTACT is the contact's short code (unique per company,
editable, suggested from the name); TYPE is a short code for the document type; NUMBER is the document's own
number. The name is built from the record (typed document or approved/extracted fields), never from the upload
file name, so the same document has the same name in every list, download and generated PDF.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

# Typed (ERP) document types.
TYPE_CODES = {
    "quotation": "QT", "sales-order": "SO", "delivery-order": "DO", "invoice": "INV", "credit-note": "CN",
    "debit-note": "DN", "payment": "OR", "refund": "CR", "purchase-order": "PO", "goods-received": "GR",
    "bill": "BILL", "purchase-credit-note": "SCN", "purchase-debit-note": "SDN", "purchase-payment": "PV",
    "purchase-refund": "SR", "money-in": "MI", "money-out": "MO", "bank-transfer": "FT", "journal-entry": "JE",
}
# Captured (Inbox) documents: (stored kind, category) -> type code. What KNT receives is a purchase document.
CAPTURED_CODES = {
    ("invoice", "accounts_payable"): "BILL", ("bill", "accounts_payable"): "BILL", ("receipt", "accounts_payable"): "RCPT",
    ("credit_note", "accounts_payable"): "SCN", ("debit_note", "accounts_payable"): "SDN",
    ("invoice", "accounts_receivable"): "INV", ("credit_note", "accounts_receivable"): "CN",
    ("debit_note", "accounts_receivable"): "DN", ("receipt", "accounts_receivable"): "OR",
    ("purchase_order", "accounts_payable"): "PO", ("delivery_order", "accounts_payable"): "DO",
    ("bank_statement", "accounts_payable"): "STMT", ("bank_statement", "accounts_receivable"): "STMT",
}
# Words that say what kind of company it is, not which one (skipped when suggesting a code).
_LEGAL_WORDS = {"sdn", "bhd", "berhad", "plt", "llp", "ltd", "limited", "inc", "co", "company", "the", "and",
                "enterprise", "enterprises", "trading", "services", "m", "s"}
_PLACEHOLDER_NUMBERS = {"", "n/a", "na", "none", "null", "unknown", "-"}


def _component(value, fallback: str, limit: int = 40) -> str:
    text = re.sub(r"[^A-Za-z0-9.-]+", "-", str(value or "").strip()).strip(".-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:limit].strip(".-") or fallback


def suggest_contact_code(name: str | None, taken=()) -> str:
    """A short upper-case code from a contact name, not in `taken` (case-insensitive).

    "Kedai Runcit Maju Sdn. Bhd." -> "KRM"; "Pulasan Ketapang Hardware Enterprise" -> "PKH";
    one significant word -> its first four letters ("Tenaga" -> "TENA"). A clash adds 2, 3, ...
    """
    words = [w for w in re.findall(r"[A-Za-z0-9]+", str(name or "")) if w.lower() not in _LEGAL_WORDS]
    if not words:
        words = re.findall(r"[A-Za-z0-9]+", str(name or "")) or ["CONTACT"]
    base = (words[0][:4] if len(words) == 1 else "".join(w[0] for w in words[:4])).upper()
    used = {str(t).strip().upper() for t in taken if t}
    if base not in used:
        return base
    n = 2
    while f"{base}{n}" in used:
        n += 1
    return f"{base}{n}"


def type_code(document_type: str | None = None, *, kind: str | None = None, category: str | None = None) -> str:
    if document_type:
        return TYPE_CODES.get(document_type, _component(document_type, "DOC", 8).upper())
    code = CAPTURED_CODES.get((kind or "", category or ""))
    if code:
        return code
    return _component(kind, "DOC", 8).upper().replace("_", "")


def display_name(document_date, contact_code, type_code_value: str, number, ext: str = ".pdf") -> str:
    """The rule itself. Missing parts get a placeholder so the shape (and the date sort) never breaks."""
    if isinstance(document_date, datetime):
        document_date = document_date.date()
    if isinstance(document_date, date):
        day = document_date.isoformat()
    else:
        text = str(document_date or "").strip()[:10]
        day = text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else "0000-00-00"
    contact = _component(contact_code, "NOCONTACT", 12).upper()
    raw_number = str(number or "").strip()
    number_part = "NONUMBER" if raw_number.casefold() in _PLACEHOLDER_NUMBERS else _component(raw_number, "NONUMBER")
    suffix = (ext if ext.startswith(".") else f".{ext}").lower() if ext else ""
    if suffix and not re.fullmatch(r"\.[a-z0-9]{1,6}", suffix):
        suffix = ".bin"
    return f"{day}_{contact}_{_component(type_code_value, 'DOC', 8).upper()}_{number_part}{suffix}"


def extension_of(filename: str | None, default: str = ".pdf") -> str:
    suffix = Path(str(filename or "")).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,6}", suffix or "") else default


# ------------------------------------------------------------------ database helpers

def ensure_contact_code(db, tenant_id, kind: str, contact_id) -> str | None:
    """The contact's code, suggesting and saving one when it has none (kind: 'supplier' or 'customer')."""
    if not contact_id or kind not in {"supplier", "customer"}:
        return None
    table, name_column = ("vendors", "vendor_name") if kind == "supplier" else ("customers", "customer_name")
    row = db.execute(f"SELECT {name_column} AS name, contact_code FROM {table} WHERE id=%s AND tenant_id=%s",
                     (contact_id, tenant_id)).fetchone()
    if not row:
        return None
    if row["contact_code"] and row["contact_code"].strip():
        return row["contact_code"].strip().upper()
    # Serialise suggestions per company so two requests cannot pick the same code.
    db.execute("SELECT pg_advisory_xact_lock(hashtext('contact_code:' || %s::text))", (str(tenant_id),))
    current = db.execute(f"SELECT contact_code FROM {table} WHERE id=%s AND tenant_id=%s", (contact_id, tenant_id)).fetchone()
    if current and current["contact_code"] and current["contact_code"].strip():
        return current["contact_code"].strip().upper()
    taken = [r["code"] for r in db.execute(f"""SELECT upper(btrim(contact_code)) AS code FROM {table}
        WHERE tenant_id=%s AND contact_code IS NOT NULL""", (tenant_id,)).fetchall()]
    code = suggest_contact_code(row["name"], taken)
    db.execute(f"UPDATE {table} SET contact_code=%s, updated_at=now() WHERE id=%s AND tenant_id=%s",
               (code, contact_id, tenant_id))
    return code


class CodeBook:
    """Contact codes of one company, loaded once for a list (normalised name -> code)."""

    def __init__(self, db, tenant_id):
        from validation.matching.vendor_matching import normalize_vendor_name
        self._normalize = normalize_vendor_name
        self._codes = {}
        for kind, table, column in (("supplier", "vendors", "vendor_name"), ("customer", "customers", "customer_name")):
            book = {}
            for r in db.execute(f"SELECT {column} AS name, contact_code FROM {table} WHERE tenant_id=%s", (tenant_id,)).fetchall():
                name = r.get("name") if hasattr(r, "get") else None
                key = normalize_vendor_name(name or "")
                if key:
                    book.setdefault(key, []).append((r.get("contact_code") or "").strip().upper() or suggest_contact_code(name))
            self._codes[kind] = book

    def code(self, kind: str, name: str | None) -> str | None:
        if not name:
            return None
        found = self._codes.get(kind, {}).get(self._normalize(name) or "", [])
        return found[0] if len(found) == 1 else suggest_contact_code(name)


def code_for_party_name(db, tenant_id, kind: str, name: str | None) -> str | None:
    """Code of the one contact with this name (normalised), else a suggestion that is not saved."""
    if not name:
        return None
    from validation.matching.vendor_matching import normalize_vendor_name
    table, name_column = ("vendors", "vendor_name") if kind == "supplier" else ("customers", "customer_name")
    wanted = normalize_vendor_name(name)
    rows = db.execute(f"SELECT {name_column} AS name,contact_code FROM {table} WHERE tenant_id=%s",
                      (tenant_id,)).fetchall()
    matches = [r for r in rows if wanted and normalize_vendor_name(r["name"] or "") == wanted]
    if len(matches) == 1:
        return matches[0]["contact_code"] or suggest_contact_code(matches[0]["name"])
    return suggest_contact_code(name)
