"""Today page summary (plan v2 B3) on the full schema in the disposable loopback TEST_DATABASE_URL.

Two companies; documents owned by, assigned to, and hidden from the accountant; bills with part payments, a printed
amount due, another currency, no due date and a full payment; a posted and a draft journal. Each tile's count must
match the length of the list it links to.
"""
import hashlib
import os
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import main  # noqa: F401 - initialise the application's route registry before route imports.
from services.routes.documents import routes as documents
from services.routes.home import routes as home

SCHEMA_FILE = Path(__file__).resolve().parents[2] / 'database' / 'PostgreSQL Database schema.sql'
TODAY = date(2026, 11, 16)


def _hash(*parts):
    return hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()


@pytest.fixture(scope='module')
def company():
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    # A fresh database: the consolidated schema checks constraints by name, so it cannot share one with another copy.
    name = 'home_summary_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute('CREATE DATABASE ' + name)
    target = parts._replace(path='/' + name).geturl()

    def connect():
        return psycopg.connect(target, row_factory=dict_row)

    try:
        with connect() as db:
            db.execute(SCHEMA_FILE.read_text(encoding='utf-8-sig'))
        ids = _seed(connect)
        yield connect, ids
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


def _seed(connect):
    ids = {}
    with connect() as db:
        def tenant(name):
            return db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,%s) RETURNING id",
                              (name, name + '-REG', name.lower().replace(' ', '.') + '@example.test')).fetchone()['id']

        def user(tenant_id, role, name):
            return db.execute("INSERT INTO users(tenant_id,name,email,role,status) VALUES (%s,%s,%s,%s,'active') RETURNING id",
                              (tenant_id, name, name.lower().replace(' ', '.') + '@example.test', role)).fetchone()['id']

        a, b = tenant('Synthetic Trading A'), tenant('Synthetic Trading B')
        mine, other, admin, admin_b = (user(a, 'accountant', 'Accountant One'), user(a, 'accountant', 'Accountant Two'),
                                       user(a, 'local_admin', 'Admin A'), user(b, 'local_admin', 'Admin B'))
        ids.update(tenant=a, tenant_b=b, mine=mine, other=other, admin=admin, admin_b=admin_b)

        def document(tenant_id, owner, status, kind='bill', category='accounts_payable', assigned=None):
            return db.execute("""INSERT INTO documents(tenant_id,owner_id,assigned_to,original_filename,kind,category,storage_path,
                    mime_type,file_size_bytes,file_hash_sha256,status,uploaded_at)
                VALUES (%s,%s,%s,%s,%s,%s,'synthetic/none.pdf','application/pdf',100,%s,%s,%s) RETURNING id""",
                              (tenant_id, owner, assigned, f'{kind}-{uuid4().hex[:6]}.pdf', kind, category, _hash(uuid4()), status,
                               TODAY)).fetchone()['id']

        def checks(document_id, tenant_id, status='passed'):
            db.execute("""INSERT INTO validation_results(document_id,tenant_id,content_revision,rule_id,document_type,status,severity,
                    prevents_approval,requires_review,message) VALUES (%s,%s,1,'TOTAL','bill',%s,%s,%s,%s,'synthetic')""",
                       (document_id, tenant_id, status, 'info' if status == 'passed' else 'high', status != 'passed', status != 'passed'))

        def exception(document_id, tenant_id, code, status='open'):
            db.execute("INSERT INTO exceptions(document_id,tenant_id,type,severity,status,message,resolved_at) VALUES (%s,%s,%s,'high',%s,'synthetic',%s)",
                       (document_id, tenant_id, 'VALIDATION:' + code, status, TODAY if status == 'resolved' else None))

        def record(tenant_id, owner, kind, category, total, due, currency='MYR', amount_due=None):
            document_id = document(tenant_id, owner, 'completed', kind, category)
            submission = db.execute("INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision) VALUES (%s,%s,%s,'submitted',1) RETURNING id",
                                    (document_id, tenant_id, owner)).fetchone()['id']
            reviewer = admin if tenant_id == a else admin_b
            db.execute("INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision) VALUES (%s,%s,%s,%s,'approved',1)",
                       (submission, document_id, tenant_id, reviewer))
            return db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,supplier_name,buyer_name,invoice_date,due_date,
                    currency,total_amount,amount_due,snapshot) VALUES (%s,%s,%s,'Synthetic Supplier','Synthetic Customer',%s,%s,%s,%s,%s,%s)
                RETURNING id, document_id""", (document_id, tenant_id, category, TODAY, due, currency, total, amount_due,
                                               Jsonb({}))).fetchone()

        def pay(tenant_id, record_row, amount):
            settlement = db.execute("""INSERT INTO settlements(tenant_id,settlement_ref,settlement_date,direction,amount,currency,source_type,created_by)
                VALUES (%s,%s,%s,'money_out',%s,'MYR','manual_record',%s) RETURNING id""",
                                    (tenant_id, 'SET-' + uuid4().hex[:8], TODAY, amount, admin)).fetchone()['id']
            return db.execute("INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,created_by) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                              (tenant_id, settlement, record_row['id'], amount, admin)).fetchone()

        def undo(allocation):
            # As reverse_allocation does: a new 'reversed' row; the original stays 'active' (append-only).
            db.execute("""INSERT INTO settlement_allocations(tenant_id,settlement_id,record_id,amount,status,created_by,reversed_at,reversal_reason,reverses_allocation_id)
                VALUES (%s,%s,%s,%s,'reversed',%s,now(),'Allocation undone',%s)""",
                       (allocation['tenant_id'], allocation['settlement_id'], allocation['record_id'], allocation['amount'], admin, allocation['id']))

        # Documents waiting in tenant A.
        ready = document(a, mine, 'extracted'); checks(ready, a)
        fixing = document(a, mine, 'exception'); checks(fixing, a, 'failed'); exception(fixing, a, 'EX-008')
        assigned = document(a, other, 'corrected', assigned=mine); exception(assigned, a, 'TOTAL'); exception(assigned, a, 'EX-008')
        hidden = document(a, other, 'extracted'); exception(hidden, a, 'BR-001')
        resolved = document(a, mine, 'extracted'); checks(resolved, a); exception(resolved, a, 'TOTAL', 'resolved')
        document(a, mine, 'in_review')

        # Payables and receivables in tenant A.
        soon = record(a, mine, 'bill', 'accounts_payable', '500.00', TODAY + timedelta(days=3)); pay(a, soon, '200.00')
        record(a, mine, 'invoice', 'accounts_payable', '1000.50', TODAY - timedelta(days=5), amount_due='800.25')
        record(a, mine, 'bill', 'accounts_payable', '50.00', TODAY + timedelta(days=2), currency='USD')
        paid = record(a, mine, 'bill', 'accounts_payable', '70.00', TODAY - timedelta(days=1)); pay(a, paid, '70.00')
        record(a, mine, 'receipt', 'accounts_payable', '30.00', TODAY - timedelta(days=10))
        record(a, mine, 'bill', 'accounts_payable', '40.00', None)
        record(a, mine, 'bill', 'accounts_payable', '60.00', TODAY + timedelta(days=8))
        undone = record(a, mine, 'bill', 'accounts_payable', '150.00', TODAY - timedelta(days=4)); undo(pay(a, undone, '150.00'))
        record(a, mine, 'invoice', 'accounts_receivable', '1200.00', TODAY - timedelta(days=2))
        posted = record(a, mine, 'invoice', 'accounts_receivable', '300.00', TODAY + timedelta(days=20))
        credit_note = record(a, mine, 'credit_note', 'accounts_payable', '25.00', None)

        # Tenant B: must never reach tenant A's figures.
        b_doc = document(b, admin_b, 'exception'); exception(b_doc, b, 'EX-008')
        record(b, admin_b, 'bill', 'accounts_payable', '999.00', TODAY - timedelta(days=3))

        # Ledger: a posted journal for `posted`, a posted adjustment for the credit note, and one draft.
        period = db.execute("INSERT INTO accounting_periods(tenant_id,period_code,starts_on,ends_on) VALUES (%s,'FY26-11',%s,%s) RETURNING id",
                            (a, date(2026, 11, 1), date(2026, 11, 30))).fetchone()['id']
        receivable, revenue = (db.execute("INSERT INTO ledger_accounts(tenant_id,code,name,account_type) VALUES (%s,%s,%s,%s) RETURNING id",
                                          (a, code, name, kind)).fetchone()['id']
                               for code, name, kind in (('1200', 'Trade receivables', 'asset'), ('4000', 'Sales', 'income')))
        for source_type, source_id, final in (('document', posted['document_id'], 'posted'),
                                              ('adjustment', credit_note['document_id'], 'posted'),
                                              ('manual', None, 'draft')):
            journal = db.execute("""INSERT INTO journal_headers(tenant_id,period_id,idempotency_key,source_type,source_id,status,journal_date,description)
                VALUES (%s,%s,%s,%s,%s,'draft',%s,'synthetic') RETURNING id""",
                                 (a, period, source_type + uuid4().hex, source_type, source_id, TODAY)).fetchone()['id']
            for n, (account, debit, credit) in enumerate(((receivable, 300, 0), (revenue, 0, 300)), 1):
                db.execute("INSERT INTO journal_lines(tenant_id,journal_id,account_id,line_number,debit,credit) VALUES (%s,%s,%s,%s,%s,%s)",
                           (a, journal, account, n, debit, credit))
            if final == 'posted':
                db.execute("UPDATE journal_headers SET status='posted',posted_at=now(),posted_by=%s WHERE id=%s", (admin, journal))

        # Bank: confirmed rows (two still to match, one matched), a preview row, and tenant B's row.
        def bank_import(tenant_id, by, status):
            return db.execute("""INSERT INTO bank_imports(tenant_id,source_bank_account_ref,source_hash,input_format,mapping_version,imported_by,status)
                VALUES (%s,'SYN-001',%s,'csv','v1',%s,%s) RETURNING id""", (tenant_id, _hash(uuid4()), by, status)).fetchone()['id']

        def bank_row(tenant_id, import_id, row, amount, status):
            db.execute("""INSERT INTO bank_transactions(tenant_id,import_id,source_row,posted_date,description,debit_decimal,credit_decimal,
                    signed_amount_decimal,direction,currency,fingerprint,status) VALUES (%s,%s,%s,%s,'synthetic',%s,%s,%s,%s,'MYR',%s,%s)""",
                       (tenant_id, import_id, row, TODAY, -amount if amount < 0 else None, amount if amount > 0 else None, amount,
                        'money_in' if amount > 0 else 'money_out', _hash(tenant_id, import_id, row), status))

        confirmed = bank_import(a, admin, 'confirmed')
        bank_row(a, confirmed, 1, -120, 'imported'); bank_row(a, confirmed, 2, 300, 'proposed'); bank_row(a, confirmed, 3, -50, 'matched')
        # A bank fee classified to an account: still 'imported', but explained, so not "to match".
        bank_row(a, confirmed, 4, -15, 'imported')
        fees = db.execute("INSERT INTO coa_accounts(tenant_id,code,name,account_class) VALUES (%s,'6100','Bank charges','expense') RETURNING id",
                          (a,)).fetchone()['id']
        fee_row = db.execute("SELECT id FROM bank_transactions WHERE import_id=%s AND source_row=4", (confirmed,)).fetchone()['id']
        db.execute("""INSERT INTO bank_coding_decisions(tenant_id,bank_transaction_id,account_id,user_id,decision,reason)
            VALUES (%s,%s,%s,%s,'manual','Monthly bank fee')""", (a, fee_row, fees, admin))
        bank_row(a, bank_import(a, admin, 'previewed'), 1, -10, 'imported')
        bank_row(b, bank_import(b, admin_b, 'confirmed'), 1, -10, 'imported')
    return ids


def _as(user_id, tenant_id, role):
    return {'id': user_id, 'tenant_id': tenant_id, 'role': role, 'name': role}


def _summary(connect, user, as_of=TODAY):
    with patch.object(home, 'require_workspace_user', return_value=user), patch.object(home, 'connect', connect):
        return home.home_summary(None, asOf=as_of)


def _documents(connect, user, lane):
    with patch.object(documents, 'require_workspace_user', return_value=user), patch.object(documents, 'connect', connect):
        return documents.list_documents(None, lane=lane)


def _records(connect, user, **query):
    """Open the records list with the link's own query, parsed as FastAPI would (asOf as a date)."""
    if 'asOf' in query:
        query['asOf'] = date.fromisoformat(query['asOf'])
    with patch.object(documents, 'require_workspace_user', return_value=user), patch.object(documents, 'connect', connect):
        return documents.list_records(None, **query)


