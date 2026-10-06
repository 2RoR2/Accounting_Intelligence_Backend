"""Run with TEST_DATABASE_URL set; creates and removes only a unique test schema."""
import os
import hashlib
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from fastapi.testclient import TestClient
import app as api
from auth.security import (
    ACCESS_TOKEN_SECONDS,
    create_access_token,
    decode_access_token,
    decrypt_totp_secret,
    encrypt_totp_secret,
    hash_password,
    needs_rehash,
    otp_hash,
    totp_code,
    verify_password,
    verify_totp,
)


class SecurityTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verified(self):
        encoded = hash_password("StrongPass123!")
        self.assertNotEqual(encoded, hash_password("StrongPass123!"))
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertTrue(verify_password("StrongPass123!", encoded))
        self.assertFalse(verify_password("wrong", encoded))
        self.assertFalse(verify_password("wrong", "invalid"))
        self.assertFalse(needs_rehash(encoded))

    def test_legacy_scrypt_hash_is_verified_and_marked_for_upgrade(self):
        salt = "legacy-test-salt"
        digest = hashlib.scrypt(b"StrongPass123!", salt=salt.encode(), n=16384, r=8, p=1).hex()
        encoded = f"scrypt${salt}${digest}"
        self.assertTrue(verify_password("StrongPass123!", encoded))
        self.assertTrue(needs_rehash(encoded))
        self.assertFalse(verify_password("WrongPass123!", encoded))

    def test_totp_generation_and_secret_encryption(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        code = totp_code(secret, timestamp=59)
        self.assertEqual(code, "287082")
        self.assertTrue(verify_totp(secret, code, timestamp=59))
        self.assertEqual(decrypt_totp_secret(encrypt_totp_secret(secret)), secret)

    def test_access_jwt_is_signed_and_short_lived(self):
        with patch.dict(os.environ, {"AUTH_SECRET": "unit-test-secret-" * 3}):
            token = create_access_token("user-id", "session-id")
            claims = decode_access_token(token)
        self.assertEqual(claims["sub"], "user-id")
        self.assertEqual(claims["jti"], "session-id")
        self.assertEqual(claims["exp"] - claims["iat"], ACCESS_TOKEN_SECONDS)

    def test_otp_is_bound_to_email_and_secret(self):
        self.assertNotEqual(otp_hash("a", "one@example.com", "123456"), otp_hash("a", "two@example.com", "123456"))
        self.assertNotEqual(otp_hash("a", "one@example.com", "123456"), otp_hash("b", "one@example.com", "123456"))


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to run PostgreSQL integration tests")
class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ["TEST_DATABASE_URL"]
        cls.schema = "auth_test_" + uuid.uuid4().hex
        with psycopg.connect(cls.url) as db:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.db_patch = patch.object(api, "connect", cls.connect)
        cls.db_patch.start()
        with cls.connect() as db:
            base = Path(__file__).resolve().parents[1]
            schema = (base / "database" / "schema.sql").read_text(encoding="utf-8-sig")
            # Omit the destructive reset preamble. All objects live in our unique schema.
            db.execute(schema[schema.index("CREATE TABLE tenants ("):])
            db.execute((base / "database" / "migrations" / "001_auth.sql").read_text())
            db.execute((base / "database" / "migrations" / "002_auth_hardening.sql").read_text())
        cls.env_patch = patch.dict(os.environ, {"AUTH_SECRET": "test-secret-" * 5, "COOKIE_SECURE": "false", "FRONTEND_ORIGIN": "http://localhost:3000"})
        cls.env_patch.start()

    @classmethod
    def connect(cls):
        return psycopg.connect(cls.url, row_factory=dict_row, options=f"-c search_path={cls.schema}")

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.env_patch.stop()
        with psycopg.connect(cls.url) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        with self.connect() as db:
            db.execute("TRUNCATE users,tenants,company_requests,auth_rate_limits,auth_challenges CASCADE")
            tenant = db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES ('Test Co','REG123','user@example.com') RETURNING id").fetchone()
            self.uid = db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                VALUES (%s,'Test User','user@example.com',%s,'accountant','active') RETURNING id""",
                (tenant["id"], hash_password("StrongPass123!"))).fetchone()["id"]
        self.client = TestClient(api.app, headers={"X-Requested-With": "AccountingIntelligence", "Origin": "http://localhost:3000"})
        self.mail = patch.object(api, "send_reset_code")
        self.send = self.mail.start()
        self.addCleanup(self.mail.stop)
        self.addCleanup(self.client.close)

    def post(self, endpoint, data):
        return self.client.post("/api/" + endpoint, json=data)

    def login(self):
        return self.post("auth/login", {"email": "user@example.com", "password": "StrongPass123!"})

    def request_code(self):
        response = self.post("auth/forgot-password", {"email": "user@example.com"})
        self.assertEqual(response.status_code, 200, response.text)
        return self.send.call_args.args[1]

    def verify(self, code):
        return self.post("auth/verify-reset-code", {"email": "user@example.com", "code": code})

    def reset(self):
        return self.post("auth/reset-password", {"password": "ChangedPass456!", "confirmation": "ChangedPass456!"})

    def test_login_session_logout(self):
        self.assertEqual(self.post("auth/login", {"email": "user@example.com", "password": "wrong"}).status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password_hash", response.json())
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("ai_access=", response.headers["set-cookie"])
        self.assertIn("ai_refresh=", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.post("auth/logout", {}).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_legacy_password_is_rehashed_after_login(self):
        salt = "legacy-test-salt"
        legacy_hash = hashlib.scrypt(b"StrongPass123!", salt=salt.encode(), n=16384, r=8, p=1).hex()
        with self.connect() as db:
            db.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                       (f"scrypt${salt}${legacy_hash}", self.uid))
        self.assertEqual(self.login().status_code, 200)
        with self.connect() as db:
            encoded = db.execute("SELECT password_hash FROM users WHERE id=%s", (self.uid,)).fetchone()["password_hash"]
        self.assertTrue(encoded.startswith("$argon2id$"))

    def test_refresh_rotates_token_and_rejects_role_mismatch(self):
        self.assertEqual(self.login().status_code, 200)
        old_refresh = self.client.cookies.get("ai_refresh")
        refreshed = self.post("auth/refresh", {})
        self.assertEqual(refreshed.status_code, 200)
        self.assertNotEqual(old_refresh, self.client.cookies.get("ai_refresh"))
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/authorize?path=workspace/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/authorize?path=admin/dashboard").status_code, 403)

    def test_privileged_login_requires_totp_enrollment(self):
        with self.connect() as db:
            tenant = db.execute("SELECT id FROM tenants LIMIT 1").fetchone()
            admin_id = db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                VALUES (%s,'Local Admin','admin@example.com',%s,'local_admin','active') RETURNING id""",
                (tenant["id"], hash_password("StrongPass123!"))).fetchone()["id"]
        response = self.post("auth/login", {"email": "admin@example.com", "password": "StrongPass123!"})
        self.assertEqual(response.status_code, 200, response.text)
        challenge = response.json()
        self.assertTrue(challenge["mfaRequired"])
        self.assertTrue(challenge["enrollmentRequired"])
        self.assertNotIn("ai_access", self.client.cookies)
        code = totp_code(challenge["totpSecret"])
        verified = self.post("auth/verify-mfa", {"code": code})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["role"], "LOCAL_ADMIN")
        self.assertEqual(self.client.get("/api/auth/authorize?path=company/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/authorize?path=workspace/dashboard").status_code, 403)
        with self.connect() as db:
            enrolled = db.execute("SELECT totp_enabled,totp_secret_enc FROM users WHERE id=%s",
                                  (admin_id,)).fetchone()
        self.assertTrue(enrolled["totp_enabled"])
        self.assertNotEqual(enrolled["totp_secret_enc"], challenge["totpSecret"])

    def test_reset_end_to_end_and_replay(self):
        self.login()
        old_access = self.client.cookies.get("ai_access")
        code = self.request_code()
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(self.reset().status_code, 400)
        self.assertEqual(self.verify(code).status_code, 200)
        grant = self.client.cookies.get("ai_reset")
        self.assertEqual(self.verify(code).status_code, 400)
        self.assertEqual(self.client.get("/api/auth/reset-status").status_code, 200)
        self.assertEqual(self.reset().status_code, 200)
        self.client.cookies.set("ai_reset", grant, path="/api/auth")
        self.assertEqual(self.reset().status_code, 400)
        self.client.cookies.set("ai_access", old_access, path="/api")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.post("auth/login", {"email": "user@example.com", "password": "ChangedPass456!"}).status_code, 200)

    def test_wrong_codes_exhaust_challenge(self):
        code = self.request_code()
        wrong = "000000" if code != "000000" else "999999"
        for _ in range(5):
            self.assertEqual(self.verify(wrong).status_code, 400)
        self.assertEqual(self.verify(code).status_code, 400)
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT attempts FROM password_challenges").fetchone()["attempts"], 5)

    def test_expired_code_and_grant(self):
        code = self.request_code()
        with self.connect() as db:
            db.execute("UPDATE password_challenges SET expires_at=now()-interval '1 second'")
        self.assertEqual(self.verify(code).status_code, 400)
        with self.connect() as db:
            db.execute("UPDATE password_challenges SET expires_at=now()+interval '1 minute'")
        self.assertEqual(self.verify(code).status_code, 200)
        with self.connect() as db:
            db.execute("UPDATE password_challenges SET grant_expires_at=now()-interval '1 second'")
        self.assertEqual(self.reset().status_code, 400)

    def test_resend_cooldown_and_old_code_invalidation(self):
        first = self.request_code()
        self.assertEqual(self.post("auth/forgot-password", {"email": "user@example.com"}).status_code, 429)
        with self.connect() as db:
            db.execute("DELETE FROM auth_rate_limits")
        with patch.object(api.secrets, "randbelow", return_value=(int(first) + 1) % 1000000):
            second = self.request_code()
        self.assertNotEqual(first, second)
        self.assertEqual(self.verify(first).status_code, 400)
        self.assertEqual(self.verify(second).status_code, 200)

    def test_unknown_email_generic_response(self):
        known = self.post("auth/forgot-password", {"email": "user@example.com"})
        unknown = self.post("auth/forgot-password", {"email": "nobody@example.com"})
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(self.send.call_count, 1)

    def test_mail_failure_does_not_create_challenge(self):
        self.send.side_effect = RuntimeError("SMTP failure")
        self.assertEqual(self.post("auth/forgot-password", {"email": "user@example.com"}).status_code, 503)
        with self.connect() as db:
            self.assertIsNone(db.execute("SELECT * FROM password_challenges").fetchone())

    def test_registration_persists_pending_and_rejects_duplicate(self):
        data = {"company": "New Co", "registration": "NEW1", "contact": "Sam", "email": "sam@example.com", "accounts": 10}
        response = self.post("company-requests", data)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "PENDING")
        self.assertEqual(self.post("company-requests", data).status_code, 409)
        self.assertEqual(self.post("company-requests", {**data, "accounts": 0}).status_code, 422)
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM company_requests").fetchone()["n"], 1)

    def test_suspended_company_blocks_existing_session(self):
        self.login()
        with self.connect() as db:
            db.execute("UPDATE tenants SET status='suspended'")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.login().status_code, 401)

    def test_csrf_and_password_policy(self):
        response = self.client.post("/api/auth/login", json={}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        with TestClient(api.app) as client:
            self.assertEqual(client.post("/api/auth/logout", json={}).status_code, 403)
        self.assertEqual(self.post("auth/reset-password", {"password": "weak", "confirmation": "weak"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
