-- PROPOSED ONLY: Aleeya review required. Never auto-applied by the importer.
-- Existing documents/document_code, MFA, prediction and approval tables unchanged.
BEGIN;
CREATE TABLE IF NOT EXISTS benchmark_demo_tenants (
    tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by UUID NOT NULL REFERENCES users(id),
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS benchmark_demo_documents (
    tenant_id UUID NOT NULL REFERENCES benchmark_demo_tenants(tenant_id),
    dataset_id TEXT NOT NULL,
    benchmark_revision TEXT NOT NULL,
    demo_code TEXT NOT NULL CHECK (demo_code ~ '^(INV|BIL|REC)[0-9]{5}$'),
    sample_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    gold_manifest_sha256 TEXT NOT NULL CHECK (gold_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    document_id UUID NOT NULL,
    imported_by UUID NOT NULL REFERENCES users(id),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,dataset_id,benchmark_revision,sample_id),
    UNIQUE (tenant_id,dataset_id,benchmark_revision,demo_code),
    UNIQUE (tenant_id,dataset_id,benchmark_revision,source_sha256),
    UNIQUE (tenant_id,document_id),
    FOREIGN KEY (document_id,tenant_id) REFERENCES documents(id,tenant_id) ON DELETE RESTRICT
);
CREATE OR REPLACE FUNCTION benchmark_demo_registration_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Demo source registrations are immutable; use a new benchmark revision';
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS benchmark_demo_registration_immutable ON benchmark_demo_documents;
CREATE TRIGGER benchmark_demo_registration_immutable BEFORE UPDATE OR DELETE ON benchmark_demo_documents
FOR EACH ROW EXECUTE FUNCTION benchmark_demo_registration_immutable();
COMMIT;
