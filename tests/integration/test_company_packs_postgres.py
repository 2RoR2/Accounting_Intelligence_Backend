"""The team demo companies against the real schema and routes: every company tallies, team accounts and Inbox
work are created as planned, each role signs in with its real second factor and sees exactly its own data, and one
company never sees another's."""
import json
import os
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from ai.synthetic.sme_books import books_for
from database.schema_compatibility import apply_migrations
from services.admin.demo_company import HEADERS, SeedError, _remote_destination

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "FixtureTeam!2026"  # synthetic, this test only


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a disposable loopback TEST_DATABASE_URL")
    if _remote_destination(url, os.environ):
        pytest.fail("Company pack tests require a disposable loopback PostgreSQL database")
    name = "test_packs_" + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = urlsplit(url)._replace(path="/" + name).geturl()
    for key, value in {"DATABASE_URL": target, "APP_ENV": "development", "S3_BUCKET": "", "DB_POOL_SIZE": "0",
                       "AUTH_SECRET": "synthetic-auth-secret-for-company-pack-tests",
                       "DOCUMENT_STORAGE_PATH": str(tmp_path / "objects")}.items():
        monkeypatch.setenv(key, value)
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / "database/PostgreSQL Database schema.sql", ROOT / "database/migrations")
        from services.admin.company_seed import require_target

        yield require_target(False), tmp_path
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.parametrize("code", ["TKK", "EHB", "CDA"])
def test_company_seeds_tallies_and_refuses_a_second_run(fresh_db, code):
    from services.admin import company_seed
    from services.admin.company_tally import tally

    connect, folder = fresh_db
    books = books_for(code)
    with connect() as db:
        planned = company_seed.plan(db, books)
    assert planned["company"].endswith(" - new") and planned["Inbox documents (already read)"] == 14
    company_seed.seed(connect, books, folder / "pack", PASSWORD)
    with connect() as db:
        summaries = tally(db, books)
        tenant = company_seed.find_company(db, books)
        assert len(summaries) == 3 and all(row[-1] == 1 for row in summaries)  # one unidentified line per month
        bank = db.execute("SELECT name,data FROM erp_catalog_entries WHERE tenant_id=%s AND kind='bank-account' "
                          "AND code='CA-1010'", (tenant["id"],)).fetchone()
        assert bank["name"] == books.bank_label and bank["data"]["bankName"] == books.bank_name
        with pytest.raises(SeedError, match="already has books"):
            company_seed.plan(db, books)
    with pytest.raises(SeedError, match="already has books"):
        company_seed.seed(connect, books, folder / "pack", PASSWORD)


def _sign_in(client, email, codes):
    from security.authentication import totp_code

    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD}, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    code = codes[email] if body.get("mfaMethod") == "email" else totp_code(body["totpSecret"])
    verified = client.post("/api/auth/verify-mfa", json={"code": code}, headers=HEADERS)
    assert verified.status_code == 200, verified.text
    return verified.json()


