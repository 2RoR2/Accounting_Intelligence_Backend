-- Rollback Migration 0002: Safely drop pending_jobs outbox table
-- Refuse destructive rollback if processing history exists

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = 'pending_jobs'
    ) THEN
        IF EXISTS (SELECT 1 FROM pending_jobs LIMIT 1) THEN
            RAISE EXCEPTION 'Refusing to drop pending_jobs: table contains job history. Rollback aborted to prevent data loss.';
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = 'raw_extractions'
    ) THEN
        IF EXISTS (SELECT 1 FROM raw_extractions WHERE pending_job_id IS NOT NULL LIMIT 1) THEN
            RAISE EXCEPTION 'Refusing to drop pending_jobs: raw_extractions contains references to pending_job_id. Rollback aborted.';
        END IF;
    END IF;
END $$;

-- Drop foreign key reference from raw_extractions before dropping pending_jobs
ALTER TABLE raw_extractions DROP CONSTRAINT IF EXISTS fk_raw_extractions_pending_job;

-- Drop pending_jobs table safely (without CASCADE, never dropping constraints owned by migration 0001)
DROP TABLE IF EXISTS pending_jobs;
