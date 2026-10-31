"""Offline API/SQL regression tests, not PostgreSQL trigger/concurrency proof.

SQLite executes the endpoint's parameterized SQL against synthetic rows. Only
PostgreSQL placeholders, FOR UPDATE, now(), and RETURNING alias are adapted.
Minimal audit/immutability triggers model the inspected schema contract.
"""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch, mock_open

from fastapi.testclient import TestClient
# Upload parsing is unrelated to this JSON-only suite. The reused AI environment
# lacks python-multipart; bypass only its import-time upload registration check.
with patch('fastapi.dependencies.utils.ensure_multipart_is_installed'):
    import main as api
from services.routes.documents import routes as document_routes


DOCUMENT = '11111111-1111-4111-8111-111111111111'
OTHER_DOCUMENT = '22222222-2222-4222-8222-222222222222'
USER = {'id': 'accountant-a', 'tenant_id': 'tenant-a', 'role': 'accountant'}


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class OfflineDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:', check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.calls = []
        self.forced_document = None
        self.fail_revision_update = False
        self.connection.executescript('''
            CREATE TABLE documents (
                id TEXT PRIMARY KEY, tenant_id TEXT, owner_id TEXT, assigned_to TEXT,
                content_revision INTEGER, status TEXT, updated_at TEXT
            );
            CREATE TABLE raw_extractions (
                id TEXT PRIMARY KEY, document_id TEXT, is_current BOOLEAN
            );
            CREATE TABLE field_predictions (
                id TEXT PRIMARY KEY, document_id TEXT, extraction_id TEXT, field_key TEXT,
                current_value TEXT, raw_value TEXT, normalized_value TEXT,
                resolution TEXT CHECK (resolution IN
                    ('parser_preserved','candidate_for_review','unresolved_missing')),
                extraction_state TEXT, confidence TEXT, evidence TEXT,
                corrected_by TEXT, corrected_at TEXT,
                CHECK (resolution IS NOT 'candidate_for_review' OR current_value IS NULL OR corrected_at IS NOT NULL)
            );
            CREATE TABLE field_corrections (
                old_value TEXT, new_value TEXT, corrected_by TEXT, corrected_at TEXT
            );
            CREATE TRIGGER protect_predictions BEFORE UPDATE ON field_predictions
            WHEN NEW.raw_value IS NOT OLD.raw_value
              OR NEW.normalized_value IS NOT OLD.normalized_value
              OR NEW.resolution IS NOT OLD.resolution
              OR NEW.extraction_state IS NOT OLD.extraction_state
              OR NEW.confidence IS NOT OLD.confidence OR NEW.evidence IS NOT OLD.evidence
            BEGIN SELECT RAISE(ABORT, 'extracted values are immutable'); END;
            CREATE TRIGGER correction_history AFTER UPDATE ON field_predictions
            WHEN NEW.current_value IS NOT OLD.current_value
            BEGIN
                INSERT INTO field_corrections VALUES
                    (OLD.current_value, NEW.current_value, NEW.corrected_by, NEW.corrected_at);
            END;
        ''')
        for doc, tenant, owner, extraction in (
            (DOCUMENT, 'tenant-a', USER['id'], 'current-a'),
            (OTHER_DOCUMENT, 'tenant-b', 'accountant-b', 'current-b'),
        ):
            self.connection.execute('INSERT INTO documents VALUES (?,?,?,?,?,?,NULL)',
                                    (doc, tenant, owner, None, 7, 'validated'))
            self.connection.execute('INSERT INTO raw_extractions VALUES (?,?,true)', (extraction, doc))
            self.connection.execute('INSERT INTO field_predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                    (extraction+'-invoice', doc, extraction, 'invoice', 'OLD-001',
                                     'original source', 'OLD-001', 'parser_preserved', 'resolved',
                                     None, '[{"page":1}]', None, None))
        self.connection.execute('INSERT INTO raw_extractions VALUES (?,?,false)', ('historic-a', DOCUMENT))
        self.connection.execute('INSERT INTO field_predictions SELECT ?,document_id,?,field_key,?,raw_value,'
                                'normalized_value,resolution,extraction_state,confidence,evidence,NULL,NULL '
                                'FROM field_predictions WHERE id=?',
                                ('historic-invoice', 'historic-a', 'HISTORIC', 'current-a-invoice'))
        self.connection.commit()

    def __enter__(self):
        self.connection.execute('BEGIN')
        return self

    def __exit__(self, kind, _value, _traceback):
        self.connection.rollback() if kind else self.connection.commit()
        return False

    def execute(self, query, params=()):
        self.calls.append((query, params))
        if 'set_config(' in query:
            return Result()
        if self.forced_document is not None and query.lstrip().startswith('SELECT') and 'FROM documents' in query:
            return Result(self.forced_document)
        if self.fail_revision_update and query.lstrip().startswith('UPDATE documents'):
            raise RuntimeError('synthetic revision write failure')
        sql = query.replace('%s', '?').replace(' FOR UPDATE', '').replace('now()', 'CURRENT_TIMESTAMP')
        sql = sql.replace('RETURNING fp.*', 'RETURNING *')
        cursor = self.connection.execute(sql, params)
        row = cursor.fetchone() if cursor.description else None
        return Result(dict(row) if row is not None else None)

    def snapshot(self):
        return {table: [tuple(row) for row in self.connection.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('documents', 'field_predictions', 'field_corrections')}


class FieldCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.db = OfflineDatabase()
        self.user = copy.deepcopy(USER)
        self.addCleanup(self.db.connection.close)
        self.patch_db = patch.object(document_routes, 'connect', return_value=self.db)
        self.patch_user = patch.object(document_routes, 'require_workspace_user', side_effect=lambda _request, _roles: self.user)
        self.patch_startup = patch.object(api, 'apply_compatibility_migrations', side_effect=AssertionError('No startup SQL'))
        for patcher in (self.patch_db, self.patch_user, self.patch_startup):
            patcher.start()
            self.addCleanup(patcher.stop)
        # No context manager: TestClient must not enter the database-mutating lifespan.
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def correct(self, document=DOCUMENT, field='invoice', value='NEW-001', revision=7):
        return self.client.patch('/api/documents/'+document+'/extraction/'+field,
                                 json={'value': value, 'expectedContentRevision': revision},
                                 headers={'X-Requested-With': 'AccountingIntelligence',
                                          'Origin': 'http://localhost:3000'})

    def unchanged(self, before):
        self.assertEqual(before, self.db.snapshot())

    def test_accountant_corrects_current_extraction_only_and_invalidates_old_approval_revision(self):
        response = self.correct()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['contentRevision'], 8)
        self.assertEqual(response.json()['correctedBy'], USER['id'])
        rows = self.db.connection.execute('SELECT id,current_value FROM field_predictions WHERE document_id=?', (DOCUMENT,)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [('current-a-invoice', 'NEW-001'), ('historic-invoice', 'HISTORIC')])
        doc = self.db.connection.execute('SELECT content_revision,status FROM documents WHERE id=?', (DOCUMENT,)).fetchone()
        self.assertEqual(tuple(doc), (8, 'corrected'))

    def test_cross_tenant_request_changes_nothing(self):
        before = self.db.snapshot()
        self.assertEqual(self.correct(document=OTHER_DOCUMENT).status_code, 404)
        self.unchanged(before)

    def test_write_remains_tenant_scoped_even_if_prior_select_is_bypassed(self):
        self.db.forced_document = {'id': OTHER_DOCUMENT, 'content_revision': 7, 'status': 'validated'}
        before = self.db.snapshot()
        self.assertEqual(self.correct(document=OTHER_DOCUMENT).status_code, 404)
        self.unchanged(before)

    def test_same_tenant_unassigned_accountant_is_denied(self):
        self.user['id'] = 'different-accountant'
        before = self.db.snapshot()
        self.assertEqual(self.correct().status_code, 404)
        self.unchanged(before)

    def test_write_remains_owner_scoped_even_if_prior_select_is_bypassed(self):
        self.user['id'] = 'different-accountant'
        self.db.forced_document = {'id': DOCUMENT, 'content_revision': 7, 'status': 'validated'}
        before = self.db.snapshot()
        self.assertEqual(self.correct().status_code, 404)
        self.unchanged(before)

    def test_assigned_accountant_and_tenant_local_admin_can_correct(self):
        self.user['id'] = 'assigned-accountant'
        self.db.connection.execute('UPDATE documents SET assigned_to=? WHERE id=?', (self.user['id'], DOCUMENT))
        self.db.connection.commit()
        self.assertEqual(self.correct().status_code, 200)
        self.user.update(id='local-admin', role='local_admin')
        self.assertEqual(self.correct(value='NEXT-001', revision=8).status_code, 200)

    def test_invalid_field_names_are_rejected(self):
        before = self.db.snapshot()
        for name in ('raw_value', 'not_a_field', 'invoice;DROP'):
            with self.subTest(name=name):
                self.assertEqual(self.correct(field=name).status_code, 422)
                self.unchanged(before)

    def test_malformed_body_values_are_rejected(self):
        before = self.db.snapshot()
        for value in (None, 123, True, {}, [], '', '   ', '\x00', 'x'*4001):
            with self.subTest(value_type=type(value).__name__, length=len(value) if isinstance(value, str) else None):
                self.assertEqual(self.correct(value=value).status_code, 422)
                self.unchanged(before)

    def test_invalid_financial_date_and_currency_values_are_rejected(self):
        before = self.db.snapshot()
        for field, value in (('total', 'NaN'), ('total_amount', 'Infinity'), ('tax', 'money'),
                             ('date', '2026-02-30'), ('invoice_date', '19/10/2026'), ('currency', 'MY')):
            with self.subTest(field=field, value=value):
                self.assertEqual(self.correct(field=field, value=value).status_code, 422)
                self.unchanged(before)

    def test_stale_and_malformed_revisions_are_rejected(self):
        before = self.db.snapshot()
        self.assertEqual(self.correct(revision=6).status_code, 409)
        for revision in (None, 0, True, '7'):
            with self.subTest(revision=revision):
                self.assertEqual(self.correct(revision=revision).status_code, 422)
                self.unchanged(before)

    def test_revision_is_required(self):
        before = self.db.snapshot()
        response = self.client.patch('/api/documents/'+DOCUMENT+'/extraction/invoice',
                                     json={'value': 'NEW-001'},
                                     headers={'X-Requested-With': 'AccountingIntelligence'})
        self.assertEqual(response.status_code, 422)
        self.unchanged(before)

    def test_preserves_extraction_states_evidence_and_append_history(self):
        for index, resolution in enumerate(('parser_preserved', 'candidate_for_review', 'unresolved_missing', None)):
            with self.subTest(resolution=resolution):
                # Set initial fixtures without going through a correction trigger.
                self.db.connection.execute('DELETE FROM field_predictions WHERE id=?', ('state-'+str(index),))
                self.db.connection.execute('INSERT INTO field_predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                           ('state-'+str(index), DOCUMENT, 'current-a', 'reference', None,
                                            'source reference', 'normalized reference', resolution,
                                            'missing', None, '[{"page":1}]', None, None))
                self.db.connection.commit()
                before = dict(self.db.connection.execute('SELECT * FROM field_predictions WHERE id=?', ('state-'+str(index),)).fetchone())
                response = self.correct(field='reference', value='REF-'+str(index), revision=7+index)
                self.assertEqual(response.status_code, 200, response.text)
                after = dict(self.db.connection.execute('SELECT * FROM field_predictions WHERE id=?', ('state-'+str(index),)).fetchone())
                for key in ('raw_value', 'normalized_value', 'resolution', 'extraction_state', 'confidence', 'evidence'):
                    self.assertEqual(before[key], after[key])
                self.assertEqual(after['corrected_by'], USER['id'])
                self.assertIsNotNone(after['corrected_at'])
                self.db.connection.execute('DELETE FROM field_predictions WHERE id=?', ('state-'+str(index),))
                self.db.connection.commit()
        history = [tuple(row) for row in self.db.connection.execute('SELECT old_value,new_value,corrected_by FROM field_corrections')]
        self.assertEqual(history, [(None, 'REF-'+str(i), USER['id']) for i in range(4)])

    def test_consecutive_corrections_preserve_prior_audit_entry_and_each_actor(self):
        self.assertEqual(self.correct().status_code, 200)
        first = tuple(self.db.connection.execute('SELECT * FROM field_corrections').fetchone())
        self.user.update(id='tenant-admin', role='local_admin')
        self.assertEqual(self.correct(value='NEW-002', revision=8).status_code, 200)
        history = [tuple(row) for row in self.db.connection.execute('SELECT * FROM field_corrections ORDER BY rowid')]
        self.assertEqual(history[0], first)
        self.assertEqual(history[1][:3], ('NEW-001', 'NEW-002', 'tenant-admin'))
        self.assertEqual(history[0][:3], ('OLD-001', 'NEW-001', USER['id']))

    def test_valid_typed_values_remain_exact_strings(self):
        for index, (field, value) in enumerate((('total_amount', '-12345678901234567890.01'),
                                                 ('invoice_date', '2026-10-19'), ('currency', 'MYR'))):
            with self.subTest(field=field):
                self.db.connection.execute('UPDATE field_predictions SET field_key=? WHERE id=?',
                                           (field, 'current-a-invoice'))
                self.db.connection.commit()
                response = self.correct(field=field, value=value, revision=7+index)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['value'], value)

    def test_revision_write_failure_rolls_back_field_and_audit(self):
        before = self.db.snapshot()
        self.db.fail_revision_update = True
        self.assertEqual(self.correct().status_code, 503)
        self.unchanged(before)

    def test_completed_documents_cannot_be_corrected(self):
        for status in ('completed', 'standardised'):
            self.db.connection.execute('UPDATE documents SET status=? WHERE id=?', (status, DOCUMENT))
            self.db.connection.commit()
            before = self.db.snapshot()
            self.assertEqual(self.correct().status_code, 409)
            self.unchanged(before)

    def test_missing_or_historic_only_field_does_not_bump_revision(self):
        self.db.connection.execute('DELETE FROM field_predictions WHERE id=?', ('current-a-invoice',))
        self.db.connection.commit()
        before = self.db.snapshot()
        self.assertEqual(self.correct().status_code, 404)
        self.unchanged(before)

    def test_text_value_is_parameterized(self):
        value = "O'Reilly; UPDATE documents SET status='completed'"
        self.assertEqual(self.correct(value=value).status_code, 200)
        row = self.db.connection.execute('SELECT current_value FROM field_predictions WHERE id=?', ('current-a-invoice',)).fetchone()
        self.assertEqual(row['current_value'], value)


