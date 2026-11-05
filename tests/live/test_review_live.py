"""Opt-in real bill/receipt PDFs, PostgreSQL, TOTP and HTTP acceptance.

Inherits all M3A EX-001..008 and six-layout regressions. Only fresh synthetic
tenants are written; original demo and prior M3A documents are hash preserved.
"""
import hashlib
import json
import os
import unittest

from tests.live import test_validation_live as invoice
from tests.extraction.synthetic_documents import digital_pdf


def financial_pdf(kind, *, omit=None, line_amount=None, quantity='2', unit_price='5.00', **updates):
    if kind == 'receipt':
        values = {'Merchant':'Synthetic M3B Retail', 'Transaction date':'2026-10-19',
                  'Transaction time':'14:35:20', 'Currency':'MYR', 'Subtotal':'10.00',
                  'Tax':'0.60', 'Total':'10.60', 'Payment method':'Card'}
        amount = line_amount if line_amount is not None else '10.00'
    else:
        values = {'Issuer':'Synthetic M3B Utilities', 'Bill reference':'M3B-BILL',
                  'Billing period':'2026-09-01 to 2026-09-30', 'Bill date':'2026-10-01',
                  'Due date':'2026-10-31', 'Currency':'MYR', 'Subtotal':'80.00', 'Amount due':'60.00'}
        amount = line_amount if line_amount is not None else '80.00'
        quantity, unit_price = '2', '40.00'
    values.update(updates)
    return digital_pdf(kind.upper() + ' - SYNTHETIC TEST DATA',
        [key + ': ' + value for key, value in values.items() if key != omit],
        ['Description','Quantity','Unit price','Line total'],
        ['Synthetic item', quantity, unit_price, amount])


def snapshot_documents(ids):
    from database.connection import connect
    with connect() as db:
        rows = {}
        for table in ('documents','raw_extractions','field_predictions','line_items','exceptions'):
            column = 'id' if table == 'documents' else 'document_id'
            rows[table] = db.execute(f'SELECT * FROM {table} WHERE {column}=ANY(%s::uuid[]) ORDER BY id', (ids,)).fetchall()
        if len(rows['documents']) != len(ids):
            raise AssertionError('Previous M3A documents missing; do not proceed')
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


