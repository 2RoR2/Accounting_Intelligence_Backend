-- PostgreSQL Database schema
-- Accounting Intelligence (Group 14)
--
--  company access requests, seat limit
-- requests, the account lifecycle with forced password change, document kinds and readable
-- codes (INVOICE_100001, REC-10021, TENANT_001), exceptions, validation rules for each company,
-- a record of every document processing job, extraction runs with the state, evidence and
-- candidates of each extracted field, a history of every correction, locked standardised
-- records, and a general audit log.

-- Authentication and MFA tables are defined here so a clean schema build has
-- the same structure as an installation created through the migrations.
--
-- Run this ONE file to reset and rebuild everything from scratch.
-- Notes for the API layer are in comments next to the relevant columns.
--
-- This consolidated schema includes the review-integrity additions from
-- 002_review_integrity.sql and the credit/debit-note reconciliation additions
-- from 003_notes_and_reconciliation.sql. Those files remain additive upgrade
-- scripts for databases that were created before this consolidated version.

DROP TABLE IF EXISTS
    audit_logs, workspace_settings, validation_rules, standardised_records,
    vendors, exceptions, line_items, field_predictions, raw_extractions,
    field_corrections, pending_jobs, documents, auth_tokens, account_limit_requests, company_requests,
    mfa_recovery_requests, auth_challenges, password_challenges, auth_rate_limits, web_sessions,
    document_custom_values, custom_fields, customer_matches, customers, supplier_matches, document_status_history, workload_assignments, backup_runs,
    platform_notifications, user_permissions, role_permissions, permission_catalog,
    document_versions, export_history, review_reminders, notifications, saved_filters,
    document_tag_links, document_tags, supplier_contacts, document_comments,
    document_reconciliations, review_decisions, review_submissions, line_item_corrections, record_amendments,
    batch_documents, document_batches,
    users, tenants
CASCADE;
DROP SEQUENCE IF EXISTS document_invoice_seq, document_bill_seq, document_receipt_seq, document_credit_note_seq, document_debit_note_seq,
    tenant_code_seq, record_code_seq;
DROP FUNCTION IF EXISTS set_document_code() CASCADE;
DROP FUNCTION IF EXISTS set_tenant_code() CASCADE;
DROP FUNCTION IF EXISTS protect_field_predictions() CASCADE;
DROP FUNCTION IF EXISTS log_field_correction() CASCADE;
DROP FUNCTION IF EXISTS prevent_correction_change() CASCADE;
DROP FUNCTION IF EXISTS switch_current_extraction(UUID) CASCADE;
DROP FUNCTION IF EXISTS set_record_code() CASCADE;
DROP FUNCTION IF EXISTS prevent_record_update() CASCADE;

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- needed for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- indexed partial-match search

-- ---------------------------------------------------------------------------
-- Companies (tenants)
-- The frontend calls these "companies". accountLimit in the frontend is seat_limit here.
-- tenant_code is the readable ID the frontend shows as Tenant ID (TENANT_001). It is made by
-- a trigger, so the API does not need to generate it. The API sends it as the company id.
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_code          TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL UNIQUE,
    registration_number  TEXT NOT NULL UNIQUE,
    admin_email          TEXT NOT NULL,
    seat_limit           INTEGER NOT NULL DEFAULT 10 CHECK (seat_limit >= 1),
    status               TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE SEQUENCE tenant_code_seq START 1;

CREATE FUNCTION set_tenant_code() RETURNS trigger AS $$
DECLARE
    n BIGINT;
BEGIN
    IF NEW.tenant_code IS NULL OR NEW.tenant_code = '' THEN
        n := nextval('tenant_code_seq');
        NEW.tenant_code := 'TENANT_' || CASE WHEN n < 1000 THEN lpad(n::text, 3, '0') ELSE n::text END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_set_tenant_code
    BEFORE INSERT ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_tenant_code();

