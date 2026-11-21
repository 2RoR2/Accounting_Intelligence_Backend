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


def test_only_readings_finished_after_the_reread_are_timed(database):
    connect, ids = database
    with connect() as db:
        db.execute("""UPDATE documents SET processing_seconds=0.3, processing_completed_at=now() - interval '1 day'
            WHERE id IN (%s,%s)""", (ids['fixing'], ids['busy']))
        since = db.execute('SELECT clock_timestamp() AS instant').fetchone()['instant']
        # The re-read of one finished; the other failed and kept its old reading time.
        db.execute("""UPDATE documents SET status='extracted', processing_seconds=12.5,
            processing_completed_at=clock_timestamp() WHERE id=%s""", (ids['fixing'],))
        db.execute("UPDATE documents SET status='failed' WHERE id=%s", (ids['busy'],))
    rows = reread.outcome(connect, [str(ids['fixing']), str(ids['busy'])], since)
    assert sorted((row['status'], row['processing_seconds']) for row in rows) == \
        [('extracted', 12.5), ('failed', None)]
    result = reread.summarize(rows)
    assert result['timed'] == 1 and result['average_seconds'] == 12.5


def test_a_running_api_cannot_take_held_jobs_but_the_command_can(database, monkeypatch):
    connect, ids = database
    from services.queue import durable, inline
    monkeypatch.setenv('QUEUE_BACKEND', 'inline')
    monkeypatch.setattr(inline, 'capacity', lambda: 2)
    taken_by_api = []
    monkeypatch.setattr(attempts, 'publish', lambda d: taken_by_api.append(d) or {'dispatchAccepted': True})

    with connect() as db:
        held = attempts.schedule_reread(db, str(ids['fixing']), str(ids['tenant']), hold_seconds=600)
    # Another API's dispatcher (same code path as its supervisor) sees nothing due.
    assert durable.dispatch_due_jobs() == 0 and taken_by_api == []

    sent_here = []
    count = reread.dispatch_held(connect, str(ids['tenant']), {str(ids['fixing']): str(held['id'])}, 1,
                                 send=lambda d: sent_here.append(d) or {'dispatchAccepted': True})
    assert count == 1 and [d.job_id for d in sent_here] == [str(held['id'])]
    with connect() as db:
        assert db.execute('SELECT status FROM pending_jobs WHERE id=%s', (held['id'],)).fetchone()['status'] == 'dispatched'
    # Nothing left to hand over, and still nothing for the other dispatcher.
    assert reread.dispatch_held(connect, str(ids['tenant']), {str(ids['fixing']): str(held['id'])}, 1,
                                send=sent_here.append) == 0
    assert durable.dispatch_due_jobs() == 0 and taken_by_api == []

    # Without a hold (--no-wait), the job is due for any dispatcher at once.
    with connect() as db:
        free = attempts.schedule_reread(db, str(ids['other']), str(ids['tenant_b']))
    assert durable.dispatch_due_jobs() == 1 and [d.job_id for d in taken_by_api] == [str(free['id'])]