class PartyFieldCompatibilityTests(unittest.TestCase):
    """Real mapper/adapter/SQL/routes with in-memory storage, no service I/O."""
    def setUp(self):
        from tests.unit.accounting.test_accounting_contract import ContractDatabase, TENANT, USER as actor
        from services.extraction.persistence import persistence
        from services.queue.attempts import Claim, Delivery
        from tests.extraction.test_financial_layout import layout
        from tests.extraction.test_multidocument import contract
        self.db=ContractDatabase()
        self.addCleanup(self.db.db.close)
        self.user=copy.deepcopy(actor)
        self.tenant=TENANT
        self.values={'supplier_address':'10 Source Street\nExample City 10000',
                     'customer_address':'20 Buyer Road\nOther City 20000',
                     'supplier_tax_id':'TAX-S-001','customer_tax_id':'TAX-C-002'}
        self.raw,self.mapped=contract(layout('INVOICE',[
            (key.replace('_',' ')+': '+value,20,60+i*70,550,110+i*70)
            for i,(key,value) in enumerate(self.values.items())]))
        self.raw.update(document_id=DOCUMENT,provenance={'service_run_id':'synthetic-party-run'})
        for column in ('line_item_id','field_key','old_value','new_value','corrected_by','corrected_at'):
            self.db.db.execute('ALTER TABLE line_item_corrections ADD COLUMN '+column+' TEXT')
        self.db.db.executescript('''
            CREATE TABLE field_corrections(old_value TEXT,new_value TEXT,corrected_by TEXT,corrected_at TEXT);
            CREATE TRIGGER party_history AFTER UPDATE ON field_predictions
            WHEN NEW.current_value IS NOT OLD.current_value BEGIN
                INSERT INTO field_corrections VALUES(OLD.current_value,NEW.current_value,NEW.corrected_by,NEW.corrected_at);
            END;
            CREATE TRIGGER party_originals BEFORE UPDATE ON field_predictions
            WHEN NEW.raw_value IS NOT OLD.raw_value OR NEW.normalized_value IS NOT OLD.normalized_value
              OR NEW.evidence IS NOT OLD.evidence OR NEW.extraction_state IS NOT OLD.extraction_state
            BEGIN SELECT RAISE(ABORT,'immutable original prediction'); END;
        ''')
        def switch(extraction):
            self.db.db.execute('UPDATE raw_extractions SET is_current=(id=?)',(extraction,))
            return 1
        self.db.db.create_function('switch_current_extraction',1,switch)
        claim=Claim(Delivery(DOCUMENT,'job-1',TENANT,'synthetic-dispatch'),'synthetic-claim')
        document={'id':DOCUMENT,'tenant_id':TENANT}
        job={'id':'job-1','tenant_id':TENANT,'status':'processing'}
        with patch.object(persistence,'connect',return_value=self.db), \
             patch.object(persistence.attempts,'lock_document',return_value=document), \
             patch.object(persistence.attempts,'job_for_update',return_value=job), \
             patch.object(persistence.attempts,'lock_claim',return_value=(document,job)), \
             patch.object(persistence.attempts,'complete'), \
             patch('validation.documents.document_validation.validate_document'), \
             patch('builtins.open',mock_open(read_data=json.dumps(self.raw))):
            persistence.persist_extraction_result(DOCUMENT,{'service_run_id':'synthetic-party-run',
                'public_result_path':'synthetic-public-result.json'},claim=claim)
        for patcher in (patch.object(document_routes,'connect',return_value=self.db),
                        patch.object(api,'connect',return_value=self.db),
                        patch.object(api,'session_user',side_effect=lambda *_:self.user)):
            patcher.start(); self.addCleanup(patcher.stop)
        self.client=TestClient(api.app,raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def detail(self):
        return self.client.get('/api/documents/'+DOCUMENT+'/extraction')

    def correct(self,key,revision=2):
        return self.client.patch('/api/documents/'+DOCUMENT+'/extraction/'+key,
            json={'value':'Accountant confirmed value','expectedContentRevision':revision},
            headers={'X-Requested-With':'AccountingIntelligence','Origin':'http://localhost:3000'})

    def test_four_optional_fields_persist_display_and_correct_with_originals_and_history(self):
        before=self.detail()
        self.assertEqual(before.status_code,200,before.text)
        fields={f['key']:f for f in before.json()['fields']}
        for index,(key,value) in enumerate(self.values.items()):
            with self.subTest(field=key):
                self.assertEqual(fields[key]['value'],value)
                self.assertTrue(fields[key]['evidence'][0]['bbox'])
                self.assertIsNone(fields[key]['confidence'])
                original=self.db.execute('SELECT * FROM field_predictions WHERE field_key=%s',(key,)).fetchone()
                response=self.correct(key,2+index)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['contentRevision'],3+index)
                after=self.db.execute('SELECT * FROM field_predictions WHERE field_key=%s',(key,)).fetchone()
                for immutable in ('raw_value','normalized_value','evidence','candidates','extraction_state','resolution'):
                    self.assertEqual(after[immutable],original[immutable])
                self.assertEqual(after['corrected_by'],self.user['id'])
                self.assertEqual(after['current_value'],'Accountant confirmed value')
        self.assertEqual(len(self.db.execute('SELECT * FROM field_corrections').fetchall()),4)
        self.assertEqual(self.detail().json()['contentRevision'],6)

    def test_four_fields_preserve_tenant_owner_revision_role_and_standardisation_guards(self):
        for key in self.values:
            with self.subTest(field=key):
                self.user['tenant_id']='other-tenant'
                self.assertEqual(self.detail().status_code,404)
                self.assertEqual(self.correct(key).status_code,404)
                self.user['tenant_id']=self.tenant
                actor=self.user['id']; self.user['id']='unassigned-accountant'
                self.assertEqual(self.correct(key).status_code,404)
                self.user['id']=actor
                self.assertEqual(self.correct(key,1).status_code,409)
                self.user['role']='super_admin'
                self.assertEqual(self.correct(key).status_code,403)
                self.user['role']='accountant'
                self.db.db.execute("UPDATE documents SET status='standardised'"); self.db.db.commit()
                self.assertEqual(self.correct(key).status_code,409)
                self.db.db.execute("UPDATE documents SET status='extracted'"); self.db.db.commit()
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM field_corrections').fetchone()['n'],0)

    def test_existing_invoices_do_not_require_new_party_fields(self):
        from tests.extraction.test_financial_layout import layout
        from tests.extraction.test_multidocument import contract
        raw,mapped=contract(layout('INVOICE',[]))
        self.assertTrue(set(self.values).isdisjoint(raw['fields']))
        self.assertTrue(set(self.values).isdisjoint(f['field_key'] for f in mapped['fields']))

    def test_four_fields_allow_assigned_accountant_and_local_admin(self):
        for index,key in enumerate(self.values):
            with self.subTest(field=key):
                self.user['id']='assigned-accountant'; self.user['role']='accountant'
                self.db.db.execute('UPDATE documents SET assigned_to=?',(self.user['id'],)); self.db.db.commit()
                self.assertEqual(self.correct(key,2+index*2).status_code,200)
                self.user.update(id='tenant-admin',role='local_admin')
                self.assertEqual(self.correct(key,3+index*2).status_code,200)

    def test_four_fields_can_be_unresolved_null_without_becoming_required(self):
        from services.extraction.contracts.accounting_contract import map_m2_result
        raw=copy.deepcopy(self.raw)
        for key in self.values:
            raw['fields'][key].update(value=None,state='missing',candidates=[])
        fields={f['field_key']:f for f in map_m2_result(raw,DOCUMENT)['fields']}
        for key in self.values:
            self.assertIsNone(fields[key]['current_value'])
            self.assertEqual(fields[key]['extraction_state'],'missing')

    def test_optional_receipt_address_persists_once_and_uses_canonical_detail_and_correction(self):
        from services.extraction.persistence import persistence
        from services.queue.attempts import Claim,Delivery
        from services.extraction.contracts.accounting_contract import map_m2_result
        from tests.extraction.test_financial_layout import layout
        from tests.extraction.test_multidocument import contract
        raw, mapped = contract(layout('RECEIPT', [
            ('Merchant: Example Shop', 20, 50, 300, 70),
            ('Merchant address: 10 Printed Road\nSource City 12345', 20, 100, 500, 150)
        ]))

        address = next(
            f for f in mapped['fields']
            if f['field_key'] == 'merchant_address'
        )
        self.assertEqual(address['current_value'], '10 Printed Road\nSource City 12345')
        self.assertNotIn('supplier_address', [f['field_key'] for f in mapped['fields']])

        raw.update(document_id=DOCUMENT, provenance={'service_run_id': 'synthetic-merchant-run'})
        self.db.db.execute('DELETE FROM field_predictions')
        self.db.db.execute('DELETE FROM raw_extractions')
        self.db.db.execute("UPDATE documents SET content_revision=1,status='queued'")
        self.db.db.commit()

        claim = Claim(Delivery(DOCUMENT, 'job-1', self.tenant, 'synthetic-dispatch'), 'synthetic-claim')
        doc = {'id': DOCUMENT, 'tenant_id': self.tenant}
        job = {'id': 'job-1', 'tenant_id': self.tenant, 'status': 'processing'}

        with patch.object(persistence, 'connect', return_value=self.db), \
             patch.object(persistence.attempts, 'lock_document', return_value=doc), \
             patch.object(persistence.attempts, 'job_for_update', return_value=job), \
             patch.object(persistence.attempts, 'lock_claim', return_value=(doc, job)), \
             patch.object(persistence.attempts, 'complete'), \
             patch('validation.documents.document_validation.validate_document'), \
             patch('builtins.open', mock_open(read_data=json.dumps(raw))):
            persistence.persist_extraction_result(
                DOCUMENT,
                {'service_run_id': 'synthetic-merchant-run',
                 'public_result_path': 'synthetic-receipt.json'},
                claim=claim
            )

        fields = {f['key']: f for f in self.detail().json()['fields']}
        self.assertEqual(fields['merchant_address']['value'], address['current_value'])
        self.assertNotIn('supplier_address', fields)

        self.assertEqual(self.correct('merchant_address', 1).status_code, 409)

        self.user['tenant_id'] = 'other-tenant'
        self.assertEqual(self.correct('merchant_address').status_code, 404)
        self.user['tenant_id'] = self.tenant

        response = self.correct('merchant_address')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['key'], 'merchant_address')

        stored = self.db.execute(
            'SELECT * FROM field_predictions WHERE field_key=%s',
            ('merchant_address',)
        ).fetchall()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['raw_value'], address['raw_value'])
        self.assertEqual(stored[0]['evidence'], address['evidence'])
        self.assertEqual(len(self.db.execute('SELECT * FROM field_corrections').fetchall()), 1)

        unresolved = copy.deepcopy(raw)
        unresolved['fields']['merchant_address'].update(
            value=None, state='missing', candidates=[]
        )
        self.assertIsNone(
            next(f for f in map_m2_result(unresolved, DOCUMENT)['fields']
                 if f['field_key'] == 'merchant_address')['current_value']
        )

        # Invoice-only supplier_address must be rejected on receipts.
        collision = copy.deepcopy(raw)
        collision['fields']['supplier_address'] = copy.deepcopy(
            collision['fields']['merchant_address']
        )
        with self.assertRaisesRegex(ValueError, 'Field is outside the canonical document scope'):
            map_m2_result(collision, DOCUMENT)

if __name__ == '__main__':
    unittest.main()
