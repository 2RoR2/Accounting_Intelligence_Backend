"""Back-to-back uploads: more reservations than inline worker threads must not fail documents.

Several dispatchers (the upload wakeup, a finishing worker, the supervisor) can each read
inline.capacity() before any of them submits, so together they reserve more jobs than there
are threads. The pool refuses the extra ones before taking them; those jobs must go back to
pending (document still queued) and be dispatched later, not fail with DISPATCH_UNCONFIRMED.
Synthetic SQLite harness: this checks the state transitions, not PostgreSQL locking.
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
from uuid import uuid4

import pytest

from services.queue import attempts, inline, jobs
from tests.integration import test_processing_attempts as harness
from tests.unit.accounting import test_accounting_contract as contract

D, T, J = contract.DOCUMENT, contract.TENANT, contract.JOB


@pytest.mark.runtime_defaults
class InlineQueueFloodTests(unittest.TestCase):
    setUp_harness = harness.AttemptProtocolTests.setUp

    def setUp(self):
        self.setUp_harness()
        env = patch.dict(os.environ, {'QUEUE_BACKEND': 'inline', 'INLINE_WORKERS': '2'})
        env.start(); self.addCleanup(env.stop)
        # Re-dispatch is driven by the test; a finishing thread must not race it.
        wake = patch.object(inline, '_wake_dispatch', lambda: None)
        wake.start(); self.addCleanup(wake.stop)
        self.release, self.ran = threading.Event(), []

        def hold_worker(delivery):
            self.ran.append(delivery.job_id)
            self.release.wait(10)

        worker = patch.object(jobs, 'process_inline_delivery', hold_worker)
        worker.start(); self.addCleanup(worker.stop)
        self.addCleanup(self._stop_pool)
        db = self.database.db
        db.execute("UPDATE pending_jobs SET dispatch_count=0, max_attempts=3 WHERE id=?", (J,))
        self.jobs = [J]
        for _ in range(5):
            document, job = str(uuid4()), str(uuid4())
            db.execute("""INSERT INTO documents(id,tenant_id,owner_id,kind,category,status,content_revision)
                VALUES (?,?,?,?,?,'queued',1)""", (document, T, contract.USER['id'], 'invoice', 'accounts_payable'))
            db.execute("""INSERT INTO pending_jobs(id,tenant_id,document_id,status,attempt_count,
                next_attempt_at,dispatch_count,max_attempts) VALUES (?,?,?,'pending',0,CURRENT_TIMESTAMP,0,3)""",
                (job, T, document))
            self.jobs.append(job)
        db.commit()

    def _stop_pool(self):
        self.release.set()
        inline.stop_supervisor()
        with inline._lock:
            inline._inflight.clear()

    def reserve_all_pending(self):
        """What racing dispatchers do together: reserve every due job, then publish."""
        with self.database as db:
            pending = db.execute("SELECT * FROM pending_jobs WHERE status='pending' ORDER BY rowid").fetchall()
            return [attempts.reserve_dispatch(db, str(row['document_id']), str(row['tenant_id']),
                                              job_id=str(row['id'])) for row in pending]

    def rows(self, sql):
        return [dict(row) for row in self.database.db.execute(sql)]

    def test_flooding_the_pool_requeues_the_overflow_and_fails_nothing(self):
        deliveries = self.reserve_all_pending()
        self.assertEqual(len(deliveries), 6)
        with ThreadPoolExecutor(max_workers=3) as dispatchers:
            results = list(dispatchers.map(attempts.publish, deliveries))

        accepted = [r for r in results if r['dispatchAccepted']]
        deferred = [r for r in results if not r['dispatchAccepted']]
        self.assertEqual((len(accepted), len(deferred)), (2, 4))
        for result in deferred:
            self.assertEqual((result['status'], result['jobStatus'], result['queueJobId']), ('QUEUED', 'PENDING', None))

        self.assertEqual(self.rows("SELECT id FROM documents WHERE status='failed'"), [])
        self.assertEqual(self.rows("SELECT id FROM pending_jobs WHERE status='failed'"), [])
        waiting = self.rows("""SELECT j.*, d.status AS document_status FROM pending_jobs j
            JOIN documents d ON d.id=j.document_id WHERE j.status='pending'""")
        self.assertEqual(len(waiting), 4)
        for job in waiting:
            # Back in the outbox exactly as before the reservation: no identity, no budget used.
            self.assertEqual((job['document_status'], job['attempt_count'], job['dispatch_count']), ('queued', 0, 0))
            self.assertIsNone(job['dispatch_token'])
            self.assertIsNone(job['dispatch_expires_at'])
        actions = [row['action'] for row in self.rows('SELECT action FROM audit_logs')]
        self.assertEqual(actions.count('extraction.dispatch_deferred'), 4)
        self.assertNotIn('extraction.dispatch_unconfirmed', actions)

        # Threads free up: the next dispatch sends the requeued jobs.
        self.release.set()
        for _ in range(250):
            if inline.inflight() == 0:
                break
            threading.Event().wait(0.02)
        self.assertEqual(inline.inflight(), 0)
        self.release.clear()
        again = [attempts.publish(delivery) for delivery in self.reserve_all_pending()]
        self.assertEqual([r['dispatchAccepted'] for r in again], [True, True, False, False])
        self.assertEqual(self.rows("SELECT id FROM pending_jobs WHERE status='failed'"), [])
        self.assertEqual(len(self.rows("SELECT id FROM pending_jobs WHERE status='dispatched'")), 4)

    def test_other_inline_transport_errors_are_still_fenced(self):
        delivery, = [d for d in self.reserve_all_pending() if d.job_id == J]
        with patch.object(inline, 'submit', side_effect=ConnectionError('pool shut down')):
            with self.assertRaisesRegex(attempts.AttemptRejected, 'DISPATCH_UNCONFIRMED'):
                attempts.publish(delivery)
        job, = self.rows(f"SELECT status,last_error FROM pending_jobs WHERE id='{J}'")
        self.assertEqual((job['status'], job['last_error']), ('failed', 'DISPATCH_UNCONFIRMED'))

    def test_a_reservation_that_changed_hands_is_not_requeued(self):
        delivery, = [d for d in self.reserve_all_pending() if d.job_id == J]
        self.database.db.execute("UPDATE pending_jobs SET dispatch_token=? WHERE id=?", (str(uuid4()), J))
        self.database.db.commit()
        self.assertFalse(attempts.return_to_outbox(delivery))
        job, = self.rows(f"SELECT status FROM pending_jobs WHERE id='{J}'")
        self.assertEqual(job['status'], 'dispatched')
