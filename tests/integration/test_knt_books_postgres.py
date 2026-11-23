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
            assert first[-1][3:7] == tuple(map(Decimal, ("4032.00", "2402.00", "25368.76", "25538.76")))
            assert db.execute("SELECT id FROM tenants WHERE tenant_code='KNT'").fetchone()["id"] == legacy
            assert db.execute("SELECT 1 FROM users WHERE id=%s", (owner,)).fetchone()
            assert db.execute("SELECT 1 FROM tenants WHERE id=%s", (other,)).fetchone()
            assert not db.execute("SELECT 1 FROM documents WHERE id=%s", (benchmark,)).fetchone()
            assert not db.execute("SELECT 1 FROM benchmark_demo_tenants WHERE tenant_id=%s", (legacy,)).fetchone()
            assert (ROOT / "ai/assets/invoice/INV00001.pdf").is_file()
            assert db.execute("""SELECT count(*) AS n FROM erp_document_links
                WHERE tenant_id=%s AND link_type='transfer'""", (legacy,)).fetchone()["n"] == 3
            db.execute("UPDATE bank_reconciliation_periods SET closing_balance=closing_balance+1 WHERE tenant_id=%s",
                       (legacy,))
            with pytest.raises(SeedError, match="closingBalance"):
                tally(db)
            db.rollback()
        reset(connect, local_knt, "FixtureDemo!2026")
        with connect() as db:
            assert tally(db) == first
