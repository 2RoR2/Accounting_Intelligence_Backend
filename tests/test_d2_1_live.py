"""Opt-in real HTTP/RQ/Docling/PostgreSQL D2.1 tests; no parser/auth mocks.

Uses only the existing fingerprint-verified D1A synthetic tenants/resources.
Inherits the five unchanged D1A invoice/security/correction regressions.
"""
from decimal import Decimal
from copy import deepcopy
import json
from uuid import uuid4

from tests import test_d1a_live as baseline
from tests.extraction.synthetic_documents import fixtures


class D21LiveTests(baseline.D1ALiveTests):
    @classmethod
    def tearDownClass(cls):
        (baseline.RESOURCE / 'd2-1-live-evidence.json').write_text(json.dumps(cls.evidence, indent=2))
        for client in cls.clients:
            client.close()

    def test_06_content_profiles_actual_docling_persistence_and_api(self):
        expected = {
            'invoice_b.pdf': ('invoice', 'EXTRACTED', 'PASS', '48.00', 'Synthetic Services B'),
            'utility_bill.pdf': ('bill', 'EXTRACTED', 'REVIEW_REQUIRED', '80.00', 'Synthetic Utilities'),
            'retail_receipt.pdf': ('receipt', 'EXTRACTED', 'REVIEW_REQUIRED', '10.60', 'Synthetic Retail'),
            'unknown.pdf': ('unknown', 'EXCEPTION', 'REVIEW_REQUIRED', None, None),
        }
        from database.connection import connect
        for name, payload in fixtures().items():
            with self.subTest(pdf=name):
                # All uploads deliberately supply invoice as intake kind. Source
                # contents must override this hint for bills/receipts, never AP/AR.
                document, extraction, validation = self.upload_and_wait(name, payload)
                kind, status, validation_status, total, supplier = expected[name]
                classification = extraction['classification']
                self.assertEqual(classification['value'], kind)
                self.assertEqual(extraction['raw']['document_type'], classification)
                self.assertEqual(validation['status'], status)
                self.assertEqual(validation['validationStatus'], validation_status)
                self.assertTrue(validation['reviewRequired'])
                self.assertFalse(validation['accountingApprovalPerformed'])
                fields = {f['key']: f for f in extraction['fields']}
                if kind == 'unknown':
                    self.assertEqual(fields, {})
                    self.assertEqual(extraction['lineItems'], [])
                    self.assertTrue(validation['blocking'])
                else:
                    self.assertEqual(fields['total']['value'], total)
                    self.assertEqual(fields['supplier']['value'], supplier)
                    self.assertTrue(classification['candidates'][0]['evidence']['prov'])
                    for field in fields.values():
                        self.assertIsNone(field['confidence'])
                        if field['value'] is not None:
                            self.assertTrue(field['evidence'][0]['source_ref'])
                            self.assertIsNotNone(field['evidence'][0]['bbox'])
                    self.assertEqual(len(extraction['lineItems']), 1)
                if kind in {'bill', 'receipt'}:
                    self.assertEqual(validation['passed'], 0)
                    self.assertFalse(validation['coverageComplete'])
                    self.assertTrue(all(r['checkStatus'] == 'NOT_CHECKED' for r in validation['results']))
                    self.assertNotIn('invoice', fields)
                if kind == 'bill':
                    self.assertEqual(fields['reference']['value'], 'SYN-BILL-001')
                    self.assertEqual(fields['account_number']['value'], 'SYN-ACC-001')
                    self.assertEqual(fields['billing_period']['value'], '2026-09-01 to 2026-09-30')
                    self.assertEqual(fields['due']['value'], '2026-10-31')
                    self.assertIsNone(fields['tax']['value'])
                    self.assertIsNone(extraction['lineItems'][0]['quantity'])
                    self.assertIsNone(extraction['lineItems'][0]['unit_price'])
                elif kind == 'receipt':
                    self.assertEqual(fields['reference']['value'], 'SYN-R-001')
                    self.assertEqual(fields['transaction_time']['value'], '14:35:20')
                    self.assertEqual(fields['payment_method']['value'], 'Card')
                    self.assertEqual(fields['tax']['value'], '0.60')
                elif kind == 'invoice':
                    self.assertEqual(fields['invoice']['value'], 'SYN-B-002')
                    self.assertEqual(fields['discount']['value'], '5.00')
                    self.assertEqual(fields['customer']['value'], 'Synthetic Buyer')
                # Read the real NUMERIC/JSONB rows independently from API DTOs.
                with connect() as db:
                    stored = db.execute('SELECT * FROM raw_extractions WHERE document_id=%s AND is_current=true', (document,)).fetchone()
                    self.assertEqual(stored['raw_json'], extraction['raw'])
                    persisted = db.execute('SELECT kind,category,content_revision FROM documents WHERE id=%s', (document,)).fetchone()
                    if kind != 'unknown':
                        self.assertEqual(persisted['kind'], kind)
                        row = db.execute("SELECT * FROM line_items WHERE document_id=%s AND extraction_id=%s", (document, stored['id'])).fetchone()
                        self.assertIsInstance(row['amount'], Decimal)
                        self.assertEqual(row['amount'], Decimal(extraction['lineItems'][0]['amount']))
                        self.assertTrue(row['evidence']['amount'])
                        if kind != 'bill':
                            self.assertEqual(row['quantity'] * row['unit_price'], row['amount'])
                        else:
                            self.assertIsNone(row['quantity'])
                            self.assertIsNone(row['unit_price'])
                    self.assertEqual(persisted['category'], 'accounts_payable')  # explicit intake, not AI
                    self.assertEqual(persisted['content_revision'], extraction['contentRevision'])
                    self.assertEqual(db.execute('SELECT count(*) AS n FROM standardised_records WHERE document_id=%s', (document,)).fetchone()['n'], 0)
                self.evidence['documents'][name]['postgres_verified'] = True
                print(f'D2.1 {name}: {kind} / {status} / {validation_status}; total={total}; PG verified')

    def test_07_reextraction_cannot_hide_existing_human_corrections(self):
        from database.connection import connect
        from services.extraction.persistence import persist_extraction_result
        document = self.document_ids['SYN-BAD-001-d1a.pdf']
        before = self.primary.get(f'/api/documents/{document}/extraction').json()
        candidate = deepcopy(before['raw'])
        run_id = 'd2-1-regression-' + uuid4().hex
        candidate['provenance']['service_run_id'] = run_id
        path = baseline.RESOURCE / (run_id + '.json')
        path.write_text(json.dumps(candidate))
        with self.assertRaisesRegex(ValueError, 'Reviewed corrections exist'):
            persist_extraction_result(document, {'service_run_id': run_id, 'public_result_path': str(path)})
        self.assertEqual(self.primary.get(f'/api/documents/{document}/extraction').json(), before)
        with connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM raw_extractions WHERE document_id=%s', (document,)).fetchone()['n'], 1)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM field_corrections fc JOIN field_predictions fp ON fp.id=fc.field_prediction_id WHERE fp.document_id=%s', (document,)).fetchone()['n'], 1)
        self.evidence['correction']['reextraction_rejected_without_changes'] = True
