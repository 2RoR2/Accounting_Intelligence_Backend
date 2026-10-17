"""
Trusted document resolution for extraction execution.
Enforces database existence, tenant ownership, filesystem containment,
and cryptographic SHA-256 integrity checks.
"""

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

from infrastructure.queue.jobs import ExtractionResolutionError
from services.documents.manager import get_document_storage_root

logger = logging.getLogger(__name__)


class DocumentResolutionError(ExtractionResolutionError):
    """Base error for document resolution."""
    pass


class DocumentNotFoundError(DocumentResolutionError):
    """Raised when document does not exist or unauthorized tenant."""
    pass


class DocumentSecurityError(DocumentResolutionError):
    """Raised when file path violates storage containment."""
    pass


class DocumentTamperedError(DocumentResolutionError):
    """Raised when file hash on disk differs from database record."""
    pass


def resolve_trusted_document_path(
    db: Any,
    document_id: str,
    tenant_id: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> Path:
    """
    Resolves the physical filesystem path for a trusted document.

    Security & Safety Guarantees:
    1. Database Lookup: Record must exist in 'documents' table.
    2. Tenant Isolation: If tenant_id is provided, must match row tenant_id.
    3. Storage Containment: Resolved path must reside within the authorized storage directory.
    4. File Existence: File must exist and be readable.
    5. Cryptographic Integrity: SHA-256 of file on disk must match database record.
    """
    if not document_id:
        raise DocumentNotFoundError("Document ID must be provided for resolution.")

    # 1. Database query
    if tenant_id:
        query = "SELECT id, tenant_id, storage_path, file_hash_sha256 FROM documents WHERE id = %s AND tenant_id = %s"
        params = (document_id, tenant_id)
    else:
        query = "SELECT id, tenant_id, storage_path, file_hash_sha256 FROM documents WHERE id = %s"
        params = (document_id,)

    row = db.execute(query, params).fetchone()
    if not row:
        logger.error("Document not found in database: doc_id=%s, tenant=%s", document_id, tenant_id)
        raise DocumentNotFoundError(f"Document '{document_id}' not found or unauthorized.")

    storage_path_str = row.get("storage_path")
    expected_hash = row.get("file_hash_sha256")

    if not storage_path_str:
        raise DocumentNotFoundError(f"Document '{document_id}' has no recorded storage path.")

    file_path = Path(storage_path_str).resolve()
    storage_root = get_document_storage_root(project_root)

    # 2. Filesystem containment check
    if not file_path.is_relative_to(storage_root):
        logger.error("Storage containment violation: %s is outside %s", file_path, storage_root)
        raise DocumentSecurityError(
            f"Document file path leaves authorized storage containment."
        )

    # 3. File existence check
    if not file_path.is_file():
        logger.error("Document file missing from disk: %s", file_path)
        raise DocumentNotFoundError(
            f"Document file does not exist on storage disk for doc_id={document_id}."
        )

    # 4. Cryptographic integrity check
    try:
        actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
    except Exception as exc:
        raise DocumentTamperedError(
            f"Failed to read document file for integrity check: {exc}"
        ) from exc

    if not expected_hash or actual_hash.lower() != expected_hash.lower():
        logger.error(
            "Cryptographic tamper detected for doc_id=%s! DB hash=%s, Disk hash=%s",
            document_id,
            expected_hash,
            actual_hash,
        )
        raise DocumentTamperedError(
            f"Document cryptographic integrity verification failed for doc_id={document_id}."
        )

    logger.info("Trusted document resolved and verified: doc_id=%s (SHA-256: %s)", document_id, actual_hash)
    return file_path
