"""Read stored documents again with the current reading settings (local databases only).

    python services/admin/manage.py reread-documents --company "<exact name>" [--pack trading]
        [--status exception,extracted,failed] [--limit N] [--dry-run]

For a demo company seeded with AI reading off: turn it on in .env, then run this. Each document
of that company is either skipped, with the reason, or given a fresh extraction attempt through
the durable queue (attempts.schedule_reread: earlier attempts are kept, `extraction.reread_scheduled`
is audited, the document goes back to `queued`). With the in-process queue (the default) this
command then runs the extraction itself and waits for it, as the seed does. While it waits it
holds the jobs (they are not due until --timeout-minutes have passed) and dispatches them to its
own workers, so an API running on the same database with other reading settings does not take
them. It dispatches only those jobs (no company-wide recovery or dispatch). At the timeout or on
Ctrl+C it stops, closes the unfinished jobs as REREAD_TIMEOUT, marks those documents failed and
exits with os._exit(3) rather than waiting for a reading in progress. If the process is killed
outright, its held jobs fall due after the hold and any dispatcher may run them. With --no-wait
there is no hold.

Never touched: documents someone corrected, submitted, reviewed or approved, documents with an
open job, and documents past review. Persistence still refuses to replace reviewed corrections.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

from services.admin.demo_company import PACK_ROOT, PACKS, WAITING_STATUSES, refusal_reason

DEFAULT_STATUSES = ("exception", "extracted", "failed")
# Held jobs stay held this long after the timeout, so the timeout clean-up closes them before any
# other dispatcher could see them due.
HOLD_MARGIN_SECONDS = 600
# Statuses at or past review. ensure_unreviewed refuses these too; listed here so the skip says why.
PAST_REVIEW = ("corrected", "in_review", "validated", "standardised", "completed")
REJECTION_TEXT = {
    "AMENDMENT_REQUIRED": "reviewed or approved; needs an amendment",
    "REVIEW_HISTORY": "has corrections or review history",
    "JOB_ALREADY_OPEN": "already has an open job",
    "DOCUMENT_NOT_REREADABLE": "status changed before it could be queued",
}


class RereadError(RuntimeError):
    """The command cannot run; the message says why."""


# --- rules (no database) -----------------------------------------------------------------------------

def parse_statuses(text):
    """'exception,failed' -> ('exception', 'failed'); only statuses a re-read may replace."""
    chosen = tuple(dict.fromkeys(part.strip().lower() for part in (text or "").split(",") if part.strip()))
    if not chosen:
        raise RereadError("--status needs at least one of: " + ", ".join(DEFAULT_STATUSES) + ".")
    unknown = [status for status in chosen if status not in DEFAULT_STATUSES]
    if unknown:
        raise RereadError(f"--status may only list {', '.join(DEFAULT_STATUSES)}; not {', '.join(unknown)}.")
    return chosen


def skip_reason(facts, statuses):
    """Why this document must not be read again, or None when it may.

    facts: one row of document_facts(). The order puts the strongest reason first.
    """
    status = facts["status"]
    if status in PAST_REVIEW:
        return f"past review ({status})"
    if facts["field_corrections"] or facts["line_corrections"]:
        return "has reviewed corrections"
    if facts["review_submissions"] or facts["review_decisions"]:
        return "has review submissions or decisions"
    if facts["standardised"]:
        return "has an approved record"
    if facts["open_job"]:
        return "already has an open job"
    if status not in statuses:
        return f"status {status} not selected"
    return None


def plan(rows, statuses, limit=None):
    """[(row, action)] where action is 'reread' or 'skip: <reason>' (including 'over --limit')."""
    planned, chosen = [], 0
    for row in rows:
        reason = skip_reason(row, statuses)
        if reason:
            planned.append((row, "skip: " + reason))
        elif limit is not None and chosen >= limit:
            planned.append((row, "skip: over --limit"))
        else:
            chosen += 1
            planned.append((row, "reread"))
    return planned


def percentile(values, share):
    """Nearest-rank percentile (share 0-1) of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def summarize(rows):
    """Counts by status and reading time for the re-read documents (rows: status, processing_seconds)."""
    seconds = [float(row["processing_seconds"]) for row in rows
               if row["processing_seconds"] is not None and row["status"] not in WAITING_STATUSES]
    return {"by_status": dict(sorted(Counter(row["status"] for row in rows).items())),
            "timed": len(seconds),
            "average_seconds": round(sum(seconds) / len(seconds), 1) if seconds else None,
            "p95_seconds": round(percentile(seconds, 0.95), 1) if seconds else None}