def test_accountant_counts_only_owned_or_assigned_documents(company):
    connect, ids = company
    tiles = _summary(connect, _as(ids['mine'], ids['tenant'], 'accountant'))['tiles']
    # Visible: ready, fixing, assigned (to me), resolved, submitted. Hidden: the other accountant's own document.
    assert tiles['toReview']['count'] == 4
    assert tiles['readyToApprove']['count'] == 2           # ready + resolved (its exception is resolved)
    assert tiles['needsFixing']['count'] == 2              # fixing + assigned; hidden's BR-001 is not mine
    assert tiles['needsFixing']['topReasons'] == [{'code': 'EX-008', 'documents': 2}, {'code': 'TOTAL', 'documents': 1}]
    assert tiles['waitingApproval']['count'] == 1


def test_local_admin_sees_the_whole_company_and_never_another_company(company):
    connect, ids = company
    tiles = _summary(connect, _as(ids['admin'], ids['tenant'], 'local_admin'))['tiles']
    assert tiles['toReview']['count'] == 5
    assert tiles['needsFixing']['count'] == 3
    assert {r['code'] for r in tiles['needsFixing']['topReasons']} == {'EX-008', 'TOTAL', 'BR-001'}
    # Imported + proposed rows; the matched row, the classified fee and the preview row are not to match.
    assert tiles['bankToMatch'] == {'available': True, 'scope': 'company', 'count': 2, 'moneyIn': 1, 'moneyOut': 1,
                                    'link': {'page': 'reconciliation', 'query': 'status=unmatched'}}
    other = _summary(connect, _as(ids['admin_b'], ids['tenant_b'], 'local_admin'))['tiles']
    assert (other['toReview']['count'], other['needsFixing']['count'], other['bankToMatch']['count']) == (1, 1, 1)
    assert (other['billsOverdue']['count'], other['billsOverdue']['amount']) == (1, '999.00')


