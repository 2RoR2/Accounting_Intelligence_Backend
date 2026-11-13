"""In-process queue transport: bounded hand-off without Redis or a database."""
import threading

import pytest

from services.queue import attempts, backend, durable, inline, jobs

pytestmark = pytest.mark.runtime_defaults


@pytest.fixture(autouse=True)
def clean_inline(monkeypatch):
    monkeypatch.setenv('QUEUE_BACKEND', 'inline')
    monkeypatch.setenv('INLINE_WORKERS', '2')
    # Completion normally pulls the next job from PostgreSQL; not in these tests.
    monkeypatch.setattr(inline, '_wake_dispatch', lambda: None)
    yield
    inline.stop_supervisor()
    with inline._lock:
        inline._inflight.clear()


def delivery(token):
    return attempts.Delivery('doc-' + token, 'job-' + token, 'tenant', token)


def test_backend_defaults_to_inline_and_rejects_unknown_names(monkeypatch):
    monkeypatch.delenv('QUEUE_BACKEND', raising=False)
    assert backend.queue_backend() == 'inline'
    monkeypatch.setenv('QUEUE_BACKEND', 'kafka')
    with pytest.raises(RuntimeError):
        backend.queue_backend()


def test_submit_runs_the_delivery_and_frees_the_worker(monkeypatch):
    done, seen = threading.Event(), []

    def process(item):
        seen.append(item.dispatch_token)
        done.set()

    monkeypatch.setattr(jobs, 'process_inline_delivery', process)
    assert inline.capacity() == 2
    inline.submit(delivery('a'))
    assert done.wait(5) and seen == ['a']
    for _ in range(100):
        if inline.capacity() == 2:
            break
        threading.Event().wait(0.02)
    assert inline.capacity() == 2 and inline.inflight() == 0


def test_capacity_is_bounded_and_duplicates_are_ignored(monkeypatch):
    release, started = threading.Event(), threading.Semaphore(0)

    def process(item):
        started.release()
        release.wait(5)

    monkeypatch.setattr(jobs, 'process_inline_delivery', process)
    inline.submit(delivery('a'))
    inline.submit(delivery('a'))  # Same reservation handed over twice: still one run.
    inline.submit(delivery('b'))
    assert started.acquire(timeout=5) and started.acquire(timeout=5)
    assert inline.capacity() == 0 and inline.inflight() == 2
    with pytest.raises(inline.InlineQueueFull):
        inline.submit(delivery('c'))
    release.set()


def test_a_failing_document_does_not_kill_the_worker(monkeypatch):
    done = threading.Event()

    def process(item):
        if item.dispatch_token == 'bad':
            raise RuntimeError('boom')
        done.set()

    monkeypatch.setattr(jobs, 'process_inline_delivery', process)
    inline.submit(delivery('bad'))
    inline.submit(delivery('good'))
    assert done.wait(5)


def test_dispatch_reserves_nothing_when_every_worker_is_busy(monkeypatch):
    monkeypatch.setattr(inline, 'capacity', lambda: 0)
    monkeypatch.setattr(durable, 'connect', lambda: pytest.fail('No reservation without free capacity'))
    assert durable.dispatch_due_jobs() == 0


def test_inline_delivery_that_cannot_be_claimed_is_rejected_without_reading_files(monkeypatch):
    def refuse(item, token):
        assert token == item.dispatch_token  # The dispatch token stands in for the RQ job id.
        raise attempts.AttemptRejected('STALE_DELIVERY')

    monkeypatch.setattr(attempts, 'claim_delivery', refuse)
    monkeypatch.setattr(jobs, '_execute_claimed', lambda *a: pytest.fail('A rejected delivery must not run'))
    assert jobs.process_inline_delivery(delivery('x')) == {'status': 'REJECTED', 'reason': 'STALE_DELIVERY'}


def test_queue_package_imports_without_redis_helpers_being_touched():
    import services.queue as package
    assert callable(package.get_queues) and callable(package.get_redis_connection)


class _RecordingDb:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self

    def fetchall(self):
        return [{'id': 'job-1'}]


