-- Supplier-level default for scanned bill posting.
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS default_expense_account_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'vendors'::regclass AND conname = 'fk_vendors_default_expense_account') THEN
        ALTER TABLE vendors ADD CONSTRAINT fk_vendors_default_expense_account
            FOREIGN KEY (default_expense_account_id, tenant_id) REFERENCES ledger_accounts(id, tenant_id)
            ON DELETE RESTRICT;
    END IF;
END $$;

-- Suppliers imported from a company pack before this column existed: use the default account code the
-- contacts import kept in supplier_contacts.notes, when it is an expense account in the company's chart.
-- The CASE keeps the jsonb cast to notes the import wrote (other notes are free text).
WITH pack AS (
    SELECT c.tenant_id, c.vendor_id,
           CASE WHEN c.notes LIKE '{%"source": "contacts-import"%}' THEN c.notes::jsonb->>'default_account_code' END AS code
    FROM supplier_contacts c
)
UPDATE vendors v SET default_expense_account_id = a.id
FROM pack p
JOIN ledger_accounts a ON a.tenant_id = p.tenant_id AND a.account_type = 'expense' AND a.code = p.code
WHERE v.default_expense_account_id IS NULL AND p.vendor_id = v.id AND p.tenant_id = v.tenant_id;
