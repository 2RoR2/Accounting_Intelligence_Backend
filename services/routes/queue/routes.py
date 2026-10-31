from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto

def list_processing_jobs(request: Request):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    with connect() as db:
        rows = db.execute("SELECT * FROM pending_jobs WHERE (%s IS NULL OR tenant_id=%s) ORDER BY created_at DESC", (scope, scope)).fetchall()
    return [{"id": str(r["id"]), "documentId": str(r["document_id"]), "tenantId": str(r["tenant_id"]), "status": r["status"].upper(), "attempts": r["attempt_count"], "createdAt": str(r["created_at"]), "completedAt": str(r["finished_at"] or "")} for r in rows]

def get_processing_job(job_id: str, request: Request):
    user = require_workspace_user(request)
    with connect() as db:
        row = db.execute("SELECT * FROM pending_jobs WHERE id=%s AND tenant_id=%s", (job_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Processing job not found.")
    error = row["last_error"]
    return {"id": str(row["id"]), "documentId": str(row["document_id"]), "tenantId": str(row["tenant_id"]), "status": row["status"].upper(), "attempts": row["attempt_count"], "maxAttempts": row["max_attempts"], "lastError": error, "error": error, "createdAt": str(row["created_at"]), "finishedAt": str(row["finished_at"] or "")}

def processing_health(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    redis_status = "unavailable"
    worker_online = False
    try:
        from services.queue.config import get_queues
        from rq import Worker
        cpu_queue, io_queue = get_queues()
        cpu_queue.connection.ping()
        io_queue.connection.ping()
        redis_status = "healthy"
        worker_online = any(
            any(queue.name == "cpu_tasks" for queue in worker.queues)
            for worker in Worker.all(connection=cpu_queue.connection)
        )
    except Exception:
        pass
    with connect() as db:
        scope = tenant_scope(user)
        pending = db.execute("SELECT count(*) AS n FROM pending_jobs WHERE status IN ('pending','processing') AND (%s IS NULL OR tenant_id=%s)", (scope, scope)).fetchone()["n"]
    return {"status": "healthy" if redis_status == "healthy" and worker_online else "degraded", "worker": worker_online, "redis": redis_status, "pendingJobs": int(pending)}

def processing_workers(request: Request):
    user = require_workspace_user(request, {"super_admin", "local_admin"})
    health = processing_health(request)
    return [{"name": "default", "queue": "cpu_tasks", "status": "healthy" if health["worker"] else "unavailable", "redis": health["redis"], "tenantScope": "platform" if user["role"] == "super_admin" else "tenant"}]

def processing_metrics(request: Request):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    with connect() as db:
        row = db.execute("""SELECT count(*) FILTER (WHERE j.status IN ('succeeded','failed')) AS processed,
            coalesce(avg(d.processing_seconds),0) AS average_seconds
            FROM pending_jobs j LEFT JOIN documents d ON d.id=j.document_id
            WHERE (%s IS NULL OR j.tenant_id=%s)""", (scope, scope)).fetchone()
        exception_filter = " AND EXISTS (SELECT 1 FROM documents d WHERE d.id=exceptions.document_id AND (d.owner_id=%s OR d.assigned_to=%s))" if user["role"] == "accountant" else ""
        exception_params = [scope, scope]
        if user["role"] == "accountant":
            exception_params.extend([user["id"], user["id"]])
        exceptions = db.execute(f"SELECT count(*) AS n FROM exceptions WHERE (%s IS NULL OR tenant_id=%s) AND status <> 'resolved'{exception_filter}", exception_params).fetchone()["n"]
    processed = int(row["processed"] or 0)
    avg_seconds = float(row["average_seconds"] or 0)
    return [{"label": "Processed documents", "value": str(processed), "target": "", "met": True, "change": ""}, {"label": "Avg. processing time", "value": f"{avg_seconds:.1f}s", "target": "= 20 sec", "met": avg_seconds <= 20 or not avg_seconds, "change": ""}, {"label": "Open exceptions", "value": str(exceptions), "target": "", "met": exceptions == 0, "change": ""}]

def processing_trends(request: Request, period: str = "week"):
    user = require_workspace_user(request)
    scope = tenant_scope(user)
    days = 30 if period in {"month", "30"} else 7
    with connect() as db:
        rows = db.execute("""SELECT date_trunc('day', j.created_at) AS day, count(*) AS processed,
            count(*) FILTER (WHERE j.status='succeeded') AS succeeded,
            count(e.id) AS exceptions
            FROM pending_jobs j LEFT JOIN exceptions e ON e.document_id=j.document_id
            WHERE j.created_at >= now() - (%s || ' days')::interval AND (%s IS NULL OR j.tenant_id=%s)
            GROUP BY 1 ORDER BY 1""", (days, scope, scope)).fetchall()
    return [{"day": r["day"].strftime("%d %b"), "processed": int(r["processed"]), "accuracy": round((int(r["succeeded"]) / int(r["processed"]) * 100) if r["processed"] else 0, 1), "exceptions": int(r["exceptions"])} for r in rows]
"""Queue API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/processing/jobs', list_processing_jobs, methods=['GET'])
    router.add_api_route('/api/processing/jobs/{job_id}', get_processing_job, methods=['GET'])
    router.add_api_route('/api/processing/health', processing_health, methods=['GET'])
    router.add_api_route('/api/processing/workers', processing_workers, methods=['GET'])
    router.add_api_route('/api/processing/metrics', processing_metrics, methods=['GET'])
    router.add_api_route('/api/processing/trends', processing_trends, methods=['GET'])
