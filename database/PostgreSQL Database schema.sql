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
    benchmark_demo_documents, benchmark_demo_tenants, mfa_backup_codes,
    bank_coding_decisions, bank_coding_suggestions,
    reconciliation_decisions, reconciliation_candidates, bank_transactions, bank_imports,
    po_match_decisions, po_match_proposals, coding_decisions, coding_suggestions,
    cost_centres, coa_accounts,
    audit_logs, workspace_settings, validation_rules, validation_results, standardised_records,
    vendors, purchase_orders, goods_receipts, exceptions, line_items, field_predictions, raw_extractions,
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
DROP VIEW IF EXISTS unmatched_bank_transactions, current_bank_coding, current_coding_decisions;
DROP VIEW IF EXISTS field_prediction_inconsistencies, stale_open_exceptions;
DROP VIEW IF EXISTS exceptions_with_user, bank_cash_summary;
DROP FUNCTION IF EXISTS coa_hierarchy_guard() CASCADE;
DROP FUNCTION IF EXISTS p1_append_only() CASCADE;
DROP FUNCTION IF EXISTS coding_suggestion_guard() CASCADE;
DROP FUNCTION IF EXISTS coding_decision_guard() CASCADE;
DROP FUNCTION IF EXISTS coding_decision_apply() CASCADE;
DROP FUNCTION IF EXISTS po_proposal_guard() CASCADE;
DROP FUNCTION IF EXISTS po_decision_guard() CASCADE;
DROP FUNCTION IF EXISTS po_decision_apply() CASCADE;
DROP FUNCTION IF EXISTS bank_transaction_guard() CASCADE;
DROP FUNCTION IF EXISTS recon_candidate_guard() CASCADE;
DROP FUNCTION IF EXISTS recon_candidate_after() CASCADE;
DROP FUNCTION IF EXISTS recon_candidate_edit_guard() CASCADE;
DROP FUNCTION IF EXISTS recon_decision_check() CASCADE;
DROP FUNCTION IF EXISTS recon_decision_apply() CASCADE;
DROP FUNCTION IF EXISTS exceptions_keep_resolved_time() CASCADE;
DROP FUNCTION IF EXISTS fill_current_extraction() CASCADE;
DROP FUNCTION IF EXISTS benchmark_demo_registration_immutable() CASCADE;
DROP FUNCTION IF EXISTS mfa_backup_code_guard() CASCADE;
DROP FUNCTION IF EXISTS raw_extractions_fill_metadata() CASCADE;
DROP FUNCTION IF EXISTS raw_extractions_protect() CASCADE;
DROP FUNCTION IF EXISTS exceptions_fill_uploaded_by() CASCADE;
DROP FUNCTION IF EXISTS bank_import_status_guard() CASCADE;
DROP FUNCTION IF EXISTS bank_transactions_normalise_amounts() CASCADE;
DROP FUNCTION IF EXISTS bank_coding_suggestion_guard() CASCADE;
DROP FUNCTION IF EXISTS bank_coding_decision_guard() CASCADE;
DROP FUNCTION IF EXISTS bank_coding_decision_apply() CASCADE;

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
-- which moves is_current atomically and rejects reruns over accountant-reviewed values.
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
-- Rerun policy: save all fields/items, then switch within the same transaction.
-- Reviewed corrections and approved records require an explicit amendment.
-- Automatic correction carry-forward is not approved. Old predictions remain
-- immutable history; content_revision is incremented by the persistence caller.
-- ---------------------------------------------------------------------------
CREATE FUNCTION switch_current_extraction(new_extraction UUID) RETURNS INTEGER AS $$
DECLARE
    doc            UUID;
    old_extraction UUID;
BEGIN
    SELECT document_id INTO doc FROM raw_extractions WHERE id = new_extraction;
    IF doc IS NULL THEN
        RAISE EXCEPTION 'Unknown extraction run.';
    END IF;
    PERFORM 1 FROM documents WHERE id = doc FOR UPDATE;
    SELECT id INTO old_extraction FROM raw_extractions WHERE document_id = doc AND is_current;
    IF old_extraction = new_extraction THEN
        RETURN 0;
    END IF;
    IF EXISTS (SELECT 1 FROM documents WHERE id = doc AND status IN ('completed','standardised'))
       OR EXISTS (SELECT 1 FROM standardised_records WHERE document_id = doc)
       OR EXISTS (SELECT 1 FROM review_decisions rd JOIN documents d
           ON d.id = rd.document_id AND d.tenant_id = rd.tenant_id
           WHERE d.id = doc AND rd.decision = 'approved'
             AND rd.approved_content_revision = d.content_revision) THEN
        RAISE EXCEPTION 'Approved documents require an explicit amendment.';
    END IF;
    IF EXISTS (SELECT 1 FROM field_predictions
           WHERE extraction_id = old_extraction AND corrected_at IS NOT NULL)
       OR EXISTS (SELECT 1 FROM line_item_corrections WHERE document_id = doc) THEN
        RAISE EXCEPTION 'Reviewed corrections exist; re-extraction requires an explicit amendment.';
    END IF;
    IF old_extraction IS NOT NULL THEN
        UPDATE raw_extractions SET is_current = FALSE WHERE id = old_extraction;
    END IF;
    UPDATE raw_extractions SET is_current = TRUE WHERE id = new_extraction;
    RETURN 0;
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

-- Versioned result for every validation rule/check at a document revision.
CREATE TABLE validation_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    tenant_id UUID NOT NULL,
    content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
    rule_id TEXT NOT NULL,
    rule_source TEXT NOT NULL DEFAULT 'SAIC',
    rule_version TEXT NOT NULL DEFAULT 'validation-v1',
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
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE RESTRICT
);
CREATE INDEX idx_validation_results_revision ON validation_results(document_id, content_revision, created_at);

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

-- Purchase orders and goods receipts used by optional AP matching.
CREATE TABLE purchase_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendor_id UUID,
    po_number TEXT NOT NULL,
    order_date DATE,
    currency CHAR(3),
    total_amount NUMERIC(14,2) CHECK (total_amount >= 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('draft','open','partially_received','closed','cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, po_number),
    FOREIGN KEY (vendor_id, tenant_id) REFERENCES vendors(id, tenant_id) ON DELETE RESTRICT
);
ALTER TABLE purchase_orders
    ADD CONSTRAINT uq_purchase_orders_id_tenant UNIQUE (id, tenant_id);
CREATE TABLE purchase_order_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purchase_order_id UUID NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL CHECK (line_number >= 1),
    description TEXT NOT NULL,
    quantity NUMERIC(14,3) NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(14,4) NOT NULL CHECK (unit_price >= 0),
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
    UNIQUE (purchase_order_id, line_number)
);
CREATE TABLE goods_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    purchase_order_id UUID NOT NULL,
    receipt_number TEXT NOT NULL,
    received_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('draft','received','cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, receipt_number),
    FOREIGN KEY (purchase_order_id) REFERENCES purchase_orders(id) ON DELETE RESTRICT
);
CREATE TABLE goods_receipt_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goods_receipt_id UUID NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
    purchase_order_line_id UUID REFERENCES purchase_order_lines(id) ON DELETE RESTRICT,
    line_number INTEGER NOT NULL CHECK (line_number >= 1),
    description TEXT NOT NULL,
    quantity NUMERIC(14,3) NOT NULL CHECK (quantity > 0),
    UNIQUE (goods_receipt_id, line_number)
);
CREATE INDEX idx_purchase_orders_tenant_status ON purchase_orders(tenant_id, status);
CREATE INDEX idx_goods_receipts_po ON goods_receipts(purchase_order_id, received_date);

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

-- Composite tenant identity is referenced by later tenant-scoped foreign keys.
-- Define it with the table so a clean current-schema bootstrap works in tests
-- and fresh deployments; the idempotent migration below remains for existing DBs.
ALTER TABLE standardised_records
    ADD CONSTRAINT uq_standardised_records_id_tenant UNIQUE (id, tenant_id);

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
    approval_limit_amount             NUMERIC(14,2),
    approval_limit_currency            CHAR(3),
    require_po_for_ap                  BOOLEAN NOT NULL DEFAULT FALSE,
    require_grn_for_ap                 BOOLEAN NOT NULL DEFAULT FALSE,
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

-- One-time MFA recovery codes. Only the digest is stored; plaintext codes are
-- returned once at generation time and are never recoverable from the DB.
CREATE TABLE IF NOT EXISTS mfa_backup_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_mfa_backup_codes_user_unused
    ON mfa_backup_codes(user_id) WHERE used_at IS NULL;

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

-- ============================================================================
-- P1 tables: chart of accounts and coding, purchase order match review, bank import and reconciliation,
-- and classification of bank rows. The same SQL is in database/migrations/005 to 008 for databases that already exist.
-- Everything below is safe to run twice.
-- ============================================================================

