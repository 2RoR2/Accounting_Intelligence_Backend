"""Bounded read-only final evidence and preservation verification."""
import os,sys,json,hashlib,subprocess,ast
from pathlib import Path
from collections import Counter
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020');F=R.parent/'Accounting_Intelligence_Frontend_V2_integration_20261020';sys.path.insert(0,str(R))
from tests.acceptance.support import phase4a_guard as h
from tests.acceptance.browser_postgres.native_docker import enable
enable()
os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),P4A_FIXTURE_OWNER='1',PYTHON_DOTENV_DISABLED='1')
E=h.RUN/'evidence/phase4ca';P=h.PRIVATE/'phase4ca-20261021'
def sha(b):return hashlib.sha256(b).hexdigest()
def g(root,*a):return subprocess.run(['/mnt/c/Program Files/Git/cmd/git.exe','-C','C:/Users/60103/Documents/GitHub/'+root.name,*a],capture_output=True,check=True).stdout
pre=json.loads((E/'preflight.json').read_text());assert pre['result']=='PASS'
for name,fingerprint in json.loads((E/'historical-hashes.json').read_text()).items():assert sha((h.RUN/name).read_bytes())==fingerprint,name
for name,fingerprint in json.loads((E/'frontend-working-source-hashes.json').read_text()).items():assert sha((F/name).read_bytes())==fingerprint,name
assert sha(g(F,'diff','--cached','--binary'))==pre[F.name]['index_sha256']
assert sha(g(F,'diff','--binary'))==pre[F.name]['working_diff_sha256']
assert sha(g(R,'diff','--cached','--binary'))==pre[R.name]['index_sha256']
for root in (R,F):
 assert not g(root,'ls-files','-u')
 assert g(root,'rev-parse','HEAD').decode().strip()==pre[root.name]['head']
 assert g(root,'rev-parse','MERGE_HEAD').decode().strip()==pre[root.name]['merge_head']
c=h.configuration();h.verify_resource(c);h.verify_resource(c,'redis')
real=h._REAL_CONNECT
def readonly(*a,**kw):kw['options']='-c default_transaction_read_only=on';return real(*a,**kw)
h._REAL_CONNECT=readonly
fixture=json.loads((E/'browser-fixture.json').read_text());document=fixture['document']
with h.connect_fixture() as db:
 identity=db.execute("SELECT current_database() AS db,current_user AS role,current_setting('cluster_name') AS cluster,(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier").fetchone()
 assert identity['system_identifier']=='7698802587763585063'
 baseline=json.loads((h.RUN/'evidence/phase4ar/original-duplicate-row-hashes-before-regression.json').read_text())
 ids=['c5433402-5ecc-4434-8014-1696dc3cf4ee','66a0534f-9e22-4068-86b5-6429c6670ec4']
 for table in ('documents','pending_jobs'):
  key='id' if table=='documents' else 'document_id'
  rows=db.execute('SELECT to_jsonb(t) AS row FROM '+table+' t WHERE '+key+'=ANY(%s::uuid[]) ORDER BY id',(ids,)).fetchall()
  assert sha(json.dumps(rows,sort_keys=True,default=str).encode())==baseline[table]
 # Earlier connected-browser rows are untouched, including their failure evidence.
 for item in json.loads((h.RUN/'evidence/phase4b/final-verification.json').read_text())['persistence']:
  row=db.execute('SELECT status,content_revision FROM documents WHERE id=%s',(item['document'],)).fetchone()
  assert row['content_revision']==item['content_revision']==2
  assert db.execute('SELECT status FROM pending_jobs WHERE id=%s',(item['job'],)).fetchone()['status']==item['status']=='succeeded'
  assert db.execute('SELECT count(*) AS n FROM standardised_records WHERE document_id=%s',(item['document'],)).fetchone()['n']==0
  assert db.execute('SELECT count(*) AS n FROM field_corrections fc JOIN field_predictions fp ON fp.id=fc.field_prediction_id WHERE fp.document_id=%s',(item['document'],)).fetchone()['n']==0
 doc=db.execute('SELECT id,status,content_revision,owner_id,tenant_id FROM documents WHERE id=%s',(document,)).fetchone()
 assert doc['status']=='validated' and doc['content_revision']==4
 fields=db.execute("SELECT field_key,raw_value,current_value FROM field_predictions WHERE document_id=%s AND field_key='invoice'",(document,)).fetchone()
 assert fields['raw_value']=='Invoice number: SYN-001' and fields['current_value']=='P4CA-REVIEWED'
 raw=db.execute('SELECT raw_json,is_current FROM raw_extractions WHERE document_id=%s',(document,)).fetchall();assert len(raw)==1 and raw[0]['is_current']
 assert raw[0]['raw_json']['fields']['invoice_number']['value']=='SYN-001'
 header=db.execute('SELECT fc.old_value,fc.new_value,fc.corrected_by FROM field_corrections fc JOIN field_predictions fp ON fp.id=fc.field_prediction_id WHERE fp.document_id=%s',(document,)).fetchall()
 assert len(header)==1 and header[0]['old_value']=='SYN-001' and header[0]['new_value']=='P4CA-REVIEWED'
 line=db.execute('SELECT old_value,new_value,corrected_by FROM line_item_corrections WHERE document_id=%s',(document,)).fetchall()
 assert len(line)==1 and line[0]['new_value']=='Synthetic review-approved stationery'
 submissions=db.execute('SELECT id,submitted_by,content_revision,created_at FROM review_submissions WHERE document_id=%s',(document,)).fetchall()
 decisions=db.execute('SELECT id,reviewed_by,approved_content_revision,reviewed_at FROM review_decisions WHERE document_id=%s',(document,)).fetchall()
 assert len(submissions)==len(decisions)==1
 assert str(submissions[0]['submitted_by'])==fixture['accounts']['owner']['id']
 assert str(decisions[0]['reviewed_by'])==fixture['accounts']['reviewer']['id']
 assert submissions[0]['content_revision']==decisions[0]['approved_content_revision']==4
 assert db.execute('SELECT count(*) AS n FROM standardised_records WHERE document_id=%s',(document,)).fetchone()['n']==0
 db.rollback()
