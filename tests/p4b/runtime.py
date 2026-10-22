"""Phase4B test-only loopback API/worker supervisor. No production source edits."""
import os,sys,json,time,hashlib,subprocess,signal
from pathlib import Path
from uuid import uuid4
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
sys.path.insert(0,str(R))
from tests import p4a_support as h
P=h.PRIVATE/'phase4b-20261021';E=h.RUN/'evidence/phase4b'
def event(name,**values):
 with (E/'runtime-events.jsonl').open('a') as f:f.write(json.dumps(dict(event=name,**values),default=str)+'\n')
def configure():
 os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),PYTHON_DOTENV_DISABLED='1')
 os.environ.pop('P4A_FIXTURE_OWNER',None)
 c=h.configuration();h.verify_resource(c);h.verify_resource(c,'redis');h.install_guards()
 from urllib.parse import quote
 os.environ.update(DATABASE_URL='postgresql://'+h.RUNTIME+':'+quote(h.secret('pg-runtime-password'),safe='')+'@127.0.0.1:55435/'+h.MAIN,
  REDIS_URL='redis://:'+quote(h.secret('redis-password'),safe='')+'@127.0.0.1:6383/0',
  AUTH_SECRET=h.secret('auth-secret'),FRONTEND_ORIGINS='http://127.0.0.1:3117',COOKIE_SECURE='false',
  DOCUMENT_STORAGE_PATH=str(h.RUN/'storage'),QWEN_ENABLED='false',HF_HUB_OFFLINE='1',
  TRANSFORMERS_OFFLINE='1',PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
 import database.connection as conn
 conn.connect=h.connect_runtime
 import app
 from job_queue import attempts
 from services.extraction import persistence
 for m in (app,attempts,persistence):m.connect=h.connect_runtime
 event('runtime_identity',mode=sys.argv[1],pid=os.getpid(),python=sys.executable,role=h.RUNTIME,db=h.MAIN,origin='http://127.0.0.1:3117')
 return app
def api():
 app=configure();accounts=json.loads((P/'accounts.json').read_text())
 recipients={u['email'] for u in accounts.values()}
 def local_email(recipient,code):
  if recipient not in recipients:raise RuntimeError('Only Phase4B synthetic recipients allowed')
  target=P/'outbox'/(hashlib.sha256(recipient.encode()).hexdigest()+'.json')
  target.write_text(json.dumps({'email':recipient,'code':code,'created_at':time.time(),'delivery':'PRIVATE TEST SINK'}))
  os.chmod(target,0o600)
  event('mfa_private_delivery',recipient_id=next(k for k,v in accounts.items() if v['email']==recipient))
 app.send_login_code=local_email
 def no_reset(*a,**kw):raise RuntimeError('Password-reset delivery out of test scope')
 app.send_reset_code=no_reset
 # Audit DDL/SQL errors without query parameters, codes or financial data.
 import psycopg
 execute=psycopg.Connection.execute
 def observed(db,q,*a,**kw):
  text=str(q).lstrip().upper()
  if text.startswith(('ALTER ','CREATE ','DROP ','TRUNCATE ','GRANT ','REVOKE ')):
   event('unexpected_runtime_ddl',operation=text.split()[0]);raise RuntimeError('Runtime DDL forbidden')
  try:return execute(db,q,*a,**kw)
  except psycopg.Error as exc:event('sql_error',sqlstate=exc.sqlstate);raise
 psycopg.Connection.execute=observed
 import uvicorn
 uvicorn.run(app.app,host='127.0.0.1',port=8017,loop='asyncio',http='h11',ws='none',access_log=False,log_level='warning')
def worker():
 configure()
 from rq import Queue,SimpleWorker,get_current_job
 from rq.serializers import JSONSerializer
 from job_queue import jobs,attempts
 from services.extraction import execution,persistence
 claim_real=attempts.claim_delivery
 def claim(delivery,rqid):
  proof=claim_real(delivery,rqid)
  event('worker_claim',pid=os.getpid(),rq_job_id=rqid,document_id=delivery.document_id,job_id=delivery.job_id,
   claim_fingerprint=hashlib.sha256(proof.claim_token.encode()).hexdigest())
  return proof
 attempts.claim_delivery=claim
 save_real=persistence.persist_extraction_result
 def save(document,result,**kw):
  value=save_real(document,result,**kw)
  event('worker_persisted',document_id=document,job_id=value['job_id'],extraction_id=value['extraction_id'],
   service_run_id=result['service_run_id'])
  return value
 persistence.persist_extraction_result=save
 def extraction(document_id,file_path,**kw):
  name=Path(file_path).name;rqid=get_current_job().id
  if 'real-docling-smoke' in name:
   out=E/('docling-result-'+document_id+'.json')
   env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('PASSWORD','SECRET','TOKEN','DATABASE','REDIS','SMTP','RESEND','P4A'))}
   env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HOME=str(E/'cache/huggingface'),
    XDG_CACHE_HOME=str(E/'cache'),TORCH_HOME=str(E/'cache/torch'),QWEN_ENABLED='false',PYTHONDONTWRITEBYTECODE='1')
   with (E/('docling-'+document_id+'.log')).open('w') as log:
    try:done=subprocess.run(['/home/donut99/.venvs/accounting-intelligence/bin/python','-B',str(E/'docling-child.py'),document_id,str(file_path),str(out)],
      cwd=R,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=95)
    except subprocess.TimeoutExpired:event('real_docling',document_id=document_id,result='BLOCKED',reason='95s bound');raise
   if done.returncode!=0 or not out.exists():
    event('real_docling',document_id=document_id,result='FAIL',exit_code=done.returncode);raise RuntimeError('Bounded real Docling failed')
   value=json.loads(out.read_text())
   event('real_docling',document_id=document_id,result=value['execution_status'],exit_code=done.returncode)
   return value
  if 'failed-once' in name:
   flag=P/('provider-failed-once-'+document_id)
   if not flag.exists():flag.write_text('synthetic provider failure');raise RuntimeError('Controlled synthetic extraction failure')
  value=json.loads((R/'tests/extraction/data/contracts/syn001_m2.json').read_text())
  run='p4b-synthetic-'+uuid4().hex;value['document_id']=document_id;value['provenance']['service_run_id']=run
  value['fields']['invoice_number']['value']='P4B-'+document_id[:12]
  for kind in ('bill','receipt','unknown'):
   if kind in name:
    value['document_type'].update(value=kind,status='unknown' if kind=='unknown' else 'classified',method='phase4b_synthetic_fixture')
  if 'missing-info' in name:
   value['fields']['invoice_number'].update(value=None,state='missing',resolution='unresolved',evidence=[],candidates=[])
  if 'inconsistent-totals' in name:value['fields']['total_amount']['value']='60.00'
  target=h.RUN/'artifacts'/(run+'.json');target.write_text(json.dumps(value))
  event('synthetic_extraction',document_id=document_id,rq_job_id=rqid,kind=value['document_type']['value'])
  return {'execution_status':'EXTRACTED','service_run_id':run,'public_result_path':str(target)}
 execution.execute_document_extraction=extraction
 r=h.redis_connection();q=Queue('cpu_tasks',connection=r,serializer=JSONSerializer)
 assert q.count==0,'Phase4B worker refuses prior queued work'
 # Suppress built-in registry sweeping to preserve retained Gate6B interruption evidence.
 SimpleWorker([q],connection=r,serializer=JSONSerializer,name='p4b-20261021-'+str(os.getpid()),maintenance_interval=10**12).work(
  burst=False,max_jobs=12,max_idle_time=600,with_scheduler=False,logging_level='WARNING')
