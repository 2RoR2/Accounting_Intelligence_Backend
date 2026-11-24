"""Workload per document (SC2 evidence): what reviewers actually did, read from records the system already keeps.

Read-only, one company at a time. Nothing here is estimated:
- Touches per document = human field edits (field_corrections, the trigger-written history) + line-item changes
  (line_item_corrections and audited added lines) + type choices + decision actions (audit_logs).
  Field edits have no audit_logs row of their own; field_corrections is their append-only record.
- Corrected fields = fields whose corrected_by is set, by field key.
- First-pass result = the document's status just before the first human touch (or now, if nobody touched it):
  'exception' or 'extracted'. Documents still processing, failed or otherwise without a result are excluded and counted.
- Ready without any correction = approved (validated / saved / completed) with no field edit, line-item change or type choice.
- Review time = the optional `review.session` client event (active seconds only). When no event exists it is reported as
  not measured, never inferred from timestamps.

Touches have no manual baseline, so they are never turned into a "% less manual entry". SC2's reduction comes only from
the counterbalanced effort study (services/routes/effort/routes.py), whose result is reported next to these figures.
"""
from __future__ import annotations

import json
from datetime import date as Date
from statistics import median

from pydantic import BaseModel, Field

from main import HTTPException, Request, connect, require_workspace_user
from services.routes.effort import routes as effort

VERSION = "workload/1"
ROLES = {"local_admin", "accountant"}
DOCUMENT_LIMIT = 500
REVIEW_SESSION = "review.session"
TYPE_CHOICE = "document.kind_chosen"
LINE_ADDED = "document.line_item_added"
# One row per decision a person takes on a document. review.bulk_* rows wrap the document.* rows below, so they are not counted.
DECISIONS = {
    "document.submitted": "submit",
    "document.approved": "approve",
    "document.returned": "return",
    "document.rejected": "return",
    "validation.review_accepted": "finding accepted",
    "ai_assist.decision": "AI suggestion decided",
    "currency.decision": "currency decided",
}
READY_STATUSES = {"validated", "standardised", "completed"}
FIRST_PASS = {"extracted": False, "exception": True}
DEFINITIONS = {
    "touches": "Human field edits + line-item changes + type choices + decisions (submit, approve, return, finding accepted, "
               "AI suggestion or currency decision) on the document.",
    "correctedFields": "Distinct fields with a human correction (corrected_by set) on the document.",
    "firstPass": "Status just before the first human touch: exception or extracted. Documents without one (still processing, "
                 "failed) are excluded and counted.",
    "readyWithoutCorrection": "Approved documents with no field edit, line-item change or type choice.",
    "reviewTime": "Active seconds from the optional review-opened/closed event. Not measured when no event was recorded.",
    "sc2": "Touches have no manual baseline, so no reduction is computed from them. SC2's reduction comes only from the "
           "effort study, valid with at least 2 paired participants covering both orders.",
}


class ReviewSessionInput(BaseModel):
    documentId: str = Field(min_length=1, max_length=64)
    activeSeconds: float = Field(ge=0, le=7200)
    idleSeconds: float = Field(default=0, ge=0, le=7200)
    openedAt: str = Field(min_length=1, max_length=40)
    closedAt: str = Field(min_length=1, max_length=40)


def _details(text):
    if isinstance(text, dict):
        return text
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "median": None, "min": None, "max": None}
    return {"n": len(values), "median": round(float(median(values)), 1), "min": min(values), "max": max(values)}


