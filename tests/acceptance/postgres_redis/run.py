"""Allowlisted acceptance runner. Fails fast, retains resources/evidence."""
import argparse
from contextlib import redirect_stdout,redirect_stderr
import io
import json
import os
import re
import unittest

from tests.acceptance.support import phase4a_guard as h


def redact(text):
    text=re.sub(r'[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}','[REDACTED JWT]',text)
    # Assertions may contain cookie/OTP/secret values; never retain such lines.
    return '\n'.join('[REDACTED sensitive assertion context]' if re.search(
        r'(?i)password|token|secret|cookie|authorization|totp|mfa.code',line) else line for line in text.splitlines())


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--suite',choices=['pg','auth','runtime','rq'],required=True)
    args=parser.parse_args()
    os.environ['P4A_FIXTURE_OWNER']='1'
    h.prepare_runtime()
    names={'pg':'tests.integration.test_postgresql_contract.PostgreSQLContractTests',
           'auth':'tests.unit.security.test_security.AccountTests',
           'runtime':'tests.acceptance.postgres_redis.test_runtime.RuntimeTests',
           'rq':'tests.acceptance.postgres_redis.test_rq.RQTests'}
    suite=unittest.defaultTestLoader.loadTestsFromName(names[args.suite])
    buffer=io.StringIO()
    with redirect_stdout(buffer),redirect_stderr(buffer):
        result=unittest.TextTestRunner(stream=buffer,verbosity=2,failfast=True).run(suite)
    (h.RUN/'evidence'/(args.suite+'-tests.log')).write_text(redact(buffer.getvalue()))
    report={'suite':args.suite,'ran':result.testsRun,'passed':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
            'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
            'failure_ids':[test.id() for test,_ in result.failures+result.errors]}
    (h.RUN/'evidence'/(args.suite+'-result.json')).write_text(json.dumps(report,indent=2))
    print(json.dumps(report))
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)

if __name__=='__main__':main()