-- ---------------------------------------------------------------------------
-- Users
-- Seats used = users with status 'active' or 'invited' (pending invitations
-- reserve a seat). password_hash stays NULL until the account is activated.
-- The frontend role ACCOUNTANT is 'accountant' here.
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              UUID REFERENCES tenants(id) ON DELETE CASCADE,
    name                   TEXT NOT NULL,
    email                  TEXT NOT NULL,
    password_hash          TEXT,
    role                   TEXT NOT NULL
        CHECK (role IN ('super_admin', 'local_admin', 'accountant')),
    status                 TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('invited', 'active', 'disabled', 'invitation_expired')),
    must_change_password   BOOLEAN NOT NULL DEFAULT FALSE,
    avatar                 TEXT,
    invited_by             UUID REFERENCES users(id) ON DELETE SET NULL,
    invited_at             TIMESTAMPTZ,
    invitation_expires_at  TIMESTAMPTZ,
    last_login_at          TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (role = 'super_admin' AND tenant_id IS NULL)
        OR (role IN ('local_admin', 'accountant') AND tenant_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX uq_users_email ON users (lower(email));

-- ---------------------------------------------------------------------------
-- Company access requests (landing page "Request company access")
-- employee_emails is a JSON array of strings.
-- ---------------------------------------------------------------------------
CREATE TABLE company_requests (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name         TEXT NOT NULL,
    registration_number  TEXT NOT NULL,
    contact_name         TEXT NOT NULL,
    contact_email        TEXT NOT NULL,
    phone                TEXT,
    requested_accounts   INTEGER NOT NULL
        CHECK (requested_accounts BETWEEN 1 AND 1000),
    employee_emails      JSONB NOT NULL DEFAULT '[]'::jsonb,
    notes                TEXT,
    status               TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    rejection_reason     TEXT,
    tenant_id            UUID REFERENCES tenants(id) ON DELETE SET NULL,  -- set on approval
    reviewed_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    submitted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at          TIMESTAMPTZ
);
-- Only one pending request per contact email.
CREATE UNIQUE INDEX uq_company_requests_pending_email
    ON company_requests (lower(contact_email)) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- Seat limit increase requests (Local Admin asks, Super Admin reviews)
-- ---------------------------------------------------------------------------
CREATE TABLE account_limit_requests (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    current_limit    INTEGER NOT NULL,
    requested_limit  INTEGER NOT NULL,
    reason           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    review_reason    TEXT,
    reviewed_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at      TIMESTAMPTZ,
    CHECK (requested_limit > current_limit)
);
-- Only one pending request per company.
CREATE UNIQUE INDEX uq_limit_requests_pending
    ON account_limit_requests (tenant_id) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- One time tokens: account activation, email login code, password reset.
-- Store a hash of the token, never the token itself.
-- ---------------------------------------------------------------------------
CREATE TABLE auth_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose     TEXT NOT NULL
        CHECK (purpose IN ('activation', 'login_otp', 'password_reset')),
    token_hash  TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Documents
-- document_code is the human readable ID shown in the UI: INVOICE_100001,
-- BILL_100001, RECEIPT_100001. Numbers are assigned per kind by the sequences
-- below, through a trigger, so the API does not need to generate them.
-- Statuses follow the frontend DocumentStatus values (lowercase here).
-- ---------------------------------------------------------------------------
CREATE SEQUENCE document_invoice_seq START 100001;
CREATE SEQUENCE document_bill_seq START 100001;
CREATE SEQUENCE document_receipt_seq START 100001;
CREATE SEQUENCE document_credit_note_seq START 100001;
CREATE SEQUENCE document_debit_note_seq START 100001;

CREATE TABLE documents (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_code             TEXT NOT NULL UNIQUE,
    tenant_id                 UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    owner_id                  UUID NOT NULL REFERENCES users(id),
    assigned_to               UUID REFERENCES users(id) ON DELETE SET NULL,
    original_filename         TEXT NOT NULL,
    kind                      TEXT NOT NULL
        CHECK (kind IN ('invoice', 'bill', 'receipt', 'credit_note', 'debit_note')),
    category                  TEXT NOT NULL
        CHECK (category IN ('accounts_payable', 'accounts_receivable')),
    storage_path              TEXT NOT NULL,
    mime_type                 TEXT NOT NULL,
    file_size_bytes           BIGINT NOT NULL
        CHECK (file_size_bytes > 0 AND file_size_bytes <= 20971520),  -- 20 MB limit, as in the frontend
    file_hash_sha256          TEXT NOT NULL,  -- used for the exact duplicate file check
    page_count                INTEGER,
    document_quality_score    NUMERIC(4,3)
        CHECK (document_quality_score IS NULL
               OR (document_quality_score >= 0 AND document_quality_score <= 1)),
    source_channel            TEXT NOT NULL DEFAULT 'upload'
        CHECK (source_channel IN ('upload', 'camera')),
    status                    TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (status IN (
            'uploaded', 'queued', 'processing', 'extracted', 'validating',
            'exception', 'in_review', 'corrected', 'validated',
            'standardised', 'completed', 'failed'
        )),
    processing_seconds        NUMERIC(6,2),
    uploaded_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    processing_started_at     TIMESTAMPTZ,
    processing_completed_at   TIMESTAMPTZ,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_revision          INTEGER NOT NULL DEFAULT 1 CHECK (content_revision >= 1),
    UNIQUE (id, tenant_id)   -- lets pending_jobs refer to a document and its company together
);

CREATE FUNCTION set_document_code() RETURNS trigger AS $$
BEGIN
    IF NEW.document_code IS NULL OR NEW.document_code = '' THEN
        NEW.document_code := upper(NEW.kind) || '_' ||
            CASE NEW.kind
                WHEN 'invoice' THEN nextval('document_invoice_seq')::text
                WHEN 'bill'    THEN nextval('document_bill_seq')::text
                WHEN 'receipt' THEN nextval('document_receipt_seq')::text
                WHEN 'credit_note' THEN nextval('document_credit_note_seq')::text
                ELSE nextval('document_debit_note_seq')::text
            END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- document_code is NOT NULL, so the trigger fills it before the constraint is checked.
CREATE TRIGGER trg_set_document_code
    BEFORE INSERT ON documents
    FOR EACH ROW EXECUTE FUNCTION set_document_code();

-- Optional relationships between related documents, such as an invoice and its
-- credit/debit note. A relationship is informational and does not replace either document.
CREATE TABLE document_reconciliations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_document_id UUID NOT NULL,
    related_document_id UUID NOT NULL,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('credit_for','debit_for','adjustment_of','related_to')),
    amount NUMERIC(14,2),
    note TEXT NOT NULL DEFAULT '',
    created_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source_document_id <> related_document_id),
    UNIQUE (tenant_id, source_document_id, related_document_id, relation_type),
    FOREIGN KEY (source_document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (related_document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Pending jobs: the database keeps the written record of every document processing job.
-- Redis only carries a message saying "try job X". This table decides who may run it.
-- The Redis job id is dispatch_token, a fresh value for every dispatch, never reused.
-- attempt_count goes up only when a worker claims the job. dispatch_count goes up on every send to Redis.
-- A document with job history cannot be hard deleted (ON DELETE RESTRICT).
-- The statements the dispatcher, workers and recovery are meant to run, and the rollback steps,
-- are in pending_jobs_usage.sql.
-- ---------------------------------------------------------------------------
CREATE TABLE pending_jobs (
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
    dispatch_token         UUID,           -- also the Redis job id for this dispatch
    dispatched_at          TIMESTAMPTZ,
    dispatch_expires_at    TIMESTAMPTZ,

    -- processing lease: a worker owns the job until processing_expires_at
    claim_token            UUID,           -- the worker ownership token, checked on every guarded write
    claimed_at             TIMESTAMPTZ,
    heartbeat_at           TIMESTAMPTZ,
    processing_expires_at  TIMESTAMPTZ,

    -- outcome
    last_error             TEXT,
    finished_at            TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),   -- set by the code that changes the row

    -- the document must belong to the same company as the job
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents (id, tenant_id) ON DELETE RESTRICT,

    CHECK (attempt_count <= max_attempts),
    CHECK (status <> 'dispatched' OR (dispatch_token IS NOT NULL AND dispatch_expires_at IS NOT NULL)),
    CHECK (status <> 'processing' OR (claim_token IS NOT NULL AND processing_expires_at IS NOT NULL)),
    CHECK (status NOT IN ('succeeded', 'failed', 'cancelled') OR finished_at IS NOT NULL)
);

-- A document can have only one open job at a time. A finished job does not block a later reprocess.
CREATE UNIQUE INDEX uq_pending_jobs_open_document
    ON pending_jobs (document_id) WHERE status IN ('pending', 'dispatched', 'processing');

-- The dispatcher looks for jobs that are due.
CREATE INDEX idx_pending_jobs_due
    ON pending_jobs (next_attempt_at) WHERE status = 'pending';
-- Recovery looks for leases that have run out.
CREATE INDEX idx_pending_jobs_dispatch_expiry
    ON pending_jobs (dispatch_expires_at) WHERE status = 'dispatched';
CREATE INDEX idx_pending_jobs_processing_expiry
    ON pending_jobs (processing_expires_at) WHERE status = 'processing';
-- Lists for one company.
CREATE INDEX idx_pending_jobs_tenant_status
    ON pending_jobs (tenant_id, status);

-- ---------------------------------------------------------------------------
-- One row for each extraction run of a document. A rerun adds a row and never changes an old one.
-- raw_json holds the complete M2 public result exactly as published, including the original method
-- names, the inference attempt status and the model information.
-- routing_decision is the route M2 chose. It does not prove that a model ran. For example, a
-- vlm_candidate route can end as human_review when inference is disabled.
-- is_current marks the run the screens show. Never set it by hand: call switch_current_extraction(run_id),
-- which moves is_current and carries the accountant's corrections forward in ONE transaction.
-- service_run_id is unique, so saving the same M2 run twice cannot create a duplicate.
-- ---------------------------------------------------------------------------
CREATE TABLE raw_extractions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id       UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    run_number        INTEGER NOT NULL CHECK (run_number >= 1),
    is_current        BOOLEAN NOT NULL DEFAULT FALSE,
    routing_decision  TEXT NOT NULL
        CHECK (routing_decision IN ('parser_only', 'vlm_candidate', 'human_review')),
    service_run_id    TEXT UNIQUE,
    schema_version    TEXT,
    pending_job_id    UUID REFERENCES pending_jobs(id) ON DELETE RESTRICT,
    raw_json          JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, run_number),
    UNIQUE (id, document_id)
);

