"""Gate6B allowlisted real RQ runner; no schema/bootstrap or model execution."""
import os,sys,json,subprocess,unittest,io,hashlib,ast
from pathlib import Path
from contextlib import redirect_stdout,redirect_stderr
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
sys.path.insert(0,str(R))
from tests.acceptance.support import phase4a_guard as h
os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),P4A_FIXTURE_OWNER='1',PYTHON_DOTENV_DISABLED='1')
E=h.RUN/'evidence/gate6b';(E/'workers').mkdir(exist_ok=True)
def g(*a):return subprocess.run(['/mnt/c/Program Files/Git/cmd/git.exe','-C','C:/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020',*a],check=True,capture_output=True).stdout
def sha(b):return hashlib.sha256(b).hexdigest()
def preflight():
 c=h.configuration();h.verify_resource(c);h.verify_resource(c,'redis')
 assert g('rev-parse','HEAD').decode().strip()=='c65b41fb1c165f49b5bac791127d8f4838a33fab'
 assert g('rev-parse','MERGE_HEAD').decode().strip()=='39e68a1850daf6e875d7935dbb0138db55f3f170'
 assert g('branch','--show-current').decode().strip()=='integration/accounting-v2-features-20261020'
 assert not g('ls-files','-u')
 assert not any(k in os.environ for k in ('PYTHONPATH','PYTHONHOME'))
 for name in ['main.py','services/queue/config.py','services/queue/jobs.py','services/queue/attempts.py','services/extraction/persistence/persistence.py','tests/acceptance/support/phase4a_guard.py','tests/acceptance/postgres_redis/worker.py','tests/acceptance/postgres_redis/test_rq.py','tests/acceptance/postgres_redis/run_gate6b.py']:
  staged=g('show',':'+name);actual=(R/name).read_bytes()
  assert staged.replace(b'\r\n',b'\n')==actual.replace(b'\r\n',b'\n'),name+' differs from staged candidate'
  if name.endswith('.py'):ast.parse(actual)
 original=h._REAL_CONNECT
 def readonly(*a,**kw):kw['options']='-c default_transaction_read_only=on';return original(*a,**kw)
 h._REAL_CONNECT=readonly
 try:
  with h.connect_fixture() as db:
   identity=db.execute("SELECT current_database() AS db,current_user AS role,current_setting('cluster_name') AS cluster,(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier").fetchone()
   assert identity['system_identifier']=='7698802587763585063';db.rollback()
  with h.connect_runtime() as db:
   runtime=db.execute('SELECT current_user AS role,current_database() AS db').fetchone()
   assert runtime=={'role':h.RUNTIME,'db':h.MAIN};db.rollback()
 finally:h._REAL_CONNECT=original
 h.prepare_runtime()
 from rq import Worker
 from importlib.metadata import version
 r=h.redis_connection()
 try:
  assert r.info('server')['redis_version']=='7.2.16'
  assert version('rq')=='2.12.0'
  workers=Worker.all(connection=r)
  for w in workers:
   assert w.name.startswith('p4a-g6b-') and (E/'workers'/(w.name.removeprefix('p4a-g6b-')+'.json')).exists(),'Unexpected worker registry'
  assert r.llen('rq:queue:cpu_tasks')==0,'Unexpected pending work'
  assert r.llen('rq:queue:gpu_tasks')==0
  key_inventory=sorted(k.decode() for k in r.scan_iter())
 finally:r.close()
 return {'pg':identity,'runtime':runtime,'redis_version':'7.2.16','rq_version':version('rq'),
  'python':sys.executable,'staged_sources_match':True,'key_inventory_before':key_inventory,'worker_registry_count':len(workers)}
def preserved():
 # Historical PostgreSQL evidence and synthetic original duplicate records are never fixture targets.
 folders=[h.RUN/'evidence/gate5',h.RUN/'evidence/phase4ar',h.RUN/'evidence/gate6a']
 hashes={str(p.relative_to(h.RUN)):sha(p.read_bytes()) for f in folders for p in f.rglob('*') if p.is_file()}
 target=E/'preserved-evidence-hashes.json'
 if target.exists():assert json.loads(target.read_text())==hashes,'Earlier acceptance evidence changed'
 else:target.write_text(json.dumps(hashes,indent=2))
 return len(hashes)
try:
 info=preflight();info['protected_artifacts_verified']=preserved()
 number=len(list(E.glob('group-*-result.json')))+1
 (E/('preflight-'+str(number)+'.json')).write_text(json.dumps(info,indent=2))
 names=sys.argv[1:]
 assert names and all(n.startswith('tests.acceptance.postgres_redis.test_rq.RQTests.test_') for n in names)
 from tests.acceptance.postgres_redis.run import redact
 suite=unittest.defaultTestLoader.loadTestsFromNames(names)
 records=[];buffer=io.StringIO()
 import psycopg
 original_execute=psycopg.Connection.execute
 active={'test':None}
 def observe(db,q,*a,**kw):
  try:return original_execute(db,q,*a,**kw)
  except psycopg.Error as exc:
   with (E/'sqlstates.jsonl').open('a') as f:f.write(json.dumps({'test':active['test'],'sqlstate':exc.sqlstate,'backend_pid':db.info.backend_pid})+'\n')
   raise
 psycopg.Connection.execute=observe
 class Result(unittest.TextTestResult):
  def startTest(self,test):active['test']=test.id();super().startTest(test)
  def stopTest(self,test):
   failures={t.id():msg for t,msg in self.failures+self.errors}
   status='FAIL' if test.id() in failures else 'SKIP' if any(t.id()==test.id() for t,_ in self.skipped) else 'PASS'
   record={'test':test.id(),'status':status}
   if status=='FAIL':record['diagnostic']=redact(failures[test.id()])
   records.append(record)
   with (E/'cases.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')
   print(json.dumps({k:v for k,v in record.items() if k!='diagnostic'}),file=sys.__stdout__,flush=True)
   super().stopTest(test)
 with redirect_stdout(buffer),redirect_stderr(buffer):
  res=unittest.TextTestRunner(stream=buffer,verbosity=2,failfast=True,resultclass=Result).run(suite)
 log=redact(buffer.getvalue());(E/('group-'+str(number)+'.log')).write_text(log)
 result={'group':number,'ran':res.testsRun,'pass':sum(x['status']=='PASS' for x in records),'fail':len(res.errors)+len(res.failures),'skip':len(res.skipped),'remaining_not_run':len(names)-res.testsRun}
 (E/('group-'+str(number)+'-result.json')).write_text(json.dumps(result,indent=2))
 print(json.dumps(result),flush=True)
 if not res.wasSuccessful():print(log[-6500:],flush=True)
 preserved()
 sys.exit(0 if res.wasSuccessful() and not res.skipped else 1)
except Exception as exc:
 if isinstance(exc,SystemExit):raise
 facts={'stage':'preflight/runner','exception':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None)}
 (E/'runner-failure.json').write_text(json.dumps(facts));print(json.dumps(facts));sys.exit(1)
