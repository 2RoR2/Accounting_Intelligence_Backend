-- Additive review revision required by the review/approval protocol.
ALTER TABLE public.review_submissions
    ADD COLUMN IF NOT EXISTS content_revision INTEGER NOT NULL DEFAULT 1;
