-- Plan L1: the internal sales/purchases module ("ERP" pages from latest_update) as reviewed schema.
-- latest_update created these tables during requests (_ensure_erp_schema); here they are part of the
-- schema and `manage.py migrate`. Every statement is idempotent: the same text is in the consolidated
-- schema file for fresh databases and runs again harmlessly on an upgraded one.
--
-- Contacts are NOT a new table: suppliers stay in `vendors` and customers in `customers` (the rows that
-- supplier memory, matching and imports already use); they gain the contact-detail columns below.
-- Only employees, which had no table, get `erp_employees`.
-- Nothing here initiates a payment or a statutory submission.

-- ---------------------------------------------------------------- contacts (L2)
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS contact_code TEXT;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS city TEXT;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS postcode TEXT;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS fields JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE customers ADD COLUMN IF NOT EXISTS contact_code TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS city TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS postcode TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS fields JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS erp_employees (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    legal_name TEXT NOT NULL CHECK (btrim(legal_name) <> ''),
    contact_code TEXT,
    registration_number TEXT,
    email TEXT,
    phone TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    postcode TEXT,
    fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, legal_name),
    UNIQUE (id, tenant_id)
);

-- ---------------------------------------------------------------- products and catalog (L3)
CREATE TABLE IF NOT EXISTS erp_products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    sku TEXT,
    kind TEXT NOT NULL DEFAULT 'inventory' CHECK (kind IN ('inventory','non-inventory','service','bundle')),
    unit TEXT NOT NULL DEFAULT 'unit',
    sale_price NUMERIC(18,4) NOT NULL DEFAULT 0 CHECK (sale_price >= 0),
    purchase_price NUMERIC(18,4) NOT NULL DEFAULT 0 CHECK (purchase_price >= 0),
    quantity NUMERIC(18,3) NOT NULL DEFAULT 0,
    group_name TEXT,
    income_account_id UUID,
    expense_account_id UUID,
    fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    FOREIGN KEY (income_account_id, tenant_id) REFERENCES ledger_accounts(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (expense_account_id, tenant_id) REFERENCES ledger_accounts(id, tenant_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_erp_products_sku ON erp_products(tenant_id, lower(sku)) WHERE sku IS NOT NULL AND btrim(sku) <> '';
CREATE INDEX IF NOT EXISTS idx_erp_products_list ON erp_products(tenant_id, name);

-- One generic table for the small lists (payment terms, methods, currencies, bank accounts, ...).
-- Tags and custom fields are NOT stored here: the API serves them from document_tags and custom_fields.
CREATE TABLE IF NOT EXISTS erp_catalog_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('product-group','category','coupon','content-page','price-level','payment-term',
        'payment-method','currency','asset-type','location','number-format','form-design','bank-account',
        'fixed-asset','store-setting')),
    code TEXT,
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    description TEXT,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- bank-account entries name the ledger cash/bank account they post to.
    ledger_account_id UUID,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, kind, name),
    UNIQUE (id, tenant_id),
    FOREIGN KEY (ledger_account_id, tenant_id) REFERENCES ledger_accounts(id, tenant_id) ON DELETE RESTRICT,
    CHECK (ledger_account_id IS NULL OR kind = 'bank-account')
);
CREATE INDEX IF NOT EXISTS idx_erp_catalog_list ON erp_catalog_entries(tenant_id, kind, name);

-- Default ledger accounts for posting typed documents (L4). Missing rows fall back to the standard
-- chart codes (1100 receivables, 4000 sales, 2030 SST output, 2000 payables, 5000 purchases,
-- 1140 SST input, 1010 bank) in the posting code.
CREATE TABLE IF NOT EXISTS erp_posting_accounts (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (purpose IN ('receivable','revenue','tax_output','payable','expense','tax_input','bank')),
    account_id UUID NOT NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, purpose),
    FOREIGN KEY (account_id, tenant_id) REFERENCES ledger_accounts(id, tenant_id) ON DELETE RESTRICT
);

-- ---------------------------------------------------------------- documents (L4)
CREATE TABLE IF NOT EXISTS erp_number_counters (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL,
    next_value INTEGER NOT NULL DEFAULT 1 CHECK (next_value >= 1),
    PRIMARY KEY (tenant_id, document_type)
);

