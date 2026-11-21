"""Read failed documents again, in bulk, with the current reading settings (plan v3, G7).

Documents the old engine (or a provider outage) left FAILED are queued for a fresh
extraction attempt through the re-read path of `manage.py reread-documents`
(`attempts.schedule_reread`): earlier attempts are kept, the document goes
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
    "REVIEW_HISTORY": "Has corrections or review history; it needs an amendment instead.",
    "DOCUMENT_NOT_REREADABLE": "Its status changed before it could be queued.",
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


_USED_SQL = """(SELECT count(*) FROM audit_logs a
    WHERE a.tenant_id=d.tenant_id AND a.resource=d.id::text AND a.action=%s)"""

# What attempts.schedule_reread (with ensure_unreviewed) refuses for a failed document, every
# time: an approved record, any review submission or decision, any corrected field or line,
# and an open job. Kept in step with services/queue/attempts.py; _queue_one still runs the
# real checks under the document lock, so this only decides what allFailed selects.
_NOT_ELIGIBLE_SQL = """(
    EXISTS (SELECT 1 FROM standardised_records sr WHERE sr.document_id=d.id)
    OR EXISTS (SELECT 1 FROM review_submissions s WHERE s.document_id=d.id AND s.tenant_id=d.tenant_id)
    OR EXISTS (SELECT 1 FROM review_decisions r WHERE r.document_id=d.id AND r.tenant_id=d.tenant_id)
    OR EXISTS (SELECT 1 FROM field_predictions fp WHERE fp.document_id=d.id AND fp.corrected_at IS NOT NULL)
    OR EXISTS (SELECT 1 FROM line_item_corrections c WHERE c.document_id=d.id)
    OR EXISTS (SELECT 1 FROM pending_jobs j WHERE j.document_id=d.id AND j.status IN ('pending','dispatched','processing')))"""


def _candidates(user, data):
    """(documents to consider in a stable order, requested ids that are not visible,
    failed documents allFailed left out because their read-again budget is used,
    failed documents allFailed left out because a re-read is always refused for them)."""
    tenant_id = user["tenant_id"]
    visibility, extra = _visibility(user)
    with connect() as db:
        if data.allFailed:
            # Budget and eligibility are applied before the limit: otherwise documents that are
            # always refused stay the oldest failures and newer ones are never reached.
            failed = f"FROM documents d WHERE d.tenant_id=%s AND d.status='failed'{visibility}"
            rows = db.execute(f"""SELECT d.id, d.document_code, d.original_filename, d.status {failed}
                AND {_USED_SQL} < %s AND NOT {_NOT_ELIGIBLE_SQL}
                ORDER BY d.uploaded_at, d.id LIMIT %s""",
                [tenant_id, *extra, AUDIT_ACTION, READ_AGAIN_LIMIT, MAX_DOCUMENTS]).fetchall()
            counts = db.execute(f"""SELECT
                    count(*) FILTER (WHERE {_USED_SQL} >= %s) AS spent,
                    count(*) FILTER (WHERE {_USED_SQL} < %s AND {_NOT_ELIGIBLE_SQL}) AS blocked
                {failed}""", [AUDIT_ACTION, READ_AGAIN_LIMIT, AUDIT_ACTION, READ_AGAIN_LIMIT, tenant_id, *extra]).fetchone()
            return list(rows), [], counts["spent"], counts["blocked"]
        valid, invalid = _valid_ids(data.documentIds)
        rows = db.execute(f"""SELECT d.id, d.document_code, d.original_filename, d.status FROM documents d
            WHERE d.tenant_id=%s AND d.id=ANY(%s::uuid[]){visibility}""", [tenant_id, valid, *extra]).fetchall() if valid else []
    by_id = {str(row["id"]): row for row in rows}
    ordered = [by_id[document_id] for document_id in valid if document_id in by_id]
    missing = invalid + [document_id for document_id in valid if document_id not in by_id]
    return ordered, missing, 0, 0


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
        # The same re-read path as `manage.py reread-documents` (#14): keeps earlier attempts,
        # refuses review history and open jobs, audits extraction.reread_scheduled.
        job = attempts.schedule_reread(db, document_id, tenant_id)
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
    documents, missing, budget_used, not_eligible = _candidates(user, data)
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
    # Failed documents allFailed left out: budgetUsed were read again READ_AGAIN_LIMIT times;
    # notEligible have review history, corrections, an approved record or an open job.
    return {"requested": len(results), "queued": queued, "skipped": len(results) - queued,
            "budgetUsed": budget_used, "notEligible": not_eligible, "results": results}
