import os
import sys
import unittest
from unittest.mock import patch

import manage
from auth.security import verify_password


class FakeDatabase:
    def __init__(self):
        self.inserted_users = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        _ = exc_type, exc_value, traceback
        return False

    def execute(self, query, params=()):
        if query.startswith("SELECT id,name FROM tenants"):
            return FakeResult({"id": "demo-tenant-id", "name": manage.DEMO_TENANT_NAME})
        if query.startswith("SELECT id,role,tenant_id FROM users"):
            return FakeResult(None)
        if query.startswith("INSERT INTO users"):
            self.inserted_users.append(params)
        return FakeResult(None)


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class DemoAccountSeedTests(unittest.TestCase):
    def test_seeding_requires_local_development_opt_in_before_connecting(self):
        with (
            patch.dict(os.environ, {"APP_ENV": "production", "ENABLE_DEMO_ACCOUNTS": "true"}),
            patch.object(sys, "argv", ["manage.py", "seed-demo-accounts"]),
            patch.object(manage, "connect") as connect,
            self.assertRaises(SystemExit),
        ):
            manage.main()
        connect.assert_not_called()

    def test_seeding_creates_all_roles_with_the_entered_password(self):
        database = FakeDatabase()
        password = "DemoPass123!"
        with (
            patch.dict(os.environ, {"APP_ENV": "development", "ENABLE_DEMO_ACCOUNTS": "true"}),
            patch.object(sys, "argv", ["manage.py", "seed-demo-accounts"]),
            patch.object(manage.getpass, "getpass", side_effect=[password, password]),
            patch.object(manage, "connect", return_value=database),
        ):
            manage.main()

        self.assertEqual(len(database.inserted_users), 3)
        accounts = {
            email: {
                "tenant_id": tenant_id,
                "name": name,
                "password_hash": stored_password,
                "role": role,
            }
            for tenant_id, name, email, stored_password, role in database.inserted_users
        }
        self.assertEqual(
            {email: account["role"] for email, account in accounts.items()},
            {
                "admin@saic.example": "super_admin",
                "alice@abc.example": "local_admin",
                "rachel@abc.example": "accountant",
            },
        )
        self.assertEqual(
            {email: account["name"] for email, account in accounts.items()},
            {
                "admin@saic.example": "SAIC Administrator",
                "alice@abc.example": "Alice Tan",
                "rachel@abc.example": "Rachel Chong",
            },
        )
        self.assertIsNone(accounts["admin@saic.example"]["tenant_id"])
        self.assertEqual(accounts["alice@abc.example"]["tenant_id"], "demo-tenant-id")
        self.assertEqual(accounts["rachel@abc.example"]["tenant_id"], "demo-tenant-id")
        for account in accounts.values():
            self.assertTrue(verify_password(password, account["password_hash"]))


if __name__ == "__main__":
    unittest.main()
