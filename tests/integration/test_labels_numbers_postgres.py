"""Labels and numbers pass on the full schema in the disposable loopback TEST_DATABASE_URL.

- The document lists show the total the AI read until a record is saved, and the record's total after (`totalSource`).
- `readyToSubmit` on a list row is the same predicate as the `ready` lane, so the badge and the Today count agree.
- `manage.py repair-nonumber` renames stored NONUMBER names from the current rule: dry run changes nothing.
"""
import hashlib
import os
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import main  # noqa: F401 - initialise the application's route registry before route imports.
from services.admin import repair_nonumber
from services.admin.demo_company import SeedError
from services.routes.documents import routes as documents
from services.routes.home import routes as home

SCHEMA_FILE = Path(__file__).resolve().parents[2] / 'database' / 'PostgreSQL Database schema.sql'
TODAY = date(2026, 11, 25)
PLAIN_NAME = '2026-11-20_KENA_BILL_BN-1042.jpg'


def _hash():
    return hashlib.sha256(uuid4().bytes).hexdigest()


@pytest.fixture(scope='module')
def company():
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'labels_numbers_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute('CREATE DATABASE ' + name)
    target = parts._replace(path='/' + name).geturl()

    def connect():
        return psycopg.connect(target, row_factory=dict_row)

    try:
        with connect() as db:
            db.execute(SCHEMA_FILE.read_text(encoding='utf-8-sig'))
        yield connect, _seed(connect)
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


