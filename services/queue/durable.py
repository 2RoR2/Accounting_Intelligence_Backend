"""Capacity-aware outbox dispatcher and fenced, append-only attempt recovery."""
import logging
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from database.connection import connect
from services.queue import attempts
from services.queue.scope import scope_clause, scope_bindings

log = logging.getLogger(__name__)


def get_queues():
    """Redis/RQ is imported only when the legacy transport is in use."""
    from services.queue.config import get_queues as _get_queues
    return _get_queues()


def _rq_capacity():
    """Idle, healthy RQ workers that are not already owed a queued job."""
    from rq.worker import Worker
    import socket
    cpu, _ = get_queues()
    ready = [w for w in Worker.all(connection=cpu.connection)
               if cpu.name in [q.name for q in w.queues]
               and cpu.connection.ttl(w.key) > 0 and w.get_state() == 'idle'
               and not getattr(w, '_shutdown_requested_date', None)
               and (w.hostname != socket.gethostname() or local_process_alive(w.pid) is not False)]
    # Count available workers, not all historical started executions. A lost
    # worker must not consume the capacity of a different, healthy idle worker.
    return max(0, len(ready)-len(cpu))


def dispatch_due_jobs(limit=50):
    """Dispatch only what healthy workers can claim before its lease expires."""
    from services.queue.backend import queue_backend
    if queue_backend() == 'inline':
        from services.queue import inline
        capacity = inline.capacity()
    else:
        capacity = _rq_capacity()
    if not capacity:
        return 0
    deliveries = []
    with connect() as db:
        rows = db.execute("""SELECT j.id,j.document_id,j.tenant_id FROM pending_jobs j
            JOIN documents d ON d.id=j.document_id AND d.tenant_id=j.tenant_id
            WHERE j.status='pending' AND j.next_attempt_at<=clock_timestamp() AND d.status='queued'
            """ + scope_clause() + """
            ORDER BY EXISTS (SELECT 1 FROM pending_jobs old WHERE old.document_id=j.document_id
                AND old.status='failed' AND old.last_error IS DISTINCT FROM 'LEASE_EXPIRED') DESC,
                j.created_at FOR UPDATE OF d SKIP LOCKED LIMIT %s""",
            (*scope_bindings(), min(limit, capacity))).fetchall()
        for row in rows:
            try:
                with db.transaction():
                    deliveries.append(attempts.reserve_dispatch(db,str(row['document_id']),str(row['tenant_id']),job_id=str(row['id'])))
            except attempts.AttemptRejected as exc:
                log.warning('Outbox job %s is not dispatchable: %s',row['id'],exc.code)
    sent = 0
    for delivery in deliveries:
        try:
            attempts.publish(delivery)
            sent += 1
        except attempts.AttemptRejected:
            log.exception('Dispatch failed for attempt %s',delivery.job_id)
    return sent


def recover_expired_jobs():
    """Close stale attempts without erasing evidence, then schedule eligible retries."""
    recovered = 0
    with connect() as db:
        rows = db.execute("""SELECT j.id,j.document_id,j.tenant_id FROM pending_jobs j
            JOIN documents d ON d.id=j.document_id AND d.tenant_id=j.tenant_id
            WHERE ((j.status='dispatched' AND j.dispatch_expires_at<clock_timestamp())
               OR (j.status='processing' AND j.processing_expires_at<clock_timestamp()))
            """ + scope_clause() + """
            ORDER BY j.created_at FOR UPDATE OF d SKIP LOCKED""", scope_bindings()).fetchall()
        for row in rows:
            document = attempts.lock_document(db,str(row['document_id']),str(row['tenant_id']))
            job=db.execute('SELECT * FROM pending_jobs WHERE id=%s FOR UPDATE',(row['id'],)).fetchone()
            deadline=job['processing_expires_at'] if job['status']=='processing' else job['dispatch_expires_at']
            if job['status'] not in {'dispatched','processing'} or deadline is None or deadline>attempts.now(db):
                continue
            try:
                attempts.ensure_unreviewed(db,document)
                protected=False
            except attempts.AttemptRejected:
                protected=True
            db.execute("""UPDATE pending_jobs SET status=%s,finished_at=clock_timestamp(),
                last_error=%s,updated_at=clock_timestamp() WHERE id=%s""",
                ('cancelled' if protected else 'failed','DOCUMENT_PROTECTED' if protected else 'LEASE_EXPIRED',job['id']))
            attempts.audit(db,document,job['id'],'extraction.cancelled' if protected else 'extraction.lease_expired')
            if not protected:
                if attempts.retry_budget(db,str(document['id']),job['max_attempts']):
                    db.execute("""INSERT INTO pending_jobs(id,tenant_id,document_id,status,next_attempt_at,max_attempts,attempt_count)
                        VALUES (%s,%s,%s,'pending',clock_timestamp(),%s,0)""",(uuid4(),row['tenant_id'],row['document_id'],job['max_attempts']))
                    state='queued'
                else:
                    state='failed'
                db.execute('UPDATE documents SET status=%s,updated_at=clock_timestamp() WHERE id=%s',(state,row['document_id']))
            recovered += 1
    return recovered