def pack_company_name(key, root=PACK_ROOT):
    path = Path(root) / key / "pack.json"
    if not path.is_file():
        raise RereadError(f"{path} does not exist; seed the company first (seed-demo-company --pack {key}).")
    return json.loads(path.read_text(encoding="utf-8"))["company"]["name"]


# --- database ----------------------------------------------------------------------------------------

def find_company(db, name):
    tenant = db.execute("SELECT id, name FROM tenants WHERE name=%s", (name,)).fetchone()
    if not tenant:
        raise RereadError(f'No company is named exactly "{name}" in this database.')
    return tenant


def document_facts(db, tenant_id):
    """Every document of one company with what the skip rules need, in document-code order."""
    return db.execute("""SELECT d.id, d.document_code, d.original_filename, d.status,
            EXISTS(SELECT 1 FROM field_predictions fp WHERE fp.document_id=d.id AND fp.corrected_at IS NOT NULL)
                AS field_corrections,
            EXISTS(SELECT 1 FROM line_item_corrections c WHERE c.document_id=d.id) AS line_corrections,
            EXISTS(SELECT 1 FROM review_submissions s WHERE s.document_id=d.id AND s.tenant_id=d.tenant_id)
                AS review_submissions,
            EXISTS(SELECT 1 FROM review_decisions r WHERE r.document_id=d.id AND r.tenant_id=d.tenant_id)
                AS review_decisions,
            EXISTS(SELECT 1 FROM standardised_records sr WHERE sr.document_id=d.id) AS standardised,
            EXISTS(SELECT 1 FROM pending_jobs j WHERE j.document_id=d.id
                   AND j.status IN ('pending','dispatched','processing')) AS open_job
        FROM documents d WHERE d.tenant_id=%s ORDER BY d.document_code""", (tenant_id,)).fetchall()


def schedule(connect, tenant_id, planned, hold_seconds=0):
    """Queue every 'reread' row, one transaction each.

    Returns ([(row, action)] with the outcome, {document id: job id} of the queued ones).
    """
    from services.queue import attempts
    results, jobs = [], {}
    for row, action in planned:
        if action != "reread":
            results.append((row, action))
            continue
        try:
            with connect() as db:
                job = attempts.schedule_reread(db, str(row["id"]), str(tenant_id), hold_seconds)
            results.append((row, f"queued (job {job['id']})"))
            jobs[str(row["id"])] = str(job["id"])
        except attempts.AttemptRejected as error:
            results.append((row, "skip: " + REJECTION_TEXT.get(error.code, error.code)))
    return results, jobs


def dispatch_held(connect, tenant_id, jobs, capacity, send=None, failed=None):
    """Hand up to `capacity` of this command's held jobs to its own workers. Returns how many were sent.

    Held jobs are not due yet, so another API on the same database does not take them;
    attempts.reserve_held makes each one due and reserves it in the same transaction. A hand-over
    the transport could not confirm (publish() raises DISPATCH_UNCONFIRMED after closing that job
    and marking the document failed) is added to `failed` and the rest carry on.
    """
    from services.queue import attempts
    send = send or attempts.publish
    if capacity <= 0 or not jobs:
        return 0
    with connect() as db:
        waiting = db.execute("""SELECT id, document_id FROM pending_jobs WHERE id = ANY(%s::uuid[])
            AND status='pending' ORDER BY created_at LIMIT %s""", (list(jobs.values()), capacity)).fetchall()
    sent = 0
    for job in waiting:
        try:
            with connect() as db:
                delivery = attempts.reserve_held(db, str(job["document_id"]), str(tenant_id), str(job["id"]))
        except attempts.AttemptRejected:
            continue
        try:
            accepted = send(delivery)["dispatchAccepted"]
        except attempts.AttemptRejected:
            if failed is not None:
                failed.append(str(job["document_id"]))
            continue
        sent += bool(accepted)
    return sent


