"""Database-backed queue dispatcher and lease recovery.

Redis carries only the pending-job UUID. PostgreSQL remains the source of truth.
"""
from __future__ import annotations

import os
from datetime import timedelta
from uuid import uuid4

from database.connection import connect
from job_queue.config import get_queues

DISPATCH_LEASE_SECONDS = int(os.getenv("DISPATCH_LEASE_SECONDS", "120"))
PROCESSING_LEASE_SECONDS = int(os.getenv("PROCESSING_LEASE_SECONDS", "900"))


def dispatch_due_jobs(limit: int = 50) -> int:
    """Lease pending jobs in PostgreSQL, then enqueue opaque IDs in Redis."""
    queued = []
    with connect() as db:
        rows = db.execute("""
            SELECT id FROM pending_jobs
            WHERE status='pending' AND next_attempt_at <= now()
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s
        """, (limit,)).fetchall()
        for row in rows:
            token = uuid4()
            updated = db.execute("""
                UPDATE pending_jobs SET status='dispatched', dispatch_token=%s,
                    dispatched_at=now(), dispatch_expires_at=now() + (%s || ' seconds')::interval,
                    dispatch_count=dispatch_count+1, updated_at=now()
                WHERE id=%s AND status='pending' RETURNING id
            """, (token, DISPATCH_LEASE_SECONDS, row['id'])).fetchone()
            if updated:
                queued.append((str(row['id']), str(token), str(row['document_id'])))
    if not queued:
        return 0
    cpu, _ = get_queues()
    for job_id, token, document_id in queued:
        try:
            cpu.enqueue('job_queue.jobs.process_document_extraction_job', document_id, job_id, token,
                        job_id=token, job_timeout=PROCESSING_LEASE_SECONDS)
        except Exception as exc:
            with connect() as db:
                db.execute("""UPDATE pending_jobs SET status='pending', dispatch_token=NULL,
                    dispatched_at=NULL, dispatch_expires_at=NULL, last_error=%s, updated_at=now()
                    WHERE id=%s AND status='dispatched'""", (str(exc)[:2000], job_id))
    return len(queued)


def claim_job(job_id: str, worker_id: str, dispatch_token: str | None = None) -> str | None:
    """Atomically claim a dispatched job and return its claim token."""
    token = uuid4()
    with connect() as db:
        token_filter = " AND dispatch_token=%s" if dispatch_token else ""
        params = [token, PROCESSING_LEASE_SECONDS, job_id]
        if dispatch_token:
            params.append(dispatch_token)
        row = db.execute(f"""UPDATE pending_jobs SET status='processing', claim_token=%s,
            claimed_at=now(), heartbeat_at=now(),
            processing_expires_at=now() + (%s || ' seconds')::interval,
            attempt_count=attempt_count+1, updated_at=now()
            WHERE id=%s AND status='dispatched' AND dispatch_expires_at>now()
            AND attempt_count < max_attempts{token_filter} RETURNING id""", params).fetchone()
    return str(token) if row else None


def claim_document(document_id: str, worker_id: str = "rq-worker") -> tuple[str, str] | None:
    """Claim the current dispatched job for a document."""
    with connect() as db:
        row = db.execute("SELECT id FROM pending_jobs WHERE document_id=%s AND status='dispatched' ORDER BY created_at DESC LIMIT 1", (document_id,)).fetchone()
    if not row:
        return None
    token = claim_job(str(row['id']), worker_id)
    return (str(row['id']), token) if token else None


def heartbeat(job_id: str, claim_token: str) -> bool:
    with connect() as db:
        row = db.execute("""UPDATE pending_jobs SET heartbeat_at=now(),
            processing_expires_at=now() + (%s || ' seconds')::interval, updated_at=now()
            WHERE id=%s AND status='processing' AND claim_token=%s RETURNING id""",
            (PROCESSING_LEASE_SECONDS, job_id, claim_token)).fetchone()
    return bool(row)


def recover_expired_jobs() -> int:
    """Return expired dispatch/processing leases to retry or fail them."""
    with connect() as db:
        rows = db.execute("""UPDATE pending_jobs SET
            status=CASE WHEN attempt_count < max_attempts THEN 'pending' ELSE 'failed' END,
            next_attempt_at=now() + (GREATEST(attempt_count,1) * 10 || ' seconds')::interval,
            last_error=COALESCE(last_error,'Worker lease expired'),
            finished_at=CASE WHEN attempt_count >= max_attempts THEN now() ELSE NULL END,
            dispatch_token=NULL, claim_token=NULL, dispatch_expires_at=NULL,
            processing_expires_at=NULL, updated_at=now()
            WHERE (status='dispatched' AND dispatch_expires_at < now())
               OR (status='processing' AND processing_expires_at < now())
            RETURNING id""").fetchall()
    return len(rows)