def test_bills_and_invoices_use_the_unpaid_amount_as_decimal_strings(company):
    connect, ids = company
    tiles = _summary(connect, _as(ids['mine'], ids['tenant'], 'accountant'))['tiles']
    soon, overdue, receivable = tiles['billsDueSoon'], tiles['billsOverdue'], tiles['invoicesOverdue']
    # 500 less a 200 part payment; the USD bill is counted but not added to the RM total; day 8 is outside the window.
    assert (soon['count'], soon['amount'], soon['otherCurrencyCount'], soon['noDueDate']) == (2, '300.00', 1, 1)
    # The printed amount due (800.25) wins over the total; the fully paid bill and the receipt are not open items;
    # the 150.00 bill whose payment was undone is unpaid again.
    assert (overdue['count'], overdue['amount'], overdue['otherCurrencyCount']) == (2, '950.25', 0)
    assert (receivable['count'], receivable['amount']) == (1, '1200.00')
    # 11 approved records; the invoice (document journal) and the credit note (adjustment journal) are posted.
    assert tiles['journalsToPost'] == {'available': True, 'scope': 'company', 'count': 9, 'draftJournals': 1,
                                       'link': {'page': 'records', 'query': 'posting=to-post'}}


def test_each_tile_opens_a_list_of_the_same_length(company):
    connect, ids = company
    for user in (_as(ids['mine'], ids['tenant'], 'accountant'), _as(ids['admin'], ids['tenant'], 'local_admin')):
        # Also a summary dated 10 days later: the due lists it links to must be dated the same way.
        for as_of in (TODAY, TODAY + timedelta(days=10)):
            tiles = _summary(connect, user, as_of)['tiles']
            # Needs fixing opens the documents list, which shares its visibility rule (Exceptions does not).
            assert tiles['needsFixing']['link'] == {'page': 'documents', 'query': 'lane=needs-fixing'}
            for key, lane in (('toReview', 'to-review'), ('readyToApprove', 'ready'), ('needsFixing', 'needs-fixing'),
                              ('waitingApproval', 'waiting-approval')):
                assert len(_documents(connect, user, lane)) == tiles[key]['count'], key
            for key in ('billsDueSoon', 'billsOverdue', 'invoicesOverdue', 'journalsToPost'):
                link = tiles[key]['link']
                assert link['page'] == 'records'
                query = dict(part.split('=') for part in link['query'].split('&'))
                if key != 'journalsToPost':
                    assert query['asOf'] == as_of.isoformat(), key
                assert len(_records(connect, user, **query)) == tiles[key]['count'], (key, as_of)


