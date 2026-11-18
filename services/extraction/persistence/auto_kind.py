"""Uploads sent with kind=auto: the document type is set by classification after reading.

The documents.kind CHECK allows only the five real kinds, so an auto upload stores
PLACEHOLDER_KIND and a neutral code (DOC_ + 12 hex characters of the document id). That
code prefix is the "type not detected yet" flag: no other path writes it, because every
other upload leaves document_code empty and the trigger assigns KIND_<number>.

When classification returns a supported kind, apply_classified_kind sets the kind and
swaps the DOC_ code for the normal KIND_<number> code from that kind's sequence. When
classification fails the document keeps its DOC_ code, and approval stays blocked by the
existing "Unknown or conflicting document classification" rule.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

AUTO_KIND = "auto"
PLACEHOLDER_KIND = "invoice"
PENDING_CODE_PREFIX = "DOC_"
KIND_SEQUENCES = {
    "invoice": "document_invoice_seq",
    "bill": "document_bill_seq",
    "receipt": "document_receipt_seq",
    "credit_note": "document_credit_note_seq",
    "debit_note": "document_debit_note_seq",
}


def pending_document_code(document_id: UUID | str) -> str:
    return PENDING_CODE_PREFIX + UUID(str(document_id)).hex[:12].upper()


def kind_pending(document_code: Any) -> bool:
    return isinstance(document_code, str) and document_code.startswith(PENDING_CODE_PREFIX)


def read_kind(row: dict[str, Any]) -> dict[str, Any]:
    """Kind fields for read DTOs: a DOC_ (type not detected yet) document reads as AUTO, never as its placeholder."""
    if kind_pending(row.get("document_code")):
        return {"kind": AUTO_KIND.upper(), "kindDetected": False}
    return {"kind": str(row["kind"]).upper(), "kindDetected": True}


def classified_kind(classification: dict[str, Any] | None) -> str | None:
    classification = classification or {}
    value = classification.get("value")
    if classification.get("status") == "classified" and value in KIND_SEQUENCES:
        return value
    return None


def apply_classified_kind(db, document: dict[str, Any], classification: dict[str, Any] | None) -> dict[str, Any]:
    """Store the classified kind; give an auto upload its KIND_<number> code. Returns the updated row values."""
    kind = classified_kind(classification)
    if kind is None:
        return document
    if kind_pending(document.get("document_code")):
        # Sequence names come only from KIND_SEQUENCES, never from input.
        row = db.execute(
            f"UPDATE documents SET kind=%s, document_code=upper(%s) || '_' || nextval('{KIND_SEQUENCES[kind]}')::text "
            "WHERE id=%s AND tenant_id=%s AND document_code LIKE 'DOC\\_%%' RETURNING kind, document_code",
            (kind, kind, document["id"], document["tenant_id"]),
        ).fetchone()
        if row:
            return {**document, "kind": row["kind"], "document_code": row["document_code"]}
        return document
    db.execute("UPDATE documents SET kind=%s WHERE id=%s AND tenant_id=%s", (kind, document["id"], document["tenant_id"]))
    return {**document, "kind": kind}
