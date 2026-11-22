-- A voided money document releases its bank line without changing the imported evidence.
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
            WHERE h.id = OLD.recorded_document_id AND h.tenant_id = OLD.tenant_id AND h.status = 'void'
        )) THEN
        RAISE EXCEPTION 'A recorded bank document link cannot be changed.';
    END IF;
    IF NEW.recorded_document_id IS NOT NULL AND NEW.status <> 'matched' THEN
        RAISE EXCEPTION 'A bank transaction with a recorded document must be matched.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Reopen lines left linked by voids that predate this migration.
WITH reopened AS (
    UPDATE bank_transactions b SET status = 'imported', recorded_document_id = NULL
    FROM erp_document_headers h
    WHERE h.id = b.recorded_document_id AND h.tenant_id = b.tenant_id AND h.status = 'void'
        AND h.document_type IN ('money-in', 'money-out')
    RETURNING b.id, b.tenant_id, h.id AS document_id
)
INSERT INTO audit_logs(tenant_id, user_name, role, action, resource, details)
SELECT tenant_id, 'System', 'system', 'bank.recorded_document_voided', id::text,
    jsonb_build_object('documentId', document_id)::text FROM reopened;
