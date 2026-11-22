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
