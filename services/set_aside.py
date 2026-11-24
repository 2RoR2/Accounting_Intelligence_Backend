"""Set-aside documents (documents.excluded_at) take part in nothing until a local admin restores them.

A document is set aside with POST /api/documents/{id}/exclude. Lists and counts leave it out with
`d.excluded_at IS NULL`; actions (review, approval, saving to records, posting, bank matching) refuse it
with SET_ASIDE_DETAIL so the person is told why and how to undo it.
"""
from __future__ import annotations

from fastapi import HTTPException

SET_ASIDE_DETAIL = ("This document is set aside. A local admin can restore it from the Set aside list at the bottom "
                    "of All documents first.")


def record_not_set_aside(alias: str = "r") -> str:
    """SQL predicate: the standardised record `alias` does not belong to a set-aside document."""
    return (f"NOT EXISTS (SELECT 1 FROM documents set_aside WHERE set_aside.id={alias}.document_id "
            f"AND set_aside.tenant_id={alias}.tenant_id AND set_aside.excluded_at IS NOT NULL)")


def refuse_set_aside(document) -> None:
    """Raise 409 when a loaded documents row is set aside."""
    if document and document.get("excluded_at"):
        raise HTTPException(409, SET_ASIDE_DETAIL)


def refuse_set_aside_records(db, tenant_id, record_ids) -> None:
    """Raise 409 when any of the standardised records belongs to a set-aside document."""
    ids = [str(record_id) for record_id in record_ids if record_id]
    if ids and db.execute("""SELECT 1 FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
            WHERE r.tenant_id=%s AND r.id=ANY(%s::uuid[]) AND d.excluded_at IS NOT NULL LIMIT 1""",
                          (tenant_id, ids)).fetchone():
        raise HTTPException(409, SET_ASIDE_DETAIL)
