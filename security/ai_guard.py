"""Security boundaries for untrusted document text and model responses.

The extraction pipeline is deliberately conservative: document text is data,
never instructions; model output is a candidate, never an accounting command.
This module contains small dependency-free guards so the same rules can be
used by tests, local extraction and hosted-model extraction.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from services.extraction.lite.sanitise import sanitise_ocr_text


ALLOWED_DOCUMENT_TYPES = {"invoice", "bill", "receipt", "credit_note", "debit_note", "unknown"}
ALLOWED_FIELD_NAMES = {
    "seller_name", "seller_address", "seller_registration_id", "seller_sst_id", "seller_tin",
    "seller_tax_id", "buyer_name", "buyer_address", "buyer_tax_id", "note_reason",
    "rounding", "document_number", "document_date", "due_date", "transaction_time",
    "purchase_order_number", "payment_terms", "payment_method", "account_number",
    "billing_period", "original_invoice_number", "currency", "subtotal", "discount",
    "tax_amount", "total_amount", "amount_due",
}
ALLOWED_LINE_ITEM_FIELDS = {"description", "quantity", "unit_price", "line_total"}
MAX_FIELD_VALUE_CHARS = 500
MAX_LINE_ITEMS = 200
MAX_RESPONSE_CHARS = 1_000_000

# These are detection signals, not a reason to discard the source. A reviewer
# must still be able to see the original evidence and decide what it means.
PROMPT_INJECTION_PATTERNS = (
    re.compile(r"\b(ignore|disregard|forget|override)\b.{0,80}\b(previous|prior|system|instructions?)\b", re.I),
    re.compile(r"\b(system prompt|developer message|reveal (?:the )?prompt|jailbreak)\b", re.I),
    re.compile(r"\b(send|upload|email|forward|publish|share)\b.{0,80}\b(data|document|invoice|records?)\b", re.I),
    re.compile(r"\b(approve|post|pay|execute|call|run)\b.{0,60}\b(payment|journal|sql|code|command|tool)\b", re.I),
)


class AIGuardError(ValueError):
    """A safe, operator-facing validation error without document content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def sha256_json(value: Any) -> str:
    """Hash canonical metadata, never log or return the raw payload."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def scan_prompt_injection(text: str) -> list[str]:
    findings: list[str] = []
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            findings.append("PROMPT_INJECTION_SIGNAL")
    return sorted(set(findings))


def sanitize_document_text(text: str, *, max_chars: int) -> tuple[str, list[str]]:
    """Make OCR text safe to place in a prompt while retaining accounting evidence.

    We do not silently delete suspicious business text. It is delimited as data,
    control characters are removed, and a finding forces human review upstream.
    """
    try:
        cleaned, _counts = sanitise_ocr_text(text, max_chars=max_chars)
    except (TypeError, ValueError):
        raise AIGuardError("AI_INPUT_INVALID") from None
    findings = scan_prompt_injection(cleaned)
    # Keep the prompt structure unambiguous without inserting zero-width
    # characters that would be reintroduced after sanitisation.
    cleaned = cleaned.replace("```", "'''")
    return cleaned, findings


def sanitize_document_text_with_metadata(text: str, *, max_chars: int) -> tuple[str, list[str], dict[str, int]]:
    """Return sanitised text, injection findings and non-sensitive counts."""
    try:
        cleaned, counts = sanitise_ocr_text(text, max_chars=max_chars)
    except (TypeError, ValueError):
        raise AIGuardError("AI_INPUT_INVALID") from None
    findings = scan_prompt_injection(cleaned)
    return cleaned.replace("```", "'''"), findings, counts


def _string_or_null(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AIGuardError("AI_RESPONSE_FIELD_TYPE_INVALID")
    if len(value) > MAX_FIELD_VALUE_CHARS:
        raise AIGuardError("AI_RESPONSE_FIELD_TOO_LONG")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise AIGuardError("AI_RESPONSE_CONTROL_CHARACTER")
    return value


def validate_extraction_response(answer: Any) -> dict[str, Any]:
    """Validate and normalise the only model contract used by lite extraction."""
    if not isinstance(answer, dict):
        raise AIGuardError("AI_RESPONSE_SCHEMA_INVALID")
    if len(json.dumps(answer, ensure_ascii=False, default=str)) > MAX_RESPONSE_CHARS:
        raise AIGuardError("AI_RESPONSE_TOO_LARGE")
    # The model contract is intentionally closed. A top-level command-like key
    # or a legacy field layout must not be silently accepted.
    allowed_top_level = {"document_type", "fields", "line_items"}
    if set(answer) - allowed_top_level:
        raise AIGuardError("AI_RESPONSE_UNKNOWN_TOP_LEVEL_KEY")

    document_type = answer.get("document_type", "unknown")
    if not isinstance(document_type, str):
        raise AIGuardError("AI_RESPONSE_DOCUMENT_TYPE_INVALID")
    document_type = document_type.casefold().replace("-", "_").replace(" ", "_")
    # An unrecognised heading is a valid safe outcome: extraction must return
    # an unknown type and leave the document for a human instead of failing
    # the whole processing job.
    if document_type not in ALLOWED_DOCUMENT_TYPES:
        document_type = "unknown"

    supplied_fields = answer.get("fields")
    if not isinstance(supplied_fields, dict):
        raise AIGuardError("AI_RESPONSE_FIELDS_INVALID")
    unknown_fields = set(supplied_fields) - ALLOWED_FIELD_NAMES
    if unknown_fields:
        raise AIGuardError("AI_RESPONSE_UNKNOWN_FIELD")

    fields: dict[str, dict[str, str | None]] = {}
    for name, cell in supplied_fields.items():
        if not isinstance(cell, dict):
            raise AIGuardError("AI_RESPONSE_FIELD_SCHEMA_INVALID")
        if set(cell) - {"value", "raw"}:
            raise AIGuardError("AI_RESPONSE_UNKNOWN_FIELD_KEY")
        fields[name] = {"value": _string_or_null(cell.get("value")), "raw": _string_or_null(cell.get("raw"))}

    items = answer.get("line_items", [])
    if items is None:
        items = []
    if not isinstance(items, list) or len(items) > MAX_LINE_ITEMS:
        raise AIGuardError("AI_RESPONSE_LINE_ITEMS_INVALID")
    line_items: list[dict[str, str | None]] = []
    for item in items:
        if not isinstance(item, dict) or set(item) - ALLOWED_LINE_ITEM_FIELDS:
            raise AIGuardError("AI_RESPONSE_LINE_ITEM_SCHEMA_INVALID")
        line_items.append({name: _string_or_null(item.get(name)) for name in ALLOWED_LINE_ITEM_FIELDS})
    return {"document_type": document_type, "fields": fields, "line_items": line_items}


def confidence_for_source(*, evidence_count: int, verified: bool, ambiguous: bool = False, suspicious: bool = False) -> float | None:
    """A deterministic, explainable confidence score for reviewer triage."""
    if evidence_count <= 0:
        return None
    score = 0.90 if verified else 0.68
    if evidence_count > 1:
        score += 0.04
    if ambiguous:
        score -= 0.15
    if suspicious:
        score -= 0.20
    return round(max(0.0, min(0.99, score)), 3)
