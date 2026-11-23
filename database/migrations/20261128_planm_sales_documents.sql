-- Plan M (S1): the sales flow KNT issues, printable copies, company identity and one naming rule.
-- Idempotent; the same text is appended to the consolidated schema file.
--
-- * Debit notes (sales and supplier) join the document types.
-- * Quotations move draft -> sent -> accepted / expired (-> converted to an invoice); they never post.
--   Quotations a previous version marked "ready" are "sent".
-- * Every issued document keeps an unchangeable PDF per revision (erp_document_files, append-only).
-- * The company profile carries the identity printed on documents and used to tell sales from purchases.
-- * Contact codes (the CONTACT part of {YYYY-MM-DD}_{CONTACT}_{TYPE}_{NUMBER}.{ext}) are unique per company.

-- ---------------------------------------------------------------- document types and statuses
ALTER TABLE erp_document_headers DROP CONSTRAINT IF EXISTS erp_document_headers_document_type_check;
ALTER TABLE erp_document_headers ADD CONSTRAINT erp_document_headers_document_type_check CHECK (document_type IN (
    'quotation','sales-order','delivery-order','invoice','credit-note','debit-note','payment','refund','purchase-order',
    'goods-received','bill','purchase-credit-note','purchase-debit-note','purchase-payment','purchase-refund','money-in',
    'money-out','bank-transfer','stock-adjustment','stock-transfer','journal-entry','contra','deemed-payment','store-order',
    'minipos-order'));

ALTER TABLE erp_document_headers DROP CONSTRAINT IF EXISTS erp_document_headers_status_check;
UPDATE erp_document_headers SET status='sent', updated_at=now() WHERE document_type='quotation' AND status='ready';
ALTER TABLE erp_document_headers ADD CONSTRAINT erp_document_headers_status_check CHECK (
    status IN ('draft','pending_approval','ready','void','sent','accepted','expired')
    AND (document_type = 'quotation' OR status NOT IN ('sent','accepted','expired'))
    AND (document_type <> 'quotation' OR status <> 'ready'));

-- ---------------------------------------------------------------- issued copies (B8)
CREATE TABLE IF NOT EXISTS erp_document_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id UUID NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    -- What the copy shows: the issued document, or the same document marked void.
    reason TEXT NOT NULL CHECK (reason IN ('issued','void')),
    file_name TEXT NOT NULL CHECK (btrim(file_name) <> ''),
    mime_type TEXT NOT NULL DEFAULT 'application/pdf',
    content BYTEA NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, document_id, revision),
    -- Issued documents are never deleted (the API deletes drafts only); a local reset that removes a company's
    -- documents takes their copies with them.
    FOREIGN KEY (document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_erp_document_files_document ON erp_document_files(tenant_id, document_id, revision DESC);

CREATE OR REPLACE FUNCTION erp_document_files_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Only the cascade from deleting the document itself may remove a copy (its header is already gone then).
    IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM erp_document_headers h
            WHERE h.id = OLD.document_id AND h.tenant_id = OLD.tenant_id) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'Issued document copies are unchangeable; render a new revision instead.';
END $$;
ALTER TABLE erp_document_files DROP CONSTRAINT IF EXISTS erp_document_files_document_id_tenant_id_fkey;
ALTER TABLE erp_document_files ADD CONSTRAINT erp_document_files_document_id_tenant_id_fkey
    FOREIGN KEY (document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE CASCADE;
DROP TRIGGER IF EXISTS trg_erp_document_files_append_only ON erp_document_files;
CREATE TRIGGER trg_erp_document_files_append_only BEFORE UPDATE OR DELETE ON erp_document_files
    FOR EACH ROW EXECUTE FUNCTION erp_document_files_append_only();

-- ---------------------------------------------------------------- company identity (M1 letterhead, M2 direction)
-- {"tin","sstNumber","address","phone","email","bankName","bankAccountName","bankAccountNumber","paymentTerms","documentTerms"}
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS profile JSONB NOT NULL DEFAULT '{}'::jsonb;

-- ---------------------------------------------------------------- contact codes (M4)
-- Older rows may repeat a code: keep it on the oldest row, clear it on the others (the API suggests a new one).
WITH ranked AS (
    SELECT id, row_number() OVER (PARTITION BY tenant_id, upper(btrim(contact_code)) ORDER BY created_at, id) AS n
    FROM vendors WHERE contact_code IS NOT NULL AND btrim(contact_code) <> ''
)
UPDATE vendors v SET contact_code = NULL FROM ranked r WHERE r.id = v.id AND r.n > 1;
WITH ranked AS (
    SELECT id, row_number() OVER (PARTITION BY tenant_id, upper(btrim(contact_code)) ORDER BY created_at, id) AS n
    FROM customers WHERE contact_code IS NOT NULL AND btrim(contact_code) <> ''
)
UPDATE customers c SET contact_code = NULL FROM ranked r WHERE r.id = c.id AND r.n > 1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_vendors_contact_code ON vendors(tenant_id, upper(btrim(contact_code)))
    WHERE contact_code IS NOT NULL AND btrim(contact_code) <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_customers_contact_code ON customers(tenant_id, upper(btrim(contact_code)))
    WHERE contact_code IS NOT NULL AND btrim(contact_code) <> '';

-- ---------------------------------------------------------------- B1: own sales documents captured by mistake
-- Captured rows are append-only (extractions, records), so a document the company itself issued is excluded rather
-- than deleted: it leaves the Inbox, review lanes and benchmark, and keeps its history for audit.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS excluded_at TIMESTAMPTZ;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS excluded_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_documents_included ON documents(tenant_id, uploaded_at DESC) WHERE excluded_at IS NULL;

-- A quotation has no approval step (draft -> sent): older ones waiting for approval go back to draft.
UPDATE erp_document_headers SET status='draft', updated_at=now() WHERE document_type='quotation' AND status='pending_approval';
