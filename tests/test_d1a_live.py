"""Opt-in real HTTP/RQ/CPU Docling/PostgreSQL D1A tests, no parser/auth mocks.

Run D1A API and worker, then set D1A_LIVE=1. Only the fingerprint-checked
disposable D1A resource is accepted. Credentials/cookies are never reported.
No schema initialization, cleanup/reset, AI approval or standardised record.
"""
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time
import unittest
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT/'runs/d1a'


def incorrect_pdf():
    """Small digital PDF fixture, financial total deliberately 99 instead of 50."""
    def text(x, y, content, size=12):
        content = content.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
        return f'BT /F1 {size} Tf {x} {y} Td ({content}) Tj ET\n'
    stream = text(45,790,'INVOICE - SYNTHETIC TEST DATA',18)
    for y, value in [(750,'Supplier: Example Stationery (Synthetic)'), (720,'Invoice number: SYN-BAD-001'),
                     (695,'Date: 2026-10-01'), (670,'Currency: MYR'), (505,'Subtotal: MYR 50.00'),
                     (480,'Tax amount: MYR 0.00'), (455,'Total amount: MYR 99.00'),
                     (410,'Invented fixture. Deliberate arithmetic error. Not a real transaction.')]:
        stream += text(45,y,value)
    stream += '0.5 w 45 540 510 90 re S\n45 585 m 555 585 l S\n'
    for x in (285,365,455):
        stream += f'{x} 540 m {x} 630 l S\n'
    for x, heading, value in [(55,'Description','Notebook'), (295,'Quantity','2'),
                               (375,'Unit price','25.00'), (465,'Line total','50.00')]:
        stream += text(x,603,heading,11) + text(x,558,value,11)
    data = stream.encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               f'<< /Length {len(data)} >>\nstream\n'.encode() + data + b'endstream']
    pdf, offsets = b'%PDF-1.4\n', [0]
    for index, obj in enumerate(objects,1):
        offsets.append(len(pdf))
        pdf += f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(pdf)
    pdf += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    pdf += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
    pdf += f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return pdf


