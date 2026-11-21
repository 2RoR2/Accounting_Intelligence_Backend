"""Codex review fixes on the teammates' accounting routes (PR #18), on the full schema in a disposable loopback TEST_DATABASE_URL.

Covers: reconfirming after an undone allocation (settlement_allocation_guard), refusing to reverse a reversal,
aging that lists only open items (invoices and bills), and the cash P&L keeping a coded bank refund that was
allocated in a direction the report does not count.
"""
import hashlib
import os
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import main  # noqa: F401 - initialise the application's route registry before route imports.
from services.routes.accounting import routes as accounting

SCHEMA_FILE = Path(__file__).resolve().parents[2] / 'database' / 'PostgreSQL Database schema.sql'
DAY = date(2026, 8, 15)


@pytest.fixture(scope='module')
def ledger():
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'accounting_fixes_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute('CREATE DATABASE ' + name)
    target = parts._replace(path='/' + name).geturl()

    @contextmanager
    def connect():
        with psycopg.connect(target, row_factory=dict_row) as db:
            yield db

    try:
        with connect() as db:
            db.execute(SCHEMA_FILE.read_text(encoding='utf-8-sig'))
        yield connect, _seed(connect)
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


def _seed(connect):
    with connect() as db:
        tenant = db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES ('Fix Check','FIX-1','a@fix.test') RETURNING id").fetchone()['id']
        admin = db.execute("INSERT INTO users(tenant_id,name,email,role,status) VALUES (%s,'Admin','admin@fix.test','local_admin','active') RETURNING id",
                           (tenant,)).fetchone()['id']
        period = db.execute("INSERT INTO accounting_periods(tenant_id,period_code,starts_on,ends_on) VALUES (%s,'2026-08','2026-08-01','2026-08-31') RETURNING id",
                            (tenant,)).fetchone()['id']
        accounts = {code: db.execute("INSERT INTO ledger_accounts(tenant_id,code,name,account_type) VALUES (%s,%s,%s,%s) RETURNING id",
                                     (tenant, code, code, kind)).fetchone()['id']
                    for code, kind in (('5000', 'expense'), ('2000', 'liability'), ('1010', 'asset'))}

        def record(kind, total):
            document = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,
                    file_size_bytes,file_hash_sha256,status,uploaded_at)
                VALUES (%s,%s,%s,%s,'accounts_payable','synthetic/none.pdf','application/pdf',100,%s,'completed',%s) RETURNING id""",
                                  (tenant, admin, f'{kind}.pdf', kind, hashlib.sha256(uuid4().bytes).hexdigest(), DAY)).fetchone()['id']
            submission = db.execute("INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision) VALUES (%s,%s,%s,'submitted',1) RETURNING id",
                                    (document, tenant, admin)).fetchone()['id']
            db.execute("INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision) VALUES (%s,%s,%s,%s,'approved',1)",
                       (submission, document, tenant, admin))
            return db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,supplier_name,invoice_date,due_date,
                    currency,total_amount,snapshot) VALUES (%s,%s,'accounts_payable','Synthetic Supplier',%s,%s,'MYR',%s,%s) RETURNING id""",
                              (document, tenant, DAY, DAY - timedelta(days=10), total, Jsonb({}))).fetchone()['id']

        records = {kind: record(kind, total) for kind, total in (('bill', '100.00'), ('receipt', '40.00'), ('credit_note', '25.00'))}
    return {'tenant': tenant, 'admin': admin, 'period': period, 'accounts': accounts, 'records': records}


def _as_admin(ledger_fixture):
    connect, ids = ledger_fixture
    user = {'tenant_id': ids['tenant'], 'id': ids['admin'], 'name': 'Admin', 'role': 'local_admin'}
    return patch.multiple(accounting, connect=connect, require_workspace_user=lambda *_args: user), ids


def test_reallocating_after_an_undone_allocation_is_not_blocked(ledger):
    connect, ids = ledger
    with connect() as db:
        settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,source_type,created_by)
            VALUES (%s,'SET-REALLOC',%s,'money_out','100.00','MYR','manual_record',%s) RETURNING id""",
                                (ids['tenant'], DAY, ids['admin'])).fetchone()['id']
        first = db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,'100.00',%s) RETURNING id",
                           (ids['tenant'], settlement, ids['records']['bill'], ids['admin'])).fetchone()['id']
        db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,status,created_by,reversed_at,reversal_reason,reverses_allocation_id)
            VALUES (%s,%s,%s,'100.00','reversed',%s,now(),'Allocation undone',%s)""",
                   (ids['tenant'], settlement, ids['records']['bill'], ids['admin'], first))
        # Before the fix the undone row still counted, so this raised "Settlement allocation exceeds the unmatched amount".
        again = db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,'100.00',%s) RETURNING id",
                           (ids['tenant'], settlement, ids['records']['bill'], ids['admin'])).fetchone()
        assert again['id']
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,'0.01',%s)",
                       (ids['tenant'], settlement, ids['records']['bill'], ids['admin']))
        db.rollback()