-- At most one current run for each document.
CREATE UNIQUE INDEX uq_raw_extractions_current ON raw_extractions (document_id) WHERE is_current;


-- ---------------------------------------------------------------------------
-- Per field extraction result, for one extraction run. Rows are never overwritten by a rerun.
-- field_key matches the frontend keys: supplier, invoice, date, due, reference, customer, currency,
-- subtotal, tax, discount, total, and so on. The M2 names map to them in the adapter:
--   supplier_name > supplier, invoice_number > invoice, invoice_date > date, currency > currency,
--   subtotal > subtotal, tax_amount > tax, total_amount > total.
-- Three values are kept separately, as the Handoff Pack asks:
--   raw_value        = the text exactly as the extractor read it (M2 raw_text)
--   normalized_value = the cleaned value from the extractor (M2 value, frontend field: original)
--   current_value    = the value now in use, after any human correction (frontend field: value)
-- extraction_state is the M2 state of the field. confidence is NULL when the extractor gives no
-- numeric score. NULL is not zero, and no artificial score is ever generated.
-- resolution is the M2 reconciliation outcome. An AI candidate never replaces the parser value:
-- while resolution is candidate_for_review, current_value stays NULL until a person corrects it.
-- evidence is a JSON array that keeps the full M2 evidence objects (page, bbox, bbox_original,
-- page_size, source_ref, coordinate_units, text). The API converts bbox to x, y, width, height for
-- the frontend if it needs them.
-- candidates is a JSON array of candidates, each marked with source parser or ai and auto_accepted
-- false. Extracted values cannot be edited after saving: only current_value, corrected_by and
-- corrected_at can change, and every change to current_value is written to field_corrections.
-- confidence, when present, is 0 to 1. The API multiplies by 100 for the frontend.
-- ---------------------------------------------------------------------------
CREATE TABLE field_predictions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_id      UUID NOT NULL,
    document_id        UUID NOT NULL,
    field_key          TEXT NOT NULL,
    field_label        TEXT NOT NULL,
    extraction_state   TEXT NOT NULL
        CHECK (extraction_state IN ('resolved', 'missing', 'ambiguous', 'conflict', 'unsupported', 'unreadable')),
    resolution         TEXT
        CHECK (resolution IN ('parser_preserved', 'candidate_for_review', 'unresolved_missing')),
    raw_value          TEXT,
    normalized_value   TEXT,
    current_value      TEXT,
    confidence         NUMERIC(4,3)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    evidence           JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence) = 'array'),
    candidates         JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(candidates) = 'array'),
    extraction_method  TEXT NOT NULL DEFAULT 'PARSER'
        CHECK (extraction_method IN ('VLM', 'OCR', 'PARSER', 'MANUAL_CORRECTION')),
    model_name         TEXT,
    model_version      TEXT,
    schema_version     TEXT NOT NULL DEFAULT '1.0',
    corrected_by       UUID REFERENCES users(id) ON DELETE SET NULL,
    corrected_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (extraction_id, document_id) REFERENCES raw_extractions (id, document_id) ON DELETE RESTRICT,
    UNIQUE (extraction_id, field_key),
    CHECK (resolution IS DISTINCT FROM 'candidate_for_review' OR current_value IS NULL OR corrected_at IS NOT NULL)
);

CREATE FUNCTION protect_field_predictions() RETURNS trigger AS $$
BEGIN
    IF NEW.extraction_id IS DISTINCT FROM OLD.extraction_id
       OR NEW.document_id IS DISTINCT FROM OLD.document_id
       OR NEW.field_key IS DISTINCT FROM OLD.field_key
       OR NEW.field_label IS DISTINCT FROM OLD.field_label
       OR NEW.extraction_state IS DISTINCT FROM OLD.extraction_state
       OR NEW.resolution IS DISTINCT FROM OLD.resolution
       OR NEW.raw_value IS DISTINCT FROM OLD.raw_value
       OR NEW.normalized_value IS DISTINCT FROM OLD.normalized_value
       OR NEW.confidence IS DISTINCT FROM OLD.confidence
       OR NEW.evidence IS DISTINCT FROM OLD.evidence
       OR NEW.candidates IS DISTINCT FROM OLD.candidates
       OR NEW.extraction_method IS DISTINCT FROM OLD.extraction_method
       OR NEW.model_name IS DISTINCT FROM OLD.model_name
       OR NEW.model_version IS DISTINCT FROM OLD.model_version
       OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'Extracted values are read only. Only current_value, corrected_by and corrected_at can change.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_field_predictions_protect
    BEFORE UPDATE ON field_predictions
    FOR EACH ROW EXECUTE FUNCTION protect_field_predictions();

