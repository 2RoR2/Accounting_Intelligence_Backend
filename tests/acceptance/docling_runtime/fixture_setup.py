"""Phase4B unique synthetic fixtures; guarded disposable DB only."""
import os,sys,json,secrets,hashlib,subprocess
from pathlib import Path
from uuid import uuid4
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
F=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Frontend_V2_integration_20261020')
sys.path.insert(0,str(R))
from tests.acceptance.support import phase4a_guard as h
os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),P4A_FIXTURE_OWNER='1',PYTHON_DOTENV_DISABLED='1')
c=h.configuration();h.verify_resource(c);h.verify_resource(c,'redis')
P=h.PRIVATE/'phase4b-20261021';E=h.RUN/'evidence/phase4b'
assert P.is_dir() and not (P/'accounts.json').exists()
for d in ('outbox','source-snapshot','pdfs'):(P/d).mkdir()
for d in ('workers','screenshots','cache'):(E/d).mkdir(exist_ok=True)
# Preserve every pre-existing unstaged/untracked frontend source and generated config.
git='/mnt/c/Program Files/Git/cmd/git.exe';win='C:/Users/60103/Documents/GitHub/'+F.name
def g(*a):return subprocess.run([git,'-C',win,*a],capture_output=True,check=True).stdout
paths=set(g('diff','--name-only','-z').decode().split('\0')+g('ls-files','--others','--exclude-standard','-z').decode().split('\0')+['next-env.d.ts','tsconfig.json'])
snapshot={}
for name in paths:
 if not name:continue
 src=F/name;dest=P/'source-snapshot'/name;dest.parent.mkdir(parents=True,exist_ok=True)
 b=src.read_bytes();dest.write_bytes(b);assert dest.read_bytes()==b
 snapshot[name]=hashlib.sha256(b).hexdigest()
(P/'source-snapshot/manifest.json').write_text(json.dumps(snapshot,indent=2))
(P/'frontend-index-before.patch').write_bytes(g('diff','--cached','--binary'))
from security.authentication import hash_password
run=uuid4().hex[:12];password='P4B!'+secrets.token_urlsafe(24)+'a9'
with h.connect_fixture() as db:
 ids=[]
 for n in ('Primary','Foreign'):
  t=db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,%s) RETURNING id,tenant_code",
   ('P4B Synthetic '+n+' '+run,uuid4().hex,'p4b.admin.'+uuid4().hex+'@example.test')).fetchone()
  ids.append(t)
 accounts={}
 for label,role,tenant in [('owner','accountant',ids[0]),('reviewer','local_admin',ids[0]),('unassigned','accountant',ids[0]),('foreign','accountant',ids[1])]:
  email='p4b.'+label+'.'+run+'@example.test'
  user=db.execute("INSERT INTO users(tenant_id,name,email,role,status,password_hash,must_change_password) VALUES (%s,%s,%s,%s,'active',%s,false) RETURNING id",
   (tenant['id'],'P4B Synthetic '+label,email,role,hash_password(password))).fetchone()
  accounts[label]={'id':str(user['id']),'email':email,'password':password,'role':role,'tenant_id':str(tenant['id']),'tenant_code':tenant['tenant_code']}
(P/'accounts.json').write_text(json.dumps(accounts,indent=2))
# Valid text-based PDFs generated locally without a package or external service.
def pdf(lines):
 text='BT /F1 12 Tf 50 780 Td 16 TL\n'+'\n'.join('('+x.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')+') Tj T*' for x in lines)+'\nET'
 stream=text.encode('ascii')
 objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
 b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
 b'<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
 out=bytearray(b'%PDF-1.4\n');offsets=[0]
 for i,obj in enumerate(objects,1):offsets.append(len(out));out+=str(i).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
 xref=len(out);out+=b'xref\n0 6\n0000000000 65535 f \n'
 for off in offsets[1:]:out+=f'{off:010d} 00000 n \n'.encode()
 out+=b'trailer << /Size 6 /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n'
 return bytes(out)
for kind,heading in [('invoice','INVOICE'),('missing-info','INVOICE'),('inconsistent-totals','INVOICE'),('bill','BILL'),('receipt','RECEIPT'),('unknown','MEETING NOTES'),('failed-once','INVOICE'),('real-docling-smoke','INVOICE')]:
 number='P4B-'+kind.upper()+'-'+run
 lines=[heading,'P4B SYNTHETIC TEST DOCUMENT - '+run,'Example Stationery Synthetic','Invoice Number: '+number,
 'Invoice Date: 2026-10-01','Currency: MYR','Bill To: Synthetic Acceptance Tenant','',
 'Description             Quantity    Unit Price    Amount','Synthetic stationery    2           25.00         50.00','',
 'Subtotal: 50.00','Tax: 0.00','Total: MYR 50.00']
 if kind=='missing-info':lines=[x for x in lines if not x.startswith('Invoice Number:')]
 if kind=='inconsistent-totals':lines[-1]='Total: MYR 60.00'
 if kind=='unknown':lines=[heading,'P4B SYNTHETIC TEST DOCUMENT - '+run,'Project planning discussion; no accounting fields.']
 (P/'pdfs'/(kind+'.pdf')).write_bytes(pdf(lines))
safe={k:{x:v for x,v in user.items() if x not in ('password','email')} for k,user in accounts.items()}
(E/'fixtures.json').write_text(json.dumps({'run':run,'accounts':safe,'pdfs':[p.name for p in (P/'pdfs').glob('*.pdf')],'frontend_snapshot_files':len(snapshot)},indent=2))
print(json.dumps({'fixture_setup':'PASS','synthetic_accounts':safe,'pdfs':8,'frontend_snapshot_files':len(snapshot)}))
