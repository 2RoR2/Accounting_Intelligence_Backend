-- P1 review: deleting a draft keeps its attachment rows for audit. Idempotent; the same text is appended to the
-- consolidated schema file.
--
-- 20261201 tied record_attachments to its typed record with ON DELETE CASCADE, so deleting a draft payment erased its
-- attachment rows, including ones already removed (soft-deleted) for audit, and with them the only reference to the
-- stored object. The foreign key is replaced by two triggers:
-- * a new or re-targeted attachment must still name a typed record of the same company (what the key checked);
-- * deleting a typed record soft-deletes its attachments (deleted_at, kept if already set): the rows and the stored
--   objects stay, and they leave every list as before.
DO $$
DECLARE
    old_key TEXT;
BEGIN
    FOR old_key IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'record_attachments'::regclass AND contype = 'f'
          AND confrelid = 'erp_document_headers'::regclass
    LOOP
        EXECUTE format('ALTER TABLE record_attachments DROP CONSTRAINT %I', old_key);
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION record_attachments_target_guard() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM erp_document_headers WHERE id = NEW.target_id AND tenant_id = NEW.tenant_id) THEN
        RAISE EXCEPTION 'An attachment must belong to a record of the same company.' USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_record_attachments_target_guard ON record_attachments;
CREATE TRIGGER trg_record_attachments_target_guard BEFORE INSERT OR UPDATE OF target_id, tenant_id ON record_attachments
    FOR EACH ROW EXECUTE FUNCTION record_attachments_target_guard();

CREATE OR REPLACE FUNCTION record_attachments_keep_on_delete() RETURNS trigger AS $$
BEGIN
    UPDATE record_attachments SET deleted_at = COALESCE(deleted_at, now())
    WHERE tenant_id = OLD.tenant_id AND target_type = 'erp_document' AND target_id = OLD.id AND deleted_at IS NULL;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_erp_document_attachments_keep ON erp_document_headers;
CREATE TRIGGER trg_erp_document_attachments_keep AFTER DELETE ON erp_document_headers
    FOR EACH ROW EXECUTE FUNCTION record_attachments_keep_on_delete();
