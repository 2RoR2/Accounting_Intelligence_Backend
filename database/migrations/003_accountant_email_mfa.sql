ALTER TABLE auth_challenges ADD COLUMN IF NOT EXISTS email_code_hash TEXT;
ALTER TABLE auth_challenges ADD COLUMN IF NOT EXISTS method TEXT NOT NULL DEFAULT 'totp';
