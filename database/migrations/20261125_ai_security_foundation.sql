-- AI supplier memory is an approved, tenant-scoped hint. It is never trained
-- from raw model output and can be revoked without deleting its history.
CREATE TABLE IF NOT EXISTS ai_supplier_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    observed_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    vendor_id UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    approved_count INTEGER NOT NULL DEFAULT 1 CHECK (approved_count > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    source_document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    last_approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_ai_supplier_memory_vendor
    ON ai_supplier_memory (tenant_id, vendor_id, status);
