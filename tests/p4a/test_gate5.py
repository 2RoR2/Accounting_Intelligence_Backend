"""Real PG Gate 5 extensions. Synthetic fixtures retained; no Redis or schema setup."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib,json,threading,time
from pathlib import Path
from unittest.mock import patch,Mock
from uuid import uuid4
import psycopg
from tests.test_postgresql_contract import PostgreSQLContractTests as Base,api,persistence,attempts
from tests import p4a_support as h

class Gate5Tests(Base):
    def evidence(self,**values):
        p=h.RUN/'evidence/phase4ar'/('extra-'+self._testMethodName+'.json')
        values.update(document=self.document,tenant=str(self.tenant))
        p.write_text(json.dumps(values,default=str,indent=2))

    @staticmethod
    def wait_blocked(observer,blocker,minimum=1):
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            observer.execute('SELECT pg_stat_clear_snapshot()')
            rows=observer.execute("""SELECT pid,pg_blocking_pids(pid) AS blockers
                FROM pg_stat_activity WHERE datname=current_database()
                AND wait_event_type='Lock' AND pid<>pg_backend_pid()""").fetchall()
            # No sleeps prove correctness: actual PostgreSQL blocker relationships do.
            relevant=[r for r in rows if blocker in r['blockers']]
            chain={r['pid'] for r in relevant}
            relevant += [r for r in rows if r not in relevant and chain.intersection(r['blockers'])]
            if len(relevant)>=minimum:return relevant
            threading.Event().wait(0.02)
        raise AssertionError('Expected independently observed PostgreSQL lock contention was absent')

    @contextmanager
    def paused_document_writer(self,module):
        held=threading.Event();release=threading.Event();pids=[]
        class Proxy:
            def __init__(self):self.db=h.connect_runtime();self.paused=False
            def __enter__(self):self.db.__enter__();return self
            def __exit__(self,*a):return self.db.__exit__(*a)
            def execute(self,q,*a,**kw):
                cursor=self.db.execute(q,*a,**kw)
                if not self.paused and isinstance(q,str) and 'FOR UPDATE' in q.upper() and 'documents' in q:
                    self.paused=True;pids.append(self.db.info.backend_pid);held.set()
                    if not release.wait(12):raise AssertionError('Controller did not release lock owner')
                return cursor
        with patch.object(module,'connect',Proxy):
            try:yield held,release,pids
            finally:release.set()

    def test_noncurrent_insert_and_visibility_before_switch(self):
        claim=self.protocol_start();artifact=self.protocol_artifact()
        observations=[]
        class Proxy:
            def __init__(self):self.db=h.connect_runtime()
            def __enter__(self):self.db.__enter__();return self
            def __exit__(self,*a):return self.db.__exit__(*a)
            def execute(proxy,q,params=()):
                if isinstance(q,str) and 'SELECT switch_current_extraction' in q:
                    run=proxy.db.execute('SELECT is_current FROM raw_extractions WHERE id=%s',params).fetchone()
                    fields=proxy.db.execute('SELECT count(*) AS n FROM field_predictions WHERE extraction_id=%s',params).fetchone()['n']
                    lines=proxy.db.execute('SELECT count(*) AS n FROM line_items WHERE extraction_id=%s',params).fetchone()['n']
                    with h.connect_fixture() as observer:
                        invisible=observer.execute('SELECT count(*) AS n FROM raw_extractions WHERE id=%s',params).fetchone()['n']
                        old=observer.execute('SELECT count(*) AS n FROM raw_extractions WHERE document_id=%s AND is_current',(self.document,)).fetchone()['n']
                    self.assertFalse(run['is_current']);self.assertGreater(fields,0);self.assertGreater(lines,0)
                    self.assertEqual((invisible,old),(0,1))
                    observations.append(dict(insert_is_current=run['is_current'],fields=fields,lines=lines,observer_new=invisible,observer_current=old))
                return proxy.db.execute(q,params)
        with patch.object(persistence,'connect',Proxy):
            first=persistence.persist_extraction_result(self.document,artifact,claim=claim)
        replay=persistence.persist_extraction_result(self.document,artifact,claim=claim)
        self.assertTrue(replay['idempotent']);self.assertEqual(replay['extraction_id'],first['extraction_id'])
        with h.connect_fixture() as db:
            rows=db.execute('SELECT is_current FROM raw_extractions WHERE document_id=%s ORDER BY run_number',(self.document,)).fetchall()
        self.assertEqual([r['is_current'] for r in rows],[False,True])
        self.evidence(observations=observations,replay_idempotent=True,current_history=[False,True])

    def test_real_unique_fk_and_switch_function_constraints(self):
        states=[]
        with h.connect_fixture() as db:
            db.execute('INSERT INTO pending_jobs(document_id,tenant_id) VALUES (%s,%s)',(self.document,self.tenant))
            for query,params,state in [
                ('INSERT INTO pending_jobs(document_id,tenant_id) VALUES (%s,%s)',(self.document,self.tenant),'23505'),
                ('INSERT INTO pending_jobs(document_id,tenant_id,status,finished_at) VALUES (%s,%s,%s,now())',(self.document,self.other_tenant,'failed'),'23503')]:
                try:
                    with db.transaction():db.execute(query,params)
                except psycopg.Error as e:self.assertEqual(e.sqlstate,state);states.append(e.sqlstate)
                else:self.fail('Constraint accepted invalid synthetic row')
            new=db.execute("""INSERT INTO raw_extractions(document_id,run_number,routing_decision,raw_json)
                VALUES (%s,2,'parser_only','{}'::jsonb) RETURNING id,is_current""",(self.document,)).fetchone()
            self.assertFalse(new['is_current'])
            try:
                with db.transaction():db.execute('UPDATE raw_extractions SET is_current=TRUE WHERE id=%s',(new['id'],))
            except psycopg.Error as e:self.assertEqual(e.sqlstate,'23505');states.append(e.sqlstate)
            else:self.fail('Two current extractions were allowed')
            self.assertEqual(db.execute('SELECT switch_current_extraction(%s) AS n',(new['id'],)).fetchone()['n'],0)
            self.assertEqual(db.execute('SELECT id FROM raw_extractions WHERE document_id=%s AND is_current',(self.document,)).fetchone()['id'],new['id'])
            db.rollback()
        with h.connect_fixture() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM raw_extractions WHERE document_id=%s',(self.document,)).fetchone()['n'],1)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM pending_jobs WHERE document_id=%s AND status='pending'",(self.document,)).fetchone()['n'],0)
        self.evidence(sqlstates=states,explicit_rollback_restored_baseline=True)

    def test_real_current_record_unique_constraint(self):
        self.review();self.assertEqual(self.standardise().status_code,201)
        with h.connect_fixture() as db:
            try:
                with db.transaction():
                    db.execute("""INSERT INTO standardised_records(document_id,tenant_id,revision_number,category,snapshot,created_by)
                    VALUES (%s,%s,2,'accounts_payable','{}'::jsonb,%s)""",(self.document,self.tenant,self.owner['id']))
            except psycopg.Error as e:self.assertEqual(e.sqlstate,'23505');state=e.sqlstate
            else:self.fail('Two current canonical records were accepted')
        self.evidence(sqlstate=state,record_unchanged=True)

    def test_two_corrections_have_observed_independent_lock_waiters(self):
        clients=[self.client(),self.client(self.assignee)]
        with h.connect_fixture() as holder,h.connect_fixture() as observer:
            holder.execute('SELECT id FROM documents WHERE id=%s FOR UPDATE',(self.document,))
            pid=holder.info.backend_pid
            with ThreadPoolExecutor(2) as pool:
                futures=[pool.submit(self.correct,'CONTROLLED-'+str(i),client=c) for i,c in enumerate(clients)]
                try:waiters=self.wait_blocked(observer,pid,2)
                finally:holder.commit()
                statuses=sorted(f.result(timeout=20).status_code for f in futures)
        self.assertEqual(statuses,[200,409]);self.assertEqual(self.state()['content_revision'],3)
        self.assertEqual(len(self.history()),1)
        self.evidence(blocker_pid=pid,waiters=waiters,http_statuses=statuses,revision=3,history_rows=1)

    def test_two_completions_have_one_commit_and_idempotent_replay(self):
        claim=self.protocol_start();artifact=self.protocol_artifact()
        with h.connect_fixture() as holder,h.connect_fixture() as observer:
            holder.execute('SELECT id FROM documents WHERE id=%s FOR UPDATE',(self.document,))
            pid=holder.info.backend_pid
            with ThreadPoolExecutor(2) as pool:
                futures=[pool.submit(persistence.persist_extraction_result,self.document,artifact,claim=claim) for _ in range(2)]
                try:waiters=self.wait_blocked(observer,pid,2)
                finally:holder.commit()
                results=[f.result(timeout=20) for f in futures]
        self.assertEqual(sorted(r['idempotent'] for r in results),[False,True])
        self.assertEqual(results[0]['extraction_id'],results[1]['extraction_id'])
        self.evidence(blocker_pid=pid,waiters=waiters,idempotent=[r['idempotent'] for r in results])

    def test_correction_first_blocks_extraction_and_retains_reviewed_values(self):
        claim=self.protocol_start();artifact=self.protocol_artifact();client=self.client()
        with h.connect_fixture() as observer,self.paused_document_writer(api) as (held,release,pids):
            with ThreadPoolExecutor(2) as pool:
                correction=pool.submit(self.correct,'CONTROLLED-REVIEW',client=client)
                try:
                    self.assertTrue(held.wait(8))
                    extraction=pool.submit(persistence.persist_extraction_result,self.document,artifact,claim=claim)
                    waiters=self.wait_blocked(observer,pids[0])
                finally:release.set()
                self.assertEqual(correction.result(timeout=20).status_code,200)
                with self.assertRaisesRegex(ValueError,'AMENDMENT_REQUIRED'):extraction.result(timeout=20)
        self.assertEqual(self.field()['current_value'],'CONTROLLED-REVIEW')
        self.assertEqual(self.state()['content_revision'],3)
        self.evidence(lock_owner_pid=pids[0],waiters=waiters,correction_http=200,extraction='AMENDMENT_REQUIRED',revision=3)

    def test_extraction_first_blocks_correction_and_rejects_stale_revision(self):
        claim=self.protocol_start();artifact=self.protocol_artifact();client=self.client()
        with h.connect_fixture() as observer,self.paused_document_writer(persistence) as (held,release,pids):
            with ThreadPoolExecutor(2) as pool:
                extraction=pool.submit(persistence.persist_extraction_result,self.document,artifact,claim=claim)
                try:
                    self.assertTrue(held.wait(8))
                    correction=pool.submit(self.correct,'STALE-CONTROLLED',client=client)
                    waiters=self.wait_blocked(observer,pids[0])
                finally:release.set()
                self.assertFalse(extraction.result(timeout=20)['idempotent'])
                self.assertEqual(correction.result(timeout=20).status_code,409)
        self.assertEqual(self.history(),[]);self.assertEqual(self.state()['content_revision'],3)
        self.evidence(lock_owner_pid=pids[0],waiters=waiters,correction_http=409,history_rows=0,revision=3)

    def test_duplicate_upload_race_rechecks_the_same_hash(self):
        clients=[self.client(),self.client(self.assignee)]
        held=threading.Event();release=threading.Event();owners=[];observations=[]
        body=b'%PDF-1.4\\n% Gate5 synthetic duplicate '+uuid4().hex.encode()+b'\\n%%EOF'
        digest=hashlib.sha256(body).hexdigest()
        class Cursor:
            def __init__(self,cursor,pid):self.cursor=cursor;self.pid=pid
            def fetchone(proxy):
                row=proxy.cursor.fetchone()
                observations.append({'pid':proxy.pid,'duplicate_found':row is not None})
                return row
        class Proxy:
            def __init__(self):self.db=h.connect_runtime()
            def __enter__(self):self.db.__enter__();return self
            def __exit__(self,*a):return self.db.__exit__(*a)
            def execute(self,q,*a,**kw):
                cursor=self.db.execute(q,*a,**kw)
                if isinstance(q,str) and 'pg_advisory_xact_lock(' in q and not owners:
                    owners.append(self.db.info.backend_pid);held.set()
                    if not release.wait(12):raise AssertionError('Controller did not release upload lock')
                if isinstance(q,str) and 'SELECT id,document_code,original_filename FROM documents WHERE tenant_id=' in q:
                    return Cursor(cursor,self.db.info.backend_pid)
                return cursor
        cpu=Mock()
        def upload(i):return clients[i].post('/api/documents',files={'file':('gate5-synthetic.pdf',body,'application/pdf')})
        with h.connect_fixture() as observer,patch.object(api,'connect',Proxy),patch('infrastructure.queue.config.get_queues',return_value=(cpu,None)):
            with ThreadPoolExecutor(2) as pool:
                first=pool.submit(upload,0)
                try:
                    self.assertTrue(held.wait(8))
                    second=pool.submit(upload,1)
                    waiters=self.wait_blocked(observer,owners[0])
                finally:release.set()
                responses=[first.result(timeout=20),second.result(timeout=20)]
        with h.connect_fixture() as db:
            docs=db.execute('SELECT id FROM documents WHERE tenant_id=%s AND file_hash_sha256=%s',(self.tenant,digest)).fetchall()
            jobs=db.execute('SELECT id,document_id,status FROM pending_jobs WHERE tenant_id=%s AND document_id=ANY(%s)',(self.tenant,[r['id'] for r in docs])).fetchall()
        statuses=sorted(r.status_code for r in responses)
        self.evidence(http_statuses=statuses,lock_owner_pid=owners[0],waiters=waiters,
                      duplicate_read_observations=observations,documents=docs,jobs=jobs,
                      enqueue_calls=cpu.enqueue_call.call_count,synthetic_bytes=True,queue_mocked=True)
        self.assertEqual(len({r['pid'] for r in observations}),2)
        self.assertEqual(sorted(r['duplicate_found'] for r in observations),[False,True])
        self.assertEqual(len(docs),1,'Identical simultaneous uploads must not create duplicate documents')
        self.assertEqual(len(jobs),1)
        self.assertEqual(cpu.enqueue_call.call_count,1)
        self.assertEqual(statuses,[201,409])

    def test_local_admin_edit_is_tenant_scoped(self):
        self.assertEqual(self.correct(user=self.admin).status_code,200)
        before=self.field(),self.state(),self.history()
        self.assertEqual(self.correct('FOREIGN',revision=3,user=self.foreign).status_code,404)
        self.assertEqual((self.field(),self.state(),self.history()),before)
        self.evidence(local_admin_http=200,foreign_admin_http=404,revision=3)

    def test_new_classification_and_validation_load_new_current_run(self):
        from validation.document_validation import load_current
        value=deepcopy(self.result);value['provenance']['service_run_id']='gate5-'+uuid4().hex
        value['document_type']['value']='receipt'
        value['fields']['total_amount']['value']='60.00'
        self.queue_new_job();saved=self.persist(value)
        with h.connect_runtime() as db:
            doc,raw,fields,items=load_current(db,self.document)
            self.assertEqual(doc['kind'],'receipt')
            self.assertEqual(str(raw['id']),saved['extraction_id'])
            self.assertTrue(all(f['extraction_id']==raw['id'] for f in fields))
            self.assertTrue(all(item['extraction_id']==raw['id'] for item in items))
            self.assertEqual(next(f['current_value'] for f in fields if f['field_key']=='total'),'60.00')
        response=self.client().get(f'/api/documents/{self.document}/validation')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['contentRevision'],3)
        self.assertTrue(response.json()['blocking'])
        self.evidence(kind='receipt',current_extraction=saved['extraction_id'],validation_http=200,blocking=True,revision=3)

    def test_retry_completion_has_observed_database_lock_waiters(self):
        claim=self.protocol_start();artifact=self.protocol_artifact();client=self.client()
        with h.connect_fixture() as holder,h.connect_fixture() as observer:
            holder.execute('SELECT id FROM documents WHERE id=%s FOR UPDATE',(self.document,))
            pid=holder.info.backend_pid
            with patch('infrastructure.queue.config.get_queues',side_effect=AssertionError('No Redis dispatch for active work')):
                with ThreadPoolExecutor(2) as pool:
                    finished=pool.submit(persistence.persist_extraction_result,self.document,artifact,claim=claim)
                    retry=pool.submit(client.post,f'/api/documents/{self.document}/retry')
                    try:waiters=self.wait_blocked(observer,pid,2)
                    finally:holder.commit()
                    self.assertFalse(finished.result(timeout=20)['idempotent'])
                    self.assertEqual(retry.result(timeout=20).status_code,409)
        with h.connect_fixture() as db:
            self.assertEqual(db.execute('SELECT status FROM pending_jobs WHERE id=%s',(claim.delivery.job_id,)).fetchone()['status'],'succeeded')
        self.evidence(blocker_pid=pid,waiters=waiters,retry_http=409,job_status='succeeded')

    def test_identical_uploads_in_two_tenants_remain_independent(self):
        body=b'%PDF-1.4\\n% cross-tenant synthetic '+uuid4().hex.encode()+b'\\n%%EOF'
        clients=[self.client(),self.client(self.foreign)]
        cpu=Mock()
        with patch('infrastructure.queue.config.get_queues',return_value=(cpu,None)):
            responses=[c.post('/api/documents',files={'file':('tenant-test.pdf',body,'application/pdf')}) for c in clients]
        self.assertEqual([r.status_code for r in responses],[201,201])
        ids=[r.json()['id'] for r in responses]
        self.assertEqual(len(set(ids)),2)
        self.assertEqual({r.json()['tenantId'] for r in responses},{str(self.tenant),str(self.other_tenant)})
        with h.connect_fixture() as db:
            jobs=db.execute('SELECT document_id,count(*) AS n FROM pending_jobs WHERE document_id=ANY(%s::uuid[]) GROUP BY document_id',(ids,)).fetchall()
        self.assertEqual(len(jobs),2)
        self.assertTrue(all(r['n']==1 for r in jobs))
        self.assertEqual(cpu.enqueue_call.call_count,2)
        self.evidence(http_statuses=[201,201],documents=ids,jobs=jobs,queue_mocked=True)

    def test_upload_rollback_releases_lock_and_waiter_can_commit(self):
        clients=[self.client(),self.client(self.assignee)]
        held=threading.Event();release=threading.Event();owners=[];rejected=[]
        filename='rollback-'+uuid4().hex+'.pdf'
        body=b'%PDF-1.4\n% synthetic rollback '+uuid4().hex.encode()+b'\n%%EOF'
        digest=hashlib.sha256(body).hexdigest()
        class Proxy:
            def __init__(self):self.db=h.connect_runtime()
            def __enter__(self):self.db.__enter__();return self
            def __exit__(self,*a):return self.db.__exit__(*a)
            def execute(self,q,*a,**kw):
                cursor=self.db.execute(q,*a,**kw)
                if isinstance(q,str) and 'pg_advisory_xact_lock(' in q and not owners:
                    owners.append(self.db.info.backend_pid);held.set()
                    if not release.wait(12):raise AssertionError('Upload controller did not release holder')
                return cursor
        original=attempts.reserve_dispatch
        def fail_first(db,document,tenant,**kwargs):
            if db.db.info.backend_pid==owners[0]:
                rejected.append(document)
                raise RuntimeError('synthetic precommit reservation failure')
            return original(db,document,tenant,**kwargs)
        cpu=Mock()
        def upload(i):return clients[i].post('/api/documents',files={'file':(filename,body,'application/pdf')})
        with h.connect_fixture() as observer,patch.object(api,'connect',Proxy),patch.object(
                attempts,'reserve_dispatch',side_effect=fail_first),patch(
                'infrastructure.queue.config.get_queues',return_value=(cpu,None)):
            with ThreadPoolExecutor(2) as pool:
                first=pool.submit(upload,0)
                try:
                    self.assertTrue(held.wait(8))
                    first_files=list((h.RUN/'storage').glob('*'+filename))
                    self.assertEqual(len(first_files),1)
                    second=pool.submit(upload,1)
                    waiters=self.wait_blocked(observer,owners[0])
                finally:release.set()
                responses=[first.result(timeout=20),second.result(timeout=20)]
        self.assertEqual([r.status_code for r in responses],[500,201])
        self.assertEqual(len(rejected),1)
        with h.connect_fixture() as db:
            docs=db.execute('SELECT id FROM documents WHERE tenant_id=%s AND file_hash_sha256=%s',(self.tenant,digest)).fetchall()
            self.assertEqual(len(docs),1)
            jobs=db.execute("""SELECT id,status,dispatch_count,attempt_count,
                dispatch_token IS NOT NULL AS reserved FROM pending_jobs WHERE document_id=%s""",(docs[0]['id'],)).fetchall()
            rejected_docs=db.execute('SELECT count(*) AS n FROM documents WHERE id=%s',(rejected[0],)).fetchone()['n']
            rejected_jobs=db.execute('SELECT count(*) AS n FROM pending_jobs WHERE document_id=%s',(rejected[0],)).fetchone()['n']
        self.assertEqual(len(jobs),1)
        self.assertEqual((jobs[0]['status'],jobs[0]['dispatch_count'],jobs[0]['attempt_count'],jobs[0]['reserved']),('dispatched',1,0,True))
        self.assertEqual((rejected_docs,rejected_jobs),(0,0))
        self.assertFalse(first_files[0].exists())
        self.assertEqual(len(list((h.RUN/'storage').glob('*'+filename))),1)
        cpu.enqueue_call.assert_called_once()
        self.evidence(http_statuses=[500,201],injected_fault='precommit reservation failure',
                      lock_owner_pid=owners[0],waiters=waiters,documents=docs,jobs=jobs,
                      rejected_documents=0,rejected_jobs=0,rejected_source_removed=True,queue_mocked=True)


def load_tests(loader,tests,pattern):
    # Discovery must not rerun all inherited baseline cases or the imported Base.
    from unittest import TestSuite
    return TestSuite(Gate5Tests(name) for name in Gate5Tests.__dict__ if name.startswith('test_'))