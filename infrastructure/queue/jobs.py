"""
Background worker jobs for document processing.
Integrates Redis RQ with Aleeya's pending_jobs lease fencing protocol,
trusted document resolution, real Docling execution, M2 persistence, and M3 validation.
"""

import json
import logging
import time
import uuid
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
    """Mocked CPU task receiving ONLY the immutable document_uuid."""
    logger.info("Mock processing document UUID: %s", document_uuid)
    return {
        "status": "mock_completed",
        "document_uuid": document_uuid,
    }


def process_document_extraction_job(
    document_id: str,
    job_id: Optional[str] = None,
    dispatch_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Main extraction queue worker.

    Behavior:
    1. If offline test resolver hook is set, uses it directly (offline harness compatibility).
    2. Production Database-Backed Path with explicit transaction & lease boundaries (P0-03, P0-D):
       Phase 1 (Claim Transaction):
         - Requires job_id and dispatch_token.
         - Atomically acquires processing lease on 'pending_jobs'.
         - Validates that claimed job's document_id matches the worker document_id argument.
         - Derives authoritative document and tenant identity strictly from the claimed job.
         - Resolves trusted PDF path within tenant private containment.
         - Commits and closes DB connection immediately.
       Phase 2 (CPU Inference):
         - Runs Docling CPU parsing entirely outside any open DB connection.
       Phase 3 (Guarded Persistence & Finish):
         - Opens new DB connection.
         - Verifies claim_token, tenant_id, and processing_expires_at validity.
         - Persists versioned raw_extractions, field_predictions, and line_items.
         - Calls switch_current_extraction(extraction_id) to carry forward corrections.
         - Runs recoverable M3 validation workflow.
         - Marks pending_jobs succeeded with finished_at = now().
    """
    logger.info("Executing extraction job for document: %s (job_id: %s)", document_id, job_id)

    # 1. Test hook path
    if _TEST_DOCUMENT_RESOLVER is not None:
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
            raise ExtractionExecutionError(
                f"Extraction execution failed with status {result.get('execution_status')} for document_id={document_id}: {sanitized_error}"
            )

        return {
            "status": result["execution_status"],
            "extraction_status": result.get("extraction_status"),
            "document_id": document_id,
            "service_run_id": result.get("service_run_id"),
            "review_required": result.get("review_required", True),
            "accounting_approval_performed": False,
        }

    # 2. Production Database-Backed Path
    # Enforce mandatory job_id and dispatch_token (P0-03)
    if not job_id or not dispatch_token:
        raise ExtractionUnauthorizedError(
            f"Document resolver not available: production worker execution requires valid job_id and dispatch_token for document_id={document_id}."
        )

    from database.connection import connect

    authoritative_doc_id: Optional[str] = None
    authoritative_tenant_id: Optional[str] = None
    claim_token: Optional[str] = None
    trusted_path: Optional[Path] = None

    # -------------------------------------------------------------------------
    # PHASE 1: Claim transaction (Short, bounded DB transaction)
    # -------------------------------------------------------------------------
    try:
        with connect() as db:
            claim_row = db.execute(
                """
                UPDATE pending_jobs
                SET status = 'processing',
                    claim_token = gen_random_uuid(),
                    claimed_at = now(),
                    heartbeat_at = now(),
                    dispatch_token = NULL,
                    dispatch_expires_at = NULL,
                    attempt_count = attempt_count + 1,
                    processing_expires_at = now() + interval '5 minutes',
                    updated_at = now()
                WHERE id = %s
                  AND dispatch_token = %s
                  AND status = 'dispatched'
                  AND dispatch_expires_at > now()
                  AND attempt_count < max_attempts
                RETURNING id, document_id, tenant_id, claim_token, attempt_count
                """,
                (job_id, dispatch_token),
            ).fetchone()

            if not claim_row:
                logger.info(
                    "Job %s is no longer claimable (already claimed, expired, or cancelled).",
                    job_id,
                )
                return {"status": "skipped", "reason": "Job lease not acquired"}

            claimed_doc_id = str(claim_row["document_id"])
            claimed_tenant_id = str(claim_row["tenant_id"])
            claim_token = str(claim_row["claim_token"])

            # Cross-check job/document identity (P0-D): Fail closed if mismatched
            if document_id is not None and str(document_id) != claimed_doc_id:
                logger.critical(
                    "Security violation: Worker invoked with mismatched document_id: payload=%s, job=%s (doc=%s)",
                    document_id, job_id, claimed_doc_id,
                )
                db.execute(
                    """
                    UPDATE pending_jobs
                    SET status = 'failed',
                        finished_at = now(),
                        last_error = %s,
                        claim_token = NULL,
                        processing_expires_at = NULL,
                        updated_at = now()
                    WHERE id = %s AND claim_token = %s
                    """,
                    (
                        f"Security error: Worker invoked with mismatched document_id: payload={document_id}, job_doc={claimed_doc_id}",
                        job_id,
                        claim_token,
                    ),
                )
                raise ExtractionJobError(
                    f"Worker document mismatch: claimed job {job_id} references document {claimed_doc_id}, but was invoked with {document_id}"
                )

            # Establish authoritative document and tenant identifiers
            authoritative_doc_id = claimed_doc_id
            authoritative_tenant_id = claimed_tenant_id

            db.execute(
                """
                UPDATE documents
                SET status = 'processing',
                    processing_started_at = now(),
                    updated_at = now()
                WHERE id = %s AND tenant_id = %s
                """,
                (authoritative_doc_id, authoritative_tenant_id),
            )

            # Resolve trusted document file path within tenant boundary
            from services.documents.resolver import resolve_trusted_document_path

            try:
                trusted_path = resolve_trusted_document_path(
                    db=db,
                    document_id=authoritative_doc_id,
                    tenant_id=authoritative_tenant_id,
                )
            except Exception as res_err:
                logger.error("Trusted path resolution failed for doc %s: %s", authoritative_doc_id, res_err)
                db.execute(
                    """
                    UPDATE pending_jobs
                    SET status = 'failed',
                        finished_at = now(),
                        last_error = %s,
                        claim_token = NULL,
                        processing_expires_at = NULL,
                        updated_at = now()
                    WHERE id = %s AND claim_token = %s
                    """,
                    (str(res_err), job_id, claim_token),
                )
                db.execute(
                    "UPDATE documents SET status = 'failed', updated_at = now() WHERE id = %s AND tenant_id = %s",
                    (authoritative_doc_id, authoritative_tenant_id),
                )
                raise res_err
    except ExtractionJobError:
        raise
    except Exception as exc:
        logger.error("Failed to acquire lease or resolve document for job %s: %s", job_id, exc)
        raise ExtractionUnauthorizedError(
            f"Worker failed closed during lease acquisition for job_id={job_id}: {exc}"
        ) from exc

    # -------------------------------------------------------------------------
    # PHASE 2: CPU extraction (Docling / parsing — NO DB CONNECTION HELD)
    # -------------------------------------------------------------------------
    from services.extraction.execution import execute_document_extraction

    t0 = time.perf_counter()
    exec_result = execute_document_extraction(
        document_id=authoritative_doc_id,
        file_path=trusted_path,
        parser=_TEST_PARSER,
    )
    elapsed = time.perf_counter() - t0

    if exec_result.get("execution_status") != "EXTRACTED":
        error_msg = exec_result.get("error") or "Extraction execution failed."
        sanitized_error = error_msg.split(":")[-1].strip() if ":" in error_msg else error_msg
        logger.error("Extraction failed for doc %s: %s", authoritative_doc_id, error_msg)

        # Record failure in fresh DB transaction
        try:
            with connect() as db:
                db.execute(
                    """
                    UPDATE pending_jobs
                    SET status = CASE WHEN attempt_count >= max_attempts THEN 'failed' ELSE 'pending' END,
                        finished_at = CASE WHEN attempt_count >= max_attempts THEN now() ELSE NULL END,
                        last_error = %s,
                        next_attempt_at = now() + interval '30 seconds',
                        claim_token = NULL,
                        processing_expires_at = NULL,
                        updated_at = now()
                    WHERE id = %s AND claim_token = %s
                    """,
                    (sanitized_error, job_id, claim_token),
                )
                db.execute(
                    """
                    UPDATE documents
                    SET status = CASE WHEN (SELECT attempt_count >= max_attempts FROM pending_jobs WHERE id = %s) THEN 'failed' ELSE 'uploaded' END,
                        updated_at = now()
                    WHERE id = %s AND tenant_id = %s
                    """,
                    (job_id, authoritative_doc_id, authoritative_tenant_id),
                )
        except Exception as db_err:
            logger.error("Failed to persist error state for job %s: %s", job_id, db_err)

        raise ExtractionExecutionError(
            f"Extraction execution failed with status {exec_result.get('execution_status')} for document_id={authoritative_doc_id}: {sanitized_error}"
        )

    # -------------------------------------------------------------------------
    # PHASE 3: Guarded persistence & finish transaction
    # -------------------------------------------------------------------------
    public_result_path = Path(exec_result["public_result_path"])
    with open(public_result_path, "r", encoding="utf-8") as f:
        public_result = json.load(f)

    with connect() as db:
        # Verify worker lease and tenant/doc identity are still active and valid
        lease_check = db.execute(
            """
            SELECT id, document_id, tenant_id FROM pending_jobs
            WHERE id = %s
              AND claim_token = %s
              AND status = 'processing'
              AND processing_expires_at > now()
            FOR UPDATE
            """,
            (job_id, claim_token),
        ).fetchone()

        if not lease_check:
            logger.error("Worker lease expired or recovered for job %s before completion.", job_id)
            raise ExtractionExecutionError(
                f"Job claim expired or was recovered by sweeper before commit for job_id={job_id}."
            )

        if str(lease_check["document_id"]) != authoritative_doc_id or str(lease_check["tenant_id"]) != authoritative_tenant_id:
            logger.critical("Job identity mismatch during commit for job %s", job_id)
            raise ExtractionExecutionError("Job/document identity mismatch during commit.")

        # Persist M2 extraction output to versioned relational tables
        from services.extraction.persistence import persist_extraction_result

        persist_extraction_result(
            db=db,
            document_id=authoritative_doc_id,
            tenant_id=authoritative_tenant_id,
            public_result=public_result,
            processing_seconds=elapsed,
            pending_job_id=job_id,
        )

        # Invoke M3 validation workflow as separate recoverable step
        from services.validation.workflow import run_document_validation

        run_document_validation(
            db=db,
            document_id=authoritative_doc_id,
            tenant_id=authoritative_tenant_id,
        )

        # Mark pending_jobs succeeded
        db.execute(
            """
            UPDATE pending_jobs
            SET status = 'succeeded',
                finished_at = now(),
                claim_token = NULL,
                processing_expires_at = NULL,
                updated_at = now()
            WHERE id = %s AND claim_token = %s AND status = 'processing'
            """,
            (job_id, claim_token),
        )

    logger.info("Extraction and validation completed successfully for doc_id=%s", authoritative_doc_id)

    return {
        "status": "succeeded",
        "document_id": authoritative_doc_id,
        "job_id": job_id,
        "execution_status": "EXTRACTED",
        "service_run_id": exec_result.get("service_run_id"),
        "review_required": True,
        "accounting_approval_performed": False,
    }