-- ---------------------------------------------------------------------------
-- Correction history. One row for every change to a current_value, written by a trigger so the
-- API cannot forget it. Rows can never be changed or deleted.
-- ---------------------------------------------------------------------------
CREATE TABLE field_corrections (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    field_prediction_id  UUID NOT NULL REFERENCES field_predictions(id) ON DELETE RESTRICT,
    old_value            TEXT,
    new_value            TEXT,
    corrected_by         UUID REFERENCES users(id),
    corrected_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION log_field_correction() RETURNS trigger AS $$
BEGIN
    IF NEW.current_value IS DISTINCT FROM OLD.current_value THEN
        INSERT INTO field_corrections (field_prediction_id, old_value, new_value, corrected_by, corrected_at)
        VALUES (NEW.id, OLD.current_value, NEW.current_value, NEW.corrected_by, COALESCE(NEW.corrected_at, clock_timestamp()));
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_field_predictions_log
    AFTER UPDATE ON field_predictions
    FOR EACH ROW EXECUTE FUNCTION log_field_correction();

CREATE FUNCTION prevent_correction_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'The correction history cannot be changed or deleted.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_field_corrections_append_only
    BEFORE UPDATE OR DELETE ON field_corrections
    FOR EACH ROW EXECUTE FUNCTION prevent_correction_change();


-- ---------------------------------------------------------------------------
-- Rerun policy, in one place. Call this once after saving all the rows of a rerun:
--   SELECT switch_current_extraction(:new_run_id);
-- In ONE transaction it makes the rerun the current run and carries every accountant correction from the
-- previous current run into the rerun, so a rerun never replaces a corrected value. The fresh prediction stays
-- visible in normalized_value. Every expected field must have a row in every run (state missing when the
-- extractor does not return it), so a correction always has a row to be carried to. The carry forward is
-- written to field_corrections by the trigger, with the original correction time. It returns how many
-- corrected values were carried forward. The document row is locked, so two reruns cannot interleave.
-- ---------------------------------------------------------------------------
CREATE FUNCTION switch_current_extraction(new_extraction UUID) RETURNS INTEGER AS $$
DECLARE
    doc            UUID;
    old_extraction UUID;
    carried        INTEGER := 0;
BEGIN
    SELECT document_id INTO doc FROM raw_extractions WHERE id = new_extraction;
    IF doc IS NULL THEN
        RAISE EXCEPTION 'Unknown extraction run.';
    END IF;
    PERFORM 1 FROM documents WHERE id = doc FOR UPDATE;
    SELECT id INTO old_extraction FROM raw_extractions WHERE document_id = doc AND is_current;
    IF old_extraction IS NOT NULL AND old_extraction <> new_extraction THEN
        UPDATE raw_extractions SET is_current = FALSE WHERE id = old_extraction;
    END IF;
    UPDATE raw_extractions SET is_current = TRUE WHERE id = new_extraction;
    IF old_extraction IS NOT NULL AND old_extraction <> new_extraction THEN
        UPDATE field_predictions np
        SET current_value = op.current_value, corrected_by = op.corrected_by, corrected_at = op.corrected_at
        FROM field_predictions op
        WHERE np.extraction_id = new_extraction
          AND op.extraction_id = old_extraction
          AND op.field_key = np.field_key
          AND op.corrected_at IS NOT NULL;
        GET DIAGNOSTICS carried = ROW_COUNT;
    END IF;
    RETURN carried;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Line items belong to an extraction run, so a rerun adds its own rows and keeps the old ones.
-- The frontend shows them and does not edit them.
-- A cell the extractor could not resolve is saved as NULL, so one unclear cell never stops the whole
-- save. field_states, evidence and candidates are JSON objects keyed by cell name (description,
-- quantity, unit_price, amount) and keep the M2 state, evidence and candidates of each cell.
-- ---------------------------------------------------------------------------
CREATE TABLE line_items (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_id  UUID NOT NULL,
    document_id    UUID NOT NULL,
    line_number    INTEGER NOT NULL,
    description    TEXT,
    quantity       NUMERIC(14,3),
    unit_price     NUMERIC(14,4),
    amount         NUMERIC(14,2),
    field_states   JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(field_states) = 'object'),
    evidence       JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
    candidates     JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(candidates) = 'object'),
    FOREIGN KEY (extraction_id, document_id) REFERENCES raw_extractions (id, document_id) ON DELETE RESTRICT,
    UNIQUE (extraction_id, line_number)
);

CREATE TABLE line_item_corrections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    line_item_id UUID NOT NULL REFERENCES line_items(id) ON DELETE RESTRICT,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    field_key TEXT NOT NULL CHECK (field_key IN ('description','quantity','unit_price','amount')),
    old_value TEXT,
    new_value TEXT,
    corrected_by UUID NOT NULL REFERENCES users(id),
    corrected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (line_item_id, field_key, corrected_at)
);

-- ---------------------------------------------------------------------------
-- Exceptions raised when validation fails or flags a warning.
-- type is not restricted, because the exception list is still growing.
-- The frontend currently uses TOTAL_MISMATCH, MISSING_INVOICE,
-- DUPLICATE_INVOICE, and LOW_CONFIDENCE.
-- note holds the correction note entered on "Save and revalidate".
-- ---------------------------------------------------------------------------
CREATE TABLE exceptions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    type         TEXT NOT NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    assigned_to  UUID REFERENCES users(id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'assigned', 'in_review', 'resolved')),
    message      TEXT NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- Suppliers, one set per company (optional resolved entity for vendor matching).
-- ---------------------------------------------------------------------------
CREATE TABLE vendors (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendor_name          TEXT NOT NULL,
    registration_number  TEXT,
    address              TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, vendor_name)
);
CREATE UNIQUE INDEX uq_vendors_id_tenant ON vendors(id, tenant_id);

CREATE TABLE customers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_name       TEXT NOT NULL,
    registration_number TEXT,
    address             TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, customer_name)
);
CREATE UNIQUE INDEX uq_customers_id_tenant ON customers(id, tenant_id);

