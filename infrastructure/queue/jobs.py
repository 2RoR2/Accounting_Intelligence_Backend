import logging
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)
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


def process_document_extraction_job(document_id: str, job_id: Optional[str] = None,
                                    tenant_id: Optional[str] = None, dispatch_token: Optional[str] = None) -> Dict[str, Any]:
    """JSON-safe v1 delivery. Legacy document-only jobs are rejected before IO."""
    from infrastructure.queue import attempts
    if not all((job_id,tenant_id,dispatch_token)):
        return {"status":"REJECTED","reason":"LEGACY_PAYLOAD"}
    from rq import get_current_job
    rq_job = get_current_job()
    if rq_job is None:
        return {"status":"REJECTED","reason":"RQ_CONTEXT_REQUIRED"}
    delivery = attempts.Delivery(document_id,job_id,tenant_id,dispatch_token)
    try:
        claim = attempts.claim_delivery(delivery,rq_job.id)
    except attempts.AttemptRejected as exc:
        return {"status":"REJECTED","reason":exc.code,"document_id":document_id,"job_id":job_id}
    except Exception:
        raise ExtractionJobError("Attempt verification unavailable; processing did not start.") from None

    from services.extraction.execution import execute_document_extraction
    from services.extraction.persistence import mark_extraction_job_failed, persist_extraction_result
    try:
        resolved_path = resolve_document_from_database(document_id)
        if not resolved_path or not Path(resolved_path).is_file():
            raise ExtractionResolutionError("Document source is unavailable.")
        # Production workers never use the optional offline resolver/parser hooks.
        result = execute_document_extraction(document_id=document_id,file_path=Path(resolved_path))
        if result.get("execution_status")!="EXTRACTED":
            raise ExtractionExecutionError("Extraction failed.")
        saved = persist_extraction_result(document_id,result,claim=claim)
    except Exception:
        try:
            mark_extraction_job_failed(document_id,claim=claim)
        except Exception:
            logger.error("Attempt failure state could not be persisted for job %s",job_id)
        # Do not let RQ store provider exceptions, paths or source text in Redis.
        raise ExtractionExecutionError("Extraction attempt failed; inspect private worker evidence.") from None
    return {"status":"EXTRACTED","document_id":document_id,"job_id":job_id,
            "service_run_id":result.get("service_run_id"),"persistence":saved,
            "review_required":True,"accounting_approval_performed":False}


def process_document_extraction_test_job(document_id: str) -> Dict[str, Any]:
    """Explicit offline adapter, never enqueued by an API or production worker."""
    if not _TEST_DOCUMENT_RESOLVER or not _TEST_PARSER:
        raise ExtractionUnauthorizedError("Offline test adapters must be explicitly configured.")
    path = _TEST_DOCUMENT_RESOLVER(document_id)
    if not path or not Path(path).is_file():
        raise ExtractionResolutionError("Offline fixture not found.")
    from services.extraction.execution import execute_document_extraction
    result = execute_document_extraction(document_id=document_id,file_path=Path(path),parser=_TEST_PARSER)
    return {"status":result["execution_status"],"document_id":document_id,
            "extraction_status":result.get("extraction_status"),
            "service_run_id":result.get("service_run_id"),"review_required":True,
            "accounting_approval_performed":False}
