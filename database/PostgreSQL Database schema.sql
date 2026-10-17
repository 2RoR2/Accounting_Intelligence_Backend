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

DROP TABLE IF EXISTS
    audit_logs, workspace_settings, validation_rules, standardised_records,
    vendors, exceptions, line_items, field_predictions, raw_extractions,
    field_corrections, pending_jobs, documents, auth_tokens, account_limit_requests, company_requests,
    mfa_recovery_requests, auth_challenges, password_challenges, auth_rate_limits, web_sessions,
    users, tenants
CASCADE;
DROP SEQUENCE IF EXISTS document_invoice_seq, document_bill_seq, document_receipt_seq,
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

CREATE TABLE documents (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_code             TEXT NOT NULL UNIQUE,
    tenant_id                 UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    owner_id                  UUID NOT NULL REFERENCES users(id),
    assigned_to               UUID REFERENCES users(id) ON DELETE SET NULL,
    original_filename         TEXT NOT NULL,
    kind                      TEXT NOT NULL
        CHECK (kind IN ('invoice', 'bill', 'receipt')),
    category                  TEXT NOT NULL DEFAULT 'accounts_payable'
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
    UNIQUE (id, tenant_id)   -- lets pending_jobs refer to a document and its company together
);

CREATE FUNCTION set_document_code() RETURNS trigger AS $$
BEGIN
    IF NEW.document_code IS NULL OR NEW.document_code = '' THEN
        NEW.document_code := upper(NEW.kind) || '_' ||
            CASE NEW.kind
                WHEN 'invoice' THEN nextval('document_invoice_seq')::text
                WHEN 'bill'    THEN nextval('document_bill_seq')::text
                ELSE                nextval('document_receipt_seq')::text
            END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- document_code is NOT NULL, so the trigger fills it before the constraint is checked.
CREATE TRIGGER trg_set_document_code
    BEFORE INSERT ON documents
    FOR EACH ROW EXECUTE FUNCTION set_document_code();

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
    document_id        UUID NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendor_id          UUID REFERENCES vendors(id),
    vendor_match_confidence NUMERIC(4,3)
        CHECK (vendor_match_confidence IS NULL
               OR (vendor_match_confidence >= 0 AND vendor_match_confidence <= 1)),
    category           TEXT NOT NULL
        CHECK (category IN ('accounts_payable', 'accounts_receivable')),
    supplier_name      TEXT NOT NULL,
    buyer_name         TEXT,
    invoice_number     TEXT NOT NULL,
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
CREATE INDEX idx_documents_owner ON documents(owner_id);
CREATE INDEX idx_documents_file_hash ON documents(tenant_id, file_hash_sha256);
CREATE INDEX idx_documents_assigned ON documents(assigned_to);
CREATE INDEX idx_raw_extractions_document ON raw_extractions(document_id);
CREATE INDEX idx_field_predictions_document ON field_predictions(document_id);
CREATE INDEX idx_field_predictions_extraction ON field_predictions(extraction_id);
CREATE INDEX idx_field_corrections_prediction ON field_corrections(field_prediction_id);
-- Duplicate invoice check: find other documents with the same invoice or supplier value.
CREATE INDEX idx_field_predictions_key_value ON field_predictions(field_key, current_value);
CREATE INDEX idx_line_items_document ON line_items(document_id);
CREATE INDEX idx_exceptions_document ON exceptions(document_id);
CREATE INDEX idx_exceptions_tenant_status ON exceptions(tenant_id, status);
CREATE INDEX idx_records_tenant_created ON standardised_records(tenant_id, created_at);
CREATE INDEX idx_records_duplicate_lookup ON standardised_records(tenant_id, supplier_name, invoice_number);
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