def test_team_accounts_inbox_roles_and_isolation(fresh_db, monkeypatch):
    import main
    from fastapi.testclient import TestClient
    from services.admin import company_seed, team_setup
    from services.admin.company_tally import tally
    from services.routes.security import routes as security_routes

    connect, folder = fresh_db
    for code in ("CDA", "TKK"):
        company_seed.seed(connect, books_for(code), folder / code, PASSWORD)
    roster = folder / "roster.json"
    roster.write_text(json.dumps({"people": [
        {"slot": "CDA.admin.1", "name": "CDA Admin", "email": "admin+cda@team.demo.test"},
        {"slot": "CDA.accountant.1", "name": "CDA One", "email": "one+cda@team.demo.test", "inbox": "full"},
        {"slot": "CDA.accountant.2", "name": "CDA Two", "email": "two+cda@team.demo.test", "inbox": "starter"},
        {"slot": "TKK.admin.1", "name": "TKK Admin", "email": "admin+tkk@team.demo.test"},
        {"slot": "TKK.accountant.1", "name": "TKK One", "email": "one+tkk@team.demo.test", "inbox": "full"},
    ]}), encoding="utf-8")
    people = team_setup.read_roster(roster)
    with connect() as db:
        rows = team_setup.plan_accounts(db, people)
        assert [action for *_rest, action in rows] == ["create"] * 5
        assert team_setup.apply_accounts(db, rows, PASSWORD) == 5
        db.execute("UPDATE users SET password_hash=NULL WHERE lower(email)='two+cda@team.demo.test'")
        assert [action for *_rest, action in team_setup.plan_accounts(db, people)] == ["exists"] * 5
    # An address already used in another company is refused before anything is written.
    clash = folder / "clash.json"
    clash.write_text(json.dumps({"people": [
        {"slot": "TKK.accountant.2", "name": "Clash", "email": "one+cda@team.demo.test"}]}), encoding="utf-8")
    with connect() as db, pytest.raises(SeedError, match="another company"):
        team_setup.plan_accounts(db, team_setup.read_roster(clash))
    for code, made in (("CDA", 18), ("TKK", 12)):
        assert team_setup.apply_inbox(connect, books_for(code), people, folder / f"{code}-team") == made
        with connect() as db:
            tally(db, books_for(code))  # lanes, posted team invoices, books still balance
            _tenant, work = team_setup.plan_inbox(db, books_for(code), people)
            assert all(not missing for _person, _user, missing in work)
        assert team_setup.apply_inbox(connect, books_for(code), people, folder / f"{code}-team") == 0

    codes = {}
    monkeypatch.setattr(security_routes, "send_login_code", lambda email, code: codes.__setitem__(email, code))
    monkeypatch.setattr(security_routes, "send_reset_code", lambda email, code: codes.__setitem__(email, code))

    def client():
        return closing(TestClient(main.app, base_url="http://127.0.0.1"))

    with connect() as db:
        cda = company_seed.find_company(db, books_for("CDA"))["id"]
        cda_invoice = db.execute("SELECT id FROM erp_document_headers WHERE tenant_id=%s AND document_type='invoice' "
                                 "LIMIT 1", (cda,)).fetchone()["id"]
        cda_bank_line = db.execute("SELECT id FROM bank_transactions WHERE tenant_id=%s LIMIT 1", (cda,)).fetchone()["id"]
        cda_documents = {str(row["id"]) for row in db.execute("SELECT id FROM documents WHERE tenant_id=%s", (cda,))}

    # A person with no password sets their own through Forgot password, then signs in with the email code.
    with client() as anyone:
        assert anyone.post("/api/auth/forgot-password", json={"email": "two+cda@team.demo.test"},
                           headers=HEADERS).status_code == 200
        assert anyone.post("/api/auth/verify-reset-code", json={"email": "two+cda@team.demo.test",
                           "code": codes["two+cda@team.demo.test"]}, headers=HEADERS).status_code == 200
        reset = anyone.post("/api/auth/reset-password", json={"password": PASSWORD, "confirmation": PASSWORD},
                            headers=HEADERS)
        assert reset.status_code == 200, reset.text

    with client() as one, client() as two, client() as admin, client() as other:
        assert _sign_in(one, "one+cda@team.demo.test", codes)["role"] == "ACCOUNTANT"
        assert _sign_in(two, "two+cda@team.demo.test", codes)["role"] == "ACCOUNTANT"
        assert _sign_in(admin, "admin+cda@team.demo.test", codes)["role"] == "LOCAL_ADMIN"  # TOTP enrolment
        assert _sign_in(other, "one+tkk@team.demo.test", codes)["role"] == "ACCOUNTANT"

        mine = one.get("/api/documents", headers=HEADERS).json()
        me = one.get("/api/auth/me", headers=HEADERS).json()["id"]
        assert len(mine) == 12 and all(doc["assignedTo"] == me for doc in mine)
        for lane, count in (("ready", 3), ("waiting-approval", 2), ("needs-fixing", 3)):
            assert len(one.get("/api/documents", params={"lane": lane}, headers=HEADERS).json()) == count, lane
        assert len(two.get("/api/documents", headers=HEADERS).json()) == 6
        assert len(admin.get("/api/documents", headers=HEADERS).json()) == 14 + 12 + 6
        assert one.get("/api/ledger/journals", headers=HEADERS).status_code == 200  # the books are company-wide

        waiting = one.get("/api/documents", params={"lane": "waiting-approval"}, headers=HEADERS).json()[0]
        decision = {"decision": "approved", "expectedContentRevision": waiting["contentRevision"], "note": "ok"}
        path = f"/api/documents/{waiting['id']}/review/decision"
        assert one.post(path, json=decision, headers=HEADERS).status_code == 403
        assert admin.post(path, json=decision, headers=HEADERS).status_code == 200
        periods = {p["periodCode"]: p for p in admin.get("/api/ledger/periods", headers=HEADERS).json()}
        assert periods["2026-09"]["status"].lower() == "closed"
        close = f"/api/ledger/periods/{periods['2026-10']['id']}/close"
        assert one.post(close, json={"reason": "x"}, headers=HEADERS).status_code == 403

        # Another company's accountant sees none of it, even by id.
        theirs = {doc["id"] for doc in other.get("/api/documents", headers=HEADERS).json()}
        assert len(theirs) == 12 and not theirs & cda_documents
        assert other.get(f"/api/documents/{waiting['id']}", headers=HEADERS).status_code == 404
        assert other.get(f"/api/erp/documents/invoice/{cda_invoice}", headers=HEADERS).status_code == 404
        assert other.get(f"/api/bank/transactions/{cda_bank_line}", headers=HEADERS).status_code == 404
        assert admin.get(f"/api/erp/documents/invoice/{cda_invoice}", headers=HEADERS).status_code == 200