def launch():
 assert not (E/'processes.json').exists(),'Do not duplicate test processes'
 for port in (8017,):
  import socket
  probe=socket.socket();probe.bind(('127.0.0.1',port));probe.close()
 records=[]
 for mode in ('api','worker'):
  log=(E/(mode+'.log')).open('w')
  process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),mode],cwd=R,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,stdin=subprocess.DEVNULL)
  records.append({'mode':mode,'pid':process.pid,'start_ticks':Path('/proc/'+str(process.pid)+'/stat').read_text().split()[21],
   'command_entry':str(Path(__file__).resolve())})
 (E/'processes.json').write_text(json.dumps(records,indent=2));print(json.dumps(records))
def stop():
 records=json.loads((E/'processes.json').read_text());results=[]
 for item in reversed(records):
  proc=Path('/proc')/str(item['pid'])
  if not proc.exists():results.append(dict(mode=item['mode'],result='already exited'));continue
  assert proc.joinpath('stat').read_text().split()[21]==item['start_ticks'],'PID reused; do not signal'
  assert item['command_entry'].encode() in proc.joinpath('cmdline').read_bytes(),'Unexpected process identity'
  os.kill(item['pid'],signal.SIGTERM);deadline=time.monotonic()+8
  while proc.exists() and time.monotonic()<deadline:
   if proc.joinpath('stat').read_text().split()[2]=='Z':break
   time.sleep(.1)
  if proc.exists() and proc.joinpath('stat').read_text().split()[2]!='Z':os.kill(item['pid'],signal.SIGKILL)
  results.append(dict(mode=item['mode'],pid=item['pid'],result='owned process stopped'))
 (E/'processes-stopped.json').write_text(json.dumps(results,indent=2));print(json.dumps(results))
if __name__=='__main__':
 {'launch':launch,'api':api,'worker':worker,'stop':stop}[sys.argv[1]]()
