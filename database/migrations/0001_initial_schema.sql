-- Migration 0001: Initial Canonical Schema (Non-Destructive)
-- Accounting Intelligence (Group 14) - Aleeya's Canonical Baseline
-- Safe for bootstrap and versioned migration without dropping existing data.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Sequences
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS tenant_code_seq START 1;
CREATE SEQUENCE IF NOT EXISTS document_invoice_seq START 100001;
CREATE SEQUENCE IF NOT EXISTS document_bill_seq START 100001;
CREATE SEQUENCE IF NOT EXISTS document_receipt_seq START 100001;
CREATE SEQUENCE IF NOT EXISTS record_code_seq START 10001;

-- ---------------------------------------------------------------------------
-- Companies (tenants)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
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

CREATE OR REPLACE FUNCTION set_tenant_code() RETURNS trigger AS $$
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

DROP TRIGGER IF EXISTS trg_set_tenant_code ON tenants;
CREATE TRIGGER trg_set_tenant_code
    BEFORE INSERT ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_tenant_code();

-- ---------------------------------------------------------------------------
-- Users
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
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
    totp_secret_enc        TEXT,
    totp_enabled           BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (
        (role = 'super_admin' AND tenant_id IS NULL)
        OR (role IN ('local_admin', 'accountant') AND tenant_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email ON users (lower(email));

-- ---------------------------------------------------------------------------
-- Company access requests
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS company_requests (
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
    tenant_id            UUID REFERENCES tenants(id) ON DELETE SET NULL,
    reviewed_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    submitted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at          TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_company_requests_pending_email
    ON company_requests (lower(contact_email)) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- Seat limit increase requests
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account_limit_requests (
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_limit_requests_pending
    ON account_limit_requests (tenant_id) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- One time auth tokens
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth_tokens (
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
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
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
        CHECK (file_size_bytes > 0 AND file_size_bytes <= 20971520),
    file_hash_sha256          TEXT NOT NULL,
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
    CONSTRAINT uq_documents_id_tenant UNIQUE (id, tenant_id)
);

CREATE OR REPLACE FUNCTION set_document_code() RETURNS trigger AS $$
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

DROP TRIGGER IF EXISTS trg_set_document_code ON documents;
CREATE TRIGGER trg_set_document_code
    BEFORE INSERT ON documents
    FOR EACH ROW EXECUTE FUNCTION set_document_code();

-- ---------------------------------------------------------------------------
-- Raw Extractions (Immutable per-run model)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_extractions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id       UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    run_number        INTEGER NOT NULL CHECK (run_number >= 1),
    is_current        BOOLEAN NOT NULL DEFAULT FALSE,
    routing_decision  TEXT NOT NULL
        CHECK (routing_decision IN ('parser_only', 'vlm_candidate', 'human_review')),
    service_run_id    TEXT UNIQUE,
    schema_version    TEXT,
    pending_job_id    UUID,
    raw_json          JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_raw_extractions_doc_run UNIQUE (document_id, run_number),
    CONSTRAINT uq_raw_extractions_id_doc UNIQUE (id, document_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_extractions_current
    ON raw_extractions (document_id) WHERE is_current;

-- ---------------------------------------------------------------------------
-- Field Predictions (Keyed by (extraction_id, field_key))
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS field_predictions (
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
    CONSTRAINT fk_field_predictions_extraction
        FOREIGN KEY (extraction_id, document_id) REFERENCES raw_extractions (id, document_id) ON DELETE RESTRICT,
    CONSTRAINT uq_field_predictions_key UNIQUE (extraction_id, field_key),
    CONSTRAINT chk_field_predictions_candidate_review
        CHECK (resolution IS DISTINCT FROM 'candidate_for_review' OR current_value IS NULL OR corrected_at IS NOT NULL)
);

CREATE OR REPLACE FUNCTION protect_field_predictions() RETURNS trigger AS $$
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

DROP TRIGGER IF EXISTS trg_field_predictions_protect ON field_predictions;
CREATE TRIGGER trg_field_predictions_protect
    BEFORE UPDATE ON field_predictions
    FOR EACH ROW EXECUTE FUNCTION protect_field_predictions();

-- ---------------------------------------------------------------------------
-- Field Corrections History (Append-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS field_corrections (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    field_prediction_id  UUID NOT NULL REFERENCES field_predictions(id) ON DELETE RESTRICT,
    old_value            TEXT,
    new_value            TEXT,
    corrected_by         UUID REFERENCES users(id),
    corrected_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION log_field_correction() RETURNS trigger AS $$
BEGIN
    IF NEW.current_value IS DISTINCT FROM OLD.current_value THEN
        INSERT INTO field_corrections (field_prediction_id, old_value, new_value, corrected_by, corrected_at)
        VALUES (NEW.id, OLD.current_value, NEW.current_value, NEW.corrected_by, COALESCE(NEW.corrected_at, clock_timestamp()));
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_field_predictions_log ON field_predictions;
CREATE TRIGGER trg_field_predictions_log
    AFTER UPDATE ON field_predictions
    FOR EACH ROW EXECUTE FUNCTION log_field_correction();

CREATE OR REPLACE FUNCTION prevent_correction_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'The correction history cannot be changed or deleted.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_field_corrections_append_only ON field_corrections;
CREATE TRIGGER trg_field_corrections_append_only
    BEFORE UPDATE OR DELETE ON field_corrections
    FOR EACH ROW EXECUTE FUNCTION prevent_correction_change();

-- ---------------------------------------------------------------------------
-- Extraction Switch & Correction Carry-Forward Function
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION switch_current_extraction(new_extraction UUID) RETURNS INTEGER AS $$
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
-- Line items
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS line_items (
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
    CONSTRAINT fk_line_items_extraction
        FOREIGN KEY (extraction_id, document_id) REFERENCES raw_extractions (id, document_id) ON DELETE RESTRICT,
    CONSTRAINT uq_line_items_run_line UNIQUE (extraction_id, line_number)
);

-- ---------------------------------------------------------------------------
-- Exceptions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exceptions (
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
-- Vendors
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vendors (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendor_name          TEXT NOT NULL,
    registration_number  TEXT,
    address              TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_vendors_tenant_name UNIQUE (tenant_id, vendor_name)
);

-- ---------------------------------------------------------------------------
-- Standardised records (Immutable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS standardised_records (
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

CREATE OR REPLACE FUNCTION set_record_code() RETURNS trigger AS $$
BEGIN
    IF NEW.record_code IS NULL OR NEW.record_code = '' THEN
        NEW.record_code := 'REC-' || nextval('record_code_seq')::text;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_set_record_code ON standardised_records;
CREATE TRIGGER trg_set_record_code
    BEFORE INSERT ON standardised_records
    FOR EACH ROW EXECUTE FUNCTION set_record_code();

CREATE OR REPLACE FUNCTION prevent_record_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Standardised records are immutable.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_records_immutable ON standardised_records;
CREATE TRIGGER trg_records_immutable
    BEFORE UPDATE ON standardised_records
    FOR EACH ROW EXECUTE FUNCTION prevent_record_update();

-- ---------------------------------------------------------------------------
-- Validation rules
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validation_rules (
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
-- Workspace settings
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace_settings (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID REFERENCES tenants(id) ON DELETE CASCADE,
    timezone       TEXT NOT NULL DEFAULT 'Asia/Kuala_Lumpur',
    notifications  TEXT NOT NULL DEFAULT 'enabled',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_settings_scope
    ON workspace_settings ((COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)));

-- ---------------------------------------------------------------------------
-- Audit log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
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
-- Authentication, MFA, and Web Sessions
-- ---------------------------------------------------------------------------
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
    CONSTRAINT uq_mfa_recovery_target_status UNIQUE (target_user_id, status)
);

-- ---------------------------------------------------------------------------
-- Common Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id, purpose);
CREATE INDEX IF NOT EXISTS idx_documents_tenant_status ON documents(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents(owner_id);
CREATE INDEX IF NOT EXISTS idx_documents_file_hash ON documents(tenant_id, file_hash_sha256);
CREATE INDEX IF NOT EXISTS idx_documents_assigned ON documents(assigned_to);
CREATE INDEX IF NOT EXISTS idx_raw_extractions_document ON raw_extractions(document_id);
CREATE INDEX IF NOT EXISTS idx_field_predictions_document ON field_predictions(document_id);
CREATE INDEX IF NOT EXISTS idx_field_predictions_extraction ON field_predictions(extraction_id);
CREATE INDEX IF NOT EXISTS idx_field_corrections_prediction ON field_corrections(field_prediction_id);
CREATE INDEX IF NOT EXISTS idx_field_predictions_key_value ON field_predictions(field_key, current_value);
CREATE INDEX IF NOT EXISTS idx_line_items_document ON line_items(document_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_document ON exceptions(document_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_tenant_status ON exceptions(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_records_tenant_created ON standardised_records(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_records_duplicate_lookup ON standardised_records(tenant_id, supplier_name, invoice_number);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_created ON audit_logs(tenant_id, created_at);
