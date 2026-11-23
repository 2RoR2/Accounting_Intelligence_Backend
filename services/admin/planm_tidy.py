"""Local only: bring an existing demo database in line with Plan M (`manage.py planm-tidy`, dry run unless --apply).

1. Contact codes (M4): every supplier and customer without a code gets one suggested from its name, unique per company.
2. Own sales documents captured by mistake (B1/M2): a captured document whose seller is the company itself, or that was
   filed as a sale (accounts receivable), is the company's own sales document. It belongs under Sales, typed. Its posted
   sales journal is reversed (append-only, in an open period), its open extraction job and benchmark entry are removed,
   and the document is marked excluded so it leaves the Inbox and review lanes. Its history stays for audit.
3. Names (M4): captured documents get {YYYY-MM-DD}_{CONTACT}_{TYPE}_{NUMBER}.{ext} from their record or extracted fields
   (database name only; the stored file keeps its key, downloads use the new name).

Same guard as seed-demo-company: APP_ENV=development and a database on this machine only.
"""
from __future__ import annotations

import os
import sys

from database.connection import connect


def add_arguments(parser):
    parser.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    parser.add_argument("--tenant", help="company name (default: every company)")


def _fields(db, document_id) -> list[dict]:
    return [dict(r) for r in db.execute("""SELECT f.field_key,f.current_value,f.normalized_value FROM field_predictions f
        JOIN raw_extractions e ON e.id=f.extraction_id AND e.is_current WHERE f.document_id=%s""", (document_id,)).fetchall()]


def tidy(db, tenant_id, *, apply: bool) -> dict:
    from services import naming
    from services.company_identity import company_identity, direction
    from services.extraction.persistence.persistence import _field_value, _usable_document_identifier, standard_filename
    report = {"contact_codes": 0, "own_sales_documents": 0, "reversed_journals": 0, "jobs_removed": 0,
              "benchmark_removed": 0, "renamed": 0, "skipped_closed_period": 0}
    for kind, table in (("supplier", "vendors"), ("customer", "customers")):
        for row in db.execute(f"""SELECT id FROM {table} WHERE tenant_id=%s AND (contact_code IS NULL OR btrim(contact_code)='')
                ORDER BY created_at, id""", (tenant_id,)).fetchall():
            naming.ensure_contact_code(db, tenant_id, kind, row["id"])
            report["contact_codes"] += 1

    identity = company_identity(db, tenant_id)
    admin = db.execute("""SELECT id,tenant_id,name,role FROM users WHERE tenant_id=%s AND role='local_admin' AND status='active'
        ORDER BY created_at LIMIT 1""", (tenant_id,)).fetchone()
    documents = db.execute("""SELECT d.*, r.supplier_name, r.buyer_name FROM documents d
        LEFT JOIN standardised_records r ON r.document_id=d.id AND r.tenant_id=d.tenant_id AND r.is_current
        WHERE d.tenant_id=%s AND d.excluded_at IS NULL ORDER BY d.uploaded_at""", (tenant_id,)).fetchall()
    for d in documents:
        fields = _fields(db, d["id"])
        seller = d["supplier_name"] or _field_value(fields, "supplier")
        buyer = d["buyer_name"] or _field_value(fields, "customer")
        way = direction(identity, seller_name=seller, seller_tin=_field_value(fields, "supplier_tin", "supplier_tax_id"),
                        seller_registration=_field_value(fields, "supplier_registration_id"), buyer_name=buyer,
                        buyer_tin=_field_value(fields, "customer_tax_id"))
        if way == "sales" or d["category"] == "accounts_receivable":
            report["own_sales_documents"] += 1
            journals = db.execute("""SELECT j.id,p.status AS period_status FROM journal_headers j
                JOIN accounting_periods p ON p.id=j.period_id AND p.tenant_id=j.tenant_id
                WHERE j.tenant_id=%s AND j.source_type='document' AND j.source_id=%s AND j.status='posted'""",
                                  (tenant_id, d["id"])).fetchall()
            if any(j["period_status"] != "open" for j in journals) or (journals and not admin):
                report["skipped_closed_period"] += 1
                continue
            if apply:
                from services.routes.erp.posting import reverse_in_transaction
                for j in journals:
                    reverse_in_transaction(db, dict(admin), j["id"], reason=f"{d['document_code']} is an own sales document "
                                                                          "captured by mistake (Plan M B1)")
                jobs = db.execute("""DELETE FROM pending_jobs WHERE document_id=%s AND tenant_id=%s
                    AND status NOT IN ('succeeded','failed','processing') RETURNING id""", (d["id"], tenant_id)).fetchall()
                bench = db.execute("DELETE FROM benchmark_demo_documents WHERE document_id=%s AND tenant_id=%s RETURNING document_id",
                                   (d["id"], tenant_id)).fetchall()
                db.execute("DELETE FROM workload_assignments WHERE document_id=%s", (d["id"],))
                db.execute("""UPDATE documents SET excluded_at=now(), excluded_reason=%s WHERE id=%s AND tenant_id=%s""",
                           ("Own sales document: create it under Sales (Plan M B1)", d["id"], tenant_id))
                report["jobs_removed"] += len(jobs)
                report["benchmark_removed"] += len(bench)
            report["reversed_journals"] += len(journals)
            continue
        identifier = _usable_document_identifier(fields, d["kind"])
        record = db.execute("""SELECT invoice_number,reference_number,invoice_date FROM standardised_records
            WHERE document_id=%s AND is_current""", (d["id"],)).fetchone()
        if record:
            identifier = record["invoice_number"] or record["reference_number"] or identifier
            if record["invoice_date"]:
                fields = [{"field_key": "date", "current_value": str(record["invoice_date"])}, *fields]
        if seller:
            fields = [{"field_key": "supplier", "current_value": seller}, *fields]
        if not identifier and not (_field_value(fields, "date") and seller):
            continue
        name = standard_filename(db, d, fields, identifier)
        if name != d["original_filename"]:
            if db.execute("SELECT 1 FROM documents WHERE tenant_id=%s AND original_filename=%s AND id<>%s",
                          (tenant_id, name, d["id"])).fetchone():
                stem, _, ext = name.rpartition(".")
                name = f"{stem}__{d['document_code']}.{ext}"
            if apply:
                db.execute("UPDATE documents SET original_filename=%s, updated_at=now() WHERE id=%s AND tenant_id=%s",
                           (name, d["id"], tenant_id))
            report["renamed"] += 1
    return report


def run(args):
    from services.admin.demo_company import refusal_reason
    reason = refusal_reason(os.environ)
    if reason:
        print(f"Refused: {reason}", file=sys.stderr)
        return 2
    with connect() as db:
        tenants = db.execute("SELECT id,name FROM tenants WHERE (%s::text IS NULL OR name=%s) ORDER BY name",
                             (args.tenant, args.tenant)).fetchall()
        for tenant in tenants:
            report = tidy(db, tenant["id"], apply=args.apply)
            print(f"{tenant['name']}: " + ", ".join(f"{k}={v}" for k, v in report.items()))
        if not args.apply:
            db.rollback()
            print("Dry run: nothing written. Add --apply to write.")
    return 0
