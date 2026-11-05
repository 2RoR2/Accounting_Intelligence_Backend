"""Opt-in real M3 API/Docling/PG acceptance in NEW disposable synthetic tenants.

No parser/auth mocks, approvals or changes to existing D1A/D2 documents. The one
explicit non-current extraction fixture is reference-query testing, not proof of
live extraction; every current extraction comes from an actual PDF upload.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import unittest
from uuid import uuid4

import httpx
from tests.live import test_document_upload_live as original
from tests.live import test_multidocument_live as multidocument
from tests.extraction.synthetic_documents import digital_pdf

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT/'runs/m3a'
PRESERVED = [
    '535dcfae-ba56-4b7c-a201-464f713b29e1','cd396203-4163-44c9-a068-305a2c5017fc',
    '99e77638-e665-44f9-a14e-31e6efe4797c','2c0b6be6-a0a5-45d6-b67c-724cd991c6a2',
    'ba304cac-d289-46d4-b2be-4decfdaa2e35','02431929-d43e-4b16-a0f3-b219bb5c3c3c',
    'b9412b3c-df41-4241-a197-d6fd59dc2332','dfa56e13-4620-4ab8-be84-4b709ecbf37e',
    'e5663203-05ec-4a01-9bf1-67a97c7213f2','d6259a8c-20e4-431c-be63-411670d37d74',
    '96fcf2ff-97e3-40c6-9b65-7b7ca1be10bf','5e7c7ab9-1dc9-4ab1-9b1a-ca8c8a812c54',
]


def snapshot_existing():
    from database.connection import connect
    with connect() as db:
        output = {}
        for table, order in [('documents','id'),('raw_extractions','id'),('field_predictions','id'),
                             ('line_items','id'),('exceptions','id')]:
            column = 'id' if table == 'documents' else 'document_id'
            rows = db.execute(f'SELECT * FROM {table} WHERE {column}=ANY(%s::uuid[]) ORDER BY {order}', (PRESERVED,)).fetchall()
            output[table] = rows
        if len(output['documents']) != len(PRESERVED):
            raise AssertionError('Expected previous demo documents are unavailable; inspect before proceeding')
    return hashlib.sha256(json.dumps(output, sort_keys=True, default=str).encode()).hexdigest()


def synthetic_invoice(number, *, supplier='Synthetic M3 Supplies Ltd', omit=None, **updates):
    line_amount = updates.pop('line_amount', '50.00')
    values = {'Supplier':supplier, 'Invoice number':number, 'Date':'2026-10-19', 'Due date':'2026-11-19',
              'Currency':'MYR', 'Subtotal':'50.00', 'Tax amount':'0.00', 'Total amount':'50.00'}
    values.update(updates)
    labels = [key + ': ' + value for key, value in values.items() if key != omit]
    return digital_pdf('INVOICE - SYNTHETIC TEST DATA', labels,
                       ['Description','Quantity','Unit price','Line total'],
                       ['Synthetic item','2','25.00',line_amount])


@unittest.skipUnless(os.getenv('M3A_LIVE') == '1', 'Explicit isolated M3A acceptance required')
class M3ALiveTests(multidocument.D21LiveTests):
    def upload_and_wait(self, name, payload):
        # Distinct source bytes exercise business duplicates, independently of
        # the unchanged exact-file hash rejection at upload.
        payload += ('\n% M3A synthetic physical copy ' + uuid4().hex + '\n').encode('ascii')
        return super().upload_and_wait(name,payload)

    @classmethod
    def setUpClass(cls):
        from scripts.d1a import configure
        configure()
        from security.authentication import hash_password, totp_code
        from database.connection import connect
        RESOURCE.mkdir(exist_ok=True)
        cls.m3run = uuid4().hex
        cls.private = RESOURCE/cls.m3run
        cls.private.mkdir()
        cls.before = snapshot_existing()
        accounts = []
        with connect() as db:
            for index in range(3):
                email = f'm3a-{cls.m3run}-{index}@example.test'
                tenant = db.execute('INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,%s) RETURNING id',
                    (f'M3A Synthetic {cls.m3run[:12]} {index}', f'SAIC-V2-M3A-SYNTHETIC-{cls.m3run}-{index}', email)).fetchone()['id']
                password = 'M3a!' + secrets.token_urlsafe(24)
                user = db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                    VALUES (%s,%s,%s,%s,'local_admin','active') RETURNING id""",
                    (tenant, f'M3A Synthetic Reviewer {index}', email, hash_password(password))).fetchone()['id']
                db.execute('INSERT INTO validation_rules(tenant_id) VALUES (%s)', (tenant,))
                accounts.append({'tenant_id':str(tenant),'user_id':str(user),'email':email,'password':password})
            # Explicitly synthetic catalogue, in a separate tenant only.
            db.execute('INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,%s)',
                       (accounts[2]['tenant_id'], 'Synthetic M3 Supplies Ltd'))
            db.execute('INSERT INTO vendors(tenant_id,vendor_name) VALUES (%s,%s)',
                       (accounts[2]['tenant_id'], 'Example Stationery (Synthetic)'))
        cls.account_path = cls.private/'accounts.json'
        cls.account_path.write_text(json.dumps(accounts[:2]))
        cls.account_path.chmod(0o600)
        super().setUpClass()
        cls.vendor_account = accounts[2]
        client = httpx.Client(base_url='http://127.0.0.1:8002', timeout=20,
                              headers={'X-Requested-With':'AccountingIntelligence'})
        cls.clients.append(client)
        cls.vendor = client
        response = client.post('/api/auth/login', json={'email':accounts[2]['email'],'password':accounts[2]['password']})
        if response.status_code != 200:
            raise AssertionError('Synthetic vendor-tenant login failed; credentials omitted')
        accounts[2]['totp_secret'] = response.json()['totpSecret']
        response = client.post('/api/auth/verify-mfa', json={'code':totp_code(accounts[2]['totp_secret'])})
        if response.status_code != 200:
            raise AssertionError('Synthetic vendor-tenant TOTP failed; code omitted')
        (cls.private/'vendor-account.json').write_text(json.dumps(accounts[2]))
        (cls.private/'vendor-account.json').chmod(0o600)
        cls.evidence['m3_cases'] = {}

    @classmethod
    def tearDownClass(cls):
        cls.evidence['previous_demo_data_unchanged'] = snapshot_existing() == cls.before
        (RESOURCE/'latest-live-evidence.json').write_text(json.dumps(cls.evidence, indent=2, default=str))
        for client in cls.clients:
            client.close()

    def upload_for(self, client, name, pdf):
        previous = self.primary
        try:
            self.primary = client
            return self.upload_and_wait(name, pdf)
        finally:
            self.primary = previous

    def correct(self, client, document, key, value):
        before = client.get(f'/api/documents/{document}/extraction').json()
        response = client.patch(f'/api/documents/{document}/extraction/{key}',
            json={'value':value,'expectedContentRevision':before['contentRevision']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['contentRevision'],before['contentRevision']+1)

    def persisted(self, document, code, severity):
        from database.connection import connect
        with connect() as db:
            rows = db.execute("SELECT * FROM exceptions WHERE document_id=%s AND type=%s AND status<>'resolved'",
                              (document,'VALIDATION:'+code)).fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['severity'],severity)
            self.assertTrue(rows[0]['message'])

    def revalidate_pass(self, client, document):
        response = client.post(f'/api/documents/{document}/revalidate')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['validationStatus'],'PASS',response.text)
        self.assertEqual(response.json()['status'],'EXTRACTED')
        self.assertTrue(response.json()['reviewRequired'])
        self.assertFalse(response.json()['accountingApprovalPerformed'])
        return response.json()

    def test_08_ex001_through_ex006_real_pdfs_and_corrections(self):
        cases = [
            ('EX-001','medium',{'omit':'Invoice number'}, [('invoice','M3A-REVIEWED-MISSING')]),
            ('EX-002','medium',{'Currency':'RM'}, [('currency','MYR')]),
            ('EX-003','high',{'line_amount':'49.00','Subtotal':'49.00','Total amount':'49.00'}, [('subtotal','50.00'),('total','50.00')]),
            ('EX-004','high',{'Subtotal':'40.00','Total amount':'40.00'}, [('subtotal','50.00'),('total','50.00')]),
            ('EX-005','high',{'Total amount':'99.00'}, [('total','50.00')]),
            ('EX-006','medium',{'Due date':'2026-10-01'}, [('due','2026-11-19')]),
        ]
        for code, severity, options, corrections in cases:
            with self.subTest(code=code):
                document, extraction, report = self.upload_and_wait(code+'.pdf', synthetic_invoice('M3A-'+code, **options))
                errors = [r for r in report['results'] if r.get('code')]
                self.assertEqual({r['rule'] for r in errors}, {code}, report)
                self.assertEqual(errors[0]['severity'],severity)
                self.assertEqual(errors[0]['result'],'FAIL' if severity=='high' else 'WARNING')
                self.assertEqual(report['blocking'],severity=='high')
                self.assertEqual(report['status'],'EXCEPTION')
                self.assertEqual(report['validationStatus'],'REVIEW_REQUIRED')
                if code == 'EX-001':
                    self.assertEqual(next(r for r in report['results'] if r['rule']=='EX-007')['checkStatus'], 'NOT_CHECKED')
                self.persisted(document,code,severity)
                if code == 'EX-003':
                    line = extraction['lineItems'][0]
                    response = self.primary.patch(f"/api/documents/{document}/line-items/{line['id']}/amount", json={'value':'50.00'})
                    self.assertEqual(response.status_code,200,response.text)
                for key, value in corrections:
                    self.correct(self.primary,document,key,value)
                after = self.revalidate_pass(self.primary,document)
                from database.connection import connect
                with connect() as db:
                    history = db.execute('SELECT status FROM exceptions WHERE document_id=%s AND type=%s', (document,'VALIDATION:'+code)).fetchall()
                    self.assertTrue(history)
                    self.assertTrue(all(row['status']=='resolved' for row in history))
                self.evidence['m3_cases'][code] = {'id':document,'initial':report,'after_correction':after,'persisted_severity':severity}
                print(f'M3A {code}: live PDF/API/PG verified, severity={severity}, corrected -> PASS')

    def test_09_duplicate_identity_current_history_and_tenant_boundaries(self):
        from database.connection import connect
        number = 'M3A-BUSINESS-DUP'
        first, _, report = self.upload_and_wait('duplicate-first.pdf', synthetic_invoice(number))
        self.assertEqual(report['validationStatus'],'PASS')
        duplicate, _, report = self.upload_and_wait('duplicate-second.pdf', synthetic_invoice(number))
        error = next(r for r in report['results'] if r['rule']=='EX-007')
        self.assertEqual(error['duplicate_of'],first)
        self.assertEqual(error['severity'],'high')
        self.assertTrue(report['blocking'])
        self.persisted(duplicate,'EX-007','high')
        self.correct(self.primary,duplicate,'invoice','M3A-REVIEWED-UNIQUE')
        self.revalidate_pass(self.primary,duplicate)
        # Same key exists only in the other tenant: no EX-007 in the target.
        self.upload_for(self.foreign,'foreign-key.pdf',synthetic_invoice('M3A-CROSS-TENANT'))
        unique, extraction, report = self.upload_and_wait('cross-tenant-target.pdf',synthetic_invoice('M3A-CROSS-TENANT'))
        self.assertEqual(report['validationStatus'],'PASS',report)
        # Explicit obsolete reference fixture; current source remains real Docling.
        with connect() as db:
            obsolete = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json,schema_version)
                VALUES (%s,2,false,'human_review',%s,'M3A-SYNTHETIC-OBSOLETE-REFERENCE') RETURNING id""",
                (duplicate, json.dumps({'fixture':'M3A synthetic non-current reference, not live extraction'}))).fetchone()['id']
            for key, value in [('supplier','Synthetic M3 Supplies Ltd'),('invoice','M3A-CROSS-TENANT')]:
                db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,extraction_state,resolution,current_value,normalized_value)
                    VALUES (%s,%s,%s,%s,'resolved','parser_preserved',%s,%s)""", (obsolete,duplicate,key,key,value,value))
        self.revalidate_pass(self.primary,unique)
        # Original raw invoice number on a corrected reference cannot match either.
        self.correct(self.primary,first,'invoice','M3A-HISTORIC-CORRECTED')
        target, _, report = self.upload_and_wait('corrected-reference-target.pdf',synthetic_invoice(number))
        self.assertEqual(report['validationStatus'],'PASS',report)
        denied = self.foreign.post(f'/api/documents/{target}/revalidate')
        self.assertEqual(denied.status_code,404)
        self.assertEqual(self.foreign.get(f'/api/documents/{target}/validation').status_code,404)
        self.evidence['m3_cases']['EX-007'] = {'duplicate':duplicate,'reference':first,'initial':error,
            'corrected_unique':True,'cross_tenant_excluded':True,'non_current_extraction_excluded':True,
            'corrected_reference_effective_value_used':True,'cross_tenant_revalidate':404}

    def test_10_ex008_uses_only_real_tenant_authorized_synthetic_catalogue(self):
        for suffix, supplier in [('fuzzy','Synthetic M3 Supplies'),('unknown','Entirely Other Synthetic Retailer')]:
            document, extraction, report = self.upload_for(self.vendor,'vendor-'+suffix+'.pdf',
                synthetic_invoice('M3A-VENDOR-'+suffix,supplier=supplier))
            error = next(r for r in report['results'] if r['rule']=='EX-008')
            self.assertEqual(error['severity'],'medium')
            self.assertFalse(report['blocking'])
            self.assertEqual(report['validationStatus'],'REVIEW_REQUIRED')
            self.assertEqual(report['status'],'EXCEPTION')
            self.assertIn('similarity', error['vendor_candidate'])
            self.assertNotIn('confidence', error['vendor_candidate'])
            self.assertTrue(all(field['confidence'] is None for field in extraction['fields']))
            self.persisted(document,'EX-008','medium')
            self.correct(self.vendor,document,'supplier','Synthetic M3 Supplies Ltd')
            self.revalidate_pass(self.vendor,document)
            self.evidence['m3_cases']['EX-008-'+suffix] = {'id':document,'initial':report,'corrected_exact_match':True}

    def test_11_no_catalogue_means_not_checked_even_if_another_tenant_has_vendors(self):
        document = self.document_ids['SYN-001-d1a.pdf']
        report = self.primary.get(f'/api/documents/{document}/validation').json()
        self.assertEqual(report['validationStatus'],'PASS',report)
        vendor = next(r for r in report['results'] if r['rule']=='EX-008')
        self.assertEqual(vendor['checkStatus'],'NOT_CHECKED')
        self.assertEqual(report['vendorMatch']['status'],'NOT_CHECKED')
        self.assertFalse(any(r.get('code')=='EX-008' for r in report['results']))
        self.evidence['vendor_catalogue_isolation'] = {'same_tenant_catalogue_absent':'NOT_CHECKED','foreign_catalogue_ignored':True}

    def test_12_existing_d21_and_d22_demo_sources_values_revisions_and_exceptions_are_untouched(self):
        self.assertEqual(snapshot_existing(),self.before)
        self.assertEqual(self.primary.get('/api/health').status_code,200)
        self.evidence['previous_demo_data_unchanged'] = True

