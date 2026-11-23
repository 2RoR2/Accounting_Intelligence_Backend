"""Exercise the KNT reset and tally against the real schema and routes."""
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from ai.synthetic.knt_pack import COMPANY
from database.schema_compatibility import apply_migrations
from services.admin.demo_company import SeedError, _remote_destination
from services.admin.knt_reset import reset
from services.admin.knt_seed import locked_database, require_local
from services.admin.knt_tally import tally

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def local_knt(monkeypatch, tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a disposable loopback TEST_DATABASE_URL")
    if _remote_destination(url, os.environ):
        pytest.fail("KNT tests require a disposable loopback PostgreSQL database")
    name = "test_knt_" + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = urlsplit(url)._replace(path="/" + name).geturl()
    monkeypatch.setenv("DATABASE_URL", target)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_SECRET", "synthetic-auth-secret-for-knt-fixture-tests")
    monkeypatch.setenv("S3_BUCKET", "")
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    monkeypatch.setenv("DOCUMENT_STORAGE_PATH", str(tmp_path / "objects"))
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / "database/PostgreSQL Database schema.sql", ROOT / "database/migrations")
        yield tmp_path / "pack"
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.parametrize("url,extras", [
    ("postgresql://operator@shared.example/demo", {}),
    ("postgresql://operator@shared.example/demo?host=localhost", {}),
    ("postgresql://operator@127.0.0.1/demo?hostaddr=192.0.2.1", {}),
    ("postgresql://operator@localhost/demo", {"PGHOSTADDR": "192.0.2.1"}),
    ("postgresql://operator@localhost/demo?service=elsewhere", {}),
    ("postgresql://operator@127.0.0.1/demo", {"S3_BUCKET": "shared"}),
])
def test_local_guard_refuses_remote_database_or_storage(monkeypatch, url, extras):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.setenv("S3_BUCKET", "")
    for key, value in extras.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SeedError, match="loopback") as error:
        require_local()
    assert url not in str(error.value)


