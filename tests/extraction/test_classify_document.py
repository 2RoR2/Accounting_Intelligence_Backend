"""Synthetic rule tests, not held-out classification accuracy."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.extraction.classification.classify_document import classify_document

def document(*texts):
    return {'schema_name': 'DoclingDocument', 'texts': [
        {'text': t, 'label': 'text', 'self_ref': f'#/texts/{i}',
         'prov': [{'page_no': 1, 'bbox': None}]} for i, t in enumerate(texts)]}

class ClassifierTests(unittest.TestCase):
    def test_current_rules_pass_the_existing_classification_boundary(self):
        from services.extraction.parsing.combine_extraction import ClassificationResult
        for heading in ('INVOICE','TAXINVOICE','SIMPLIFIED TAX INVOICE','CREDIT NOTE','RECEIPT','Unidentified text'):
            with self.subTest(heading=heading):
                result=classify_document(document(heading))
                boundary=ClassificationResult.model_validate({**result,'source_document_json':'document.json',
                    'source_json_sha256':'a'*64})
                self.assertEqual(boundary.value,result['value'])
    def test_retail_payment_does_not_override_compact_or_simplified_invoice_heading(self):
        for heading in ('TAXINVOICE','SIMPLIFIED TAX INVOICE','Tax Invoice','CREDIT NOTE'):
            with self.subTest(heading=heading):
                result=classify_document(document(heading,'TOTAL 10.00','CASH 20.00','CHANGE 10.00'))
                self.assertEqual(result['value'],'credit_note' if heading=='CREDIT NOTE' else 'invoice')

    def test_damaged_qualified_invoice_heading_remains_reviewable(self):
        doc=document('UNRECOGNISED INVOICE','TOTAL 10.00','CASH 20.00','CHANGE 10.00')
        doc['texts'][0]['label']='section_header'
        result=classify_document(doc)
        self.assertEqual(result['value'],'unknown')
        self.assertTrue(result['unsupported_headings'])
    def test_supported_titles(self):
        for title, expected in [('INVOICE - SYNTHETIC TEST DATA', 'invoice'),
                                ('Tax Invoice', 'invoice'), ('Utility Bill', 'bill'),
                                ('Sales Receipt', 'receipt')]:
            with self.subTest(title=title):
                self.assertEqual(classify_document(document(title))['value'], expected)

    def test_bill_to_and_invoice_references_do_not_classify(self):
        r = classify_document(document('Bill to: Example Ltd', 'Invoice number: SYN-001',
                                       'Keep this receipt for your records'))
        self.assertEqual((r['value'], r['status']), ('unknown', 'unknown'))

    def test_incompatible_headings_are_ambiguous(self):
        r = classify_document(document('INVOICE', 'RECEIPT'))
        self.assertEqual((r['value'], r['status']), ('unknown', 'ambiguous'))
        self.assertEqual(len(r['candidates']), 2)

    def test_repeated_same_type_is_not_ambiguous(self):
        self.assertEqual(classify_document(document('INVOICE', 'Tax Invoice'))['value'], 'invoice')

    def test_unsupported_heading_blocks_supported_guess(self):
        # Credit notes are already supported by the production title vocabulary.
        r = classify_document(document('Purchase Order', 'Invoice'))
        self.assertEqual((r['value'], r['status']), ('unknown', 'unsupported'))
        self.assertEqual(len(r['unsupported_headings']), 1)

    def test_proforma_is_not_invoice(self):
        self.assertEqual(classify_document(document('Pro forma invoice'))['value'], 'unknown')

    def test_empty_document_is_unknown(self):
        self.assertEqual(classify_document(document())['status'], 'unknown')

    def test_furniture_ignored(self):
        d = document('RECEIPT')
        d['texts'][0]['content_layer'] = 'furniture'
        self.assertEqual(classify_document(d)['value'], 'unknown')

    def test_table_cell_ignored(self):
        d = document('RECEIPT')
        d['texts'][0]['label'] = 'table_cell'
        self.assertEqual(classify_document(d)['value'], 'unknown')

    def test_evidence_preserved_without_mutation(self):
        d = document('Invoice')
        before = deepcopy(d)
        r = classify_document(d)
        self.assertEqual(d, before)
        self.assertEqual(r['candidates'][0]['evidence']['prov'], d['texts'][0]['prov'])
        self.assertEqual(r['candidates'][0]['evidence']['source_ref'], '#/texts/0')
        self.assertTrue(r['review_required'])

    def test_wrong_input_schema_rejected(self):
        with self.assertRaises(ValueError): classify_document({'schema_name': 'Other'})

    def test_explicit_heading_and_identifier_on_separate_lines(self):
        r = classify_document(document('INVOICE', 'Invoice number:', 'SYN-482'))
        self.assertEqual((r['value'], r['status']), ('invoice', 'classified'))
        self.assertEqual(len(r['candidates']), 1)

    def test_structured_heading_is_not_hidden_by_retained_lines(self):
        d = document('Example Trading', 'Invoice number: SYN-482')
        d['texts'][0].update(text='INVOICE', label='section_header')
        d['m2_text_lines'] = [{'text': 'Example Trading', 'label': 'text',
            'self_ref': '#/m2_text_lines/0', 'prov': [{'page_no': 1, 'bbox': None}]}]
        r = classify_document(d)
        self.assertEqual(r['value'], 'invoice')
        self.assertEqual(r['candidates'][0]['evidence']['source_ref'], '#/texts/0')

    def test_duplicate_heading_streams_keep_both_source_references(self):
        d = document('INVOICE')
        d['texts'][0]['label'] = 'section_header'
        d['m2_text_lines'] = [{**deepcopy(d['texts'][0]), 'self_ref': '#/m2_text_lines/0'}]
        before = deepcopy(d)
        r = classify_document(d)
        self.assertEqual(len(r['candidates']), 1)
        evidence = r['candidates'][0]['evidence']
        self.assertEqual(evidence['source_ref'], '#/m2_text_lines/0')
        self.assertEqual(evidence['corroborating_sources'][0]['source_ref'], '#/texts/0')
        self.assertEqual(d, before)

    def test_different_headings_across_streams_remain_ambiguous(self):
        d = document('RECEIPT')
        d['texts'][0]['label'] = 'section_header'
        d['m2_text_lines'] = [{'text': 'INVOICE', 'label': 'text', 'self_ref': '#/m2_text_lines/0',
                              'prov': [{'page_no': 1, 'bbox': None}]}]
        self.assertEqual(classify_document(d)['status'], 'ambiguous')

    def test_merged_structured_heading_after_letterhead(self):
        d = document('Example Trading', 'INVOICE\nInvoice Number: SYN-482 Date: 2026-09-18 Total: 10.00')
        d['texts'][1]['label'] = 'section_header'
        d['m2_text_lines'] = [{'text': 'Example Trading', 'label': 'text',
            'self_ref': '#/m2_text_lines/0', 'prov': [{'page_no': 1, 'bbox': None}]}]
        r = classify_document(d)
        self.assertEqual(r['value'], 'invoice')
        self.assertEqual(r['candidates'][0]['evidence']['source_ref'], '#/texts/1')

    def test_merged_retained_heading(self):
        d = document()
        d['m2_text_lines'] = [{'text': 'INVOICE\nInvoice Number: SYN-482 Date: 2026-09-18 Total: 10.00',
            'label': 'text', 'self_ref': '#/m2_text_lines/0', 'prov': [{'page_no': 1, 'bbox': None}]}]
        self.assertEqual(classify_document(d)['value'], 'invoice')

    def identity_document(self):
        return document('Invoice no: EXAMPLE-482', 'Date of issue:', '2026-09-18',
                        'Seller:', 'Example Supplier', 'Client:', 'Example Customer',
                        'Description', 'Qty', 'Unit Price', 'Total')

    def test_invoice_identifier_requires_independent_financial_evidence(self):
        d = self.identity_document()
        before = deepcopy(d)
        r = classify_document(d)
        self.assertEqual((r['value'], r['status']), ('invoice', 'classified'))
        self.assertEqual(r['method'], 'content_evidence_rules')
        self.assertEqual(r['reason'], 'INVOICE_IDENTITY_AND_FINANCIAL_EVIDENCE')
        self.assertEqual(len(r['candidates']), 1)
        evidence = r['candidates'][0]['evidence']
        self.assertEqual(evidence['source_ref'], '#/texts/0')
        self.assertTrue(evidence['corroborating_sources'])
        self.assertEqual(d, before)

    def test_each_missing_identity_cue_keeps_unknown(self):
        for index in (1, 2, 3, 5, 7, 8, 9, 10):
            with self.subTest(missing=index):
                d = self.identity_document()
                del d['texts'][index]
                self.assertEqual(classify_document(d)['status'], 'unknown')

    def test_identity_cues_cannot_be_borrowed_from_other_pages(self):
        d = self.identity_document()
        for item in d['texts'][1:]:
            item['prov'][0]['page_no'] = 2
        self.assertEqual(classify_document(d)['status'], 'unknown')

    def test_footer_invoice_reference_is_insufficient(self):
        d = self.identity_document()
        d['texts'].append(d['texts'].pop(0))
        self.assertEqual(classify_document(d)['status'], 'unknown')

    def test_unsupported_heading_vetoes_invoice_identity_context(self):
        d = self.identity_document()
        d['texts'].append(document('Quotation')['texts'][0])
        self.assertEqual(classify_document(d)['status'], 'unsupported')

    def test_bill_invoice_heading_is_ambiguous(self):
        self.assertEqual(classify_document(document('BILL / INVOICE'))['status'], 'ambiguous')

    def test_credit_note_and_invoice_are_conflicting_supported_kinds(self):
        self.assertEqual(classify_document(document('Credit Note', 'Invoice'))['status'], 'ambiguous')

    def test_utility_supplier_does_not_override_explicit_tax_invoice(self):
        d = document('Example Utility Company', 'TAX INVOICE', 'Electricity consumption', 'Amount Due')
        self.assertEqual(classify_document(d)['value'], 'invoice')

    def utility_document(self, heading='TAX INVOICE'):
        from tests.extraction.test_financial_layout import layout
        return layout(heading, [('Supplier: Example Services Ltd',20,50,280,70),
            ('Invoice No: EX-482',20,80,220,100),('Date: 2026-09-18',20,110,220,130),
            ('Due Date',20,150,110,170),('2026-10-18',20,175,130,195),
            ('Amount Due',320,150,430,170),('EUR 25.00',320,175,430,195),
            ('Description',20,240,150,260),('Amount',450,240,550,260),
            ('Water consumption',20,280,240,300),('25.00',450,280,550,300),
            ('TOTAL TAX',220,350,320,370),('GRAND TOTAL',420,350,550,370),
            ('EUR 0.00',220,380,320,400),('EUR 25.00',420,380,550,400)])

    def test_utility_business_function_preserves_fiscal_title_evidence(self):
        for heading in ('TAX INVOICE','TAXINVOICE','SIMPLIFIED TAX INVOICE','Unlabelled statement'):
            with self.subTest(heading=heading):
                doc=self.utility_document(heading);before=deepcopy(doc)
                result=classify_document(doc)
                self.assertEqual((result['value'],result['status']),('bill','classified'))
                self.assertEqual(result['reason'],'UTILITY_USAGE_AND_BILLING_EVIDENCE')
                self.assertTrue(result['review_required'])
                evidence=result['candidates'][0]['evidence']
                self.assertTrue(evidence['corroborating_sources'])
                if heading!='Unlabelled statement':
                    printed=evidence['printed_headings'][0]['evidence']
                    self.assertEqual(printed['text'],heading)
                    self.assertEqual(printed['source_ref'],'#/m2_text_lines/0')
                    self.assertEqual(printed['prov'],doc['m2_text_lines'][0]['prov'])
                self.assertEqual(doc,before)

    def test_utility_incomplete_or_competing_evidence_stays_reviewable(self):
        for problem in ('no_due_amount','no_deadline','receipt','invoice','credit_note','quotation'):
            with self.subTest(problem=problem):
                doc=self.utility_document()
                if problem=='no_due_amount':
                    doc['m2_text_lines']=[x for x in doc['m2_text_lines'] if x['text']!='Amount Due']
                elif problem=='no_deadline':
                    doc['m2_text_lines']=[x for x in doc['m2_text_lines'] if x['text']!='Due Date']
                else:
                    doc['m2_text_lines'][0]['text']={'receipt':'RECEIPT','invoice':'INVOICE',
                        'credit_note':'CREDIT NOTE','quotation':'QUOTATION'}[problem]
                result=classify_document(doc)
                self.assertEqual(result['value'],'unknown')
                self.assertEqual(result['status'],'unsupported' if problem=='quotation' else 'ambiguous')
                self.assertTrue(result['review_required'])

    def test_utility_context_needs_linked_values_and_a_billed_service_row(self):
        for problem in ('different_page','no_geometry','bad_date','amount_prose','installation',
                        'supplier_name','nonfinite_box','no_charge','unrelated_deadline'):
            with self.subTest(problem=problem):
                doc=self.utility_document('Unlabelled statement')
                lines=doc['m2_text_lines']
                if problem=='different_page':
                    doc['pages']['2']=deepcopy(doc['pages']['1'])
                    for x in lines:
                        if x['text'] in {'Amount Due','EUR 25.00'}: x['prov'][0]['page_no']=2
                elif problem=='no_geometry': doc['pages']={}
                elif problem=='bad_date': lines[5]['text']='99/99/2026'
                elif problem=='amount_prose': lines[7]['text']='pay later'
                elif problem=='installation': lines[10]['text']='Water meter installation'
                elif problem=='supplier_name': lines[10]['text']='Example Electricity Utility Company'
                elif problem=='nonfinite_box': lines[10]['prov'][0]['bbox']['r']=float('nan')
                elif problem=='no_charge': lines[11]['text']='unreadable'
                else: lines[5]['prov'][0]['bbox'].update(l=450,r=550)
                result=classify_document(doc)
                self.assertEqual(result['value'],'unknown')

    def test_utility_bill_reuses_invoice_form_without_amount_or_settlement_substitution(self):
        from tests.extraction.test_multidocument import contract
        doc=self.utility_document()
        raw,canonical=contract(doc)
        fields=raw['fields']
        self.assertEqual(raw['document_type']['value'],'bill')
        for key,value in {'issuer':'Example Services Ltd','bill_reference':'EX-482',
                          'bill_date':'2026-09-18','amount_due':'25.00',
                          'tax_amount':'0.00','total_amount':'25.00','currency':'EUR'}.items():
            self.assertEqual(fields[key]['value'],value)
            self.assertTrue(fields[key]['evidence'])
        self.assertIsNone(fields['amount_paid']['value'])
        self.assertIsNone(fields['document_charges']['value'])
        self.assertIsNone(fields['account_number']['value'])
        self.assertEqual(next(f['current_value'] for f in canonical['fields'] if f['field_key']=='total'),'25.00')
        self.assertEqual(next(f['current_value'] for f in canonical['fields'] if f['field_key']=='amount_due'),'25.00')
        self.assertFalse(raw['accounting_approval_performed'])
        self.assertTrue(raw['review_required'])

    def test_utility_and_retail_context_cannot_silently_override_each_other(self):
        from tests.extraction.test_financial_layout import layout
        doc=self.utility_document()
        extra=layout('unused',[('Electricity usage',20,310,240,330),('5.00',450,310,550,330),
            ('Total',320,450,420,470),('30.00',450,450,550,470),
            ('Cash',320,480,420,500),('30.00',450,480,550,500)])['m2_text_lines'][1:]
        for i,item in enumerate(extra): item['self_ref']=f'#/m2_text_lines/{100+i}'
        doc['m2_text_lines'].extend(extra)
        result=classify_document(doc)
        self.assertEqual((result['value'],result['status']),('unknown','ambiguous'))
        self.assertEqual({c['value'] for c in result['candidates']},{'bill','receipt'})

    def test_utility_inline_values_and_duplicate_amounts_keep_linking_guards(self):
        doc=self.utility_document()
        doc['m2_text_lines'][4]['text']='Payment terms: Net 30 days'
        doc['m2_text_lines'][6]['text']='Amount Due: EUR 25.00'
        doc['m2_text_lines']=[x for i,x in enumerate(doc['m2_text_lines']) if i not in {5,7}]
        self.assertEqual(classify_document(doc)['value'],'bill')
        doc=self.utility_document()
        extra=deepcopy(doc['m2_text_lines'][7]);extra['self_ref']='#/m2_text_lines/99'
        extra['text']='EUR 30.00'
        extra['prov'][0]['bbox'].update(l=325,r=435)
        doc['m2_text_lines'].append(extra)
        result=classify_document(doc)
        self.assertEqual((result['value'],result['status']),('unknown','ambiguous'))

    def retail_document(self, heading='TAX INVOICE'):
        from tests.extraction.test_financial_layout import layout
        return layout(heading,[('Date: 25/12/2025 13:15:00',20,60,300,80),
            ('QTY ITEM',20,100,250,120),('TOTAL',400,100,500,120),
            ('2 General goods',20,140,250,160),('15.00',400,140,500,160),
            ('1 Other goods',20,180,250,200),('5.00',400,180,500,200),
            ('Total Rounded',20,240,180,260),('20.00',400,240,500,260),
            ('CashTendered',20,280,180,300),('30.00',400,280,500,300),
            ('Change',20,320,180,340),('10.00',400,320,500,340)])

    def test_retail_context_outweighs_fiscal_title_and_keeps_printed_sources(self):
        for heading in ('TAX INVOICE','TAXINVOICE','SIMPLIFIED TAX INVOICE'):
            with self.subTest(heading=heading):
                doc=self.retail_document(heading);original=deepcopy(doc)
                result=classify_document(doc)
                self.assertEqual((result['value'],result['status']),('receipt','classified'))
                self.assertEqual(result['reason'],'RETAIL_TOTAL_TENDER_CHANGE_EVIDENCE')
                evidence=result['candidates'][0]['evidence']
                self.assertEqual(evidence['printed_headings'][0]['evidence']['text'],heading)
                self.assertTrue(all(e['source_ref'] and e['prov'] for e in evidence['corroborating_sources']))
                self.assertIn('Total Rounded',[e['text'] for e in evidence['corroborating_sources']])
                self.assertTrue(result['review_required'])
                self.assertEqual(doc,original)

    def test_retail_incomplete_values_never_repair_ocr_or_imply_settlement(self):
        for problem in ('missing_date','missing_itemization','bad_tender','bad_change','wrong_page','no_geometry'):
            with self.subTest(problem=problem):
                doc=self.retail_document();lines=doc['m2_text_lines']
                if problem=='missing_date': lines[1]['text']='unreadable'
                elif problem=='missing_itemization': lines[2]['text']='unreadable'
                elif problem=='bad_tender': lines[11]['text']='3000'
                elif problem=='bad_change': lines[13]['text']='1000'
                elif problem=='wrong_page':
                    doc['pages']['2']=deepcopy(doc['pages']['1'])
                    lines[13]['prov'][0]['page_no']=2
                else: doc['pages']={}
                result=classify_document(doc)
                self.assertNotEqual(result['value'],'receipt')
                if problem in {'bad_tender','bad_change','wrong_page'}:
                    self.assertEqual((result['value'],result['status']),('unknown','ambiguous'))
                self.assertTrue(result['review_required'])

    def test_retail_conflicting_specific_headings_and_other_page_headings_are_reviewable(self):
        for heading in ('INVOICE','UTILITY BILL','CREDIT NOTE','QUOTATION'):
            with self.subTest(heading=heading):
                result=classify_document(self.retail_document(heading))
                self.assertEqual(result['value'],'unknown')
                self.assertEqual(result['status'],'unsupported' if heading=='QUOTATION' else 'ambiguous')
        for doc in (self.retail_document(),self.utility_document()):
            doc['pages']['2']=deepcopy(doc['pages']['1'])
            doc['m2_text_lines'][0]['prov'][0]['page_no']=2
            result=classify_document(doc)
            self.assertEqual((result['value'],result['status']),('unknown','ambiguous'))

    def pos_identity_document(self, heading='TAX INVOICE'):
        from tests.extraction.test_financial_layout import layout
        return layout(heading,[('Invoice No: EX-482',20,50,280,70),
            ('Date: 25/12/2025 13:15:00',20,80,330,100),('Cashier: C7',20,110,200,130),
            ('Description',20,200,180,220),('Qty',200,200,235,220),
            ('Unit Price',260,200,360,220),('Amount',440,200,540,220),
            ('SKU-135',20,240,180,260),('1 20.00',260,240,360,260),('20.00 SR',440,240,540,260),
            ('Total Rounded',20,300,240,320),('20.00',440,300,540,320)])

    def test_pos_identity_recognizes_receipt_without_payment_reads(self):
        from tests.extraction.test_financial_layout import layout
        from tests.extraction.test_multidocument import contract
        for bad_payment,heading in ((False,'TAX INVOICE'),(True,'TAX INVOICE'),(False,'Counter transaction')):
            with self.subTest(bad_payment=bad_payment,heading=heading):
                doc=self.pos_identity_document(heading)
                if bad_payment:
                    extra=layout('unused',[('CashTendered',20,350,240,370),('3000',440,350,540,370),
                        ('Change',20,390,240,410),('1000',440,390,540,410)])['m2_text_lines'][1:]
                    for i,item in enumerate(extra): item['self_ref']=f'#/m2_text_lines/{100+i}'
                    doc['m2_text_lines'].extend(extra)
                original=deepcopy(doc)
                result=classify_document(doc)
                self.assertEqual((result['value'],result['status']),('receipt','classified'))
                self.assertEqual(result['reason'],'POS_IDENTITY_ITEMIZATION_AND_TOTAL_EVIDENCE')
                ev=result['candidates'][0]['evidence']
                if heading=='TAX INVOICE':
                    self.assertEqual(ev['printed_headings'][0]['evidence']['text'],heading)
                texts={x['text'] for x in ev['corroborating_sources']}
                self.assertTrue({'Cashier: C7','Date: 25/12/2025 13:15:00','SKU-135','20.00 SR'} <= texts)
                raw,_=contract(doc)
                self.assertIsNone(raw['fields']['payment_method']['value'])
                self.assertNotIn('amount_paid',raw['fields']);self.assertNotIn('amount_due',raw['fields'])
                self.assertTrue(result['review_required']);self.assertFalse(raw['accounting_approval_performed'])
                self.assertEqual(doc,original)

    def test_pos_identity_requires_every_independent_cue_and_credible_geometry(self):
        for problem,index in [('reference',1),('timestamp',2),('operator',3),('description',4),
                              ('price_header',6),('item',8),('price',9),('charge',10),('total',11),
                              ('total_value',12),('invalid_date',2),('other_page',2),('no_geometry',0),('subtotal_row',8)]:
            with self.subTest(problem=problem):
                doc=self.pos_identity_document()
                if problem=='invalid_date': doc['m2_text_lines'][index]['text']='Date: 99/99/2025 13:15:00'
                elif problem=='other_page':
                    doc['pages']['2']=deepcopy(doc['pages']['1'])
                    doc['m2_text_lines'][index]['prov'][0]['page_no']=2
                elif problem=='no_geometry': doc['pages']={}
                elif problem=='subtotal_row': doc['m2_text_lines'][index]['text']='Subtotal'
                else: doc['m2_text_lines'].pop(index)
                result=classify_document(doc)
                self.assertNotEqual(result['value'],'receipt')
                self.assertTrue(result['review_required'])

    def test_pos_identity_protects_notes_and_competing_invoice_context(self):
        for heading,expected in [('CREDIT NOTE','credit_note'),('DEBIT NOTE','debit_note'),
                                 ('QUOTATION','unknown'),('INVOICE','unknown')]:
            with self.subTest(heading=heading):
                result=classify_document(self.pos_identity_document(heading))
                self.assertEqual(result['value'],expected)
        from tests.extraction.test_financial_layout import layout
        doc=self.pos_identity_document()
        extra=layout('unused',[('Date of issue:',350,50,500,70),
            ('Seller:',20,145,100,165),('Client:',300,145,380,165)])['m2_text_lines'][1:]
        for i,item in enumerate(extra): item['self_ref']=f'#/m2_text_lines/{100+i}'
        doc['m2_text_lines'].extend(extra)
        result=classify_document(doc)
        self.assertEqual((result['value'],result['status']),('unknown','ambiguous'))
        self.assertTrue(result['candidates'][-1]['evidence']['competing_invoice_identity'])

    def test_pos_conflicting_final_totals_remain_ambiguous(self):
        from tests.extraction.test_financial_layout import layout
        doc=self.pos_identity_document()
        extra=layout('unused',[('Grand Total',20,340,240,360),('21.00',440,340,540,360)])['m2_text_lines'][1:]
        for i,item in enumerate(extra): item['self_ref']=f'#/m2_text_lines/{100+i}'
        doc['m2_text_lines'].extend(extra)
        result=classify_document(doc)
        self.assertEqual((result['value'],result['status']),('unknown','ambiguous'))

    def test_receipt_without_title_preserves_payment_evidence_rule(self):
        r = classify_document(document('Example Shop', 'Total 10.00', 'Cash 20.00', 'Change 10.00'))
        self.assertEqual((r['value'], r['method']), ('receipt', 'content_evidence_rules'))

    def test_filename_catalog_codes_and_metadata_are_not_evidence(self):
        d = document('Example Trading', 'Thank you')
        d.update(filename='invoice.pdf', document_id='INV00001', demo_code='BIL00001',
                 expected_document_type='invoice', template_id='utility_bill')
        self.assertEqual(classify_document(d)['status'], 'unknown')

    def test_structured_furniture_cannot_supply_a_heading(self):
        d = document('INVOICE')
        d['texts'][0].update(label='page_header', content_layer='furniture')
        d['m2_text_lines'] = [{'text': 'Example Company', 'label': 'text',
            'self_ref': '#/m2_text_lines/0', 'prov': [{'page_no': 1, 'bbox': None}]}]
        self.assertEqual(classify_document(d)['status'], 'unknown')

if __name__ == '__main__':
    unittest.main()


