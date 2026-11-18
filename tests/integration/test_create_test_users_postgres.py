"""E1: create-test-users against the real schema in a disposable local PostgreSQL.

Creates a uniquely named database next to TEST_DATABASE_URL, loads the consolidated schema, adds a
demo company matching a small pack.json, runs the command twice and drops the database afterwards.
Skipped without TEST_DATABASE_URL. All addresses are synthetic (example.test).
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from database.schema_compatibility import apply_migrations
from security.authentication import verify_password
from services.admin import test_users as tu

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "Uat-Test-2026!"
COMPANY = {"name": "Kedai Ujian Sdn Bhd (Demo)", "ssm": "202601099999"}
EMAILS = ("lead.admin@uat.example.test:local_admin, acct.one@uat.example.test, "
          "acct.two@uat.example.test, owner@uat.example.test:super_admin")


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires the disposable local TEST_DATABASE_URL")
    parts = urlsplit(url)
    assert parts.hostname in {"127.0.0.1", "localhost", "::1"}
    name = "e1_test_users_" + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    target = urlunsplit(parts._replace(path="/" + name))
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / "database/PostgreSQL Database schema.sql",
                             tmp_path_factory.mktemp("no-migrations"))
            db.execute("""INSERT INTO tenants(name,registration_number,admin_email,seat_limit)
                VALUES (%s,%s,'admin@trading.demo.test',2)""", (COMPANY["name"], COMPANY["ssm"]))
            other = db.execute("""INSERT INTO tenants(name,registration_number,admin_email)
                VALUES ('Other Co','OTHER-1','x@other.test') RETURNING id""").fetchone()
            db.execute("""INSERT INTO users(tenant_id,name,email,role,status)
                VALUES (%s,'Elsewhere','elsewhere@example.test','accountant','active')""", (other["id"],))
        yield target
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="module")
def pack_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("packs")
    (root / "trading").mkdir()
    (root / "trading" / "pack.json").write_text(json.dumps({"company": COMPANY}), encoding="utf-8")
    return root


def invoke(database, pack_root, emails, reset=False, password=PASSWORD):
    environ = {"APP_ENV": "development", "DATABASE_URL": "postgresql://postgres@127.0.0.1:5433/x",
               "UAT_TEST_EMAILS": emails, "UAT_TEST_PASSWORD": password}
    lines = []
    args = SimpleNamespace(pack="trading", company=None, reset_passwords=reset, pack_root=str(pack_root))
    code = tu.run(args, environ=environ, out=lines.append,
                  connect=lambda: psycopg.connect(database, row_factory=dict_row),
                  prompt=lambda _: pytest.fail("must not prompt when UAT_TEST_PASSWORD is set"))
    return code, lines


def users(database):
    with psycopg.connect(database, row_factory=dict_row) as db:
        return {row["email"]: row for row in db.execute(
            """SELECT u.*, t.name AS company FROM users u LEFT JOIN tenants t ON t.id=u.tenant_id
               WHERE u.email LIKE '%%@uat.example.test'""")}


def seat_limit(database):
    with psycopg.connect(database, row_factory=dict_row) as db:
        return db.execute("SELECT seat_limit FROM tenants WHERE registration_number=%s",
                          (COMPANY["ssm"],)).fetchone()["seat_limit"]


def test_creates_users_in_the_demo_company_then_is_idempotent(database, pack_root, capsys):
    code, lines = invoke(database, pack_root, EMAILS)
    assert code == 0, capsys.readouterr().err
    rows = users(database)
    assert {email: row["role"] for email, row in rows.items()} == {
        "lead.admin@uat.example.test": "local_admin", "acct.one@uat.example.test": "accountant",
        "acct.two@uat.example.test": "accountant", "owner@uat.example.test": "super_admin"}
    assert rows["owner@uat.example.test"]["tenant_id"] is None
    assert {rows[e]["company"] for e in rows if e != "owner@uat.example.test"} == {COMPANY["name"]}
    assert all(row["status"] == "active" and not row["must_change_password"] for row in rows.values())
    assert all(verify_password(PASSWORD, row["password_hash"]) for row in rows.values())
    assert rows["acct.one@uat.example.test"]["name"] == "Acct One"
    # three company users on a seat limit of 2: raised so the admin pages stay consistent
    assert seat_limit(database) == 3
    text = "\n".join(lines)
    assert "uat.example.test" not in text and "l***@u***.test" in text

    before = {email: (row["id"], row["password_hash"]) for email, row in users(database).items()}
    code, lines = invoke(database, pack_root, EMAILS, password="Another-Pass-9!")
    assert code == 0
    assert [line.split()[0] for line in lines[:4]] == ["unchanged"] * 4
    assert {email: (row["id"], row["password_hash"]) for email, row in users(database).items()} == before


def test_role_change_and_password_reset_sign_the_user_out(database, pack_root):
    with psycopg.connect(database, row_factory=dict_row) as db:
        user_id = db.execute("SELECT id FROM users WHERE email='acct.two@uat.example.test'").fetchone()["id"]
        db.execute("""INSERT INTO web_sessions(user_id,token_hash,expires_at)
            VALUES (%s,%s,now()+interval '1 hour')""", (user_id, uuid4().hex))
    promoted = EMAILS.replace("acct.two@uat.example.test", "acct.two@uat.example.test:local_admin")
    code, _ = invoke(database, pack_root, promoted, reset=True, password="Reset-Pass-7!")
    assert code == 0
    row = users(database)["acct.two@uat.example.test"]
    assert row["role"] == "local_admin" and verify_password("Reset-Pass-7!", row["password_hash"])
    with psycopg.connect(database, row_factory=dict_row) as db:
        assert db.execute("SELECT count(*) n FROM web_sessions WHERE user_id=%s", (user_id,)).fetchone()["n"] == 0


def test_refuses_a_user_of_another_company_and_writes_nothing(database, pack_root, capsys):
    before = users(database)
    code, lines = invoke(database, pack_root, EMAILS + ", new.person@uat.example.test, elsewhere@example.test")
    assert code == 2 and lines == []
    err = capsys.readouterr().err
    assert "e***@e***.test" in err and "elsewhere@example.test" not in err
    assert users(database).keys() == before.keys()


def test_unassigned_documents_are_shared_among_test_accountants(database, pack_root):
    with psycopg.connect(database, row_factory=dict_row) as db:
        tenant = db.execute("SELECT id FROM tenants WHERE registration_number=%s", (COMPANY["ssm"],)).fetchone()["id"]
        owner = db.execute("SELECT id FROM users WHERE email='lead.admin@uat.example.test'").fetchone()["id"]
        held = None
        for n in range(5):
            row = db.execute("""INSERT INTO documents(document_code,tenant_id,owner_id,original_filename,kind,category,
                    storage_path,mime_type,file_size_bytes,file_hash_sha256)
                VALUES (%s,%s,%s,%s,'bill','accounts_payable','x','application/pdf',1,%s) RETURNING id""",
                (f"BILL_E1_{n}", tenant, owner, f"bill{n}.pdf", uuid4().hex)).fetchone()
            held = held or row["id"]
        db.execute("UPDATE documents SET assigned_to=%s WHERE id=%s", (owner, held))  # already held: left alone
    code, lines = invoke(database, pack_root, EMAILS)
    assert code == 0 and "4 unassigned documents shared among the test accountants." in lines
    with psycopg.connect(database, row_factory=dict_row) as db:
        counts = {row["email"]: row["n"] for row in db.execute(
            """SELECT u.email, count(*) n FROM documents d JOIN users u ON u.id=d.assigned_to
               WHERE d.tenant_id=%s GROUP BY u.email""", (tenant,))}
    # this run sets acct.two back to accountant (it was promoted earlier), so the two accountants share 4
    assert counts == {"lead.admin@uat.example.test": 1, "acct.one@uat.example.test": 2,
                      "acct.two@uat.example.test": 2}
    with psycopg.connect(database, row_factory=dict_row) as db:
        # recorded like the local admin's assign endpoint: one workload row per document, by the test admin
        workload = db.execute("""SELECT count(*) n, count(DISTINCT w.assigned_by) admins, bool_and(u.email='lead.admin@uat.example.test') by_admin
            FROM workload_assignments w JOIN users u ON u.id=w.assigned_by WHERE w.tenant_id=%s""", (tenant,)).fetchone()
        notes = db.execute("""SELECT count(*) n FROM notifications WHERE tenant_id=%s AND title='Documents assigned to you'""",
                           (tenant,)).fetchone()["n"]
        audit = db.execute("""SELECT details FROM audit_logs WHERE tenant_id=%s AND action='DOCUMENTS_ASSIGNED'""",
                           (tenant,)).fetchall()
    assert (workload["n"], workload["admins"], workload["by_admin"]) == (4, 1, True)
    assert notes == 2
    assert [json.loads(row["details"]) for row in audit] == [{"documents": 4, "accountants": 2}]
    assert "uat.example.test" not in audit[0]["details"]
    code, lines = invoke(database, pack_root, EMAILS)
    assert code == 0 and not any("shared among" in line for line in lines)


def test_invited_account_gets_the_password_and_a_demoted_admin_loses_the_authenticator(database, pack_root):
    from security.authentication import encrypt_totp_secret, new_totp_secret
    with psycopg.connect(database, row_factory=dict_row) as db:
        tenant = db.execute("SELECT id FROM tenants WHERE registration_number=%s", (COMPANY["ssm"],)).fetchone()["id"]
        db.execute("""INSERT INTO users(tenant_id,name,email,role,status,invited_at)
            VALUES (%s,'Invited','invited@uat.example.test','accountant','invited',now())""", (tenant,))
        lead = db.execute("""UPDATE users SET totp_secret_enc=%s,totp_enabled=true
            WHERE email='lead.admin@uat.example.test' RETURNING id""", (encrypt_totp_secret(new_totp_secret()),)).fetchone()["id"]
        db.execute("INSERT INTO mfa_backup_codes(user_id,code_hash) VALUES (%s,'x')", (lead,))
    demoted = (EMAILS.replace("lead.admin@uat.example.test:local_admin", "lead.admin@uat.example.test")
               + ", boss@uat.example.test:local_admin, invited@uat.example.test")
    code, lines = invoke(database, pack_root, demoted)
    assert code == 0, lines
    assert any(line.endswith("l***@u***.test (authenticator cleared)") for line in lines)
    assert any(line.endswith("i***@u***.test (password set)") for line in lines)
    rows = users(database)
    assert rows["invited@uat.example.test"]["status"] == "active"
    assert verify_password(PASSWORD, rows["invited@uat.example.test"]["password_hash"])
    lead_row = rows["lead.admin@uat.example.test"]
    assert (lead_row["role"], lead_row["totp_enabled"], lead_row["totp_secret_enc"]) == ("accountant", False, None)
    with psycopg.connect(database, row_factory=dict_row) as db:
        assert db.execute("SELECT count(*) n FROM mfa_backup_codes WHERE user_id=%s", (lead,)).fetchone()["n"] == 0


def test_missing_demo_company_points_to_the_seed(database, tmp_path, capsys):
    (tmp_path / "trading").mkdir()
    (tmp_path / "trading" / "pack.json").write_text(json.dumps({"company": {"name": "Nope", "ssm": "0"}}))
    code, _ = invoke(database, tmp_path, EMAILS)
    assert code == 2 and "seed-demo-company --pack trading" in capsys.readouterr().err
