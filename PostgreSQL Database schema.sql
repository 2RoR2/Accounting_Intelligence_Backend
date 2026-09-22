-- PostgreSQL Database schema
-- Accounting Intelligence (Group 14)
--
-- Implements multi-company accounts, seat limits and audited support-view access.
-- Run this one file to reset and rebuild everything from scratch.

DROP TABLE IF EXISTS audit_logs, line_items, standardised_records,
    raw_extractions, vendors, documents, users, tenants CASCADE;

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- needed for gen_random_uuid()

-- One row per client company. seat_limit supports the "choose its seat
-- limit" step in the Super Admin approval flow.
CREATE TABLE tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    seat_limit  INTEGER NOT NULL DEFAULT 10,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per person who can log in. status tracks the activation
-- lifecycle (invited -> active -> disabled) separately from role,
-- since a pending invitation still reserves a seat.
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    email       TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL CHECK (role IN ('super_admin', 'local_admin', 'user')),
    status      TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('invited', 'active', 'disabled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (role = 'super_admin' AND tenant_id IS NULL)
        OR (role IN ('local_admin', 'user') AND tenant_id IS NOT NULL)
    )
);

-- Every uploaded file, tracked through the whole pipeline, scoped to
-- the company that uploaded it. correction_note holds the reviewer's
-- explanation when editing a flagged document.
CREATE TABLE documents (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    original_filename       TEXT NOT NULL,
    storage_path            TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (status IN (
            'uploaded', 'queued', 'processing', 'validated',
            'exception', 'reviewed', 'rejected', 'stored', 'failed'
        )),
    reviewer_action         TEXT
        CHECK (reviewer_action IN ('approve', 'edit', 'reject')),
    correction_note         TEXT,
    reviewed_at             TIMESTAMPTZ,
    uploaded_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    queued_at               TIMESTAMPTZ,
    extraction_started_at   TIMESTAMPTZ,
    extraction_completed_at TIMESTAMPTZ,
    validated_at            TIMESTAMPTZ,
    stored_at               TIMESTAMPTZ
);

-- One row per distinct supplier, per company.
CREATE TABLE vendors (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    vendor_name  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, vendor_name)
);

-- Raw output from whichever extraction path was used.
CREATE TABLE raw_extractions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    raw_json          JSONB NOT NULL,
    extraction_path   TEXT NOT NULL CHECK (extraction_path IN ('A', 'B', 'C')),
    validation_errors JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The final, validated record. One per document.
CREATE TABLE standardised_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    vendor_id       UUID NOT NULL REFERENCES vendors(id),
    invoice_number  TEXT NOT NULL,
    invoice_date    DATE NOT NULL,
    due_date        DATE,
    subtotal        NUMERIC(14,2) NOT NULL,
    tax             NUMERIC(14,2) NOT NULL,
    total           NUMERIC(14,2) NOT NULL,
    currency        CHAR(3) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Individual line items belonging to a standardised record.
CREATE TABLE line_items (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id    UUID NOT NULL REFERENCES standardised_records(id) ON DELETE CASCADE,
    description  TEXT NOT NULL,
    quantity     NUMERIC(14,3) NOT NULL,
    unit_price   NUMERIC(14,4) NOT NULL,
    amount       NUMERIC(14,2) NOT NULL
);

-- Records every SAIC "Support View" access to a record's financial
-- detail, per the frontend's audited-access requirement.
CREATE TABLE audit_logs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id),
    record_id    UUID NOT NULL REFERENCES standardised_records(id),
    reason       TEXT NOT NULL,
    accessed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for the lookups the app will actually do often.
CREATE INDEX idx_documents_status ON documents(status);
CREATE INDEX idx_documents_tenant ON documents(tenant_id);
CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_vendors_tenant_name ON vendors(tenant_id, vendor_name);
CREATE INDEX idx_raw_extractions_document_id ON raw_extractions(document_id);
CREATE INDEX idx_standardised_records_vendor_invoice
    ON standardised_records(vendor_id, invoice_number);
CREATE INDEX idx_audit_logs_record ON audit_logs(record_id);
