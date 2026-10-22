-- Money In and Money Out
-- Accounting Intelligence (Group 14), database part
--
-- HOW TO RUN
--   Run this file AFTER the main schema (PostgreSQL Database schema.sql) and, if you use it, the mock data.
--   In DBeaver: open a script on the right connection, paste this file, press Alt+X.
--   It runs as ONE transaction. If any statement fails, click Stop, then run ROLLBACK; and nothing is saved.
--   It is safe to run twice: existing tables, functions and triggers are skipped or replaced.
--   Test it first on a COPY (the Local connection) with money_in_out_test.sql.
--
-- WHAT IT CREATES
--   money_transactions          one row per Money In or Money Out (direction says which)
--   money_transaction_items     what the money was for (account and amount)
--   money_transaction_payments  which bank or cash account it moved through, with the fee
--   money_transaction_totals    a view with the item, payment and fee totals of each transaction
--   Small supporting tables the three tables point to: accounts, tax_codes, payment_methods, bank_accounts.
--   These are minimal versions with the agreed fields. If the full accounting tables already exist, the
--   CREATE TABLE IF NOT EXISTS lines below do nothing.
--
-- RULES THE DATABASE ENFORCES
--   1. A transaction starts as a draft. Statuses: draft, pending_approval, ready, void.
--   2. Leaving draft needs at least one item and one payment, and the item total must equal the payment total.
--      (The fee is not counted in either total. If the Bukku demo shows otherwise, change money_transaction_guard.)
--   3. Items and payments can only change while the transaction is a draft.
--   4. A ready transaction cannot be edited or deleted, only voided. Void is final.
--      Only a draft or a void transaction can be deleted.
--   5. Becoming ready needs posted_by. Voiding needs voided_by and void_reason.
--      Set journal_entry_id in the SAME update that sets status to ready, because a ready row is locked.
--   6. Amounts are above 0. A fee needs a fee account. Money in has no vendor and money out has no customer.
--   7. Every account, bank account, payment method, tax code, customer, vendor and document must belong to the
--      same company as the transaction. Archived accounts and inactive bank accounts, methods and tax codes
--      cannot be used.
--   8. Transaction numbers are generated when blank (RCP-n for money in, PV-n for money out), like record codes.
--
-- TO MERGE INTO THE SCHEMA FILE
--   Paste this after the last CREATE TABLE and add these lines to the drop list at the top:
--     DROP VIEW IF EXISTS money_transaction_totals;
--     DROP TABLE IF EXISTS money_transaction_payments, money_transaction_items, money_transactions CASCADE;
--     DROP FUNCTION IF EXISTS set_money_transaction_number() CASCADE;
--     DROP FUNCTION IF EXISTS check_money_transaction_insert() CASCADE;
--     DROP FUNCTION IF EXISTS money_transaction_guard() CASCADE;
--     DROP FUNCTION IF EXISTS money_transaction_no_delete() CASCADE;
--     DROP FUNCTION IF EXISTS money_line_guard() CASCADE;
--     DROP FUNCTION IF EXISTS money_item_links_guard() CASCADE;
--     DROP FUNCTION IF EXISTS money_payment_links_guard() CASCADE;
--     DROP FUNCTION IF EXISTS money_check_header_links(UUID, UUID, UUID, UUID) CASCADE;
--     DROP SEQUENCE IF EXISTS money_in_number_seq, money_out_number_seq;
--   Keep the schema file's Windows line endings, and update the schema fingerprint in the PostgreSQL tests.

BEGIN;

-- ============================================================
-- 1. Supporting tables (minimal versions)
-- ============================================================

CREATE TABLE IF NOT EXISTS accounts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL CHECK (category IN ('assets', 'liabilities', 'equity', 'income', 'expenses')),
    parent_id   UUID REFERENCES accounts(id),
    description TEXT,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    created_by  UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code)
);

CREATE TABLE IF NOT EXISTS tax_codes (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code       TEXT NOT NULL,
    name       TEXT NOT NULL,
    rate       NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (rate >= 0 AND rate <= 100),
    tax_type   TEXT NOT NULL CHECK (tax_type IN ('sales_tax', 'service_tax', 'exempt')),
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code)
);