CREATE TABLE customer_matches (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id    UUID NOT NULL,
    customer_id    UUID,
    candidate_name TEXT NOT NULL,
    confidence     NUMERIC(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    source         TEXT NOT NULL CHECK (source IN ('ai','rule','manual')),
    decision       TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','accepted','rejected')),
    decided_by     UUID REFERENCES users(id) ON DELETE SET NULL,
    decided_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (customer_id, tenant_id) REFERENCES customers(id, tenant_id) ON DELETE RESTRICT
);

-- ---------------------------------------------------------------------------
-- Standardised records: one per document, immutable once created.
-- record_code is the readable ID the frontend shows as Record ID and writes in the first
-- column of the CSV export (REC-10021). It is made by a trigger. The API sends it as the id.
-- invoice_date and the amounts can be empty, because a company can switch the date and total
-- checks off in its validation rules.
-- There is no unique constraint on supplier and invoice number: duplicate checking is a
-- setting for each company, so it is done by validation and not forced by the database.
-- The columns are the fields used for lists, search, duplicate checks and CSV
-- export (Record ID, Category, Invoice, Supplier, Invoice date, Subtotal, Tax,
-- Discount, Total, Currency, Validation). snapshot holds the whole canonical record
-- (see canonical_accounting_record_schema_v2.json), exactly as it was when the record was created.
-- ---------------------------------------------------------------------------
CREATE TABLE standardised_records (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    record_code        TEXT NOT NULL UNIQUE,
    document_id        UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision_number    INTEGER NOT NULL DEFAULT 1 CHECK (revision_number >= 1),
    supersedes_record_id UUID REFERENCES standardised_records(id) ON DELETE RESTRICT,
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendor_id          UUID REFERENCES vendors(id),
    vendor_match_confidence NUMERIC(4,3)
        CHECK (vendor_match_confidence IS NULL
               OR (vendor_match_confidence >= 0 AND vendor_match_confidence <= 1)),
    category           TEXT NOT NULL
        CHECK (category IN ('accounts_payable', 'accounts_receivable')),
    supplier_name      TEXT,
    buyer_name         TEXT,
    invoice_number     TEXT,
    reference_number   TEXT,
    invoice_date       DATE,
    due_date           DATE,
    currency           CHAR(3) NOT NULL DEFAULT 'MYR',
    subtotal           NUMERIC(14,2),
    discount           NUMERIC(14,2) DEFAULT 0,
    tax_amount         NUMERIC(14,2),
    total_amount       NUMERIC(14,2),
    validation_status  TEXT NOT NULL DEFAULT 'passed'
        CHECK (validation_status IN ('passed')),
    validated_at       TIMESTAMPTZ,
    snapshot           JSONB NOT NULL,
    created_by         UUID REFERENCES users(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    ,UNIQUE (document_id, revision_number)
);

CREATE TABLE record_amendments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    original_record_id UUID NOT NULL REFERENCES standardised_records(id) ON DELETE RESTRICT,
    replacement_record_id UUID UNIQUE REFERENCES standardised_records(id) ON DELETE RESTRICT,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    requested_by UUID NOT NULL REFERENCES users(id),
    approved_by UUID REFERENCES users(id),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','approved','rejected','completed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE custom_fields (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    label TEXT NOT NULL,
    field_type TEXT NOT NULL CHECK (field_type IN ('text','number','date','boolean','select')),
    options JSONB NOT NULL DEFAULT '[]'::jsonb,
    required BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, field_key)
);

CREATE TABLE document_custom_values (
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    field_id UUID NOT NULL REFERENCES custom_fields(id) ON DELETE CASCADE,
    value JSONB NOT NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, field_id)
);

CREATE SEQUENCE record_code_seq START 10001;

CREATE FUNCTION set_record_code() RETURNS trigger AS $$
BEGIN
    IF NEW.record_code IS NULL OR NEW.record_code = '' THEN
        NEW.record_code := 'REC-' || nextval('record_code_seq')::text;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_set_record_code
    BEFORE INSERT ON standardised_records
    FOR EACH ROW EXECUTE FUNCTION set_record_code();

CREATE FUNCTION prevent_record_update() RETURNS trigger AS $$
BEGIN
    IF NEW.is_current IS DISTINCT FROM OLD.is_current
       AND (to_jsonb(NEW) - 'is_current') = (to_jsonb(OLD) - 'is_current') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Standardised records are immutable.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_records_immutable
    BEFORE UPDATE ON standardised_records
    FOR EACH ROW EXECUTE FUNCTION prevent_record_update();

-- ---------------------------------------------------------------------------
-- Validation rules, one row per company (created with defaults on approval).
-- confidence_threshold is stored as 0 to 1 (frontend shows it as a percentage).
-- ---------------------------------------------------------------------------
CREATE TABLE validation_rules (
    tenant_id               UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    require_supplier        BOOLEAN NOT NULL DEFAULT TRUE,
    require_invoice_number  BOOLEAN NOT NULL DEFAULT TRUE,
    validate_date           BOOLEAN NOT NULL DEFAULT TRUE,
    validate_total          BOOLEAN NOT NULL DEFAULT TRUE,
    check_duplicate         BOOLEAN NOT NULL DEFAULT TRUE,
    require_invoice_number_for_invoice BOOLEAN NOT NULL DEFAULT TRUE,
    require_reference_for_bill        BOOLEAN NOT NULL DEFAULT TRUE,
    require_receipt_identifier        BOOLEAN NOT NULL DEFAULT FALSE,
    confidence_threshold    NUMERIC(3,2) NOT NULL DEFAULT 0.80
        CHECK (confidence_threshold >= 0 AND confidence_threshold <= 1),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Workspace preferences. tenant_id NULL = platform settings for SAIC.
-- ---------------------------------------------------------------------------
CREATE TABLE workspace_settings (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID REFERENCES tenants(id) ON DELETE CASCADE,
    timezone       TEXT NOT NULL DEFAULT 'Asia/Kuala_Lumpur',
    notifications  TEXT NOT NULL DEFAULT 'enabled',
    session_timeout_minutes INTEGER NOT NULL DEFAULT 30 CHECK (session_timeout_minutes IN (15, 30, 60)),
    default_document_view TEXT NOT NULL DEFAULT 'all' CHECK (default_document_view IN ('all', 'exceptions', 'review')),
    email_assignment_notifications BOOLEAN NOT NULL DEFAULT TRUE,
    processing_status_notifications BOOLEAN NOT NULL DEFAULT TRUE,
    upload_max_size_mb INTEGER NOT NULL DEFAULT 20 CHECK (upload_max_size_mb BETWEEN 1 AND 20),
    allow_camera_uploads BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_workspace_settings_scope
    ON workspace_settings ((COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)));

-- ---------------------------------------------------------------------------
-- General audit log. Covers uploads, corrections, approvals, invitations and
-- Support View access (action SUPPORT_VIEW_ACCESSED, resource = record, details
-- = the reason given). resource holds the readable code of what was acted on (document code,
-- record code, or company name). user_name is kept as a snapshot. user_id NULL = system.
-- The API should hide extracted financial values from SAIC in audit details.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID REFERENCES tenants(id) ON DELETE SET NULL,
    user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
    user_name   TEXT NOT NULL,
    role        TEXT NOT NULL
        CHECK (role IN ('super_admin', 'local_admin', 'accountant', 'system')),
    action      TEXT NOT NULL,
    resource    TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Indexes for the lookups the app does most often
-- ---------------------------------------------------------------------------
CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_auth_tokens_user ON auth_tokens(user_id, purpose);
CREATE INDEX idx_documents_tenant_status ON documents(tenant_id, status);
CREATE INDEX idx_documents_filename_search ON documents USING gin (lower(original_filename) gin_trgm_ops);
CREATE INDEX idx_documents_owner ON documents(owner_id);
CREATE INDEX idx_documents_file_hash ON documents(tenant_id, file_hash_sha256);
CREATE INDEX idx_documents_assigned ON documents(assigned_to);
CREATE INDEX idx_raw_extractions_document ON raw_extractions(document_id);
CREATE INDEX idx_field_predictions_document ON field_predictions(document_id);
CREATE INDEX idx_field_predictions_extraction ON field_predictions(extraction_id);
CREATE INDEX idx_field_corrections_prediction ON field_corrections(field_prediction_id);
-- Duplicate invoice check: find other documents with the same invoice or supplier value.
CREATE INDEX idx_field_predictions_key_value ON field_predictions(field_key, current_value);
CREATE INDEX idx_field_predictions_value_search ON field_predictions USING gin (lower(coalesce(current_value, normalized_value, raw_value, '')) gin_trgm_ops);
CREATE INDEX idx_line_items_document ON line_items(document_id);
CREATE INDEX idx_exceptions_document ON exceptions(document_id);
CREATE INDEX idx_exceptions_tenant_status ON exceptions(tenant_id, status);
CREATE INDEX idx_records_tenant_created ON standardised_records(tenant_id, created_at);
CREATE INDEX idx_records_duplicate_lookup ON standardised_records(tenant_id, supplier_name, invoice_number);
CREATE INDEX idx_records_amount ON standardised_records(tenant_id, total_amount);
CREATE INDEX idx_records_business_search ON standardised_records USING gin (lower(coalesce(supplier_name, '') || ' ' || coalesce(buyer_name, '') || ' ' || coalesce(invoice_number, '') || ' ' || coalesce(reference_number, '')) gin_trgm_ops);
CREATE INDEX idx_audit_tenant_created ON audit_logs(tenant_id, created_at);

-- ---------------------------------------------------------------------------
-- Authentication, MFA, and recovery
-- ---------------------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret_enc TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    session_id UUID NOT NULL DEFAULT gen_random_uuid()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_web_sessions_session_id ON web_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions(expires_at);

CREATE TABLE IF NOT EXISTS password_challenges (
    email TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0,
    grant_hash TEXT,
    grant_expires_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    key TEXT PRIMARY KEY,
    window_start TIMESTAMPTZ NOT NULL DEFAULT now(),
    hits INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS auth_challenges (
    challenge_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pending_totp_secret_enc TEXT,
    email_code_hash TEXT,
    method TEXT NOT NULL DEFAULT 'totp',
    expires_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mfa_recovery_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requester_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by UUID REFERENCES users(id),
    UNIQUE (target_user_id, status)
);

-- ---------------------------------------------------------------------------
-- Accountant workflow extensions
-- These tables support digital shoebox batches, formal review history,
-- collaboration, supplier contacts, organisation, reminders, exports and
-- document version traceability. They are intentionally append/history-aware.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS document_batches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_by      UUID NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'queued', 'processing', 'review', 'completed', 'failed', 'cancelled')),
    total_documents INTEGER NOT NULL DEFAULT 0 CHECK (total_documents >= 0),
    processed_documents INTEGER NOT NULL DEFAULT 0 CHECK (processed_documents >= 0),
    failed_documents INTEGER NOT NULL DEFAULT 0 CHECK (failed_documents >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    CHECK (processed_documents + failed_documents <= total_documents)
);

