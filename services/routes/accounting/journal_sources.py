"""SQL for identifying the source and currency of a journal and its reversal."""

JOURNAL_SOURCE_TYPE = "COALESCE(original.source_type,j.source_type)"
JOURNAL_SOURCE_KEY = "COALESCE(original.idempotency_key,j.idempotency_key)"
JOURNAL_SOURCE_ID = "CASE WHEN j.source_type='reversal' THEN original.source_id ELSE j.source_id END"
JOURNAL_CURRENCY = "COALESCE(sr.currency,settlement.currency,typed.currency)"
OPENING_JOURNAL = f"{JOURNAL_SOURCE_KEY} LIKE 'opening-balances:%%'"

# Prefer the posted revision over a later extraction. Source-linked journals
# without a revision in their key use the latest record for that document.
JOURNAL_CURRENCY_JOINS = f"""
    LEFT JOIN journal_headers original ON original.id=j.reversed_journal_id AND original.tenant_id=j.tenant_id
    LEFT JOIN LATERAL (
        SELECT record.currency
        FROM standardised_records record
        WHERE record.tenant_id=j.tenant_id AND record.document_id={JOURNAL_SOURCE_ID}
            AND {JOURNAL_SOURCE_TYPE} IN ('document','adjustment')
        ORDER BY (split_part({JOURNAL_SOURCE_KEY}, ':repost:', 1) IN (
            'supplier-bill:'||record.id::text||':revision:'||record.revision_number::text,
            'customer-invoice:'||record.id::text||':revision:'||record.revision_number::text,
            'adjustment:credit_note:'||record.id::text||':revision:'||record.revision_number::text,
            'adjustment:debit_note:'||record.id::text||':revision:'||record.revision_number::text
        )) DESC, record.revision_number DESC
        LIMIT 1
    ) sr ON true
    LEFT JOIN settlements settlement ON settlement.tenant_id=j.tenant_id
        AND {JOURNAL_SOURCE_TYPE}='settlement' AND settlement.id={JOURNAL_SOURCE_ID}
    LEFT JOIN erp_document_headers typed ON typed.tenant_id=j.tenant_id
        AND {JOURNAL_SOURCE_TYPE}='erp_document' AND typed.id={JOURNAL_SOURCE_ID}
"""


def in_books(j, end="%(end)s"):
    """SQL: journal `j` is in the books at the cutoff `end`: dated by then and not reversed by a journal dated by then
    (the general ledger reads a reversed journal and its reversal as two entries, each on its own date)."""
    return (f"({j}.status IN ('posted','reversed') AND {j}.journal_date<={end}"
            f" AND NOT EXISTS (SELECT 1 FROM journal_headers {j}_undo WHERE {j}_undo.tenant_id={j}.tenant_id"
            f" AND {j}_undo.reversed_journal_id={j}.id AND {j}_undo.status='posted' AND {j}_undo.journal_date<={end}))")


def document_posted(r, account_type="%(account_type)s", end="%(end)s"):
    """SQL: the scanned record `r` (its uploaded document) has a journal in the books by `end` with a line on an account
    of `account_type` (asset for an invoice's receivable, liability for a bill's payable)."""
    return (f"""EXISTS (SELECT 1 FROM journal_headers doc_journal
            JOIN journal_lines doc_line ON doc_line.journal_id=doc_journal.id AND doc_line.tenant_id=doc_journal.tenant_id
            JOIN ledger_accounts doc_account ON doc_account.id=doc_line.account_id AND doc_account.tenant_id=doc_line.tenant_id
            WHERE doc_journal.tenant_id={r}.tenant_id AND doc_journal.source_type IN ('document','adjustment')
                AND doc_journal.source_id={r}.document_id AND {in_books('doc_journal', end)}
                AND doc_account.account_type={account_type})""")
