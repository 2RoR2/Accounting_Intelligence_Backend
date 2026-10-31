"""New Phase4CA synthetic browser accounts/document; no queue or inference."""
import os,sys,json,secrets,tempfile,hashlib
from pathlib import Path
from uuid import uuid4
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
sys.path.insert(0,str(R))
from tests.acceptance.support import phase4a_guard as h
os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),P4A_FIXTURE_OWNER='1',PYTHON_DOTENV_DISABLED='1')
P=h.PRIVATE/'phase4ca-20261021';E=h.RUN/'evidence/phase4ca'
assert json.loads((E/'preflight.json').read_text())['result']=='PASS'
assert P.is_dir() and not (P/'accounts.json').exists()
for d in ('outbox','pdfs'):(P/d).mkdir(mode=0o700)
(E/'screenshots').mkdir()
h.prepare_runtime()
from security.authentication import hash_password
from tests.integration.test_postgresql_contract import PostgreSQLContractTests
run=uuid4().hex[:12];accounts={}
with h.connect_fixture() as db:
 tenants=[]
 for name in ('Primary','Foreign'):
  tenants.append(db.execute('INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,%s) RETURNING id,tenant_code',
   ('P4CA Synthetic '+name+' '+run,uuid4().hex,uuid4().hex+'@example.test')).fetchone())
 for label,role,tenant in [('owner','accountant',tenants[0]),('reviewer','local_admin',tenants[0]),('foreign','accountant',tenants[1])]:
  password='P4CA!'+secrets.token_urlsafe(24)+'a9';email='p4ca.'+label+'.'+run+'@example.test'
  user=db.execute("INSERT INTO users(tenant_id,name,email,role,status,password_hash,must_change_password) VALUES (%s,%s,%s,%s,'active',%s,false) RETURNING id",
   (tenant['id'],'P4CA Synthetic '+label,email,role,hash_password(password))).fetchone()
  accounts[label]={'id':str(user['id']),'email':email,'password':password,'role':role,'tenant_id':str(tenant['id']),'tenant_code':tenant['tenant_code']}
(P/'accounts.json').write_text(json.dumps(accounts,indent=2));os.chmod(P/'accounts.json',0o600)
# Reuse only the existing pure PDF generator, never its top-level fixture setup.
import ast
old=(R/'tests/acceptance/docling_runtime/fixture_setup.py').read_text()
node=next(n for n in ast.parse(old).body if isinstance(n,ast.FunctionDef) and n.name=='pdf')
namespace={};exec(compile(ast.Module(body=[node],type_ignores=[]),'<pure synthetic PDF generator>','exec'),namespace)
payload=namespace['pdf'](['INVOICE','P4CA SYNTHETIC REVIEW FIXTURE '+run,'Example Stationery','Invoice SYN-001','2 x 25.00 = 50.00','Total MYR 50.00'])
pdf=P/'pdfs/review.pdf';pdf.write_bytes(payload);os.chmod(pdf,0o600)
case=PostgreSQLContractTests('test_stale_revision_does_not_write')
case.tenant=tenants[0]['id'];case.owner={'id':accounts['owner']['id']};case.assignee=case.owner
with tempfile.TemporaryDirectory() as temporary:
 case.temp=type('Directory',(),{'name':temporary})()
 case.document,case.result=case.document_fixture()
 saved=case.persist()
with h.connect_fixture() as db:
 db.execute('UPDATE documents SET storage_path=%s,file_size_bytes=%s,file_hash_sha256=%s WHERE id=%s AND tenant_id=%s',
  (str(pdf),len(payload),hashlib.sha256(payload).hexdigest(),case.document,case.tenant))
fixture={'run':run,'document':case.document,'extraction':saved['extraction_id'],'job':saved['job_id'],
 'accounts':{k:{x:v for x,v in u.items() if x not in ('password','email')} for k,u in accounts.items()},
 'extraction_provenance':'Explicit synthetic M2 fixture, persisted via real attempt/persistence protocol; no Redis dispatch or inference'}
(E/'browser-fixture.json').write_text(json.dumps(fixture,indent=2));print(json.dumps(fixture))
