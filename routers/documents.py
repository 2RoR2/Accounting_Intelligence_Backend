"""
Document management and processing API routes.
Provides authenticated synthetic-PDF upload, document listing,
status tracking, extraction/validation result retrieval, PDF download with SHA-256 integrity verification,
accountant field corrections (with append-only audit triggers), and
explicit document approval creating immutable standardised records.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from database.connection import connect
from infrastructure.queue.config import get_queue
from infrastructure.queue.dispatcher import dispatch_pending_jobs
from services.documents.manager import save_uploaded_document
from services.validation.workflow import run_document_validation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])


class FieldCorrectionRequest(BaseModel):
    value: Optional[str] = None


def _get_authenticated_user(request: Request, db: Any) -> Dict[str, Any]:
    """Helper to authenticate and fetch the active company user from session."""
    from app import active, session_user

    user = session_user(db, request)
    if not active(user) or not user.get("tenant_id"):
        raise HTTPException(
            status_code=403,
            detail="Active company account required for document operations.",
        )
    return user


def parse_upload_payload(content_type: str, headers: dict, body: bytes) -> tuple[bytes, str, str, str]:
    """Universal parser for file uploads supporting multipart/form-data, raw PDF, and JSON base64."""
    filename = headers.get("x-file-name", "document.pdf")
    kind = headers.get("x-document-kind", "invoice")
    category = headers.get("x-document-category", "accounts_payable")

    if "application/json" in content_type:
        import base64
        data = json.loads(body.decode("utf-8"))
        raw_b64 = data.get("content_base64", "")
        file_bytes = base64.b64decode(raw_b64)
        filename = data.get("filename", filename)
        kind = data.get("kind", kind)
        category = data.get("category", category)
        return file_bytes, filename, kind, category

    if "application/pdf" in content_type or body.startswith(b"%PDF-"):
        return body, filename, kind, category

    if "multipart/form-data" in content_type:
        import email
        raw_msg = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8", errors="replace") + body
        msg = email.message_from_bytes(raw_msg)
        file_bytes = b""
        for part in msg.walk():
            fname = part.get_filename()
            cd = part.get("Content-Disposition", "")
            if fname or "filename=" in cd:
                filename = fname or filename
                payload = part.get_payload(decode=True)
                if payload:
                    file_bytes = payload
            elif 'name="kind"' in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    kind = payload.decode("utf-8", errors="replace").strip()
            elif 'name="category"' in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    category = payload.decode("utf-8", errors="replace").strip()
        if file_bytes:
            return file_bytes, filename, kind, category

    if body.startswith(b"%PDF-"):
        return body, filename, kind, category

    raise ValueError("Invalid document payload. Provide PDF content, multipart form, or JSON with content_base64.")


@router.post("/upload", status_code=201)
async def upload_document(request: Request):
    """
    Authenticated synthetic-PDF document upload endpoint.
    Supports multipart/form-data, direct application/pdf, and JSON base64 payloads.
    Atomically writes file to private storage, creates document metadata,
    and inserts durable pending_job outbox record.

    Outbox transaction ordering (P0-A):
    - Document insertion commits to PostgreSQL FIRST.
    - Opportunistic dispatch to Redis occurs strictly AFTER PostgreSQL commit.
    """
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        content, filename, kind, category = parse_upload_payload(content_type, headers, body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid upload format: {exc}") from exc

    with connect() as db:
        user = _get_authenticated_user(request, db)

        # 2. Save document & create pending job atomically in PostgreSQL
        try:
            doc_meta = save_uploaded_document(
                db=db,
                user=user,
                file_bytes=content,
                original_filename=filename,
                kind=kind,
                category=category,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("Upload failure: %s", exc)
            raise HTTPException(
                status_code=500, detail="Failed to save document record."
            ) from exc

    # Database commit complete.
    # 3. Opportunistic dispatch to Redis RQ happens strictly AFTER PostgreSQL commit
    try:
        queue = get_queue()
        if queue is not None:
            with connect() as dispatch_db:
                dispatched = dispatch_pending_jobs(dispatch_db, queue=queue, batch_size=1)
                if dispatched and doc_meta.get("pending_job"):
                    doc_meta["pending_job"]["status"] = "dispatched"
                    doc_meta["status"] = "queued"
    except Exception as exc:
        logger.debug("Opportunistic dispatch deferred to worker/sweeper: %s", exc)

    return doc_meta


@router.get("", status_code=200)
def list_documents(
    request: Request,
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    Lists documents belonging to the authenticated user's company (tenant).
    Strictly tenant-scoped.
    """
    with connect() as db:
        user = _get_authenticated_user(request, db)
        tenant_id = user["tenant_id"]

        if status:
            rows = db.execute(
                """
                SELECT id, document_code, original_filename, kind, category,
                       status, file_size_bytes, uploaded_at, updated_at
                FROM documents
                WHERE tenant_id = %s AND status = %s
                ORDER BY uploaded_at DESC
                LIMIT %s OFFSET %s
                """,
                (tenant_id, status.lower(), limit, offset),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT id, document_code, original_filename, kind, category,
                       status, file_size_bytes, uploaded_at, updated_at
                FROM documents
                WHERE tenant_id = %s
                ORDER BY uploaded_at DESC
                LIMIT %s OFFSET %s
                """,
                (tenant_id, limit, offset),
            ).fetchall()

        return [
            {
                "id": str(r["id"]),
                "document_code": r["document_code"],
                "original_filename": r["original_filename"],
                "kind": r["kind"],
                "category": r["category"],
                "status": r["status"],
                "file_size_bytes": r["file_size_bytes"],
                "uploaded_at": str(r["uploaded_at"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]


@router.get("/{document_id}", status_code=200)
def get_document_details(document_id: str, request: Request):
    """
    Retrieves metadata, processing status, and pending job state for a specific document.
    Enforces tenant ownership.
    """
    with connect() as db:
        user = _get_authenticated_user(request, db)
        tenant_id = user["tenant_id"]

        doc = db.execute(
            """
            SELECT id, document_code, tenant_id, original_filename, kind, category,
                   status, file_size_bytes, file_hash_sha256, processing_seconds,
                   uploaded_at, processing_started_at, processing_completed_at, updated_at
            FROM documents
            WHERE id = %s AND tenant_id = %s
            """,
            (document_id, tenant_id),
        ).fetchone()

        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")

        # Query pending job status if any
        job_row = db.execute(
            """
            SELECT id, status, attempt_count, max_attempts, last_error
            FROM pending_jobs
            WHERE document_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()

        pending_job = None
        if job_row:
            pending_job = {
                "id": str(job_row["id"]),
                "status": job_row["status"],
                "attempt_count": job_row["attempt_count"],
                "max_attempts": job_row["max_attempts"],
                "last_error": job_row["last_error"],
            }

        return {
            "id": str(doc["id"]),
            "document_code": doc["document_code"],
            "tenant_id": str(doc["tenant_id"]),
            "original_filename": doc["original_filename"],
            "kind": doc["kind"],
            "category": doc["category"],
            "status": doc["status"],
            "file_size_bytes": doc["file_size_bytes"],
            "file_hash_sha256": doc["file_hash_sha256"],
            "processing_seconds": doc["processing_seconds"],
            "uploaded_at": str(doc["uploaded_at"]),
            "processing_started_at": str(doc["processing_started_at"]) if doc["processing_started_at"] else None,
            "processing_completed_at": str(doc["processing_completed_at"]) if doc["processing_completed_at"] else None,
            "updated_at": str(doc["updated_at"]),
            "pending_job": pending_job,
        }


@router.get("/{document_id}/download", status_code=200)
def download_document(document_id: str, request: Request):
    """
    Secure original-PDF download endpoint (P0-E).
    Enforces authentication, tenant scoping, private storage containment,
    and SHA-256 integrity verification.
    Rejects tampered files with 409 Conflict and escaped paths with 403 Forbidden.
    """
    with connect() as db:
        user = _get_authenticated_user(request, db)
        tenant_id = user["tenant_id"]

        from services.documents.resolver import (
            resolve_trusted_document_path,
            DocumentNotFoundError,
            DocumentSecurityError,
            DocumentTamperedError,
        )

        try:
            trusted_path = resolve_trusted_document_path(
                db=db,
                document_id=document_id,
                tenant_id=tenant_id,
            )
        except DocumentNotFoundError:
            raise HTTPException(status_code=404, detail="Document not found.")
        except DocumentTamperedError as exc:
            logger.error("Download integrity check failed for doc %s: %s", document_id, exc)
            raise HTTPException(
                status_code=409,
                detail=f"Document integrity verification failed. File content has been modified or tampered: {exc}",
            )
        except DocumentSecurityError as exc:
            logger.error("Download security error for doc %s: %s", document_id, exc)
            raise HTTPException(status_code=403, detail=f"Access denied: {exc}")
        except Exception as exc:
            logger.error("Download resolution failed for doc %s: %s", document_id, exc)
            raise HTTPException(status_code=500, detail="Unable to retrieve document.")

        doc = db.execute(
            "SELECT original_filename, mime_type FROM documents WHERE id = %s AND tenant_id = %s",
            (document_id, tenant_id),
        ).fetchone()

        filename = doc["original_filename"] if doc else f"{document_id}.pdf"
        mime_type = doc["mime_type"] if doc and doc.get("mime_type") else "application/pdf"

        return FileResponse(
            path=trusted_path,
            media_type=mime_type,
            filename=filename,
            content_disposition_type="attachment",
        )


@router.get("/{document_id}/result", status_code=200)
def get_document_result(document_id: str, request: Request):
    """
    Retrieves the extracted field predictions, line items, and accounting validation exceptions
    for the current extraction run. Enforces tenant ownership.
    """
    with connect() as db:
        user = _get_authenticated_user(request, db)
        tenant_id = user["tenant_id"]

        doc = db.execute(
            """
            SELECT id, document_code, status
            FROM documents
            WHERE id = %s AND tenant_id = %s
            """,
            (document_id, tenant_id),
        ).fetchone()

        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")

        # Find current active extraction run
        curr_run = db.execute(
            """
            SELECT id, run_number, routing_decision, raw_json
            FROM raw_extractions
            WHERE document_id = %s AND is_current = TRUE
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()

        field_rows = []
        line_rows = []

        if curr_run:
            extraction_id = curr_run["id"]

            # Query canonical field_predictions
            field_rows = db.execute(
                """
                SELECT field_key, field_label, extraction_state, resolution,
                       raw_value, normalized_value, current_value,
                       confidence, evidence, candidates, extraction_method,
                       corrected_by, corrected_at
                FROM field_predictions
                WHERE extraction_id = %s
                ORDER BY field_key
                """,
                (extraction_id,),
            ).fetchall()

            # Query canonical line_items
            line_rows = db.execute(
                """
                SELECT line_number, description, quantity, unit_price, amount,
                       field_states, evidence, candidates
                FROM line_items
                WHERE extraction_id = %s
                ORDER BY line_number
                """,
                (extraction_id,),
            ).fetchall()

        # Exceptions
        exc_rows = db.execute(
            """
            SELECT id, type, severity, status, message, note, created_at, resolved_at
            FROM exceptions
            WHERE document_id = %s AND tenant_id = %s
            ORDER BY created_at
            """,
            (document_id, tenant_id),
        ).fetchall()

    exceptions = [
        {
            "id": str(e["id"]),
            "type": e["type"],
            "severity": e["severity"],
            "status": e["status"],
            "message": e["message"],
            "note": e["note"],
            "created_at": str(e["created_at"]),
            "resolved_at": str(e["resolved_at"]) if e["resolved_at"] else None,
        }
        for e in exc_rows
    ]

    has_open_exceptions = any(e["status"] == "open" for e in exceptions)
    # P0-B: Approval is strictly permitted ONLY when document is validated with zero open exceptions
    can_approve = doc["status"] == "validated" and not has_open_exceptions
    # Review is required prior to standardised state or whenever open exceptions exist
    review_required = has_open_exceptions or doc["status"] != "standardised"

    return {
        "document_id": str(doc["id"]),
        "document_code": doc["document_code"],
        "status": doc["status"],
        "review_required": review_required,
        "can_approve": can_approve,
        "run_number": curr_run["run_number"] if curr_run else None,
        "routing_decision": curr_run["routing_decision"] if curr_run else None,
        "fields": [
            {
                "field_key": f["field_key"],
                "field_label": f["field_label"],
                "extraction_state": f["extraction_state"],
                "resolution": f["resolution"],
                "raw_value": f["raw_value"],
                "normalized_value": f["normalized_value"],
                "current_value": f["current_value"],
                "confidence": float(f["confidence"]) if f["confidence"] is not None else None,
                "evidence": f["evidence"] if isinstance(f["evidence"], list) else json.loads(f["evidence"] or "[]"),
                "candidates": f["candidates"] if isinstance(f["candidates"], list) else json.loads(f["candidates"] or "[]"),
                "extraction_method": f["extraction_method"],
                "is_corrected": f["corrected_at"] is not None,
                "corrected_at": str(f["corrected_at"]) if f["corrected_at"] else None,
            }
            for f in field_rows
        ],
        "line_items": [
            {
                "line_number": l["line_number"],
                "description": l["description"],
                "quantity": float(l["quantity"]) if l["quantity"] is not None else None,
                "unit_price": float(l["unit_price"]) if l["unit_price"] is not None else None,
                "amount": float(l["amount"]) if l["amount"] is not None else None,
            }
            for l in line_rows
        ],
        "exceptions": exceptions,
    }


@router.patch("/{document_id}/fields/{field_key}", status_code=200)
def patch_field_correction(
    document_id: str,
    field_key: str,
    payload: FieldCorrectionRequest,
    request: Request,
):
    """
    Accountant correction endpoint.
    Updates the current_value for a prediction in the active extraction run.
    PostgreSQL triggers protect raw provenance and record an append-only audit trail in field_corrections.
    Invalidates prior 'validated' status, transitioning the document to 'corrected' (P0-B).
    """
    with connect() as db:
        user = _get_authenticated_user(request, db)
        tenant_id = user["tenant_id"]

        doc = db.execute(
            "SELECT id, status FROM documents WHERE id = %s AND tenant_id = %s",
            (document_id, tenant_id),
        ).fetchone()

        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")

        # P0-B: Prevent modifications after document is already approved and standardised
        if doc["status"] == "standardised":
            raise HTTPException(status_code=400, detail="Cannot correct an approved and standardised document.")

        # Find current extraction
        curr_run = db.execute(
            "SELECT id FROM raw_extractions WHERE document_id = %s AND is_current = TRUE LIMIT 1",
            (document_id,),
        ).fetchone()

        if not curr_run:
            raise HTTPException(status_code=400, detail="No active extraction found to correct.")

        extraction_id = curr_run["id"]

        updated_pred = db.execute(
            """
            UPDATE field_predictions
            SET current_value = %s,
                corrected_by = %s,
                corrected_at = now()
            WHERE extraction_id = %s AND field_key = %s
            RETURNING id, field_key, current_value, corrected_at, corrected_by
            """,
            (payload.value, user["id"], extraction_id, field_key),
        ).fetchone()

        if not updated_pred:
            raise HTTPException(status_code=404, detail=f"Field '{field_key}' not found in current extraction.")

        # Any accountant correction invalidates prior validation status
        db.execute(
            "UPDATE documents SET status = 'corrected', updated_at = now() WHERE id = %s AND tenant_id = %s",
            (document_id, tenant_id),
        )

    return {
        "success": True,
        "document_id": document_id,
        "field_key": field_key,
        "new_value": updated_pred["current_value"],
        "corrected_at": str(updated_pred["corrected_at"]),
        "corrected_by": str(updated_pred["corrected_by"]),
        "document_status": "corrected",
    }


@router.post("/{document_id}/approve", status_code=200)
def approve_document(document_id: str, request: Request):
    """
    Accountant approval endpoint (P0-B).
    Guarantees:
    - Only accountants or administrators can approve documents.
    - Requires that the document is strictly 'validated' (never 'corrected' without revalidation).
    - Requires ZERO open blocking exceptions.
    - Rejects duplicate approval if already standardised.
    - Enforces mandatory financial identifiers (supplier_name, invoice_number, invoice_date, total_amount);
      NEVER fabricates or defaults mandatory financial values.
    - Atomically snapshots verified header fields, line items, and real ISO 8601 UTC approval timestamp
      into immutable standardised_records.
    """
    with connect() as db:
        user = _get_authenticated_user(request, db)
        tenant_id = user["tenant_id"]

        # Role enforcement: accountant or admin roles only
        if user.get("role") not in ("accountant", "local_admin", "super_admin"):
            raise HTTPException(
                status_code=403,
                detail="Only accountants or administrators can approve documents.",
            )

        doc = db.execute(
            """
            SELECT id, document_code, status, kind, category
            FROM documents
            WHERE id = %s AND tenant_id = %s
            """,
            (document_id, tenant_id),
        ).fetchone()

        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")

        # P0-B: Reject duplicate approval
        existing = db.execute(
            "SELECT id, record_code FROM standardised_records WHERE document_id = %s",
            (document_id,),
        ).fetchone()
        if existing or doc["status"] == "standardised":
            rec_code = existing["record_code"] if existing else "existing record"
            raise HTTPException(
                status_code=400,
                detail=f"Document has already been approved as {rec_code}.",
            )

        # P0-B: Approval only allowed from 'validated', NEVER 'corrected' or 'exception'
        if doc["status"] != "validated":
            raise HTTPException(
                status_code=400,
                detail=f"Document cannot be approved in '{doc['status']}' status. It must be 'validated'. Please run revalidation after any corrections.",
            )

        # Check for open blocking exceptions
        open_exc = db.execute(
            "SELECT count(*) AS cnt FROM exceptions WHERE document_id = %s AND status = 'open'",
            (document_id,),
        ).fetchone()

        if open_exc and open_exc["cnt"] > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Document has {open_exc['cnt']} open exception(s) and cannot be approved until resolved.",
            )

        # Retrieve current extraction run
        curr_run = db.execute(
            """
            SELECT id, raw_json FROM raw_extractions
            WHERE document_id = %s AND is_current = TRUE
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()

        if not curr_run:
            raise HTTPException(status_code=400, detail="No active extraction found for document.")

        preds = db.execute(
            """
            SELECT field_key, current_value, normalized_value, corrected_at
            FROM field_predictions
            WHERE extraction_id = %s
            """,
            (curr_run["id"],),
        ).fetchall()

        # Prioritize human corrections (corrected_at IS NOT NULL), otherwise normalized_value
        field_vals = {}
        for p in preds:
            if p.get("corrected_at") is not None:
                field_vals[p["field_key"]] = p["current_value"]
            else:
                field_vals[p["field_key"]] = p["normalized_value"]

        # P0-B: Strict validation of mandatory financial fields — NEVER fabricate values!
        supplier = field_vals.get("supplier_name")
        if not supplier or not str(supplier).strip():
            raise HTTPException(
                status_code=400,
                detail="Approval rejected: 'supplier_name' is missing or empty. Mandatory financial fields cannot be fabricated.",
            )

        inv_number = field_vals.get("invoice_number")
        if not inv_number or not str(inv_number).strip():
            raise HTTPException(
                status_code=400,
                detail="Approval rejected: 'invoice_number' is missing or empty. Mandatory financial fields cannot be fabricated.",
            )

        inv_date = field_vals.get("invoice_date")
        if not inv_date:
            raise HTTPException(
                status_code=400,
                detail="Approval rejected: 'invoice_date' is missing. Mandatory financial fields cannot be fabricated.",
            )

        from services.extraction.adapter import convert_numeric

        total = convert_numeric(field_vals.get("total_amount"))
        if total is None:
            raise HTTPException(
                status_code=400,
                detail="Approval rejected: 'total_amount' is missing or not a valid numeric value.",
            )

        subtotal = convert_numeric(field_vals.get("subtotal"))
        tax = convert_numeric(field_vals.get("tax_amount"))
        due_date = field_vals.get("due_date")
        currency = (field_vals.get("currency") or "MYR").strip()[:3].upper()

        # Fetch verified line items for the current extraction run
        line_rows = db.execute(
            """
            SELECT line_number, description, quantity, unit_price, amount
            FROM line_items
            WHERE extraction_id = %s
            ORDER BY line_number
            """,
            (curr_run["id"],),
        ).fetchall()

        # Real ISO 8601 UTC approval timestamp
        approved_timestamp = datetime.now(timezone.utc).isoformat()

        # Snapshot of canonical record with real data and line items
        snapshot = {
            "document_id": document_id,
            "document_code": doc["document_code"],
            "supplier_name": str(supplier).strip(),
            "invoice_number": str(inv_number).strip(),
            "invoice_date": str(inv_date),
            "due_date": str(due_date) if due_date else None,
            "currency": currency,
            "subtotal": str(subtotal) if subtotal is not None else None,
            "tax_amount": str(tax) if tax is not None else None,
            "total_amount": str(total),
            "line_items": [
                {
                    "line_number": li["line_number"],
                    "description": li["description"],
                    "quantity": float(li["quantity"]) if li["quantity"] is not None else None,
                    "unit_price": float(li["unit_price"]) if li["unit_price"] is not None else None,
                    "amount": float(li["amount"]) if li["amount"] is not None else None,
                }
                for li in line_rows
            ],
            "approved_by": str(user["id"]),
            "approved_at": approved_timestamp,
        }

        # Insert standardised record (trigger trg_set_record_code assigns REC-...)
        record = db.execute(
            """
            INSERT INTO standardised_records (
                document_id, tenant_id, category, supplier_name,
                invoice_number, invoice_date, due_date, currency,
                subtotal, tax_amount, total_amount, validation_status,
                validated_at, snapshot, created_by
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'passed', now(), %s::jsonb, %s
            )
            RETURNING id, record_code, created_at
            """,
            (
                document_id,
                tenant_id,
                doc["category"],
                str(supplier).strip(),
                str(inv_number).strip(),
                inv_date,
                due_date,
                currency,
                subtotal,
                tax,
                total,
                json.dumps(snapshot),
                user["id"],
            ),
        ).fetchone()

        # Update document status to 'standardised'
        db.execute(
            "UPDATE documents SET status = 'standardised', updated_at = now() WHERE id = %s AND tenant_id = %s",
            (document_id, tenant_id),
        )

        # Audit log entry
        db.execute(
            """
            INSERT INTO audit_logs (
                tenant_id, user_id, user_name, role, action, resource, details
            ) VALUES (
                %s, %s, %s, %s, 'APPROVE_DOCUMENT', %s, %s
            )
            """,
            (
                tenant_id,
                user["id"],
                user.get("name", "Accountant"),
                user.get("role", "accountant"),
                record["record_code"],
                f"Approved document {doc['document_code']} as standardized record {record['record_code']}.",
            ),
        )

    return {
        "success": True,
        "document_id": document_id,
        "document_code": doc["document_code"],
        "document_status": "standardised",
        "standardised_record_id": str(record["id"]),
        "record_code": record["record_code"],
        "approved_by": str(user["id"]),
        "approved_at": approved_timestamp,
    }


@router.post("/{document_id}/revalidate", status_code=200)
def revalidate_document(document_id: str, request: Request):
    """
    Manually triggers revalidation of a document's current extracted data against accounting rules.
    Purely recoverable: reads stored extraction and predictions without re-parsing PDF.
    Evaluates latest human corrections in field_predictions.current_value.
    """
    with connect() as db:
        user = _get_authenticated_user(request, db)
        tenant_id = user["tenant_id"]

        doc = db.execute(
            """
            SELECT id, status FROM documents
            WHERE id = %s AND tenant_id = %s
            """,
            (document_id, tenant_id),
        ).fetchone()

        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")

        try:
            outcome = run_document_validation(
                db=db,
                document_id=document_id,
                tenant_id=tenant_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("Revalidation error: %s", exc)
            raise HTTPException(
                status_code=500, detail="Revalidation failed."
            ) from exc

    return outcome
