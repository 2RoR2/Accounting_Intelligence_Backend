"""Rename stored document names that still say NONUMBER (`manage.py repair-nonumber`).

    python services/admin/manage.py repair-nonumber                      # dry run: prints old -> new for each document
    python services/admin/manage.py repair-nonumber --apply              # writes the new names

Until the 24 Nov naming fix, a bill whose printed number was read was still named `..._NONUMBER.jpg`, because the
naming step did not look at the bill's own number field. The rule is right now (services/extraction/persistence/
persistence.py: `_usable_document_identifier` and `standard_filename`); this command applies it again to the names
already stored, from the document's saved record or its current reading.

- Only `documents.original_filename` changes, as in planm-tidy: the stored file keeps its key and downloads use the
  new name. Nothing else is written. A name whose number cannot be found is left as it is and listed as skipped.
- A name already used by another document of the company gets the document code added (`__DOC-0001`), as at upload.
- Without --apply the whole run is one read-only transaction that is rolled back.
- A database that is not on this machine (hosted UAT) needs both --allow-remote and REPAIR_NONUMBER_REMOTE_HOST set to
  that database's host name, so it cannot be reached by accident. The connection string is never printed.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

from services.admin.demo_company import SeedError, _is_loopback

MARKER = "NONUMBER"


def add_arguments(parser):
    parser.add_argument("--apply", action="store_true", help="write the new names (default: print them only)")
    parser.add_argument("--tenant", help="company name (default: every company)")
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit a database that is not on this machine (also needs REPAIR_NONUMBER_REMOTE_HOST)")


def require_target(allow_remote: bool, environ=None):
    """The connect function for the database in DATABASE_URL, refusing a remote one unless explicitly confirmed."""
    environ = os.environ if environ is None else environ
    from database.connection import connect

    try:
        host = urlsplit(environ.get("DATABASE_URL", "")).hostname or ""
    except ValueError:
        host = ""
    if not host:
        raise SeedError("DATABASE_URL is not set to a PostgreSQL URL.")
    if not _is_loopback(host):
        if not allow_remote or (environ.get("REPAIR_NONUMBER_REMOTE_HOST") or "").strip().lower() != host.lower():
            raise SeedError("This database is not on this machine. Pass --allow-remote and set REPAIR_NONUMBER_REMOTE_HOST "
                            "to its host name to use it.")
    return connect


def _fields(db, document_id) -> list[dict]:
    return [dict(r) for r in db.execute("""SELECT f.field_key,f.current_value,f.normalized_value FROM field_predictions f
        JOIN raw_extractions e ON e.id=f.extraction_id AND e.is_current WHERE f.document_id=%s""", (document_id,)).fetchall()]


def corrected_name(db, document) -> tuple[str | None, str]:
    """(new name, why not) for one document: the name the current rule gives it, or None and the reason it has none."""
    from services.extraction.persistence.persistence import _usable_document_identifier, standard_filename
    fields = _fields(db, document["id"])
    identifier = _usable_document_identifier(fields, document["kind"])
    record = db.execute("""SELECT invoice_number,reference_number,invoice_date,supplier_name,buyer_name FROM standardised_records
        WHERE document_id=%s AND tenant_id=%s ORDER BY is_current DESC, revision_number DESC LIMIT 1""",
                        (document["id"], document["tenant_id"])).fetchone()
    if record:
        identifier = record["invoice_number"] or record["reference_number"] or identifier
        # The record's own values win, exactly as the document list reads them.
        extra = [{"field_key": "supplier", "current_value": record["supplier_name"]},
                 {"field_key": "customer", "current_value": record["buyer_name"]}]
        if record["invoice_date"]:
            extra.append({"field_key": "date", "current_value": str(record["invoice_date"])})
        fields = [row for row in extra if row["current_value"]] + fields
    if not identifier or not str(identifier).strip():
        return None, "no document number was read"
    name = standard_filename(db, document, fields, str(identifier).strip())
    if MARKER in name:
        return None, "the number read is not usable"
    return name, ""


def repair(db, tenant_id, *, apply: bool) -> list[dict]:
    """Rename the stored names of one company that contain NONUMBER; one result row per document."""
    results, assigned = [], set()
    documents = db.execute("""SELECT * FROM documents WHERE tenant_id=%s AND original_filename LIKE %s
        ORDER BY uploaded_at, id""", (tenant_id, f"%{MARKER}%")).fetchall()
    for document in documents:
        old = document["original_filename"]
        new, reason = corrected_name(db, document)
        if new is None:
            results.append({"code": document["document_code"], "old": old, "new": None, "skipped": reason})
            continue
        # Taken by another document already, or by one this run just renamed (a dry run writes nothing, so it tracks them).
        if new in assigned or db.execute("SELECT 1 FROM documents WHERE tenant_id=%s AND original_filename=%s AND id<>%s",
                                         (tenant_id, new, document["id"])).fetchone():
            stem, _, extension = new.rpartition(".")
            new = f"{stem}__{document['document_code']}.{extension}" if stem else f"{new}__{document['document_code']}"
        assigned.add(new)
        if new == old:
            results.append({"code": document["document_code"], "old": old, "new": None, "skipped": "name is already right"})
            continue
        if apply:
            db.execute("UPDATE documents SET original_filename=%s, updated_at=now() WHERE id=%s AND tenant_id=%s",
                       (new, document["id"], tenant_id))
        results.append({"code": document["document_code"], "old": old, "new": new, "skipped": None})
    return results


def run(args, environ=None) -> int:
    try:
        connect = require_target(args.allow_remote, environ)
    except SeedError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    renamed = skipped = 0
    with connect() as db:
        if not args.apply:
            db.execute("SET TRANSACTION READ ONLY")
        tenants = db.execute("SELECT id,name FROM tenants WHERE (%s::text IS NULL OR name=%s) ORDER BY name",
                             (args.tenant, args.tenant)).fetchall()
        for tenant in tenants:
            results = repair(db, tenant["id"], apply=args.apply)
            if not results:
                continue
            print(f"{tenant['name']}:")
            for row in results:
                if row["new"]:
                    renamed += 1
                    print(f"  {row['code']}: {row['old']} → {row['new']}")
                else:
                    skipped += 1
                    print(f"  {row['code']}: {row['old']} (left as it is: {row['skipped']})")
        if not args.apply:
            db.rollback()
    verb = "Renamed" if args.apply else "Would rename"
    print(f"{verb} {renamed} document(s); {skipped} left as they are.")
    if not args.apply:
        print("Dry run: nothing written. Add --apply to write.")
    return 0