def test_a_reversal_cannot_be_reversed(ledger):
    patcher, ids = _as_admin(ledger)
    with patcher:
        journal = accounting.post_journal(accounting.JournalInput(
            periodId=str(ids['period']), idempotencyKey='manual:' + uuid4().hex, sourceType='manual', journalDate=DAY,
            description='Synthetic expense', lines=[
                accounting.JournalLineInput(accountId=str(ids['accounts']['5000']), debit=Decimal('10.00')),
                accounting.JournalLineInput(accountId=str(ids['accounts']['1010']), credit=Decimal('10.00'))]), None)
        reversal = accounting.reverse_journal(journal['id'], None)
        with pytest.raises(HTTPException) as error:
            accounting.reverse_journal(reversal['id'], None)
    assert error.value.status_code == 409
    assert 'A reversal cannot be reversed' in error.value.detail


def test_aging_lists_only_invoices_and_bills(ledger):
    patcher, ids = _as_admin(ledger)
    with patcher:
        report = accounting.get_profit_and_loss_dashboard(None, DAY, DAY, 'accrual', 'month')
    payables = [row for row in report['aging'] if row['category'] == 'accounts_payable']
    # The bill (100.00, possibly part-allocated by another test) is open; the receipt and the credit note are not.
    assert sum(row['itemCount'] for row in payables) <= 1
    assert all(Decimal(row['amount']) <= Decimal('100.00') for row in payables)