-- 005_p1_coa_cost_centres_coding.sql
-- P1 chart of accounts, cost centres and accountant coding decisions (roadmap gate G4).
-- Owner: Aleeya (Module 4). Contract: docs/DOMAIN_AND_API_CONTRACTS_V2.md, section 5.
--
-- Rules this file enforces in the database:
--   * Every row belongs to one company. Links between rows are checked with composite foreign
--     keys (id, tenant_id), so a row can never point at another company's data.
--   * A confirmed account is NOT a journal entry. Nothing here posts to a ledger.
--   * Decisions are append only. A newer decision replaces an older one in the current view,
--     it never edits it.
--   * A decision is refused if the approved record changed since the accountant reviewed it.
--   * Suggestions are candidates. Only a person's decision confirms an account.
--
-- Apply with manage.py migrate on an ISOLATED database first. The migration runner owns the
-- transaction, so this file has no BEGIN or COMMIT. It can be run twice safely.

-- Lets other tables prove that a record belongs to the same company.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_standardised_records_id_tenant') THEN
        ALTER TABLE standardised_records ADD CONSTRAINT uq_standardised_records_id_tenant UNIQUE (id, tenant_id);
    END IF;
END $$;

-- ---------------------------------------------------------------- chart of accounts
CREATE TABLE IF NOT EXISTS coa_accounts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code           TEXT NOT NULL CHECK (btrim(code) <> ''),
    name           TEXT NOT NULL CHECK (btrim(name) <> ''),
    account_class  TEXT NOT NULL CHECK (account_class IN ('asset', 'liability', 'equity', 'revenue', 'cogs', 'expense')),
    parent_id      UUID,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    policy_version TEXT NOT NULL DEFAULT 'v1',
    created_by     UUID,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code),
    UNIQUE (id, tenant_id),
    CHECK (parent_id IS NULL OR parent_id <> id),
    FOREIGN KEY (parent_id, tenant_id) REFERENCES coa_accounts(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (created_by, tenant_id) REFERENCES users(id, tenant_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS cost_centres (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code       TEXT NOT NULL CHECK (btrim(code) <> ''),
    name       TEXT NOT NULL CHECK (btrim(name) <> ''),
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code),
    UNIQUE (id, tenant_id),
    FOREIGN KEY (created_by, tenant_id) REFERENCES users(id, tenant_id) ON DELETE RESTRICT
);

-- A child account keeps its parent's class, and the hierarchy cannot loop.
CREATE OR REPLACE FUNCTION coa_hierarchy_guard() RETURNS trigger AS $$
DECLARE
    parent_class TEXT;
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT account_class INTO parent_class
    FROM coa_accounts WHERE id = NEW.parent_id AND tenant_id = NEW.tenant_id;
    IF parent_class IS NULL THEN
        RAISE EXCEPTION 'The parent account does not exist in this company.';
    END IF;
    IF parent_class <> NEW.account_class THEN
        RAISE EXCEPTION 'An account must have the same class as its parent.';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF EXISTS (
            WITH RECURSIVE chain(id, parent_id) AS (
                SELECT a.id, a.parent_id FROM coa_accounts a WHERE a.id = NEW.parent_id
                UNION
                SELECT a.id, a.parent_id FROM coa_accounts a JOIN chain c ON a.id = c.parent_id
            )
            SELECT 1 FROM chain WHERE id = NEW.id
        ) THEN
            RAISE EXCEPTION 'An account cannot be its own ancestor.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_coa_hierarchy ON coa_accounts;
CREATE TRIGGER trg_coa_hierarchy
    BEFORE INSERT OR UPDATE OF parent_id, account_class ON coa_accounts
    FOR EACH ROW EXECUTE FUNCTION coa_hierarchy_guard();

-- Shared by every history table in the P1 migrations.
CREATE OR REPLACE FUNCTION p1_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'This history is append only. Add a new decision instead.';
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------- coding suggestions
CREATE TABLE IF NOT EXISTS coding_suggestions (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    record_id                UUID NOT NULL,
    line_item_id             UUID REFERENCES line_items(id) ON DELETE RESTRICT,
    candidate_coa_id         UUID,
    candidate_cost_centre_id UUID,
    evidence                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    method                   TEXT NOT NULL CHECK (method IN ('rule', 'vendor_history', 'semantic_match', 'model_candidate')),
    rule_version             TEXT NOT NULL CHECK (btrim(rule_version) <> ''),
    status                   TEXT NOT NULL DEFAULT 'suggested'
                             CHECK (status IN ('suggested', 'accepted', 'overridden', 'rejected', 'superseded')),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    CHECK (candidate_coa_id IS NOT NULL OR candidate_cost_centre_id IS NOT NULL),
    FOREIGN KEY (record_id, tenant_id) REFERENCES standardised_records(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (candidate_coa_id, tenant_id) REFERENCES coa_accounts(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (candidate_cost_centre_id, tenant_id) REFERENCES cost_centres(id, tenant_id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION coding_suggestion_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.candidate_coa_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM coa_accounts
        WHERE id = NEW.candidate_coa_id AND tenant_id = NEW.tenant_id AND is_active) THEN
        RAISE EXCEPTION 'The suggested account is inactive.';
    END IF;
    IF NEW.candidate_cost_centre_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM cost_centres
        WHERE id = NEW.candidate_cost_centre_id AND tenant_id = NEW.tenant_id AND is_active) THEN
        RAISE EXCEPTION 'The suggested cost centre is inactive.';
    END IF;
    IF NEW.line_item_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM line_items li
        JOIN standardised_records r ON r.document_id = li.document_id
        WHERE li.id = NEW.line_item_id AND r.id = NEW.record_id AND r.tenant_id = NEW.tenant_id) THEN
        RAISE EXCEPTION 'The line item does not belong to this record.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_coding_suggestion_guard ON coding_suggestions;
CREATE TRIGGER trg_coding_suggestion_guard
    BEFORE INSERT ON coding_suggestions
    FOR EACH ROW EXECUTE FUNCTION coding_suggestion_guard();

-- ---------------------------------------------------------------- coding decisions
CREATE TABLE IF NOT EXISTS coding_decisions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    record_id      UUID NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
    line_item_id   UUID REFERENCES line_items(id) ON DELETE RESTRICT,
    suggestion_id  UUID,
    account_id     UUID NOT NULL,
    cost_centre_id UUID,
    user_id        UUID NOT NULL,
    decision       TEXT NOT NULL CHECK (decision IN ('accepted', 'overridden', 'manual')),
    reason         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    CONSTRAINT ck_coding_reason CHECK (decision = 'accepted' OR (reason IS NOT NULL AND btrim(reason) <> '')),
    CONSTRAINT ck_coding_suggestion CHECK (decision = 'manual' OR suggestion_id IS NOT NULL),
    FOREIGN KEY (record_id, tenant_id) REFERENCES standardised_records(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (suggestion_id, tenant_id) REFERENCES coding_suggestions(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (account_id, tenant_id) REFERENCES coa_accounts(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (cost_centre_id, tenant_id) REFERENCES cost_centres(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, tenant_id) REFERENCES users(id, tenant_id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION coding_decision_guard() RETURNS trigger AS $$
DECLARE
    rec_revision INTEGER;
    rec_current  BOOLEAN;
    sug_record   UUID;
BEGIN
    SELECT revision_number, is_current INTO rec_revision, rec_current
    FROM standardised_records WHERE id = NEW.record_id AND tenant_id = NEW.tenant_id;
    IF rec_revision IS NULL THEN
        RAISE EXCEPTION 'The record does not exist in this company.';
    END IF;
    IF NOT rec_current OR rec_revision <> NEW.source_revision THEN
        RAISE EXCEPTION 'The record changed since it was reviewed.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM coa_accounts
                   WHERE id = NEW.account_id AND tenant_id = NEW.tenant_id AND is_active) THEN
        RAISE EXCEPTION 'The chosen account is inactive.';
    END IF;
    IF NEW.cost_centre_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM cost_centres
        WHERE id = NEW.cost_centre_id AND tenant_id = NEW.tenant_id AND is_active) THEN
        RAISE EXCEPTION 'The chosen cost centre is inactive.';
    END IF;
    IF NEW.line_item_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM line_items li
        JOIN standardised_records r ON r.document_id = li.document_id
        WHERE li.id = NEW.line_item_id AND r.id = NEW.record_id AND r.tenant_id = NEW.tenant_id) THEN
        RAISE EXCEPTION 'The line item does not belong to this record.';
    END IF;
    IF NEW.suggestion_id IS NOT NULL THEN
        SELECT record_id INTO sug_record
        FROM coding_suggestions WHERE id = NEW.suggestion_id AND tenant_id = NEW.tenant_id;
        IF sug_record IS DISTINCT FROM NEW.record_id THEN
            RAISE EXCEPTION 'The suggestion belongs to a different record.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION coding_decision_apply() RETURNS trigger AS $$
BEGIN
    IF NEW.suggestion_id IS NOT NULL THEN
        UPDATE coding_suggestions
        SET status = CASE WHEN NEW.decision = 'accepted' THEN 'accepted' ELSE 'overridden' END
        WHERE id = NEW.suggestion_id AND tenant_id = NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_coding_decision_guard ON coding_decisions;
CREATE TRIGGER trg_coding_decision_guard
    BEFORE INSERT ON coding_decisions
    FOR EACH ROW EXECUTE FUNCTION coding_decision_guard();

DROP TRIGGER IF EXISTS trg_coding_decision_apply ON coding_decisions;
CREATE TRIGGER trg_coding_decision_apply
    AFTER INSERT ON coding_decisions
    FOR EACH ROW EXECUTE FUNCTION coding_decision_apply();

DROP TRIGGER IF EXISTS trg_coding_decisions_append_only ON coding_decisions;
CREATE TRIGGER trg_coding_decisions_append_only
    BEFORE UPDATE OR DELETE ON coding_decisions
    FOR EACH ROW EXECUTE FUNCTION p1_append_only();

-- The latest decision for each record (and line item). This is what the screens read.
CREATE OR REPLACE VIEW current_coding_decisions AS
SELECT DISTINCT ON (record_id, COALESCE(line_item_id, '00000000-0000-0000-0000-000000000000'::uuid))
       id, tenant_id, record_id, source_revision, line_item_id, suggestion_id,
       account_id, cost_centre_id, user_id, decision, reason, created_at
FROM coding_decisions
ORDER BY record_id, COALESCE(line_item_id, '00000000-0000-0000-0000-000000000000'::uuid), created_at DESC, id DESC;

CREATE INDEX IF NOT EXISTS idx_coa_accounts_active ON coa_accounts (tenant_id, is_active, code);
CREATE INDEX IF NOT EXISTS idx_coding_suggestions_record ON coding_suggestions (tenant_id, record_id, status);
CREATE INDEX IF NOT EXISTS idx_coding_decisions_record ON coding_decisions (tenant_id, record_id, created_at);

-- 006_p1_po_match_review.sql
-- P1 purchase order matching: proposals made by the matching rules and the reviewer's decision
-- (roadmap gate G5). Owner: Aleeya (Module 4). Contract: docs/DOMAIN_AND_API_CONTRACTS_V2.md.
--
-- The purchase_orders and purchase_order_lines tables already exist (AP controls migration).
-- This file adds the two review tables and two columns the contract asks for.
-- Do not store PO-only data in document_reconciliations: that table links documents to documents.
--
-- Apply after 005. Safe to run twice. No BEGIN or COMMIT: the migration runner owns the transaction.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_purchase_orders_id_tenant') THEN
        ALTER TABLE purchase_orders ADD CONSTRAINT uq_purchase_orders_id_tenant UNIQUE (id, tenant_id);
    END IF;
END $$;

-- Hash of the imported file, and a revision number that rises when the order is re-imported.
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS source_hash TEXT
    CHECK (source_hash IS NULL OR source_hash ~ '^[0-9a-f]{64}$');
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1);

CREATE TABLE IF NOT EXISTS po_match_proposals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL,
    po_id           UUID NOT NULL,
    item_alignments JSONB NOT NULL DEFAULT '[]'::jsonb,
    discrepancies   JSONB NOT NULL DEFAULT '[]'::jsonb,
    method          TEXT NOT NULL CHECK (method IN ('rule', 'semantic_match', 'model_candidate')),
    status          TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'approved', 'rejected', 'overridden', 'superseded')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (po_id, tenant_id) REFERENCES purchase_orders(id, tenant_id) ON DELETE RESTRICT
);

