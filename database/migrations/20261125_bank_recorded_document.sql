-- A statement line can be the posting evidence for a single typed money document.
ALTER TABLE bank_transactions
    ADD COLUMN IF NOT EXISTS recorded_document_id UUID;

-- Keep the link tenant-scoped.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_bank_recorded_document_tenant'
                   AND conrelid = 'bank_transactions'::regclass) THEN
        ALTER TABLE bank_transactions ADD CONSTRAINT fk_bank_recorded_document_tenant
            FOREIGN KEY (recorded_document_id, tenant_id) REFERENCES erp_document_headers(id, tenant_id);
    END IF;
END $$;

-- Preserve the imported evidence. The only new transition is attaching its first posted document.
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
        AND (OLD.recorded_document_id IS NOT NULL OR OLD.status IN ('matched', 'ignored')) THEN
        RAISE EXCEPTION 'A recorded bank document link cannot be changed.';
    END IF;
    IF NEW.recorded_document_id IS NOT NULL AND NEW.status <> 'matched' THEN
        RAISE EXCEPTION 'A bank transaction with a recorded document must be matched.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