def _seed(connect):
    ids = {}
    with connect() as db:
        tenant = db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES ('Synthetic Trading','SYN-REG','syn@example.test') RETURNING id").fetchone()['id']
        other = db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES ('Synthetic Other','OTH-REG','oth@example.test') RETURNING id").fetchone()['id']
        admin = db.execute("INSERT INTO users(tenant_id,name,email,role,status) VALUES (%s,'Admin','admin@example.test','local_admin','active') RETURNING id", (tenant,)).fetchone()['id']
        ids.update(tenant=tenant, other=other, admin=admin)

        def document(owner_tenant, status, name='upload.pdf', kind='bill', category='accounts_payable'):
            return db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,
                    file_size_bytes,file_hash_sha256,status,uploaded_at)
                VALUES (%s,%s,%s,%s,%s,'synthetic/none.pdf','application/pdf',100,%s,%s,%s) RETURNING id""",
                              (owner_tenant, admin, name, kind, category, _hash(), status, TODAY)).fetchone()['id']

        def extraction(document_id, fields, current=True, run=1):
            eid = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
                VALUES (%s,%s,%s,'parser_only',%s) RETURNING id""", (document_id, run, current, Jsonb({}))).fetchone()['id']
            for key, (raw, current_value) in fields.items():
                db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,extraction_state,resolution,
                        raw_value,normalized_value,current_value) VALUES (%s,%s,%s,%s,'resolved','parser_preserved',%s,%s,%s)""",
                           (eid, document_id, key, key, raw, raw, current_value))

        def passed(document_id):
            db.execute("""INSERT INTO validation_results(document_id,tenant_id,content_revision,rule_id,document_type,status,severity,
                    prevents_approval,requires_review,message) VALUES (%s,%s,1,'TOTAL','bill','passed','info',false,false,'synthetic')""",
                       (document_id, tenant))

        def open_exception(document_id):
            db.execute("INSERT INTO exceptions(document_id,tenant_id,type,severity,status,message) VALUES (%s,%s,'VALIDATION:TOTAL','high','open','synthetic')",
                       (document_id, tenant))

        # Totals.
        ids['read_total'] = document(tenant, 'extracted'); extraction(ids['read_total'], {'total': ('RM 1,250.50', None)})
        ids['read_amount_due'] = document(tenant, 'extracted'); extraction(ids['read_amount_due'], {'amount_due': ('300.00', None)})
        ids['total_first'] = document(tenant, 'extracted'); extraction(ids['total_first'], {'amount_due': ('40.00', None), 'total': ('100.00', None)})
        ids['corrected_total'] = document(tenant, 'corrected'); extraction(ids['corrected_total'], {'total': ('80.00', '88.00')})
        ids['no_total'] = document(tenant, 'extracted'); extraction(ids['no_total'], {'supplier': ('Synthetic Supplier', None)})
        ids['junk_total'] = document(tenant, 'extracted'); extraction(ids['junk_total'], {'total': ('see attached', None)})
        ids['old_reading'] = document(tenant, 'extracted')
        extraction(ids['old_reading'], {'total': ('999.00', None)}, current=False, run=1)
        extraction(ids['old_reading'], {'supplier': ('Synthetic Supplier', None)}, current=True, run=2)
        approved = document(tenant, 'completed'); extraction(approved, {'total': ('777.00', None)})
        submission = db.execute("INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision,content_revision) VALUES (%s,%s,%s,'submitted',1) RETURNING id",
                                (approved, tenant, admin)).fetchone()['id']
        db.execute("INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision) VALUES (%s,%s,%s,%s,'approved',1)",
                   (submission, approved, tenant, admin))
        db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,supplier_name,buyer_name,invoice_date,currency,total_amount,snapshot)
            VALUES (%s,%s,'accounts_payable','Synthetic Supplier','Synthetic Customer',%s,'MYR','500.00',%s)""", (approved, tenant, TODAY, Jsonb({})))
        ids['approved'] = approved

        # Ready to submit: passed checks, no open exception, still waiting for the accountant.
        ids['ready'] = document(tenant, 'extracted'); passed(ids['ready'])
        ids['ready_corrected'] = document(tenant, 'corrected'); passed(ids['ready_corrected'])
        ids['unchecked'] = document(tenant, 'extracted')
        ids['open_exception'] = document(tenant, 'extracted'); passed(ids['open_exception']); open_exception(ids['open_exception'])
        ids['in_review'] = document(tenant, 'in_review'); passed(ids['in_review'])

        # NONUMBER names: two documents with one number clash on the new name, one has no number, one is in another company.
        fields = {'supplier': ('Kenanga Trading Sdn Bhd', None), 'date': ('2026-11-20', None), 'bill_number': ('BN-1042', None)}
        ids['nonumber_read'] = document(tenant, 'extracted', '2026-11-20_KEN_BILL_NONUMBER.jpg')
        extraction(ids['nonumber_read'], fields)
        ids['nonumber_clash'] = document(tenant, 'extracted', '2026-11-20_KEN_BILL_NONUMBER.jpg')
        extraction(ids['nonumber_clash'], fields)
        ids['nonumber_unread'] = document(tenant, 'extracted', '2026-11-20_KEN_BILL_NONUMBER.pdf')
        extraction(ids['nonumber_unread'], {'supplier': ('Kenanga Trading Sdn Bhd', None), 'date': ('2026-11-20', None)})
        ids['already_named'] = document(tenant, 'extracted', '2026-11-20_KENA_BILL_BN-1042.png')
        ids['other_company'] = document(other, 'extracted', '2026-11-20_KEN_BILL_NONUMBER.jpg')
        extraction(ids['other_company'], fields)
    return ids


def _as_admin(ids):
    return {'id': ids['admin'], 'tenant_id': ids['tenant'], 'role': 'local_admin', 'name': 'Admin'}


def _listing(connect, ids, **query):
    with patch.object(documents, 'require_workspace_user', return_value=_as_admin(ids)), patch.object(documents, 'connect', connect):
        return {row['id']: row for row in documents.list_documents(None, **query)}


def _total(rows, ids, key):
    row = rows[str(ids[key])]
    return row['totalAmount'], row['totalSource']


def test_total_is_what_the_ai_read_until_a_record_is_saved(company):
    connect, ids = company
    rows = _listing(connect, ids)
    assert _total(rows, ids, 'read_total') == (1250.5, 'read')
    assert _total(rows, ids, 'read_amount_due') == (300.0, 'read')
    # total wins over amount_due; a reviewer's correction wins over the printed value.
    assert _total(rows, ids, 'total_first') == (100.0, 'read')
    assert _total(rows, ids, 'corrected_total') == (88.0, 'read')


def test_total_is_left_empty_rather_than_guessed(company):
    connect, ids = company
    rows = _listing(connect, ids)
    assert _total(rows, ids, 'no_total') == (None, None)
    assert _total(rows, ids, 'junk_total') == (None, None)
    # An earlier reading's total is not the current reading's.
    assert _total(rows, ids, 'old_reading') == (None, None)