def wait_for(connect, document_ids, timeout_seconds, poll=3.0, tenant_id=None, jobs=None, deadline=None,
             failed=None):
    """Hand this command's own jobs to its workers until these documents leave the queue.

    Returns the seconds taken, or None at the deadline (time.monotonic(); default: timeout_seconds
    from now). The deadline is checked before every hand-over, so no reading starts after it.
    Only the selected jobs are dispatched: no lease recovery or company-wide dispatch runs here,
    and a finishing worker does not pull other waiting jobs (the inline stop flag, set before the
    first hand-over, turns that wake-up off). Nothing the plan skipped is read with these settings.
    """
    from services.queue.backend import queue_backend
    inline = None
    if queue_backend() == "inline":
        from services.queue import inline
        inline.stop_supervisor()  # sets the stop flag; submit() still works, completions stop dispatching
    started, last = time.monotonic(), None
    deadline = started + timeout_seconds if deadline is None else deadline
    try:
        while True:
            with connect() as db:
                waiting = db.execute("SELECT count(*) n FROM documents WHERE id = ANY(%s::uuid[]) AND status = ANY(%s)",
                                     (document_ids, list(WAITING_STATUSES))).fetchone()["n"]
            if waiting != last:
                print(f"  reading: {len(document_ids) - waiting}/{len(document_ids)} documents done", flush=True)
                last = waiting
            if not waiting:
                return round(time.monotonic() - started, 1)
            if time.monotonic() >= deadline:
                print(f"  {waiting} documents still waiting after {timeout_seconds // 60} minutes; stopping.")
                return None
            if inline is not None:
                dispatch_held(connect, tenant_id, jobs or {}, inline.capacity(), failed=failed)
            time.sleep(poll)
    finally:
        if inline is not None:
            inline.stop_supervisor()  # hand out nothing more


def abandon(connect, tenant_id, jobs):
    """Close every job this command did not see finish (timeout or Ctrl+C). Returns their document ids.

    Each job fails as REREAD_TIMEOUT and its document becomes `failed`, so a later
    `reread-documents --status failed` (or "Read again" in the app) retries it. A worker thread
    still running cannot store anything afterwards: its claim no longer matches.
    """
    from services.queue import attempts
    closed = []
    for document_id, job_id in jobs.items():
        try:
            with connect() as db:
                if attempts.abandon_reread(db, document_id, str(tenant_id), job_id):
                    closed.append(document_id)
        except attempts.AttemptRejected:
            continue
    return closed


def outcome(connect, document_ids, since):
    """Status and reading time of each document. A failed read leaves processing_seconds from the
    earlier run, so only a reading completed after `since` (when the re-reads were queued) is timed."""
    with connect() as db:
        return db.execute("""SELECT status, CASE WHEN processing_completed_at >= %s
                THEN processing_seconds END AS processing_seconds
            FROM documents WHERE id = ANY(%s::uuid[])""", (since, document_ids)).fetchall()


# --- command -----------------------------------------------------------------------------------------

