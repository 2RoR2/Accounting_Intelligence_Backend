-- Migration 0002: Pending Jobs Outbox Table & Fencing Constraints
-- Accounting Intelligence (Group 14) - Aleeya's Canonical Pending Jobs Schema
-- Enforces durable lease fencing, atomic dispatch, and tenant-scoped job lifecycle.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_documents_id_tenant'
    ) THEN
        ALTER TABLE documents ADD CONSTRAINT uq_documents_id_tenant UNIQUE (id, tenant_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS pending_jobs (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              UUID NOT NULL,
    document_id            UUID NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'dispatched', 'processing', 'succeeded', 'failed', 'cancelled')),

    -- attempts: attempt_count counts worker claims only. dispatch_count counts sends to Redis.
    attempt_count          INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts           INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    dispatch_count         INTEGER NOT NULL DEFAULT 0 CHECK (dispatch_count >= 0),
    next_attempt_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- dispatch lease: the job was handed to Redis and nobody has claimed it yet
    dispatch_token         UUID,
    dispatched_at          TIMESTAMPTZ,
    dispatch_expires_at    TIMESTAMPTZ,

    -- processing lease: a worker owns the job until processing_expires_at
    claim_token            UUID,
    claimed_at             TIMESTAMPTZ,
    heartbeat_at           TIMESTAMPTZ,
    processing_expires_at  TIMESTAMPTZ,

    -- outcome
    last_error             TEXT,
    finished_at            TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- the document must belong to the same company as the job
    CONSTRAINT fk_pending_jobs_document_tenant
        FOREIGN KEY (document_id, tenant_id) REFERENCES documents (id, tenant_id) ON DELETE RESTRICT,

    CONSTRAINT chk_pending_jobs_attempt_limit
        CHECK (attempt_count <= max_attempts),
    CONSTRAINT chk_pending_jobs_dispatched_lease
        CHECK (status <> 'dispatched' OR (dispatch_token IS NOT NULL AND dispatch_expires_at IS NOT NULL)),
    CONSTRAINT chk_pending_jobs_processing_lease
        CHECK (status <> 'processing' OR (claim_token IS NOT NULL AND processing_expires_at IS NOT NULL)),
    CONSTRAINT chk_pending_jobs_finished_timestamp
        CHECK (status NOT IN ('succeeded', 'failed', 'cancelled') OR finished_at IS NOT NULL)
);

-- Ensure additive columns exist if table was previously created with minimal schema
ALTER TABLE pending_jobs ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMPTZ;
ALTER TABLE pending_jobs ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE pending_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE pending_jobs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
ALTER TABLE pending_jobs ADD COLUMN IF NOT EXISTS last_error TEXT;

-- Connect raw_extractions pending_job_id if table exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_raw_extractions_pending_job'
    ) THEN
        ALTER TABLE raw_extractions
        ADD CONSTRAINT fk_raw_extractions_pending_job
        FOREIGN KEY (pending_job_id) REFERENCES pending_jobs(id) ON DELETE RESTRICT;
    END IF;
END $$;

-- A document can have only one open job at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_jobs_open_document
    ON pending_jobs (document_id) WHERE status IN ('pending', 'dispatched', 'processing');

-- Dispatcher looks for jobs that are due.
CREATE INDEX IF NOT EXISTS idx_pending_jobs_due
    ON pending_jobs (next_attempt_at) WHERE status = 'pending';

-- Recovery looks for leases that have run out.
CREATE INDEX IF NOT EXISTS idx_pending_jobs_dispatch_expiry
    ON pending_jobs (dispatch_expires_at) WHERE status = 'dispatched';
CREATE INDEX IF NOT EXISTS idx_pending_jobs_processing_expiry
    ON pending_jobs (processing_expires_at) WHERE status = 'processing';

-- Company job list index
CREATE INDEX IF NOT EXISTS idx_pending_jobs_tenant_status
    ON pending_jobs (tenant_id, status);
