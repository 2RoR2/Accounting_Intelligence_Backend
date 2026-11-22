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
