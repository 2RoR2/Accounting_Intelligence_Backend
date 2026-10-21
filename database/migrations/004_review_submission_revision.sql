-- Explicit owner-only compatibility upgrade. Never run by application startup.
-- Historical 002/003 files are not present in this candidate. This is not a
-- substitute for their complete changes or a way to bootstrap a nonempty DB.
-- Existing submissions get conservative revision 1, matching the former ALTER:
-- do not infer a later review revision or automatically approve older content.
ALTER TABLE public.review_submissions
    ADD COLUMN IF NOT EXISTS content_revision INTEGER NOT NULL DEFAULT 1;
