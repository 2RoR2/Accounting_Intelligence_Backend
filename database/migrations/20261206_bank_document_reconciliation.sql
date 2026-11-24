-- P3-lite: reconcile a statement line against the typed payment that already posted its cash (a payment
-- voucher, payment received, refund or money in/out). The line points at the document through
-- bank_transactions.recorded_document_id, exactly like a money document recorded from the line, so trace,
-- tally and "payments to post" treat it the same way. This table records that the link was a reconciliation
-- (no new allocation or journal), which is what makes it safe to undo.
CREATE TABLE IF NOT EXISTS bank_document_reconciliations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    bank_transaction_id UUID NOT NULL,
    document_id UUID NOT NULL,
    match_label TEXT NOT NULL CHECK (match_label IN ('exact', 'reference', 'amount+date', 'memory', 'manual')),
    match_reason TEXT,
    created_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    undone_at TIMESTAMPTZ,
    undone_by UUID REFERENCES users(id) ON DELETE RESTRICT,
    undo_reason TEXT,
    UNIQUE (id, tenant_id),
    FOREIGN KEY (bank_transaction_id, tenant_id) REFERENCES bank_transactions(id, tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id) ON DELETE RESTRICT,
    CHECK (undone_at IS NULL OR btrim(COALESCE(undo_reason, '')) <> '')
);
-- One active reconciliation per bank line and per document.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_document_reconciliation_line
    ON bank_document_reconciliations (bank_transaction_id) WHERE undone_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_document_reconciliation_document
    ON bank_document_reconciliations (document_id) WHERE undone_at IS NULL;

-- The imported evidence stays immutable. A recorded-document link changes only when its document is voided, or
-- when an active reconciliation link (not a money document recorded from the line) is undone.
CREATE OR REPLACE FUNCTION bank_transaction_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('proposed', 'matched') THEN
            RAISE EXCEPTION 'A bank row that is part of a match cannot be deleted.';
        END IF;
        RETURN OLD;
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status', 'recorded_document_id'])
        <> (to_jsonb(OLD) - ARRAY['status', 'recorded_document_id']) THEN
        RAISE EXCEPTION 'A bank row is evidence and cannot be edited. Only its status can change.';
    END IF;
    IF NEW.recorded_document_id IS DISTINCT FROM OLD.recorded_document_id
        AND (OLD.recorded_document_id IS NOT NULL OR OLD.status IN ('matched', 'ignored'))
        AND NOT (NEW.recorded_document_id IS NULL AND NEW.status = 'imported' AND (
            EXISTS (
                SELECT 1 FROM erp_document_headers h
                WHERE h.id = OLD.recorded_document_id AND h.tenant_id = OLD.tenant_id AND h.status = 'void'
            ) OR EXISTS (
                SELECT 1 FROM bank_document_reconciliations r
                WHERE r.tenant_id = OLD.tenant_id AND r.bank_transaction_id = OLD.id
                    AND r.document_id = OLD.recorded_document_id AND r.undone_at IS NULL
            )
        )) THEN
        RAISE EXCEPTION 'A recorded bank document link cannot be changed.';
    END IF;
    IF NEW.recorded_document_id IS NOT NULL AND NEW.status <> 'matched' THEN
        RAISE EXCEPTION 'A bank transaction with a recorded document must be matched.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
