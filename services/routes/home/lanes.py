"""Shared definitions for the Today page counts and the lists they open (plan v2, task B3).

A Today tile counts rows with one of these predicates and the list it opens filters with the same predicate,
so the number on the tile and the length of the list cannot drift apart. Status meanings follow the existing
workflow in services/routes/documents/routes.py and validation/documents/document_validation.py:
extracted / corrected / exception wait for the accountant, in_review waits for a local admin's decision,
validated / standardised / completed are approved. Nothing here changes a status.

SQL aliases: `d` = documents, `r` = standardised_records.
"""
from __future__ import annotations

# Same visibility as list_documents in services/routes/documents/routes.py (kept in sync by a test).
ACCOUNTANT_DOCUMENT_VISIBILITY = ("(d.owner_id=%s OR d.assigned_to=%s OR EXISTS (SELECT 1 FROM benchmark_demo_documents bd "
                                  "JOIN benchmark_demo_tenants bt ON bt.tenant_id=bd.tenant_id AND bt.enabled=true "
                                  "WHERE bd.tenant_id=d.tenant_id AND bd.document_id=d.id))")

OPEN_EXCEPTION_ON_DOCUMENT = ("EXISTS (SELECT 1 FROM exceptions e WHERE e.document_id=d.id AND e.tenant_id=d.tenant_id "
                              "AND e.status <> 'resolved')")

# The stored-result half of _require_approvable_document: the current revision was validated and nothing fails.
CURRENT_REVISION_PASSED = (
    "EXISTS (SELECT 1 FROM validation_results v WHERE v.document_id=d.id AND v.tenant_id=d.tenant_id "
    "AND v.content_revision=d.content_revision) "
    "AND NOT EXISTS (SELECT 1 FROM validation_results v WHERE v.document_id=d.id AND v.tenant_id=d.tenant_id "
    "AND v.content_revision=d.content_revision "
    "AND (v.status='failed' OR v.prevents_approval OR (v.status='not_checked' AND v.requires_review)))")

DOCUMENT_LANES = {
    # Everything waiting for the accountant's check.
    'to-review': "d.status IN ('extracted','corrected','exception')",
    # Checks passed on the current revision and no open exception: the next step is submit / approve.
    'ready': f"d.status IN ('extracted','corrected') AND NOT {OPEN_EXCEPTION_ON_DOCUMENT} AND {CURRENT_REVISION_PASSED}",
    # At least one open exception (the Exceptions page lists them).
    'needs-fixing': OPEN_EXCEPTION_ON_DOCUMENT,
    # Submitted by an accountant, waiting for a local admin's approval.
    'waiting-approval': "d.status='in_review'",
}

# A record still to be paid or received: the printed amount due when there is one (as bank matching and
# settlement allocation use it), otherwise the total, less allocations still in force. Allocations are append-only:
# undoing one (reverse_allocation, or undoing a bank match) adds a 'reversed' row pointing at the original, which
# itself stays 'active', so an active allocation with a reversal row no longer counts.
OUTSTANDING = ("(COALESCE(r.amount_due, r.total_amount) - COALESCE((SELECT SUM(a.amount) FROM settlement_allocations a "
               "WHERE a.record_id=r.id AND a.tenant_id=r.tenant_id AND a.status='active' "
               "AND NOT EXISTS (SELECT 1 FROM settlement_allocations x WHERE x.reverses_allocation_id=a.id "
               "AND x.tenant_id=a.tenant_id)), 0))")

# Bank lines (alias t) still waiting for a person: not matched, not ignored and not classified (a fee, interest or
# owner top-up coded to an account), as the schema's unmatched_bank_transactions view and _period_result define it.
BANK_LINE_CLASSIFIED = "EXISTS (SELECT 1 FROM current_bank_coding c WHERE c.bank_transaction_id=t.id AND c.tenant_id=t.tenant_id)"
BANK_LINE_TO_MATCH = f"t.status IN ('imported','needs_review','proposed') AND NOT {BANK_LINE_CLASSIFIED}"

# Receipts are already paid and credit / debit notes adjust another document, so only invoices and bills are open items.
OPEN_ITEM_KINDS = {'accounts_payable': ('invoice', 'bill'), 'accounts_receivable': ('invoice',)}

DUE_WINDOWS = {
    'next-7-days': "r.due_date >= %s::date AND r.due_date <= %s::date + 7",
    'overdue': "r.due_date < %s::date",
}

# Supplier bills and customer invoices post with source_type 'document'; credit and debit notes post through
# post_document_adjustment with source_type 'adjustment'. Both carry the record's document id.
POSTED_JOURNAL_FOR_RECORD = ("EXISTS (SELECT 1 FROM journal_headers j WHERE j.tenant_id=r.tenant_id "
                             "AND j.source_type IN ('document','adjustment') AND j.source_id=r.document_id AND j.status='posted')")

# Tables each open-item query reads; without them the tiles are unavailable rather than an error.
OPEN_ITEM_TABLES = ('standardised_records', 'settlement_allocations')

APPROVED_RECORD = "r.is_current AND r.validation_status='passed'"


def document_lane(lane: str) -> str:
    """SQL predicate for one lane; KeyError for an unknown lane name."""
    return DOCUMENT_LANES[lane]


def open_item_base(category: str) -> tuple[str, list]:
    """WHERE fragment (alias r, needs `JOIN documents d ON d.id=r.document_id`) for approved, unpaid open items."""
    return (f"{APPROVED_RECORD} AND r.category=%s AND d.kind = ANY(%s) AND {OUTSTANDING} > 0",
            [category, list(OPEN_ITEM_KINDS[category])])


def due_window(window: str, today) -> tuple[str, list]:
    """WHERE fragment (alias r) for a due-date window relative to `today`; KeyError for an unknown window."""
    sql = DUE_WINDOWS[window]
    return sql, [today] * sql.count('%s')


def company_today(db, tenant_id):
    """The company's date in its workspace time zone (Asia/Kuala_Lumpur by default)."""
    row = db.execute("""SELECT (now() AT TIME ZONE COALESCE(
            (SELECT timezone FROM workspace_settings WHERE tenant_id=%s LIMIT 1), 'Asia/Kuala_Lumpur'))::date AS today""",
                     (tenant_id,)).fetchone()
    return row['today']


def records_to_post() -> str:
    """Approved records with no posted journal yet (aliases r and d). Only invoices, bills and credit notes post
    from the records list; an approved expense receipt is kept as a record and never waits here."""
    return f"{APPROVED_RECORD} AND d.kind IN ('invoice','bill','credit_note') AND NOT {POSTED_JOURNAL_FOR_RECORD}"
