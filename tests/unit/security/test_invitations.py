"""Invitation and activation flow. Run with TEST_DATABASE_URL pointing at a disposable local
PostgreSQL; creates and drops only a unique schema. The mailer is always stubbed."""
import hashlib
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

import main as api
import services.routes.admin.routes as admin_routes
import services.routes.security.routes as security_routes
from security.authentication import encrypt_totp_secret, hash_password, new_totp_secret, totp_code

PASSWORD = "StrongPass123!"
NEW_PASSWORD = "Invited#Pass456"
HEADERS = {"X-Requested-With": "AccountingIntelligence", "Origin": "http://localhost:3000"}


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class InvitationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ["TEST_DATABASE_URL"]
        cls.schema = "invite_test_" + uuid.uuid4().hex
        with psycopg.connect(cls.url) as db:
            db.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.addClassCleanup(cls._drop_schema)
        # Every route module binds its own `connect`; patch them all so nothing reaches .env's database.
        cls.patches = [patch.object(api, "connect", cls.connect)] + [
            patch.object(module, "connect", cls.connect) for name, module in list(sys.modules.items())
            if name.startswith("services.routes.") and hasattr(module, "connect")]
        cls.patches.append(patch.dict(os.environ, {"AUTH_SECRET": "test-secret-" * 5, "COOKIE_SECURE": "false",
                                                   "FRONTEND_ORIGIN": "http://localhost:3000"}))
        for item in cls.patches:
            item.start()
            cls.addClassCleanup(item.stop)
        with cls.connect() as db:
            base = Path(__file__).resolve().parents[3]
            schema = (base / "database/PostgreSQL Database schema.sql").read_text(encoding="utf-8-sig")
            db.execute(schema[schema.index("CREATE TABLE tenants ("):])

    @classmethod
    def connect(cls):
        db = psycopg.connect(cls.url, row_factory=dict_row)
        db.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(cls.schema)))
        return db

    @classmethod
    def _drop_schema(cls):
        with psycopg.connect(cls.url) as db:
            db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        with self.connect() as db:
            db.execute("TRUNCATE users,tenants,company_requests,auth_rate_limits,auth_challenges,auth_tokens CASCADE")
            self.tenant = db.execute("""INSERT INTO tenants(name,registration_number,admin_email,seat_limit)
                VALUES ('Alpha Sdn Bhd','ALPHA1','admin@alpha.example',3) RETURNING id""").fetchone()["id"]
            self.other_tenant = db.execute("""INSERT INTO tenants(name,registration_number,admin_email,seat_limit)
                VALUES ('Beta Sdn Bhd','BETA1','admin@beta.example',3) RETURNING id""").fetchone()["id"]
        self.secrets = {}
        self.create_privileged("admin@alpha.example", "local_admin", self.tenant)
        self.create_privileged("admin@beta.example", "local_admin", self.other_tenant)
        self.create_privileged("root@platform.example", "super_admin", None)
        self.invite_mail = patch.object(admin_routes, "send_invitation_email")
        self.sent = self.invite_mail.start()
        self.addCleanup(self.invite_mail.stop)
        self.login_mail = patch.object(security_routes, "send_login_code")
        self.login_sent = self.login_mail.start()
        self.addCleanup(self.login_mail.stop)

    # Helpers ---------------------------------------------------------------------------------
    def create_privileged(self, email, role, tenant_id):
        secret = new_totp_secret()
        self.secrets[email] = secret
        with self.connect() as db:
            db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status,totp_enabled,totp_secret_enc)
                VALUES (%s,%s,%s,%s,%s,'active',true,%s)""",
                (tenant_id, email.split("@")[0], email, hash_password(PASSWORD), role, encrypt_totp_secret(secret)))

    def client(self):
        client = TestClient(api.app, headers=HEADERS)
        self.addCleanup(client.close)
        return client

    def signed_in(self, email):
        client = self.client()
        response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        verified = client.post("/api/auth/verify-mfa", json={"code": totp_code(self.secrets[email])})
        self.assertEqual(verified.status_code, 200, verified.text)
        return client

    def last_token(self):
        link = self.sent.call_args.kwargs["link"]
        self.assertTrue(link.startswith("http://localhost:3000/activate?token="), link)
        return parse_qs(urlsplit(link).query)["token"][0]

    def activate(self, token, password=NEW_PASSWORD):
        return self.client().post("/api/auth/activate", json={"token": token, "password": password,
                                                              "confirmation": password})

    def invite(self, admin, email="new.accountant@alpha.example", name="New Accountant"):
        return admin.post("/api/users/invitations", json={"name": name, "email": email})

    # Tests -----------------------------------------------------------------------------------
    def test_invite_emails_single_use_link_and_stores_only_its_hash(self):
        response = self.invite(self.signed_in("admin@alpha.example"))
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["status"], "INVITED")
        self.assertEqual(body["role"], "ACCOUNTANT")
        self.assertEqual(body["invitation"]["emailSent"], True)
        self.assertIsNone(body["invitation"]["activationLink"])
        self.assertEqual(self.sent.call_args.args[0], "new.accountant@alpha.example")
        self.assertEqual(self.sent.call_args.kwargs["company"], "Alpha Sdn Bhd")
        token = self.last_token()
        self.assertNotIn(token, response.text)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM auth_tokens WHERE purpose='activation'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, str(rows[0]))

    def test_invited_accountant_activates_once_then_signs_in_with_email_code(self):
        self.invite(self.signed_in("admin@alpha.example"))
        token = self.last_token()
        invitee = self.client()
        # An invited account cannot sign in before activation.
        self.assertEqual(invitee.post("/api/auth/login", json={"email": "new.accountant@alpha.example",
                                                               "password": NEW_PASSWORD}).status_code, 401)
        self.assertEqual(self.activate(token).status_code, 200)
        self.assertEqual(self.activate(token, "Another#Pass789").status_code, 400)  # single use
        login = invitee.post("/api/auth/login", json={"email": "new.accountant@alpha.example", "password": NEW_PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(login.json()["mfaMethod"], "email")
        code = self.login_sent.call_args.args[1]
        verified = invitee.post("/api/auth/verify-mfa", json={"code": code})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["role"], "ACCOUNTANT")
        self.assertEqual(verified.json()["status"], "ACTIVE")
        self.assertFalse(verified.json()["mustChangePassword"])

    def test_expired_token_is_rejected(self):
        self.invite(self.signed_in("admin@alpha.example"))
        token = self.last_token()
        with self.connect() as db:
            db.execute("UPDATE auth_tokens SET expires_at=now()-interval '1 second'")
        self.assertEqual(self.activate(token).status_code, 400)
        with self.connect() as db:
            status = db.execute("SELECT status FROM users WHERE email='new.accountant@alpha.example'").fetchone()["status"]
        self.assertEqual(status, "invited")

    def test_resend_invalidates_the_old_link(self):
        admin = self.signed_in("admin@alpha.example")
        user_id = self.invite(admin).json()["id"]
        first = self.last_token()
        resent = admin.post(f"/api/users/{user_id}/resend-invitation")
        self.assertEqual(resent.status_code, 200, resent.text)
        self.assertTrue(resent.json()["invitation"]["emailSent"])
        second = self.last_token()
        self.assertNotEqual(first, second)
        self.assertEqual(self.activate(first).status_code, 400)
        self.assertEqual(self.activate(second).status_code, 200)
        # Resending to an activated account would lock them out; refuse it.
        self.assertEqual(admin.post(f"/api/users/{user_id}/resend-invitation").status_code, 409)

    def test_cancelled_invitation_link_stops_working_until_reinvited(self):
        admin = self.signed_in("admin@alpha.example")
        user_id = self.invite(admin).json()["id"]
        cancelled_token = self.last_token()
        self.assertEqual(admin.patch(f"/api/users/{user_id}/status", json={"status": "invitation_expired"}).status_code, 200)
        self.assertEqual(self.activate(cancelled_token).status_code, 400)
        reinvited = admin.post(f"/api/users/{user_id}/resend-invitation")
        self.assertEqual(reinvited.status_code, 200, reinvited.text)
        self.assertEqual(reinvited.json()["status"], "INVITED")
        self.assertEqual(self.activate(self.last_token()).status_code, 200)

    def test_admin_of_another_company_cannot_resend(self):
        user_id = self.invite(self.signed_in("admin@alpha.example")).json()["id"]
        self.sent.reset_mock()
        other = self.signed_in("admin@beta.example")
        self.assertEqual(other.post(f"/api/users/{user_id}/resend-invitation").status_code, 404)
        self.sent.assert_not_called()
        # A Company Admin cannot use the Super Admin endpoint for any company.
        self.assertEqual(other.post(f"/api/tenants/{self.tenant}/users",
                                    json={"name": "X", "email": "x@alpha.example", "role": "accountant"}).status_code, 403)

    def test_seat_limit_blocks_invites_and_super_admin_adds(self):
        admin = self.signed_in("admin@alpha.example")  # seat 1 of 3
        self.assertEqual(self.invite(admin, "one@alpha.example").status_code, 201)  # seat 2
        self.assertEqual(self.invite(admin, "two@alpha.example").status_code, 201)  # seat 3
        self.assertEqual(self.invite(admin, "three@alpha.example").status_code, 409)
        root = self.signed_in("root@platform.example")
        response = root.post(f"/api/tenants/{self.tenant}/users",
                             json={"name": "Four", "email": "four@alpha.example", "role": "accountant"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "Account limit reached.")
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM users WHERE tenant_id=%s", (self.tenant,)).fetchone()["n"], 3)
        # A cancelled invitation frees its seat; re-inviting it must respect the limit again.
        cancelled = self.invite(admin, "one@alpha.example")  # already registered
        self.assertEqual(cancelled.status_code, 409)
        users = {u["email"]: u["id"] for u in admin.get("/api/users").json()}
        self.assertEqual(admin.patch(f"/api/users/{users['one@alpha.example']}/status",
                                     json={"status": "invitation_expired"}).status_code, 200)
        self.assertEqual(self.invite(admin, "three@alpha.example").status_code, 201)  # takes the freed seat
        self.assertEqual(admin.post(f"/api/users/{users['one@alpha.example']}/resend-invitation").status_code, 409)

    def test_email_failure_still_invites_and_returns_copy_once_link_without_logging_it(self):
        self.sent.side_effect = RuntimeError("Brevo rejected the email (HTTP 401).")
        admin = self.signed_in("admin@alpha.example")
        with self.assertLogs("services.routes.admin.routes", level="WARNING") as logs:
            response = self.invite(admin)
        self.assertEqual(response.status_code, 201, response.text)
        invitation = response.json()["invitation"]
        self.assertFalse(invitation["emailSent"])
        link = invitation["activationLink"]
        self.assertTrue(link.startswith("http://localhost:3000/activate?token="))
        token = parse_qs(urlsplit(link).query)["token"][0]
        self.assertNotIn(token, "\n".join(logs.output))
        self.assertEqual(self.activate(token).status_code, 200)

    def test_company_approval_emails_local_admin_who_enrols_totp(self):
        public = self.client()
        request = public.post("/api/company-requests", json={"company": "Gamma Trading", "registration": "GAMMA1",
                                                             "contact": "Gina", "email": "gina@gamma.example", "accounts": 4})
        self.assertEqual(request.status_code, 201, request.text)
        root = self.signed_in("root@platform.example")
        approved = root.post(f"/api/company-requests/{request.json()['id']}/approve", json={"limit": 4})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertTrue(approved.json()["invitation"]["emailSent"])
        self.assertEqual(self.sent.call_args.kwargs["role"], "local_admin")
        self.assertEqual(self.sent.call_args.kwargs["company"], "Gamma Trading")
        self.assertEqual(self.activate(self.last_token()).status_code, 200)
        admin = self.client()
        login = admin.post("/api/auth/login", json={"email": "gina@gamma.example", "password": NEW_PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertTrue(login.json()["enrollmentRequired"])
        verified = admin.post("/api/auth/verify-mfa", json={"code": totp_code(login.json()["totpSecret"])})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["role"], "LOCAL_ADMIN")

    def test_super_admin_adds_company_user_with_role(self):
        root = self.signed_in("root@platform.example")
        created = root.post(f"/api/tenants/{self.tenant}/users",
                            json={"name": "Second Admin", "email": "Second.Admin@Alpha.example", "role": "local_admin"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["role"], "LOCAL_ADMIN")
        self.assertEqual(created.json()["tenantId"], str(self.tenant))
        self.assertEqual(self.sent.call_args.args[0], "second.admin@alpha.example")
        # Emails are unique across the platform, case-insensitively, including other companies.
        for email in ("second.admin@ALPHA.example", "admin@beta.example", "root@platform.example"):
            duplicate = root.post(f"/api/tenants/{self.tenant}/users", json={"name": "Dup", "email": email, "role": "accountant"})
            self.assertEqual(duplicate.status_code, 409, email)
        self.assertEqual(root.post(f"/api/tenants/{self.tenant}/users",
                                   json={"name": "Bad", "email": "bad@alpha.example", "role": "super_admin"}).status_code, 422)
        self.assertEqual(root.post(f"/api/tenants/{uuid.uuid4()}/users",
                                   json={"name": "None", "email": "none@x.example", "role": "accountant"}).status_code, 404)
        # Super Admin can resend a Company Admin invitation.
        self.assertEqual(root.post(f"/api/users/{created.json()['id']}/resend-invitation").status_code, 200)

    def test_suspended_company_cannot_receive_or_activate_invites(self):
        admin = self.signed_in("admin@alpha.example")
        self.invite(admin)
        token = self.last_token()
        root = self.signed_in("root@platform.example")
        with self.connect() as db:
            db.execute("UPDATE tenants SET status='suspended' WHERE id=%s", (self.tenant,))
        self.assertEqual(root.post(f"/api/tenants/{self.tenant}/users",
                                   json={"name": "Late", "email": "late@alpha.example", "role": "accountant"}).status_code, 409)
        self.assertEqual(self.activate(token).status_code, 400)


if __name__ == "__main__":
    unittest.main()