def test_lease_release_touches_only_stale_leases_inside_the_queue_scope(monkeypatch):
    import database.connection
    db = _RecordingDb()
    monkeypatch.setattr(database.connection, 'connect', lambda: db)
    tenant = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setenv('QUEUE_TENANT_ID', tenant)
    monkeypatch.delenv('INLINE_STALE_LEASE_SECONDS', raising=False)
    assert inline.release_orphaned_leases() == 1
    sql, params = db.calls[0]
    # A lease a live process is still heartbeating (another worker, a teammate's API) is never cut.
    assert 'GREATEST(j.heartbeat_at, j.updated_at) < clock_timestamp()' in sql
    assert params[0] == 90
    # The same tenant/owner/upload scope as dispatch applies on a shared database.
    assert 'j.tenant_id=%s::uuid' in sql and tenant in params


@pytest.mark.parametrize('value', ['59', '3601', 'soon'])
def test_stale_lease_window_is_validated(monkeypatch, value):
    monkeypatch.setenv('INLINE_STALE_LEASE_SECONDS', value)
    with pytest.raises(RuntimeError):
        inline.stale_lease_seconds()


def test_every_supervisor_cycle_releases_stale_leases_first(monkeypatch):
    order = []
    monkeypatch.setattr(inline, 'release_orphaned_leases', lambda: order.append('release') or 0)
    monkeypatch.setattr(durable, 'recover_expired_jobs', lambda: order.append('recover') or 0)
    monkeypatch.setattr(durable, 'dispatch_due_jobs', lambda limit=50: order.append('dispatch') or 0)
    monkeypatch.delenv('INLINE_RECOVER_ON_START', raising=False)
    assert inline.run_cycle() == (0, 0) and order == ['release', 'recover', 'dispatch']
    order.clear()
    monkeypatch.setenv('INLINE_RECOVER_ON_START', 'false')
    inline.run_cycle()
    assert order == ['recover', 'dispatch']


def test_legacy_supervisor_refuses_to_start_without_rq(monkeypatch):
    # A teammate who still starts the old supervisor after merging must not get a second,
    # unexpected extraction pool: it stops with a clear message instead.
    from services.queue import queue_supervisor
    monkeypatch.delenv('QUEUE_BACKEND', raising=False)
    monkeypatch.setattr(queue_supervisor, 'dispatch_due_jobs', lambda **k: pytest.fail('must not dispatch'))
    with pytest.raises(SystemExit) as stopped:
        queue_supervisor.main()
    assert 'QUEUE_BACKEND=rq' in str(stopped.value)


def test_legacy_rq_worker_refuses_to_start_without_rq(monkeypatch):
    pytest.importorskip('rq')
    from services.queue import worker
    monkeypatch.delenv('QUEUE_BACKEND', raising=False)
    monkeypatch.setattr(worker, 'get_redis_connection', lambda: pytest.fail('must not connect to Redis'))
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    assert 'QUEUE_BACKEND=rq' in str(stopped.value)


def test_require_rq_allows_the_legacy_setup(monkeypatch):
    monkeypatch.setenv('QUEUE_BACKEND', 'rq')
    backend.require_rq('The RQ worker')  # No exit.


@pytest.mark.parametrize('code', sorted(attempts.LITE_FAILURE_CODES))
def test_lite_failure_codes_are_stored_as_is(code):
    assert attempts.safe_failure_code('Extraction execution failed: ' + code) == code


@pytest.mark.parametrize('message,expected', [
    ('Extraction execution failed: OCR_NO_TEXT', 'OCR_NO_TEXT'),
    ('Extraction execution failed: UNSUPPORTED_EXTRACTION_FORMAT', 'UNSUPPORTED_EXTRACTION_FORMAT'),
    ('Extraction execution failed: SOURCE_SIZE_LIMIT', 'SOURCE_SIZE_LIMIT'),
    ('Extraction attempt failed: TimeoutError', 'EXTRACTION_FAILED:TimeoutError'),
    ('Extraction attempt failed: not an identifier!', 'EXTRACTION_FAILED'),
    # Arbitrary text, including code-shaped words that are not allowlisted, never reaches the database.
    ('Extraction execution failed: SECRET_CUSTOMER_NAME at C:/private/file.pdf', 'EXTRACTION_FAILED'),
    ('INVALID_OR_UNREADABLE_PDFX', 'EXTRACTION_FAILED'),
    ('', 'EXTRACTION_FAILED'),
    (None, 'EXTRACTION_FAILED'),
])
def test_failure_code_selection_keeps_only_allowlisted_codes(message, expected):
    assert attempts.safe_failure_code(message) == expected