@unittest.skipUnless(os.getenv('D1A_LIVE') == '1', 'Opt-in isolated D1A services required')
class D1ALiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts.d1a import configure
        configure()
        from auth.security import totp_code
        cls.accounts = json.loads((RESOURCE/'accounts.json').read_text())
        cls.evidence = {'authentication': [], 'documents': {}, 'security': {}, 'human_approval_performed': False}
        cls.clients = []
        for account in cls.accounts:
            client = httpx.Client(base_url='http://127.0.0.1:8002', timeout=20,
                                  headers={'X-Requested-With':'AccountingIntelligence'})
            cls.clients.append(client)
            response = client.post('/api/auth/login', json={'email':account['email'], 'password':account['password']})
            if response.status_code != 200:
                raise AssertionError(f'Login failed: HTTP {response.status_code}')
            login = response.json()
            if login.get('enrollmentRequired'):
                account['totp_secret'] = login['totpSecret']
                (RESOURCE/'accounts.json').write_text(json.dumps(cls.accounts, indent=2))
            response = client.post('/api/auth/verify-mfa', json={'code':totp_code(account['totp_secret'])})
            if response.status_code != 200:
                raise AssertionError(f'MFA failed: HTTP {response.status_code}')
            me = client.get('/api/auth/me')
            if me.status_code != 200 or me.json()['id'] != account['user_id']:
                raise AssertionError('Real session identity mismatch')
            cls.evidence['authentication'].append({'login':200, 'totp_mfa':200, 'session_me':200, 'role':me.json()['role']})
        cls.primary, cls.foreign = cls.clients
        cls.document_ids = {}
        cls.run_tag = uuid4().hex

    @classmethod
    def tearDownClass(cls):
        (RESOURCE/'live-evidence.json').write_text(json.dumps(cls.evidence, indent=2))
        for client in cls.clients:
            client.close()

    def upload_and_wait(self, name, payload):
        # A legal trailing PDF comment makes each synthetic run a new source
        # without changing invoice text or touching previously uploaded data.
        original_sha256 = hashlib.sha256(payload).hexdigest()
        payload += ('\n% D1A synthetic integration run ' + self.run_tag + '\n').encode('ascii')
        response = self.primary.post('/api/documents', params={'kind':'invoice','category':'accounts_payable'},
                                     files={'file':(name,payload,'application/pdf')})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertIsNotNone(body['queueJobId'], 'Upload was not dispatched to isolated Redis')
        document = body['id']
        self.document_ids[name] = document
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            detail = self.primary.get(f'/api/documents/{document}')
            self.assertEqual(detail.status_code,200,detail.text)
            status = detail.json()['status']
            if status in {'EXTRACTED','EXCEPTION','FAILED'}:
                break
            time.sleep(0.5)
        self.assertIn(status, {'EXTRACTED','EXCEPTION'}, f'Document did not complete: {status}')
        extraction = self.primary.get(f'/api/documents/{document}/extraction')
        validation = self.primary.get(f'/api/documents/{document}/validation')
        self.assertEqual(extraction.status_code,200,extraction.text)
        self.assertEqual(validation.status_code,200,validation.text)
        self.evidence['documents'][name] = {'upload_status':response.status_code, 'upload':body,
            'detail':detail.json(), 'extraction':extraction.json(), 'validation':validation.json(),
            'source_sha256':hashlib.sha256(payload).hexdigest(), 'fixture_sha256':original_sha256}
        self.assertEqual(self.primary.post('/api/records/from-document/'+document).status_code,422)
        return document, extraction.json(), validation.json()

    def test_01_complete_invoice_actual_extraction_and_review_ready(self):
        source = ROOT/'tests/extraction/data/development/synthetic_invoice_001.pdf'
        document, extraction, validation = self.upload_and_wait('SYN-001-d1a.pdf', source.read_bytes())
        fields = {field['key']:field for field in extraction['fields']}
        self.assertEqual(extraction['raw']['document_type']['value'],'invoice')
        self.assertEqual(extraction['raw']['document_type']['status'],'classified')
        self.assertEqual(fields['invoice']['value'],'SYN-001')
        self.assertEqual(fields['total']['value'],'50.00')
        self.assertEqual(fields['tax']['value'],'0.00')
        self.assertIsNone(fields['total']['confidence'])
        self.assertTrue(fields['total']['evidence'][0]['source_ref'])
        self.assertEqual(validation['validationStatus'],'PASS')
        self.assertEqual(validation['status'],'EXTRACTED')
        self.assertFalse(validation['blocking'])
        self.assertTrue(validation['reviewRequired'])
        self.assertFalse(validation['accountingApprovalPerformed'])
        line = extraction['lineItems'][0]
        self.assertEqual(Decimal(line['quantity']) * Decimal(line['unit_price']),Decimal(line['amount']))
        private = self.primary.get(f'/api/documents/{document}/file')
        self.assertEqual(private.status_code,200)
        self.assertEqual(hashlib.sha256(private.content).hexdigest(), self.evidence['documents']['SYN-001-d1a.pdf']['source_sha256'])

    def test_02_incorrect_invoice_actual_extraction_is_blocked(self):
        document, extraction, validation = self.upload_and_wait('SYN-BAD-001-d1a.pdf', incorrect_pdf())
        fields = {field['key']:field for field in extraction['fields']}
        self.assertEqual(fields['total']['value'],'99.00')
        self.assertEqual(validation['status'],'EXCEPTION')
        self.assertTrue(validation['blocking'])
        self.assertTrue(any(row['rule']=='EX-005' for row in validation['results']),validation)
        revalidate = self.primary.post(f'/api/documents/{document}/revalidate')
        self.assertEqual(revalidate.status_code,200,revalidate.text)
        self.assertEqual(revalidate.json()['status'],'EXCEPTION')
        self.assertFalse(revalidate.json()['accountingApprovalPerformed'])

    def test_03_cross_tenant_access_and_correction_are_denied(self):
        document = self.document_ids['SYN-001-d1a.pdf']
        before = self.primary.get(f'/api/documents/{document}/extraction').json()
        denied = {}
        for suffix in ('','/extraction','/validation','/file'):
            response = self.foreign.get(f'/api/documents/{document}'+suffix)
            self.assertEqual(response.status_code,404)
            denied[suffix or '/detail'] = response.status_code
        response = self.foreign.patch(f'/api/documents/{document}/extraction/invoice',
                    json={'value':'FOREIGN','expectedContentRevision':before['contentRevision']})
        self.assertEqual(response.status_code,404)
        self.assertEqual(self.primary.get(f'/api/documents/{document}/extraction').json(),before)
        self.evidence['security']['cross_tenant'] = {'get':denied,'patch':response.status_code,'data_unchanged':True}

    def test_04_real_correction_revision_audit_and_revalidation(self):
        document = self.document_ids['SYN-BAD-001-d1a.pdf']
        before = self.primary.get(f'/api/documents/{document}/extraction').json()
        response = self.primary.patch(f'/api/documents/{document}/extraction/total',
            json={'value':'50.00','expectedContentRevision':before['contentRevision']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['contentRevision'],before['contentRevision']+1)
        stale = self.primary.patch(f'/api/documents/{document}/extraction/total',
            json={'value':'1.00','expectedContentRevision':before['contentRevision']})
        self.assertEqual(stale.status_code,409)
        after = self.primary.get(f'/api/documents/{document}/extraction').json()
        total = next(field for field in after['fields'] if field['key']=='total')
        self.assertEqual(total['normalizedValue'],'99.00')
        self.assertEqual(total['currentValue'],'50.00')
        self.assertEqual(total['correctedBy'],self.accounts[0]['user_id'])
        revalidate = self.primary.post(f'/api/documents/{document}/revalidate')
        self.assertEqual(revalidate.status_code,200,revalidate.text)
        self.assertEqual(revalidate.json()['status'],'EXTRACTED')
        self.assertEqual(revalidate.json()['validationStatus'],'PASS')
        self.assertEqual(self.primary.post('/api/records/from-document/'+document).status_code,422)
        from database.connection import connect
        with connect() as db:
            corrections = db.execute("""SELECT fc.* FROM field_corrections fc JOIN field_predictions fp
                ON fp.id=fc.field_prediction_id WHERE fp.document_id=%s""",(document,)).fetchall()
            self.assertEqual(len(corrections),1)
            self.assertEqual((corrections[0]['old_value'],corrections[0]['new_value']),('99.00','50.00'))
        self.evidence['correction'] = {'response':response.json(),'stale_status':stale.status_code,
            'raw_normalized_total':total['normalizedValue'],'reviewed_current_total':total['currentValue'],
            'revalidation':revalidate.json(),'postgres_audit_rows':len(corrections)}

    def test_05_no_approved_records_and_queue_jobs_are_real(self):
        from database.connection import connect
        with connect() as db:
            for document in self.document_ids.values():
                jobs = db.execute('SELECT status FROM pending_jobs WHERE document_id=%s',(document,)).fetchall()
                self.assertEqual([row['status'] for row in jobs],['succeeded'])
                self.assertEqual(db.execute('SELECT count(*) AS n FROM standardised_records WHERE document_id=%s',(document,)).fetchone()['n'],0)
                self.assertEqual(db.execute('SELECT count(*) AS n FROM review_decisions WHERE document_id=%s',(document,)).fetchone()['n'],0)
        self.evidence['no_approved_records'] = True


if __name__ == '__main__':
    unittest.main()
