"""Run selected integration regressions with network and PostgreSQL connections denied.

No schema/service startup, models, package installs or existing runtime config.
Bank/PO modules use plain test functions; wrap them in unittest.FunctionTestCase.
"""
import ast
import importlib
import inspect
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
for key in ('DATABASE_URL', 'TEST_DATABASE_URL', 'C1_PG_CONFIG', 'P4A_PG_CONFIG', 'P4A_CONFIG', 'P4A_RUN_ID', 'P4A_FIXTURE_OWNER'):
    os.environ.pop(key, None)
os.environ.update({'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
                   'AUTH_SECRET': 'offline-synthetic-integration-secret-' * 2,
                   'FRONTEND_ORIGINS': 'http://localhost:3000', 'QWEN_ENABLED': 'false', 'PYTHON_DOTENV_DISABLED': '1'})

MODULES = [
    'tests.test_field_corrections', 'tests.test_accounting_contract',
    'tests.test_bill_receipt_validation', 'tests.test_document_validation', 'tests.test_m3_validation',
    'tests.test_phase4ar_remediation', 'tests.test_phase3e_attempts', 'tests.test_integration_safety', 'tests.test_phase3d_mutations', 'tests.test_auth', 'tests.test_postgresql_contract',
    'tests.extraction.test_adapter', 'tests.extraction.test_multidocument',
    'tests.extraction.test_classify_document', 'tests.extraction.test_combine_extraction',
    'tests.extraction.test_extraction_v1', 'tests.extraction.test_route_extraction',
    'tests.extraction.test_selective_qwen', 'tests.extraction.test_reconcile_selective',
    'tests.extraction.test_module2_public_result', 'tests.extraction.test_document_intelligence_service',
    'tests.extraction.test_p0_validation',
    'tests.test_bank_matching', 'tests.test_bank_reconciliation', 'tests.test_po_matching',
]

def denied(*args, **kwargs):
    raise AssertionError('Offline verification forbids network/database connections')

# Parse tracked source without importing app lifespan or executing SQL.
paths = [ROOT/'app.py', ROOT/'check_schema_compat.py']
for directory in ('auth','database','notifications','infrastructure','services','validation','tests','scripts'):
    paths.extend((ROOT/directory).rglob('*.py'))
for path in paths:
    if 'runs' not in path.parts:
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
print('STATIC AST PASS:', len(paths), 'Python files', flush=True)

import psycopg
loader = unittest.TestLoader()
suite = unittest.TestSuite()
with patch.object(socket.socket, 'connect', denied), patch.object(socket.socket, 'connect_ex', denied), patch.object(socket, 'create_connection', denied), patch.object(psycopg, 'connect', denied):
    for name in MODULES:
        module = importlib.import_module(name)
        suite.addTests(loader.loadTestsFromModule(module))
        for test_name, function in inspect.getmembers(module, inspect.isfunction):
            if test_name.startswith('test_') and function.__module__ == module.__name__:
                if inspect.signature(function).parameters:
                    raise RuntimeError('Unsupported test fixture: '+name+'.'+test_name)
                suite.addTest(unittest.FunctionTestCase(function, description=name+'.'+test_name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
print('RESULT ran=%d failures=%d errors=%d skipped=%d' % (result.testsRun, len(result.failures), len(result.errors), len(result.skipped)), flush=True)
print('PostgreSQL suites collected but blocked/skipped; live RQ/Docling/Qwen not run; test_execution needs an absent historical Docling artifact; pytest test_validation requires unavailable pytest.', flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