CREATE TABLE IF NOT EXISTS batch_documents (
    batch_id     UUID NOT NULL REFERENCES document_batches(id) ON DELETE CASCADE,
    document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL CHECK (position >= 1),
    status       TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'review', 'completed', 'failed')),
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, document_id),
    UNIQUE (batch_id, position)
);

CREATE TABLE IF NOT EXISTS review_submissions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id    UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    submitted_by   UUID NOT NULL REFERENCES users(id),
    decision       TEXT NOT NULL CHECK (decision IN ('submitted', 'approved', 'rejected', 'returned')),
    note           TEXT NOT NULL DEFAULT '',
    content_revision INTEGER NOT NULL DEFAULT 1 CHECK (content_revision >= 1),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE review_submissions ADD COLUMN IF NOT EXISTS content_revision INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS review_decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id   UUID NOT NULL REFERENCES review_submissions(id) ON DELETE RESTRICT,
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    reviewed_by     UUID NOT NULL REFERENCES users(id),
    decision        TEXT NOT NULL CHECK (decision IN ('approved','rejected','returned')),
    note            TEXT NOT NULL DEFAULT '',
    reviewed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE review_submissions ADD CONSTRAINT uq_review_submission_document_tenant
    UNIQUE (id, document_id, tenant_id);
ALTER TABLE review_submissions ADD CONSTRAINT fk_review_submissions_document_tenant
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE;
ALTER TABLE review_decisions ADD COLUMN approved_content_revision INTEGER;
ALTER TABLE review_decisions ADD CONSTRAINT fk_review_decisions_document_tenant
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE;
ALTER TABLE review_decisions ADD CONSTRAINT fk_review_decisions_submission_consistency
    FOREIGN KEY (submission_id, document_id, tenant_id)
    REFERENCES review_submissions(id, document_id, tenant_id) ON DELETE RESTRICT;

CREATE TABLE IF NOT EXISTS document_comments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    author_id     UUID NOT NULL REFERENCES users(id),
    comment       TEXT NOT NULL CHECK (length(trim(comment)) > 0),
    page_number   INTEGER CHECK (page_number IS NULL OR page_number >= 1),
    field_key     TEXT,
    resolved_at   TIMESTAMPTZ,
    resolved_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS supplier_contacts (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendor_id          UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    contact_name       TEXT NOT NULL,
    email              TEXT,
    phone              TEXT,
    role               TEXT,
    notes              TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_tags (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#64748b',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS document_tag_links (
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_id      UUID NOT NULL REFERENCES document_tags(id) ON DELETE CASCADE,
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, tag_id)
);

