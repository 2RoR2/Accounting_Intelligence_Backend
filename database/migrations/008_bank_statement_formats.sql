-- 008_bank_statement_formats.sql
-- Allow bank statement imports from CSV, Excel workbooks, and PDFs.

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'bank_imports'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%input_format%';

    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE bank_imports DROP CONSTRAINT %I', constraint_name);
    END IF;
END $$;

ALTER TABLE bank_imports
    ADD CONSTRAINT bank_imports_input_format_check
    CHECK (input_format IN ('csv', 'xlsx', 'pdf'));
