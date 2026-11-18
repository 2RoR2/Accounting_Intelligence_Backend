"""A2 imports and the A3 company setup against the real schema in a disposable local PostgreSQL.

Creates a uniquely named database next to TEST_DATABASE_URL, loads the consolidated schema, imports a
generated company pack through the import code and drops the database afterwards. Skipped without
TEST_DATABASE_URL.
"""
import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row

import main  # noqa: F401  (initialises the route registry)
from ai.synthetic import company_pack
from database.schema_compatibility import apply_migrations
from services.admin import demo_company
from services.imports.common import preview_token
from services.routes.imports import routes as import_routes

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def database(tmp_path_factory):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'a2_imports_' + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE {name}')
    target = urlunsplit(parts._replace(path='/' + name))
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / 'database/PostgreSQL Database schema.sql', tmp_path_factory.mktemp('no-migrations'))
        yield target
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


@pytest.fixture(scope='module')
def pack(tmp_path_factory):
    return company_pack.generate_pack('trading', tmp_path_factory.mktemp('packs'))


@pytest.fixture
def company(database):
    def connect():
        return psycopg.connect(database, row_factory=dict_row)
    with connect() as db:
        tenant = db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,'a@x.test') RETURNING id",
                            ('Synthetic ' + uuid4().hex[:8], uuid4().hex[:12])).fetchone()['id']
        admin = db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
            VALUES (%s,'Synthetic admin',%s,'x','local_admin','active') RETURNING id""", (tenant, uuid4().hex + '@x.test')).fetchone()['id']
        db.execute("INSERT INTO accounting_periods(tenant_id,period_code,starts_on,ends_on) VALUES (%s,'2026-08','2026-08-01','2026-08-31')", (tenant,))
    user = {'id': admin, 'tenant_id': tenant, 'role': 'local_admin', 'name': 'Synthetic admin'}
    with patch.object(import_routes, 'connect', side_effect=connect):
        yield connect, user


def run_import(user, kind, content):
    preview = import_routes._preview(kind, user, content)
    assert preview['canConfirm'], preview['fileErrors'] or [r for r in preview['rows'] if r['errors']][:3]
    return preview, import_routes._confirm(kind, user, content, preview['previewToken'])


def test_pack_imports_into_the_existing_tables(company, pack):
    connect, user = company
    _, coa = run_import(user, 'chart_of_accounts', (pack / 'coa.csv').read_bytes())
    assert coa['chartAccountsCreated'] == coa['ledgerAccountsCreated'] == len(company_pack.COA)
    _, contacts = run_import(user, 'contacts', (pack / 'contacts.csv').read_bytes())
    pack_contacts = demo_company.read_csv(pack / 'contacts.csv')
    assert contacts['suppliersCreated'] == sum(c['type'] == 'supplier' for c in pack_contacts)
    assert contacts['customersCreated'] == sum(c['type'] == 'customer' for c in pack_contacts)
    _, posted = run_import(user, 'opening_balances', (pack / 'opening_balances.csv').read_bytes())
    assert posted['journalStatus'] == 'posted' and posted['totalDebit'] == posted['totalCredit']
    tenant = user['tenant_id']
    opening_rows = demo_company.read_csv(pack / 'opening_balances.csv')
    with connect() as db:
        supplier = db.execute("""SELECT v.registration_number, c.email, c.notes FROM vendors v
            JOIN supplier_contacts c ON c.vendor_id=v.id WHERE v.tenant_id=%s ORDER BY v.vendor_name LIMIT 1""", (tenant,)).fetchone()
        assert supplier['registration_number'] and supplier['email'].endswith('.test') and '"tin"' in supplier['notes']
        balance = {r['code']: r for r in db.execute(
            'SELECT code, total_debit, total_credit FROM internal_trial_balance WHERE tenant_id=%s', (tenant,)).fetchall()}
        for row in opening_rows:
            if row['row_type'] == 'account':
                expected = Decimal(row['debit'] or '0') - Decimal(row['credit'] or '0')
                got = balance[row['account_code']]
                assert got['total_debit'] - got['total_credit'] == expected, row['account_code']
        items = db.execute("""SELECT count(*) n FROM journal_lines l JOIN journal_headers h ON h.id=l.journal_id
            WHERE h.tenant_id=%s AND l.source_field LIKE 'opening_item:%%'""", (tenant,)).fetchone()['n']
        assert items == sum(1 for r in opening_rows if r['row_type'] != 'account')
        actions = {r['action'] for r in db.execute('SELECT action FROM audit_logs WHERE tenant_id=%s', (tenant,)).fetchall()}
        assert {'chart_of_accounts.import_confirmed', 'contacts.import_confirmed', 'opening_balances.import_confirmed'} <= actions

    # A second import of the same files adds nothing: duplicates are reported, the opening journal posts once.
    again = import_routes._preview('contacts', user, (pack / 'contacts.csv').read_bytes())
    assert again['newCount'] == 0 and again['duplicateCount'] == len(pack_contacts) and not again['canConfirm']
    with pytest.raises(HTTPException) as nothing_new:
        import_routes._confirm('contacts', user, (pack / 'contacts.csv').read_bytes(), again['previewToken'])
    assert nothing_new.value.status_code == 409
    opening_again = import_routes._preview('opening_balances', user, (pack / 'opening_balances.csv').read_bytes())
    assert any('already posted' in error for error in opening_again['fileErrors'])


def test_imports_are_scoped_to_one_company(company, pack):
    connect, user = company
    run_import(user, 'chart_of_accounts', (pack / 'coa.csv').read_bytes())
    with connect() as db:
        other = db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,'b@x.test') RETURNING id",
                           ('Other ' + uuid4().hex[:8], uuid4().hex[:12])).fetchone()['id']
    other_user = {**user, 'tenant_id': other}
    preview = import_routes._preview('chart_of_accounts', other_user, (pack / 'coa.csv').read_bytes())
    assert preview['newCount'] == len(company_pack.COA) and preview['duplicateCount'] == 0
    # A preview token from one company does not confirm in another.
    token = preview_token('chart_of_accounts', user['tenant_id'], (pack / 'coa.csv').read_bytes())
    with pytest.raises(HTTPException) as wrong_company:
        import_routes._confirm('chart_of_accounts', other_user, (pack / 'coa.csv').read_bytes(), token)
    assert wrong_company.value.status_code == 409


def test_opening_balances_respect_closed_periods(company, pack):
    connect, user = company
    run_import(user, 'chart_of_accounts', (pack / 'coa.csv').read_bytes())
    with connect() as db:
        db.execute("UPDATE accounting_periods SET status='closed' WHERE tenant_id=%s", (user['tenant_id'],))
    preview = import_routes._preview('opening_balances', user, (pack / 'opening_balances.csv').read_bytes())
    assert not preview['canConfirm'] and any('No open accounting period' in e for e in preview['fileErrors'])
    with pytest.raises(HTTPException) as refused:
        import_routes._confirm('opening_balances', user, (pack / 'opening_balances.csv').read_bytes(), preview['previewToken'])
    assert refused.value.status_code == 422


def test_reversed_opening_journal_can_be_replaced(company, pack):
    from services.routes.accounting import routes as accounting_routes
    connect, user = company
    content = (pack / 'opening_balances.csv').read_bytes()
    run_import(user, 'chart_of_accounts', (pack / 'coa.csv').read_bytes())
    _, first = run_import(user, 'opening_balances', content)
    with patch.object(accounting_routes, 'require_workspace_user', return_value=user), \
         patch.object(accounting_routes, 'connect', side_effect=connect):
        accounting_routes.reverse_journal(first['journalId'], None)
    _, second = run_import(user, 'opening_balances', content)
    assert second['journalId'] != first['journalId'] and second['journalStatus'] == 'posted'
    with connect() as db:
        rows = db.execute("""SELECT idempotency_key, status FROM journal_headers
            WHERE tenant_id=%s AND idempotency_key LIKE 'opening-balances:%%' ORDER BY created_at""", (user['tenant_id'],)).fetchall()
    as_at = demo_company.read_csv(pack / 'opening_balances.csv')[0]['as_at']
    assert [(r['idempotency_key'], r['status']) for r in rows] == [
        (f'opening-balances:{as_at}', 'reversed'), (f'opening-balances:{as_at}:2', 'posted')]
    # With the replacement posted, the date is blocked again.
    again = import_routes._preview('opening_balances', user, content)
    assert any('already posted' in error for error in again['fileErrors'])


def test_seed_company_and_users_are_created_once(database, pack):
    info = json.loads((pack / 'pack.json').read_text(encoding='utf-8'))
    key = 'it' + uuid4().hex[:6]
    info['company'] = {**info['company'], 'ssm': uuid4().hex[:12], 'name': 'Seed ' + key}
    with psycopg.connect(database, row_factory=dict_row) as db:
        db.execute("DELETE FROM users WHERE email='superadmin@demo.test'")
        tenant, users, passwords = demo_company.ensure_company(db, key, info)
        assert set(users) == {'accountant', 'local_admin', 'super_admin'}
        assert users['super_admin']['tenant_id'] is None and users['accountant']['tenant_id'] == tenant['id']
        assert all(len(password) >= 12 for _, _, password in passwords)
        again, users_again, passwords_again = demo_company.ensure_company(db, key, info)
        assert again['id'] == tenant['id'] and {p for _, _, p in passwords_again} == {'(unchanged)'}
        assert users_again['accountant']['id'] == users['accountant']['id']
