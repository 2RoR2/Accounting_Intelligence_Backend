-- Safe retries for write actions (services/routes/idempotency.py). Idempotent; the same text is appended to the
-- consolidated schema file.
--
-- A request that carries an Idempotency-Key header stores its first successful response here under
-- (company, user, key). A repeat with the same key and the same request hash gets that response back; the same key
-- with a different request is refused. Retention: rows are only needed while a client may still retry, so rows older
-- than 7 days are removed for a user on that user's next keyed request; they can also be deleted at any time.
-- They hold response bodies (document details), so they stay tenant-scoped and are removed with the company or user.

CREATE TABLE IF NOT EXISTS idempotency_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 160),
    route TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_created ON idempotency_keys (tenant_id, user_id, created_at);
