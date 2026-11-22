"""Plan L1: the sales/purchases ("ERP") tables are schema, not request-time DDL.

Checks, on disposable local databases next to TEST_DATABASE_URL (loopback only):
- a fresh database bootstraps from the consolidated schema and migrate re-runs the module file harmlessly;
- a database built from the previous consolidated schema (no ERP tables) is upgraded by migrate;
- check_runtime_schema accepts both and names a missing ERP table;
- tenant-scoped foreign keys refuse a cross-tenant contact;
- typed documents can post with the new journal source type.
Skipped without TEST_DATABASE_URL.
"""
import os
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from database.schema_compatibility import SchemaCompatibilityError, apply_migrations, check_runtime_schema

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / 'database' / 'PostgreSQL Database schema.sql'
MIGRATIONS = ROOT / 'database' / 'migrations'
MODULE = MIGRATIONS / '20261122_erp_module.sql'
ERP_TABLES = {'erp_employees', 'erp_products', 'erp_catalog_entries', 'erp_posting_accounts', 'erp_number_counters',
              'erp_document_headers', 'erp_document_allocations', 'erp_document_links', 'erp_document_events',
              'erp_document_shares', 'myinvois_consolidated_submissions', 'myinvois_consolidated_documents'}


@contextmanager
def scratch_database():
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'erp_schema_' + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute('CREATE DATABASE ' + name)
    try:
        yield parts._replace(path='/' + name).geturl()
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


def _tables(db):
    rows = db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
    return {row['tablename'] for row in rows}


def _previous_schema():
    """The consolidated schema as it was before this module: everything above its section marker."""
    text = SCHEMA.read_text(encoding='utf-8-sig')
    head = text[:text.index('-- Consolidated migration: 20261122_erp_module.sql')]
    return head[:head.rstrip().rfind('\n')]  # drop the "====" line above the marker


def test_no_request_time_ddl_in_route_modules():
    """No route module creates or alters an ERP/MyInvois table while serving a request (latest_update's
    _ensure_erp_schema). The older bank-reconciliation DDL in services/routes/bank is outside Plan L."""
    ddl = re.compile(r'(CREATE\s+TABLE|ALTER\s+TABLE)(\s+IF\s+(NOT\s+)?EXISTS)?\s+(erp_|myinvois_|vendors|customers)|_ensure_erp_schema',
                     re.IGNORECASE)
    offenders = []
    for path in (ROOT / 'services' / 'routes').rglob('*.py'):
        if ddl.search(path.read_text(encoding='utf-8')):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_module_file_is_in_the_consolidated_schema():
    schema = SCHEMA.read_text(encoding='utf-8-sig').replace('\r\n', '\n')
    assert MODULE.read_text(encoding='utf-8').replace('\r\n', '\n').strip() in schema


def test_fresh_database_bootstraps_and_migrate_reruns_the_module_harmlessly():
    with scratch_database() as url, psycopg.connect(url, row_factory=dict_row) as db:
        applied = apply_migrations(db, SCHEMA, MIGRATIONS)
        assert applied[0] == 'consolidated schema'
        assert '20261122_erp_module.sql' in applied  # runs again on top of the schema that already has it
        assert ERP_TABLES <= _tables(db)
        assert apply_migrations(db, SCHEMA, MIGRATIONS) == []
        check_runtime_schema(db)


