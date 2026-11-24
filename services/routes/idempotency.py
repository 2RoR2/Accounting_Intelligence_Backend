"""Safe retries for write actions: an optional ``Idempotency-Key`` header.

A browser double click, or a retry after a timeout, sends the same request twice. When the request carries an
``Idempotency-Key`` (1-160 characters), the first successful response is kept in ``idempotency_keys`` under
(company, user, key) and a repeat with the same key and the same request gets that response back: no second record,
no second action. The same key with a different request (another body or another URL) is refused with 409.
Without the header nothing changes.

Concurrency: while one request with a key runs, a transaction-scoped advisory lock on (company, user, key) is held on
a separate connection, so a parallel twin waits and then replays the stored response. The unique constraint on the
table is the second guard. Only 2xx results are stored: a request that failed (validation, permission, a closed
period) rolled back its own work, so retrying it with the same key runs it again.

Known gap: if the process dies after the action commits but before the response is stored, a retry runs the action
again. The existing per-action guards (a repeated "ready" posts once, a void is idempotent) still apply.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

HEADER = "Idempotency-Key"
MAX_KEY_LENGTH = 160
RETENTION = "7 days"  # rows older than this are removed for the same user on their next keyed request
LOCK_TIMEOUT = "30s"
log = logging.getLogger(__name__)


def _key(request: Request) -> str | None:
    raw = request.headers.get(HEADER)
    if raw is None:
        return None
    key = raw.strip()
    if not 1 <= len(key) <= MAX_KEY_LENGTH:
        raise HTTPException(422, f"The {HEADER} header must be 1 to {MAX_KEY_LENGTH} characters.")
    return key


def _request_hash(request: Request, arguments: dict) -> str:
    payload = {"method": request.method, "path": request.url.path, "query": sorted(request.query_params.multi_items()),
               "arguments": jsonable_encoder(arguments)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _lock_id(tenant_id, user_id, key: str) -> int:
    digest = hashlib.sha256(f"saic:idempotency:v1:{tenant_id}:{user_id}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _status_code(request: Request) -> int:
    route = request.scope.get("route")
    return getattr(route, "status_code", None) or 200


def _replay(row, request_hash: str) -> JSONResponse:
    if row["request_hash"] != request_hash:
        raise HTTPException(409, "This retry key was already used for a different request. Reload the page and try again.")
    return JSONResponse(row["response_body"], status_code=row["response_status"], headers={"Idempotency-Replayed": "true"})


def idempotent(handler):
    """Wrap a sync FastAPI route handler that takes ``request: Request``; the signature is kept for FastAPI."""

    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        request = kwargs.get("request")
        key = _key(request) if isinstance(request, Request) else None
        if key is None:
            return handler(*args, **kwargs)
        import main  # late: route modules load while main is still importing

        user = main.require_workspace_user(request)
        if not user.get("tenant_id"):
            return handler(*args, **kwargs)
        tenant_id, user_id = user["tenant_id"], user["id"]
        request_hash = _request_hash(request, {name: value for name, value in kwargs.items() if name != "request"})
        route = f"{request.method} {request.url.path}"[:300]
        try:
            with main.connect() as guard:
                guard.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                try:
                    guard.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(tenant_id, user_id, key),))
                except pg_errors.LockNotAvailable:
                    raise HTTPException(409, "This action is still being processed. Wait a moment, then check before trying again.")
                row = guard.execute("""SELECT request_hash,response_status,response_body FROM idempotency_keys
                    WHERE tenant_id=%s AND user_id=%s AND idempotency_key=%s""", (tenant_id, user_id, key)).fetchone()
                if row:
                    return _replay(row, request_hash)
                result = handler(*args, **kwargs)
                if isinstance(result, Response):
                    return result  # files and streams are not stored
                body, status = jsonable_encoder(result), _status_code(request)
                stored = guard.execute("""INSERT INTO idempotency_keys
                        (tenant_id,user_id,idempotency_key,route,request_hash,response_status,response_body)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id,user_id,idempotency_key) DO NOTHING RETURNING id""",
                    (tenant_id, user_id, key, route, request_hash, status, Jsonb(body))).fetchone()
                if stored is None:  # not expected while the advisory lock is held
                    log.warning("idempotency key stored twice for route %s", route)
                guard.execute(f"DELETE FROM idempotency_keys WHERE tenant_id=%s AND user_id=%s AND created_at < now() - interval '{RETENTION}'",
                              (tenant_id, user_id))
                return body
        except pg_errors.UndefinedTable:
            # The migration has not been applied yet: behave as before rather than fail the save.
            log.warning("idempotency_keys table missing; running %s without retry protection", route)
            return handler(*args, **kwargs)

    # Route modules use `from __future__ import annotations`; FastAPI would resolve those strings against this module.
    wrapper.__signature__ = inspect.signature(handler, eval_str=True)
    return wrapper
