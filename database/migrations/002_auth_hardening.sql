-- Add MFA enrollment and rotating refresh sessions. Existing opaque sessions are
-- intentionally invalidated because they cannot be upgraded to signed JWTs.
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret_enc TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE web_sessions ADD COLUMN IF NOT EXISTS session_id UUID;
UPDATE web_sessions SET session_id = gen_random_uuid() WHERE session_id IS NULL;
ALTER TABLE web_sessions ALTER COLUMN session_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_web_sessions_session_id ON web_sessions(session_id);
DELETE FROM web_sessions;

CREATE TABLE IF NOT EXISTS auth_challenges (
    challenge_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pending_totp_secret_enc TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE auth_challenges ADD COLUMN IF NOT EXISTS email_code_hash TEXT;
ALTER TABLE auth_challenges ADD COLUMN IF NOT EXISTS method TEXT NOT NULL DEFAULT 'totp';