CREATE TABLE IF NOT EXISTS erp_document_headers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL CHECK (document_type IN ('quotation','sales-order','delivery-order','invoice',
        'credit-note','payment','refund','purchase-order','goods-received','bill','purchase-credit-note',
        'purchase-payment','purchase-refund','money-in','money-out','bank-transfer','stock-adjustment',
        'stock-transfer','journal-entry','contra','deemed-payment','store-order','minipos-order')),
    document_number TEXT NOT NULL CHECK (btrim(document_number) <> ''),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','pending_approval','ready','void')),
    -- At most one contact, from the existing supplier/customer lists or the employee list.
    vendor_id UUID,
    customer_id UUID,
    employee_id UUID,
    contact_name TEXT,
    document_date DATE NOT NULL DEFAULT CURRENT_DATE,
    due_date DATE,
    currency CHAR(3) NOT NULL DEFAULT 'MYR' CHECK (currency = upper(currency)),
    reference_no TEXT,
    description TEXT,
    subtotal NUMERIC(18,2) NOT NULL DEFAULT 0,
    tax_amount NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (tax_amount >= 0),
    total_amount NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (total_amount >= 0),
    -- {"lines": [...], "tags": [...], "fields": {...}}; lines are validated by the API before they are stored.
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Bank/cash account (bank-account catalog entry) for payments, refunds, money in/out and transfers.
    bank_account_id UUID,
    -- Posting: set together when a financial document becomes ready; the reversal when it is voided.
    journal_id UUID,
    reversal_journal_id UUID,
    settlement_id UUID,
    posted_at TIMESTAMPTZ,
    void_reason TEXT,
    voided_at TIMESTAMPTZ,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, document_type, document_number),
    UNIQUE (id, tenant_id),
    FOREIGN KEY (vendor_id, tenant_id) REFERENCES vendors(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (customer_id, tenant_id) REFERENCES customers(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (employee_id, tenant_id) REFERENCES erp_employees(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (bank_account_id, tenant_id) REFERENCES erp_catalog_entries(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (journal_id, tenant_id) REFERENCES journal_headers(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (reversal_journal_id, tenant_id) REFERENCES journal_headers(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (settlement_id, tenant_id) REFERENCES settlements(id, tenant_id) ON DELETE RESTRICT,
    CHECK (num_nonnulls(vendor_id, customer_id, employee_id) <= 1),
    CHECK (status = 'void' OR (voided_at IS NULL AND reversal_journal_id IS NULL)),
    CHECK (reversal_journal_id IS NULL OR journal_id IS NOT NULL),
    CHECK (journal_id IS NULL OR status IN ('ready','void'))
);
CREATE INDEX IF NOT EXISTS idx_erp_documents_list ON erp_document_headers(tenant_id, document_type, document_date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_erp_documents_vendor ON erp_document_headers(tenant_id, vendor_id) WHERE vendor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_erp_documents_customer ON erp_document_headers(tenant_id, customer_id) WHERE customer_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_erp_documents_journal ON erp_document_headers(tenant_id, journal_id) WHERE journal_id IS NOT NULL;

-- Payment or credit note -> the typed invoice/bill it settles. Scanned (standardised) records keep using
-- settlement_allocations; this table only links typed documents.
CREATE TABLE IF NOT EXISTS erp_document_allocations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_document_id UUID NOT NULL,
    target_document_id UUID NOT NULL,
    amount NUMERIC(18,2) NOT NULL CHECK (amount > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','reversed')),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reversed_at TIMESTAMPTZ,
    UNIQUE (id, tenant_id),
    FOREIGN KEY (source_document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (target_document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE RESTRICT,
    CHECK (source_document_id <> target_document_id),
    CHECK ((status = 'active') = (reversed_at IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_erp_allocations_target ON erp_document_allocations(tenant_id, target_document_id, status);
CREATE INDEX IF NOT EXISTS idx_erp_allocations_source ON erp_document_allocations(tenant_id, source_document_id, status);

CREATE TABLE IF NOT EXISTS erp_document_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_document_id UUID NOT NULL,
    target_document_id UUID NOT NULL,
    link_type TEXT NOT NULL CHECK (link_type IN ('duplicate','transfer')),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_document_id, target_document_id, link_type),
    FOREIGN KEY (source_document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (target_document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE CASCADE
);

-- Document history. Deleting a draft removes its events; the deletion itself goes to audit_logs.
CREATE TABLE IF NOT EXISTS erp_document_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    reason TEXT,
    actor_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_erp_events_document ON erp_document_events(tenant_id, document_id, created_at);

-- ---------------------------------------------------------------- share links and MyInvois record (L7)
-- Off by default (ENABLE_SHARE_LINKS). Only the SHA-256 of the token is stored; links expire and can be revoked.
CREATE TABLE IF NOT EXISTS erp_document_shares (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id UUID NOT NULL,
    token_hash TEXT NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    channel TEXT NOT NULL DEFAULT 'link' CHECK (channel IN ('link','email')),
    recipient TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_opened_at TIMESTAMPTZ,
    open_count INTEGER NOT NULL DEFAULT 0 CHECK (open_count >= 0),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE CASCADE,
    CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS idx_erp_shares_document ON erp_document_shares(tenant_id, document_id);

-- Off by default (ENABLE_MYINVOIS_PREP). A prepared consolidation record only: never sent to LHDN.
CREATE TABLE IF NOT EXISTS myinvois_consolidated_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL CHECK (document_type IN ('invoice','credit-note','bill','purchase-credit-note','minipos-order')),
    date_from DATE NOT NULL,
    date_to DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared','cancelled')),
    idempotency_key TEXT NOT NULL,
    total_amount NUMERIC(18,2) NOT NULL DEFAULT 0,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (id, tenant_id),
    CHECK (date_from <= date_to)
);

CREATE TABLE IF NOT EXISTS myinvois_consolidated_documents (
    submission_id UUID NOT NULL,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id UUID NOT NULL,
    PRIMARY KEY (submission_id, document_id),
    UNIQUE (tenant_id, document_id),
    FOREIGN KEY (submission_id, tenant_id) REFERENCES myinvois_consolidated_submissions(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE RESTRICT
);

-- ---------------------------------------------------------------- ledger: typed documents post as their own source
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'journal_headers_source_type_check'
               AND conrelid = 'journal_headers'::regclass
               AND pg_get_constraintdef(oid) NOT LIKE '%erp_document%') THEN
        ALTER TABLE journal_headers DROP CONSTRAINT journal_headers_source_type_check;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'journal_headers_source_type_check'
                   AND conrelid = 'journal_headers'::regclass) THEN
        ALTER TABLE journal_headers ADD CONSTRAINT journal_headers_source_type_check
            CHECK (source_type IN ('document','adjustment','settlement','reversal','manual','erp_document'));
    END IF;
END $$;
