"""C1A/C1B integration against the explicitly initialized disposable PG18 DB.

Run only after approval and schema initialization; C1_PG_CONFIG selects the
ignored resource configuration. This suite never initializes/resets a schema.
Each test adds unique synthetic fixtures; it never deletes historical records.
Real session fixtures skip login/MFA delivery, not session/tenant/role checks.
Review fixtures use actual submit/decision endpoints with distinct actors;
these exercise approval gates, not M3 financial validation or human UI review.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import secrets
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

with patch('fastapi.dependencies.utils.ensure_multipart_is_installed'):
    import app as api
from auth.security import create_access_token
from services.extraction import persistence
from services.extraction.accounting_contract import record_values
from tests.c1pg_support import connect

FIXTURE = Path(__file__).parent / 'extraction/data/contracts/syn001_m2.json'


@unittest.skipUnless(os.getenv('C1_PG_CONFIG'), 'BLOCKED: explicitly initialized isolated C1-PG database required')
class PostgreSQLContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connect() as db:
            if not db.execute("SELECT to_regclass('public.field_predictions') AS table_name").fetchone()['table_name']:
                raise RuntimeError('C1-PG schema not initialized; explicit approval required')

    def setUp(self):
        self.patchers = [patch.object(api, 'connect', connect), patch.object(persistence, 'connect', connect),
                         patch.object(api, 'apply_compatibility_migrations', side_effect=AssertionError('No startup SQL')),
                         patch.dict(os.environ, {'AUTH_SECRET': secrets.token_urlsafe(48), 'COOKIE_SECURE': 'false'})]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with connect() as db:
            self.tenant = self.make_tenant(db)
            self.other_tenant = self.make_tenant(db)
            self.owner = self.make_user(db, self.tenant)
            self.assignee = self.make_user(db, self.tenant)
            self.unrelated = self.make_user(db, self.tenant)
            self.admin = self.make_user(db, self.tenant, 'local_admin')
            self.foreign = self.make_user(db, self.other_tenant, 'local_admin')
        self.document, self.result = self.document_fixture()
        self.persist()

    @staticmethod
    def make_tenant(db):
        key = uuid4().hex
        return db.execute("""INSERT INTO tenants(name,registration_number,admin_email)
            VALUES (%s,%s,%s) RETURNING id""", ('C1PG synthetic ' + key, key, key + '@example.test')).fetchone()['id']

    @staticmethod
    def make_user(db, tenant, role='accountant'):
        key = uuid4().hex
        return db.execute("""INSERT INTO users(tenant_id,name,email,role,status)
            VALUES (%s,%s,%s,%s,'active') RETURNING *""",
                          (tenant, 'C1PG synthetic ' + role, key + '@example.test', role)).fetchone()

    def document_fixture(self, category='accounts_payable'):
        document = str(uuid4())
        result = json.loads(FIXTURE.read_text())
        result['document_id'] = document
        result['provenance']['service_run_id'] = 'c1pg-' + uuid4().hex
        with connect() as db:
            db.execute("""INSERT INTO documents(id,tenant_id,owner_id,assigned_to,original_filename,kind,
                category,storage_path,mime_type,file_size_bytes,file_hash_sha256,status)
                VALUES (%s,%s,%s,%s,'SYN-001.pdf','invoice',%s,'synthetic/no-source-file',
                        'application/pdf',1,%s,'queued')""",
                       (document, self.tenant, self.owner['id'], self.assignee['id'], category, uuid4().hex * 2))
            db.execute('INSERT INTO pending_jobs(document_id,tenant_id) VALUES (%s,%s)', (document, self.tenant))
        return document, result

    def persist(self, result=None, document=None):
        value = self.result if result is None else result
        target = Path(self.temp.name) / (uuid4().hex + '.json')
        target.write_text(json.dumps(value))
        return persistence.persist_extraction_result(document or self.document, {
            'service_run_id': value['provenance']['service_run_id'], 'public_result_path': str(target)})

    def client(self, user=None):
        user = user or self.owner
        client = TestClient(api.app, raise_server_exceptions=False)  # no lifespan/startup
        self.addCleanup(client.close)
        with connect() as db:
            session, _refresh, _expires = api.issue_session(db, user)
        client.cookies.set(api.ACCESS, create_access_token(str(user['id']), session))
        client.headers['X-Requested-With'] = 'AccountingIntelligence'
        return client

    def correct(self, value='SYN-CORRECTED', key='invoice', revision=2, user=None, client=None):
        return (client or self.client(user)).patch(f'/api/documents/{self.document}/extraction/{key}',
                   json={'value': value, 'expectedContentRevision': revision})

    def field(self, key='invoice', current=True):
        with connect() as db:
            return db.execute("""SELECT fp.* FROM field_predictions fp JOIN raw_extractions re
                ON re.id=fp.extraction_id WHERE fp.document_id=%s AND fp.field_key=%s
                AND re.is_current=%s ORDER BY re.run_number DESC LIMIT 1""", (self.document, key, current)).fetchone()

    def state(self):
        with connect() as db:
            return db.execute('SELECT status,content_revision FROM documents WHERE id=%s', (self.document,)).fetchone()

    def history(self):
        with connect() as db:
            return db.execute("""SELECT fc.* FROM field_corrections fc JOIN field_predictions fp
                ON fp.id=fc.field_prediction_id WHERE fp.document_id=%s ORDER BY fc.corrected_at,fc.id""",
                              (self.document,)).fetchall()

    def review(self):
        revision = self.state()['content_revision']
        response = self.client().post(f'/api/documents/{self.document}/review',
                    json={'decision': 'submitted', 'expectedContentRevision': revision, 'note': 'Synthetic contract review'})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client(self.admin).post(f'/api/documents/{self.document}/review/decision',
                    json={'decision': 'approved', 'expectedContentRevision': revision, 'note': 'Synthetic contract decision'})
        self.assertEqual(response.status_code, 200, response.text)
        with connect() as db:
            decision = db.execute('SELECT * FROM review_decisions WHERE document_id=%s ORDER BY reviewed_at DESC LIMIT 1', (self.document,)).fetchone()
        self.assertEqual(decision['reviewed_by'], self.admin['id'])
        self.assertEqual(decision['approved_content_revision'], revision)

    def standardise(self):
        return self.client().post('/api/records/from-document/' + self.document)

    def record(self):
        with connect() as db:
            return db.execute('SELECT * FROM standardised_records WHERE document_id=%s', (self.document,)).fetchone()

    def test_same_tenant_correction_preserves_raw_state_revision_actor_and_audit(self):
        before = self.field()
        response = self.correct()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['contentRevision'], 3)
        after = self.field()
        self.assertEqual(after['current_value'], 'SYN-CORRECTED')
        self.assertEqual(after['corrected_by'], self.owner['id'])
        self.assertIsNotNone(after['corrected_at'])
        for key in ('raw_value', 'normalized_value', 'extraction_state', 'resolution', 'evidence', 'candidates', 'confidence'):
            self.assertEqual(after[key], before[key], key)
        audit = self.history()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]['old_value'], audit[0]['new_value'], audit[0]['corrected_by']),
                         ('SYN-001', 'SYN-CORRECTED', self.owner['id']))
        with connect() as db:
            status = db.execute("""SELECT * FROM document_status_history WHERE document_id=%s
                AND new_status='corrected'""", (self.document,)).fetchone()
        self.assertEqual(status['changed_by'], self.owner['id'])
        self.assertEqual(status['reason'], 'Extracted field corrected')

    def test_cross_tenant_denied_without_any_data_change(self):
        before = self.field(), self.state(), self.history()
        self.assertEqual(self.correct(user=self.foreign).status_code, 404)
        self.assertEqual((self.field(), self.state(), self.history()), before)

    def test_unassigned_accountant_denied_but_assignee_allowed(self):
        before = self.field(), self.state(), self.history()
        self.assertEqual(self.correct(user=self.unrelated).status_code, 404)
        self.assertEqual((self.field(), self.state(), self.history()), before)
        self.assertEqual(self.correct(user=self.assignee).status_code, 200)
        self.assertEqual(self.field()['corrected_by'], self.assignee['id'])

    def test_stale_revision_does_not_write(self):
        self.assertEqual(self.correct().status_code, 200)
        before = self.field(), self.state(), self.history()
        self.assertEqual(self.correct('STALE', revision=2).status_code, 409)
        self.assertEqual((self.field(), self.state(), self.history()), before)

    def test_concurrent_corrections_have_one_winner_and_one_stale_response(self):
        clients = [self.client(), self.client(self.assignee)]
        barrier = Barrier(2)
        def attempt(index):
            barrier.wait(timeout=5)
            return self.correct('RACE-' + str(index), client=clients[index])
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(attempt, range(2)))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        winner = next(r.json() for r in responses if r.status_code == 200)
        self.assertEqual(self.field()['current_value'], winner['value'])
        self.assertEqual(self.state()['content_revision'], 3)
        self.assertEqual(len(self.history()), 1)

    def test_invalid_fields_and_malformed_values_are_atomic(self):
        before = self.field(), self.state(), self.history()
        for key, value in [('resolution', 'verified'), ('total', 'NaN'), ('date', '2026-02-30'),
                           ('currency', 'myr'), ('invoice', ''), ('invoice', 123), ('invoice', 'bad\x00text')]:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.correct(value, key).status_code, 422)
                self.assertEqual((self.field(), self.state(), self.history()), before)

    def test_candidate_correction_satisfies_real_resolution_constraint(self):
        result = deepcopy(self.result)
        result['provenance']['service_run_id'] = 'c1pg-' + uuid4().hex
        cell = result['fields']['invoice_number']
        cell.update(value=None, state='ambiguous', resolution='candidate_for_review')
        cell['candidates'] = [{'value': 'AI-UNVERIFIED', 'source': 'ai', 'auto_accepted': False}]
        self.persist(result)
        self.assertIsNone(self.field()['current_value'])
        response = self.correct('REVIEWED', revision=3)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.field()['resolution'], 'candidate_for_review')
        self.assertEqual(self.field()['current_value'], 'REVIEWED')
        self.assertEqual(self.field()['candidates'], cell['candidates'])

    def test_audit_append_only_and_raw_prediction_protected_by_triggers(self):
        self.assertEqual(self.correct().status_code, 200)
        with connect() as db:
            for query, parameter in [
                ("UPDATE field_predictions SET raw_value='tampered' WHERE id=%s", self.field()['id']),
                ("UPDATE field_corrections SET new_value='tampered' WHERE id=%s", self.history()[0]['id']),
                ('DELETE FROM field_corrections WHERE id=%s', self.history()[0]['id'])]:
                with self.assertRaises(psycopg.errors.RaiseException):
                    with db.transaction():
                        db.execute(query, (parameter,))
        self.assertEqual(len(self.history()), 1)

    def test_real_sql_error_after_field_write_rolls_back_field_audit_and_revision(self):
        before = self.field(), self.state(), self.history()
        class FailAfterFieldWrite:
            def __init__(self):
                self.db = connect()
            def __enter__(self):
                self.db.__enter__()
                return self
            def __exit__(self, *args):
                return self.db.__exit__(*args)
            def execute(self, sql, params=()):
                if 'UPDATE documents AS d' in sql:
                    self.db.execute('SELECT 1/0')
                return self.db.execute(sql, params)
        client = self.client()
        with patch.object(api, 'connect', FailAfterFieldWrite):
            self.assertEqual(self.correct(client=client).status_code, 503)
        self.assertEqual((self.field(), self.state(), self.history()), before)

    def test_syn001_persistence_jsonb_evidence_nullable_confidence_and_line_numeric(self):
        with connect() as db:
            raw = db.execute('SELECT * FROM raw_extractions WHERE document_id=%s AND is_current', (self.document,)).fetchone()
            fields = db.execute('SELECT * FROM field_predictions WHERE extraction_id=%s', (raw['id'],)).fetchall()
            line = db.execute('SELECT * FROM line_items WHERE extraction_id=%s', (raw['id'],)).fetchone()
        self.assertEqual(raw['raw_json'], self.result)
        values = record_values(fields)
        self.assertEqual((values['supplier'], values['invoice'], values['date'], values['total']),
                         ('Example Stationery (Synthetic)', 'SYN-001', '2026-10-01', '50.00'))
        self.assertTrue(self.field()['evidence'])
        self.assertIsNone(self.field()['confidence'])
        self.assertEqual((line['quantity'], line['unit_price'], line['amount']),
                         (Decimal('2.000'), Decimal('25.0000'), Decimal('50.00')))
        self.assertEqual(line['quantity'] * line['unit_price'], line['amount'])
        self.assertTrue(line['evidence']['amount'])

    def test_real_review_gate_snapshot_and_numeric_zero(self):
        self.assertEqual(self.standardise().status_code, 422)
        self.review()
        response = self.standardise()
        self.assertEqual(response.status_code, 201, response.text)
        record = self.record()
        self.assertEqual(record['total_amount'], Decimal('50.00'))
        self.assertEqual(record['tax_amount'], Decimal('0.00'))
        self.assertIsNone(record['discount'])
        self.assertIsNone(record['buyer_name'])
        self.assertEqual(record['snapshot']['extraction'], self.result)
        self.assertEqual(record['snapshot']['contentRevision'], 2)
        self.assertEqual(record['snapshot']['lineItems'][0]['amount'], '50.00')
        self.assertNotIn('paymentStatus', record['snapshot'])

    def test_missing_number_and_currency_remain_null_and_block_records(self):
        result = deepcopy(self.result)
        result['provenance']['service_run_id'] = 'c1pg-' + uuid4().hex
        for key in ('invoice_number', 'currency'):
            result['fields'][key].update(value=None, state='missing', resolution='unresolved')
        self.persist(result)
        for key in ('invoice', 'currency'):
            self.assertIsNone(self.field(key)['current_value'])
            self.assertIsNone(self.field(key)['normalized_value'])
        self.review()
        self.assertEqual(self.standardise().status_code, 422)
        self.assertIsNone(self.record())

    def test_numeric_precision_and_zeroes_survive_postgresql(self):
        result = deepcopy(self.result)
        result['provenance']['service_run_id'] = 'c1pg-' + uuid4().hex
        for key, value in [('subtotal', '0.00'), ('tax_amount', '0.00'), ('total_amount', '0.00')]:
            result['fields'][key]['value'] = value
        line = result['line_items'][0]
        for key, value in [('quantity', '1.125'), ('unit_price', '0.0000'), ('line_total', '0.00')]:
            line[key]['value'] = value
        self.persist(result)
        self.review()
        self.assertEqual(self.standardise().status_code, 201)
        self.assertEqual(self.record()['total_amount'], Decimal('0.00'))
        with connect() as db:
            saved = db.execute('SELECT li.* FROM line_items li JOIN raw_extractions re ON re.id=li.extraction_id WHERE re.document_id=%s AND re.is_current', (self.document,)).fetchone()
        self.assertEqual(saved['quantity'], Decimal('1.125'))
        self.assertEqual(saved['unit_price'], Decimal('0.0000'))
        self.assertEqual(saved['amount'], Decimal('0.00'))

    def test_ai_candidate_is_jsonb_and_never_an_approved_value(self):
        result = deepcopy(self.result)
        result['provenance']['service_run_id'] = 'c1pg-' + uuid4().hex
        result['fields']['invoice_number'].update(value=None, state='ambiguous', resolution='candidate_for_review',
            candidates=[{'value': 'AI-GUESS', 'source': 'ai', 'auto_accepted': False}])
        self.persist(result)
        self.assertEqual(self.field()['candidates'][0]['value'], 'AI-GUESS')
        self.assertIsNone(self.field()['current_value'])
        self.review()
        self.assertEqual(self.standardise().status_code, 422)

    def test_fractional_line_and_money_precision_survive_record_creation(self):
        result = deepcopy(self.result)
        result['provenance']['service_run_id'] = 'c1pg-' + uuid4().hex
        for key in ('subtotal', 'total_amount'):
            result['fields'][key]['value'] = '28.26'
        for key, value in [('quantity', '1.125'), ('unit_price', '25.1200'), ('line_total', '28.26')]:
            result['line_items'][0][key]['value'] = value
        self.persist(result)
        self.review()
        self.assertEqual(self.standardise().status_code, 201)
        record = self.record()
        self.assertEqual(record['total_amount'], Decimal('28.26'))
        saved = record['snapshot']['lineItems'][0]
        self.assertEqual((saved['quantity'], saved['unit_price'], saved['amount']), ('1.125', '25.1200', '28.26'))

    def test_later_ai_run_does_not_overwrite_human_effective_value(self):
        self.assertEqual(self.correct().status_code, 200)
        corrected = self.field()
        result = deepcopy(self.result)
        result['provenance']['service_run_id'] = 'c1pg-' + uuid4().hex
        result['fields']['invoice_number']['value'] = 'LATER-AI'
        self.persist(result)
        current = self.field()
        self.assertEqual(current['normalized_value'], 'LATER-AI')
        self.assertEqual(current['current_value'], corrected['current_value'])
        self.assertEqual(current['corrected_by'], corrected['corrected_by'])
        self.assertEqual(current['corrected_at'], corrected['corrected_at'])
        self.assertEqual(self.field(current=False)['current_value'], 'SYN-CORRECTED')
        self.assertEqual(self.state()['content_revision'], 4)
        self.assertEqual(self.standardise().status_code, 422)  # new review still required

    def test_ap_ar_customer_requirement_remains_distinct(self):
        self.document, self.result = self.document_fixture('accounts_receivable')
        self.persist()
        self.review()
        self.assertEqual(self.standardise().status_code, 422)
        with connect() as db:
            self.assertEqual(db.execute('SELECT category FROM documents WHERE id=%s', (self.document,)).fetchone()['category'], 'accounts_receivable')
        self.assertIsNone(self.record())

    def test_correction_invalidates_prior_approval_until_fresh_review(self):
        self.review()
        self.assertEqual(self.correct().status_code, 200)
        self.assertEqual(self.standardise().status_code, 422)
        self.review()
        self.assertEqual(self.standardise().status_code, 201)
        self.assertEqual(self.record()['invoice_number'], 'SYN-CORRECTED')

    def test_approved_snapshot_and_record_cannot_be_changed(self):
        self.review()
        self.assertEqual(self.standardise().status_code, 201)
        before = self.record()
        with connect() as db:
            for sql in ["UPDATE standardised_records SET snapshot='{}'::jsonb WHERE id=%s",
                        'UPDATE standardised_records SET is_current=FALSE,total_amount=999.00 WHERE id=%s']:
                # Always roll back even if a defective trigger accepts the write.
                db.execute('SAVEPOINT immutable_probe')
                try:
                    with self.assertRaises(psycopg.errors.RaiseException):
                        db.execute(sql, (before['id'],))
                finally:
                    db.execute('ROLLBACK TO SAVEPOINT immutable_probe')
        self.assertEqual(self.record(), before)
        self.assertEqual(self.correct(revision=2).status_code, 409)
        with self.assertRaises(ValueError):
            self.persist()

    def test_postgresql_approval_trigger_rejects_unreviewed_record(self):
        with connect() as db:
            with self.assertRaises(psycopg.errors.RaiseException):
                with db.transaction():
                    db.execute("""INSERT INTO standardised_records(document_id,tenant_id,category,snapshot,created_by)
                        VALUES (%s,%s,'accounts_payable','{}'::jsonb,%s)""", (self.document, self.tenant, self.owner['id']))
        self.assertIsNone(self.record())


if __name__ == '__main__':
    unittest.main()
