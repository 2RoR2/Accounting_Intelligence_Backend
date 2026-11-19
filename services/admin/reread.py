"""Read stored documents again with the current reading settings (local databases only).

    python services/admin/manage.py reread-documents --company "<exact name>" [--pack trading]
        [--status exception,extracted,failed] [--limit N] [--dry-run]

For a demo company seeded with AI reading off: turn it on in .env, then run this. Each document
of that company is either skipped, with the reason, or given a fresh extraction attempt through
the durable queue (attempts.schedule_reread: earlier attempts are kept, `extraction.reread_scheduled`
is audited, the document goes back to `queued`). With the in-process queue (the default) this
command then runs the extraction itself and waits for it, as the seed does. Stop any API running
on the same database first (or start it with the same reading settings): its queue would take
some of the jobs and read them with its own settings. The SQL claim still makes sure each job
runs once.

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


def schedule(connect, tenant_id, planned):
    """Queue every 'reread' row, one transaction each. Returns [(row, action)] with the outcome."""
    from services.queue import attempts
    results = []
    for row, action in planned:
        if action != "reread":
            results.append((row, action))
            continue
        try:
            with connect() as db:
                job = attempts.schedule_reread(db, str(row["id"]), str(tenant_id))
            results.append((row, f"queued (job {job['id']})"))
        except attempts.AttemptRejected as error:
            results.append((row, "skip: " + REJECTION_TEXT.get(error.code, error.code)))
    return results


def wait_for(connect, document_ids, timeout_seconds, poll=3.0):
    """Drive the in-process queue (if that is the transport) until these documents leave the queue."""
    from services.queue.backend import queue_backend
    inline = None
    if queue_backend() == "inline":
        from services.queue import inline
    started, last = time.monotonic(), None
    try:
        while True:
            if inline is not None:
                inline.run_cycle()
            with connect() as db:
                waiting = db.execute("SELECT count(*) n FROM documents WHERE id = ANY(%s::uuid[]) AND status = ANY(%s)",
                                     (document_ids, list(WAITING_STATUSES))).fetchone()["n"]
            if waiting != last:
                print(f"  reading: {len(document_ids) - waiting}/{len(document_ids)} documents done", flush=True)
                last = waiting
            if not waiting:
                return round(time.monotonic() - started, 1)
            if time.monotonic() - started > timeout_seconds:
                print(f"  {waiting} documents still waiting after {timeout_seconds // 60} minutes; "
                      "their jobs stay queued and the backend picks them up when it runs.")
                return None
            time.sleep(poll)
    finally:
        if inline is not None:
            # Stop handing out new work; a document still running is recovered by its lease.
            inline.stop_supervisor()


def outcome(connect, document_ids):
    with connect() as db:
        return db.execute("SELECT status, processing_seconds FROM documents WHERE id = ANY(%s::uuid[])",
                          (document_ids,)).fetchall()


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
    print(f"{tenant['name']}: {len(planned)} documents; re-reading status {', '.join(statuses)}"
          f"{' (dry run, nothing changes)' if dry_run else ''}; AI extraction: {os.getenv('AI_EXTRACTION') or 'off'}.")
    if not dry_run:
        # Keep this process's dispatch and lease recovery to this one company.
        os.environ["QUEUE_TENANT_ID"] = str(tenant["id"])
    results = planned if dry_run else schedule(connect, tenant["id"], planned)
    for row, action in results:
        print(f"  {row['document_code']}  {row['original_filename']}  "
              f"{'would re-read' if action == 'reread' else action}")
    queued = [str(row["id"]) for row, action in results if action.startswith("queued")]
    skipped = Counter(action[len("skip: "):] for _row, action in results if action.startswith("skip: "))
    report = {"company": tenant["name"], "documents": len(results), "dry_run": dry_run,
              "selected": sum(action == "reread" for _row, action in planned), "queued": len(queued),
              "skipped": dict(sorted(skipped.items()))}
    if queued and wait:
        report["seconds"] = wait_for(connect, queued, timeout_minutes * 60)
        report["result"] = summarize(outcome(connect, queued))
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
            print(f"Reading time over {result['timed']} documents: average {result['average_seconds']} s, "
                  f"p95 {result['p95_seconds']} s.")


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
    return 0