CREATE TABLE IF NOT EXISTS payment_methods (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS bank_accounts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    bank_name      TEXT,
    account_number TEXT,
    currency       CHAR(3) NOT NULL DEFAULT 'MYR',
    account_id     UUID REFERENCES accounts(id),
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- ============================================================
-- 2. Money In and Money Out tables
-- ============================================================

CREATE SEQUENCE IF NOT EXISTS money_in_number_seq START 1;
CREATE SEQUENCE IF NOT EXISTS money_out_number_seq START 1;

CREATE TABLE IF NOT EXISTS money_transactions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    direction          TEXT NOT NULL CHECK (direction IN ('money_in', 'money_out')),
    transaction_number TEXT NOT NULL,
    transaction_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    customer_id        UUID REFERENCES customers(id),
    vendor_id          UUID REFERENCES vendors(id),
    currency           CHAR(3) NOT NULL DEFAULT 'MYR',
    reference_number   TEXT,
    remarks            TEXT,
    description        TEXT,
    source_document_id UUID REFERENCES documents(id),
    status             TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'pending_approval', 'ready', 'void')),
    -- No foreign key yet: the journal tables do not exist. When they do, run:
    --   ALTER TABLE money_transactions ADD CONSTRAINT fk_money_transactions_journal
    --       FOREIGN KEY (journal_entry_id) REFERENCES journal_entries(id);
    journal_entry_id   UUID,
    created_by         UUID REFERENCES users(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_by          UUID REFERENCES users(id),
    posted_at          TIMESTAMPTZ,
    voided_by          UUID REFERENCES users(id),
    voided_at          TIMESTAMPTZ,
    void_reason        TEXT,
    CONSTRAINT uq_money_transactions_number UNIQUE (tenant_id, direction, transaction_number),
    CONSTRAINT ck_money_contact CHECK (
        (direction = 'money_in' AND vendor_id IS NULL)
        OR (direction = 'money_out' AND customer_id IS NULL)
    ),
    CONSTRAINT ck_money_ready_fields CHECK (
        status <> 'ready' OR (posted_by IS NOT NULL AND posted_at IS NOT NULL)
    ),
    CONSTRAINT ck_money_void_fields CHECK (
        status <> 'void' OR (voided_by IS NOT NULL AND voided_at IS NOT NULL AND void_reason IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS money_transaction_items (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    money_transaction_id UUID NOT NULL REFERENCES money_transactions(id) ON DELETE CASCADE,
    line_number          INTEGER NOT NULL CHECK (line_number > 0),
    account_id           UUID NOT NULL REFERENCES accounts(id),
    description          TEXT,
    amount               NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    tax_code_id          UUID REFERENCES tax_codes(id),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (money_transaction_id, line_number)
);

CREATE TABLE IF NOT EXISTS money_transaction_payments (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    money_transaction_id UUID NOT NULL REFERENCES money_transactions(id) ON DELETE CASCADE,
    line_number          INTEGER NOT NULL CHECK (line_number > 0),
    payment_method_id    UUID NOT NULL REFERENCES payment_methods(id),
    bank_account_id      UUID NOT NULL REFERENCES bank_accounts(id),
    amount               NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    reference_number     TEXT,
    fee_amount           NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (fee_amount >= 0),
    fee_account_id       UUID REFERENCES accounts(id),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (money_transaction_id, line_number),
    CONSTRAINT ck_money_payment_fee CHECK (fee_amount = 0 OR fee_account_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_money_transactions_list
    ON money_transactions (tenant_id, direction, transaction_date DESC);
CREATE INDEX IF NOT EXISTS idx_money_transactions_customer
    ON money_transactions (customer_id) WHERE customer_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_money_transactions_vendor
    ON money_transactions (vendor_id) WHERE vendor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_money_transactions_document
    ON money_transactions (source_document_id) WHERE source_document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_money_items_account
    ON money_transaction_items (account_id);
CREATE INDEX IF NOT EXISTS idx_money_payments_bank
    ON money_transaction_payments (bank_account_id);

-- ============================================================
-- 3. Functions and triggers
-- ============================================================

-- Fills a blank transaction number: RCP-n for money in, PV-n for money out.
CREATE OR REPLACE FUNCTION set_money_transaction_number() RETURNS trigger AS $$
BEGIN
    IF NEW.transaction_number IS NULL OR btrim(NEW.transaction_number) = '' THEN
        IF NEW.direction = 'money_in' THEN
            NEW.transaction_number := 'RCP-' || nextval('money_in_number_seq')::text;
        ELSE
            NEW.transaction_number := 'PV-' || nextval('money_out_number_seq')::text;
        END IF;
    ELSE
        NEW.transaction_number := btrim(NEW.transaction_number);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The customer, vendor and source document must belong to the transaction's company.
CREATE OR REPLACE FUNCTION money_check_header_links(t UUID, c UUID, v UUID, d UUID) RETURNS void AS $$
BEGIN
    IF c IS NOT NULL AND NOT EXISTS (SELECT 1 FROM customers WHERE id = c AND tenant_id = t) THEN
        RAISE EXCEPTION 'The customer belongs to a different company.';
    END IF;
    IF v IS NOT NULL AND NOT EXISTS (SELECT 1 FROM vendors WHERE id = v AND tenant_id = t) THEN
        RAISE EXCEPTION 'The vendor belongs to a different company.';
    END IF;
    IF d IS NOT NULL AND NOT EXISTS (SELECT 1 FROM documents WHERE id = d AND tenant_id = t) THEN
        RAISE EXCEPTION 'The source document belongs to a different company.';
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION check_money_transaction_insert() RETURNS trigger AS $$
BEGIN
    IF NEW.status <> 'draft' THEN
        RAISE EXCEPTION 'A money transaction must start as a draft.';
    END IF;
    PERFORM money_check_header_links(NEW.tenant_id, NEW.customer_id, NEW.vendor_id, NEW.source_document_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Status changes, the ready lock, the void rules and the checks for leaving draft.
CREATE OR REPLACE FUNCTION money_transaction_guard() RETURNS trigger AS $$
DECLARE
    item_count INTEGER;
    item_total NUMERIC;
    pay_count  INTEGER;
    pay_total  NUMERIC;
BEGIN
    IF NEW.tenant_id <> OLD.tenant_id OR NEW.direction <> OLD.direction THEN
        RAISE EXCEPTION 'The company and the direction of a money transaction cannot be changed.';
    END IF;

    IF OLD.status = 'void' THEN
        RAISE EXCEPTION 'A void money transaction cannot be changed.';
    END IF;

    IF OLD.status = 'ready' THEN
        -- a ready transaction may only be voided, and nothing else may change with it
        IF NEW.status <> 'void'
           OR (to_jsonb(NEW) - ARRAY['status', 'voided_by', 'voided_at', 'void_reason'])
              <> (to_jsonb(OLD) - ARRAY['status', 'voided_by', 'voided_at', 'void_reason']) THEN
            RAISE EXCEPTION 'A ready money transaction cannot be edited, only voided.';
        END IF;
    END IF;

    IF OLD.status = 'pending_approval' AND NEW.status = 'pending_approval' THEN
        RAISE EXCEPTION 'A money transaction waiting for approval cannot be edited.';
    END IF;

    IF NEW.status <> OLD.status THEN
        IF NOT (
            (OLD.status = 'draft' AND NEW.status IN ('pending_approval', 'ready', 'void'))
            OR (OLD.status = 'pending_approval' AND NEW.status IN ('draft', 'ready', 'void'))
            OR (OLD.status = 'ready' AND NEW.status = 'void')
        ) THEN
            RAISE EXCEPTION 'A money transaction cannot move from % to %.', OLD.status, NEW.status;
        END IF;
    END IF;

    IF OLD.status = 'draft' AND NEW.status IN ('pending_approval', 'ready') THEN
        SELECT count(*), COALESCE(sum(amount), 0) INTO item_count, item_total
        FROM money_transaction_items WHERE money_transaction_id = NEW.id;
        SELECT count(*), COALESCE(sum(amount), 0) INTO pay_count, pay_total
        FROM money_transaction_payments WHERE money_transaction_id = NEW.id;
        IF item_count = 0 OR pay_count = 0 THEN
            RAISE EXCEPTION 'A money transaction needs at least one item and one payment.';
        END IF;
        IF item_total <> pay_total THEN
            RAISE EXCEPTION 'The item total (%) must equal the payment total (%).', item_total, pay_total;
        END IF;
    END IF;

    IF NEW.status = 'ready' AND OLD.status <> 'ready' THEN
        IF NEW.posted_by IS NULL THEN
            RAISE EXCEPTION 'posted_by is required when a money transaction becomes ready.';
        END IF;
        NEW.posted_at := COALESCE(NEW.posted_at, now());
    END IF;

    IF NEW.status = 'void' AND OLD.status <> 'void' THEN
        IF NEW.voided_by IS NULL OR NEW.void_reason IS NULL OR btrim(NEW.void_reason) = '' THEN
            RAISE EXCEPTION 'voided_by and void_reason are required to void a money transaction.';
        END IF;
        NEW.voided_at := COALESCE(NEW.voided_at, now());
    END IF;

    IF NEW.customer_id IS DISTINCT FROM OLD.customer_id
       OR NEW.vendor_id IS DISTINCT FROM OLD.vendor_id
       OR NEW.source_document_id IS DISTINCT FROM OLD.source_document_id THEN
        PERFORM money_check_header_links(NEW.tenant_id, NEW.customer_id, NEW.vendor_id, NEW.source_document_id);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Only a draft or a void transaction can be deleted.
CREATE OR REPLACE FUNCTION money_transaction_no_delete() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('pending_approval', 'ready') THEN
        RAISE EXCEPTION 'A % money transaction cannot be deleted. Void a ready one instead.', OLD.status;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

-- Items and payments: only while the transaction is a draft, and in the same company.
CREATE OR REPLACE FUNCTION money_line_guard() RETURNS trigger AS $$
DECLARE
    parent_id     UUID;
    parent_status TEXT;
    parent_tenant UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        parent_id := OLD.money_transaction_id;
    ELSE
        parent_id := NEW.money_transaction_id;
    END IF;

    SELECT status, tenant_id INTO parent_status, parent_tenant
    FROM money_transactions WHERE id = parent_id;

    IF parent_status IS NULL THEN
        -- the transaction is being deleted together with its lines
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'The money transaction does not exist.';
    END IF;

    IF parent_status <> 'draft' THEN
        RAISE EXCEPTION 'Items and payments can only change while the money transaction is a draft.';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    IF NEW.tenant_id <> parent_tenant THEN
        RAISE EXCEPTION 'The line belongs to a different company than its money transaction.';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.money_transaction_id <> OLD.money_transaction_id THEN
            RAISE EXCEPTION 'A line cannot be moved to another money transaction.';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION money_item_links_guard() RETURNS trigger AS $$
DECLARE
    check_account BOOLEAN := TRUE;
    check_tax     BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        check_account := NEW.account_id IS DISTINCT FROM OLD.account_id;
        check_tax := NEW.tax_code_id IS NOT NULL AND NEW.tax_code_id IS DISTINCT FROM OLD.tax_code_id;
    ELSE
        check_tax := NEW.tax_code_id IS NOT NULL;
    END IF;

    IF check_account THEN
        IF NOT EXISTS (SELECT 1 FROM accounts
                       WHERE id = NEW.account_id AND tenant_id = NEW.tenant_id AND NOT is_archived) THEN
            RAISE EXCEPTION 'The account is archived or belongs to a different company.';
        END IF;
    END IF;

    IF check_tax THEN
        IF NOT EXISTS (SELECT 1 FROM tax_codes
                       WHERE id = NEW.tax_code_id AND tenant_id = NEW.tenant_id AND is_active) THEN
            RAISE EXCEPTION 'The tax code is inactive or belongs to a different company.';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION money_payment_links_guard() RETURNS trigger AS $$
DECLARE
    check_bank   BOOLEAN := TRUE;
    check_method BOOLEAN := TRUE;
    check_fee    BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        check_bank := NEW.bank_account_id IS DISTINCT FROM OLD.bank_account_id;
        check_method := NEW.payment_method_id IS DISTINCT FROM OLD.payment_method_id;
        check_fee := NEW.fee_account_id IS NOT NULL AND NEW.fee_account_id IS DISTINCT FROM OLD.fee_account_id;
    ELSE
        check_fee := NEW.fee_account_id IS NOT NULL;
    END IF;

    IF check_bank THEN
        IF NOT EXISTS (SELECT 1 FROM bank_accounts
                       WHERE id = NEW.bank_account_id AND tenant_id = NEW.tenant_id AND is_active) THEN
            RAISE EXCEPTION 'The bank account is inactive or belongs to a different company.';
        END IF;
    END IF;

    IF check_method THEN
        IF NOT EXISTS (SELECT 1 FROM payment_methods
                       WHERE id = NEW.payment_method_id AND tenant_id = NEW.tenant_id AND is_active) THEN
            RAISE EXCEPTION 'The payment method is inactive or belongs to a different company.';
        END IF;
    END IF;

    IF check_fee THEN
        IF NOT EXISTS (SELECT 1 FROM accounts
                       WHERE id = NEW.fee_account_id AND tenant_id = NEW.tenant_id AND NOT is_archived) THEN
            RAISE EXCEPTION 'The fee account is archived or belongs to a different company.';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Triggers fire in alphabetical order of their names, hence the numbers.
DROP TRIGGER IF EXISTS trg_money_transactions_1_number ON money_transactions;
CREATE TRIGGER trg_money_transactions_1_number
    BEFORE INSERT ON money_transactions
    FOR EACH ROW EXECUTE FUNCTION set_money_transaction_number();

DROP TRIGGER IF EXISTS trg_money_transactions_2_insert_check ON money_transactions;
CREATE TRIGGER trg_money_transactions_2_insert_check
    BEFORE INSERT ON money_transactions
    FOR EACH ROW EXECUTE FUNCTION check_money_transaction_insert();

DROP TRIGGER IF EXISTS trg_money_transactions_3_guard ON money_transactions;
CREATE TRIGGER trg_money_transactions_3_guard
    BEFORE UPDATE ON money_transactions
    FOR EACH ROW EXECUTE FUNCTION money_transaction_guard();

DROP TRIGGER IF EXISTS trg_money_transactions_4_no_delete ON money_transactions;
CREATE TRIGGER trg_money_transactions_4_no_delete
    BEFORE DELETE ON money_transactions
    FOR EACH ROW EXECUTE FUNCTION money_transaction_no_delete();

DROP TRIGGER IF EXISTS trg_money_items_1_guard ON money_transaction_items;
CREATE TRIGGER trg_money_items_1_guard
    BEFORE INSERT OR UPDATE OR DELETE ON money_transaction_items
    FOR EACH ROW EXECUTE FUNCTION money_line_guard();

DROP TRIGGER IF EXISTS trg_money_items_2_links ON money_transaction_items;
CREATE TRIGGER trg_money_items_2_links
    BEFORE INSERT OR UPDATE ON money_transaction_items
    FOR EACH ROW EXECUTE FUNCTION money_item_links_guard();

DROP TRIGGER IF EXISTS trg_money_payments_1_guard ON money_transaction_payments;
CREATE TRIGGER trg_money_payments_1_guard
    BEFORE INSERT OR UPDATE OR DELETE ON money_transaction_payments
    FOR EACH ROW EXECUTE FUNCTION money_line_guard();

DROP TRIGGER IF EXISTS trg_money_payments_2_links ON money_transaction_payments;
CREATE TRIGGER trg_money_payments_2_links
    BEFORE INSERT OR UPDATE ON money_transaction_payments
    FOR EACH ROW EXECUTE FUNCTION money_payment_links_guard();

-- ============================================================
-- 4. Totals view, for list screens and reports
-- ============================================================

CREATE OR REPLACE VIEW money_transaction_totals AS
SELECT t.id AS money_transaction_id,
       t.tenant_id,
       t.direction,
       t.transaction_number,
       t.transaction_date,
       t.currency,
       t.status,
       COALESCE((SELECT sum(i.amount) FROM money_transaction_items i
                 WHERE i.money_transaction_id = t.id), 0) AS item_total,
       COALESCE((SELECT sum(p.amount) FROM money_transaction_payments p
                 WHERE p.money_transaction_id = t.id), 0) AS payment_total,
       COALESCE((SELECT sum(p.fee_amount) FROM money_transaction_payments p
                 WHERE p.money_transaction_id = t.id), 0) AS fee_total
FROM money_transactions t;

COMMIT;
