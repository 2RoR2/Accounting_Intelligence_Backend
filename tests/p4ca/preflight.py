"""Fresh isolation gate; read-only database/Redis probes, no queue execution."""
import os,sys,json,hashlib,subprocess
from pathlib import Path
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
F=R.parent/'Accounting_Intelligence_Frontend_V2_integration_20261020'
sys.path.insert(0,str(R))
from tests import p4a_support as h
os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),P4A_FIXTURE_OWNER='1',PYTHON_DOTENV_DISABLED='1')
E=h.RUN/'evidence/phase4ca';P=h.PRIVATE/'phase4ca-20261021'
def sha(b):return hashlib.sha256(b).hexdigest()
def g(root,*a):
 return subprocess.run(['/mnt/c/Program Files/Git/cmd/git.exe','-C','C:/Users/60103/Documents/GitHub/'+root.name,*a],capture_output=True,check=True).stdout
def main():
 assert not E.exists() and not P.exists(),'Do not overwrite evidence'
 E.mkdir();P.mkdir(mode=0o700)
 facts={}
 try:
  c=h.configuration();h.verify_resource(c);h.verify_resource(c,'redis')
  for root,head,merge,branch in [(R,'c65b41fb1c165f49b5bac791127d8f4838a33fab','39e68a1850daf6e875d7935dbb0138db55f3f170','integration/accounting-v2-features-20261020'),(F,'14883335d8a4badff1fdf4c2f25027f12feca160','645135776929184a07b1a3292e13e05934bb4668','integration/features-accounting-v2-20261020')]:
   assert not g(root,'ls-files','-u')
   assert g(root,'rev-parse','HEAD').decode().strip()==head
   assert g(root,'rev-parse','MERGE_HEAD').decode().strip()==merge
   assert g(root,'branch','--show-current').decode().strip()==branch
   facts[root.name]={'head':head,'merge_head':merge,'branch':branch,'index_sha256':sha(g(root,'diff','--cached','--binary')),'working_diff_sha256':sha(g(root,'diff','--binary'))}
  prior={str(p.relative_to(h.RUN)):sha(p.read_bytes()) for p in (h.RUN/'evidence').rglob('*') if p.is_file() and not p.is_relative_to(E)}
  (E/'historical-hashes.json').write_text(json.dumps(prior,indent=2))
  real=h._REAL_CONNECT
  def readonly(*a,**kw):kw['options']='-c default_transaction_read_only=on';return real(*a,**kw)
  h._REAL_CONNECT=readonly
  try:
   with h.connect_fixture() as db:
    row=db.execute("SELECT current_database() AS db,current_user AS role,current_setting('cluster_name') AS cluster,(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier").fetchone()
    assert row['system_identifier']=='7698802587763585063';facts['postgresql']=row;db.rollback()
   with h.connect_runtime() as db:
    facts['runtime']=db.execute("SELECT current_database() AS db,current_user AS role,host(inet_server_addr()) AS address,inet_server_port() AS port,current_setting('server_version') AS version").fetchone()
    cols=db.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND table_name IN ('review_submissions','review_decisions')").fetchall()
    assert {'table_name':'review_decisions','column_name':'reviewed_at'} in cols
    assert {'table_name':'review_decisions','column_name':'created_at'} not in cols
    facts['history_columns']=cols;db.rollback()
  finally:h._REAL_CONNECT=real
  r=h.redis_connection()
  try:
   assert r.info('server')['redis_version']=='7.2.16'
   approved=set(json.loads((h.RUN/'evidence/gate6b/final-results.json').read_text())['redis']['retained_keys'])
   # Earlier accepted Phase4B added two completed jobs and one stopped worker.
   for file in ('browser-upload.json','real-docling-upload.json'):
    priorjob=json.loads((h.RUN/'evidence/phase4b'/file).read_text())
    approved.add('rq:job:'+priorjob['queueJobId'])
    approved.add('rq:results:'+priorjob['queueJobId'])
   proc=json.loads((h.RUN/'evidence/phase4b/processes.json').read_text())
   worker=next(x for x in proc if x['mode']=='worker')
   approved.add('rq:worker:p4b-20261021-'+str(worker['pid']))
   keys={k.decode() for k in r.scan_iter()}
   # Index/registry keys shared by the two already documented acceptance runs.
   approved.update({'rq:workers','rq:queues','rq:finished:cpu_tasks'})
   assert keys.issubset(approved),'Unrecognized Redis keys: '+repr(sorted(keys-approved))
   assert r.llen('rq:queue:cpu_tasks')==r.llen('rq:queue:gpu_tasks')==0
   me=r.client_id();assert all(int(x['id'])==me for x in r.client_list()),'Unexpected client'
   for raw in r.smembers('rq:workers'):
    key=raw.decode();name=key.removeprefix('rq:worker:')
    assert name.startswith(('p4a-g6b-','p4b-20261021-')),'Unexpected worker'
    pid=r.hget(key,'pid')
    if pid:
     cmd=Path('/proc')/pid.decode()/'cmdline'
     assert not cmd.exists() or not any(t in cmd.read_bytes() for t in (b'tests.p4a.worker',b'/phase4b/runtime.py',b'tests/p4b/runtime.py')),'Live prior worker'
   facts['redis']={'run_id':r.info('server')['run_id'],'marker_matches':True,'keys':sorted(keys),'unexpected_clients':0}
  finally:r.close()
  fm={}
  for name in set(g(F,'ls-files','-z').decode().split('\0')+g(F,'ls-files','--others','--exclude-standard','-z').decode().split('\0')):
   if name and (F/name).is_file():fm[name]=sha((F/name).read_bytes())
  (E/'frontend-working-source-hashes.json').write_text(json.dumps(fm,indent=2))
  for name in ('next-env.d.ts','tsconfig.json'):
   target=P/name;target.write_bytes((F/name).read_bytes());os.chmod(target,0o600)
  facts.update(resource_ids={k:c[k] for k in ('pg_id','redis_id','pg_image','redis_image','network_id')},historical_artifacts=len(prior),result='PASS')
 except Exception as exc:
  facts.update(result='STOP',exception=type(exc).__name__,reason=str(exc) if isinstance(exc,(h.IsolationError,AssertionError)) else 'Preflight failed; credentials suppressed')
 (E/'preflight.json').write_text(json.dumps(facts,indent=2));print(json.dumps(facts,indent=2))
 return facts['result']=='PASS'
if __name__=='__main__':raise SystemExit(0 if main() else 1)
