-- A bank line matched by hand to the money document that already recorded it (a payment voucher, a payment received)
-- can be unmatched with a reason; a line whose money document was created from the line stays fixed. Idempotent; the
-- same text is appended to the consolidated schema file.
--
-- bank_document_matches says how a link was made: an active row (unmatched_at IS NULL) is a manual match. Rows are kept
-- for audit: only the unmatch fields can be set, once, and rows are never deleted.

CREATE TABLE IF NOT EXISTS bank_document_matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    bank_transaction_id UUID NOT NULL REFERENCES bank_transactions(id) ON DELETE RESTRICT,
    document_id UUID NOT NULL REFERENCES erp_document_headers(id) ON DELETE RESTRICT,
    matched_by UUID REFERENCES users(id) ON DELETE SET NULL,
    matched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    unmatched_by UUID REFERENCES users(id) ON DELETE SET NULL,
    unmatched_at TIMESTAMPTZ,
    unmatch_reason TEXT,
    CHECK (unmatched_at IS NULL OR char_length(btrim(COALESCE(unmatch_reason, ''))) >= 5)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_document_matches_active_line
    ON bank_document_matches (tenant_id, bank_transaction_id) WHERE unmatched_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_document_matches_active_document
    ON bank_document_matches (tenant_id, document_id) WHERE unmatched_at IS NULL;

CREATE OR REPLACE FUNCTION bank_document_match_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'A bank document match is kept for audit and cannot be deleted.';
    END IF;
    IF OLD.unmatched_at IS NOT NULL
        OR (to_jsonb(NEW) - ARRAY['unmatched_by', 'unmatched_at', 'unmatch_reason'])
            <> (to_jsonb(OLD) - ARRAY['unmatched_by', 'unmatched_at', 'unmatch_reason']) THEN
        RAISE EXCEPTION 'A bank document match can only be undone once, with a reason.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bank_document_match_guard ON bank_document_matches;
CREATE TRIGGER trg_bank_document_match_guard
    BEFORE UPDATE OR DELETE ON bank_document_matches
    FOR EACH ROW EXECUTE FUNCTION bank_document_match_guard();

-- The 26 Nov guard, plus one exception: a link made by hand (an active match row) may be cleared, returning the line
-- to imported. Evidence columns stay immutable and a link created from the line still cannot change.
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
            )
            OR EXISTS (
                SELECT 1 FROM bank_document_matches m
                WHERE m.tenant_id = OLD.tenant_id AND m.bank_transaction_id = OLD.id
                    AND m.document_id = OLD.recorded_document_id AND m.unmatched_at IS NULL
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

-- One money document is the bank copy of at most one line. Skipped (and left to a person) if old data breaks it.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM bank_transactions WHERE recorded_document_id IS NOT NULL
        GROUP BY tenant_id, recorded_document_id HAVING count(*) > 1
    ) THEN
        CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_transactions_recorded_document
            ON bank_transactions (tenant_id, recorded_document_id) WHERE recorded_document_id IS NOT NULL;
    END IF;
END $$;
