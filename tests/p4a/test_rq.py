"""REAL PostgreSQL + REAL Redis/RQ + SYNTHETIC M2. No model inference."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib,json,os,subprocess,sys,threading,time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from redis import Redis
from rq import Queue
from rq.serializers import JSONSerializer
from tests.test_postgresql_contract import PostgreSQLContractTests as Base,api,persistence,attempts
from tests import p4a_support as h
from tests.p4a.worker import source_fingerprint
E=h.RUN/'evidence/gate6b'
FUNCTION='job_queue.jobs.process_document_extraction_job'

def digest(value):
 return hashlib.sha256(json.dumps(value,default=str,sort_keys=True).encode()).hexdigest()

class RQTests(Base):
 def setUp(self):
  self.entries=[];self.children=[];self.owned_documents=[]
  self.addCleanup(self.retain_case)
  super().setUp()
  self.owned_documents.append(self.document)
  self.redis=h.redis_connection()
  self.addCleanup(self.redis.close)
  self.queue=Queue('cpu_tasks',connection=self.redis,serializer=JSONSerializer)
  self.assertEqual(self.queue.job_ids,[])
  self.addCleanup(self.bound_children)
 def retain_case(self):
  (E/(self._testMethodName+'.json')).write_text(json.dumps({'test':self.id(),'tenant':str(getattr(self,'tenant','')),
   'fixture_document':getattr(self,'document',None),'owned_documents':self.owned_documents,
   'real_redis':True,'real_postgresql':True,'extraction':'SYNTHETIC M2','events':self.entries},default=str,indent=2))
 def note(self,event,**values):self.entries.append(dict(event=event,**values))
 def bound_children(self):
  for child in self.children:
   if child['process'].poll() is None:
    child['process'].kill()
    out,_=child['process'].communicate(timeout=10)
    self.note('bounded_cleanup',pid=child['process'].pid,exit_code=child['process'].returncode)
    self.log(child,out)
 def log(self,child,out):
  from tests.p4a.run import redact
  (E/'workers'/(child['record']+'.log')).write_text(redact(out or ''))
 def row(self,job):
  with h.connect_runtime() as db:
   r=db.execute('SELECT * FROM pending_jobs WHERE id=%s',(job,)).fetchone()
  return r
 def job_evidence(self,job):
  r=self.row(job)
  return {**{k:str(r[k]) if r[k] is not None else None for k in ('id','document_id','tenant_id','dispatch_token')},
   **{k:r[k] for k in ('status','dispatch_count','attempt_count','last_error')},
   'claim_present':r['claim_token'] is not None,
   'claim_fingerprint':hashlib.sha256(str(r['claim_token']).encode()).hexdigest() if r['claim_token'] else None}
 def canonical(self,document):
  with h.connect_fixture() as db:
   value={t:db.execute('SELECT * FROM '+t+' WHERE document_id=%s ORDER BY id',(document,)).fetchall()
    for t in ('raw_extractions','field_predictions','line_items','standardised_records','line_item_corrections')}
   value['document']=db.execute('SELECT * FROM documents WHERE id=%s',(document,)).fetchone()
   value['header_history']=db.execute('SELECT fc.* FROM field_corrections fc JOIN field_predictions fp ON fp.id=fc.field_prediction_id WHERE fp.document_id=%s ORDER BY fc.id',(document,)).fetchall()
  return value
 def counts(self,document):
  with h.connect_runtime() as db:
   return db.execute("""SELECT count(*) AS runs,count(*) FILTER (WHERE is_current) AS current,
      array_agg(pending_job_id ORDER BY run_number) AS jobs FROM raw_extractions WHERE document_id=%s""",(document,)).fetchone()
 def upload(self):
  body=b'%PDF-1.4\n% Gate6B synthetic '+uuid4().hex.encode()+b'\n%%EOF'
  response=self.client().post('/api/documents',files={'file':('g6b-'+uuid4().hex+'.pdf',body,'application/pdf')})
  data=response.json();self.assertEqual(response.status_code,201,response.text)
  self.assertTrue(data['dispatchAccepted'])
  self.owned_documents.append(data['id'])
  self.note('upload',http=response.status_code,document=data['id'],job=data['jobId'],rq_job_id=data['queueJobId'],
   before=self.job_evidence(data['jobId']))
  return data
 def payload(self,data):
  job=self.queue.fetch_job(data['queueJobId']);self.assertIsNotNone(job)
  args=tuple(job.args)
  self.assertEqual(args,(data['id'],data['jobId'],str(self.tenant),data['queueJobId']))
  self.assertEqual(job.id,data['queueJobId']);self.assertNotEqual(job.id,data['jobId'])
  self.assertEqual(job.func_name,FUNCTION)
  self.assertEqual((job.result_ttl,job.failure_ttl,job.timeout,job.ttl),(0,0,1800,300))
  self.assertIsNone(job.retries_left)
  self.note('payload',rq_job_id=job.id,args=args,serializer=job.serializer.__name__,result_ttl=job.result_ttl,failure_ttl=job.failure_ttl)
  return args
 def start(self,rqid,mode='success'):
  self.assertEqual(self.queue.job_ids,[rqid])
  record=uuid4().hex;env=os.environ.copy();env.pop('P4A_FIXTURE_OWNER',None)
  env['PYTHONNOUSERSITE']='1';env['PYTHONDONTWRITEBYTECODE']='1'
  command=[sys.executable,'-B','-m','tests.p4a.worker','--mode',mode,'--record',record,
   '--expected-job',rqid,'--source-sha256',source_fingerprint()]
  proc=subprocess.Popen(command,cwd=h.ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
  child={'record':record,'process':proc,'rq_job_id':rqid}
  self.children.append(child);self.note('worker_started',pid=proc.pid,record=record,rq_job_id=rqid,mode=mode)
  return child
 def worker_facts(self,child):return json.loads((E/'workers'/(child['record']+'.json')).read_text())
 def ready(self,child):
  target=E/'workers'/(child['record']+'.ready');deadline=time.monotonic()+35
  while not target.exists():
   if child['process'].poll() is not None:
    self.finish(child);self.fail('Worker exited before synchronization gate')
   if time.monotonic()>deadline:self.fail('Worker gate deadline exceeded')
   threading.Event().wait(.03)
  facts=self.worker_facts(child);self.assertTrue(facts['claim_acquired'])
  self.assertEqual(target.read_text(),child['rq_job_id'])
  return facts
 def release(self,child):(E/'workers'/(child['record']+'.release')).write_text(child['rq_job_id'])
 def finish(self,child,expected_exit=0):
  try:out,_=child['process'].communicate(timeout=55)
  except subprocess.TimeoutExpired:
   child['process'].kill();out,_=child['process'].communicate(timeout=10);self.log(child,out)
   self.fail('Owned worker exceeded bounded execution deadline')
  self.log(child,out)
  self.note('worker_finished',pid=child['process'].pid,exit_code=child['process'].returncode,record=child['record'])
  self.assertEqual(child['process'].returncode,expected_exit)
  facts=self.worker_facts(child)
  self.assertEqual(facts['database_identity']['role'],h.RUNTIME)
  self.assertEqual(facts['database_identity']['db'],h.MAIN)
  self.assertEqual(facts['source_sha256'],source_fingerprint())
  self.assertEqual(facts['python'],sys.executable)
  return facts
 def work(self,data,mode='success'):return self.finish(self.start(data['queueJobId'],mode))
 def successful(self,data,facts):
  r=self.row(data['jobId']);c=self.counts(data['id'])
  self.assertEqual(r['status'],'succeeded');self.assertEqual(r['attempt_count'],1)
  self.assertIsNotNone(r['claim_token']);self.assertEqual(c['current'],1)
  self.assertEqual(str(c['jobs'][-1]),data['jobId'])
  self.assertTrue(facts['claim_acquired'])
  claim=next(e for e in facts['events'] if e['event']=='claimed')
  self.assertEqual(claim['claim_fingerprint'],hashlib.sha256(str(r['claim_token']).encode()).hexdigest())
  self.note('completion',job=self.job_evidence(data['jobId']),extractions=c)
 def enqueue(self,args,rqid):
  self.queue.enqueue_call(FUNCTION,args=tuple(args),job_id=rqid,timeout=1800,ttl=300,result_ttl=0,failure_ttl=0)
  self.note('test_delivery',rq_job_id=rqid,args=args)
 def data_for(self,delivery):return dict(id=delivery.document_id,jobId=delivery.job_id,queueJobId=delivery.dispatch_token)
 def make_source(self):
  p=h.RUN/'storage'/('g6b-source-'+uuid4().hex+'.pdf');p.write_bytes(b'%PDF-1.4\n% synthetic\n%%EOF')
  with h.connect_fixture() as db:db.execute('UPDATE documents SET storage_path=%s WHERE id=%s',(str(p),self.document))
 def baseline_dispatch(self):
  self.make_source();delivery=self.protocol_delivery();data=self.data_for(delivery)
  attempts.publish(delivery);self.note('baseline_dispatch',**data,before=self.job_evidence(data['jobId']))
  return data
 def retry(self,data):
  response=self.client().post('/api/documents/'+data['id']+'/retry')
  self.note('retry',http=response.status_code,document=data['id'])
  return response
 def expire(self,data):
  with h.connect_fixture() as db:
   row=db.execute("UPDATE pending_jobs SET processing_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s AND document_id=%s AND tenant_id=%s AND status='processing' RETURNING id",
    (data['jobId'],data['id'],self.tenant)).fetchone()
   self.assertIsNotNone(row)
  self.note('synthetic_lease_expiry',job=data['jobId'],clock='PostgreSQL',accelerated=True)
 def test_01_upload_dispatch(self):
  data=self.upload();self.payload(data);self.assertEqual(self.row(data['jobId'])['status'],'dispatched')
  self.assertEqual(self.counts(data['id'])['runs'],0)
  facts=self.work(data);self.successful(data,facts)
 def test_02_four_identity_json_payload(self):
  data=self.upload();args=self.payload(data)
  self.assertTrue(all(isinstance(x,str) for x in args))
  self.successful(data,self.work(data))
 def test_03_attempt_specific_claim(self):
  data=self.upload();self.payload(data);child=self.start(data['queueJobId'],'pause-success')
  facts=self.ready(child);r=self.row(data['jobId'])
  self.assertEqual((r['status'],r['attempt_count']),('processing',1))
  self.assertAlmostEqual((r['processing_expires_at']-r['claimed_at']).total_seconds(),1800,delta=.1)
  self.assertAlmostEqual((r['dispatch_expires_at']-r['dispatched_at']).total_seconds(),300,delta=.1)
  self.note('claim_live',job=self.job_evidence(data['jobId']),worker_pid=facts['pid'])
  self.release(child);self.successful(data,self.finish(child))
 def test_04_transactional_success_history(self):
  data=self.baseline_dispatch();before=self.canonical(self.document)
  facts=self.work(data);self.successful(data,facts)
  after=self.canonical(self.document)
  self.assertEqual(self.counts(self.document)['runs'],2)
  old=before['raw_extractions'][0];old_after=next(x for x in after['raw_extractions'] if x['id']==old['id'])
  self.assertEqual(old['raw_json'],old_after['raw_json']);self.assertFalse(old_after['is_current'])
  self.assertEqual(after['document']['content_revision'],before['document']['content_revision']+1)
  with h.connect_runtime() as db:
   events=db.execute("SELECT action FROM audit_logs WHERE resource=%s AND details=%s ORDER BY created_at",(self.document,data['jobId'])).fetchall()
  self.assertIn('extraction.succeeded',[x['action'] for x in events])
  self.note('history_preserved',old_run=str(old['id']),runs=2,current=1,revision=after['document']['content_revision'])
 def test_05_worker_failure_preserves_history(self):
  data=self.baseline_dispatch();before=self.canonical(self.document)
  facts=self.work(data,'failure');r=self.row(data['jobId']);after=self.canonical(self.document)
  self.assertTrue(facts['claim_acquired']);self.assertEqual(r['status'],'failed')
  self.assertEqual(r['last_error'],'EXTRACTION_FAILED')
  self.assertEqual(after['raw_extractions'],before['raw_extractions'])
  self.assertEqual(after['field_predictions'],before['field_predictions'])
  self.assertEqual(after['document']['content_revision'],before['document']['content_revision'])
  self.assertEqual(after['document']['status'],'failed')
  self.assertIn({'event':'failure_update','accepted':True},facts['events'])
  self.note('failure',job=self.job_evidence(data['jobId']),history_digest=digest(after['raw_extractions']))
 def test_06_duplicate_delivery(self):
  data=self.upload();args=self.payload(data);self.successful(data,self.work(data))
  before=self.canonical(data['id']);job_before=self.row(data['jobId'])
  self.enqueue(args,data['queueJobId']);facts=self.work(data)
  self.assertFalse(facts['claim_acquired']);self.assertIn({'event':'claim_rejected','reason':'STALE_DELIVERY'},facts['events'])
  self.assertEqual(self.canonical(data['id']),before);self.assertEqual(self.row(data['jobId']),job_before)
  self.assertEqual(self.counts(data['id'])['runs'],1)
  self.note('duplicate_unchanged',canonical_digest=digest(before),job=self.job_evidence(data['jobId']))
 def stale(self,mode):
  old=self.upload();child=self.start(old['queueJobId'],mode);self.ready(child);self.expire(old)
  response=self.retry(old);self.assertEqual(response.status_code,200,response.text);fresh=response.json()
  self.successful(fresh,self.work(fresh))
  before=self.canonical(old['id']);new_before=self.row(fresh['jobId']);old_before=self.row(old['jobId'])
  self.release(child);facts=self.finish(child)
  self.assertEqual(self.canonical(old['id']),before);self.assertEqual(self.row(fresh['jobId']),new_before)
  self.assertEqual(self.row(old['jobId']),old_before);self.assertEqual(old_before['last_error'],'LEASE_EXPIRED')
  self.assertIn({'event':'failure_update','accepted':False},facts['events'])
  if mode=='pause-success':self.assertIn({'event':'persistence_rejected','reason':'STALE_ATTEMPT'},facts['events'])
  self.note('late_result_fenced',mode=mode,old=self.job_evidence(old['jobId']),new=self.job_evidence(fresh['jobId']),
   unchanged_digest=digest(before))
 def test_07_stale_success(self):self.stale('pause-success')
 def test_08_stale_failure(self):self.stale('pause-failure')
 def test_09_foreign_identities(self):
  self.make_source()
  for which in ('tenant','document','job','dispatch'):
   delivery=self.protocol_delivery();data=self.data_for(delivery)
   args=[delivery.document_id,delivery.job_id,delivery.tenant_id,delivery.dispatch_token]
   args[{'document':0,'job':1,'tenant':2,'dispatch':3}[which]]=str(self.other_tenant if which=='tenant' else uuid4())
   before=self.canonical(self.document);jbefore=self.row(delivery.job_id)
   self.enqueue(args,delivery.dispatch_token);facts=self.work(data)
   self.assertFalse(facts['claim_acquired'])
   self.assertFalse(any(x['event']=='synthetic_parser_entered' for x in facts['events']))
   self.assertEqual(self.canonical(self.document),before);self.assertEqual(self.row(delivery.job_id),jbefore)
   self.note('foreign_rejected',dimension=which,unchanged_digest=digest(before),job=self.job_evidence(delivery.job_id))
   self.enqueue((delivery.document_id,delivery.job_id,delivery.tenant_id,delivery.dispatch_token),delivery.dispatch_token)
   self.successful(data,self.work(data))
 def test_10_terminal_failure_retry_and_open_rejection(self):
  old=self.upload();self.work(old,'failure');old_before=self.row(old['jobId'])
  barrier=threading.Barrier(2)
  def request(_):
   c=self.client();barrier.wait(timeout=8);return c.post('/api/documents/'+old['id']+'/retry')
  with ThreadPoolExecutor(2) as pool:responses=list(pool.map(request,range(2)))
  self.assertEqual(sorted(r.status_code for r in responses),[200,409])
  fresh=next(r.json() for r in responses if r.status_code==200)
  self.assertNotEqual(fresh['jobId'],old['jobId']);self.assertNotEqual(fresh['queueJobId'],old['queueJobId'])
  self.assertEqual(self.queue.job_ids,[fresh['queueJobId']])
  self.assertEqual(self.retry(old).status_code,409)
  self.assertEqual(self.row(old['jobId']),old_before)
  self.successful(fresh,self.work(fresh))
  with h.connect_runtime() as db:
   n=db.execute('SELECT count(*) AS n FROM pending_jobs WHERE document_id=%s',(old['id'],)).fetchone()['n']
  self.assertEqual(n,2)
  self.note('retry_history',http=[r.status_code for r in responses],jobs=2,old=self.job_evidence(old['jobId']),new=self.job_evidence(fresh['jobId']))
 def test_11_dispatch_uncertainty(self):
  import job_queue.config as config
  bad=Redis(host='127.0.0.1',port=6383,password='deliberately-wrong-synthetic',socket_timeout=4,socket_connect_timeout=4)
  try:
   with patch.object(config,'get_redis_connection',return_value=bad):
    response=self.client().post('/api/documents',files={'file':('bad-auth-'+uuid4().hex+'.pdf',b'%PDF-1.4 '+uuid4().hex.encode(),'application/pdf')})
  finally:bad.close()
  self.assertEqual(response.status_code,503,response.text)
  detail=response.json()['detail'];self.assertFalse(detail['dispatchAccepted'])
  self.owned_documents.append(detail['documentId'])
  self.assertEqual(self.row(detail['jobId'])['status'],'failed');self.assertEqual(self.queue.job_ids,[])
  self.note('dispatch_auth_failure',http=503,job=self.job_evidence(detail['jobId']))
  original=Queue.enqueue_call
  for already_claimed in (False,True):
   queued={};child_holder=[]
   def accepted_then_error(q,*args,**kw):
    job=original(q,*args,**kw);queued.update(id=kw['args'][0],jobId=kw['args'][1],queueJobId=job.id)
    if already_claimed:
     child=self.start(job.id,'pause-success');child_holder.append(child);self.ready(child)
    raise ConnectionError('Controlled client error after actual Redis acknowledgement')
   with patch.object(Queue,'enqueue_call',accepted_then_error):
    response=self.client().post('/api/documents',files={'file':('ambiguous-'+uuid4().hex+'.pdf',b'%PDF-1.4 '+uuid4().hex.encode(),'application/pdf')})
   self.assertEqual(response.status_code,503,response.text);self.assertFalse(response.json()['detail']['dispatchAccepted'])
   self.owned_documents.append(queued['id'])
   if already_claimed:
    self.assertEqual(self.row(queued['jobId'])['status'],'processing')
    self.release(child_holder[0]);self.successful(queued,self.finish(child_holder[0]))
   else:
    self.assertEqual(self.row(queued['jobId'])['status'],'failed')
    facts=self.work(queued);self.assertFalse(facts['claim_acquired']);self.assertEqual(self.counts(queued['id'])['runs'],0)
   self.note('accepted_then_client_error',http=503,already_claimed=already_claimed,job=self.job_evidence(queued['jobId']),fault_injected=True)
 def test_12_legacy_payload(self):
  before=self.canonical(self.document);jbefore=self.protocol_snapshot()['pending_jobs']
  rqid=str(uuid4());self.enqueue((self.document,),rqid)
  facts=self.finish(self.start(rqid))
  self.assertFalse(facts['claim_acquired']);self.assertIn({'event':'task_return','status':'REJECTED','reason':'LEGACY_PAYLOAD'},facts['events'])
  self.assertEqual(self.canonical(self.document),before);self.assertEqual(self.protocol_snapshot()['pending_jobs'],jbefore)
  self.note('legacy_rejected',rq_job_id=rqid,unchanged_digest=digest(before))
 def test_13_interruption_manual_recovery(self):
  old=self.upload();child=self.start(old['queueJobId'],'pause-success');self.ready(child)
  before=self.canonical(old['id']);job_before=self.row(old['jobId'])
  self.assertEqual(job_before['status'],'processing');self.assertEqual(self.counts(old['id'])['runs'],0)
  child['process'].kill();self.finish(child,expected_exit=-9)
  self.assertEqual(self.canonical(old['id']),before);self.assertEqual(self.row(old['jobId']),job_before)
  self.assertEqual(self.retry(old).status_code,409)
  self.expire(old);response=self.retry(old);self.assertEqual(response.status_code,200,response.text)
  fresh=response.json();self.successful(fresh,self.work(fresh))
  self.assertEqual(self.row(old['jobId'])['last_error'],'LEASE_EXPIRED')
  self.note('manual_recovery',terminated_pid=child['process'].pid,old=self.job_evidence(old['jobId']),new=self.job_evidence(fresh['jobId']),automatic_recovery=False)
 def fresh_baseline(self):
  self.document,self.result=self.document_fixture();self.owned_documents.append(self.document)
  # Independent synthetic invoices avoid triggering legitimate duplicate-invoice validation.
  self.result['fields']['invoice_number']['value']='G6B-'+uuid4().hex
  self.persist()
 def protected_snapshot(self):
  value=self.canonical(self.document);return value
 def test_14_reviewed_document_protection(self):
  for protection in ('corrected-before-claim','corrected-after-claim','approved','standardised'):
   if protection!='corrected-before-claim':self.fresh_baseline()
   if protection in ('approved','standardised'):
    self.make_source();self.review()
    if protection=='standardised':self.assertEqual(self.standardise().status_code,201)
    token=str(uuid4())
    with h.connect_fixture() as db:
     row=db.execute("INSERT INTO pending_jobs(document_id,tenant_id,status,dispatch_token,dispatch_count,dispatched_at,dispatch_expires_at) VALUES (%s,%s,'dispatched',%s,1,clock_timestamp(),clock_timestamp()+interval '300 seconds') RETURNING id",(self.document,self.tenant,token)).fetchone()
    data=dict(id=self.document,jobId=str(row['id']),queueJobId=token)
    self.enqueue((self.document,data['jobId'],str(self.tenant),token),token)
    self.note('synthetic_protected_reservation',policy=protection,job=data['jobId'])
   else:data=self.baseline_dispatch()
   child=None
   if protection=='corrected-after-claim':
    child=self.start(data['queueJobId'],'pause-success');self.ready(child)
   if protection.startswith('corrected'):
    self.assertEqual(self.correct().status_code,200)
   before=self.protected_snapshot()
   self.assertEqual(self.retry(data).status_code,409)
   if child:
    self.release(child);facts=self.finish(child)
   else:facts=self.work(data)
   self.assertEqual(self.protected_snapshot(),before)
   self.assertEqual(self.row(data['jobId'])['status'],'cancelled')
   self.assertEqual(facts['claim_acquired'],child is not None)
   self.note('protected',policy=protection,job=self.job_evidence(data['jobId']),unchanged_digest=digest(before))
 def test_15_restricted_json_worker(self):
  data=self.upload();self.payload(data);facts=self.work(data);self.successful(data,facts)
  self.assertEqual((facts['serializer'],facts['scheduler'],facts['max_jobs']),('JSONSerializer',False,1))
  with h.connect_runtime() as db:
   role=db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
  self.assertFalse(any(role.values()))
  self.note('least_privilege',role=h.RUNTIME,privilege_flags=role,automatic_retry=False,scheduler=False)
 def test_16_metadata_minimization(self):
  data=self.upload();self.payload(data);facts=self.work(data);self.successful(data,facts)
  with h.connect_runtime() as db:
   raw=db.execute('SELECT raw_json,service_run_id FROM raw_extractions WHERE document_id=%s AND is_current',(data['id'],)).fetchone()
  self.assertEqual(raw['raw_json']['document_id'],data['id'])
  self.assertEqual(raw['raw_json']['provenance']['service_run_id'],raw['service_run_id'])
  self.assertFalse(self.redis.exists('rq:job:'+data['queueJobId']))
  self.assertFalse(list(self.redis.scan_iter(match='rq:results:'+data['queueJobId']+'*')))
  text=json.dumps(facts)
  for secret in (h.secret('redis-password'),h.secret('pg-runtime-password'),str(self.row(data['jobId'])['claim_token'])):
   self.assertNotIn(secret,text)
  self.assertNotIn('Example Stationery',text);self.assertNotIn('public_result_path',text)
  self.note('minimization',rq_job_hash_absent=True,financial_values_in_redis_results=False,
   service_run_id=raw['service_run_id'],claim_fingerprint_only=True)

def load_tests(loader,tests,pattern):
 from unittest import TestSuite
 return TestSuite(RQTests(name) for name in RQTests.__dict__ if name.startswith('test_'))
