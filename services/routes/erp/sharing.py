"""Plan L7: read-only share links for ready documents, and the MyInvois consolidation record. Both off by default.

Share links (ENABLE_SHARE_LINKS=1): only a "ready" document, an unguessable token (32 random bytes; only its
SHA-256 is stored), an expiry (1-30 days, default 7) and revoke. The public page shows the document read-only,
without internal ids; a voided, revoked or expired link answers 410. Links are handed over by the user (copy);
nothing is e-mailed from here.

MyInvois (ENABLE_MYINVOIS_PREP=1): records which ready documents a consolidated e-invoice would cover for a
date range, so nothing is consolidated twice. It is a prepared record only: nothing is sent to LHDN, and the
responses say so. Live statutory submission is out of scope.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import date
from decimal import Decimal

from main import *  # noqa: F401,F403  (Request, HTTPException, connect)
from pydantic import BaseModel, Field

from .common import audit, erp_user, money, parse_uuid
from .documents import LABELS, _type

PREPARED_ONLY = "Prepared only, not submitted to LHDN."
MYINVOIS_TYPES = {"invoice", "credit-note", "bill", "purchase-credit-note", "minipos-order"}


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _require(name: str, what: str):
    if not _enabled(name):
        raise HTTPException(404, f"{what} are switched off on this server.")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ShareInput(BaseModel):
    expiresInDays: int = Field(default=7, ge=1, le=30)
    recipient: str | None = Field(default=None, max_length=254)


class ConsolidationInput(BaseModel):
    documentType: str
    dateFrom: date
    dateTo: date
    documentIds: list[str] = Field(min_length=1, max_length=500)
    idempotencyKey: str = Field(min_length=1, max_length=300)


def sharing_status(request: Request):
    erp_user(request)
    return {"shareLinks": _enabled("ENABLE_SHARE_LINKS"), "myinvoisPrep": _enabled("ENABLE_MYINVOIS_PREP"),
            "myinvoisNote": PREPARED_ONLY}


def _share_dto(row, now):
    state = "revoked" if row["revoked_at"] else "expired" if row["expires_at"] <= now else "active"
    return {"id": str(row["id"]), "recipient": row["recipient"], "status": state, "expiresAt": str(row["expires_at"]),
            "createdAt": str(row["created_at"]), "lastOpenedAt": str(row["last_opened_at"]) if row["last_opened_at"] else None,
            "openCount": row["open_count"]}


def _now(db):
    return db.execute("SELECT now() AS now").fetchone()["now"]


def create_share(document_type: str, document_id: str, data: ShareInput, request: Request):
    _require("ENABLE_SHARE_LINKS", "Share links")
    _type(document_type)
    user = erp_user(request)
    token = secrets.token_urlsafe(32)
    base = (os.getenv("APP_PUBLIC_URL") or os.getenv("FRONTEND_URL") or "http://127.0.0.1:3000").rstrip("/")
    with connect() as db:
        doc = db.execute("SELECT id,status FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s",
                         (parse_uuid(document_id, "document id"), user["tenant_id"], document_type)).fetchone()
        if not doc:
            raise HTTPException(404, "Document not found.")
        if doc["status"] != "ready":
            raise HTTPException(409, "Only a ready document can be shared.")
        row = db.execute("""INSERT INTO erp_document_shares(tenant_id,document_id,token_hash,channel,recipient,expires_at,created_by)
            VALUES (%s,%s,%s,'link',%s,now() + make_interval(days => %s),%s) RETURNING *""",
                         (user["tenant_id"], doc["id"], _hash(token), data.recipient, data.expiresInDays, user["id"])).fetchone()
        audit(db, user, "erp.document_shared", str(doc["id"]), {"shareId": str(row["id"]), "days": data.expiresInDays})
        now = _now(db)
    # The token is shown once; only its hash is kept.
    return {**_share_dto(row, now), "url": f"{base}/share/{token}"}


def list_shares(document_type: str, document_id: str, request: Request):
    _require("ENABLE_SHARE_LINKS", "Share links")
    _type(document_type)
    user = erp_user(request)
    with connect() as db:
        rows = db.execute("""SELECT s.* FROM erp_document_shares s JOIN erp_document_headers h ON h.id=s.document_id AND h.tenant_id=s.tenant_id
            WHERE s.tenant_id=%s AND s.document_id=%s AND h.document_type=%s ORDER BY s.created_at DESC""",
                          (user["tenant_id"], parse_uuid(document_id, "document id"), document_type)).fetchall()
        now = _now(db)
    return [_share_dto(row, now) for row in rows]


def revoke_share(share_id: str, request: Request):
    _require("ENABLE_SHARE_LINKS", "Share links")
    user = erp_user(request)
    with connect() as db:
        row = db.execute("""UPDATE erp_document_shares SET revoked_at=COALESCE(revoked_at, now())
            WHERE id=%s AND tenant_id=%s RETURNING *""", (parse_uuid(share_id, "share id"), user["tenant_id"])).fetchone()
        if not row:
            raise HTTPException(404, "Share link not found.")
        audit(db, user, "erp.share_revoked", str(row["document_id"]), {"shareId": str(row["id"])})
        now = _now(db)
    return _share_dto(row, now)


def public_share(token: str):
    """No sign-in: the token is the permission. Read-only, no internal ids."""
    _require("ENABLE_SHARE_LINKS", "Share links")
    if not 20 <= len(token) <= 100:
        raise HTTPException(404, "Share link not found.")
    with connect() as db:
        row = db.execute("""SELECT s.id AS share_id,s.expires_at,s.revoked_at,h.*,t.name AS company_name
            FROM erp_document_shares s
            JOIN erp_document_headers h ON h.id=s.document_id AND h.tenant_id=s.tenant_id
            JOIN tenants t ON t.id=s.tenant_id
            WHERE s.token_hash=%s""", (_hash(token),)).fetchone()
        if not row:
            raise HTTPException(404, "Share link not found.")
        if row["revoked_at"] or row["expires_at"] <= _now(db) or row["status"] != "ready":
            raise HTTPException(410, "This share link is no longer available.")
        db.execute("UPDATE erp_document_shares SET last_opened_at=now(),open_count=open_count+1 WHERE id=%s", (row["share_id"],))
    payload = row["payload"] or {}
    lines = [{"description": l.get("description") or "", "quantity": str(l.get("quantity", "1")),
              "unitPrice": money(l.get("unitPrice", 0)), "discount": money(l.get("discount", 0)), "tax": money(l.get("tax", 0))}
             for l in payload.get("lines", [])]
    return {"companyName": row["company_name"], "documentType": row["document_type"], "documentLabel": LABELS[row["document_type"]],
            "number": row["document_number"], "documentDate": str(row["document_date"]),
            "dueDate": str(row["due_date"]) if row["due_date"] else None, "contactName": row["contact_name"],
            "referenceNo": row["reference_no"], "description": row["description"], "currency": row["currency"],
            "lines": lines, "subtotal": money(row["subtotal"]), "taxAmount": money(row["tax_amount"]),
            "total": money(row["total_amount"]), "expiresAt": str(row["expires_at"])}


def prepare_consolidation(data: ConsolidationInput, request: Request):
    _require("ENABLE_MYINVOIS_PREP", "MyInvois preparation records")
    if data.documentType not in MYINVOIS_TYPES:
        raise HTTPException(422, "This document type cannot be part of a consolidated e-invoice.")
    if data.dateTo < data.dateFrom:
        raise HTTPException(422, "The end date must be on or after the start date.")
    user = erp_user(request, {"local_admin"})
    with connect() as db:
        existing = db.execute("""SELECT s.*, (SELECT count(*) FROM myinvois_consolidated_documents d WHERE d.submission_id=s.id) AS n
            FROM myinvois_consolidated_submissions s WHERE s.tenant_id=%s AND s.idempotency_key=%s""",
                              (user["tenant_id"], data.idempotencyKey)).fetchone()
        if existing:
            return {"id": str(existing["id"]), "documentType": existing["document_type"], "status": existing["status"],
                    "documentCount": existing["n"], "totalAmount": money(existing["total_amount"]), "note": PREPARED_ONLY,
                    "idempotent": True}
        ids = list(dict.fromkeys(parse_uuid(raw, "document id") for raw in data.documentIds))
        rows = db.execute("""SELECT id,document_number,document_date,status,total_amount FROM erp_document_headers
            WHERE tenant_id=%s AND document_type=%s AND id = ANY(%s) FOR UPDATE""",
                          (user["tenant_id"], data.documentType, ids)).fetchall()
        if len(rows) != len(ids):
            raise HTTPException(404, "One or more documents were not found in this company.")
        for row in rows:
            if row["status"] != "ready":
                raise HTTPException(409, f"{row['document_number']} must be ready before it is consolidated.")
            if not data.dateFrom <= row["document_date"] <= data.dateTo:
                raise HTTPException(422, f"{row['document_number']} is outside the chosen dates.")
        taken = db.execute("""SELECT h.document_number FROM myinvois_consolidated_documents d
            JOIN erp_document_headers h ON h.id=d.document_id AND h.tenant_id=d.tenant_id
            WHERE d.tenant_id=%s AND d.document_id = ANY(%s) LIMIT 1""", (user["tenant_id"], ids)).fetchone()
        if taken:
            raise HTTPException(409, f"{taken['document_number']} is already in a prepared consolidation.")
        total = sum((Decimal(r["total_amount"]) for r in rows), Decimal("0"))
        submission = db.execute("""INSERT INTO myinvois_consolidated_submissions(tenant_id,document_type,date_from,date_to,status,
                idempotency_key,total_amount,created_by)
            VALUES (%s,%s,%s,%s,'prepared',%s,%s,%s) RETURNING id""",
                                (user["tenant_id"], data.documentType, data.dateFrom, data.dateTo, data.idempotencyKey, total,
                                 user["id"])).fetchone()
        for row in rows:
            db.execute("INSERT INTO myinvois_consolidated_documents(submission_id,tenant_id,document_id) VALUES (%s,%s,%s)",
                       (submission["id"], user["tenant_id"], row["id"]))
        audit(db, user, "erp.myinvois_prepared", str(submission["id"]), {"documents": len(rows), "note": PREPARED_ONLY})
    return {"id": str(submission["id"]), "documentType": data.documentType, "status": "prepared", "documentCount": len(rows),
            "totalAmount": money(total), "note": PREPARED_ONLY, "idempotent": False}


def list_consolidations(request: Request):
    _require("ENABLE_MYINVOIS_PREP", "MyInvois preparation records")
    user = erp_user(request)
    with connect() as db:
        rows = db.execute("""SELECT s.*, (SELECT count(*) FROM myinvois_consolidated_documents d WHERE d.submission_id=s.id) AS n
            FROM myinvois_consolidated_submissions s WHERE s.tenant_id=%s ORDER BY s.created_at DESC""", (user["tenant_id"],)).fetchall()
    return [{"id": str(r["id"]), "documentType": r["document_type"], "dateFrom": str(r["date_from"]), "dateTo": str(r["date_to"]),
             "status": r["status"], "documentCount": r["n"], "totalAmount": money(r["total_amount"]), "note": PREPARED_ONLY,
             "createdAt": str(r["created_at"])} for r in rows]


def register(router):
    router.add_api_route('/api/erp/sharing-status', sharing_status, methods=['GET'], tags=['ERP Sharing'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/shares', create_share, methods=['POST'], status_code=201, tags=['ERP Sharing'])
    router.add_api_route('/api/erp/documents/{document_type}/{document_id}/shares', list_shares, methods=['GET'], tags=['ERP Sharing'])
    router.add_api_route('/api/erp/shares/{share_id}/revoke', revoke_share, methods=['POST'], tags=['ERP Sharing'])
    router.add_api_route('/api/public/erp-share/{token}', public_share, methods=['GET'], tags=['Public Shares'])
    router.add_api_route('/api/myinvois/consolidated', prepare_consolidation, methods=['POST'], status_code=201, tags=['MyInvois'])
    router.add_api_route('/api/myinvois/consolidated', list_consolidations, methods=['GET'], tags=['MyInvois'])