def heartbeat(job_id, claim_token):
    """Compatibility entry point; never extend a stale or replaced claim."""
    with connect() as db:
        row=db.execute('SELECT * FROM pending_jobs WHERE id=%s AND claim_token=%s',(job_id,claim_token)).fetchone()
    if not row: return False
    proof=attempts.Claim(attempts.Delivery(str(row['document_id']),str(row['id']),str(row['tenant_id']),str(row['dispatch_token'])),str(claim_token))
    return attempts.heartbeat_claim(proof)


def local_process_alive(pid):
    """Conservative Windows death proof; never use os.kill(pid, 0) on Windows."""
    import os
    if os.name != 'nt' or not pid:
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)


def recover_lost_local_workers():
    """Recover only positively dead local processes, preserving failed history."""
    import socket
    from rq import Worker
    from rq.job import Job, JobStatus
    from rq.serializers import JSONSerializer
    cpu, _ = get_queues()
    recovered = 0
    for worker in Worker.all(connection=cpu.connection):
        if (worker.hostname != socket.gethostname() or cpu.name not in [q.name for q in worker.queues]
                or local_process_alive(worker.pid) is not False):
            continue
        rq_id = worker.get_current_job_id()
        if not rq_id:
            continue
        job = Job.fetch(rq_id, connection=cpu.connection, serializer=JSONSerializer)
        if job.get_status() != JobStatus.STARTED:
            continue
        with connect() as db:
            row = db.execute('SELECT j.* FROM pending_jobs j JOIN documents d ON d.id=j.document_id WHERE j.dispatch_token=%s ' + scope_clause(), (rq_id, *scope_bindings())).fetchone()
            if not row:
                continue
            document = attempts.lock_document(db, str(row['document_id']), str(row['tenant_id']))
            current = db.execute('SELECT * FROM pending_jobs WHERE id=%s FOR UPDATE', (row['id'],)).fetchone()
            if current['status'] == 'processing':
                try:
                    attempts.ensure_unreviewed(db, document)
                    protected = False
                except attempts.AttemptRejected:
                    protected = True
                db.execute("""UPDATE pending_jobs SET status=%s,last_error='WORKER_EXITED',
                    finished_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s""",
                    ('cancelled' if protected else 'failed', row['id']))
                attempts.audit(db, document, row['id'], 'extraction.worker_exited')
                if not protected:
                    if attempts.retry_budget(db, str(document['id']), current['max_attempts']):
                        db.execute("""INSERT INTO pending_jobs(id,tenant_id,document_id,status,next_attempt_at,attempt_count)
                            VALUES (%s,%s,%s,'pending',clock_timestamp(),0)""", (uuid4(), row['tenant_id'], row['document_id']))
                        state = 'queued'
                    else:
                        state = 'failed'
                    db.execute('UPDATE documents SET status=%s,updated_at=clock_timestamp() WHERE id=%s', (state, row['document_id']))
                recovered += 1
            elif current['status'] not in {'failed', 'cancelled'}:
                continue
        # Use RQ's ordinary failure lifecycle, retaining the failed job/result.
        executions = job.get_executions()
        worker.execution = next((e for e in executions if e.worker_name == worker.name), None)
        worker.serializer = JSONSerializer
        job.ended_at = datetime.now(timezone.utc)
        worker.handle_job_failure(job, cpu, exc_string='Worker process exited before extraction completed (WORKER_EXITED).')
    return recovered
