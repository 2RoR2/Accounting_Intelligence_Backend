-- Plan P1: proof attached to typed records (payments, refunds, money in/out, bank transfers, manual journals).
-- Idempotent; the same text is appended to the consolidated schema file.
--
-- * A file lives in the private document storage (storage_path: s3:// uri or a local path); a link is an https:// address.
-- * media_type is decided from the file's own bytes at upload. Removing is a soft delete (deleted_at): the row and the
--   stored object stay for audit; every upload and removal also writes an audit_logs row.
CREATE TABLE IF NOT EXISTS record_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK (target_type IN ('erp_document')),
    target_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file','link')),
    storage_path TEXT,
    url TEXT,
    original_name TEXT NOT NULL CHECK (btrim(original_name) <> ''),
    media_type TEXT,
    size_bytes INTEGER,
    uploaded_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    deleted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (target_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE CASCADE,
    CHECK ((kind = 'file' AND storage_path IS NOT NULL AND url IS NULL
            AND media_type IS NOT NULL AND media_type IN ('application/pdf','image/png','image/jpeg','image/webp')
            AND size_bytes IS NOT NULL AND size_bytes BETWEEN 1 AND 10485760)
        OR (kind = 'link' AND url IS NOT NULL AND url ~ '^https://' AND length(url) <= 2000 AND storage_path IS NULL
            AND media_type IS NULL AND size_bytes IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_record_attachments_target ON record_attachments(tenant_id, target_type, target_id, created_at)
    WHERE deleted_at IS NULL;