def reread(company=None, pack=None, statuses=DEFAULT_STATUSES, limit=None, dry_run=False, wait=True,
           timeout_minutes=30, pack_root=PACK_ROOT, connect=None):
    reason = refusal_reason()
    if reason:
        raise RereadError("Refusing to re-read: " + reason)
    if not company and not pack:
        raise RereadError("Give --company (exact name) or --pack.")
    if pack:
        named = pack_company_name(pack, pack_root)
        if company and company != named:
            raise RereadError(f'--company "{company}" is not the {pack} pack\'s company ("{named}").')
        company = named
    if limit is not None and limit < 1:
        raise RereadError("--limit must be at least 1.")
    if connect is None:
        from database.connection import connect
    with connect() as db:
        tenant = find_company(db, company)
        planned = plan(document_facts(db, tenant["id"]), statuses, limit)
        since = db.execute("SELECT clock_timestamp() AS instant").fetchone()["instant"]
    print(f"{tenant['name']}: {len(planned)} documents; re-reading status {', '.join(statuses)}"
          f"{' (dry run, nothing changes)' if dry_run else ''}; AI extraction: {os.getenv('AI_EXTRACTION') or 'off'}.")
    if not dry_run:
        # Keep this process's dispatch and lease recovery to this one company.
        os.environ["QUEUE_TENANT_ID"] = str(tenant["id"])
    # Driving the in-process queue here, hold the jobs for this process, so a running API with other
    # reading settings cannot take them. The deadline starts before the first job is queued and the
    # hold lasts HOLD_MARGIN_SECONDS longer, so the timeout clean-up closes every job before any hold
    # ends. Without the wait (or with RQ) there is no hold and any dispatcher may run them.
    from services.queue.backend import queue_backend
    deadline = time.monotonic() + timeout_minutes * 60
    hold_seconds = timeout_minutes * 60 + HOLD_MARGIN_SECONDS if wait and queue_backend() == "inline" else 0
    results, jobs = (planned, {}) if dry_run else schedule(connect, tenant["id"], planned, hold_seconds)
    for row, action in results:
        print(f"  {row['document_code']}  {row['original_filename']}  "
              f"{'would re-read' if action == 'reread' else action}")
    queued = [str(row["id"]) for row, action in results if action.startswith("queued")]
    skipped = Counter(action[len("skip: "):] for _row, action in results if action.startswith("skip: "))
    report = {"company": tenant["name"], "documents": len(results), "dry_run": dry_run,
              "selected": sum(action == "reread" for _row, action in planned), "queued": len(queued),
              "skipped": dict(sorted(skipped.items()))}
    if queued and wait:
        names = {str(row["id"]): f"{row['document_code']}  {row['original_filename']}" for row, _a in results}
        failed = []
        try:
            report["seconds"] = wait_for(connect, queued, timeout_minutes * 60, tenant_id=str(tenant["id"]),
                                         jobs=jobs, deadline=deadline, failed=failed)
        except KeyboardInterrupt:
            print("  interrupted; stopping.")
            report["seconds"] = None
        except Exception:
            abandon(connect, tenant["id"], jobs)  # an unexpected error still closes every unfinished job
            raise
        report["dispatch_failed"] = [names[document_id] for document_id in failed]
        if report["seconds"] is None:
            report["abandoned"] = [names[document_id] for document_id in abandon(connect, tenant["id"], jobs)]
        report["result"] = summarize(outcome(connect, queued, since))
    return report


def print_report(report):
    count, verb = ((report["selected"], "would be re-read") if report["dry_run"]
                   else (report["queued"], "queued for re-reading"))
    print(f"\n{report['company']}: {report['documents']} documents, {count} {verb}, "
          f"{sum(report['skipped'].values())} skipped.")
    for reason, number in report["skipped"].items():
        print(f"  {number:4}  {reason}")
    result = report.get("result")
    if result:
        print("Re-read documents by status now:", json.dumps(result["by_status"]))
        if result["timed"]:
            print(f"Reading time over the {result['timed']} documents read in this run: "
                  f"average {result['average_seconds']} s, p95 {result['p95_seconds']} s.")
    if report.get("dispatch_failed"):
        print(f"Not handed to a worker: {len(report['dispatch_failed'])} documents (DISPATCH_UNCONFIRMED); "
              "they are marked failed; read them again with --status failed.")
        for name in report["dispatch_failed"]:
            print("  " + name)
    if report.get("abandoned"):
        print(f"Not finished: {len(report['abandoned'])} documents. Their jobs are closed (REREAD_TIMEOUT) and "
              "the documents marked failed; read them again with --status failed.")
        for name in report["abandoned"]:
            print("  " + name)


def add_arguments(parser):
    parser.add_argument("--company", help="exact company name")
    parser.add_argument("--pack", choices=PACKS, help="the demo company seeded from this pack")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="comma-separated statuses to re-read (default: exception,extracted,failed)")
    parser.add_argument("--limit", type=int, help="re-read at most this many documents")
    parser.add_argument("--dry-run", action="store_true", help="list what would happen; change nothing")
    parser.add_argument("--no-wait", action="store_true", help="queue the jobs and exit without reading them here")
    parser.add_argument("--timeout-minutes", type=int, default=30)
    parser.add_argument("--pack-root", default=str(PACK_ROOT), help="folder holding <pack>/pack.json")


def run(args):
    try:
        report = reread(args.company, args.pack, parse_statuses(args.status), args.limit, args.dry_run,
                        not args.no_wait, args.timeout_minutes, args.pack_root)
    except RereadError as error:
        print(str(error), file=sys.stderr)
        return 2
    print_report(report)
    if report.get("abandoned"):
        # A worker thread may still be inside OCR or an AI request, and Python joins pool threads at
        # shutdown, so a normal exit would wait for it. Every job is already closed in the database
        # (that thread can no longer store anything), so leave at once.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(3)
    return 0
