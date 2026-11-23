from types import SimpleNamespace

import pytest

from services.admin import browser_session

LOCAL = {"APP_ENV": "development", "DATABASE_URL": "postgresql://u:p@127.0.0.1:5433/db", "S3_BUCKET": ""}


def args(tmp_path, email=browser_session.DEFAULT_EMAIL):
    return SimpleNamespace(email=email, out=str(tmp_path / "cookies.json"))


def no_db():
    raise AssertionError("must not connect")


@pytest.mark.parametrize("environ", [
    {**LOCAL, "APP_ENV": "production"},
    {**LOCAL, "DATABASE_URL": "postgresql://u:p@db.example.com/db"},
    {**LOCAL, "DATABASE_URL": "postgresql://u:p@localhost,db.example.com/db"},
    {**LOCAL, "DATABASE_URL": "postgresql://u:p@localhost/db?hostaddr=203.0.113.10"},
    {**LOCAL, "PGHOSTADDR": "203.0.113.10"},
    {**LOCAL, "S3_BUCKET": "shared"},
])
def test_refuses_outside_local_development(tmp_path, environ):
    assert browser_session.run(args(tmp_path), environ=environ, connect=no_db) == 2
    assert not (tmp_path / "cookies.json").exists()


@pytest.mark.parametrize("email", ["rachel@abc.example", "someone@gmail.com", "x@demo.test.evil.com"])
def test_refuses_non_synthetic_accounts(tmp_path, email):
    assert browser_session.run(args(tmp_path, email), environ=LOCAL, connect=no_db) == 2
    assert not (tmp_path / "cookies.json").exists()


def test_accepts_demo_addresses():
    assert browser_session.check_email(" Accountant@Trading.Demo.Test ") == "accountant@trading.demo.test"
