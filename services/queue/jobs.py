import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)
# jobs.py is stored in <backend>/services/queue; the backend root is one parent up.
# Using parents[2] resolves to the monorepo root and makes workers reject files
# uploaded into <backend>/storage as being outside the trusted storage root.
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class ExtractionJobError(RuntimeError):
    """Base exception for extraction queue job failures."""
    pass


class ExtractionUnauthorizedError(ExtractionJobError):
    """Raised when no authorized document resolver is configured."""
    pass


class ExtractionResolutionError(ExtractionJobError):
    """Raised when a document cannot be resolved to a valid file."""
    pass


class ExtractionExecutionError(ExtractionJobError):
    """Raised when extraction execution fails (infrastructure or structural failure)."""
    pass


# Optional test-only hooks for isolated offline testing.
# In production, these are None and the worker fails closed.
_TEST_DOCUMENT_RESOLVER: Optional[Callable[[str], Optional[Path]]] = None
_TEST_PARSER: Optional[Callable] = None


def set_test_extraction_environment(
    resolver: Optional[Callable[[str], Optional[Path]]] = None,
    parser: Optional[Callable] = None,
) -> None:
    """Configure isolated offline document resolver and parser for tests."""
    global _TEST_DOCUMENT_RESOLVER, _TEST_PARSER
    _TEST_DOCUMENT_RESOLVER = resolver
    _TEST_PARSER = parser


def reset_test_extraction_environment() -> None:
    """Reset test hooks to ensure production default fail-closed behavior."""
    global _TEST_DOCUMENT_RESOLVER, _TEST_PARSER
    _TEST_DOCUMENT_RESOLVER = None
    _TEST_PARSER = None


