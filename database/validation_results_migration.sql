-- Safe migration for existing databases. Run once after deploying the
-- versioned-validation code; the full schema file also creates this table.
CREATE TABLE IF NOT EXISTS validation_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    tenant_id UUID NOT NULL,
    content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
    rule_id TEXT NOT NULL,
    rule_source TEXT NOT NULL DEFAULT 'SAIC',
    rule_version TEXT NOT NULL DEFAULT 'validation-v2',
    document_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed','failed','not_checked','warning')),
    severity TEXT NOT NULL CHECK (severity IN ('info','medium','high','blocking')),
    field_key TEXT,
    actual_value TEXT,
    expected_value TEXT,
    difference TEXT,
    prevents_approval BOOLEAN NOT NULL DEFAULT FALSE,
    requires_review BOOLEAN NOT NULL DEFAULT TRUE,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents (id, tenant_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_validation_results_revision
    ON validation_results(document_id, content_revision, created_at);
