-- P1 line defaults: a product remembers a default tax-code label, and the tax-code list becomes an editable catalog.
-- Idempotent; the same text is appended to the consolidated schema file.
-- The default income/expense accounts a product offers already exist (erp_products.income_account_id/expense_account_id).
-- Tax is never calculated: a tax code is a label printed on the line, the tax amount is typed.

ALTER TABLE erp_products ADD COLUMN IF NOT EXISTS default_tax_code TEXT;

DO $$
DECLARE
    old_check TEXT;
BEGIN
    SELECT conname INTO old_check FROM pg_constraint
    WHERE conrelid = 'erp_catalog_entries'::regclass AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%''payment-term''%' AND pg_get_constraintdef(oid) NOT LIKE '%''tax-code''%';
    IF old_check IS NOT NULL THEN
        EXECUTE format('ALTER TABLE erp_catalog_entries DROP CONSTRAINT %I', old_check);
        ALTER TABLE erp_catalog_entries ADD CONSTRAINT erp_catalog_entries_kind_check CHECK (kind IN (
            'product-group','category','coupon','content-page','price-level','payment-term','payment-method','currency',
            'asset-type','location','number-format','form-design','bank-account','fixed-asset','store-setting','tax-code'));
    END IF;
END $$;
