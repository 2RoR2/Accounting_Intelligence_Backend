"""In-process extraction runner. PostgreSQL is the queue; there is no Redis.

A delivery reserved by attempts.reserve_dispatch() is handed to a bounded thread
pool in this process. The worker thread claims it through the same SQL fence the
RQ worker uses (attempts.claim_delivery), so a duplicate or stale hand-off cannot
run or persist anything.

A supervisor thread, started from the API lifespan, repeats what the separate
queue-supervisor service did: close expired leases and dispatch waiting jobs.

Extraction here is OCR on a few CPU threads plus one outbound model request, so a
small pool is enough. INLINE_WORKERS (default 2, 1-8) bounds concurrent documents
and therefore peak memory.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

_lock = threading.Lock()
_inflight: set[str] = set()
_executor: ThreadPoolExecutor | None = None
_supervisor: threading.Thread | None = None
_stop = threading.Event()


class InlineQueueFull(RuntimeError):
    """Every worker thread is busy; the reservation is released for a later cycle."""


def max_workers() -> int:
    text = os.getenv('INLINE_WORKERS', '2').strip()
    if not text.isdecimal() or not 1 <= int(text) <= 8:
        raise RuntimeError('INLINE_WORKERS must be an integer from 1 to 8')
    return int(text)


def capacity() -> int:
    """Free worker threads right now; dispatch reserves no more than this."""
    with _lock:
        return max(0, max_workers() - len(_inflight))


def inflight() -> int:
    with _lock:
        return len(_inflight)


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=max_workers(), thread_name_prefix='extraction')
    return _executor


def submit(delivery) -> None:
    """Hand one reserved delivery to a worker thread. Raises when no thread is free."""
    with _lock:
        if delivery.dispatch_token in _inflight:
            return  # Already handed over; the SQL claim would reject a second run anyway.
        if len(_inflight) >= max_workers():
            raise InlineQueueFull('INLINE_QUEUE_FULL')
        _inflight.add(delivery.dispatch_token)
        pool = _pool()
    try:
        pool.submit(_run, delivery)
    except BaseException:
        with _lock:
            _inflight.discard(delivery.dispatch_token)
        raise


def _run(delivery) -> None:
    from services.queue.jobs import process_inline_delivery
    try:
        process_inline_delivery(delivery)
    except Exception:
        # The failure is already recorded against the claim; never kill the pool thread.
        log.exception('Inline extraction failed for document %s', delivery.document_id)
    finally:
        with _lock:
            _inflight.discard(delivery.dispatch_token)
        _wake_dispatch()


def _wake_dispatch() -> None:
    """A thread just became free: pull the next waiting job without waiting for the timer."""
    if _stop.is_set():
        return
    try:
        from services.queue.durable import dispatch_due_jobs
        dispatch_due_jobs()
    except Exception:
        log.exception('Inline dispatch after completion failed; the supervisor will retry')


def stale_lease_seconds() -> int:
    """A live worker heartbeats every 30 s; three missed beats mean nobody is running the job."""
    text = os.getenv('INLINE_STALE_LEASE_SECONDS', '90').strip()
    if not text.isdecimal() or not 60 <= int(text) <= 3600:
        raise RuntimeError('INLINE_STALE_LEASE_SECONDS must be an integer from 60 to 3600')
    return int(text)


def release_orphaned_leases() -> int:
    """Expire leases whose worker stopped heartbeating, so recovery can retry them now.

    An inline job lives in the process that claimed it. After a restart (deploy, crash,
    or a free host waking from sleep) nothing runs it, yet its lease would block the
    document for up to the 30-minute processing window. Expiring it lets the ordinary
    recovery path close the attempt, keep its history and schedule a bounded retry.

    Only leases that have not been renewed for INLINE_STALE_LEASE_SECONDS are touched, so
    a job running in another live process (a second worker, or a teammate's API on a
    shared database) is never cut short, and the same queue scope as dispatch applies.
    """
    from database.connection import connect
    from services.queue.scope import scope_bindings, scope_clause
    with connect() as db:
        rows = db.execute("""UPDATE pending_jobs j
            SET dispatch_expires_at = CASE WHEN j.status='dispatched'
                    THEN clock_timestamp() - interval '1 second' ELSE j.dispatch_expires_at END,
                processing_expires_at = CASE WHEN j.status='processing'
                    THEN clock_timestamp() - interval '1 second' ELSE j.processing_expires_at END,
                updated_at = clock_timestamp()
            FROM documents d
            WHERE d.id=j.document_id AND j.status IN ('dispatched','processing')
              AND GREATEST(j.heartbeat_at, j.updated_at) < clock_timestamp() - (%s * interval '1 second')
            """ + scope_clause() + " RETURNING j.id",
            (stale_lease_seconds(), *scope_bindings())).fetchall()
    return len(rows)


def run_cycle() -> tuple[int, int]:
    from services.queue.durable import dispatch_due_jobs, recover_expired_jobs
    if os.getenv('INLINE_RECOVER_ON_START', 'true').strip().lower() == 'true':
        released = release_orphaned_leases()
        if released:
            log.info('Released %s lease(s) whose worker stopped heartbeating', released)
    recovered = recover_expired_jobs()
    dispatched = dispatch_due_jobs(limit=int(os.getenv('QUEUE_DISPATCH_BATCH', '50')))
    return recovered, dispatched


def _supervise(interval: float) -> None:
    # Each cycle first releases leases nobody is heartbeating (see release_orphaned_leases),
    # so a job left by a restarted process is retried within about INLINE_STALE_LEASE_SECONDS.
    while not _stop.is_set():
        try:
            recovered, dispatched = run_cycle()
            if recovered or dispatched:
                log.info('queue cycle: recovered=%s dispatched=%s', recovered, dispatched)
        except Exception:
            log.exception('Inline queue cycle failed')
        _stop.wait(interval)


def start_supervisor() -> bool:
    """Start the background recovery/dispatch loop once per process."""
    global _supervisor
    if os.getenv('INLINE_SUPERVISOR', 'true').strip().lower() != 'true':
        return False
    with _lock:
        if _supervisor is not None and _supervisor.is_alive():
            return False
        _stop.clear()
        interval = max(1.0, float(os.getenv('QUEUE_SUPERVISOR_INTERVAL_SECONDS', '5')))
        _supervisor = threading.Thread(target=_supervise, args=(interval,), name='queue-supervisor', daemon=True)
        _supervisor.start()
    return True


def stop_supervisor(timeout: float = 5.0) -> None:
    """Stop dispatching. Running documents finish or are recovered by their lease."""
    global _supervisor, _executor
    _stop.set()
    thread, _supervisor = _supervisor, None
    if thread is not None:
        thread.join(timeout=timeout)
    pool, _executor = _executor, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def status() -> dict:
    return {'backend': 'inline', 'workers': max_workers(), 'busy': inflight(),
            'supervisor': bool(_supervisor is not None and _supervisor.is_alive())}