def test_the_saved_record_total_wins_once_approved(company):
    connect, ids = company
    assert _total(_listing(connect, ids), ids, 'approved') == (500.0, 'record')


def test_ready_to_submit_badge_agrees_with_the_ready_lane(company):
    connect, ids = company
    rows = _listing(connect, ids)
    flagged = {key for key, value in rows.items() if value['readyToSubmit']}
    assert flagged == {str(ids['ready']), str(ids['ready_corrected'])}
    assert flagged == set(_listing(connect, ids, lane='ready'))
    assert not rows[str(ids['unchecked'])]['readyToSubmit']
    assert not rows[str(ids['open_exception'])]['readyToSubmit']
    assert not rows[str(ids['in_review'])]['readyToSubmit']
    assert not rows[str(ids['approved'])]['readyToSubmit']
    # Today counts the same documents (the tile is the same predicate).
    with patch.object(home, 'require_workspace_user', return_value=_as_admin(ids)), patch.object(home, 'connect', connect):
        assert home.home_summary(None, asOf=TODAY)['tiles']['readyToApprove']['count'] == len(flagged)


def _names(connect):
    with connect() as db:
        return {r['id']: r['original_filename'] for r in db.execute("SELECT id,original_filename FROM documents").fetchall()}


def test_repair_dry_run_prints_old_and_new_and_writes_nothing(company, capsys):
    connect, ids = company
    before = _names(connect)
    with patch.object(repair_nonumber, 'require_target', return_value=connect):
        assert repair_nonumber.run(Namespace(apply=False, tenant=None, allow_remote=False)) == 0
    out = capsys.readouterr().out
    assert f'2026-11-20_KEN_BILL_NONUMBER.jpg → {PLAIN_NAME}' in out
    assert 'left as it is: no document number was read' in out
    assert 'Would rename 3 document(s); 1 left as they are.' in out
    assert 'Dry run: nothing written' in out
    assert before == _names(connect)


def test_repair_apply_renames_from_the_current_rule(company):
    connect, ids = company
    with connect() as db:
        repair_nonumber.repair(db, ids['tenant'], apply=True)
    names = _names(connect)
    pair = sorted([names[ids['nonumber_read']], names[ids['nonumber_clash']]])
    # Two documents with one number: one takes the name, the other keeps it unique with its own document code.
    assert PLAIN_NAME in pair
    other = next(name for name in pair if name != PLAIN_NAME)
    assert other.startswith('2026-11-20_KENA_BILL_BN-1042__') and other.endswith('.jpg')
    assert all('NONUMBER' not in name for name in pair)
    assert names[ids['nonumber_unread']] == '2026-11-20_KEN_BILL_NONUMBER.pdf'
    assert names[ids['already_named']] == '2026-11-20_KENA_BILL_BN-1042.png'
    # Another company's document is never touched, and a second run finds nothing to rename.
    assert names[ids['other_company']] == '2026-11-20_KEN_BILL_NONUMBER.jpg'
    with connect() as db:
        again = repair_nonumber.repair(db, ids['tenant'], apply=True)
    assert [row for row in again if row['new']] == []


@pytest.mark.parametrize('environ, allow, ok', [
    ({'DATABASE_URL': 'postgresql://u:p@127.0.0.1:5432/db'}, False, True),
    ({'DATABASE_URL': 'postgresql://u:p@db.example.com/db'}, False, False),
    ({'DATABASE_URL': 'postgresql://u:p@db.example.com/db'}, True, False),
    ({'DATABASE_URL': 'postgresql://u:p@db.example.com/db', 'REPAIR_NONUMBER_REMOTE_HOST': 'db.example.com'}, False, False),
    ({'DATABASE_URL': 'postgresql://u:p@db.example.com/db', 'REPAIR_NONUMBER_REMOTE_HOST': 'other.example.com'}, True, False),
    ({'DATABASE_URL': 'postgresql://u:p@db.example.com/db', 'REPAIR_NONUMBER_REMOTE_HOST': 'DB.example.com'}, True, True),
    ({}, True, False),
])
def test_remote_database_needs_the_flag_and_the_matching_host(environ, allow, ok):
    if ok:
        assert callable(repair_nonumber.require_target(allow, environ))
    else:
        with pytest.raises(SeedError):
            repair_nonumber.require_target(allow, environ)
