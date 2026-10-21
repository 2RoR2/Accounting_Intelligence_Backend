"""Phase4CA test-only loopback API/worker supervisor. No production source edits."""
import os,sys,json,time,hashlib,subprocess,signal
from pathlib import Path
from uuid import uuid4
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
sys.path.insert(0,str(R))
from tests import p4a_support as h
P=h.PRIVATE/'phase4ca-20261021';E=h.RUN/'evidence/phase4ca'
def event(name,**values):
 with (E/'runtime-events.jsonl').open('a') as f:f.write(json.dumps(dict(event=name,**values),default=str)+'\n')
def configure():
 os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),PYTHON_DOTENV_DISABLED='1')
 os.environ.pop('P4A_FIXTURE_OWNER',None)
 from tests.p4ca.native_docker import enable
 enable()
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
 from infrastructure.queue import attempts
 from services.extraction import persistence
 for m in (app,attempts,persistence):m.connect=h.connect_runtime
 event('runtime_identity',mode=sys.argv[1],pid=os.getpid(),python=sys.executable,role=h.RUNTIME,db=h.MAIN,origin='http://127.0.0.1:3117')
 return app
def api():
 app=configure();accounts=json.loads((P/'accounts.json').read_text())
 recipients={u['email'] for u in accounts.values()}
 def local_email(recipient,code):
  if recipient not in recipients:raise RuntimeError('Only Phase4CA synthetic recipients allowed')
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
def launch():
 assert not (E/'processes-recheck.json').exists(),'Do not duplicate test processes'
 for port in (8017,):
  import socket
  probe=socket.socket();probe.bind(('127.0.0.1',port));probe.close()
 records=[]
 for mode in ('api',):
  log=(E/(mode+'-recheck.log')).open('w')
  process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),mode],cwd=R,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,stdin=subprocess.DEVNULL)
  records.append({'mode':mode,'pid':process.pid,'start_ticks':Path('/proc/'+str(process.pid)+'/stat').read_text().split()[21],
   'command_entry':str(Path(__file__).resolve())})
 (E/'processes-recheck.json').write_text(json.dumps(records,indent=2));print(json.dumps(records))
def stop():
 records=json.loads((E/'processes-recheck.json').read_text());results=[]
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
 (E/'processes-recheck-stopped.json').write_text(json.dumps(results,indent=2));print(json.dumps(results))
if __name__=='__main__':
 {'launch':launch,'api':api,'stop':stop}[sys.argv[1]]()
