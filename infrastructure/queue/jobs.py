import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


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


def process_document_extraction_job(document_id: str) -> Dict[str, Any]:
    """
    Offline/development extraction job adapter.

    Accepts ONLY an immutable document identifier (never an arbitrary raw file path from Redis).

    FAIL-CLOSED SAFETY:
    In production, durable outbox leases (pending_jobs), tenant ownership authorization,
    and a backend document storage resolver are required. Without these, this worker
    fails closed immediately.

    In isolated offline tests, a trusted test resolver can be injected via
    set_test_extraction_environment().

    The returned payload is minimal metadata only — no financial amounts, invoice numbers,
    filesystem paths, or extracted PII are stored in Redis.
    """
    logger.info("Executing extraction job for document: %s", document_id)

    if _TEST_DOCUMENT_RESOLVER is None:
        logger.error(
            "Production document resolver and outbox lease management are not yet implemented. Failing closed for document: %s",
            document_id,
        )
        raise ExtractionUnauthorizedError(
            f"Document resolver not available; worker failed closed for document_id={document_id}."
        )

    resolved_path = _TEST_DOCUMENT_RESOLVER(document_id)
    if not resolved_path or not Path(resolved_path).is_file():
        logger.error("Trusted document resolver could not locate file for: %s", document_id)
        raise ExtractionResolutionError(
            f"Trusted document file could not be resolved for document_id={document_id}."
        )

    from services.extraction.execution import execute_document_extraction

    result = execute_document_extraction(
        document_id=document_id,
        file_path=Path(resolved_path),
        parser=_TEST_PARSER,
    )

    if result.get("execution_status") != "EXTRACTED":
        error_msg = result.get("error") or "Extraction execution failed."
        sanitized_error = error_msg.split(":")[-1].strip() if ":" in error_msg else error_msg
        logger.error(
            "Extraction execution failed for %s with status %s: %s",
            document_id,
            result.get("execution_status"),
            error_msg,
        )
        raise ExtractionExecutionError(
            f"Extraction execution failed with status {result.get('execution_status')} for document_id={document_id}: {sanitized_error}"
        )

    # Return minimal JSON-safe metadata (no filesystem paths, financial amounts, or PII in Redis)
    return {
        "status": result["execution_status"],
        "extraction_status": result.get("extraction_status"),
        "document_id": document_id,
        "service_run_id": result.get("service_run_id"),
        "review_required": result.get("review_required", True),
        "accounting_approval_performed": False,
    }