@unittest.skipUnless(os.getenv('M3B_LIVE') == '1', 'Explicit isolated M3B acceptance required')
class M3BLiveTests(invoice.M3ALiveTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        previous = json.loads((invoice.RESOURCE/'latest-live-evidence.json').read_text())
        cls.preserved_m3a = list({row['upload']['id'] for row in previous['documents'].values()})
        cls.before_m3a = snapshot_documents(cls.preserved_m3a)
        cls.evidence['m3b_cases'] = {}
        cls.pdf_dir = invoice.ROOT/'runs/m3b'/cls.m3run/'pdfs'
        cls.pdf_dir.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        cls.evidence['previous_demo_data_unchanged'] = invoice.snapshot_existing() == cls.before
        cls.evidence['previous_m3a_data_unchanged'] = snapshot_documents(cls.preserved_m3a) == cls.before_m3a
        cls.evidence['private_account_path'] = str(cls.account_path)
        (invoice.ROOT/'runs/m3b/latest-live-evidence.json').write_text(json.dumps(cls.evidence, indent=2, default=str))
        for client in cls.clients:
            client.close()

    def upload_and_wait(self, name, payload):
        # Keep every actual synthetic input for reproducibility, not notebook JSON.
        (self.pdf_dir/name).write_bytes(payload)
        return super().upload_and_wait(name,payload)

    def no_open_findings(self, document):
        from database.connection import connect
        with connect() as db:
            rows = db.execute("SELECT * FROM exceptions WHERE document_id=%s AND type LIKE 'VALIDATION:%%'", (document,)).fetchall()
            self.assertTrue(rows)
            self.assertTrue(all(row['status']=='resolved' and row['resolved_at'] for row in rows))
            self.assertEqual(db.execute('SELECT count(*) AS n FROM standardised_records WHERE document_id=%s', (document,)).fetchone()['n'],0)
        return len(rows)

    def revalidate_review(self, document):
        response = self.primary.post(f'/api/documents/{document}/revalidate')
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual(result['errors'],[])
        self.assertEqual(result['status'],'EXTRACTED')
        self.assertEqual(result['validationStatus'],'REVIEW_REQUIRED')
        self.assertFalse(result['coverageComplete'])
        return result

    def test_13_original_bill_and_receipt_supported_checks_and_persistence(self):
        for name, expected in [('utility_bill.pdf','REVIEW_REQUIRED'),('retail_receipt.pdf','PASS')]:
            document=self.document_ids[name]
            extraction=self.primary.get(f'/api/documents/{document}/extraction').json()
            report=self.primary.get(f'/api/documents/{document}/validation').json()
            self.assertEqual(report['validationStatus'],expected)
            self.assertFalse(report['blocking'])
            self.assertTrue(all(f['confidence'] is None for f in extraction['fields']))
            self.assertTrue(all(f['evidence'] for f in extraction['fields'] if f['value'] is not None))
            result=self.primary.post(f'/api/documents/{document}/revalidate')
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['validationStatus'],expected)
            self.evidence['m3b_cases'][name]={'id':document,'validation':report,'revalidate':result.json()}

    def test_14_receipt_total_failure_saved_correction_revision_and_audit(self):
        document, extraction, report=self.upload_and_wait('receipt-bad-total.pdf', financial_pdf('receipt',Total='99.00'))
        error=next(row for row in report['results'] if row['rule']=='RECEIPT-TOTAL')
        self.assertEqual(error['result'],'FAIL')
        self.assertEqual(error['severity'],'high')
        self.assertTrue(report['blocking'])
        self.assertEqual(report['status'],'EXCEPTION')
        self.persisted(document,'RECEIPT-TOTAL','high')
        self.correct(self.primary,document,'total','10.60')
        stale=self.primary.patch(f'/api/documents/{document}/extraction/total',
            json={'value':'1.00','expectedContentRevision':extraction['contentRevision']})
        self.assertEqual(stale.status_code,409)
        after=self.revalidate_pass(self.primary,document)
        saved=self.primary.get(f'/api/documents/{document}/extraction').json()
        original=next(f for f in extraction['fields'] if f['key']=='total')
        total=next(f for f in saved['fields'] if f['key']=='total')
        for key in ('rawValue','normalizedValue','evidence','state','resolution'):
            self.assertEqual(total[key],original[key])
        self.assertEqual(total['currentValue'],'10.60')
        self.assertEqual(total['normalizedValue'],'99.00')
        self.assertEqual(total['correctedBy'],self.accounts[0]['user_id'])
        from database.connection import connect
        with connect() as db:
            audit=db.execute('SELECT * FROM field_corrections WHERE field_prediction_id=(SELECT id FROM field_predictions WHERE document_id=%s AND field_key=%s)',(document,'total')).fetchall()
            self.assertEqual(len(audit),1)
        self.evidence['m3b_cases']['receipt-total']={'id':document,'initial':report,'after':after,
            'stale_revision':409,'resolved_history_rows':self.no_open_findings(document),'raw_and_evidence_preserved':True}

    def test_15_receipt_missing_malformed_zero_and_null_values_live(self):
        cases=[('missing-merchant',{'omit':'Merchant'},'RECEIPT-REQUIRED','supplier','Synthetic M3B Retail'),
               ('bad-date',{'Transaction date':'2026-02-30'},'RECEIPT-FORMAT','date','2026-10-19'),
               ('bad-time',{'Transaction time':'25:00'},'RECEIPT-FORMAT','transaction_time','14:35:20'),
               ('missing-currency',{'omit':'Currency'},'RECEIPT-REQUIRED','currency','MYR')]
        for name, options, code, field, value in cases:
            with self.subTest(pdf=name):
                document, extraction, report=self.upload_and_wait('receipt-'+name+'.pdf',financial_pdf('receipt',**options))
                self.assertEqual({r['rule'] for r in report['results'] if r.get('code')},{code},report)
                self.assertEqual(report['validationStatus'],'REVIEW_REQUIRED')
                self.persisted(document,code,'medium')
                self.correct(self.primary,document,field,value)
                after=self.revalidate_pass(self.primary,document)
                self.evidence['m3b_cases']['receipt-'+name]={'id':document,'initial':report,'after':after,
                    'resolved_history_rows':self.no_open_findings(document)}
        document, extraction, report=self.upload_and_wait('receipt-zero.pdf',financial_pdf('receipt',
            unit_price='0.00',line_amount='0.00',Subtotal='0.00',Tax='0.00',Total='0.00'))
        self.assertEqual(report['validationStatus'],'PASS',report)
        self.assertEqual(next(f for f in extraction['fields'] if f['key']=='total')['value'],'0.00')
        self.evidence['m3b_cases']['receipt-zero']={'id':document,'validation':report}
        document, extraction, report=self.upload_and_wait('receipt-missing-tax.pdf',financial_pdf('receipt',omit='Tax'))
        self.assertEqual(report['validationStatus'],'REVIEW_REQUIRED')
        self.assertFalse(report['coverageComplete'])
        self.assertFalse(report['blocking'])
        self.assertIsNone(next(f for f in extraction['fields'] if f['key']=='tax')['value'])
        self.assertTrue(any(r['rule']=='RECEIPT-TOTAL' and r['checkStatus']=='NOT_CHECKED' for r in report['results']))
        self.evidence['m3b_cases']['receipt-null-tax']={'id':document,'validation':report}

    def test_16_bill_math_and_dates_persist_then_resolve_without_false_pass(self):
        cases=[('subtotal',{'Subtotal':'70.00'},'BILL-SUBTOTAL','high',[('subtotal','80.00')]),
               ('line',{'line_amount':'70.00','Subtotal':'70.00'},'BILL-LINE_MATH','high',[]),
               ('due',{'Due date':'2026-09-01'},'BILL-DATES','medium',[('due','2026-10-31')]),
               ('period',{'Billing period':'2026-09-30 to 2026-09-01'},'BILL-DATES','medium',
                [('billing_period','2026-09-01 to 2026-09-30')])]
        for name, options, code, severity, corrections in cases:
            with self.subTest(pdf=name):
                document, extraction, report=self.upload_and_wait('bill-bad-'+name+'.pdf',financial_pdf('bill',**options))
                self.assertEqual({r['rule'] for r in report['results'] if r.get('code')},{code},report)
                self.assertEqual(report['status'],'EXCEPTION')
                self.assertEqual(report['blocking'],severity=='high')
                self.persisted(document,code,severity)
                if name=='line':
                    line=extraction['lineItems'][0]
                    response=self.primary.patch(f"/api/documents/{document}/line-items/{line['id']}/amount",json={'value':'80.00'})
                    self.assertEqual(response.status_code,200,response.text)
                    corrections=[('subtotal','80.00')]
                for field,value in corrections:
                    self.correct(self.primary,document,field,value)
                after=self.revalidate_review(document)
                self.evidence['m3b_cases']['bill-'+name]={'id':document,'initial':report,'after':after,
                    'resolved_history_rows':self.no_open_findings(document)}

    def test_17_bill_missing_fields_and_zero_balance_are_not_invented(self):
        for name,options,field,value in [('issuer',{'omit':'Issuer'},'supplier','Synthetic M3B Utilities'),
            ('date',{'omit':'Bill date'},'date','2026-10-01')]:
            with self.subTest(pdf=name):
                document,extraction,report=self.upload_and_wait('bill-missing-'+name+'.pdf',financial_pdf('bill',**options))
                self.assertEqual({r['rule'] for r in report['results'] if r.get('code')},{'BILL-REQUIRED'},report)
                self.persisted(document,'BILL-REQUIRED','medium')
                self.correct(self.primary,document,field,value)
                after=self.revalidate_review(document)
                self.evidence['m3b_cases']['bill-missing-'+name]={'id':document,'initial':report,'after':after,
                    'resolved_history_rows':self.no_open_findings(document)}
        document,extraction,report=self.upload_and_wait('bill-zero-balance.pdf',financial_pdf('bill',**{'Amount due':'0.00'}))
        self.assertFalse(report['blocking'])
        self.assertEqual(report['status'],'EXTRACTED')
        self.assertEqual(report['validationStatus'],'REVIEW_REQUIRED')
        self.assertEqual(next(f for f in extraction['fields'] if f['key']=='total')['value'],'0.00')
        self.evidence['m3b_cases']['bill-zero-balance']={'id':document,'validation':report}

    def test_18_bill_receipt_tenant_access_and_prior_data_preservation(self):
        for name in ('utility_bill.pdf','retail_receipt.pdf'):
            document=self.document_ids[name]
            before=self.primary.get(f'/api/documents/{document}/extraction').json()
            for suffix in ('/extraction','/validation','/file'):
                self.assertEqual(self.foreign.get(f'/api/documents/{document}'+suffix).status_code,404)
            self.assertEqual(self.foreign.post(f'/api/documents/{document}/revalidate').status_code,404)
            self.assertEqual(self.foreign.patch(f'/api/documents/{document}/extraction/total',
                json={'value':'999.00','expectedContentRevision':before['contentRevision']}).status_code,404)
            self.assertEqual(self.primary.get(f'/api/documents/{document}/extraction').json(),before)
        self.assertEqual(snapshot_documents(self.preserved_m3a),self.before_m3a)
        self.assertEqual(invoice.snapshot_existing(),self.before)
        self.evidence['bill_receipt_tenant_isolation']={'read_file_validation_correction_revalidate':404,'data_unchanged':True}

    def test_19_receipt_line_and_subtotal_failures_use_saved_line_corrections(self):
        for name,options,code in [
            ('line',{'line_amount':'9.00','Subtotal':'9.00','Total':'9.60'},'RECEIPT-LINE_MATH'),
            ('subtotal',{'Subtotal':'9.00','Total':'9.60'},'RECEIPT-SUBTOTAL')]:
            with self.subTest(pdf=name):
                document,extraction,report=self.upload_and_wait('receipt-bad-'+name+'.pdf',financial_pdf('receipt',**options))
                self.assertEqual({r['rule'] for r in report['results'] if r.get('code')},{code},report)
                self.persisted(document,code,'high')
                if name=='line':
                    line=extraction['lineItems'][0]
                    response=self.primary.patch(f"/api/documents/{document}/line-items/{line['id']}/amount",json={'value':'10.00'})
                    self.assertEqual(response.status_code,200,response.text)
                    from database.connection import connect
                    with connect() as db:
                        audit=db.execute('SELECT * FROM line_item_corrections WHERE document_id=%s',(document,)).fetchall()
                        self.assertEqual(len(audit),1)
                        self.assertEqual(audit[0]['old_value'],'9.00')
                        self.assertEqual(audit[0]['new_value'],'10.00')
                self.correct(self.primary,document,'subtotal','10.00')
                self.correct(self.primary,document,'total','10.60')
                after=self.revalidate_pass(self.primary,document)
                saved=self.primary.get(f'/api/documents/{document}/extraction').json()
                self.assertEqual(saved['raw'],extraction['raw'])
                self.assertEqual(saved['lineItems'][0]['evidence'],extraction['lineItems'][0]['evidence'])
                self.evidence['m3b_cases']['receipt-'+name]={'id':document,'initial':report,'after':after,
                    'resolved_history_rows':self.no_open_findings(document),'raw_and_line_evidence_preserved':True}

    def test_20_receipt_incomplete_lines_and_signed_return_remain_not_checked(self):
        labels=['Merchant: Synthetic M3B Retail','Transaction date: 2026-10-19','Currency: MYR',
                'Subtotal: 10.00','Tax: 0.60','Total: 10.60']
        payload=digital_pdf('RECEIPT - SYNTHETIC TEST DATA',labels,
            ['Description','Line total'],['Synthetic item','10.00'])
        document,extraction,report=self.upload_and_wait('receipt-no-quantity.pdf',payload)
        self.assertEqual(report['validationStatus'],'REVIEW_REQUIRED')
        self.assertFalse(report['blocking'])
        self.assertIsNone(extraction['lineItems'][0]['quantity'])
        self.assertTrue(any(r['rule']=='RECEIPT-LINE_MATH' and r['checkStatus']=='NOT_CHECKED' for r in report['results']))
        self.evidence['m3b_cases']['receipt-no-quantity']={'id':document,'validation':report}
        document,extraction,report=self.upload_and_wait('receipt-signed-return.pdf',financial_pdf('receipt',
            quantity='-2',line_amount='-10.00',Subtotal='-10.00',Tax='-0.60',Total='-10.60'))
        self.assertEqual(report['validationStatus'],'REVIEW_REQUIRED')
        self.assertEqual(report['status'],'EXTRACTED')
        self.assertFalse(report['blocking'])
        self.assertTrue(any(r['rule']=='RECEIPT-RETURNS' and r['checkStatus']=='NOT_CHECKED' for r in report['results']))
        self.assertEqual(next(f for f in extraction['fields'] if f['key']=='total')['value'],'-10.60')
        self.evidence['m3b_cases']['receipt-signed-return']={'id':document,'validation':report}
