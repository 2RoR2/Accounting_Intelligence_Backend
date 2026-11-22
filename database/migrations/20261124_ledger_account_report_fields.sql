-- 020_ledger_account_report_fields.sql
-- Adds statement, normal_balance and report_section to ledger_accounts, so the chart import (coa.csv) can keep them.
-- statement and normal_balance are always filled by a trigger from the account type when not given.
-- report_section may be empty, because it cannot be worked out from the type.
-- (cost of sales and operating expenses are both expenses, so the type cannot tell them apart).

ALTER TABLE ledger_accounts ADD COLUMN IF NOT EXISTS statement TEXT;
ALTER TABLE ledger_accounts ADD COLUMN IF NOT EXISTS normal_balance TEXT;
ALTER TABLE ledger_accounts ADD COLUMN IF NOT EXISTS report_section TEXT;

CREATE OR REPLACE FUNCTION ledger_account_report_defaults() RETURNS trigger AS $$
DECLARE
    st TEXT := CASE WHEN NEW.account_type IN ('asset', 'liability', 'equity') THEN 'balance_sheet' ELSE 'profit_and_loss' END;
    nb TEXT := CASE WHEN NEW.account_type IN ('asset', 'expense') THEN 'debit' ELSE 'credit' END;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.account_type <> OLD.account_type THEN
        IF NEW.statement IS NOT DISTINCT FROM OLD.statement THEN NEW.statement := st; END IF;
        IF NEW.normal_balance IS NOT DISTINCT FROM OLD.normal_balance THEN NEW.normal_balance := nb; END IF;
    END IF;
    NEW.statement := COALESCE(NEW.statement, st);
    NEW.normal_balance := COALESCE(NEW.normal_balance, nb);
    NEW.report_section := NULLIF(btrim(NEW.report_section), '');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ledger_account_report_defaults ON ledger_accounts;
CREATE TRIGGER trg_ledger_account_report_defaults
    BEFORE INSERT OR UPDATE ON ledger_accounts
    FOR EACH ROW EXECUTE FUNCTION ledger_account_report_defaults();

-- fill the accounts that already exist (the trigger works out the values)
UPDATE ledger_accounts SET statement = NULL, normal_balance = NULL WHERE statement IS NULL OR normal_balance IS NULL;

ALTER TABLE ledger_accounts ALTER COLUMN statement SET NOT NULL;
ALTER TABLE ledger_accounts ALTER COLUMN normal_balance SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_ledger_accounts_statement') THEN
        ALTER TABLE ledger_accounts ADD CONSTRAINT ck_ledger_accounts_statement
            CHECK ((account_type IN ('asset', 'liability', 'equity') AND statement = 'balance_sheet')
                OR (account_type IN ('income', 'expense') AND statement = 'profit_and_loss'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_ledger_accounts_normal_balance') THEN
        ALTER TABLE ledger_accounts ADD CONSTRAINT ck_ledger_accounts_normal_balance CHECK (normal_balance IN ('debit', 'credit'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_ledger_accounts_report_section') THEN
        ALTER TABLE ledger_accounts ADD CONSTRAINT ck_ledger_accounts_report_section
            CHECK (report_section IS NULL OR btrim(report_section) <> '');
    END IF;
END $$;