with h.connect_runtime() as db:
 role=db.execute('SELECT current_user AS role,current_database() AS db,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
 assert not any(v for k,v in role.items() if k not in ('role','db'));db.rollback()
r=h.redis_connection()
try:
 keys=sorted(k.decode() for k in r.scan_iter());assert set(keys).issubset(set(pre['redis']['keys']))
 assert r.llen('rq:queue:cpu_tasks')==r.llen('rq:queue:gpu_tasks')==0
 me=r.client_id();assert all(int(x['id'])==me for x in r.client_list())
 redis={'run_id':r.info('server')['run_id'],'owner_matches':True,'no_new_keys':True,'queues_empty':True,'only_probe_client':True}
finally:r.close()
for file in ('processes.json','processes-recheck.json'):
 for item in json.loads((E/file).read_text()):
  proc=Path('/proc')/str(item['pid'])
  assert not proc.exists() or proc.joinpath('stat').read_text().split()[2]=='Z','Owned API still running'
events=[json.loads(x) for x in (E/'runtime-events.jsonl').read_text().splitlines()]
assert not any(x['event'] in ('unexpected_runtime_ddl','sql_error') for x in events)
browser=json.loads((E/'browser-results.json').read_text());assert len(browser['results'])==12 and all(x['status']=='PASS' for x in browser['results'])
result={'result':'PASS','postgresql_identity':identity,'runtime_role':role,'redis':redis,'browser_counts':dict(Counter(x['status'] for x in browser['results'])),
 'history_http_responses':dict(Counter(x['status'] for x in browser['http'] if x['path'].endswith('/reviews'))),
 'document':doc,'header_corrections':header,'line_corrections':line,'submissions':submissions,'decisions':decisions,
 'original_raw_extraction_unchanged':True,'current_extractions':1,'records_created':0,'schema_sha256':h.SCHEMA_SHA,
 'historical_files_unchanged':len(json.loads((E/'historical-hashes.json').read_text())),
 'frontend_complete_source_and_index_preserved':True,'backend_original_index_preserved_before_targeted_staging':True,
 'historical_duplicate_rows_unchanged':True,'historical_phase4b_documents_unchanged':True,'owned_api_processes_stopped':True,'runtime_sql_errors':0,'runtime_ddl_attempts':0,
 'screenshots':sorted(p.name for p in (E/'screenshots').glob('*.png'))}
(E/'final-verification.json').write_text(json.dumps(result,default=str,indent=2));print(json.dumps(result,default=str,indent=2))
