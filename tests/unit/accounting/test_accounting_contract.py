"""Synthetic M2 -> real persistence/standardization SQL contract tests.

SYN-001 fixture was derived from the verified C0 Docling JSON, using the real
mapper/adapters. Reviews below are explicit synthetic DB fixtures; no approval
endpoint is patched. SQLite cannot prove PostgreSQL constraints or locking.
"""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
with patch('fastapi.dependencies.utils.ensure_multipart_is_installed'):
    import main as api
from services.extraction.persistence import persistence
from services.queue.attempts import Claim, Delivery
from services.extraction.contracts.accounting_contract import (
    VERSION, decimal_text, map_m2_result, record_values,
)

DOCUMENT = '11111111-1111-4111-8111-111111111111'
TENANT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
USER = {'id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'tenant_id': TENANT, 'role': 'accountant'}
# Stable synthetic pending-job identity shared by the phase-3 queue contracts.
JOB = '11111111-1111-4111-8111-111111111113'
DISPATCH_TOKEN = '22222222-2222-4222-8222-222222222222'
CLAIM_TOKEN = '33333333-3333-4333-8333-333333333333'
FIXTURE = Path(__file__).parents[2]/'extraction/data/contracts/syn001_m2.json'


def fixture():
    return json.loads(FIXTURE.read_text())


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class ContractDatabase:
    """Execute production SQL; adapt only unsupported PostgreSQL syntax/types."""
    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.calls = []
        self.db.executescript('''
        CREATE TABLE documents (id TEXT PRIMARY KEY, tenant_id TEXT, owner_id TEXT, assigned_to TEXT,
          kind TEXT, category TEXT, status TEXT, content_revision INTEGER,
          uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT, processing_seconds TEXT, processing_completed_at TEXT);
        CREATE TABLE pending_jobs (id TEXT PRIMARY KEY, tenant_id TEXT, document_id TEXT, status TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT, last_error TEXT, updated_at TEXT,
          dispatch_token TEXT, claim_token TEXT, dispatch_expires_at TEXT, processing_expires_at TEXT,
          attempt_count INTEGER DEFAULT 1);
        CREATE TABLE raw_extractions (id TEXT PRIMARY KEY, document_id TEXT, run_number INTEGER, is_current BOOLEAN,
          routing_decision TEXT, service_run_id TEXT UNIQUE, schema_version TEXT, pending_job_id TEXT, raw_json TEXT);
        CREATE TABLE field_predictions (id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
          extraction_id TEXT, document_id TEXT, field_key TEXT, field_label TEXT, extraction_state TEXT,
          resolution TEXT, raw_value TEXT, normalized_value TEXT, current_value TEXT, confidence TEXT,
          evidence TEXT, candidates TEXT, extraction_method TEXT, model_name TEXT, model_version TEXT,
          schema_version TEXT, corrected_by TEXT, corrected_at TEXT);
        CREATE TABLE line_items (id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), extraction_id TEXT,
          document_id TEXT, line_number INTEGER, description TEXT, quantity TEXT, unit_price TEXT, amount TEXT,
          field_states TEXT, evidence TEXT, candidates TEXT);
        CREATE TABLE line_item_corrections (document_id TEXT);
        CREATE TABLE audit_logs (tenant_id TEXT, user_name TEXT, role TEXT, action TEXT, resource TEXT, details TEXT);
        CREATE TABLE vendors (id TEXT, tenant_id TEXT, vendor_name TEXT);
        CREATE TABLE review_decisions (document_id TEXT, tenant_id TEXT, decision TEXT, approved_content_revision INTEGER);
        CREATE TABLE validation_rules (tenant_id TEXT, require_supplier BOOLEAN, require_invoice_number_for_invoice BOOLEAN,
          require_reference_for_bill BOOLEAN, require_receipt_identifier BOOLEAN);
        CREATE TABLE exceptions (document_id TEXT, tenant_id TEXT, severity TEXT, status TEXT DEFAULT 'open',
          type TEXT, message TEXT, note TEXT, resolved_at TEXT);
        CREATE TABLE validation_results (document_id TEXT, tenant_id TEXT, content_revision INTEGER,
          rule_id TEXT, rule_source TEXT, rule_version TEXT, document_type TEXT, status TEXT,
          severity TEXT, field_key TEXT, prevents_approval BOOLEAN, requires_review BOOLEAN, message TEXT);
        CREATE TABLE standardised_records (id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
          record_code TEXT DEFAULT 'REC-SYNTHETIC', document_id TEXT, tenant_id TEXT, category TEXT,
          vendor_id TEXT, vendor_match_confidence TEXT, supplier_name TEXT, invoice_number TEXT, buyer_name TEXT, reference_number TEXT, invoice_date TEXT,
          due_date TEXT, currency TEXT, total_amount TEXT, subtotal TEXT, tax_amount TEXT, discount TEXT,
          validation_status TEXT, snapshot TEXT, created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          revision_number INTEGER DEFAULT 1);
        ''')
        self.db.execute('INSERT INTO documents(id,tenant_id,owner_id,kind,category,status,content_revision) VALUES (?,?,?,?,?,?,?)',
                        (DOCUMENT, TENANT, USER['id'], 'invoice', 'accounts_payable', 'processing', 1))
        self.db.execute('''INSERT INTO pending_jobs
          (id,tenant_id,document_id,status,dispatch_token,claim_token,dispatch_expires_at,processing_expires_at)
          VALUES (?,?,?,?,?,?,datetime('now','+1 hour'),datetime('now','+1 hour'))''',
          (JOB, TENANT, DOCUMENT, 'processing', DISPATCH_TOKEN, CLAIM_TOKEN))
        self.db.commit()

    def __enter__(self):
        self.db.execute('BEGIN')
        return self

    def __exit__(self, kind, _value, _traceback):
        self.db.rollback() if kind else self.db.commit()
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if 'set_config(' in sql:
            return Rows([])
        if 'switch_current_extraction(' in sql:
            extraction_id = str(params[0])
            document_id = self.db.execute(
                'SELECT document_id FROM raw_extractions WHERE id=?', (extraction_id,)).fetchone()[0]
            self.db.execute('UPDATE raw_extractions SET is_current=FALSE WHERE document_id=?', (document_id,))
            self.db.execute('UPDATE raw_extractions SET is_current=TRUE WHERE id=?', (extraction_id,))
            return Rows([])
        sql = (sql.replace(' FOR UPDATE', '').replace('%s', '?')
               .replace('clock_timestamp()', 'CURRENT_TIMESTAMP')
               .replace('now()', 'CURRENT_TIMESTAMP'))
        sql = re.sub(r'EXTRACT\(EPOCH FROM \(CURRENT_TIMESTAMP - uploaded_at\)\)', '0', sql)
        sql = sql.replace('::date', '').replace('::numeric', '').replace('::text', '').replace('RETURNING fp.*', 'RETURNING *')
        params = [json.dumps(p.obj) if isinstance(p, Jsonb) else str(p) if isinstance(p, (Decimal, UUID)) else p for p in params]
        cursor = self.db.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        for row in rows:
            for key in ('raw_json', 'snapshot', 'evidence', 'candidates', 'field_states'):
                if row.get(key) is not None:
                    row[key] = json.loads(row[key])
        return Rows(rows)


class AccountingContractTests(unittest.TestCase):
    def setUp(self):
        self.database = ContractDatabase()
        self.addCleanup(self.database.db.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.claim = Claim(Delivery(DOCUMENT, JOB, TENANT, DISPATCH_TOKEN), CLAIM_TOKEN)
        self.patchers = [patch.object(persistence, 'connect', return_value=self.database),
                         patch.object(api, 'connect', return_value=self.database),
                         patch.object(api, 'require_workspace_user', return_value=USER),
                         patch.object(api, 'session_user', return_value=USER),
                         patch.object(api, 'apply_compatibility_migrations', side_effect=AssertionError('No startup SQL'))]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        # FastAPI stores dependency callables when routes are created; patching
        # the module symbol alone does not replace those captured callables.
        def authenticated_user():
            return USER
        for route in api.app.routes:
            for dependency in getattr(getattr(route, 'dependant', None), 'dependencies', ()):
                if getattr(dependency.call, '__name__', '') == 'require_workspace_user':
                    api.app.dependency_overrides[dependency.call] = authenticated_user
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def persist(self, result=None):
        result = fixture() if result is None else result
        target = Path(self.temp.name)/'public.json'
        target.write_text(json.dumps(result))
        return persistence.persist_extraction_result(DOCUMENT, {'service_run_id': result['provenance']['service_run_id'],
                                                                'public_result_path': str(target)}, claim=self.claim)

    def reviewed_fixture(self, revision=None):
        # Explicit synthetic starting state for exercising the unchanged gate SQL.
        actual = self.database.db.execute('SELECT content_revision FROM documents WHERE id=?', (DOCUMENT,)).fetchone()[0]
        self.database.db.execute("UPDATE documents SET status='validated' WHERE id=?", (DOCUMENT,))
        self.database.db.execute('INSERT INTO review_decisions VALUES (?,?,?,?)',
                                 (DOCUMENT, TENANT, 'approved', actual if revision is None else revision))
        self.database.db.commit()

    def standardise(self):
        return self.client.post('/api/records/from-document/'+DOCUMENT,
                                headers={'X-Requested-With': 'AccountingIntelligence'})

    def saved_record(self):
        return self.database.execute('SELECT * FROM standardised_records').fetchone()

    def test_verified_syn001_reaches_existing_standardization_contract(self):
        original = fixture()
        self.persist(original)
        self.assertEqual(self.database.execute('SELECT raw_json FROM raw_extractions').fetchone()['raw_json'], original)
        self.reviewed_fixture()
        response = self.standardise()
        self.assertEqual(response.status_code, 201, response.text)
        record = self.saved_record()
        self.assertEqual((record['supplier_name'], record['invoice_number'], record['invoice_date'], record['total_amount']),
                         ('Example Stationery (Synthetic)', 'SYN-001', '2026-10-01', '50.00'))
        self.assertIsNone(record['buyer_name'])
        self.assertIsNone(record['discount'])

    def test_document_reference_stays_out_of_invoice_number_column(self):
        result = fixture()
        result['fields']['reference_number'] = deepcopy(result['fields']['invoice_number'])
        result['fields']['reference_number']['value'] = 'REF-001'
        self.persist(result)
        self.reviewed_fixture()

        response = self.standardise()

        self.assertEqual(response.status_code, 201, response.text)
        record = self.saved_record()
        self.assertEqual(record['invoice_number'], 'SYN-001')
        self.assertEqual(record['reference_number'], 'REF-001')
        self.assertEqual(record['tax_amount'], '0.00')
        snapshot = record['snapshot']
        self.assertEqual(snapshot['schemaVersion'], VERSION)
        self.assertEqual(snapshot['contentRevision'], 2)
        self.assertEqual(snapshot['lineItems'][0]['amount'], '50.00')
        self.assertTrue(snapshot['lineItems'][0]['evidence']['amount'])
        self.assertEqual(snapshot['extraction'], original)
        self.assertNotIn('paymentStatus', snapshot)
        line_write = next(params for sql, params in self.database.calls if 'INSERT INTO line_items' in sql)
        self.assertTrue(all(isinstance(value, Decimal) for value in line_write[4:7]))

    def test_missing_number_and_currency_remain_null_and_block_standardization(self):
        for name in ('invoice_number', 'currency'):
            with self.subTest(name=name):
                value = fixture()
                value['fields'][name].update(value=None, state='missing', resolution='unresolved')
                mapped = map_m2_result(value, DOCUMENT)
                field = next(row for row in mapped['fields'] if row['field_key'] == {'invoice_number': 'invoice', 'currency': 'currency'}[name])
                self.assertIsNone(field['current_value'])
                self.assertIsNone(field['normalized_value'])
        missing = fixture()
        missing['fields']['currency'].update(value=None, state='missing', resolution='unresolved')
        self.persist(missing)
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 422)
        self.assertIsNone(self.saved_record())

    def test_zero_and_decimal_precision_are_preserved_without_floats(self):
        self.assertEqual(decimal_text('0.00'), '0.00')
        self.assertEqual(decimal_text('1,234.50'), '1234.50')
        result = fixture()
        for name in ('subtotal', 'tax_amount', 'total_amount'):
            result['fields'][name]['value'] = '0.00'
        result['line_items'][0]['unit_price']['value'] = '0.00'
        result['line_items'][0]['line_total']['value'] = '0.00'
        self.persist(result)
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 201)
        record = self.saved_record()
        self.assertEqual(record['total_amount'], '0.00')
        self.assertEqual(record['snapshot']['lineItems'][0]['amount'], '0.00')

    def test_malformed_values_rejected_before_any_persistence(self):
        for value in (1.1, True, 'NaN', 'Infinity', '12,34.50', '1e3', '1.001', '1000000000000.00'):
            with self.subTest(value=value):
                result = fixture()
                result['fields']['total_amount']['value'] = value
                with self.assertRaises(ValueError):
                    self.persist(result)
                self.assertIsNone(self.database.execute('SELECT * FROM raw_extractions').fetchone())
        result = fixture()
        result['line_items'][0]['quantity']['value'] = 2.0
        with self.assertRaises(ValueError):
            self.persist(result)

    def test_unknown_kind_and_direction_are_not_defaulted(self):
        result = fixture()
        result['document_type'].update(value='unknown', status='unknown')
        mapped = map_m2_result(result, DOCUMENT)
        self.assertEqual(mapped['document_kind']['value'], 'unknown')
        self.assertIsNone(mapped['category'])
        self.persist(result)
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 422)

    def test_missing_invoice_number_blocks_record_even_when_raw_text_exists(self):
        result = fixture()
        result['fields']['invoice_number'].update(value=None, state='missing', resolution='unresolved')
        self.persist(result)
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 422)
        self.assertIsNone(self.saved_record())

    def test_unknown_direction_blocks_even_when_document_is_classified(self):
        self.persist()
        self.database.db.execute('UPDATE documents SET category=NULL WHERE id=?', (DOCUMENT,))
        self.database.db.commit()
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 422)
        self.assertIsNone(self.saved_record())

    def test_unreviewed_vlm_values_and_line_candidates_are_never_promoted(self):
        result = fixture()
        result['fields']['invoice_number'].update(method='VLM', value='AI-ONLY', resolution='parser_preserved')
        result['line_items'][0]['line_total'].update(method='VLM')
        mapped = map_m2_result(result, DOCUMENT)
        self.assertIsNone(next(row for row in mapped['fields'] if row['field_key'] == 'invoice')['current_value'])
        self.assertIsNone(mapped['line_items'][0]['amount'])
        self.persist(result)
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 422)

    def test_malformed_states_dates_currency_and_candidates_are_rejected(self):
        for key, update in (('invoice_date', {'value': '2026-02-30'}), ('currency', {'value': 'myr'}),
                            ('invoice_number', {'state': 'guessed'}), ('invoice_number', {'method': 'guess'}),
                            ('total_amount', {'confidence': float('nan')}),
                            ('invoice_number', {'candidates': [{'value': 'AUTO', 'auto_accepted': True}]})):
            with self.subTest(key=key, update=update):
                result = fixture()
                result['fields'][key].update(update)
                with self.assertRaises(ValueError):
                    map_m2_result(result, DOCUMENT)
    def test_explicit_receivable_direction_is_preserved(self):
        self.persist()
        self.database.db.execute("UPDATE documents SET category='accounts_receivable' WHERE id=?", (DOCUMENT,))
        self.database.db.commit()
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 422)  # AR still requires its buyer.
        self.database.db.execute("INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,extraction_state,resolution,current_value,corrected_by,corrected_at) SELECT id,?,'customer','Customer','missing','unresolved_missing','Reviewed synthetic buyer',?,CURRENT_TIMESTAMP FROM raw_extractions", (DOCUMENT, USER['id']))
        self.database.db.execute("UPDATE documents SET content_revision=content_revision+1,status='corrected' WHERE id=?", (DOCUMENT,))
        self.database.db.commit()
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 201)
        self.assertEqual(self.saved_record()['category'], 'accounts_receivable')

    def test_qwen_candidate_never_becomes_effective_value_or_approved_record(self):
        result = fixture()
        result['fields']['invoice_number'].update(value=None, state='missing', ai_candidate='AI-GUESS', resolution='candidate_for_review')
        self.persist(result)
        row = self.database.execute("SELECT * FROM field_predictions WHERE field_key='invoice'").fetchone()
        self.assertIsNone(record_values([row])['invoice'])
        self.assertEqual(row['candidates'][-1], {'source': 'ai', 'value': 'AI-GUESS', 'auto_accepted': False})
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 422)

    def test_raw_fallback_cannot_promote_missing_or_candidate_fields(self):
        result = fixture()
        result['fields']['invoice_number'].update(value=None, state='missing', raw_text='unverified number')
        self.persist(result)
        row = self.database.execute("SELECT * FROM field_predictions WHERE field_key='invoice'").fetchone()
        self.assertIsNone(record_values([row])['invoice'])

    def test_corrected_current_value_wins_and_requires_current_revision_review(self):
        self.persist()
        self.reviewed_fixture()
        response = self.client.patch('/api/documents/'+DOCUMENT+'/extraction/invoice',
                                     json={'value': 'REVIEWED-002', 'expectedContentRevision': 2},
                                     headers={'X-Requested-With': 'AccountingIntelligence'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['contentRevision'], 3)
        self.reviewed_fixture(revision=2)
        self.assertEqual(self.standardise().status_code, 422)
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 201)
        record = self.saved_record()
        self.assertEqual(record['invoice_number'], 'REVIEWED-002')
        field = next(row for row in record['snapshot']['fieldPredictions'] if row['key'] == 'invoice')
        self.assertEqual(field['normalizedValue'], 'SYN-001')
        self.assertEqual(field['currentValue'], 'REVIEWED-002')
        self.assertEqual(field['correctedBy'], USER['id'])
        self.assertEqual(record['snapshot']['contentRevision'], 3)

    def test_approval_and_blocking_exception_gates_remain_enforced(self):
        self.persist()
        self.assertEqual(self.standardise().status_code, 422)
        self.database.db.execute("UPDATE documents SET status='validated' WHERE id=?", (DOCUMENT,))
        self.database.db.commit()
        self.assertEqual(self.standardise().status_code, 422)
        self.reviewed_fixture()
        self.database.db.execute('INSERT INTO exceptions(document_id,tenant_id,severity,status) VALUES (?,?,?,?)', (DOCUMENT, TENANT, 'high', 'open'))
        self.database.db.commit()
        self.assertEqual(self.standardise().status_code, 422)
        self.assertIsNone(self.saved_record())

    def test_existing_canonical_seed_shapes_and_snapshots_are_unchanged(self):
        # Populate the existing mock_data.sql snapshot shape as a fixture, never
        # mutate an approved snapshot through production code.
        self.database.db.execute('''INSERT INTO standardised_records
            (document_id,tenant_id,category,invoice_number,currency,total_amount,validation_status,snapshot)
            VALUES (?,?,?,?,?,?,?,?)''', (DOCUMENT, TENANT, 'accounts_receivable', 'INV-2026-1001',
                                        'MYR', '1250.00', 'passed', json.dumps({'fixture_id': 'DEMO-DOC-0001'})))
        self.database.db.commit()
        before = self.saved_record()
        response = self.client.get('/api/records/'+before['id'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['snapshot'], {'fixture_id': 'DEMO-DOC-0001'})
        self.assertEqual(response.json()['totalAmount'], '1250.00')
        with self.assertRaises(ValueError):
            self.persist()
        self.assertEqual(self.saved_record(), before)

    def test_legacy_seed_field_rows_still_standardize_without_rewriting_keys(self):
        self.persist()
        self.database.db.execute("UPDATE raw_extractions SET raw_json=?", (json.dumps({'fixture_id': 'SYNTHETIC-LEGACY'}),))
        self.database.db.execute("INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,extraction_state,resolution,current_value,normalized_value) SELECT id,?,'customer','Customer','resolved','parser_preserved','Synthetic buyer','Synthetic buyer' FROM raw_extractions", (DOCUMENT,))
        self.database.db.commit()
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 201)
        self.assertEqual(self.saved_record()['buyer_name'], 'Synthetic buyer')

    def test_idempotent_run_does_not_invalidate_revision_twice(self):
        first = self.persist()
        second = self.persist()
        self.assertTrue(second['idempotent'])
        self.assertEqual(first['extraction_id'], second['extraction_id'])
        self.assertEqual(self.database.execute('SELECT content_revision FROM documents').fetchone()['content_revision'], 2)

    def test_new_run_cannot_hide_a_human_field_correction(self):
        self.persist()
        self.database.db.execute("UPDATE field_predictions SET current_value='REVIEWED',corrected_by=?,corrected_at=CURRENT_TIMESTAMP WHERE field_key='invoice'", (USER['id'],))
        self.database.db.commit()
        result = fixture()
        result['provenance']['service_run_id'] = 'new-run-after-correction'
        with self.assertRaisesRegex(ValueError, 'Reviewed corrections exist'):
            self.persist(result)
        self.assertEqual(self.database.execute('SELECT count(*) AS n FROM raw_extractions').fetchone()['n'], 1)
        self.assertEqual(self.database.execute("SELECT current_value FROM field_predictions WHERE field_key='invoice'").fetchone()['current_value'], 'REVIEWED')

    def test_new_run_cannot_hide_line_correction_history(self):
        self.persist()
        self.database.db.execute('INSERT INTO line_item_corrections(document_id) VALUES (?)', (DOCUMENT,))
        self.database.db.commit()
        result = fixture()
        result['provenance']['service_run_id'] = 'new-run-after-line-correction'
        with self.assertRaisesRegex(ValueError, 'Reviewed corrections exist'):
            self.persist(result)
        self.assertEqual(self.database.execute('SELECT content_revision FROM documents').fetchone()['content_revision'], 2)

    def test_extraction_dto_keeps_null_confidence_evidence_revision_and_decimal_strings(self):
        self.persist()
        response = self.client.get('/api/documents/'+DOCUMENT+'/extraction')
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        field = next(row for row in data['fields'] if row['key'] == 'tax')
        self.assertEqual(field['value'], '0.00')
        self.assertIsNone(field['confidence'])
        self.assertTrue(field['evidence'])
        self.assertEqual(data['contentRevision'], 2)
        self.assertEqual(data['lineItems'][0]['quantity'], '2')

    def test_unsupported_contract_and_alias_collisions_rejected(self):
        for change in ({'schema_version': 'future'}, {'review_required': False}, {'accounting_approval_performed': True},
                       {'safety': []}, {'provenance': []}, {'document_type': None}, {'status': 'approved'}):
            result = fixture()
            result.update(change)
            with self.assertRaises(ValueError):
                map_m2_result(result, DOCUMENT)
        result = fixture()
        result['fields']['supplier'] = deepcopy(result['fields']['supplier_name'])
        with self.assertRaises(ValueError):
            map_m2_result(result, DOCUMENT)

    def test_existing_long_keys_normalize_at_same_boundary_without_renaming_rows(self):
        self.persist()
        self.database.db.execute("UPDATE field_predictions SET field_key='invoice_number' WHERE field_key='invoice'")
        self.database.db.commit()
        self.reviewed_fixture()
        self.assertEqual(self.standardise().status_code, 201)
        self.assertEqual(self.saved_record()['invoice_number'], 'SYN-001')
        self.assertIsNotNone(self.database.execute("SELECT * FROM field_predictions WHERE field_key='invoice_number'").fetchone())


if __name__ == '__main__':
    unittest.main()