def _rate(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _status_at(history, before, current):
    """Status just before `before` (a timestamp, or None for now). Rows in one transaction share a timestamp, so the
    chain inside a group (e.g. processing -> extracted -> exception) is followed to its end."""
    if before is None:
        return current
    earlier = [row for row in history if row["created_at"] < before]
    if not earlier:
        if not history:
            return current
        first = [row for row in history if row["created_at"] == history[0]["created_at"]]
        news = {row["new_status"] for row in first}
        starts = [row for row in first if row["old_status"] not in news] or first
        return starts[0]["old_status"]
    last = [row for row in earlier if row["created_at"] == earlier[-1]["created_at"]]
    olds = {row["old_status"] for row in last}
    ends = [row for row in last if row["new_status"] not in olds] or last
    return ends[0]["new_status"]


def collect(db, tenant_id, start=None, end=None):
    """Per-document workload for one company. `start`/`end` filter on the upload date in the company's time zone."""
    zone = db.execute("SELECT timezone FROM workspace_settings WHERE tenant_id=%s LIMIT 1", (tenant_id,)).fetchone()
    zone = (zone or {}).get("timezone") or "Asia/Kuala_Lumpur"
    where, params = ["d.tenant_id=%s", "d.excluded_at IS NULL"], [zone, tenant_id]  # set-aside uploads are not workload
    if start:
        where.append("(d.uploaded_at AT TIME ZONE %s)::date >= %s")
        params += [zone, start]
    if end:
        where.append("(d.uploaded_at AT TIME ZONE %s)::date <= %s")
        params += [zone, end]
    rows = db.execute(f"""SELECT d.id, d.document_code, d.kind, d.status, d.uploaded_at,
            to_char(d.uploaded_at AT TIME ZONE %s, 'YYYY-MM') AS period
        FROM documents d WHERE {' AND '.join(where)} ORDER BY d.uploaded_at, d.id""", params).fetchall()
    docs = {str(r["id"]): {"id": str(r["id"]), "code": r["document_code"], "kind": r["kind"], "status": r["status"],
                           "period": r["period"], "uploadedAt": r["uploaded_at"].isoformat(),
                           "fieldEdits": 0, "lineItemChanges": 0, "typeChoices": 0, "decisions": 0,
                           "decisionsByType": {}, "correctedFields": set(), "touchTimes": [], "reviewSeconds": None,
                           "history": []} for r in rows}
    if not docs:
        return zone, []
    ids = list(docs)

    for r in db.execute("""SELECT p.document_id, p.field_key, c.corrected_at FROM field_corrections c
            JOIN field_predictions p ON p.id=c.field_prediction_id
            JOIN documents d ON d.id=p.document_id AND d.tenant_id=%s
            WHERE c.corrected_by IS NOT NULL AND p.document_id = ANY(%s::uuid[])""", (tenant_id, ids)).fetchall():
        doc = docs[str(r["document_id"])]
        doc["fieldEdits"] += 1
        doc["touchTimes"].append(r["corrected_at"])
    for r in db.execute("""SELECT p.document_id, p.field_key FROM field_predictions p
            JOIN documents d ON d.id=p.document_id AND d.tenant_id=%s
            WHERE p.corrected_by IS NOT NULL AND p.document_id = ANY(%s::uuid[])""", (tenant_id, ids)).fetchall():
        docs[str(r["document_id"])]["correctedFields"].add(r["field_key"])
    for r in db.execute("""SELECT c.document_id, c.corrected_at FROM line_item_corrections c
            JOIN documents d ON d.id=c.document_id AND d.tenant_id=%s
            WHERE c.document_id = ANY(%s::uuid[])""", (tenant_id, ids)).fetchall():
        doc = docs[str(r["document_id"])]
        doc["lineItemChanges"] += 1
        doc["touchTimes"].append(r["corrected_at"])

    actions = [TYPE_CHOICE, LINE_ADDED, REVIEW_SESSION, *DECISIONS]
    for r in db.execute("""SELECT a.resource, a.action, a.details, a.created_at FROM audit_logs a
            WHERE a.tenant_id=%s AND a.action = ANY(%s) AND a.resource = ANY(%s)
            ORDER BY a.created_at""", (tenant_id, actions, ids)).fetchall():
        doc = docs[r["resource"]]
        action = r["action"]
        if action == REVIEW_SESSION:
            seconds = _details(r["details"]).get("activeSeconds")
            if isinstance(seconds, (int, float)) and seconds >= 0:
                doc["reviewSeconds"] = round((doc["reviewSeconds"] or 0) + seconds, 1)
            continue
        doc["touchTimes"].append(r["created_at"])
        if action == TYPE_CHOICE:
            doc["typeChoices"] += 1
        elif action == LINE_ADDED:
            doc["lineItemChanges"] += 1
        else:
            doc["decisions"] += 1
            label = DECISIONS[action]
            doc["decisionsByType"][label] = doc["decisionsByType"].get(label, 0) + 1

    for r in db.execute("""SELECT document_id, old_status, new_status, created_at FROM document_status_history
            WHERE tenant_id=%s AND document_id = ANY(%s::uuid[]) ORDER BY created_at""", (tenant_id, ids)).fetchall():
        docs[str(r["document_id"])]["history"].append(r)

    out = []
    for doc in docs.values():
        first_touch = min(doc["touchTimes"]) if doc["touchTimes"] else None
        verdict = _status_at(doc.pop("history"), first_touch, doc["status"])
        corrections = doc["fieldEdits"] + doc["lineItemChanges"]
        doc.pop("touchTimes")
        doc["correctedFields"] = sorted(doc["correctedFields"])
        doc["touches"] = corrections + doc["typeChoices"] + doc["decisions"]
        doc["firstPassStatus"] = verdict
        doc["firstPassException"] = FIRST_PASS.get(verdict)
        doc["ready"] = doc["status"] in READY_STATUSES
        doc["readyWithoutCorrection"] = doc["ready"] and corrections == 0 and doc["typeChoices"] == 0
        out.append(doc)
    return zone, out


def metrics(docs):
    checked = [d for d in docs if d["firstPassException"] is not None]
    exceptions = sum(1 for d in checked if d["firstPassException"])
    ready = [d for d in docs if d["ready"]]
    clean = sum(1 for d in ready if d["readyWithoutCorrection"])
    timed = [d["reviewSeconds"] for d in docs if d["reviewSeconds"] is not None]
    return {
        "documents": len(docs),
        "touches": stats([d["touches"] for d in docs]),
        "touchTotals": {"fieldEdits": sum(d["fieldEdits"] for d in docs), "lineItemChanges": sum(d["lineItemChanges"] for d in docs),
                        "typeChoices": sum(d["typeChoices"] for d in docs), "decisions": sum(d["decisions"] for d in docs)},
        "correctedFields": stats([len(d["correctedFields"]) for d in docs]),
        "firstPass": {"checked": len(checked), "exceptions": exceptions, "rate": _rate(exceptions, len(checked)),
                      "excluded": len(docs) - len(checked)},
        "readyWithoutCorrection": {"ready": len(ready), "withoutCorrection": clean, "rate": _rate(clean, len(ready)),
                                   "notReady": len(docs) - len(ready)},
        "reviewTime": {**stats(timed), "measured": bool(timed), "documentsNotTimed": len(docs) - len(timed)},
    }


def _by_mode(sessions):
    out = {}
    for mode in ("manual", "ai_assisted"):
        rows = [s for s in sessions if s.get("mode") == mode]
        out[mode] = {"sessions": len(rows), "activeSeconds": stats([s.get("activeSeconds") for s in rows]),
                     "fieldsTyped": stats([s.get("fieldsTyped") for s in rows])}
    return out


def effort_study(user):
    """The existing effort study's own result, unchanged, plus measured per-task medians by mode."""
    sessions = effort._sessions(user)
    result = effort.summarise(sessions)
    return {key: result[key] for key in ("version", "status", "valid", "reduction", "participants", "sessions", "rule")} | {
        "pairedParticipants": len(result["paired"]), "byMode": _by_mode(sessions)}


def build(db, tenant_id, start=None, end=None):
    zone, docs = collect(db, tenant_id, start, end)
    periods = {}
    for doc in docs:
        periods.setdefault(doc["period"], []).append(doc)
    fields = {}
    for doc in docs:
        for key in doc["correctedFields"]:
            fields[key] = fields.get(key, 0) + 1
    return {
        "version": VERSION, "timezone": zone,
        "filters": {"fromDate": str(start) if start else None, "toDate": str(end) if end else None},
        "definitions": DEFINITIONS,
        "overall": metrics(docs),
        "periods": [{"period": key, **metrics(rows)} for key, rows in sorted(periods.items())],
        "correctedFieldKeys": [{"fieldKey": key, "documents": count}
                               for key, count in sorted(fields.items(), key=lambda item: (-item[1], item[0]))],
        "documents": docs[:DOCUMENT_LIMIT], "documentsTotal": len(docs), "documentsShown": min(len(docs), DOCUMENT_LIMIT),
    }


def workload_report(request: Request, fromDate: Date | None = None, toDate: Date | None = None):
    user = require_workspace_user(request, ROLES)
    if fromDate and toDate and fromDate > toDate:
        raise HTTPException(422, "The start date must be on or before the end date.")
    with connect() as db:
        report = build(db, user["tenant_id"], fromDate, toDate)
    study = effort_study(user)
    report["effortStudy"] = study
    report["sc2"] = {"status": study["status"], "reduction": study["reduction"], "source": "effort-study",
                     "note": DEFINITIONS["sc2"]}
    return report


def record_review_session(data: ReviewSessionInput, request: Request):
    """Optional client event: a review was opened and closed, with active seconds. Anonymous: no user id or name is
    stored, only the company, role and document, so the report can time reviews without tracking people."""
    user = require_workspace_user(request, ROLES)
    with connect() as db:
        found = db.execute("SELECT id FROM documents WHERE id::text=%s AND tenant_id=%s",
                           (data.documentId, user["tenant_id"])).fetchone()
        if not found:
            raise HTTPException(404, "Document not found.")
        details = {"activeSeconds": round(data.activeSeconds, 1), "idleSeconds": round(data.idleSeconds, 1),
                   "openedAt": data.openedAt, "closedAt": data.closedAt}
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,NULL,'Reviewer',%s,%s,%s,%s)""", (user["tenant_id"], user["role"], REVIEW_SESSION,
                                                         str(found["id"]), json.dumps(details)))
    return {"recorded": True, **details}


def register(router):
    router.add_api_route("/api/reports/workload", workload_report, methods=["GET"], tags=["Reports"])
    router.add_api_route("/api/workload/review-sessions", record_review_session, methods=["POST"], status_code=201,
                         tags=["Reports"])