def _stray_waiting_job(connect, ids):
    """A document of the same company, not selected by the re-read, with a job that is due now."""
    with connect() as db:
        owner = db.execute('SELECT owner_id FROM documents WHERE id=%s', (ids['fixing'],)).fetchone()['owner_id']
        stray = db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,
                mime_type,file_size_bytes,file_hash_sha256,status)
            VALUES (%s,%s,'stray.pdf','bill','accounts_payable','synthetic/none.pdf','application/pdf',100,%s,'queued')
            RETURNING id""", (ids['tenant'], owner, hashlib.sha256(b'stray').hexdigest())).fetchone()['id']
        job = db.execute("INSERT INTO pending_jobs(tenant_id,document_id,status) VALUES (%s,%s,'pending') RETURNING id",
                         (ids['tenant'], stray)).fetchone()['id']
    return stray, job


def test_the_wait_dispatches_only_the_selected_jobs(database, monkeypatch):
    connect, ids = database
    from services.queue import inline
    stray, stray_job = _stray_waiting_job(connect, ids)
    monkeypatch.setenv('QUEUE_BACKEND', 'inline')
    monkeypatch.setattr(inline, 'capacity', lambda: 2)
    with connect() as db:
        held = attempts.schedule_reread(db, str(ids['fixing']), str(ids['tenant']), hold_seconds=600)
    sent, wake_up_off = [], []

    def worker(delivery):  # stands in for an inline thread that reads and stores the document
        sent.append(delivery.job_id)
        wake_up_off.append(inline._stop.is_set())
        with connect() as db:
            db.execute("UPDATE pending_jobs SET status='succeeded',finished_at=now() WHERE id=%s", (delivery.job_id,))
            db.execute("UPDATE documents SET status='extracted',processing_completed_at=now() WHERE id=%s",
                       (delivery.document_id,))
        return {'dispatchAccepted': True}

    monkeypatch.setattr(attempts, 'publish', worker)
    seconds = reread.wait_for(connect, [str(ids['fixing'])], 60, poll=0.01, tenant_id=str(ids['tenant']),
                              jobs={str(ids['fixing']): str(held['id'])})
    assert seconds is not None
    assert sent == [str(held['id'])] and wake_up_off == [True]
    with connect() as db:
        assert db.execute('SELECT status FROM pending_jobs WHERE id=%s', (stray_job,)).fetchone()['status'] == 'pending'
        assert db.execute('SELECT status FROM documents WHERE id=%s', (stray,)).fetchone()['status'] == 'queued'


def test_timeout_closes_unfinished_jobs_for_a_retry(database, monkeypatch, capsys):
    connect, ids = database
    from services.queue import inline
    monkeypatch.setenv('QUEUE_BACKEND', 'inline')
    monkeypatch.setattr(inline, 'capacity', lambda: 1)
    monkeypatch.setattr(attempts, 'publish', lambda d: {'dispatchAccepted': True})  # a worker that never finishes
    with connect() as db:
        db.execute("UPDATE documents SET status='failed' WHERE id=%s", (ids['other'],))
    report = reread.reread(company='Synthetic Reread A', timeout_minutes=0, connect=connect)

    assert report['seconds'] is None and report['abandoned'] == [report_line for report_line in report['abandoned']]
    assert len(report['abandoned']) == 1 and report['abandoned'][0].endswith('needs-fixing.pdf')
    with connect() as db:
        job = db.execute("""SELECT status,last_error FROM pending_jobs WHERE document_id=%s
            ORDER BY created_at DESC LIMIT 1""", (ids['fixing'],)).fetchone()
        assert (job['status'], job['last_error']) == ('failed', 'REREAD_TIMEOUT')
        assert db.execute('SELECT status FROM documents WHERE id=%s', (ids['fixing'],)).fetchone()['status'] == 'failed'
        actions = [r['action'] for r in db.execute('SELECT action FROM audit_logs WHERE resource=%s ORDER BY created_at',
                                                    (str(ids['fixing']),)).fetchall()]
        assert actions[0] == 'extraction.reread_scheduled' and actions[-1] == 'extraction.reread_timeout'
    reread.print_report(report)
    assert 'read them again with --status failed' in capsys.readouterr().out

    # Retryable: the next run picks it up again as a failed document.
    again = reread.reread(company='Synthetic Reread A', statuses=('failed',), wait=False, connect=connect)
    assert again['queued'] == 1


def test_abandon_leaves_finished_jobs_alone(database):
    connect, ids = database
    with connect() as db:
        assert attempts.abandon_reread(db, str(ids['fixing']), str(ids['tenant']), str(ids['fixing_job'])) is False
        assert db.execute('SELECT status FROM documents WHERE id=%s', (ids['fixing'],)).fetchone()['status'] == 'exception'


def _second_exception_document(connect, ids):
    with connect() as db:
        owner = db.execute('SELECT owner_id FROM documents WHERE id=%s', (ids['fixing'],)).fetchone()['owner_id']
        return db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,
                mime_type,file_size_bytes,file_hash_sha256,status)
            VALUES (%s,%s,'second.pdf','bill','accounts_payable','synthetic/none.pdf','application/pdf',100,%s,'exception')
            RETURNING id""", (ids['tenant'], owner, hashlib.sha256(b'second').hexdigest())).fetchone()['id']


