-- A statement line can be matched to the payment voucher or official receipt that already recorded it (pay by
-- voucher first, see the bank line later). The voucher's journal already moved the bank ledger, so the match posts
-- nothing; it only links the line (recorded_document_id) and marks it matched. Idempotent; the same text is appended
-- to the consolidated schema file.
--
-- Unlike a money-in/out document created from the line (released only by voiding it), a match to an independently
-- recorded payment can be undone: the line returns to 'imported' and the payment stays as it is. Every other rule of
-- the guard is unchanged.
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
        AND NOT (NEW.recorded_document_id IS NULL AND NEW.status = 'imported' AND EXISTS (
            SELECT 1 FROM erp_document_headers h
            WHERE h.id = OLD.recorded_document_id AND h.tenant_id = OLD.tenant_id
                AND (h.status = 'void' OR h.document_type IN ('payment', 'purchase-payment'))
        )) THEN
        RAISE EXCEPTION 'A recorded bank document link cannot be changed.';
    END IF;
    IF NEW.recorded_document_id IS NOT NULL AND NEW.status <> 'matched' THEN
        RAISE EXCEPTION 'A bank transaction with a recorded document must be matched.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- One payment records one bank movement: a voucher or receipt is matched to at most one statement line.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_transactions_recorded_document
    ON bank_transactions (tenant_id, recorded_document_id) WHERE recorded_document_id IS NOT NULL;
