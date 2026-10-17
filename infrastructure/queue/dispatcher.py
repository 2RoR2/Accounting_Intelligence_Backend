"""
Durable Outbox Dispatcher & Sweeper for 'pending_jobs'.
Implements Aleeya's lease fencing, dispatch token generation,
dispatch_count tracking, and crash/OOM sweep recovery.

Two-phase commit ordering:
1. Database dispatch lease and token are committed and visible FIRST.
2. Redis RQ enqueue occurs only AFTER database commit.
3. If no queue is provided, jobs are NEVER marked as dispatched.
4. If Redis enqueue fails, the job lease is safely reverted to 'pending'.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def dispatch_pending_jobs(
    db: Any,
    queue: Any = None,
    batch_size: int = 10,
) -> List[Dict[str, Any]]:
    """
    Polls the pending_jobs outbox table, acquires dispatch leases using
    SELECT ... FOR UPDATE SKIP LOCKED, commits the dispatch state to PostgreSQL,
    and then enqueues to Redis RQ using dispatch_token as the RQ job ID.

    Guarantees:
    - If queue is None, jobs are NEVER marked as dispatched.
    - PostgreSQL commit occurs BEFORE Redis enqueue, guaranteeing visibility to workers.
    - Zero double-dispatching across multiple concurrent dispatchers.
    - Redis RQ Job ID is the unique dispatch_token UUID.
    - dispatch_count increments on every dispatch attempt.
    - attempt_count remains unchanged until a worker actually claims the job.
    - Automatic rollback to 'pending' if Redis enqueue fails.
    """
    if queue is None:
        logger.debug("No queue provided to dispatch_pending_jobs; skipping dispatch.")
        return []

    # 1. Phase 1: Atomically claim up to batch_size claimable jobs directly into 'dispatched'
    dispatched_rows = db.execute(
        """
        WITH claimable AS (
            SELECT id FROM pending_jobs
            WHERE (status = 'pending' AND next_attempt_at <= now())
               OR (status = 'dispatched' AND dispatch_expires_at < now())
            ORDER BY created_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE pending_jobs pj
        SET status = 'dispatched',
            dispatch_token = gen_random_uuid(),
            dispatched_at = now(),
            dispatch_count = pj.dispatch_count + 1,
            dispatch_expires_at = now() + interval '2 minutes',
            updated_at = now()
        FROM claimable
        WHERE pj.id = claimable.id
        RETURNING pj.id, pj.document_id, pj.tenant_id, pj.dispatch_token, pj.dispatch_count, pj.attempt_count
        """,
        (batch_size,),
    ).fetchall()

    if not dispatched_rows:
        return []

    # Update document status to 'queued' for all claimed rows
    for row in dispatched_rows:
        db.execute(
            """
            UPDATE documents
            SET status = 'queued',
                updated_at = now()
            WHERE id = %s AND status = 'uploaded'
            """,
            (row["document_id"],),
        )

    # CRITICAL: Commit Phase 1 changes in PostgreSQL so the dispatch token and
    # 'dispatched' status are durable and visible to external worker connections BEFORE enqueue!
    if hasattr(db, "commit"):
        try:
            db.commit()
        except Exception as commit_err:
            logger.error("Failed to commit dispatch lease in PostgreSQL: %s", commit_err)
            return []

    dispatched = []

    # 2. Phase 2: Enqueue to Redis RQ
    for row in dispatched_rows:
        job_id_str = str(row["id"])
        doc_id_str = str(row["document_id"])
        token_str = str(row["dispatch_token"])
        redis_job_id = token_str

        try:
            queue.enqueue(
                "infrastructure.queue.jobs.process_document_extraction_job",
                args=(doc_id_str,),
                kwargs={
                    "job_id": job_id_str,
                    "dispatch_token": token_str,
                },
                job_id=redis_job_id,
            )
            logger.info("Enqueued job %s to Redis RQ with token: %s", job_id_str, redis_job_id)
            dispatched.append({
                "job_id": job_id_str,
                "document_id": doc_id_str,
                "tenant_id": str(row["tenant_id"]),
                "dispatch_token": token_str,
                "rq_job_id": redis_job_id,
            })
        except Exception as exc:
            logger.error("Failed to enqueue job %s to Redis: %s. Reverting lease to pending.", job_id_str, exc)
            # Safe recovery: Revert dispatch lease back to pending so it is not stuck
            db.execute(
                """
                UPDATE pending_jobs
                SET status = 'pending',
                    dispatch_token = NULL,
                    dispatch_expires_at = NULL,
                    last_error = %s,
                    updated_at = now()
                WHERE id = %s AND dispatch_token = %s
                """,
                (f"Redis enqueue failed: {exc}", row["id"], row["dispatch_token"]),
            )
            db.execute(
                """
                UPDATE documents
                SET status = 'uploaded',
                    updated_at = now()
                WHERE id = %s AND status = 'queued'
                """,
                (row["document_id"],),
            )
            if hasattr(db, "commit"):
                try:
                    db.commit()
                except Exception:
                    pass

    return dispatched


def sweep_stale_jobs(db: Any) -> int:
    """
    Sweeper process to recover abandoned or crashed worker leases.
    Recovers expired dispatch leases (worker never picked up) and
    expired processing leases (worker crash or OOM).
    Resets to 'pending' with cleared tokens, or marks 'failed' if max attempts reached.
    """
    swept_rows = db.execute(
        """
        UPDATE pending_jobs
        SET status = CASE
                WHEN attempt_count >= max_attempts THEN 'failed'
                ELSE 'pending'
            END,
            finished_at = CASE
                WHEN attempt_count >= max_attempts THEN now()
                ELSE NULL
            END,
            claim_token = NULL,
            dispatch_token = NULL,
            dispatch_expires_at = NULL,
            processing_expires_at = NULL,
            next_attempt_at = now() + interval '30 seconds',
            last_error = 'Job lease timed out (dispatch lost or worker crash/OOM detected).',
            updated_at = now()
        WHERE (status = 'processing' AND processing_expires_at < now())
           OR (status = 'dispatched' AND dispatch_expires_at < now())
        RETURNING id, document_id, attempt_count, max_attempts
        """
    ).fetchall()

    for row in swept_rows:
        if row["attempt_count"] >= row["max_attempts"]:
            db.execute(
                """
                UPDATE documents
                SET status = 'failed',
                    updated_at = now()
                WHERE id = %s
                """,
                (row["document_id"],),
            )
            logger.warning(
                "Job %s reached max attempts (%d); marked document %s as failed.",
                row["id"],
                row["max_attempts"],
                row["document_id"],
            )

    if hasattr(db, "commit"):
        try:
            db.commit()
        except Exception:
            pass

    return len(swept_rows)
