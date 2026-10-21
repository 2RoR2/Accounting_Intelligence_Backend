"""Gate 5 PostgreSQL-only runner; no Redis, DDL fixtures or schema bootstrap."""
import os,sys,json,hashlib,subprocess,unittest,io,traceback,ast
from pathlib import Path
from contextlib import redirect_stdout,redirect_stderr
from importlib.metadata import version
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
sys.path.insert(0,str(R))
from tests import p4a_support as h
os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),P4A_FIXTURE_OWNER='1',PYTHON_DOTENV_DISABLED='1')
E=h.RUN/'evidence/phase4ar'
E.mkdir(exist_ok=True)
def g(*args):
 return subprocess.run(['/mnt/c/Program Files/Git/cmd/git.exe','-C','C:/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020',*args],check=True,capture_output=True).stdout
def preflight():
 c=h.configuration();h.verify_resource(c);h.verify_resource(c,'redis')
 assert g('rev-parse','HEAD').decode().strip()=='c65b41fb1c165f49b5bac791127d8f4838a33fab'
 assert g('rev-parse','MERGE_HEAD').decode().strip()=='39e68a1850daf6e875d7935dbb0138db55f3f170'
 assert g('rev-parse','--abbrev-ref','HEAD').decode().strip()=='integration/accounting-v2-features-20261020'
 assert not g('ls-files','-u')
 for name in ('app.py','infrastructure/queue/attempts.py','infrastructure/queue/jobs.py','services/extraction/persistence.py','validation/document_validation.py','database/PostgreSQL Database schema.sql','auth/security.py','requirements.txt'):
  staged=g('show',':'+name); actual=(R/name).read_bytes()
  assert (staged==actual if name!='requirements.txt' else staged.replace(b'\r\n',b'\n')==actual.replace(b'\r\n',b'\n')),name+' differs from staged integration'
 before=h._REAL_CONNECT
 def readonly(*a,**kw):
  kw['options']='-c default_transaction_read_only=on'
  return before(*a,**kw)
 h._REAL_CONNECT=readonly
 try:
  with h.connect_fixture() as db:
   identity=db.execute("""SELECT current_database() AS db,current_user AS role,version() AS version,
    current_setting('cluster_name') AS cluster,(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier""").fetchone()
   assert identity['system_identifier']=='7698802587763585063'
   counts={name:db.execute(query).fetchone()['n'] for name,query in {
    'tables':"SELECT count(*) AS n FROM pg_tables WHERE schemaname='public'",
    'triggers':"SELECT count(*) AS n FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal",
    'indexes':"SELECT count(*) AS n FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND i.indisvalid AND i.indisready"}.items()}
   assert counts=={'tables':51,'triggers':9,'indexes':138}
   db.rollback()
  with h.connect_runtime() as db:
   runtime=db.execute("SELECT current_user AS role,current_database() AS db").fetchone()
   assert runtime=={'role':h.RUNTIME,'db':h.MAIN}
   db.rollback()
 finally:h._REAL_CONNECT=before
 deps={n:version(n) for n in ('fastapi','starlette','anyio','psycopg','rq','redis','python-multipart')}
 from packaging.version import Version
 assert Version('0.115')<=Version(deps['fastapi'])<Version('0.116')
 assert Version('0.40')<=Version(deps['starlette'])<Version('0.42')
 assert Version('3.7')<=Version(deps['anyio'])<Version('4')
 backups={}
 for folder in ('phase3e/prechange','phase4a-20261021a1/evidence/prechange','phase4a-20261021a1/evidence/restricted-role-prechange'):
  p=R/'runs'/folder
  assert p.is_dir(),folder
  backups[folder]=len(list(p.rglob('*')))
 result={'identity':identity,'runtime':runtime,'counts':counts,'dependencies':deps,'python':sys.version.split()[0],
 'staged_sources_match':True,'index_diff_sha256':hashlib.sha256(g('diff','--cached','--raw')).hexdigest(),'backups_present':backups}
 (E/'preflight.json').write_text(json.dumps(result,indent=2))
 return result
try:
 info=preflight()
 if sys.argv[1:] == ['preflight']:
  print(json.dumps(info,indent=2));sys.exit(0)
 h.prepare_runtime()
 # PostgreSQL-only authorization: explicitly prevent even an accidental Redis socket.
 previous=h._REAL_SOCKET_CONNECT
 def pg_only(sock,address):
  if isinstance(address,tuple) and address[1]==6383:
   raise h.IsolationError('Redis access prohibited during Gate 5')
  return previous(sock,address)
 h._REAL_SOCKET_CONNECT=pg_only
 import psycopg
 active_case={'id':None}
 original_execute=psycopg.Connection.execute
 def audited_execute(db,query,*a,**kw):
  try:return original_execute(db,query,*a,**kw)
  except psycopg.Error as exc:
   event={'test':active_case['id'],'sqlstate':exc.sqlstate,'exception':type(exc).__name__,'backend_pid':db.info.backend_pid}
   with (E/'sqlstate-events.jsonl').open('a') as f:f.write(json.dumps(event)+'\n')
   raise
 psycopg.Connection.execute=audited_execute
 from tests.p4a.run import redact
 names=sys.argv[1:]
 suite=unittest.TestSuite()
 for name in names:
  assert name.startswith(('tests.test_postgresql_contract.PostgreSQLContractTests.','tests.p4a.test_runtime.RuntimeTests.','tests.p4a.test_gate5.'))
  suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
 class Result(unittest.TextTestResult):
  def startTest(self,test):
   super().startTest(test);self.current={}
   self.current['test']=test.id()
   active_case['id']=test.id()
  def stopTest(self,test):
   ids={t.id() for t,_ in self.errors+self.failures}
   status='FAIL' if test.id() in ids else ('SKIP' if any(t.id()==test.id() for t,_ in self.skipped) else 'PASS')
   record={'test':test.id(),'status':status,'document':getattr(test,'document',None),'tenant':str(getattr(test,'tenant',''))}
   if status=='FAIL':
    err=next(msg for t,msg in self.errors+self.failures if t.id()==test.id())
    record['diagnostic']=redact(err)
   records.append(record)
   (E/'cases.jsonl').open('a').write(json.dumps(record)+'\n')
   print(json.dumps({k:v for k,v in record.items() if k!='diagnostic'}),file=sys.__stdout__,flush=True)
   super().stopTest(test)
 records=[];buf=io.StringIO()
 with redirect_stdout(buf),redirect_stderr(buf):
  result=unittest.TextTestRunner(stream=buf,verbosity=2,failfast=True,resultclass=Result).run(suite)
 group='group-'+str(len(list(E.glob('group-*-result.json')))+1)
 (E/(group+'.log')).write_text(redact(buf.getvalue()))
 summary={'group':group,'ran':result.testsRun,'pass':sum(r['status']=='PASS' for r in records),
 'fail':len(result.errors)+len(result.failures),'skip':len(result.skipped),'tests':names,'remaining_not_run':len(names)-result.testsRun}
 (E/(group+'-result.json')).write_text(json.dumps(summary,indent=2))
 print(json.dumps(summary),flush=True)
 if result.errors or result.failures:print(redact(buf.getvalue())[-7000:],flush=True)
 sys.exit(0 if result.wasSuccessful() else 1)
except Exception as exc:
 safe={'stage':'preflight/import/runner','exception':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None)}
 # No locals or credential-bearing URLs; traceback diagnostic source lines only.
 (E/'runner-failure.json').write_text(json.dumps(safe))
 print(json.dumps(safe));sys.exit(1)
