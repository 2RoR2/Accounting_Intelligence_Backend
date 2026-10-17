"""
Document management service: authenticated synthetic-PDF upload,
private filesystem containment, tenant-scoped metadata, and atomic outbox job creation.
"""

import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB database constraint
PDF_MAGIC_BYTES = b"%PDF-"


def get_document_storage_root(project_root: Optional[Path] = None) -> Path:
    """Returns the absolute, contained private storage directory for documents."""
    root = (
        Path(project_root)
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    custom_storage = os.getenv("DOCUMENT_STORAGE_DIR")
    if custom_storage:
        storage_path = Path(custom_storage).expanduser().resolve()
    else:
        storage_path = (root / "storage" / "documents").resolve()

    storage_path.mkdir(parents=True, exist_ok=True)
    return storage_path


def validate_pdf_content(content: bytes) -> str:
    """
    Validates document byte constraints and PDF header magic bytes.
    Returns the SHA-256 checksum on success, or raises ValueError.
    """
    if not content or len(content) == 0:
        raise ValueError("Uploaded file is empty.")

    if len(content) > MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f"File exceeds maximum allowed size of {MAX_FILE_SIZE_BYTES} bytes (20 MB)."
        )

    if not content.startswith(PDF_MAGIC_BYTES):
        raise ValueError("Invalid document format: Only PDF documents are supported.")

    return hashlib.sha256(content).hexdigest()


def save_uploaded_document(
    db: Any,
    user: Dict[str, Any],
    file_bytes: bytes,
    original_filename: str,
    kind: str = "invoice",
    category: str = "accounts_payable",
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Atomically saves an uploaded document to private storage and records
    both the document metadata and the durable pending_job outbox record.

    Guarantees:
    - Pure tenant scoping via authenticated session user.
    - Private filesystem containment.
    - Single atomic database transaction.
    """
    tenant_id = user.get("tenant_id")
    if not tenant_id:
        raise ValueError("User is not associated with an active company (tenant).")

    user_id = user.get("id")
    if not user_id:
        raise ValueError("Invalid user session.")

    clean_kind = (kind or "invoice").strip().lower()
    if clean_kind not in ("invoice", "bill", "receipt"):
        raise ValueError(f"Unsupported document kind: '{kind}'")

    clean_category = (category or "accounts_payable").strip().lower()
    if clean_category not in ("accounts_payable", "accounts_receivable"):
        raise ValueError(f"Unsupported document category: '{category}'")

    # 1. Validate file content and calculate hash
    sha256_hash = validate_pdf_content(file_bytes)

    # 2. Allocate identifiers and private storage path
    document_id = str(uuid.uuid4())
    storage_root = get_document_storage_root(project_root)
    tenant_dir = storage_root / str(tenant_id)
    tenant_dir.mkdir(parents=True, exist_ok=True)

    target_file = tenant_dir / f"{document_id}.pdf"

    # Enforce containment: target must be inside storage_root
    if not target_file.resolve().is_relative_to(storage_root):
        raise ValueError("Security violation: Storage path leaves authorized containment root.")

    # 3. Write file bytes to disk
    target_file.write_bytes(file_bytes)

    # 4. Atomic PostgreSQL transaction: document + pending_jobs
    try:
        # Note: document_code is automatically assigned by database trigger trg_set_document_code
        doc_row = db.execute(
            """
            INSERT INTO documents (
                id, tenant_id, owner_id, original_filename, kind, category,
                storage_path, mime_type, file_size_bytes, file_hash_sha256,
                status, source_channel
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, 'application/pdf', %s, %s, 'uploaded', 'upload'
            )
            RETURNING id, document_code, tenant_id, owner_id, original_filename, kind,
                      category, storage_path, file_size_bytes, file_hash_sha256, status, uploaded_at
            """,
            (
                document_id,
                tenant_id,
                user_id,
                Path(original_filename).name,
                clean_kind,
                clean_category,
                str(target_file),
                len(file_bytes),
                sha256_hash,
            ),
        ).fetchone()

        job_row = db.execute(
            """
            INSERT INTO pending_jobs (
                document_id, tenant_id, status, dispatch_count, attempt_count, max_attempts
            ) VALUES (
                %s, %s, 'pending', 0, 0, 3
            )
            RETURNING id, status, dispatch_count, attempt_count, max_attempts, created_at
            """,
            (document_id, tenant_id),
        ).fetchone()

    except Exception as exc:
        # If DB transaction fails, clean up the written file to avoid orphaned storage leaks
        if target_file.is_file():
            try:
                target_file.unlink()
            except OSError:
                pass
        raise exc

    logger.info(
        "Document uploaded successfully: doc_id=%s, code=%s, job_id=%s, tenant=%s",
        document_id,
        doc_row.get("document_code"),
        job_row.get("id"),
        tenant_id,
    )

    return {
        "document_id": str(doc_row["id"]),
        "document_code": doc_row["document_code"],
        "tenant_id": str(doc_row["tenant_id"]),
        "original_filename": doc_row["original_filename"],
        "kind": doc_row["kind"],
        "category": doc_row["category"],
        "status": doc_row["status"],
        "file_size_bytes": doc_row["file_size_bytes"],
        "file_hash_sha256": doc_row["file_hash_sha256"],
        "uploaded_at": str(doc_row["uploaded_at"]),
        "pending_job": {
            "id": str(job_row["id"]),
            "status": job_row["status"],
            "dispatch_count": job_row["dispatch_count"],
            "attempt_count": job_row["attempt_count"],
            "max_attempts": job_row["max_attempts"],
        },
    }
