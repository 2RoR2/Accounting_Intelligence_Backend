"""Explicitly selected disposable PostgreSQL tests. No default/shared DB access.

Synthetic fixtures below test transaction/lease behavior, not model accuracy.
The separate saved HTTP/browser evidence contains the genuine document runs.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from services.queue import attempts
from services.extraction.persistence import persistence


@unittest.skipUnless(os.getenv('MODULE2_LIVE_CONFIG'),'Explicit owned disposable resource configuration required')
class Module2PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(os.environ['MODULE2_LIVE_CONFIG']).resolve()
        root=Path(__file__).resolve().parents[1]
        assert path.is_relative_to(root/'runs')
        cls.config=json.loads(path.read_text(encoding='utf-8-sig'))
        assert (cls.config['dbname'],cls.config['pgPort'],cls.config['cluster'])==('saic_m2_20261027',55447,'saic_m2_20261027')
        with cls.db() as db:
            row=db.execute("SELECT current_database() AS name,current_user AS role,current_setting('cluster_name') AS cluster").fetchone()
            assert (row['name'],row['role'],row['cluster'])==(cls.config['dbname'],cls.config['dbuser'],cls.config['cluster'])

    @classmethod
    def db(cls):
        return psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        with self.db() as db:
            owner=db.execute("SELECT id,tenant_id FROM users WHERE email='m2-accountant-1@example.test'").fetchone()
            self.tenant=str(owner['tenant_id'])
            self.document=str(db.execute("""INSERT INTO documents(tenant_id,owner_id,original_filename,kind,category,storage_path,mime_type,file_size_bytes,file_hash_sha256,status)
                VALUES (%s,%s,'synthetic-transaction-control.pdf','invoice','accounts_payable','synthetic-control-not-processed','application/pdf',1,%s,'queued') RETURNING id""",
                (self.tenant,owner['id'],uuid4().hex+uuid4().hex)).fetchone()['id'])
            self.job=str(db.execute('INSERT INTO pending_jobs(tenant_id,document_id) VALUES (%s,%s) RETURNING id',(self.tenant,self.document)).fetchone()['id'])
        with attempts.connect() as db:self.delivery=attempts.reserve_dispatch(db,self.document,self.tenant,job_id=self.job)

    def claim(self):return attempts.claim_delivery(self.delivery,self.delivery.dispatch_token)

    def artifact(self):
        raw=json.loads((Path(__file__).parent/'extraction/data/contracts/syn001_m2.json').read_text())
        raw['document_id']=self.document;run='synthetic-pg-control-'+uuid4().hex
        raw['provenance']['service_run_id']=run
        path=Path(self.temp.name)/'synthetic-result.json';path.write_text(json.dumps(raw))
        return {'public_result_path':str(path),'service_run_id':run}

    def counts(self):
        with self.db() as db:
            return {name:db.execute('SELECT count(*) AS n FROM '+name+' WHERE document_id=%s',(self.document,)).fetchone()['n']
                    for name in ('raw_extractions','field_predictions','line_items')}

    def test_concurrent_delivery_claims_once_on_real_postgresql(self):
        def claim(_):
            try:return self.claim()
            except attempts.AttemptRejected:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,range(2)))
        valid=[r for r in results if r is not None]
        self.assertEqual(len(valid),1)
        self.assertTrue(attempts.fail_claim(valid[0]))
        self.assertFalse(attempts.fail_claim(valid[0]))

    def test_persistence_is_atomic_and_completed_replay_is_read_only(self):
        claim=self.claim();artifact=self.artifact()
        first=persistence.persist_extraction_result(self.document,artifact,claim=claim)
        before=self.counts()
        second=persistence.persist_extraction_result(self.document,artifact,claim=claim)
        self.assertTrue(second['idempotent']);self.assertEqual(first['extraction_id'],second['extraction_id'])
        self.assertEqual(before,self.counts())
        with self.db() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM raw_extractions WHERE document_id=%s AND is_current',(self.document,)).fetchone()['n'],1)
            self.assertEqual(db.execute('SELECT status FROM pending_jobs WHERE id=%s',(self.job,)).fetchone()['status'],'succeeded')
            self.assertIsNotNone(db.execute('SELECT quantity FROM line_items WHERE document_id=%s',(self.document,)).fetchone()['quantity'])

    def test_expired_claim_cannot_persist_or_downgrade(self):
        claim=self.claim();artifact=self.artifact()
        with self.db() as db:db.execute("UPDATE pending_jobs SET processing_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(self.job,))
        with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_ATTEMPT'):
            persistence.persist_extraction_result(self.document,artifact,claim=claim)
        self.assertFalse(attempts.fail_claim(claim));self.assertEqual(self.counts()['raw_extractions'],0)

    def test_canonical_insert_failure_rolls_back_every_extraction_write(self):
        claim=self.claim();artifact=self.artifact();original=persistence.map_m2_result
        def invalid(*args):
            result=original(*args);result['fields'][0]['field_label']=None;return result
        with patch.object(persistence,'map_m2_result',side_effect=invalid):
            with self.assertRaises(psycopg.errors.NotNullViolation):
                persistence.persist_extraction_result(self.document,artifact,claim=claim)
        self.assertEqual(self.counts(),{'raw_extractions':0,'field_predictions':0,'line_items':0})
        self.assertTrue(attempts.fail_claim(claim))

    def test_cross_tenant_delivery_is_denied_before_claim(self):
        forged=attempts.Delivery(self.document,self.job,str(uuid4()),self.delivery.dispatch_token)
        with self.assertRaises(attempts.AttemptRejected):attempts.claim_delivery(forged,forged.dispatch_token)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM pending_jobs WHERE id=%s',(self.job,)).fetchone()['status'],'dispatched')

    def test_wrong_rq_identity_is_rejected_without_claim(self):
        with self.assertRaisesRegex(attempts.AttemptRejected,'RQ_DELIVERY_MISMATCH'):
            attempts.claim_delivery(self.delivery,str(uuid4()))
        self.assertEqual(self.counts()['raw_extractions'],0)

    def test_reviewed_state_during_processing_cannot_be_overwritten(self):
        claim=self.claim();artifact=self.artifact()
        with self.db() as db:db.execute("UPDATE documents SET status='corrected' WHERE id=%s",(self.document,))
        with self.assertRaisesRegex(attempts.AttemptRejected,'AMENDMENT_REQUIRED'):
            persistence.persist_extraction_result(self.document,artifact,claim=claim)
        self.assertTrue(attempts.fail_claim(claim))
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM documents WHERE id=%s',(self.document,)).fetchone()['status'],'corrected')
        self.assertEqual(self.counts()['raw_extractions'],0)

    def test_final_lease_fence_rolls_back_inserted_fields_and_current_run(self):
        claim=self.claim();artifact=self.artifact();complete=attempts.complete
        def expire(db,proof,document):
            db.execute("UPDATE pending_jobs SET processing_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(self.job,))
            complete(db,proof,document)
        with patch.object(attempts,'complete',side_effect=expire):
            with self.assertRaisesRegex(attempts.AttemptRejected,'STALE_ATTEMPT'):
                persistence.persist_extraction_result(self.document,artifact,claim=claim)
        self.assertEqual(self.counts(),{'raw_extractions':0,'field_predictions':0,'line_items':0})
        self.assertTrue(attempts.fail_claim(claim))