def test_reset_removes_legacy_benchmark_and_tally_detects_drift(local_knt):
    with locked_database() as connect:
        with connect() as db:
            legacy = db.execute("""INSERT INTO tenants(name,registration_number,admin_email)
                VALUES (%s,%s,'admin@trading.demo.test') RETURNING id""",
                                (COMPANY["name"], COMPANY["ssm"])).fetchone()["id"]
            other = db.execute("""INSERT INTO tenants(name,registration_number,admin_email)
                VALUES ('Independent workspace','INDEPENDENT-001','other@example.test') RETURNING id""").fetchone()["id"]
            owner = db.execute("""INSERT INTO users(tenant_id,name,email,role,status)
                VALUES (%s,'Legacy demo admin','admin@trading.demo.test','local_admin','active') RETURNING id""",
                               (legacy,)).fetchone()["id"]
            benchmark = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,
                    storage_path,mime_type,file_size_bytes,file_hash_sha256)
                VALUES (%s,%s,'TechVision-Sharma.pdf','invoice','accounts_receivable',
                        'ai/assets/invoice/INV00001.pdf','application/pdf',100,'legacy-fixture') RETURNING id""",
                                   (legacy, owner)).fetchone()["id"]
            db.execute("""INSERT INTO benchmark_demo_tenants(tenant_id,enabled,approved_by,rationale)
                VALUES (%s,true,%s,'Legacy benchmark import')""", (legacy, owner))
            db.execute("""INSERT INTO benchmark_demo_documents(tenant_id,dataset_id,benchmark_revision,demo_code,
                    sample_id,source_sha256,gold_manifest_sha256,document_id,imported_by)
                VALUES (%s,'eval','v1','INV00001','old',repeat('a',64),repeat('b',64),%s,%s)""",
                       (legacy, benchmark, owner))
        reset(connect, local_knt, "FixtureDemo!2026")
        with connect() as db:
            first = tally(db)
            assert [row[-1] for row in first] == [1, 1, 1]
            # AR, AP, bank ledger, bank statement: the bank figures are the original fixture's, unchanged by the
            # richer data (its payments go to cash in hand).
            assert first[-1][3:7] == tuple(map(Decimal, ("71450.13", "56751.38", "25368.76", "25538.76")))
            counts = {table: db.execute(f"SELECT count(*) AS n FROM {table} WHERE tenant_id=%s", (legacy,)).fetchone()["n"]
                      for table in ("customers", "vendors", "erp_products")}
            assert counts == {"customers": 17, "vendors": 13, "erp_products": 41}
            assert db.execute("""SELECT count(*) AS n FROM erp_catalog_entries WHERE tenant_id=%s
                AND kind IN ('tax-code','payment-term')""", (legacy,)).fetchone()["n"] == 9
            assert db.execute("SELECT id FROM tenants WHERE tenant_code='KNT'").fetchone()["id"] == legacy
            assert db.execute("SELECT 1 FROM users WHERE id=%s", (owner,)).fetchone()
            assert db.execute("SELECT 1 FROM tenants WHERE id=%s", (other,)).fetchone()
            assert not db.execute("SELECT 1 FROM documents WHERE id=%s", (benchmark,)).fetchone()
            assert not db.execute("SELECT 1 FROM benchmark_demo_tenants WHERE tenant_id=%s", (legacy,)).fetchone()
            assert (ROOT / "ai/assets/invoice/INV00001.pdf").is_file()
            assert db.execute("""SELECT count(*) AS n FROM erp_document_links
                WHERE tenant_id=%s AND link_type='transfer'""", (legacy,)).fetchone()["n"] == 20
            db.execute("UPDATE bank_reconciliation_periods SET closing_balance=closing_balance+1 WHERE tenant_id=%s",
                       (legacy,))
            with pytest.raises(SeedError, match="closingBalance"):
                tally(db)
            db.rollback()
        reset(connect, local_knt, "FixtureDemo!2026")
        with connect() as db:
            assert tally(db) == first


@pytest.mark.parametrize("allow,host_env,message", [
    (False, None, "not on this machine"),
    (True, None, "not on this machine"),
    (True, "other.example", "not on this machine"),
])
def test_extend_refuses_a_remote_database_unless_confirmed_by_name(allow, host_env, message):
    from services.admin.knt_extend import require_target

    environ = {"DATABASE_URL": "postgresql://operator@db.example.invalid/uat"}
    if host_env:
        environ["KNT_EXTEND_REMOTE_HOST"] = host_env
    with pytest.raises(SeedError, match=message):
        require_target(allow, environ)
    assert callable(require_target(True, {**environ, "KNT_EXTEND_REMOTE_HOST": "DB.example.invalid"}))
    assert callable(require_target(False, {"DATABASE_URL": "postgresql://operator@127.0.0.1/knt"}))


def test_extend_adds_the_rich_data_to_original_books_without_touching_them(local_knt, monkeypatch):
    """The hosted-UAT path: books built from the original fixture only, then extended in place."""
    from ai.synthetic import knt_extra, knt_pack
    from services.admin import knt_extend, knt_seed

    preserved = ("tenants", "users", "documents", "raw_extractions", "standardised_records", "journal_headers",
                 "journal_lines", "bank_imports", "bank_transactions", "bank_reconciliation_periods",
                 "erp_document_headers", "erp_document_links", "erp_document_allocations", "settlements",
                 "settlement_allocations", "customers", "vendors", "ledger_accounts")

    def fingerprints(db):
        return {table: {row["h"] for row in db.execute(f"SELECT md5(row_to_json(t)::text) AS h FROM {table} t").fetchall()}
                for table in preserved}

    with locked_database() as connect:  # held only while rebuilding: extend takes the same lock itself
        with monkeypatch.context() as original:  # build what hosted UAT holds today: the original fixture alone
            original.setattr(knt_seed, "books", knt_pack.original_books)
            original.setattr(knt_seed, "extras", lambda: ([], [], []))
            original.setattr(knt_seed, "setup_catalogue", lambda api, accounts: (None, {}))
            original.setattr(knt_seed, "CONTACTS", {code: knt_pack.CONTACTS[code] for code in knt_pack.BASE_CONTACTS})
            reset(connect, local_knt, "FixtureDemo!2026")
    with connect() as db:
        tenant = db.execute("SELECT id FROM tenants WHERE tenant_code='KNT'").fetchone()["id"]
        before = fingerprints(db)
        assert db.execute("SELECT count(*) AS n FROM erp_products").fetchone()["n"] == 0
        found = knt_extend.plan(knt_extend.read_state(db, tenant), knt_extra.extension())
        assert {kind: len(items) for kind, items in found.items()} == {
            "contacts": 25, "tax codes": 4, "payment terms": 5, "cash account": 1, "products": 41,
            "quotations": 8, "sales documents": 36, "supplier bills": 27, "payments": 30}
    # Dry run writes nothing; --apply adds everything; the books still tally.
    assert knt_extend.extend(connect, "admin@knt.demo.test") is None
    with connect() as db:
        assert fingerprints(db) == before
    summaries = knt_extend.extend(connect, "admin@knt.demo.test", apply=True)
    assert summaries[-1][3:7] == tuple(map(Decimal, ("71450.13", "56751.38", "25368.76", "25538.76")))
    with connect() as db:
        after = fingerprints(db)
        for table in preserved:  # every row that was there is still there, unedited
            assert before[table] <= after[table], table
        assert tally(db) == summaries
        assert all(not items for items in knt_extend.plan(knt_extend.read_state(db, tenant),
                                                          knt_extra.extension()).values())
    # A second run has nothing to do.
    knt_extend.extend(connect, "admin@knt.demo.test", apply=True)
    with connect() as db:
        assert fingerprints(db) == after
    with pytest.raises(SeedError, match="company admin"):
        knt_extend.extend(connect, "accountant@knt.demo.test")