def resolve_document_from_database(document_id: str) -> Optional[Path]:
    """Resolve an uploaded document through PostgreSQL, never from a queue path."""
    from database.connection import connect

    # Queue payloads are opaque document UUIDs. Validate before sending the
    # value to PostgreSQL so malformed jobs fail closed as authorization/input
    # failures instead of leaking a database cast error.
    try:
        uuid.UUID(str(document_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ExtractionUnauthorizedError("Extraction resolver not available for an invalid document id.") from exc

    with connect() as db:
        row = db.execute(
            "SELECT storage_path FROM documents WHERE id=%s",
            (document_id,),
        ).fetchone()
    if not row or not row["storage_path"]:
        return None

    if str(row["storage_path"]).startswith("s3://"):
        from database.object_storage.object_store import download
        downloaded = download(str(row["storage_path"]))
        # S3 downloads are initially materialised by the storage adapter in
        # the OS temporary directory.  Keep the resolver fail-closed, but
        # move the authorised snapshot into the worker's trusted extraction
        # root before handing it to image/OCR or Docling.  In particular, the
        # image bridge rejects paths outside this root by design.
        extraction_root = (BACKEND_ROOT / ".extraction-tmp").resolve()
        extraction_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(downloaded).suffix.lower() or ".bin"
        with tempfile.NamedTemporaryFile(
            prefix="s3-source-", suffix=suffix, dir=extraction_root,
            delete=False,
        ) as target:
            trusted = Path(target.name)
        try:
            shutil.copyfile(downloaded, trusted)
        finally:
            try:
                Path(downloaded).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary S3 snapshot")
        return trusted

    storage_root = Path(os.getenv("DOCUMENT_STORAGE_PATH", str(BACKEND_ROOT / "storage"))).expanduser()
    if not storage_root.is_absolute():
        storage_root = (BACKEND_ROOT / storage_root).resolve()
    else:
        storage_root = storage_root.resolve()

    candidate = Path(row["storage_path"]).expanduser()
    if not candidate.is_absolute():
        candidate = (BACKEND_ROOT / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if not candidate.is_relative_to(storage_root):
        raise ExtractionUnauthorizedError("Stored document path is outside the configured document storage root.")
    return candidate


def process_document_mock(document_uuid: str) -> Dict[str, Any]:
    """
    Mocked CPU task receiving ONLY the immutable document_uuid.
    
    LIMITATION (Milestone 3A): PostgreSQL ownership verification is NOT implemented
    merely because the worker receives document_uuid. In the future, the worker must 
    query the DB to verify tenant ownership and document existence.
    
    LIMITATION (Milestone 3A): The durable outbox, dispatcher, leases, and recovery 
    sweeper remain future implementation work. Dual-write consistency is NOT solved 
    in this milestone.
    
    This mocked job returns minimal metadata. RQ stores job return values in Redis, 
    so we DO NOT return invoice numbers, financial fields, source documents, or extraction 
    JSON from queue jobs.
    """
    logger.info(f"Mock processing document UUID: {document_uuid}")
    
    # Return minimal JSON metadata to avoid storing sensitive data in Redis
    return {
        "status": "mock_completed",
        "document_uuid": document_uuid
    }


def process_document_extraction_job(document_id: str, pending_job_id: str | None = None,
                                    tenant_or_dispatch_token: str | None = None,
                                    dispatch_token: str | None = None) -> Dict[str, Any]:
    """Process an extraction job using the durable queue claim flow."""
    logger.info("Executing extraction job for document: %s", document_id)
    logger.info("Extraction image path: enabled=%s python=%s timeout=%s",
                os.getenv("ENABLE_IMAGE_OCR", "").lower() == "true",
                os.getenv("IMAGE_OCR_PYTHON", sys.executable),
                os.getenv("IMAGE_OCR_TIMEOUT_SECONDS", "60"))
    from rq import get_current_job
    from services.queue import attempts
    current_job = get_current_job()
    if current_job is None:
        return {"status": "REJECTED", "reason": "LEGACY_PAYLOAD" if pending_job_id is None else "RQ_CONTEXT_REQUIRED"}

    # Production publish() sends only the document UUID; the RQ job id is the
    # dispatch token.  Older durable dispatchers may still send
    # (document_id, pending_job_id, dispatch_token), so accept that shape too.
    if dispatch_token is None and tenant_or_dispatch_token is not None:
        dispatch_token = tenant_or_dispatch_token
        tenant_or_dispatch_token = None
    with attempts.connect() as db:
        if pending_job_id:
            row = db.execute("""SELECT id,document_id,tenant_id,dispatch_token
                FROM pending_jobs WHERE id=%s AND document_id=%s""",
                (pending_job_id, document_id)).fetchone()
        else:
            row = db.execute("""SELECT id,document_id,tenant_id,dispatch_token
                FROM pending_jobs WHERE document_id=%s AND dispatch_token=%s
                ORDER BY created_at DESC LIMIT 1""",
                (document_id, str(current_job.id))).fetchone()
    if not row:
        raise ExtractionResolutionError("Job is not dispatched or its lease has expired.")
    delivery = attempts.Delivery(str(row['document_id']), str(row['id']),
                                 str(row['tenant_id']), str(row['dispatch_token']))
    claim = attempts.claim_delivery(delivery, str(current_job.id))
    if not claim:
        raise ExtractionResolutionError("Job is not dispatched or its lease has expired.")

    from services.extraction.workflow.execution import execute_document_extraction
    from services.extraction.persistence.persistence import mark_extraction_job_failed, persist_extraction_result

    try:
        resolver = _TEST_DOCUMENT_RESOLVER or resolve_document_from_database
        resolved_path = resolver(document_id)
        if not resolved_path or not Path(resolved_path).is_file():
            logger.error("Trusted document resolver could not locate file for: %s", document_id)
            raise ExtractionExecutionError("Extraction attempt failed.")
        result = execute_document_extraction(
            document_id=document_id,
            file_path=Path(resolved_path),
            parser=_TEST_PARSER,
        )

        if result.get("execution_status") != "EXTRACTED":
            error_msg = result.get("error") or "Extraction execution failed."
            logger.error(
                "Extraction result rejected for %s: status=%s error=%s",
                document_id,
                result.get("execution_status"),
                str(error_msg)[:240],
            )
            raise ExtractionExecutionError(f"Extraction execution failed: {error_msg}")

        try:
            uuid.UUID(str(document_id))
            persistence = persist_extraction_result(document_id, result, claim=claim)
        except (ValueError, AttributeError, TypeError):
            if not (_TEST_DOCUMENT_RESOLVER or _TEST_PARSER):
                raise
            persistence = {"skipped": "offline test document id"}
    except Exception as exc:
        logger.error("Extraction execution failed for %s: %s", document_id, type(exc).__name__)
        safe_error = exc if str(exc) == "Extraction attempt failed." else ExtractionExecutionError("Extraction attempt failed.")
        try:
            if not (_TEST_DOCUMENT_RESOLVER or _TEST_PARSER):
                mark_extraction_job_failed(document_id, str(safe_error), claim=claim)
        except Exception:
            logger.exception("Could not persist failed status for extraction job %s", document_id)
        if safe_error is not exc:
            raise safe_error from None
        raise

    return {
        "status": result["execution_status"],
        "extraction_status": result.get("extraction_status"),
        "document_id": document_id,
        "service_run_id": result.get("service_run_id"),
        "persistence": persistence,
        "review_required": result.get("review_required", True),
        "accounting_approval_performed": False,
    }