def test_previous_database_is_upgraded_by_migrate_and_checked():
    with scratch_database() as url, psycopg.connect(url, row_factory=dict_row) as db:
        db.execute(_previous_schema())
        db.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for migration in sorted(MIGRATIONS.glob('*.sql')):
            if migration.name < '20261122':
                db.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (migration.name,))
        assert not (ERP_TABLES & _tables(db))
        with pytest.raises(SchemaCompatibilityError) as missing:
            check_runtime_schema(db)
        assert 'erp_document_headers' in str(missing.value)
        db.commit()
        assert apply_migrations(db, SCHEMA, MIGRATIONS) == ['20261122_erp_module.sql']
        assert ERP_TABLES <= _tables(db)
        check_runtime_schema(db)
        columns = {row['column_name'] for row in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='vendors'").fetchall()}
        assert {'email', 'phone', 'is_active', 'fields', 'contact_code'} <= columns


def test_tenant_scoped_keys_and_new_journal_source():
    with scratch_database() as url, psycopg.connect(url, row_factory=dict_row) as db:
        apply_migrations(db, SCHEMA, MIGRATIONS)

        def company(label):
            key = uuid4().hex
            return db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,'a@x.test') RETURNING id",
                              (label + ' ' + key[:8], key[:12])).fetchone()['id']

        own, other = company('Own'), company('Other')
        admin = db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
            VALUES (%s,'Admin',%s,'x','local_admin','active') RETURNING id""", (own, uuid4().hex + '@x.test')).fetchone()['id']
        foreign_vendor = db.execute("INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,'Other supplier') RETURNING id",
                                    (other,)).fetchone()['id']
        vendor = db.execute("INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,'Own supplier') RETURNING id, is_active, fields",
                            (own,)).fetchone()
        customer = db.execute("INSERT INTO customers(tenant_id,customer_name) VALUES (%s,'Both') RETURNING id", (own,)).fetchone()['id']
        assert vendor['is_active'] is True and vendor['fields'] == {}
        db.commit()

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,vendor_id)
                VALUES (%s,'bill','BL-1',%s)""", (own, foreign_vendor))
        db.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,vendor_id,customer_id)
                VALUES (%s,'bill','BL-2',%s,%s)""", (own, vendor['id'], customer))
        db.rollback()

        period = db.execute("""INSERT INTO accounting_periods(tenant_id,period_code,starts_on,ends_on)
            VALUES (%s,'2026-08','2026-08-01','2026-08-31') RETURNING id""", (own,)).fetchone()['id']
        journal = db.execute("""INSERT INTO journal_headers(tenant_id,period_id,idempotency_key,source_type,source_id,journal_date,description,posted_by,status)
            VALUES (%s,%s,'erp:test','erp_document',gen_random_uuid(),'2026-08-10','Typed bill',%s,'draft') RETURNING id""",
                             (own, period, admin)).fetchone()['id']
        document = db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,vendor_id,status,journal_id)
            VALUES (%s,'bill','BL-3',%s,'ready',%s) RETURNING id""", (own, vendor['id'], journal)).fetchone()['id']
        db.commit()
        with pytest.raises(psycopg.errors.CheckViolation):  # a posted document cannot go back to draft
            db.execute("UPDATE erp_document_headers SET status='draft' WHERE id=%s", (document,))
        db.rollback()


def test_database_with_latest_update_prototype_tables_is_upgraded_without_losing_data():
    """A database that served latest_update's request-time ERP tables: they become legacy_*, contacts join the shared lists."""
    prototype = (Path(__file__).resolve().parent / 'fixtures' / 'latest_update_erp_prototype.sql').read_text(encoding='utf-8')
    with scratch_database() as url, psycopg.connect(url, row_factory=dict_row) as db:
        db.execute(_previous_schema())
        db.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for migration in sorted(MIGRATIONS.glob('*.sql')):
            if migration.name < '20261122':
                db.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (migration.name,))
        db.execute(prototype)
        tenant = db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES ('Proto','P1','a@x.test') RETURNING id").fetchone()['id']
        db.execute("INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,'Existing Supplier')", (tenant,))
        db.execute("""INSERT INTO erp_contacts(tenant_id,kind,legal_name,email) VALUES
            (%s,'supplier','Existing Supplier','dup@x.test'),(%s,'supplier','New Supplier','new@x.test'),
            (%s,'customer','Typed Customer',NULL),(%s,'employee','Staff One',NULL)""", (tenant,) * 4)
        db.execute("""INSERT INTO erp_document_headers(tenant_id,document_type,document_number,total_amount)
            VALUES (%s,'invoice','IV-00001',100)""", (tenant,))
        db.commit()
        assert apply_migrations(db, SCHEMA, MIGRATIONS) == ['20261122_erp_module.sql']
        check_runtime_schema(db)
        tables = _tables(db)
        assert {'legacy_erp_contacts', 'legacy_erp_document_headers'} <= tables and ERP_TABLES <= tables
        assert db.execute("SELECT count(*) AS n FROM legacy_erp_document_headers").fetchone()['n'] == 1
        vendors = {r['vendor_name']: r['email'] for r in db.execute("SELECT vendor_name,email FROM vendors WHERE tenant_id=%s", (tenant,))}
        assert vendors == {'Existing Supplier': None, 'New Supplier': 'new@x.test'}  # the existing row wins
        assert db.execute("SELECT customer_name FROM customers WHERE tenant_id=%s", (tenant,)).fetchone()['customer_name'] == 'Typed Customer'
        assert db.execute("SELECT legal_name FROM erp_employees WHERE tenant_id=%s", (tenant,)).fetchone()['legal_name'] == 'Staff One'
        vendor = db.execute("SELECT id FROM vendors WHERE tenant_id=%s AND vendor_name='New Supplier'", (tenant,)).fetchone()['id']
        db.execute("INSERT INTO erp_document_headers(tenant_id,document_type,document_number,vendor_id) VALUES (%s,'bill','BL-1',%s)",
                   (tenant, vendor))
        db.commit()
        assert apply_migrations(db, SCHEMA, MIGRATIONS) == []
