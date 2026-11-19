"""Read failed documents again, in bulk, with the current reading settings (plan v3, G7).

Documents the old engine (or a provider outage) left FAILED are queued for a fresh
extraction attempt through the same durable path as the single-document retry
(`attempts.reserve_dispatch(retry=True)`): earlier attempts are kept, the document goes
back to `queued`, and whichever reader the API runs now (lite engine, current AI setting)
reads it. Nothing is approved or posted.

Rules
- Local admin: any document of their company. Accountant: documents they own or that
  are assigned to them. Another company's document is reported as not found and never
  touched.
- Only FAILED documents; anything else is skipped with the reason.
- Budget: at most READ_AGAIN_LIMIT bulk read-again requests per document, counted from
  their audit rows, so a file that can never be read is not retried forever. Worker claims
  used by the old engine or by automatic lease recovery (`attempts.retry_budget`) do not
  count: they say nothing about the current reader, and old-engine failures have usually
  used all three, which would make this action refuse exactly the documents it is for.
  The single-document retry has no budget at all.
- One transaction and one user-attributed audit row per queued document; one document's
  refusal never undoes another's.
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from database.connection import connect

MAX_DOCUMENTS = 200
READ_AGAIN_LIMIT = 3
AUDIT_ACTION = "extraction.read_again_requested"

# Plain-language reasons for the per-document result list.
REASONS = {
    "NOT_FOUND": "Not found in your documents.",
    "NOT_FAILED": "Only documents that could not be read are read again.",
    "ATTEMPT_BUDGET_USED": f"Already read again {READ_AGAIN_LIMIT} times; open it to check the file.",
    "AMENDMENT_REQUIRED": "Already reviewed or approved; it needs an amendment instead.",
    "JOB_ALREADY_OPEN": "Already being read.",
    "DOCUMENT_NOT_RETRYABLE": "Its status changed before it could be queued.",
    "JOB_NOT_DISPATCHABLE": "Its reading job could not be queued.",
    "ERROR": "Could not be queued; try again.",
}


class ReadAgainInput(BaseModel):
    documentIds: list[str] | None = Field(default=None, min_length=1, max_length=MAX_DOCUMENTS)
    allFailed: bool = False

    @model_validator(mode="after")
    def one_selection(self):
        if bool(self.documentIds) == self.allFailed:
            raise ValueError("Send either documentIds or allFailed: true.")
        return self


def _visibility(user):
    """SQL and parameters limiting an accountant to their own or assigned documents."""
    if user["role"] == "accountant":
        return " AND (d.owner_id=%s OR d.assigned_to=%s)", [user["id"], user["id"]]
    return "", []


def _valid_ids(document_ids):
    valid, invalid = [], []
    for value in dict.fromkeys(document_ids):
        try:
            valid.append(str(uuid.UUID(str(value))))
        except (ValueError, TypeError, AttributeError):
            invalid.append(value)
    return valid, invalid


def _candidates(user, data):
    """(documents to consider in a stable order, requested ids that are not visible)."""
    tenant_id = user["tenant_id"]
    visibility, extra = _visibility(user)
    with connect() as db:
        if data.allFailed:
            rows = db.execute(f"""SELECT d.id, d.document_code, d.original_filename, d.status FROM documents d
                WHERE d.tenant_id=%s AND d.status='failed'{visibility}
                ORDER BY d.uploaded_at, d.id LIMIT %s""", [tenant_id, *extra, MAX_DOCUMENTS]).fetchall()
            return list(rows), []
        valid, invalid = _valid_ids(data.documentIds)
        rows = db.execute(f"""SELECT d.id, d.document_code, d.original_filename, d.status FROM documents d
            WHERE d.tenant_id=%s AND d.id=ANY(%s::uuid[]){visibility}""", [tenant_id, valid, *extra]).fetchall() if valid else []
    by_id = {str(row["id"]): row for row in rows}
    ordered = [by_id[document_id] for document_id in valid if document_id in by_id]
    missing = invalid + [document_id for document_id in valid if document_id not in by_id]
    return ordered, missing


def _queue_one(user, document):
    """Queue one failed document in its own transaction. Returns the new job id."""
    from services.queue import attempts
    document_id, tenant_id = str(document["id"]), str(user["tenant_id"])
    with connect() as db:
        db.execute("SELECT set_config('app.user_id',%s,true), set_config('app.status_reason',%s,true)",
                   (str(user["id"]), "Read again (bulk)"))
        locked = attempts.lock_document(db, document_id, tenant_id)
        visibility, extra = _visibility(user)
        # Re-check under the lock: assignment or status may have changed since the selection.
        if visibility and not db.execute("SELECT 1 FROM documents d WHERE d.id=%s" + visibility,
                                         [document_id, *extra]).fetchone():
            raise attempts.AttemptRejected("NOT_FOUND")
        if locked["status"] != "failed":
            raise attempts.AttemptRejected("NOT_FAILED")
        used = db.execute("SELECT count(*) AS n FROM audit_logs WHERE tenant_id=%s AND resource=%s AND action=%s",
                          (tenant_id, document_id, AUDIT_ACTION)).fetchone()["n"]
        if used >= READ_AGAIN_LIMIT:
            raise attempts.AttemptRejected("ATTEMPT_BUDGET_USED")
        job = attempts.reserve_dispatch(db, document_id, tenant_id, retry=True, defer=True)
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (tenant_id, user["id"], user.get("name", "Workspace user"), user["role"], AUDIT_ACTION, document_id,
             json.dumps({"jobId": str(job["id"]), "bulk": True})))
    return str(job["id"])


def _wake_dispatcher():
    """Best effort, as for a single retry: committed jobs are picked up by the supervisor anyway."""
    try:
        from services.queue.durable import dispatch_due_jobs
        dispatch_due_jobs()
    except Exception:
        logging.getLogger(__name__).exception("Dispatcher wakeup after bulk read-again failed; supervisor will retry")


def read_again(user, data: ReadAgainInput):
    """Queue the selected (or all) FAILED documents; one result per document."""
    if user["role"] not in {"local_admin", "accountant"} or not user.get("tenant_id"):
        raise HTTPException(403, "Reading documents again requires a company accountant or administrator.")
    from services.queue import attempts
    documents, missing = _candidates(user, data)
    results = [{"id": str(document_id), "code": None, "name": None, "outcome": "skipped",
                "reasonCode": "NOT_FOUND", "reason": REASONS["NOT_FOUND"]} for document_id in missing]
    queued = 0
    for document in documents:
        item = {"id": str(document["id"]), "code": document["document_code"], "name": document["original_filename"]}
        if document["status"] != "failed":
            results.append({**item, "outcome": "skipped", "reasonCode": "NOT_FAILED", "reason": REASONS["NOT_FAILED"]})
            continue
        try:
            job_id = _queue_one(user, document)
        except attempts.AttemptRejected as exc:
            code = exc.code if exc.code in REASONS else "ERROR"
            results.append({**item, "outcome": "skipped", "reasonCode": code, "reason": REASONS[code]})
        except Exception:
            logging.getLogger(__name__).exception("Bulk read-again could not queue document %s", item["id"])
            results.append({**item, "outcome": "error", "reasonCode": "ERROR", "reason": REASONS["ERROR"]})
        else:
            queued += 1
            results.append({**item, "outcome": "queued", "jobId": job_id, "status": "QUEUED"})
    if queued:
        _wake_dispatcher()
    return {"requested": len(results), "queued": queued, "skipped": len(results) - queued, "results": results}
