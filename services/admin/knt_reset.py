"""Wipe and rebuild only the local KNT workspace's demo books."""
from pathlib import Path

# Tenant-owned children with restrictive references are deleted before parents.
# The parent documents also cascade to their review history, tags and matches.
TENANT_TABLES = (
    "benchmark_demo_documents", "benchmark_demo_tenants",
    "erp_document_allocations", "myinvois_consolidated_documents", "myinvois_consolidated_submissions",
    "reconciliation_decisions", "reconciliation_candidates",
    "bank_coding_decisions", "bank_coding_suggestions", "bank_transaction_anomalies",
    "bank_reconciliation_signoffs", "bank_reconciliation_reopens", "bank_reconciliation_periods",
    "bank_transactions", "bank_imports",
    "settlement_allocations", "erp_document_headers", "settlements",
    "journal_lines", "journal_headers", "accounting_periods",
    "record_amendments", "coding_decisions", "coding_suggestions",
    "po_match_decisions", "po_match_proposals", "goods_receipts", "purchase_orders",
    "review_decisions", "exceptions", "validation_results",
)
CATALOG_TABLES = (
    "erp_products", "erp_catalog_entries", "erp_posting_accounts", "erp_number_counters",
    "vendors", "customers", "ledger_accounts", "coa_accounts", "cost_centres",
)
# Local reset is the one path that erases immutable fixture history. Disable only
# the named guards, transactionally; foreign keys stay active throughout.
DELETE_GUARDS = (
    ("benchmark_demo_documents", "benchmark_demo_registration_immutable"),
    ("reconciliation_decisions", "trg_recon_decisions_append_only"),
    ("reconciliation_candidates", "trg_recon_candidate_edit_guard"),
    ("bank_coding_decisions", "trg_bank_coding_decisions_append_only"),
    ("bank_reconciliation_signoffs", "trg_bank_recon_signoffs_append_only"),
    ("bank_reconciliation_reopens", "trg_bank_recon_reopens_append_only"),
    ("bank_transactions", "trg_bank_transaction_guard"),
    ("settlement_allocations", "trg_settlement_allocations_append_only"),
    ("journal_lines", "trg_ledger_lines_append_only"),
    ("journal_headers", "trg_ledger_headers_append_only"),
    ("coding_decisions", "trg_coding_decisions_append_only"),
    ("po_match_decisions", "trg_po_decisions_append_only"),
    ("field_corrections", "trg_field_corrections_append_only"),
)


def wipe(db, tenant):
    from psycopg import sql

    db.execute("SET LOCAL lock_timeout='5s'")
    db.execute("SELECT id FROM tenants WHERE id=%s FOR UPDATE", (tenant,))
    stored = db.execute("SELECT id,storage_path FROM documents WHERE tenant_id=%s FOR UPDATE",
                        (tenant,)).fetchall()
    ids = [row["id"] for row in stored]
    for table, trigger in DELETE_GUARDS:
        db.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(sql.Identifier(table), sql.Identifier(trigger)))
    for table in TENANT_TABLES:
        db.execute(sql.SQL("DELETE FROM {} WHERE tenant_id=%s").format(sql.Identifier(table)), (tenant,))
    db.execute("""DELETE FROM field_corrections c USING field_predictions p
        WHERE c.field_prediction_id=p.id AND p.document_id=ANY(%s)""", (ids,))
    for table in ("line_item_corrections", "field_predictions", "line_items", "raw_extractions", "pending_jobs"):
        db.execute(sql.SQL("DELETE FROM {} WHERE document_id=ANY(%s)").format(sql.Identifier(table)), (ids,))
    db.execute("DELETE FROM documents WHERE tenant_id=%s", (tenant,))
    db.execute("DELETE FROM document_batches WHERE tenant_id=%s", (tenant,))
    for table in CATALOG_TABLES:
        db.execute(sql.SQL("DELETE FROM {} WHERE tenant_id=%s").format(sql.Identifier(table)), (tenant,))
    for table, trigger in DELETE_GUARDS:
        db.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(sql.Identifier(table), sql.Identifier(trigger)))
    return [row["storage_path"] for row in stored]


def reset(connect, folder, password=None):
    import main
    from database.object_storage.object_store import local_storage_root
    from services.admin.knt_seed import find_company, seed

    main.check_schema_compatibility()
    with connect() as db:
        tenant = find_company(db)
        stored = wipe(db, tenant["id"]) if tenant else []
    if tenant:
        tenant_root = local_storage_root() / "tenants" / str(tenant["id"])
        for value in stored:
            if "://" in value:
                continue
            path = Path(value).resolve()
            if path.is_relative_to(tenant_root) and path.is_file():
                path.unlink()
        print(f"Cleared {len(stored)} KNT Inbox documents and the KNT accounting data.")
    seed(connect, folder, password)
