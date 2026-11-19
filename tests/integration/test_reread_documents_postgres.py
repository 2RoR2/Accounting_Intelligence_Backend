"""reread-documents on the full schema in the disposable loopback TEST_DATABASE_URL.

Two companies. In the first: a "needs fixing" document with an earlier finished attempt (re-read), one with a
corrected field, one with an approved review, one completed and one with an open job (all skipped). The second
company has a "needs fixing" document that must stay exactly as it was.
"""
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from services.admin import reread
from services.queue import attempts

SCHEMA_FILE = Path(__file__).resolve().parents[2] / 'database' / 'PostgreSQL Database schema.sql'


@pytest.fixture()
def database(monkeypatch):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'reread_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute('CREATE DATABASE ' + name)
    target = parts._replace(path='/' + name).geturl()

    def connect():
        return psycopg.connect(target, row_factory=dict_row)

    # The command's own guard must accept this database: local development, loopback, no bucket.
    for key in ('S3_BUCKET', 'PGHOST', 'PGHOSTADDR', 'PGSERVICE', 'QUEUE_REPLAY_DOCUMENT_IDS',
                'QUEUE_OWNER_ID', 'QUEUE_MIN_UPLOADED_AT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('DATABASE_URL', target)
    monkeypatch.setenv('QUEUE_TENANT_ID', '')  # the command sets it; monkeypatch restores it afterwards
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
        def tenant(name):
            return db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,%s) RETURNING id",
                              (name, name + '-REG', 'admin@' + name.lower().replace(' ', '') + '.test')).fetchone()['id']

        def user(tenant_id, role, email):
            return db.execute("INSERT INTO users(tenant_id,name,email,role,status) VALUES (%s,%s,%s,%s,'active') RETURNING id",
                              (tenant_id, email, email, role)).fetchone()['id']

        def document(tenant_id, owner, status, label):
            return db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,
                    mime_type,file_size_bytes,file_hash_sha256,status)
                VALUES (%s,%s,%s,'bill','accounts_payable','synthetic/none.pdf','application/pdf',100,%s,%s) RETURNING id""",
                              (tenant_id, owner, label + '.pdf', hashlib.sha256(label.encode()).hexdigest(),
                               status)).fetchone()['id']

        def finished_job(tenant_id, document_id):
            return db.execute("""INSERT INTO pending_jobs(tenant_id,document_id,status,attempt_count,finished_at)
                VALUES (%s,%s,'succeeded',1,now()) RETURNING id""", (tenant_id, document_id)).fetchone()['id']

        a, b = tenant('Synthetic Reread A'), tenant('Synthetic Reread B')
        accountant, admin = user(a, 'accountant', 'accountant@a.test'), user(a, 'local_admin', 'admin@a.test')
        other = user(b, 'accountant', 'accountant@b.test')
        ids.update(tenant=a, tenant_b=b)

        ids['fixing'] = document(a, accountant, 'exception', 'needs-fixing')
        ids['fixing_job'] = finished_job(a, ids['fixing'])

        ids['corrected'] = document(a, accountant, 'extracted', 'corrected-field')
        extraction = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
            VALUES (%s,1,true,'parser_only','{}') RETURNING id""", (ids['corrected'],)).fetchone()['id']
        db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,extraction_state,
                resolution,current_value,corrected_by,corrected_at)
            VALUES (%s,%s,'total_amount','Total','resolved','parser_preserved','100.00',%s,now())""",
                   (extraction, ids['corrected'], accountant))

        ids['approved'] = document(a, accountant, 'extracted', 'approved-review')
        submission = db.execute("""INSERT INTO review_submissions(document_id,tenant_id,submitted_by,decision)
            VALUES (%s,%s,%s,'submitted') RETURNING id""", (ids['approved'], a, accountant)).fetchone()['id']
        db.execute("""INSERT INTO review_decisions(submission_id,document_id,tenant_id,reviewed_by,decision,approved_content_revision)
            VALUES (%s,%s,%s,%s,'approved',1)""", (submission, ids['approved'], a, admin))

        ids['completed'] = document(a, accountant, 'completed', 'completed')
        ids['busy'] = document(a, accountant, 'exception', 'open-job')
        db.execute("INSERT INTO pending_jobs(tenant_id,document_id,status) VALUES (%s,%s,'pending')", (a, ids['busy']))

        ids['other'] = document(b, other, 'exception', 'other-company')
        ids['other_job'] = finished_job(b, ids['other'])
    return ids


def _state(connect):
    with connect() as db:
        return {
            'documents': {str(r['id']): r['status'] for r in db.execute('SELECT id,status FROM documents').fetchall()},
            'jobs': sorted((str(r['document_id']), r['status']) for r in db.execute(
                'SELECT document_id,status FROM pending_jobs').fetchall()),
            'audits': db.execute('SELECT count(*) n FROM audit_logs').fetchone()['n'],
        }


def test_dry_run_changes_nothing(database, capsys):
    connect, ids = database
    before = _state(connect)
    report = reread.reread(company='Synthetic Reread A', dry_run=True, connect=connect)
    assert _state(connect) == before
    assert report['selected'] == 1 and report['queued'] == 0
    assert 'needs-fixing.pdf  would re-read' in capsys.readouterr().out


def test_rereads_only_the_unreviewed_document_of_that_company(database, capsys):
    connect, ids = database
    before = _state(connect)
    report = reread.reread(company='Synthetic Reread A', wait=False, connect=connect)

    assert report['documents'] == 5 and report['queued'] == 1
    assert report['skipped'] == {'already has an open job': 1, 'has reviewed corrections': 1,
                                 'has review submissions or decisions': 1, 'past review (completed)': 1}
    out = capsys.readouterr().out
    for line in ('corrected-field.pdf  skip: has reviewed corrections', 'approved-review.pdf  skip: has review',
                 'completed.pdf  skip: past review (completed)', 'open-job.pdf  skip: already has an open job',
                 'needs-fixing.pdf  queued (job '):
        assert line in out

    after = _state(connect)
    fixing = str(ids['fixing'])
    assert after['documents'][fixing] == 'queued'
    assert {k: v for k, v in after['documents'].items() if k != fixing} == \
        {k: v for k, v in before['documents'].items() if k != fixing}
    with connect() as db:
        jobs = db.execute('SELECT id,status,attempt_count FROM pending_jobs WHERE document_id=%s ORDER BY created_at',
                          (ids['fixing'],)).fetchall()
        audit = db.execute("SELECT action,details FROM audit_logs WHERE resource=%s", (fixing,)).fetchall()
        other_jobs = db.execute('SELECT id,status FROM pending_jobs WHERE document_id=%s', (ids['other'],)).fetchall()
        assert db.execute('SELECT status FROM documents WHERE id=%s', (ids['other'],)).fetchone()['status'] == 'exception'
    # The earlier attempt is kept; the new one is a fresh, dispatchable outbox row.
    assert [(job['status'], job['attempt_count']) for job in jobs] == [('succeeded', 1), ('pending', 0)]
    assert jobs[0]['id'] == ids['fixing_job']
    assert audit == [{'action': 'extraction.reread_scheduled', 'details': str(jobs[1]['id'])}]
    assert [(job['id'], job['status']) for job in other_jobs] == [(ids['other_job'], 'succeeded')]
    with connect() as db:
        delivery = attempts.reserve_dispatch(db, fixing, str(ids['tenant']), job_id=str(jobs[1]['id']))
    assert delivery.job_id == str(jobs[1]['id'])

    # Running it again finds the job already open and queues nothing more.
    again = reread.reread(company='Synthetic Reread A', wait=False, connect=connect)
    assert again['queued'] == 0 and again['skipped']['already has an open job'] == 2


def test_the_queue_refuses_corrected_or_approved_documents_directly(database):
    connect, ids = database
    for key, code in (('corrected', 'AMENDMENT_REQUIRED'), ('approved', 'AMENDMENT_REQUIRED'),
                      ('completed', 'AMENDMENT_REQUIRED'), ('busy', 'JOB_ALREADY_OPEN')):
        with connect() as db, pytest.raises(attempts.AttemptRejected) as rejected:
            attempts.schedule_reread(db, str(ids[key]), str(ids['tenant']))
        assert rejected.value.code == code, key
    # A submitted-but-not-yet-decided document is refused too, although ensure_unreviewed alone would allow it.
    with connect() as db:
        db.execute('DELETE FROM review_decisions WHERE document_id=%s', (ids['approved'],))
    with connect() as db, pytest.raises(attempts.AttemptRejected) as rejected:
        attempts.schedule_reread(db, str(ids['approved']), str(ids['tenant']))
    assert rejected.value.code == 'REVIEW_HISTORY'
    # Another company's document cannot be reached through this company.
    with connect() as db, pytest.raises(attempts.AttemptRejected) as rejected:
        attempts.schedule_reread(db, str(ids['other']), str(ids['tenant']))
    assert rejected.value.code == 'ATTEMPT_DOCUMENT_MISMATCH'
