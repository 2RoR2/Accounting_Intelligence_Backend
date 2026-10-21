import os,sys,json,time,shutil
from pathlib import Path
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020');sys.path.insert(0,str(R))
from tests import p4a_support as h
os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),P4A_FIXTURE_OWNER='1',PYTHON_DOTENV_DISABLED='1')
c=h.configuration();E=h.RUN/'evidence/phase4ca';P=h.PRIVATE/'phase4ca-20261021'
timings={}
for name in ('windows_interop','native_explicit_socket'):
 if name=='native_explicit_socket':
  from tests.p4ca.native_docker import enable
  enable()
 start=time.monotonic();h.verify_resource(c);h.verify_resource(c,'redis');timings[name]=round(time.monotonic()-start,3)
# Preserve the complete first browser failure before the bounded second run.
for source in (E/'browser-results.json',E/'screenshots/failure.png',E/'screenshots/01-review-history-enabled.png'):
 if source.exists():
  target=source.with_name('initial-'+source.name);assert not target.exists();shutil.copyfile(source,target)
fixture=json.loads((E/'browser-fixture.json').read_text())
target=h.RUN/'storage'/('p4ca-'+fixture['run']+'.pdf');assert not target.exists()
target.write_bytes((P/'pdfs/review.pdf').read_bytes())
with h.connect_fixture() as db:
 result=db.execute('UPDATE documents SET storage_path=%s WHERE id=%s AND tenant_id=%s AND content_revision=2 RETURNING id',
  (str(target),fixture['document'],fixture['accounts']['owner']['tenant_id'])).fetchone()
 assert str(result['id'])==fixture['document']
(E/'harness-diagnostic.json').write_text(json.dumps({'full_manifest_verification_seconds':timings,'same_assertions':True,'cache':False,'fixture_storage_root_corrected':True},indent=2))
print(json.dumps({'full_manifest_verification_seconds':timings,'same_assertions':True,'fixture_storage_root_corrected':True}))
