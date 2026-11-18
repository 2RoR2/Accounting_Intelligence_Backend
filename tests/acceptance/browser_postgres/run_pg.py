"""Only five approved real PostgreSQL review-history cases, never the full suite."""
import os,sys,json,unittest,io
from pathlib import Path
R=Path('/mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2_Integration_20261020')
sys.path.insert(0,str(R))
from tests.acceptance.support import phase4a_guard as h
os.environ.update(P4A_RUN_ID=h.RUN_ID,P4A_CONFIG=str(h.CONFIG),P4A_FIXTURE_OWNER='1',PYTHON_DOTENV_DISABLED='1')
E=h.RUN/'evidence/phase4ca'
assert json.loads((E/'preflight.json').read_text())['result']=='PASS'
h.prepare_runtime()
from tests.acceptance.browser_postgres.test_history_pg import HistoryPostgreSQLTests
from tests.acceptance.postgres_redis.run import redact
names=[n for n in dir(HistoryPostgreSQLTests) if n.startswith('test_history_')]
assert len(names)==5
records=[]
class Result(unittest.TextTestResult):
 def stopTest(self,test):
  status='FAIL' if any(t==test for t,_ in self.failures+self.errors) else 'SKIP' if any(t==test for t,_ in self.skipped) else 'PASS'
  records.append({'test':test.id(),'status':status,'document':getattr(test,'document',None),'tenant':str(getattr(test,'tenant',''))})
  print(json.dumps(records[-1]),file=sys.__stdout__,flush=True)
  super().stopTest(test)
stream=io.StringIO()
result=unittest.TextTestRunner(stream=stream,resultclass=Result,verbosity=2,failfast=True).run(unittest.TestSuite(HistoryPostgreSQLTests(n) for n in names))
(E/'postgresql-recheck.log').write_text(redact(stream.getvalue()))
report={'cases':records,'PASS':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),'FAIL':len(result.failures)+len(result.errors),'SKIP':len(result.skipped),'BLOCKED':len(names)-result.testsRun,'role':h.RUNTIME,'synthetic_extraction':True,'authentication':'real database sessions; HTTP cookies issued by test helper, not browser MFA'}
(E/'postgresql-recheck-results.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