def test_an_unconfirmed_hand_over_is_reported_and_the_rest_carry_on(database, monkeypatch):
    connect, ids = database
    from services.queue import inline
    second = _second_exception_document(connect, ids)
    monkeypatch.setenv('QUEUE_BACKEND', 'inline')
    monkeypatch.setattr(inline, 'capacity', lambda: 2)
    handed = []

    def transport(delivery):
        handed.append(delivery.document_id)
        with connect() as db:
            if delivery.document_id == str(ids['fixing']):
                # What publish() does when the transport fails: close the reservation, fail the document.
                db.execute("""UPDATE pending_jobs SET status='failed',finished_at=now(),last_error='DISPATCH_UNCONFIRMED'
                    WHERE id=%s""", (delivery.job_id,))
                db.execute("UPDATE documents SET status='failed' WHERE id=%s", (delivery.document_id,))
                fail = True
            else:
                db.execute("UPDATE pending_jobs SET status='succeeded',finished_at=now() WHERE id=%s", (delivery.job_id,))
                db.execute("UPDATE documents SET status='extracted',processing_completed_at=now() WHERE id=%s",
                           (delivery.document_id,))
                fail = False
        if fail:
            raise attempts.AttemptRejected('DISPATCH_UNCONFIRMED')
        return {'dispatchAccepted': True}

    monkeypatch.setattr(attempts, 'publish', transport)
    report = reread.reread(company='Synthetic Reread A', timeout_minutes=1, connect=connect)

    assert sorted(handed) == sorted([str(ids['fixing']), str(second)])
    assert report['seconds'] is not None and not report.get('abandoned')
    assert len(report['dispatch_failed']) == 1 and report['dispatch_failed'][0].endswith('needs-fixing.pdf')
    assert report['result']['by_status'] == {'extracted': 1, 'failed': 1}


def test_nothing_is_handed_over_after_the_deadline(database, monkeypatch):
    connect, ids = database
    import time
    from services.queue import inline
    monkeypatch.setenv('QUEUE_BACKEND', 'inline')
    monkeypatch.setattr(inline, 'capacity', lambda: 2)
    with connect() as db:
        held = attempts.schedule_reread(db, str(ids['fixing']), str(ids['tenant']), hold_seconds=600)
    handed = []
    monkeypatch.setattr(attempts, 'publish', lambda d: handed.append(d) or {'dispatchAccepted': True})
    seconds = reread.wait_for(connect, [str(ids['fixing'])], 60, poll=0.01, tenant_id=str(ids['tenant']),
                              jobs={str(ids['fixing']): str(held['id'])}, deadline=time.monotonic() - 1)
    assert seconds is None and handed == []
    with connect() as db:
        assert db.execute('SELECT status FROM pending_jobs WHERE id=%s', (held['id'],)).fetchone()['status'] == 'pending'


def test_holds_outlast_the_timeout(database, monkeypatch):
    connect, ids = database
    monkeypatch.setenv('QUEUE_BACKEND', 'inline')  # the hold applies when this command drives the inline queue
    report = reread.reread(company='Synthetic Reread A', timeout_minutes=0, connect=connect)
    with connect() as db:
        job = db.execute("""SELECT next_attempt_at - created_at AS hold FROM pending_jobs WHERE document_id=%s
            ORDER BY created_at DESC LIMIT 1""", (ids['fixing'],)).fetchone()
    assert report['abandoned'] and job['hold'].total_seconds() >= reread.HOLD_MARGIN_SECONDS - 1