def test_cash_report_keeps_a_coded_refund_allocated_against_the_report_direction(ledger):
    connect, ids = ledger
    with connect() as db:
        batch = db.execute("""INSERT INTO bank_imports(tenant_id,source_bank_account_ref,source_hash,input_format,mapping_version,imported_by)
            VALUES (%s,'FIX-BANK',%s,'csv','v1',%s) RETURNING id""", (ids['tenant'], hashlib.sha256(uuid4().bytes).hexdigest(), ids['admin'])).fetchone()['id']
        refund = db.execute("""INSERT INTO bank_transactions(tenant_id,import_id,source_row,posted_date,description,credit_decimal,
                signed_amount_decimal,direction,currency,fingerprint,status)
            VALUES (%s,%s,1,%s,'Supplier refund','30.00','30.00','money_in','MYR',%s,'imported') RETURNING id""",
                            (ids['tenant'], batch, DAY, hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()['id']
        expense = db.execute("INSERT INTO coa_accounts(tenant_id,code,name,account_class) VALUES (%s,'6100','Supplies','expense') RETURNING id",
                             (ids['tenant'],)).fetchone()['id']
        db.execute("INSERT INTO bank_coding_decisions(tenant_id,bank_transaction_id,user_id,decision,account_id,reason) VALUES (%s,%s,%s,'manual',%s,'Supplier refund')",
                   (ids['tenant'], refund, ids['admin'], expense))
        settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,source_type,source_id,created_by)
            VALUES (%s,'BANK-REFUND',%s,'money_in','30.00','MYR','bank_import',%s,%s) RETURNING id""",
                                (ids['tenant'], DAY, refund, ids['admin'])).fetchone()['id']
        # A money-in settlement allocated to a payable: allocation_rows does not count it.
        db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,'30.00',%s)",
                   (ids['tenant'], settlement, ids['records']['receipt'], ids['admin']))
        db.commit()
    patcher, ids = _as_admin(ledger)
    with patcher:
        report = accounting.get_profit_and_loss_dashboard(None, DAY, DAY, 'cash', 'month')
    refund_rows = [row for row in report['accounts'] if row['accountCode'] == '6100']
    assert refund_rows and Decimal(refund_rows[0]['amount']) == Decimal('-30.00')


def test_migrate_upgrades_an_existing_database_to_the_new_allocation_guard(tmp_path):
    """An existing database keeps the old guard until `manage.py migrate` runs the migration file."""
    from database.schema_compatibility import apply_migrations
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    root = Path(__file__).resolve().parents[2]
    # A database created before the fix: the current schema with the old guard (a checked-in copy) put back.
    old_guard = (Path(__file__).resolve().parent / 'fixtures' / 'settlement_allocation_guard_before_20261121.sql').read_text(encoding='utf-8')
    assert 'reverses_allocation_id' not in old_guard
    name = 'accounting_upgrade_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute('CREATE DATABASE ' + name)
    target = parts._replace(path='/' + name).geturl()

    @contextmanager
    def connect():
        with psycopg.connect(target, row_factory=dict_row) as db:
            yield db

    try:
        with connect() as db:
            db.execute(SCHEMA_FILE.read_text(encoding='utf-8-sig'))
            db.execute(old_guard)
        ids = _seed(connect)

        def reallocate():
            with connect() as db:
                settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,source_type,created_by)
                    VALUES (%s,%s,%s,'money_out','100.00','MYR','manual_record',%s) RETURNING id""",
                                        (ids['tenant'], 'SET-' + uuid4().hex[:8], DAY, ids['admin'])).fetchone()['id']
                first = db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,'100.00',%s) RETURNING id",
                                   (ids['tenant'], settlement, ids['records']['bill'], ids['admin'])).fetchone()['id']
                db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,status,created_by,reversed_at,reversal_reason,reverses_allocation_id)
                    VALUES (%s,%s,%s,'100.00','reversed',%s,now(),'Allocation undone',%s)""",
                           (ids['tenant'], settlement, ids['records']['bill'], ids['admin'], first))
                db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,'100.00',%s)",
                           (ids['tenant'], settlement, ids['records']['bill'], ids['admin']))

        with pytest.raises(psycopg.errors.RaiseException):
            reallocate()  # the old guard still counts the undone row
        with connect() as db:
            applied = apply_migrations(db, root / 'database' / 'PostgreSQL Database schema.sql', root / 'database' / 'migrations')
        assert '20261121_settlement_allocation_guard_reversals.sql' in applied
        reallocate()
        with connect() as db:  # idempotent: a second migrate applies nothing
            assert apply_migrations(db, root / 'database' / 'PostgreSQL Database schema.sql', root / 'database' / 'migrations') == []
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


def test_settlement_list_does_not_count_an_undone_allocation(ledger):
    connect, ids = ledger
    with connect() as db:
        settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,source_type,created_by)
            VALUES (%s,'SET-SHOWN',%s,'money_out','100.00','MYR','manual_record',%s) RETURNING id""",
                                (ids['tenant'], DAY, ids['admin'])).fetchone()['id']
        first = db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,'100.00',%s) RETURNING id",
                           (ids['tenant'], settlement, ids['records']['bill'], ids['admin'])).fetchone()['id']
        db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,status,created_by,reversed_at,reversal_reason,reverses_allocation_id)
            VALUES (%s,%s,%s,'100.00','reversed',%s,now(),'Allocation undone',%s)""",
                   (ids['tenant'], settlement, ids['records']['bill'], ids['admin'], first))
        db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,'100.00',%s)",
                   (ids['tenant'], settlement, ids['records']['bill'], ids['admin']))
        db.commit()
    patcher, ids = _as_admin(ledger)
    with patcher:
        shown = next(row for row in accounting.list_settlements(None) if row['settlementRef'] == 'SET-SHOWN')
        allocations = [row for row in accounting.list_allocations(None) if row['settlementRef'] == 'SET-SHOWN']
    assert (shown['allocated'], shown['unmatchedAmount']) == ('100.00', '0.00')
    assert {row['unmatchedSettlementAmount'] for row in allocations} == {'0.00'}


def test_cash_report_recognises_a_bank_payment_on_its_bank_date(ledger):
    """The settlement was recorded later than the bank posting; the payment counts once, in the bank's month."""
    connect, ids = ledger
    bank_day, recorded_day = date(2026, 7, 30), date(2026, 8, 2)
    with connect() as db:
        batch = db.execute("""INSERT INTO bank_imports(tenant_id,source_bank_account_ref,source_hash,input_format,mapping_version,imported_by)
            VALUES (%s,'FIX-BANK-2',%s,'csv','v1',%s) RETURNING id""", (ids['tenant'], hashlib.sha256(uuid4().bytes).hexdigest(), ids['admin'])).fetchone()['id']
        payment = db.execute("""INSERT INTO bank_transactions(tenant_id,import_id,source_row,posted_date,description,debit_decimal,
                signed_amount_decimal,direction,currency,fingerprint,status)
            VALUES (%s,%s,1,%s,'Supplier payment','60.00','-60.00','money_out','MYR',%s,'matched') RETURNING id""",
                             (ids['tenant'], batch, bank_day, hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()['id']
        settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,source_type,source_id,created_by)
            VALUES (%s,'BANK-PAY',%s,'money_out','60.00','MYR','bank_import',%s,%s) RETURNING id""",
                                (ids['tenant'], recorded_day, payment, ids['admin'])).fetchone()['id']
        bill = db.execute("SELECT id FROM standardised_records WHERE tenant_id=%s AND id=%s", (ids['tenant'], ids['records']['credit_note'])).fetchone()['id']
        db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,'25.00',%s)",
                   (ids['tenant'], settlement, bill, ids['admin']))
        db.commit()
    patcher, ids = _as_admin(ledger)
    with patcher:
        july = accounting.get_profit_and_loss_dashboard(None, date(2026, 7, 1), date(2026, 7, 31), 'cash', 'month')
    july_ap = [row for row in july['accounts'] if row['accountCode'] == 'ALLOC-AP']
    assert july_ap and Decimal(july_ap[0]['amount']) == Decimal('25.00')


def test_bank_transaction_detail_names_the_payable_account_the_matched_bill_was_posted_to(ledger):
    """The payment form preselects this account; the posting endpoint requires it."""
    from services.routes.bank import routes as bank
    connect, ids = ledger
    patcher, ids = _as_admin(ledger)
    with patcher:
        accounting.post_journal(accounting.JournalInput(
            periodId=str(ids['period']), idempotencyKey='bill-posting:' + uuid4().hex, sourceType='document',
            sourceId=str(_document_of(connect, ids['records']['bill'])), journalDate=DAY, description='Synthetic bill',
            lines=[accounting.JournalLineInput(accountId=str(ids['accounts']['5000']), debit=Decimal('100.00')),
                   accounting.JournalLineInput(accountId=str(ids['accounts']['2000']), credit=Decimal('100.00'))]), None)
    with connect() as db:
        batch = db.execute("""INSERT INTO bank_imports(tenant_id,source_bank_account_ref,source_hash,input_format,mapping_version,imported_by)
            VALUES (%s,'FIX-BANK-3',%s,'csv','v1',%s) RETURNING id""", (ids['tenant'], hashlib.sha256(uuid4().bytes).hexdigest(), ids['admin'])).fetchone()['id']
        payment = db.execute("""INSERT INTO bank_transactions(tenant_id,import_id,source_row,posted_date,description,debit_decimal,
                signed_amount_decimal,direction,currency,fingerprint,status)
            VALUES (%s,%s,1,%s,'Pay bill','100.00','-100.00','money_out','MYR',%s,'proposed') RETURNING id""",
                             (ids['tenant'], batch, DAY, hashlib.sha256(uuid4().bytes).hexdigest())).fetchone()['id']
        db.execute("INSERT INTO reconciliation_candidates(tenant_id,bank_transaction_id,record_id,proposed_by,status) VALUES (%s,%s,%s,'accountant','confirmed')",
                   (ids['tenant'], payment, ids['records']['bill']))
        db.commit()
    user = {'tenant_id': ids['tenant'], 'id': ids['admin'], 'name': 'Admin', 'role': 'local_admin'}
    with patch.multiple(bank, connect=connect, require_workspace_user=lambda *_args: user):
        detail = bank.get_bank_transaction(str(payment), None)
    assert detail['billPayableAccountId'] == str(ids['accounts']['2000'])


def _document_of(connect, record_id):
    with connect() as db:
        return db.execute("SELECT document_id FROM standardised_records WHERE id=%s", (record_id,)).fetchone()['document_id']


def _manual_journal(ids, amount='10.00'):
    return accounting.post_journal(accounting.JournalInput(
        periodId=str(ids['period']), idempotencyKey='manual:' + uuid4().hex, sourceType='manual', journalDate=DAY,
        description='Synthetic expense', lines=[
            accounting.JournalLineInput(accountId=str(ids['accounts']['5000']), debit=Decimal(amount)),
            accounting.JournalLineInput(accountId=str(ids['accounts']['1010']), credit=Decimal(amount))]), None)


def test_trial_balance_nets_a_reversed_journal_to_zero(ledger):
    patcher, ids = _as_admin(ledger)
    connect, _ = ledger
    with connect() as db:
        before = {r['code']: r['net_balance'] for r in db.execute(
            'SELECT code, net_balance FROM internal_trial_balance WHERE tenant_id=%s', (ids['tenant'],)).fetchall()}
    with patcher:
        accounting.reverse_journal(_manual_journal(ids, '37.00')['id'], None)
    with connect() as db:
        after = {r['code']: r['net_balance'] for r in db.execute(
            'SELECT code, net_balance FROM internal_trial_balance WHERE tenant_id=%s', (ids['tenant'],)).fetchall()}
    # Before the fix the reversed original dropped out and 1010 moved by +37.00 (5000 by -37.00).
    assert after.get('1010', 0) == before.get('1010', 0)
    assert after.get('5000', 0) == before.get('5000', 0)


def test_a_journal_in_a_closed_period_cannot_be_reversed(ledger):
    patcher, ids = _as_admin(ledger)
    connect, _ = ledger
    with patcher:
        journal = _manual_journal(ids)
        with connect() as db:
            db.execute("UPDATE accounting_periods SET status='closed' WHERE id=%s", (ids['period'],))
        try:
            with pytest.raises(HTTPException) as error:
                accounting.reverse_journal(journal['id'], None)
        finally:
            with connect() as db:
                db.execute("UPDATE accounting_periods SET status='open' WHERE id=%s", (ids['period'],))
    assert error.value.status_code == 409 and 'is closed' in error.value.detail