-- One open proposal for the same document and order.
CREATE UNIQUE INDEX IF NOT EXISTS uq_po_match_open
    ON po_match_proposals (tenant_id, document_id, po_id) WHERE status = 'proposed';

CREATE TABLE IF NOT EXISTS po_match_decisions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    proposal_id UUID NOT NULL,
    decision    TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'overridden')),
    reviewer_id UUID NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_po_decision_reason CHECK (decision = 'approved' OR (reason IS NOT NULL AND btrim(reason) <> '')),
    FOREIGN KEY (proposal_id, tenant_id) REFERENCES po_match_proposals(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (reviewer_id, tenant_id) REFERENCES users(id, tenant_id) ON DELETE RESTRICT
);

-- A proposal's content is evidence. Only its status can change.
CREATE OR REPLACE FUNCTION po_proposal_guard() RETURNS trigger AS $$
BEGIN
    IF (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status') THEN
        RAISE EXCEPTION 'A match proposal cannot be edited. Only its status can change.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_po_proposal_guard ON po_match_proposals;
CREATE TRIGGER trg_po_proposal_guard
    BEFORE UPDATE ON po_match_proposals
    FOR EACH ROW EXECUTE FUNCTION po_proposal_guard();

-- A proposal can be decided once.
CREATE OR REPLACE FUNCTION po_decision_guard() RETURNS trigger AS $$
DECLARE
    proposal_status TEXT;
BEGIN
    SELECT status INTO proposal_status
    FROM po_match_proposals WHERE id = NEW.proposal_id AND tenant_id = NEW.tenant_id FOR UPDATE;
    IF proposal_status IS NULL THEN
        RAISE EXCEPTION 'The proposal does not exist in this company.';
    END IF;
    IF proposal_status <> 'proposed' THEN
        RAISE EXCEPTION 'This proposal has already been decided.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION po_decision_apply() RETURNS trigger AS $$
BEGIN
    UPDATE po_match_proposals SET status = NEW.decision
    WHERE id = NEW.proposal_id AND tenant_id = NEW.tenant_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_po_decision_guard ON po_match_decisions;
CREATE TRIGGER trg_po_decision_guard
    BEFORE INSERT ON po_match_decisions
    FOR EACH ROW EXECUTE FUNCTION po_decision_guard();

DROP TRIGGER IF EXISTS trg_po_decision_apply ON po_match_decisions;
CREATE TRIGGER trg_po_decision_apply
    AFTER INSERT ON po_match_decisions
    FOR EACH ROW EXECUTE FUNCTION po_decision_apply();

DROP TRIGGER IF EXISTS trg_po_decisions_append_only ON po_match_decisions;
CREATE TRIGGER trg_po_decisions_append_only
    BEFORE UPDATE OR DELETE ON po_match_decisions
    FOR EACH ROW EXECUTE FUNCTION p1_append_only();

CREATE INDEX IF NOT EXISTS idx_po_match_proposals_document ON po_match_proposals (tenant_id, document_id, status);
CREATE INDEX IF NOT EXISTS idx_po_match_decisions_proposal ON po_match_decisions (proposal_id, created_at);

-- 007_p1_bank_import_reconciliation.sql
-- P1 bank CSV import, Money In / Money Out and reconciliation (roadmap gate G6).
-- Owner: Aleeya (Module 4). Contract: docs/DOMAIN_AND_API_CONTRACTS_V2.md, section 5.
--
-- Rules this file enforces in the database:
--   * Money Out is a bank DEBIT and Money In is a bank CREDIT. The direction is stored with the row
--     and must agree with the debit and credit columns. Anything unclear is stored as needs_review
--     and can never be matched.
--   * A bank row is evidence. Once imported, only its status can change.
--   * P1 policy: one to one, same currency, full amount matches. A match can be confirmed only
--     when the currency and the amount agree exactly with the current approved record.
--   * A bank row and a record can each be confirmed once. Reversing a match is a new, audited
--     decision. Nothing is ever edited or deleted.
--   * Confirming a match records what the bank shows. It does not pay anything and it does not
--     post to a ledger. No account numbers are stored: only an anonymised reference.
--
-- Apply after 005 and 006. Safe to run twice. No BEGIN or COMMIT: the migration runner owns it.

-- ---------------------------------------------------------------- imports
CREATE TABLE IF NOT EXISTS bank_imports (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_bank_account_ref TEXT NOT NULL CHECK (btrim(source_bank_account_ref) <> ''),
    source_hash             TEXT NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    input_format            TEXT NOT NULL CHECK (input_format IN ('csv')),
    mapping_version         TEXT NOT NULL CHECK (btrim(mapping_version) <> ''),
    imported_by             UUID NOT NULL,
    imported_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_count               INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    status                  TEXT NOT NULL DEFAULT 'previewed'
                            CHECK (status IN ('previewed', 'confirmed', 'failed', 'reverted')),
    UNIQUE (id, tenant_id),
    FOREIGN KEY (imported_by, tenant_id) REFERENCES users(id, tenant_id) ON DELETE RESTRICT
);

-- The same file cannot be imported twice while an earlier import of it is still open.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_imports_open_file
    ON bank_imports (tenant_id, source_hash) WHERE status IN ('previewed', 'confirmed');

-- ---------------------------------------------------------------- normalised rows
CREATE TABLE IF NOT EXISTS bank_transactions (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    import_id             UUID NOT NULL,
    source_row            INTEGER NOT NULL CHECK (source_row >= 1),
    posted_date           DATE,
    value_date            DATE,
    description           TEXT NOT NULL DEFAULT '',
    reference             TEXT,
    debit_decimal         NUMERIC(14,2) CHECK (debit_decimal IS NULL OR debit_decimal >= 0),
    credit_decimal        NUMERIC(14,2) CHECK (credit_decimal IS NULL OR credit_decimal >= 0),
    signed_amount_decimal NUMERIC(14,2),
    direction             TEXT CHECK (direction IN ('money_in', 'money_out')),
    currency              CHAR(3) CHECK (currency IS NULL OR currency = upper(currency)),
    -- Built by the importer: a sha256 of the company, bank reference, date, amount, description and a
    -- counter for repeated identical rows on one day. It stops overlapping statements from loading a row twice.
    fingerprint           TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    status                TEXT NOT NULL DEFAULT 'imported'
                          CHECK (status IN ('imported', 'needs_review', 'proposed', 'matched', 'ignored')),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    UNIQUE (tenant_id, fingerprint),
    UNIQUE (import_id, source_row),
    CONSTRAINT ck_bank_direction CHECK (
        (direction IS NULL AND status IN ('needs_review', 'ignored'))
        OR (direction = 'money_out' AND debit_decimal IS NOT NULL AND debit_decimal > 0
            AND credit_decimal IS NULL AND signed_amount_decimal = -debit_decimal)
        OR (direction = 'money_in' AND credit_decimal IS NOT NULL AND credit_decimal > 0
            AND debit_decimal IS NULL AND signed_amount_decimal = credit_decimal)
    ),
    FOREIGN KEY (import_id, tenant_id) REFERENCES bank_imports(id, tenant_id) ON DELETE RESTRICT
);

-- A bank row is evidence: only its status may change, and a row in a match cannot be deleted.
CREATE OR REPLACE FUNCTION bank_transaction_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('proposed', 'matched') THEN
            RAISE EXCEPTION 'A bank row that is part of a match cannot be deleted.';
        END IF;
        RETURN OLD;
    END IF;
    IF (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status') THEN
        RAISE EXCEPTION 'A bank row is evidence and cannot be edited. Only its status can change.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bank_transaction_guard ON bank_transactions;
CREATE TRIGGER trg_bank_transaction_guard
    BEFORE UPDATE OR DELETE ON bank_transactions
    FOR EACH ROW EXECUTE FUNCTION bank_transaction_guard();

-- ---------------------------------------------------------------- candidates
CREATE TABLE IF NOT EXISTS reconciliation_candidates (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    bank_transaction_id  UUID NOT NULL,
    record_id            UUID NOT NULL,
    reason_codes         JSONB NOT NULL DEFAULT '[]'::jsonb,
    score_components     JSONB NOT NULL DEFAULT '{}'::jsonb,
    amount_delta_decimal NUMERIC(14,2),
    date_delta_days      INTEGER,
    proposed_by          TEXT NOT NULL CHECK (proposed_by IN ('rules', 'semantic_match', 'accountant')),
    status               TEXT NOT NULL DEFAULT 'proposed'
                         CHECK (status IN ('proposed', 'confirmed', 'rejected', 'reversed', 'superseded')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    UNIQUE (bank_transaction_id, record_id),
    FOREIGN KEY (bank_transaction_id, tenant_id) REFERENCES bank_transactions(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (record_id, tenant_id) REFERENCES standardised_records(id, tenant_id) ON DELETE RESTRICT
);

-- One confirmed match per bank row and per record: this is what stops a double allocation.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recon_confirmed_bank
    ON reconciliation_candidates (bank_transaction_id) WHERE status = 'confirmed';
CREATE UNIQUE INDEX IF NOT EXISTS uq_recon_confirmed_record
    ON reconciliation_candidates (record_id) WHERE status = 'confirmed';

CREATE OR REPLACE FUNCTION recon_candidate_guard() RETURNS trigger AS $$
DECLARE
    tx_status  TEXT;
    rec_current BOOLEAN;
BEGIN
    SELECT status INTO tx_status
    FROM bank_transactions WHERE id = NEW.bank_transaction_id AND tenant_id = NEW.tenant_id;
    IF tx_status IS NULL THEN
        RAISE EXCEPTION 'The bank row does not exist in this company.';
    END IF;
    IF tx_status NOT IN ('imported', 'proposed') THEN
        RAISE EXCEPTION 'This bank row cannot be matched in its current status.';
    END IF;
    SELECT is_current INTO rec_current
    FROM standardised_records WHERE id = NEW.record_id AND tenant_id = NEW.tenant_id;
    IF rec_current IS NULL OR NOT rec_current THEN
        RAISE EXCEPTION 'Only the current approved record can be matched.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION recon_candidate_after() RETURNS trigger AS $$
BEGIN
    UPDATE bank_transactions SET status = 'proposed'
    WHERE id = NEW.bank_transaction_id AND tenant_id = NEW.tenant_id AND status = 'imported';
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- A candidate's content is evidence. Only its status can change.
CREATE OR REPLACE FUNCTION recon_candidate_edit_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'confirmed' THEN
            RAISE EXCEPTION 'A confirmed match cannot be deleted. Reverse it with a new decision.';
        END IF;
        RETURN OLD;
    END IF;
    IF (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status') THEN
        RAISE EXCEPTION 'A match candidate cannot be edited. Only its status can change.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_recon_candidate_guard ON reconciliation_candidates;
CREATE TRIGGER trg_recon_candidate_guard
    BEFORE INSERT ON reconciliation_candidates
    FOR EACH ROW EXECUTE FUNCTION recon_candidate_guard();

DROP TRIGGER IF EXISTS trg_recon_candidate_after ON reconciliation_candidates;
CREATE TRIGGER trg_recon_candidate_after
    AFTER INSERT ON reconciliation_candidates
    FOR EACH ROW EXECUTE FUNCTION recon_candidate_after();

DROP TRIGGER IF EXISTS trg_recon_candidate_edit_guard ON reconciliation_candidates;
CREATE TRIGGER trg_recon_candidate_edit_guard
    BEFORE UPDATE OR DELETE ON reconciliation_candidates
    FOR EACH ROW EXECUTE FUNCTION recon_candidate_edit_guard();

-- ---------------------------------------------------------------- decisions
CREATE TABLE IF NOT EXISTS reconciliation_decisions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    candidate_id      UUID NOT NULL,
    decision          TEXT NOT NULL CHECK (decision IN ('confirmed', 'rejected', 'reversed')),
    reviewer_id       UUID NOT NULL,
    reason            TEXT,
    confirmed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expected_revision INTEGER NOT NULL CHECK (expected_revision >= 1),
    UNIQUE (id, tenant_id),
    CONSTRAINT ck_recon_reason CHECK (decision = 'confirmed' OR (reason IS NOT NULL AND btrim(reason) <> '')),
    FOREIGN KEY (candidate_id, tenant_id) REFERENCES reconciliation_candidates(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (reviewer_id, tenant_id) REFERENCES users(id, tenant_id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION recon_decision_check() RETURNS trigger AS $$
DECLARE
    c_row   reconciliation_candidates%ROWTYPE;
    tx_row  bank_transactions%ROWTYPE;
    rec_row standardised_records%ROWTYPE;
BEGIN
    SELECT * INTO c_row FROM reconciliation_candidates
    WHERE id = NEW.candidate_id AND tenant_id = NEW.tenant_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'The match candidate does not exist in this company.';
    END IF;
    SELECT * INTO tx_row FROM bank_transactions WHERE id = c_row.bank_transaction_id FOR UPDATE;
    SELECT * INTO rec_row FROM standardised_records WHERE id = c_row.record_id;

    IF NEW.decision = 'reversed' THEN
        IF c_row.status <> 'confirmed' THEN
            RAISE EXCEPTION 'Only a confirmed match can be reversed.';
        END IF;
        RETURN NEW;
    END IF;

    IF c_row.status <> 'proposed' THEN
        RAISE EXCEPTION 'This match candidate has already been decided.';
    END IF;

    IF NEW.decision = 'confirmed' THEN
        IF tx_row.status NOT IN ('imported', 'proposed') THEN
            RAISE EXCEPTION 'This bank row cannot be matched in its current status.';
        END IF;
        IF tx_row.direction IS NULL OR tx_row.currency IS NULL OR tx_row.signed_amount_decimal IS NULL THEN
            RAISE EXCEPTION 'A bank row with a missing direction, currency or amount needs review first.';
        END IF;
        IF rec_row.currency IS NULL OR rec_row.currency <> tx_row.currency THEN
            RAISE EXCEPTION 'The bank row and the record must have the same currency.';
        END IF;
        IF rec_row.total_amount IS NULL OR abs(tx_row.signed_amount_decimal) <> rec_row.total_amount THEN
            RAISE EXCEPTION 'Only full amount matches can be confirmed.';
        END IF;
        IF NOT rec_row.is_current OR rec_row.revision_number <> NEW.expected_revision THEN
            RAISE EXCEPTION 'The record changed since it was reviewed.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION recon_decision_apply() RETURNS trigger AS $$
DECLARE
    tx_id  UUID;
    rec_id UUID;
BEGIN
    SELECT bank_transaction_id, record_id INTO tx_id, rec_id
    FROM reconciliation_candidates WHERE id = NEW.candidate_id;

    IF NEW.decision = 'confirmed' THEN
        UPDATE reconciliation_candidates SET status = 'confirmed' WHERE id = NEW.candidate_id;
        UPDATE bank_transactions SET status = 'matched' WHERE id = tx_id;
        UPDATE reconciliation_candidates SET status = 'superseded'
        WHERE tenant_id = NEW.tenant_id AND status = 'proposed' AND id <> NEW.candidate_id
          AND (bank_transaction_id = tx_id OR record_id = rec_id);
        UPDATE bank_transactions b SET status = 'imported'
        WHERE b.tenant_id = NEW.tenant_id AND b.status = 'proposed' AND b.id <> tx_id
          AND NOT EXISTS (SELECT 1 FROM reconciliation_candidates c
                          WHERE c.bank_transaction_id = b.id AND c.status = 'proposed');
    ELSIF NEW.decision = 'rejected' THEN
        UPDATE reconciliation_candidates SET status = 'rejected' WHERE id = NEW.candidate_id;
        UPDATE bank_transactions SET status = 'imported'
        WHERE id = tx_id AND status = 'proposed'
          AND NOT EXISTS (SELECT 1 FROM reconciliation_candidates
                          WHERE bank_transaction_id = tx_id AND status = 'proposed');
    ELSE
        UPDATE reconciliation_candidates SET status = 'reversed' WHERE id = NEW.candidate_id;
        UPDATE bank_transactions SET status = 'imported' WHERE id = tx_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_recon_decision_check ON reconciliation_decisions;
CREATE TRIGGER trg_recon_decision_check
    BEFORE INSERT ON reconciliation_decisions
    FOR EACH ROW EXECUTE FUNCTION recon_decision_check();

DROP TRIGGER IF EXISTS trg_recon_decision_apply ON reconciliation_decisions;
CREATE TRIGGER trg_recon_decision_apply
    AFTER INSERT ON reconciliation_decisions
    FOR EACH ROW EXECUTE FUNCTION recon_decision_apply();

DROP TRIGGER IF EXISTS trg_recon_decisions_append_only ON reconciliation_decisions;
CREATE TRIGGER trg_recon_decisions_append_only
    BEFORE UPDATE OR DELETE ON reconciliation_decisions
    FOR EACH ROW EXECUTE FUNCTION p1_append_only();

-- Bank rows that still need a person: not matched, and not ignored.
CREATE OR REPLACE VIEW unmatched_bank_transactions AS
SELECT id, tenant_id, import_id, source_row, posted_date, value_date, description, reference,
       debit_decimal, credit_decimal, signed_amount_decimal, direction, currency, status, created_at
FROM bank_transactions
WHERE status IN ('imported', 'needs_review', 'proposed');

CREATE INDEX IF NOT EXISTS idx_bank_transactions_status ON bank_transactions (tenant_id, status, posted_date);
CREATE INDEX IF NOT EXISTS idx_recon_candidates_status ON reconciliation_candidates (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_recon_candidates_record ON reconciliation_candidates (record_id);
CREATE INDEX IF NOT EXISTS idx_recon_decisions_candidate ON reconciliation_decisions (candidate_id, confirmed_at);

-- 008_p1_bank_row_coding.sql
-- Classifying bank rows that have no invoice (bank charges, interest, owner top ups), so the
-- Money In and Money Out screens can show what each unmatched row was for.
-- Owner: Aleeya (Module 4). Follows the same pattern as the coding tables in migration 005.
--
-- Rules this file enforces in the database:
--   * Money In and Money Out are decided by the bank row's debit and credit (migration 007).
--     This file only adds a classification: an account and an optional cost centre.
--   * A classification is NOT a journal entry and NOT a payment. Nothing is posted anywhere.
--   * Only an imported bank row can be classified. A row in a match, a row that needs review
--     and an ignored row cannot.
--   * Decisions are append only. To undo a classification, add a 'cleared' decision with a reason.
--     A classified row cannot be matched to a record until the classification is cleared.
--   * Suggestions from rules or models are candidates. Only a person's decision classifies a row.
--
-- Apply after 005, 006 and 007. Safe to run twice. No BEGIN or COMMIT: the migration runner owns it.

-- ---------------------------------------------------------------- suggestions
CREATE TABLE IF NOT EXISTS bank_coding_suggestions (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    bank_transaction_id      UUID NOT NULL,
    candidate_coa_id         UUID,
    candidate_cost_centre_id UUID,
    evidence                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    method                   TEXT NOT NULL CHECK (method IN ('rule', 'vendor_history', 'semantic_match', 'model_candidate')),
    rule_version             TEXT NOT NULL CHECK (btrim(rule_version) <> ''),
    status                   TEXT NOT NULL DEFAULT 'suggested'
                             CHECK (status IN ('suggested', 'accepted', 'overridden', 'rejected', 'superseded')),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    CHECK (candidate_coa_id IS NOT NULL OR candidate_cost_centre_id IS NOT NULL),
    FOREIGN KEY (bank_transaction_id, tenant_id) REFERENCES bank_transactions(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (candidate_coa_id, tenant_id) REFERENCES coa_accounts(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (candidate_cost_centre_id, tenant_id) REFERENCES cost_centres(id, tenant_id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION bank_coding_suggestion_guard() RETURNS trigger AS $$
DECLARE
    tx_status TEXT;
BEGIN
    SELECT status INTO tx_status
    FROM bank_transactions WHERE id = NEW.bank_transaction_id AND tenant_id = NEW.tenant_id;
    IF tx_status IS NULL THEN
        RAISE EXCEPTION 'The bank row does not exist in this company.';
    END IF;
    IF tx_status NOT IN ('imported', 'proposed') THEN
        RAISE EXCEPTION 'This bank row cannot be classified in its current status.';
    END IF;
    IF NEW.candidate_coa_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM coa_accounts
        WHERE id = NEW.candidate_coa_id AND tenant_id = NEW.tenant_id AND is_active) THEN
        RAISE EXCEPTION 'The suggested account is inactive.';
    END IF;
    IF NEW.candidate_cost_centre_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM cost_centres
        WHERE id = NEW.candidate_cost_centre_id AND tenant_id = NEW.tenant_id AND is_active) THEN
        RAISE EXCEPTION 'The suggested cost centre is inactive.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bank_coding_suggestion_guard ON bank_coding_suggestions;
CREATE TRIGGER trg_bank_coding_suggestion_guard
    BEFORE INSERT ON bank_coding_suggestions
    FOR EACH ROW EXECUTE FUNCTION bank_coding_suggestion_guard();

-- ---------------------------------------------------------------- decisions
CREATE TABLE IF NOT EXISTS bank_coding_decisions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    bank_transaction_id UUID NOT NULL,
    suggestion_id       UUID,
    account_id          UUID,
    cost_centre_id      UUID,
    user_id             UUID NOT NULL,
    decision            TEXT NOT NULL CHECK (decision IN ('accepted', 'overridden', 'manual', 'cleared')),
    reason              TEXT,
    -- clock_timestamp() so two decisions in one transaction still sort in the order they were saved.
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, tenant_id),
    CONSTRAINT ck_bank_coding_account CHECK (
        (decision = 'cleared' AND account_id IS NULL AND cost_centre_id IS NULL AND suggestion_id IS NULL)
        OR (decision <> 'cleared' AND account_id IS NOT NULL)
    ),
    CONSTRAINT ck_bank_coding_reason CHECK (decision = 'accepted' OR (reason IS NOT NULL AND btrim(reason) <> '')),
    CONSTRAINT ck_bank_coding_suggestion CHECK (decision IN ('manual', 'cleared') OR suggestion_id IS NOT NULL),
    FOREIGN KEY (bank_transaction_id, tenant_id) REFERENCES bank_transactions(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (suggestion_id, tenant_id) REFERENCES bank_coding_suggestions(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (account_id, tenant_id) REFERENCES coa_accounts(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (cost_centre_id, tenant_id) REFERENCES cost_centres(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, tenant_id) REFERENCES users(id, tenant_id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION bank_coding_decision_guard() RETURNS trigger AS $$
DECLARE
    tx_status TEXT;
    sug_tx    UUID;
BEGIN
    SELECT status INTO tx_status
    FROM bank_transactions WHERE id = NEW.bank_transaction_id AND tenant_id = NEW.tenant_id;
    IF tx_status IS NULL THEN
        RAISE EXCEPTION 'The bank row does not exist in this company.';
    END IF;
    IF tx_status <> 'imported' THEN
        RAISE EXCEPTION 'Only an imported bank row without an open match can be classified.';
    END IF;
    IF NEW.decision <> 'cleared' THEN
        IF NOT EXISTS (SELECT 1 FROM coa_accounts
                       WHERE id = NEW.account_id AND tenant_id = NEW.tenant_id AND is_active) THEN
            RAISE EXCEPTION 'The chosen account is inactive.';
        END IF;
        IF NEW.cost_centre_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM cost_centres
            WHERE id = NEW.cost_centre_id AND tenant_id = NEW.tenant_id AND is_active) THEN
            RAISE EXCEPTION 'The chosen cost centre is inactive.';
        END IF;
    END IF;
    IF NEW.suggestion_id IS NOT NULL THEN
        SELECT bank_transaction_id INTO sug_tx
        FROM bank_coding_suggestions WHERE id = NEW.suggestion_id AND tenant_id = NEW.tenant_id;
        IF sug_tx IS DISTINCT FROM NEW.bank_transaction_id THEN
            RAISE EXCEPTION 'The suggestion belongs to a different bank row.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION bank_coding_decision_apply() RETURNS trigger AS $$
BEGIN
    IF NEW.suggestion_id IS NOT NULL THEN
        UPDATE bank_coding_suggestions
        SET status = CASE WHEN NEW.decision = 'accepted' THEN 'accepted' ELSE 'overridden' END
        WHERE id = NEW.suggestion_id AND tenant_id = NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bank_coding_decision_guard ON bank_coding_decisions;
CREATE TRIGGER trg_bank_coding_decision_guard
    BEFORE INSERT ON bank_coding_decisions
    FOR EACH ROW EXECUTE FUNCTION bank_coding_decision_guard();

DROP TRIGGER IF EXISTS trg_bank_coding_decision_apply ON bank_coding_decisions;
CREATE TRIGGER trg_bank_coding_decision_apply
    AFTER INSERT ON bank_coding_decisions
    FOR EACH ROW EXECUTE FUNCTION bank_coding_decision_apply();

DROP TRIGGER IF EXISTS trg_bank_coding_decisions_append_only ON bank_coding_decisions;
CREATE TRIGGER trg_bank_coding_decisions_append_only
    BEFORE UPDATE OR DELETE ON bank_coding_decisions
    FOR EACH ROW EXECUTE FUNCTION p1_append_only();

-- ---------------------------------------------------------------- views
-- The current classification of each bank row. A row whose latest decision is 'cleared' has none.
CREATE OR REPLACE VIEW current_bank_coding AS
SELECT id, tenant_id, bank_transaction_id, suggestion_id, account_id, cost_centre_id, user_id, decision, reason, created_at
FROM (
    SELECT DISTINCT ON (bank_transaction_id)
           id, tenant_id, bank_transaction_id, suggestion_id, account_id, cost_centre_id, user_id, decision, reason, created_at
    FROM bank_coding_decisions
    ORDER BY bank_transaction_id, created_at DESC, id DESC
) latest
WHERE decision <> 'cleared';

-- Bank rows that still need a person: not matched, not ignored and not classified.
CREATE OR REPLACE VIEW unmatched_bank_transactions AS
SELECT b.id, b.tenant_id, b.import_id, b.source_row, b.posted_date, b.value_date, b.description, b.reference,
       b.debit_decimal, b.credit_decimal, b.signed_amount_decimal, b.direction, b.currency, b.status, b.created_at
FROM bank_transactions b
WHERE b.status IN ('imported', 'needs_review', 'proposed')
  AND NOT EXISTS (SELECT 1 FROM current_bank_coding c WHERE c.bank_transaction_id = b.id);

-- A classified bank row cannot be matched to a record until its classification is cleared.
-- This replaces the function from migration 007 with the same checks plus the new one.
CREATE OR REPLACE FUNCTION recon_candidate_guard() RETURNS trigger AS $$
DECLARE
    tx_status     TEXT;
    rec_current   BOOLEAN;
    last_decision TEXT;
BEGIN
    SELECT status INTO tx_status
    FROM bank_transactions WHERE id = NEW.bank_transaction_id AND tenant_id = NEW.tenant_id;
    IF tx_status IS NULL THEN
        RAISE EXCEPTION 'The bank row does not exist in this company.';
    END IF;
    IF tx_status NOT IN ('imported', 'proposed') THEN
        RAISE EXCEPTION 'This bank row cannot be matched in its current status.';
    END IF;
    SELECT decision INTO last_decision
    FROM bank_coding_decisions
    WHERE bank_transaction_id = NEW.bank_transaction_id AND tenant_id = NEW.tenant_id
    ORDER BY created_at DESC, id DESC LIMIT 1;
    IF last_decision IS NOT NULL AND last_decision <> 'cleared' THEN
        RAISE EXCEPTION 'This bank row has been classified. Clear the classification before matching it.';
    END IF;
    SELECT is_current INTO rec_current
    FROM standardised_records WHERE id = NEW.record_id AND tenant_id = NEW.tenant_id;
    IF rec_current IS NULL OR NOT rec_current THEN
        RAISE EXCEPTION 'Only the current approved record can be matched.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE INDEX IF NOT EXISTS idx_bank_coding_suggestions_row ON bank_coding_suggestions (tenant_id, bank_transaction_id, status);
CREATE INDEX IF NOT EXISTS idx_bank_coding_decisions_row ON bank_coding_decisions (tenant_id, bank_transaction_id, created_at);

-- ============================================================================
-- Integrity rules for exceptions and validation results (migration 009). The same SQL is in database/migrations/009.
-- Safe to run twice.
-- ============================================================================

-- 009_integrity_exceptions_validation.sql
-- Jira task: Enforce Accounting Record Integrity and Data Consistency. Owner: Aleeya (Module 4).
--
-- What this file does, and why it is safe for the other modules:
--   1. An exception must belong to the same company as its document. The database now checks it.
--   2. An exception's resolved time always agrees with its status. The database fills it in or clears it,
--      so the assign and resolve code does not change. Assigning a resolved exception reopens it cleanly.
--   3. A validation result's flags must agree with its status (a passed result never blocks approval,
--      a failed result always does). The current validation code already writes them this way.
--   4. Validation results and exceptions record the extraction run they were made from. The database fills
--      it in from the document's current extraction, so nobody has to change code. When a document is
--      processed again, old results stay linked to the old run.
--   5. Two read only views list data that needs attention: field values that disagree with their state,
--      and open exceptions that came from an older extraction run.
--
-- Deliberately NOT done, because the code shows they would break validation or re-extraction:
--   * no uniqueness rule on validation_results or exceptions (one invoice can duplicate several earlier
--     ones, which correctly gives several identical EX-007 rows)
--   * no "one extraction per job" rule (re-extraction reuses the latest job)
--   * no hard rule on extracted field values (AI output is stored as it is and reported by a view instead)
--
-- Run it on an ISOLATED database first. Run integrity_009_precheck.sql before applying it anywhere.
-- The migration runner owns the transaction, so there is no BEGIN or COMMIT here. Safe to run twice.

-- ---------------------------------------------------------------- 1. exceptions belong to their document's company
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_exceptions_document_tenant') THEN
        ALTER TABLE exceptions ADD CONSTRAINT fk_exceptions_document_tenant
            FOREIGN KEY (document_id, tenant_id) REFERENCES documents (id, tenant_id) ON DELETE CASCADE;
    END IF;
END $$;

-- ---------------------------------------------------------------- 2. resolved time always agrees with the status
CREATE OR REPLACE FUNCTION exceptions_keep_resolved_time() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'resolved' THEN
        IF NEW.resolved_at IS NULL THEN
            NEW.resolved_at := now();
        END IF;
    ELSE
        NEW.resolved_at := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_exceptions_keep_resolved_time ON exceptions;
CREATE TRIGGER trg_exceptions_keep_resolved_time
    BEFORE INSERT OR UPDATE ON exceptions
    FOR EACH ROW EXECUTE FUNCTION exceptions_keep_resolved_time();

-- Old rows that were reopened without clearing the time: the trigger above would clear it anyway.
UPDATE exceptions SET resolved_at = NULL WHERE status <> 'resolved' AND resolved_at IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_exceptions_resolved_time') THEN
        ALTER TABLE exceptions ADD CONSTRAINT ck_exceptions_resolved_time
            CHECK ((status = 'resolved') = (resolved_at IS NOT NULL));
    END IF;
END $$;

-- ---------------------------------------------------------------- 3. validation result flags agree with the status
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_validation_passed_flags') THEN
        ALTER TABLE validation_results ADD CONSTRAINT ck_validation_passed_flags
            CHECK (status <> 'passed' OR (prevents_approval = FALSE AND requires_review = FALSE));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_validation_failed_blocks') THEN
        ALTER TABLE validation_results ADD CONSTRAINT ck_validation_failed_blocks
            CHECK (status <> 'failed' OR prevents_approval = TRUE);
    END IF;
END $$;

-- ---------------------------------------------------------------- 4. results and exceptions know their extraction run
ALTER TABLE validation_results ADD COLUMN IF NOT EXISTS extraction_id UUID;
ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS extraction_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_validation_results_extraction') THEN
        ALTER TABLE validation_results ADD CONSTRAINT fk_validation_results_extraction
            FOREIGN KEY (extraction_id, document_id) REFERENCES raw_extractions (id, document_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_exceptions_extraction') THEN
        ALTER TABLE exceptions ADD CONSTRAINT fk_exceptions_extraction
            FOREIGN KEY (extraction_id, document_id) REFERENCES raw_extractions (id, document_id) ON DELETE RESTRICT;
    END IF;
END $$;

-- When the code does not say which run a row belongs to, use the document's current run.
-- Rows that already exist keep an empty extraction_id: their run is unknown, and the database does not guess.
CREATE OR REPLACE FUNCTION fill_current_extraction() RETURNS trigger AS $$
BEGIN
    IF NEW.extraction_id IS NULL THEN
        SELECT id INTO NEW.extraction_id
        FROM raw_extractions WHERE document_id = NEW.document_id AND is_current;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validation_results_fill_extraction ON validation_results;
CREATE TRIGGER trg_validation_results_fill_extraction
    BEFORE INSERT ON validation_results
    FOR EACH ROW EXECUTE FUNCTION fill_current_extraction();

DROP TRIGGER IF EXISTS trg_exceptions_fill_extraction ON exceptions;
CREATE TRIGGER trg_exceptions_fill_extraction
    BEFORE INSERT ON exceptions
    FOR EACH ROW EXECUTE FUNCTION fill_current_extraction();

CREATE INDEX IF NOT EXISTS idx_validation_results_extraction ON validation_results (extraction_id) WHERE extraction_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_exceptions_extraction ON exceptions (extraction_id) WHERE extraction_id IS NOT NULL;

-- ---------------------------------------------------------------- 5. views for data that needs attention
-- Field values that disagree with their state. They are stored as they are and reported here, not rejected.
CREATE OR REPLACE VIEW field_prediction_inconsistencies AS
SELECT id, document_id, extraction_id, field_key, extraction_state, raw_value, normalized_value, current_value,
       CASE
           WHEN extraction_state = 'missing' AND (raw_value IS NOT NULL OR normalized_value IS NOT NULL) THEN 'missing_but_has_value'
           WHEN extraction_state = 'resolved' AND raw_value IS NULL AND normalized_value IS NULL THEN 'resolved_but_no_value'
           ELSE 'unclear_but_no_candidates'
       END AS problem
FROM field_predictions
WHERE (extraction_state = 'missing' AND (raw_value IS NOT NULL OR normalized_value IS NOT NULL))
   OR (extraction_state = 'resolved' AND raw_value IS NULL AND normalized_value IS NULL)
   OR (extraction_state IN ('ambiguous', 'conflict') AND jsonb_array_length(candidates) = 0);

-- Open exceptions that were made from an older extraction run than the document's current one.
CREATE OR REPLACE VIEW stale_open_exceptions AS
SELECT e.id, e.tenant_id, e.document_id, e.type, e.severity, e.status, e.message, e.created_at,
       e.extraction_id AS exception_extraction_id, cur.id AS current_extraction_id
FROM exceptions e
JOIN raw_extractions cur ON cur.document_id = e.document_id AND cur.is_current
WHERE e.status <> 'resolved' AND e.extraction_id IS NOT NULL AND e.extraction_id <> cur.id;

-- ============================================================================
-- Migration 010 (tables the backend needs: demo registry, backup codes protections) and
-- migration 011 (alignment with the final JSON, user columns, views, bank amount fix).
-- The same SQL is in database/migrations/010 and 011. Safe to run twice.
-- ============================================================================

-- 010_backend_required_tables.sql
-- Tables the newest backend code already depends on. Owner: Aleeya (Module 4).
-- Apply this FIRST, and soon: without these tables some backend routes fail with "relation does not exist".
--
--   1. benchmark_demo_tenants and benchmark_demo_documents (the registry from Jonathan's handoff package).
--      The accountant's document list, document detail and document file routes all test against them,
--      so a database without them breaks those routes for every user, not only the demo.
--      The definitions follow Jonathan's proposal (the columns the code reads), with one hardening:
--      the approver and the importer must belong to the same company as the registration.
--   2. mfa_backup_codes, used by POST /api/auth/backup-codes. A teammate already added the table to the schema
--      file. This creates it where it is missing and adds the protections the schema file's version lacks.
--
-- Safe to run twice. No BEGIN or COMMIT: the migration runner owns the transaction.

-- ---------------------------------------------------------------- 1. demo registry
CREATE TABLE IF NOT EXISTS benchmark_demo_tenants (
    tenant_id   UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by UUID NOT NULL,
    rationale   TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (approved_by, tenant_id) REFERENCES users(id, tenant_id)
);

CREATE TABLE IF NOT EXISTS benchmark_demo_documents (
    tenant_id           UUID NOT NULL REFERENCES benchmark_demo_tenants(tenant_id),
    dataset_id          TEXT NOT NULL,
    benchmark_revision  TEXT NOT NULL,
    demo_code           TEXT NOT NULL CHECK (demo_code ~ '^(INV|BIL|REC)[0-9]{5}$'),
    sample_id           TEXT NOT NULL,
    source_sha256       TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    gold_manifest_sha256 TEXT NOT NULL CHECK (gold_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    document_id         UUID NOT NULL,
    imported_by         UUID NOT NULL,
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, dataset_id, benchmark_revision, sample_id),
    UNIQUE (tenant_id, dataset_id, benchmark_revision, demo_code),
    UNIQUE (tenant_id, dataset_id, benchmark_revision, source_sha256),
    UNIQUE (tenant_id, document_id),
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (imported_by, tenant_id) REFERENCES users(id, tenant_id)
);

-- A registration is never edited. A change means a new benchmark revision.
CREATE OR REPLACE FUNCTION benchmark_demo_registration_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Demo source registrations are immutable; use a new benchmark revision';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS benchmark_demo_registration_immutable ON benchmark_demo_documents;
CREATE TRIGGER benchmark_demo_registration_immutable
    BEFORE UPDATE OR DELETE ON benchmark_demo_documents
    FOR EACH ROW EXECUTE FUNCTION benchmark_demo_registration_immutable();

-- ---------------------------------------------------------------- 2. backup codes
CREATE TABLE IF NOT EXISTS mfa_backup_codes (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash  TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mfa_backup_codes_user_unused ON mfa_backup_codes (user_id) WHERE used_at IS NULL;

-- Added on top of the schema file's version: the same code twice for one user, and a blank hash, are refused.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_mfa_backup_codes_user_hash') THEN
        ALTER TABLE mfa_backup_codes ADD CONSTRAINT uq_mfa_backup_codes_user_hash UNIQUE (user_id, code_hash);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mfa_backup_codes_hash') THEN
        ALTER TABLE mfa_backup_codes ADD CONSTRAINT ck_mfa_backup_codes_hash CHECK (btrim(code_hash) <> '');
    END IF;
END $$;

-- Only used_at may change, and it can only go from empty to filled.
CREATE OR REPLACE FUNCTION mfa_backup_code_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.user_id <> OLD.user_id OR NEW.code_hash <> OLD.code_hash OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'A backup code cannot be edited. Generate new codes instead.';
    END IF;
    IF OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at THEN
        RAISE EXCEPTION 'A used backup code cannot be reused.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_mfa_backup_code_guard ON mfa_backup_codes;
CREATE TRIGGER trg_mfa_backup_code_guard
    BEFORE UPDATE ON mfa_backup_codes
    FOR EACH ROW EXECUTE FUNCTION mfa_backup_code_guard();

-- 011_json_alignment_and_user_columns.sql
-- Owner: Aleeya (Module 4). Apply after 010. Safe to run twice. No BEGIN or COMMIT: the migration runner owns it.
--
-- 1. Corrections can carry a reason (the review JSON requires a rationale for every correction).
-- 2. Line items can store the extra line cells from the prediction JSON, and the service's row id.
-- 3. An extraction records its predicted document type, extractor version, configuration hash and capture time,
--    filled automatically from raw_json when those keys are there.
-- 4. The original extraction is protected: only is_current may change.
-- 5. Exceptions record the user who uploaded the document that caused them, and a view shows names.
-- 6. A view gives the cash summary per company and currency (money in, money out, verified in, verified out).
-- 7. A bank import can be 'discarded', and it cannot be discarded or reverted while its rows are in a match.
-- 8. A bank row saved with 0.00 in its unused debit or credit column is stored with that column empty.
--
-- Nothing here renames or removes a column, so the running backend keeps working.

-- ---------------------------------------------------------------- 1. a reason for every correction
ALTER TABLE field_corrections ADD COLUMN IF NOT EXISTS reason TEXT CHECK (reason IS NULL OR btrim(reason) <> '');
ALTER TABLE line_item_corrections ADD COLUMN IF NOT EXISTS reason TEXT CHECK (reason IS NULL OR btrim(reason) <> '');

-- ---------------------------------------------------------------- 2. extra line cells and the service row id
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS extra_fields JSONB NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(extra_fields) = 'object');
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS prediction_row_id TEXT;

-- ---------------------------------------------------------------- 3 and 4. extraction metadata and protection
-- The protection is dropped first so this file can run twice: it must not block its own backfill below.
DROP TRIGGER IF EXISTS trg_raw_extractions_protect ON raw_extractions;

ALTER TABLE raw_extractions ADD COLUMN IF NOT EXISTS predicted_document_type TEXT
    CHECK (predicted_document_type IS NULL OR predicted_document_type IN ('invoice', 'bill', 'receipt', 'credit_note', 'debit_note', 'unknown'));
ALTER TABLE raw_extractions ADD COLUMN IF NOT EXISTS extractor_version TEXT;
ALTER TABLE raw_extractions ADD COLUMN IF NOT EXISTS configuration_sha256 TEXT
    CHECK (configuration_sha256 IS NULL OR configuration_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE raw_extractions ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ;

-- New extractions: copy the metadata out of raw_json when the keys exist and are valid. Anything else stays empty.
CREATE OR REPLACE FUNCTION raw_extractions_fill_metadata() RETURNS trigger AS $$
DECLARE
    v TEXT;
BEGIN
    IF NEW.predicted_document_type IS NULL THEN
        v := NEW.raw_json->>'predicted_document_type';
        IF v IN ('invoice', 'bill', 'receipt', 'credit_note', 'debit_note', 'unknown') THEN
            NEW.predicted_document_type := v;
        END IF;
    END IF;
    IF NEW.extractor_version IS NULL THEN
        NEW.extractor_version := NEW.raw_json->>'extractor_version';
    END IF;
    IF NEW.configuration_sha256 IS NULL THEN
        v := NEW.raw_json->>'configuration_sha256';
        IF v ~ '^[0-9a-f]{64}$' THEN
            NEW.configuration_sha256 := v;
        END IF;
    END IF;
    IF NEW.captured_at IS NULL THEN
        BEGIN
            NEW.captured_at := (NEW.raw_json->>'captured_at')::timestamptz;
        EXCEPTION WHEN others THEN
            NEW.captured_at := NULL;
        END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_raw_extractions_fill_metadata ON raw_extractions;
CREATE TRIGGER trg_raw_extractions_fill_metadata
    BEFORE INSERT ON raw_extractions
    FOR EACH ROW EXECUTE FUNCTION raw_extractions_fill_metadata();

-- Existing extractions: the same copy, once. captured_at is left empty for old rows.
UPDATE raw_extractions
SET predicted_document_type = CASE WHEN raw_json->>'predicted_document_type' IN ('invoice', 'bill', 'receipt', 'credit_note', 'debit_note', 'unknown')
                                   THEN raw_json->>'predicted_document_type' END,
    extractor_version = raw_json->>'extractor_version',
    configuration_sha256 = CASE WHEN raw_json->>'configuration_sha256' ~ '^[0-9a-f]{64}$' THEN raw_json->>'configuration_sha256' END
WHERE predicted_document_type IS NULL AND extractor_version IS NULL AND configuration_sha256 IS NULL
  AND (raw_json->>'predicted_document_type' IS NOT NULL OR raw_json->>'extractor_version' IS NOT NULL OR raw_json->>'configuration_sha256' IS NOT NULL);

-- The original prediction stays as it was. The only change the code makes is switching is_current.
CREATE OR REPLACE FUNCTION raw_extractions_protect() RETURNS trigger AS $$
BEGIN
    IF (to_jsonb(NEW) - 'is_current') <> (to_jsonb(OLD) - 'is_current') THEN
        RAISE EXCEPTION 'An original extraction cannot be edited. Only is_current can change.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_raw_extractions_protect
    BEFORE UPDATE ON raw_extractions
    FOR EACH ROW EXECUTE FUNCTION raw_extractions_protect();

-- ---------------------------------------------------------------- 5. the user who uploaded the document behind an exception
ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS uploaded_by UUID REFERENCES users(id) ON DELETE SET NULL;

CREATE OR REPLACE FUNCTION exceptions_fill_uploaded_by() RETURNS trigger AS $$
BEGIN
    IF NEW.uploaded_by IS NULL THEN
        SELECT d.owner_id INTO NEW.uploaded_by
        FROM documents d JOIN users u ON u.id = d.owner_id AND u.tenant_id = d.tenant_id
        WHERE d.id = NEW.document_id AND d.tenant_id = NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_exceptions_fill_uploaded_by ON exceptions;
CREATE TRIGGER trg_exceptions_fill_uploaded_by
    BEFORE INSERT ON exceptions
    FOR EACH ROW EXECUTE FUNCTION exceptions_fill_uploaded_by();

-- Existing exceptions get the document's owner as the uploader.
UPDATE exceptions e SET uploaded_by = d.owner_id
FROM documents d JOIN users u ON u.id = d.owner_id AND u.tenant_id = d.tenant_id
WHERE e.document_id = d.id AND e.tenant_id = d.tenant_id AND e.uploaded_by IS NULL;

CREATE INDEX IF NOT EXISTS idx_exceptions_uploaded_by ON exceptions (uploaded_by) WHERE uploaded_by IS NOT NULL;

-- Each exception with the names of the people involved.
CREATE OR REPLACE VIEW exceptions_with_user AS
SELECT e.id, e.tenant_id, e.document_id, d.document_code, d.original_filename,
       e.type, e.severity, e.status, e.message, e.created_at, e.resolved_at,
       e.uploaded_by, up.name AS uploaded_by_name, up.email AS uploaded_by_email,
       e.assigned_to, asg.name AS assigned_to_name
FROM exceptions e
JOIN documents d ON d.id = e.document_id
LEFT JOIN users up ON up.id = e.uploaded_by
LEFT JOIN users asg ON asg.id = e.assigned_to;

-- ---------------------------------------------------------------- 6. cash summary per company and currency
-- Money in is the sum of credits, money out is the sum of debits, and "verified" means the row is matched.
CREATE OR REPLACE VIEW bank_cash_summary AS
SELECT tenant_id, currency,
       COALESCE(sum(credit_decimal), 0) AS money_in,
       COALESCE(sum(debit_decimal), 0) AS money_out,
       COALESCE(sum(credit_decimal) FILTER (WHERE status = 'matched'), 0) AS verified_in,
       COALESCE(sum(debit_decimal) FILTER (WHERE status = 'matched'), 0) AS verified_out
FROM bank_transactions
WHERE direction IS NOT NULL AND status <> 'ignored'
GROUP BY tenant_id, currency;

-- ---------------------------------------------------------------- 7. discarding a bank import
DO $$
DECLARE
    c RECORD;
BEGIN
    -- Drop the old status rule whatever it is called: the one that lists the statuses but not 'discarded'.
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'bank_imports'::regclass AND contype = 'c'
               AND position('previewed' in pg_get_constraintdef(oid)) > 0 AND position('discarded' in pg_get_constraintdef(oid)) = 0
    LOOP
        EXECUTE 'ALTER TABLE bank_imports DROP CONSTRAINT ' || quote_ident(c.conname);
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_bank_imports_status') THEN
        ALTER TABLE bank_imports ADD CONSTRAINT ck_bank_imports_status
            CHECK (status IN ('previewed', 'confirmed', 'failed', 'reverted', 'discarded'));
    END IF;
END $$;

CREATE OR REPLACE FUNCTION bank_import_status_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.status IN ('discarded', 'reverted') AND OLD.status IS DISTINCT FROM NEW.status THEN
        IF EXISTS (SELECT 1 FROM bank_transactions b
                   WHERE b.import_id = NEW.id AND b.tenant_id = NEW.tenant_id AND b.status IN ('proposed', 'matched')) THEN
            RAISE EXCEPTION 'Reject or reverse the matches before discarding or reverting this import.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bank_import_status_guard ON bank_imports;
CREATE TRIGGER trg_bank_import_status_guard
    BEFORE UPDATE OF status ON bank_imports
    FOR EACH ROW EXECUTE FUNCTION bank_import_status_guard();

-- ---------------------------------------------------------------- 8. zero in the unused debit or credit column
-- Bank files usually show 0.00 in the column that does not apply. The backend passes that 0.00 straight through,
-- and the rule on bank rows (a Money Out row has no credit, a Money In row has no debit) would refuse the whole import.
-- A zero amount means "nothing in this column", so it is stored as empty.
CREATE OR REPLACE FUNCTION bank_transactions_normalise_amounts() RETURNS trigger AS $$
BEGIN
    IF NEW.debit_decimal = 0 THEN
        NEW.debit_decimal := NULL;
    END IF;
    IF NEW.credit_decimal = 0 THEN
        NEW.credit_decimal := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bank_transactions_normalise_amounts ON bank_transactions;
CREATE TRIGGER trg_bank_transactions_normalise_amounts
    BEFORE INSERT ON bank_transactions
    FOR EACH ROW EXECUTE FUNCTION bank_transactions_normalise_amounts();

