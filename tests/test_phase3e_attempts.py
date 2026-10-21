"""Synthetic SQL/worker regressions. SQLite serialization is NOT PG concurrency proof."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
import app as api
from infrastructure.queue import attempts, jobs
from services.extraction import persistence
from tests import test_accounting_contract as contract
from tests.test_phase3d_mutations import MutationDatabase

D,T,J = contract.DOCUMENT,contract.TENANT,contract.JOB


class AttemptProtocolTests(unittest.TestCase):
    def setUp(self):
        self.database=MutationDatabase()
        self.addCleanup(self.database.db.close)
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cpu=Mock()
        self.cpu.enqueue_call.return_value=SimpleNamespace(id='ignored-untrusted-return-value')
        for guard in (patch.object(attempts,'connect',return_value=self.database),
                      patch.object(persistence,'connect',return_value=self.database),
                      patch.object(api,'connect',return_value=self.database),
                      patch.object(api,'session_user',return_value=contract.USER),
                      patch('infrastructure.queue.config.get_queues',return_value=(self.cpu,None)),
                      patch.object(api,'check_schema_compatibility',side_effect=AssertionError('No startup SQL'))):
            guard.start(); self.addCleanup(guard.stop)
        self.client=TestClient(api.app,raise_server_exceptions=False,headers={'X-Requested-With':'AccountingIntelligence'})
        self.addCleanup(self.client.close)

    def reserve(self):
        with self.database as db:
            return attempts.reserve_dispatch(db,D,T,job_id=J)

    def claim(self,delivery=None):
        delivery=delivery or self.reserve()
        return attempts.claim_delivery(delivery,delivery.dispatch_token)

    def artifact(self,run=None):
        value=contract.fixture()
        value['provenance']['service_run_id']=run or 'synthetic-'+uuid4().hex
        file=Path(self.temp.name)/(uuid4().hex+'.json')
        file.write_text(json.dumps(value))
        return {'service_run_id':value['provenance']['service_run_id'],'public_result_path':str(file)}

    def persist(self,claim,artifact=None):
        return persistence.persist_extraction_result(D,artifact or self.artifact(),claim=claim)

    def retry(self):
        return self.client.post('/api/documents/'+D+'/retry')

    def job(self,job_id=J):
        return self.database.execute('SELECT * FROM pending_jobs WHERE id=%s',(job_id,)).fetchone()

    def expire(self,claim):
        self.database.db.execute('UPDATE pending_jobs SET processing_expires_at=? WHERE id=?',
            (datetime.now(timezone.utc)-timedelta(seconds=1),claim.delivery.job_id))
        self.database.db.commit()

    def test_normal_success_binds_raw_run_claim_and_single_current(self):
        claim=self.claim(); result=self.persist(claim)
        job=self.job()
        self.assertEqual((job['status'],job['attempt_count'],job['dispatch_count']),('succeeded',1,1))
        self.assertEqual(self.database.execute('SELECT pending_job_id,is_current FROM raw_extractions').fetchone(),
                         {'pending_job_id':J,'is_current':1})
        self.assertEqual(result['job_id'],J)
        self.assertEqual(self.database.execute('SELECT content_revision FROM documents').fetchone()['content_revision'],2)
        self.assertIn('extraction.succeeded',[r['action'] for r in self.database.execute('SELECT action FROM audit_logs').fetchall()])

    def test_completion_requires_exact_identity_before_reading_artifact(self):
        with patch('builtins.open',side_effect=AssertionError('No artifact read')):
            with self.assertRaisesRegex(attempts.AttemptRejected,'IDENTITY_REQUIRED'):
                persistence.persist_extraction_result(D,{})

    def test_worker_failure_is_bound_sanitized_and_preserves_history(self):
        claim=self.claim(); before=self.database.execute('SELECT * FROM raw_extractions').fetchall()
        self.assertTrue(persistence.mark_extraction_job_failed(D,'private invoice amount',claim=claim))
        self.assertEqual(self.job()['status'],'failed')
        self.assertEqual(self.job()['last_error'],'EXTRACTION_FAILED')
        self.assertEqual(self.database.execute('SELECT status FROM documents').fetchone()['status'],'failed')
        self.assertEqual(self.database.execute('SELECT * FROM raw_extractions').fetchall(),before)
        self.assertFalse(persistence.mark_extraction_job_failed(D,claim=claim))

    def test_failed_attempt_retry_creates_new_id_and_terminal_row_is_unchanged(self):
        claim=self.claim(); attempts.fail_claim(claim); old=self.job()
        response=self.retry()
        self.assertEqual(response.status_code,200,response.text)
        new=response.json()['jobId']; self.assertNotEqual(new,J)
        self.assertEqual(self.job(),old)
        self.assertEqual(self.job(new)['status'],'dispatched')
        self.assertEqual(self.job(new)['attempt_count'],0)

    def test_repeated_retry_dispatches_once(self):
        attempts.fail_claim(self.claim())
        self.assertEqual(self.retry().status_code,200)
        for _ in range(4):
            self.assertEqual(self.retry().status_code,409)
        self.cpu.enqueue_call.assert_called_once()
        self.assertEqual(self.database.execute("SELECT count(*) AS n FROM pending_jobs WHERE status IN ('pending','dispatched','processing')").fetchone()['n'],1)

    def test_pending_legacy_queued_job_is_not_guessed_or_dispatched(self):
        before=self.database.snapshot()
        self.assertEqual(self.retry().status_code,409)
        self.assertEqual(self.database.snapshot(),before)
        self.cpu.enqueue_call.assert_not_called()

    def test_active_dispatched_retry_is_non_mutating(self):
        self.reserve(); before=self.database.snapshot()
        self.assertEqual(self.retry().status_code,409)
        self.assertEqual(self.database.snapshot(),before)

    def test_active_processing_retry_is_non_mutating(self):
        self.claim(); before=self.database.snapshot()
        self.assertEqual(self.retry().status_code,409)
        self.assertEqual(self.database.snapshot(),before)

    def test_expired_claim_is_closed_before_new_attempt_and_fences_old_success(self):
        old=self.claim(); artifact=self.artifact(); self.expire(old)
        response=self.retry(); self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.job()['last_error'],'LEASE_EXPIRED')
        before=self.database.snapshot()
        with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_ATTEMPT'):
            self.persist(old,artifact)
        self.assertEqual(self.database.snapshot(),before)

    def test_old_failure_after_new_success_cannot_downgrade_current_run(self):
        old=self.claim(); attempts.fail_claim(old)
        result=self.retry().json()
        fresh=attempts.Delivery(D,result['jobId'],T,result['queueJobId'])
        new=attempts.claim_delivery(fresh,fresh.dispatch_token)
        self.persist(new)
        before=self.database.snapshot()
        self.assertFalse(attempts.fail_claim(old))
        self.assertEqual(self.database.snapshot(),before)
        self.assertEqual(self.job(fresh.job_id)['status'],'succeeded')

    def test_old_success_after_new_success_is_rejected_without_writes(self):
        old=self.claim(); artifact=self.artifact(); attempts.fail_claim(old)
        result=self.retry().json()
        fresh=attempts.Delivery(D,result['jobId'],T,result['queueJobId'])
        self.persist(attempts.claim_delivery(fresh,fresh.dispatch_token))
        before=self.database.snapshot()
        with self.assertRaises(attempts.AttemptRejected): self.persist(old,artifact)
        self.assertEqual(self.database.snapshot(),before)

    def test_terminal_failed_and_cancelled_delivery_replay_is_rejected(self):
        delivery=self.reserve()
        for status in ('failed','cancelled','succeeded'):
            self.database.db.execute("UPDATE pending_jobs SET status=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",(status,J))
            self.database.db.commit()
            before=self.database.snapshot()
            with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_DELIVERY'):
                attempts.claim_delivery(delivery,delivery.dispatch_token)
            self.assertEqual(self.database.snapshot(),before)

    def test_duplicate_delivery_can_claim_only_once(self):
        delivery=self.reserve(); self.claim(delivery); before=self.database.snapshot()
        with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_DELIVERY'): self.claim(delivery)
        self.assertEqual(self.database.snapshot(),before)
        self.assertEqual(self.job()['attempt_count'],1)

    def test_rq_id_is_verified_and_rq_dedup_is_not_assumed(self):
        delivery=self.reserve(); before=self.database.snapshot()
        with self.assertRaisesRegex(attempts.AttemptRejected,'RQ_DELIVERY_MISMATCH'):
            attempts.claim_delivery(delivery,str(uuid4()))
        self.assertEqual(self.database.snapshot(),before)

    def test_dispatch_uses_pinned_job_id_json_payload_no_automatic_rq_retry(self):
        delivery=self.reserve(); response=attempts.publish(delivery)
        args,kwargs=self.cpu.enqueue_call.call_args
        self.assertEqual(kwargs['args'],(D,J,T,delivery.dispatch_token))
        self.assertEqual(kwargs['job_id'],delivery.dispatch_token)
        self.assertNotIn('retry',kwargs)
        self.assertEqual(kwargs['failure_ttl'],0)
        self.assertEqual(response['queueJobId'],delivery.dispatch_token)
        json.dumps(kwargs['args'])

    def test_dispatch_error_fences_ambiguous_redis_delivery_and_reports_503(self):
        self.database.db.execute("UPDATE documents SET status='failed'"); self.database.db.commit()
        self.cpu.enqueue_call.side_effect=ConnectionError('private path and invoice fields')
        response=self.retry()
        self.assertEqual(response.status_code,503,response.text)
        self.assertFalse(response.json()['detail']['dispatchAccepted'])
        self.assertEqual(self.job()['status'],'failed')
        self.assertNotIn('private path',response.text)
        delivery_args=self.cpu.enqueue_call.call_args.kwargs['args']
        delivery=attempts.Delivery(*delivery_args)
        with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_DELIVERY'): self.claim(delivery)

    def test_dispatch_exception_after_worker_claim_does_not_fail_owned_attempt(self):
        delivery=self.reserve()
        def accepted_then_error(*args,**kwargs):
            self.claim(delivery)
            raise ConnectionError('ambiguous response')
        self.cpu.enqueue_call.side_effect=accepted_then_error
        with self.assertRaisesRegex(attempts.AttemptRejected,'DISPATCH_UNCONFIRMED'): attempts.publish(delivery)
        self.assertEqual(self.job()['status'],'processing')
        self.assertEqual(self.database.execute('SELECT status FROM documents').fetchone()['status'],'processing')

    def test_foreign_tenant_delivery_cannot_claim(self):
        delivery=self.reserve(); before=self.database.snapshot()
        foreign=replace(delivery,tenant_id=str(uuid4()))
        with self.assertRaisesRegex(attempts.AttemptRejected,'DOCUMENT_MISMATCH'): self.claim(foreign)
        self.assertEqual(self.database.snapshot(),before)

    def test_foreign_document_or_job_delivery_cannot_claim(self):
        delivery=self.reserve(); before=self.database.snapshot()
        for bad in (replace(delivery,document_id=str(uuid4())),replace(delivery,job_id=str(uuid4()))):
            with self.assertRaises(attempts.AttemptRejected): self.claim(bad)
        self.assertEqual(self.database.snapshot(),before)

    def test_wrong_claim_token_cannot_complete_or_fail(self):
        claim=self.claim(); forged=replace(claim,claim_token=str(uuid4()))
        before=self.database.snapshot()
        with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_ATTEMPT'): self.persist(forged)
        self.assertFalse(attempts.fail_claim(forged))
        self.assertEqual(self.database.snapshot(),before)

    def test_corrected_document_blocks_claim_even_when_payload_is_valid(self):
        delivery=self.reserve()
        self.database.db.execute("UPDATE documents SET status='corrected'"); self.database.db.commit()
        before=self.database.snapshot()
        with self.assertRaisesRegex(attempts.AttemptRejected,'AMENDMENT_REQUIRED'): self.claim(delivery)
        self.assertEqual(self.job()['status'],'cancelled')
        self.assertEqual(self.database.snapshot()['documents'],before['documents'])
        self.assertFalse(self.database.execute('SELECT * FROM raw_extractions').fetchall())

    def test_approved_document_blocks_completion_and_failure_cannot_downgrade_it(self):
        claim=self.claim()
        self.database.db.execute("INSERT INTO review_decisions VALUES (?,?,'approved',1,NULL,NULL,NULL,NULL,NULL)",(D,T))
        self.database.db.commit()
        before=self.database.execute('SELECT * FROM documents').fetchone()
        with self.assertRaisesRegex(attempts.AttemptRejected,'AMENDMENT_REQUIRED'): self.persist(claim)
        self.assertTrue(attempts.fail_claim(claim))
        self.assertEqual(self.job()['status'],'cancelled')
        self.assertEqual(self.database.execute('SELECT * FROM documents').fetchone(),before)

    def test_canonical_insert_failure_rolls_back_run_fields_revision_and_job(self):
        claim=self.claim(); before=self.database.snapshot()
        execute=self.database.execute
        def broken(sql,params=()):
            if 'INSERT INTO field_predictions' in sql: raise RuntimeError('synthetic insertion failure')
            return execute(sql,params)
        with patch.object(self.database,'execute',side_effect=broken):
            with self.assertRaisesRegex(RuntimeError,'insertion failure'): self.persist(claim)
        self.assertEqual(self.database.snapshot(),before)
        self.assertEqual(self.job()['status'],'processing')

    def test_lease_expiry_at_final_fence_rolls_back_switched_current_run(self):
        claim=self.claim(); before=self.database.snapshot()
        original=attempts.complete
        def expire_then_complete(db,proof,document):
            db.execute('UPDATE pending_jobs SET processing_expires_at=%s WHERE id=%s',
                       (datetime.now(timezone.utc)-timedelta(seconds=1),J))
            return original(db,proof,document)
        with patch.object(attempts,'complete',side_effect=expire_then_complete):
            with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_ATTEMPT'): self.persist(claim)
        self.assertEqual(self.database.snapshot(),before)

    def test_completed_claim_replay_is_read_only_with_another_open_attempt(self):
        claim=self.claim(); artifact=self.artifact()
        first=self.persist(claim,artifact)
        self.database.db.execute('INSERT INTO pending_jobs(id,tenant_id,document_id) VALUES (?,?,?)',(str(uuid4()),T,D))
        self.database.db.commit(); before=self.database.snapshot()
        second=self.persist(claim,artifact)
        self.assertTrue(second['idempotent'])
        self.assertEqual(first['extraction_id'],second['extraction_id'])
        self.assertEqual(self.database.snapshot(),before)

    def test_duplicate_claim_threads_are_serialized_by_adapter_not_pg_proof(self):
        delivery=self.reserve()
        def run(_):
            try: self.claim(delivery); return 'claimed'
            except attempts.AttemptRejected: return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,range(2)))
        self.assertEqual(sorted(results),['claimed','rejected'])
        self.assertEqual(self.job()['attempt_count'],1)

    def test_retry_completion_race_is_serialized_by_adapter_not_pg_proof(self):
        claim=self.claim()
        def complete(): self.persist(claim); return 'complete'
        def retry(): return self.retry().status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(complete); b=pool.submit(retry)
            self.assertEqual(a.result(),'complete'); self.assertEqual(b.result(),409)
        self.assertEqual(self.job()['status'],'succeeded')
        self.assertEqual(self.database.execute('SELECT count(*) AS n FROM pending_jobs').fetchone()['n'],1)

    def test_production_worker_claims_and_persists_without_using_offline_hooks(self):
        delivery=self.reserve(); artifact=self.artifact()
        source=Path(self.temp.name)/'synthetic.pdf'; source.write_bytes(b'%PDF-synthetic')
        with patch('rq.get_current_job',return_value=SimpleNamespace(id=delivery.dispatch_token)), patch.object(jobs,'resolve_document_from_database',return_value=source), patch('services.extraction.execution.execute_document_extraction',return_value={'execution_status':'EXTRACTED',**artifact}), patch.object(jobs,'_TEST_DOCUMENT_RESOLVER',side_effect=AssertionError('No offline hook')):
            result=jobs.process_document_extraction_job(D,J,T,delivery.dispatch_token)
        self.assertEqual(result['status'],'EXTRACTED')
        self.assertEqual(self.job()['status'],'succeeded')
        self.assertNotIn('public_result_path',json.dumps(result))

    def test_worker_resolution_failure_is_persisted_for_exact_claim(self):
        delivery=self.reserve()
        with patch('rq.get_current_job',return_value=SimpleNamespace(id=delivery.dispatch_token)), patch.object(jobs,'resolve_document_from_database',return_value=None):
            with self.assertRaisesRegex(jobs.ExtractionExecutionError,'attempt failed'):
                jobs.process_document_extraction_job(D,J,T,delivery.dispatch_token)
        self.assertEqual(self.job()['status'],'failed')
        self.assertEqual(self.job()['last_error'],'EXTRACTION_FAILED')

    def test_worker_execution_error_is_redacted_before_rq_receives_it(self):
        delivery=self.reserve(); source=Path(self.temp.name)/'source'; source.write_bytes(b'synthetic')
        with patch('rq.get_current_job',return_value=SimpleNamespace(id=delivery.dispatch_token)), patch.object(jobs,'resolve_document_from_database',return_value=source), patch('services.extraction.execution.execute_document_extraction',side_effect=RuntimeError('customer source secret')):
            with self.assertRaises(jobs.ExtractionExecutionError) as caught:
                jobs.process_document_extraction_job(D,J,T,delivery.dispatch_token)
        self.assertNotIn('customer source secret',str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.job()['status'],'failed')

    def test_legacy_payload_is_rejected_without_database_or_file_io(self):
        with patch.object(attempts,'connect',side_effect=AssertionError('No DB')), patch.object(jobs,'resolve_document_from_database',side_effect=AssertionError('No source IO')):
            self.assertEqual(jobs.process_document_extraction_job(D)['reason'],'LEGACY_PAYLOAD')

    def test_production_worker_requires_rq_execution_context(self):
        delivery=self.reserve(); before=self.database.snapshot()
        with patch('rq.get_current_job',return_value=None):
            self.assertEqual(jobs.process_document_extraction_job(D,J,T,delivery.dispatch_token)['reason'],'RQ_CONTEXT_REQUIRED')
        self.assertEqual(self.database.snapshot(),before)


    def test_expired_dispatch_recovery_uses_new_job_and_rejects_old_delivery(self):
        old=self.reserve()
        self.database.db.execute('UPDATE pending_jobs SET dispatch_expires_at=? WHERE id=?',
            (datetime.now(timezone.utc)-timedelta(seconds=1),J))
        self.database.db.commit()
        response=self.retry()
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotEqual(response.json()['jobId'],J)
        before=self.database.snapshot()
        with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_DELIVERY'): self.claim(old)
        self.assertEqual(self.database.snapshot(),before)

    def test_terminal_claim_completion_and_failure_replay_cannot_write(self):
        claim=self.claim()
        for state in ('failed','cancelled'):
            self.database.db.execute('UPDATE pending_jobs SET status=?,finished_at=CURRENT_TIMESTAMP WHERE id=?',(state,J))
            self.database.db.commit(); before=self.database.snapshot()
            with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_ATTEMPT'): self.persist(claim)
            self.assertFalse(attempts.fail_claim(claim))
            self.assertEqual(self.database.snapshot(),before)

    def test_failure_of_later_attempt_preserves_previous_extraction_and_evidence(self):
        self.persist(self.claim())
        before={table:self.database.execute('SELECT * FROM '+table).fetchall()
                for table in ('raw_extractions','field_predictions','line_items')}
        new_id=str(uuid4())
        self.database.db.execute('INSERT INTO pending_jobs(id,tenant_id,document_id) VALUES (?,?,?)',(new_id,T,D))
        self.database.db.execute("UPDATE documents SET status='queued'"); self.database.db.commit()
        with self.database as db: delivery=attempts.reserve_dispatch(db,D,T,job_id=new_id)
        claim=self.claim(delivery); self.assertTrue(attempts.fail_claim(claim))
        self.assertEqual({table:self.database.execute('SELECT * FROM '+table).fetchall() for table in before},before)
        self.assertEqual(self.database.execute('SELECT content_revision FROM documents').fetchone()['content_revision'],2)

    def test_foreign_tenant_and_foreign_job_claim_cannot_complete_or_fail(self):
        claim=self.claim(); before=self.database.snapshot()
        for bad in (replace(claim,delivery=replace(claim.delivery,tenant_id=str(uuid4()))),
                    replace(claim,delivery=replace(claim.delivery,job_id=str(uuid4())))):
            with self.assertRaises(attempts.AttemptRejected): self.persist(bad)
            self.assertFalse(attempts.fail_claim(bad))
        self.assertEqual(self.database.snapshot(),before)

    def test_success_audit_failure_rolls_back_fields_switch_revision_and_job(self):
        claim=self.claim(); before=self.database.snapshot(); original=attempts.audit
        def fail_audit(db,doc,job_id,action):
            if action=='extraction.succeeded': raise RuntimeError('synthetic audit failure')
            return original(db,doc,job_id,action)
        with patch.object(attempts,'audit',side_effect=fail_audit):
            with self.assertRaisesRegex(RuntimeError,'audit failure'): self.persist(claim)
        self.assertEqual(self.database.snapshot(),before)

    def upload(self,user=None):
        # Direct route invocation: synthetic authenticated user, no HTTP multipart
        # dependency or lifespan. SQL, signature check and private file write run.
        async def read(): return b'%PDF-synthetic protocol test'
        file=SimpleNamespace(filename='synthetic.pdf',content_type='application/pdf',read=read)
        original=self.database.execute
        def defaults(sql,params=()):
            if 'pg_advisory_xact_lock(' in sql:
                # Offline adapter only; this does not prove PostgreSQL locking.
                return contract.Rows([{'isolation':'read committed'}])
            # PostgreSQL gen_random_uuid defaults absent from SQLite fixture.
            if 'INSERT INTO documents(tenant_id,owner_id' in sql:
                sql=sql.replace('documents(tenant_id,owner_id','documents(id,tenant_id,owner_id').replace('VALUES (%s','VALUES (%s,%s',1)
                params=(str(uuid4()),)+tuple(params)
            elif 'INSERT INTO pending_jobs(tenant_id,document_id)' in sql:
                sql=sql.replace('pending_jobs(tenant_id,document_id)','pending_jobs(id,tenant_id,document_id)').replace('VALUES (%s','VALUES (%s,%s',1)
                params=(str(uuid4()),)+tuple(params)
            return original(sql,params)
        with patch.object(self.database,'execute',side_effect=defaults), patch.object(api,'require_workspace_user',return_value=user or contract.USER), patch.dict('os.environ',{'DOCUMENT_STORAGE_PATH':self.temp.name}):
            return asyncio.run(api.upload_document(None,file))

    def test_upload_commits_document_job_and_reservation_before_redis(self):
        def check_commit(*args,**kwargs):
            self.assertFalse(self.database.db.in_transaction)
            row=self.database.execute('SELECT status,dispatch_token FROM pending_jobs WHERE id=%s',(kwargs['args'][1],)).fetchone()
            self.assertEqual(row['status'],'dispatched')
            self.assertEqual(row['dispatch_token'],kwargs['job_id'])
        self.cpu.enqueue_call.side_effect=check_commit
        response=self.upload()
        self.assertTrue(response['dispatchAccepted'])
        self.assertEqual(response['status'],'QUEUED')
        self.assertEqual(response['jobStatus'],'DISPATCHED')
        self.assertNotEqual(response['jobId'],J)
        self.assertEqual(len(list(Path(self.temp.name).glob('*synthetic.pdf'))),1)

    def test_upload_reservation_failure_rolls_back_new_document_and_removes_only_new_file(self):
        before=self.database.snapshot()
        with patch.object(attempts,'reserve_dispatch',side_effect=RuntimeError('synthetic reservation failure')):
            with self.assertRaisesRegex(RuntimeError,'reservation failure'): self.upload()
        self.assertEqual(self.database.snapshot(),before)
        self.assertEqual(list(Path(self.temp.name).glob('*synthetic.pdf')),[])
        self.cpu.enqueue_call.assert_not_called()

    def test_upload_dispatch_failure_keeps_document_ids_and_truthful_failure(self):
        from fastapi import HTTPException
        self.cpu.enqueue_call.side_effect=ConnectionError('private transport detail')
        with self.assertRaises(HTTPException) as error: self.upload()
        self.assertEqual(error.exception.status_code,503)
        self.assertFalse(error.exception.detail['dispatchAccepted'])
        doc=error.exception.detail['documentId']; job=error.exception.detail['jobId']
        self.assertNotEqual(doc,D)
        self.assertEqual(self.job(job)['status'],'failed')
        self.assertEqual(self.database.execute('SELECT status FROM documents WHERE id=%s',(doc,)).fetchone()['status'],'failed')
        self.assertEqual(len(list(Path(self.temp.name).glob('*synthetic.pdf'))),1)


    def test_expiry_at_failure_cas_rejects_and_rolls_back_all_failure_writes(self):
        claim=self.claim(); before=self.database.snapshot()
        original=attempts.ensure_unreviewed
        def expire(db,document):
            db.execute('UPDATE pending_jobs SET processing_expires_at=%s WHERE id=%s',
                (datetime.now(timezone.utc)-timedelta(seconds=1),J))
            return original(db,document)
        with patch.object(attempts,'ensure_unreviewed',side_effect=expire):
            self.assertFalse(attempts.fail_claim(claim))
        self.assertEqual(self.database.snapshot(),before)