CREATE TABLE IF NOT EXISTS saved_filters (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_by  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    resource    TEXT NOT NULL CHECK (resource IN ('documents', 'exceptions', 'records')),
    definition  JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(definition) = 'object'),
    is_shared   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID REFERENCES tenants(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type         TEXT NOT NULL,
    title        TEXT NOT NULL,
    message      TEXT NOT NULL,
    resource     TEXT,
    read_at      TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_reminders (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    assigned_to   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    remind_at     TIMESTAMPTZ NOT NULL,
    sent_at       TIMESTAMPTZ,
    created_by    UUID NOT NULL REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS export_history (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    requested_by  UUID NOT NULL REFERENCES users(id),
    resource      TEXT NOT NULL CHECK (resource IN ('records', 'documents', 'exceptions')),
    filters       JSONB NOT NULL DEFAULT '{}'::jsonb,
    format        TEXT NOT NULL DEFAULT 'csv' CHECK (format IN ('csv', 'json', 'xlsx')),
    row_count     INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    status        TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('queued', 'completed', 'failed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS document_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    version_number  INTEGER NOT NULL CHECK (version_number >= 1),
    storage_path    TEXT NOT NULL,
    file_hash_sha256 TEXT NOT NULL,
    file_size_bytes BIGINT NOT NULL CHECK (file_size_bytes > 0),
    created_by      UUID NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, version_number),
    UNIQUE (document_id, file_hash_sha256)
);

CREATE INDEX IF NOT EXISTS idx_batches_tenant_status ON document_batches(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_batch_documents_document ON batch_documents(document_id);
CREATE INDEX IF NOT EXISTS idx_review_submissions_document ON review_submissions(document_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_document ON document_comments(document_id, created_at);
CREATE INDEX IF NOT EXISTS idx_supplier_contacts_vendor ON supplier_contacts(vendor_id);
CREATE INDEX IF NOT EXISTS idx_tag_links_document ON document_tag_links(document_id);
CREATE INDEX IF NOT EXISTS idx_saved_filters_user ON saved_filters(created_by, resource);
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, read_at, created_at);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON review_reminders(remind_at) WHERE sent_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_export_history_tenant ON export_history(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_document_versions_document ON document_versions(document_id, version_number);

-- ---------------------------------------------------------------------------
-- Integrity constraints and derived workflow state
-- ---------------------------------------------------------------------------
ALTER TABLE users ADD CONSTRAINT uq_users_id_tenant UNIQUE (id, tenant_id);
ALTER TABLE document_batches ADD CONSTRAINT uq_document_batches_id_tenant UNIQUE (id, tenant_id);
ALTER TABLE batch_documents ADD CONSTRAINT fk_batch_documents_batch_tenant
    FOREIGN KEY (batch_id, tenant_id) REFERENCES document_batches(id, tenant_id) ON DELETE CASCADE;
ALTER TABLE batch_documents ADD CONSTRAINT fk_batch_documents_document_tenant
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE;
ALTER TABLE review_submissions ADD CONSTRAINT fk_review_submissions_submitter_tenant
    FOREIGN KEY (submitted_by, tenant_id) REFERENCES users(id, tenant_id);
ALTER TABLE review_decisions ADD CONSTRAINT fk_review_decisions_reviewer_tenant
    FOREIGN KEY (reviewed_by, tenant_id) REFERENCES users(id, tenant_id);
ALTER TABLE document_comments ADD CONSTRAINT fk_document_comments_author_tenant
    FOREIGN KEY (author_id, tenant_id) REFERENCES users(id, tenant_id);
ALTER TABLE supplier_contacts ADD CONSTRAINT fk_supplier_contacts_vendor_tenant
    FOREIGN KEY (vendor_id, tenant_id) REFERENCES vendors(id, tenant_id) ON DELETE CASCADE;

ALTER TABLE standardised_records ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT TRUE;
CREATE UNIQUE INDEX uq_standardised_records_current
    ON standardised_records(document_id) WHERE is_current;
ALTER TABLE standardised_records ADD CONSTRAINT fk_records_document_tenant
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE;
ALTER TABLE standardised_records ADD CONSTRAINT fk_records_creator_tenant
    FOREIGN KEY (created_by, tenant_id) REFERENCES users(id, tenant_id);

CREATE OR REPLACE FUNCTION require_approved_review_for_record() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM review_decisions
        WHERE document_id = NEW.document_id
          AND tenant_id = NEW.tenant_id
          AND decision = 'approved'
          AND approved_content_revision = (SELECT content_revision FROM documents WHERE id = NEW.document_id AND tenant_id = NEW.tenant_id)
    ) THEN
        RAISE EXCEPTION 'A document must have an approved review decision before standardisation.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_standardised_record_requires_approval
    BEFORE INSERT ON standardised_records
    FOR EACH ROW EXECUTE FUNCTION require_approved_review_for_record();

CREATE OR REPLACE FUNCTION record_document_status_change() RETURNS trigger AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO document_status_history(document_id, tenant_id, old_status, new_status, changed_by, reason)
        VALUES (NEW.id, NEW.tenant_id, OLD.status, NEW.status,
                NULLIF(current_setting('app.user_id', true), '')::uuid,
                COALESCE(current_setting('app.status_reason', true), ''));
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The line_items row is the authoritative current value; line_item_corrections is append-only history.
CREATE OR REPLACE VIEW current_line_items AS
SELECT li.id, li.document_id, li.extraction_id, li.line_number,
       COALESCE(c.description, li.description) AS description,
       COALESCE(CASE WHEN c.quantity ~ '^-?[0-9]+(\.[0-9]+)?$' THEN c.quantity::numeric END, li.quantity) AS quantity,
       COALESCE(CASE WHEN c.unit_price ~ '^-?[0-9]+(\.[0-9]+)?$' THEN c.unit_price::numeric END, li.unit_price) AS unit_price,
       COALESCE(CASE WHEN c.amount ~ '^-?[0-9]+(\.[0-9]+)?$' THEN c.amount::numeric END, li.amount) AS amount,
       li.field_states, li.evidence, li.candidates
FROM line_items li
LEFT JOIN LATERAL (
    SELECT
      (SELECT new_value FROM line_item_corrections WHERE line_item_id=li.id AND field_key='description' ORDER BY corrected_at DESC LIMIT 1) AS description,
      (SELECT new_value FROM line_item_corrections WHERE line_item_id=li.id AND field_key='quantity' ORDER BY corrected_at DESC LIMIT 1) AS quantity,
      (SELECT new_value FROM line_item_corrections WHERE line_item_id=li.id AND field_key='unit_price' ORDER BY corrected_at DESC LIMIT 1) AS unit_price,
      (SELECT new_value FROM line_item_corrections WHERE line_item_id=li.id AND field_key='amount' ORDER BY corrected_at DESC LIMIT 1) AS amount
) c ON true;

-- ---------------------------------------------------------------------------
-- Role-specific administration and review controls
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS permission_catalog (
    permission_key TEXT PRIMARY KEY,
    description    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role           TEXT NOT NULL CHECK (role IN ('super_admin', 'local_admin', 'accountant')),
    permission_key TEXT NOT NULL REFERENCES permission_catalog(permission_key) ON DELETE CASCADE,
    granted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (role, permission_key)
);

CREATE TABLE IF NOT EXISTS user_permissions (
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission_key TEXT NOT NULL REFERENCES permission_catalog(permission_key) ON DELETE CASCADE,
    granted        BOOLEAN NOT NULL DEFAULT TRUE,
    granted_by     UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, permission_key)
);

-- ---------------------------------------------------------------------------
-- Persisted workspace navigation
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workspace_modules (
    module_key      TEXT PRIMARY KEY,
    role            TEXT NOT NULL CHECK (role IN ('super_admin', 'local_admin', 'accountant')),
    section         TEXT NOT NULL,
    label           TEXT NOT NULL,
    route           TEXT NOT NULL,
    icon_key        TEXT NOT NULL DEFAULT 'folder',
    sort_order      INTEGER NOT NULL DEFAULT 0,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (role, section, label)
);

CREATE INDEX IF NOT EXISTS idx_workspace_modules_role_order
    ON workspace_modules(role, sort_order, label);

CREATE TABLE IF NOT EXISTS platform_notifications (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    notification_type TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    title         TEXT NOT NULL,
    message       TEXT NOT NULL,
    created_by    UUID REFERENCES users(id) ON DELETE SET NULL,
    acknowledged_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backup_runs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    backup_type    TEXT NOT NULL CHECK (backup_type IN ('database', 'documents', 'full')),
    status         TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    storage_ref    TEXT,
    checksum       TEXT,
    error_message  TEXT,
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workload_assignments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    assigned_to   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_by   UUID NOT NULL REFERENCES users(id),
    priority      TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    due_at        TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id)
);

CREATE TABLE IF NOT EXISTS document_status_history (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    old_status   TEXT,
    new_status   TEXT NOT NULL,
    changed_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_documents_status_history
    AFTER UPDATE OF status ON documents
    FOR EACH ROW EXECUTE FUNCTION record_document_status_change();

CREATE TABLE IF NOT EXISTS supplier_matches (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id    UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    vendor_id      UUID REFERENCES vendors(id) ON DELETE SET NULL,
    candidate_name TEXT NOT NULL,
    confidence     NUMERIC(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    source         TEXT NOT NULL CHECK (source IN ('ai', 'rule', 'manual')),
    decision       TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'accepted', 'rejected')),
    decided_by     UUID REFERENCES users(id) ON DELETE SET NULL,
    decided_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE supplier_matches
    ADD CONSTRAINT fk_supplier_matches_document_tenant
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE;
ALTER TABLE supplier_matches
    ADD CONSTRAINT fk_supplier_matches_vendor_tenant
    FOREIGN KEY (vendor_id, tenant_id) REFERENCES vendors(id, tenant_id) ON DELETE RESTRICT;

INSERT INTO permission_catalog(permission_key, description) VALUES
    ('dashboard.view', 'View the accountant workspace dashboard'),
    ('platform.monitoring', 'View platform health, workload and processing metrics'),
    ('platform.permissions', 'Manage granular user permissions'),
    ('platform.notifications', 'Create and acknowledge administrative notifications'),
    ('platform.backups', 'Request and review backup and recovery runs'),
    ('team.workload', 'Assign and manage team document workload'),
    ('documents.approve', 'Approve or return documents for correction'),
    ('suppliers.manage', 'Manage suppliers and supplier contacts'),
    ('documents.tag', 'Create and apply document tags'),
    ('documents.inbox', 'View and filter the document inbox'),
    ('documents.batch_upload', 'Create and process document batches'),
    ('documents.upload', 'Upload documents for processing'),
    ('documents.download', 'Download documents assigned to the user'),
    ('documents.history', 'View document status and version history'),
    ('documents.review', 'Review evidence and extracted fields'),
    ('documents.submit_approval', 'Submit reviewed documents for approval'),
    ('documents.comment', 'Add and resolve document comments'),
    ('processing.view', 'View processing and failed processing queues'),
    ('processing.validate', 'Validate documents in the processing queue'),
    ('processing.retry', 'Retry failed document processing'),
    ('exceptions.view', 'View assigned document exceptions'),
    ('exceptions.resolve', 'Resolve assigned document exceptions'),
    ('contacts.view', 'View customers and suppliers'),
    ('records.view', 'View standardised accounting records'),
    ('records.export', 'Export standardised accounting records'),
    ('reports.view', 'View tenant processing, validation and review reports'),
    ('exports.create', 'Create tenant-scoped exports'),
    ('audit.view', 'View tenant-scoped audit history'),
    ('notifications.view', 'View personal workspace notifications'),
    ('notifications.update', 'Mark personal notifications as read'),
    ('settings.view', 'View personal workspace settings'),
    ('settings.update', 'Update personal workspace settings'),
    ('suppliers.match', 'Accept or reject supplier matches')
ON CONFLICT (permission_key) DO NOTHING;

INSERT INTO role_permissions(role, permission_key)
SELECT 'super_admin', permission_key FROM permission_catalog
WHERE permission_key LIKE 'platform.%'
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions(role, permission_key)
SELECT 'local_admin', permission_key FROM permission_catalog
WHERE permission_key NOT LIKE 'platform.%'
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions(role, permission_key)
SELECT 'accountant', permission_key FROM permission_catalog
WHERE permission_key IN (
    'dashboard.view',
    'documents.inbox', 'documents.upload', 'documents.download', 'documents.history',
    'documents.review', 'documents.submit_approval', 'documents.comment',
    'processing.view', 'processing.validate', 'processing.retry',
    'exceptions.view', 'exceptions.resolve',
    'contacts.view', 'suppliers.match',
    'records.view', 'records.export',
    'reports.view', 'exports.create',
    'audit.view',
    'notifications.view', 'notifications.update',
    'settings.view', 'settings.update'
)
ON CONFLICT DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_user_permissions_user ON user_permissions(user_id, granted);
CREATE INDEX IF NOT EXISTS idx_platform_notifications_unread ON platform_notifications(acknowledged_at, created_at);
CREATE INDEX IF NOT EXISTS idx_backup_runs_status ON backup_runs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_workload_tenant_assignee ON workload_assignments(tenant_id, assigned_to, completed_at);
CREATE INDEX IF NOT EXISTS idx_document_status_history_document ON document_status_history(document_id, created_at);
CREATE INDEX IF NOT EXISTS idx_supplier_matches_document ON supplier_matches(document_id, decision);
CREATE INDEX IF NOT EXISTS idx_customer_matches_document ON customer_matches(document_id, decision);
CREATE INDEX IF NOT EXISTS idx_customers_tenant ON customers(tenant_id, customer_name);
CREATE INDEX IF NOT EXISTS idx_line_item_corrections_item ON line_item_corrections(line_item_id, corrected_at);
CREATE INDEX IF NOT EXISTS idx_record_amendments_original ON record_amendments(original_record_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_decisions_document ON review_decisions(document_id, reviewed_at);
