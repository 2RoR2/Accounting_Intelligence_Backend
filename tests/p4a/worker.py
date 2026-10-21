"""Gate 6B real SimpleWorker subprocess; synthetic extraction boundary only."""
import argparse,hashlib,json,os,re,sys,time
from pathlib import Path
from uuid import uuid4
from tests import p4a_support as h

SOURCE_FILES=('app.py','infrastructure/queue/config.py','infrastructure/queue/attempts.py',
 'infrastructure/queue/jobs.py','services/extraction/persistence.py','tests/p4a_support.py',
 'tests/p4a/worker.py','services/extraction/execution.py')
def source_fingerprint():
 d=hashlib.sha256()
 for name in SOURCE_FILES:d.update(name.encode());d.update((h.ROOT/name).read_bytes().replace(b'\r\n',b'\n'))
 return d.hexdigest()
def main():
 p=argparse.ArgumentParser()
 p.add_argument('--mode',choices=['success','failure','pause-success','pause-failure'],default='success')
 p.add_argument('--record',required=True);p.add_argument('--expected-job',required=True)
 p.add_argument('--source-sha256',required=True)
 a=p.parse_args()
 if not re.fullmatch('[0-9a-f]{32}',a.record):raise RuntimeError('Invalid worker evidence ID')
 if not re.fullmatch('[0-9a-f-]{36}',a.expected_job):raise RuntimeError('Invalid expected queue job')
 if source_fingerprint()!=a.source_sha256:raise RuntimeError('Worker source fingerprint mismatch')
 os.environ.pop('P4A_FIXTURE_OWNER',None)
 h.prepare_runtime()
 E=h.RUN/'evidence/gate6b/workers';E.mkdir(exist_ok=True)
 record=E/(a.record+'.json')
 facts={'pid':os.getpid(),'python':sys.executable,'mode':a.mode,'source_sha256':source_fingerprint(),
  'expected_rq_job':a.expected_job,'synthetic_extraction':True,'claim_acquired':False,'events':[]}
 def emit(kind,**data):
  facts['events'].append(dict(event=kind,**data));record.write_text(json.dumps(facts,indent=2))
 from rq import SimpleWorker,Queue,get_current_job
 from rq.serializers import JSONSerializer
 from infrastructure.queue import attempts,jobs
 from services.extraction import persistence,execution
 import psycopg
 original_execute=psycopg.Connection.execute
 def sql_observer(db,q,*args,**kw):
  try:return original_execute(db,q,*args,**kw)
  except psycopg.Error as exc:
   emit('sql_error',sqlstate=exc.sqlstate);raise
 psycopg.Connection.execute=sql_observer
 with h.connect_runtime() as db:
  identity=db.execute('SELECT current_user AS role,current_database() AS db,pg_backend_pid() AS pid').fetchone()
  assert identity['role']==h.RUNTIME and identity['db']==h.MAIN
 facts['database_identity']=identity
 claim_real=attempts.claim_delivery
 def claim(delivery,rqid):
  emit('claim_requested',rq_job_id=rqid,document_id=delivery.document_id,job_id=delivery.job_id,tenant_id=delivery.tenant_id,
       rq_dispatch_match=rqid==delivery.dispatch_token)
  try:owned=claim_real(delivery,rqid)
  except attempts.AttemptRejected as exc:emit('claim_rejected',reason=exc.code);raise
  facts['claim_acquired']=True
  emit('claimed',claim_fingerprint=hashlib.sha256(owned.claim_token.encode()).hexdigest(),job_id=owned.delivery.job_id)
  return owned
 attempts.claim_delivery=claim
 save_real=persistence.persist_extraction_result
 def save(document,result,**kw):
  try:out=save_real(document,result,**kw)
  except Exception as exc:
   emit('persistence_rejected',reason=exc.code if isinstance(exc,attempts.AttemptRejected) else type(exc).__name__);raise
  emit('persisted',extraction_id=out['extraction_id'],idempotent=out['idempotent'])
  return out
 persistence.persist_extraction_result=save
 fail_real=persistence.mark_extraction_job_failed
 def fail(*args,**kw):
  out=fail_real(*args,**kw);emit('failure_update',accepted=out);return out
 persistence.mark_extraction_job_failed=fail
 def synthetic(document_id,file_path,**kw):
  emit('synthetic_parser_entered',document_id=document_id)
  if a.mode.startswith('pause'):
   ready=E/(a.record+'.ready');release=E/(a.record+'.release')
   ready.write_text(a.expected_job)
   deadline=time.monotonic()+100
   while not release.exists():
    if time.monotonic()>deadline:raise RuntimeError('Controlled gate timeout')
    time.sleep(.05)
   emit('released')
  if a.mode.endswith('failure'):raise RuntimeError('Controlled synthetic parser failure')
  value=json.loads((h.ROOT/'tests/extraction/data/contracts/syn001_m2.json').read_text())
  run='p4a-g6b-'+uuid4().hex;value['document_id']=document_id;value['provenance']['service_run_id']=run
  target=h.RUN/'artifacts'/(run+'.json');target.write_text(json.dumps(value))
  emit('synthetic_artifact',service_run_id=run)
  return {'execution_status':'EXTRACTED','service_run_id':run,'public_result_path':str(target)}
 execution.execute_document_extraction=synthetic
 real=jobs.process_document_extraction_job
 def observed(*args,**kw):
  rqjob=get_current_job();assert rqjob and rqjob.id==a.expected_job
  emit('rq_execution',rq_job_id=rqjob.id)
  try:out=real(*args,**kw)
  except Exception as exc:emit('task_raised',exception=type(exc).__name__);raise
  emit('task_return',**{k:v for k,v in out.items() if k in ('status','reason','document_id','job_id','service_run_id')})
  return out
 jobs.process_document_extraction_job=observed
 r=h.redis_connection()
 try:
  q=Queue('cpu_tasks',connection=r,serializer=JSONSerializer)
  assert q.job_ids==[a.expected_job],'Worker refuses unexpected queued work'
  facts.update(serializer='JSONSerializer',scheduler=False,max_jobs=1,queue='cpu_tasks')
  emit('worker_ready')
  worker=SimpleWorker([q],connection=r,serializer=JSONSerializer,name='p4a-g6b-'+a.record)
  worked=worker.work(burst=True,max_jobs=1,with_scheduler=False,logging_level='WARNING')
  emit('worker_exited',worked=worked)
 finally:r.close()
if __name__=='__main__':main()