def test_month_end_progress_and_reconciliation_null_until_a_period_exists(company):
    connect, ids = company
    admin = _as(ids['admin'], ids['tenant'], 'local_admin')
    month = _summary(connect, admin)['monthEnd']
    assert month['period'] == {'code': 'FY26-11', 'startsOn': '2026-11-01', 'endsOn': '2026-11-30', 'status': 'open',
                               'source': 'accounting_period'}
    steps = {s['key']: s for s in month['steps']}
    # 6 waiting documents + 11 approved ones uploaded today; submitted + approved count as reviewed.
    assert (steps['reviewed']['done'], steps['reviewed']['total']) == (12, 17)
    # Four confirmed lines: one matched and one classified fee are explained.
    assert (steps['matched']['done'], steps['matched']['total']) == (2, 4)
    # Invoice posted as a document journal, credit note as an adjustment journal.
    assert (steps['posted']['done'], steps['posted']['total']) == (2, 11)
    assert steps['reconciled']['available'] is False and steps['reconciled']['reason']
    with connect() as db:
        db.execute("""INSERT INTO bank_reconciliation_periods(tenant_id,bank_account_ref,starts_on,ends_on,opening_balance,closing_balance,book_balance)
            VALUES (%s,'SYN-001',%s,%s,0,0,0)""", (ids['tenant'], date(2026, 11, 1), date(2026, 11, 30)))
    steps = {s['key']: s for s in _summary(connect, admin)['monthEnd']['steps']}
    assert (steps['reconciled']['available'], steps['reconciled']['done'], steps['reconciled']['total']) == (True, 0, 1)


def test_summary_is_fast_on_the_fixture(company):
    connect, ids = company
    user = _as(ids['admin'], ids['tenant'], 'local_admin')
    _summary(connect, user)
    started = time.perf_counter()
    _summary(connect, user)
    assert time.perf_counter() - started < 1.0  # Generous bound for CI; the target is ~200 ms on the demo data.
