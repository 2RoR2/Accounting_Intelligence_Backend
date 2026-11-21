-- latest_update's request-time ERP tables (services/routes/erp/routes.py _ensure_erp_schema, origin/latest_update 5d7b521).
-- Used to test that migrate upgrades a database that served those requests.
CREATE TABLE IF NOT EXISTS erp_contacts (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('customer','supplier','employee')), legal_name TEXT NOT NULL,
        code TEXT, registration_number TEXT, email TEXT, phone TEXT, address TEXT, city TEXT, state TEXT, postcode TEXT,
        fields JSONB NOT NULL DEFAULT '{}'::jsonb, is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_by UUID REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, kind, legal_name), UNIQUE (id, tenant_id)
    );
CREATE TABLE IF NOT EXISTS erp_products (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        name TEXT NOT NULL, sku TEXT, kind TEXT NOT NULL CHECK (kind IN ('inventory','non-inventory','service','bundle')),
        unit TEXT NOT NULL DEFAULT 'unit', sale_price NUMERIC(18,4) NOT NULL DEFAULT 0, purchase_price NUMERIC(18,4) NOT NULL DEFAULT 0,
        quantity NUMERIC(18,3) NOT NULL DEFAULT 0, group_name TEXT, fields JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (id, tenant_id)
    );
CREATE TABLE IF NOT EXISTS erp_catalog_entries (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, code TEXT, name TEXT NOT NULL, description TEXT, data JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (id, tenant_id),
        UNIQUE (tenant_id, kind, name)
    );
CREATE TABLE IF NOT EXISTS erp_document_headers (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_type TEXT NOT NULL, document_number TEXT NOT NULL, contact_id UUID, contact_name TEXT,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','pending_approval','ready','void')),
        document_date DATE, due_date DATE, currency CHAR(3) NOT NULL DEFAULT 'MYR', reference_no TEXT, description TEXT,
        total_amount NUMERIC(18,2) NOT NULL DEFAULT 0, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by UUID REFERENCES users(id) ON DELETE SET NULL, updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), voided_at TIMESTAMPTZ,
        UNIQUE (tenant_id, document_type, document_number), UNIQUE (id, tenant_id)
    );
ALTER TABLE erp_document_headers ADD COLUMN IF NOT EXISTS contact_name TEXT;
CREATE TABLE IF NOT EXISTS erp_document_links (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        source_document_id UUID NOT NULL, target_document_id UUID NOT NULL, link_type TEXT NOT NULL,
        created_by UUID REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, source_document_id, target_document_id, link_type)
    );
CREATE TABLE IF NOT EXISTS erp_document_events (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_id UUID NOT NULL, event_type TEXT NOT NULL, from_status TEXT, to_status TEXT, reason TEXT,
        actor_id UUID REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
CREATE TABLE IF NOT EXISTS erp_document_shares (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_id UUID NOT NULL, document_type TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
        channel TEXT NOT NULL CHECK (channel IN ('link','email','whatsapp','sms')),
        recipient TEXT, form_design TEXT, subject TEXT, message TEXT,
        status TEXT NOT NULL DEFAULT 'ready', created_by UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_opened_at TIMESTAMPTZ
    );
CREATE TABLE IF NOT EXISTS myinvois_consolidated_submissions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_type TEXT NOT NULL, date_from DATE NOT NULL, date_to DATE NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','submitted','failed')),
        idempotency_key TEXT NOT NULL, total_amount NUMERIC(18,2) NOT NULL DEFAULT 0,
        created_by UUID REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, idempotency_key)
    );
CREATE TABLE IF NOT EXISTS myinvois_consolidated_documents (
        submission_id UUID NOT NULL REFERENCES myinvois_consolidated_submissions(id) ON DELETE CASCADE,
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_id UUID NOT NULL, document_type TEXT NOT NULL,
        PRIMARY KEY (submission_id, document_id), UNIQUE (tenant_id, document_id, document_type)
    );
CREATE TABLE IF NOT EXISTS erp_number_counters (
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, document_type TEXT NOT NULL, next_value INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (tenant_id, document_type)
    );
CREATE INDEX IF NOT EXISTS idx_erp_documents_list ON erp_document_headers(tenant_id, document_type, document_date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_erp_contacts_list ON erp_contacts(tenant_id, kind, legal_name);
CREATE INDEX IF NOT EXISTS idx_erp_products_list ON erp_products(tenant_id, name);
