"""Synthetic, evidence-grounded row and bill regressions; no accuracy claims."""
from copy import deepcopy
from services.extraction.contracts.accounting_contract import map_m2_result
import unittest

from tests.extraction.test_multidocument import contract


def layout(kind, cells):
    lines = [(kind, 10, 10, 160, 30)] + cells
    return {'schema_name': 'DoclingDocument', 'pages': {'1': {'size': {'width': 600, 'height': 900}}},
            'texts': [], 'm2_text_lines': [
                {'text': text, 'self_ref': f'#/m2_text_lines/{i}', 'label': 'text', 'from_ocr': True,
                 'ocr_score': .8, 'prov': [{'page_no': 1, 'bbox': dict(l=l,t=t,r=r,b=b,coord_origin='TOPLEFT')}]}
                for i,(text,l,t,r,b) in enumerate(lines)]}


def financial_summary(unit='$', net='100,00', tax='10,00', gross='110,00', footer='Total'):
    return layout('INVOICE', [('SUMMARY',20,60,160,80),
        ('VAT [%]',140,100,200,120), ('Net Worth',250,100,320,120),
        ('VAT',360,100,400,120), ('Gross Worth',450,100,550,120),
        ('10%',160,140,200,160), (net,250,140,320,160),
        (tax,360,140,400,160), (gross,450,140,550,160),
        (footer,20,180,120,200), (unit+' '+net,240,180,320,200),
        (unit+' '+tax,350,180,400,200), (unit+' '+gross,440,180,550,200)])


def item_table(headers, rows):
    doc=layout('INVOICE',[])
    cells=[]
    for ri,values in enumerate([headers]+rows):
        for ci,text in enumerate(values):
            cells.append({'text':text,'start_row_offset_idx':ri,'start_col_offset_idx':ci,
                'row_span':1,'col_span':1,'column_header':ri==0,
                'bbox':dict(l=20+ci*70,t=100+ri*40,r=80+ci*70,b=120+ri*40,coord_origin='TOPLEFT')})
    doc['tables']=[{'self_ref':'#/tables/0','prov':[{'page_no':1}],
        'data':{'orientation':'rot_0','num_rows':len(rows)+1,'num_cols':len(headers),'table_cells':cells}}]
    return doc


class FinancialLayoutTests(unittest.TestCase):
    def retail_bill(self):
        return layout('Example shop',[('Example Shop Sdn Bhd',20,50,300,70),
            ('10 Printed Road,',20,72,280,90),('City 12345, State',20,92,280,110),
            ('Bill No',20,150,100,170),(': TX-123',180,150,300,170),
            ('Date: 25/03/2018 6:41:02 PM',20,180,350,200),('Cashier',20,210,100,230),
            ('Qty Description',20,250,220,270),('Amount',400,250,490,270),
            ('1 Office paper',20,280,250,300),('10.00 SR',400,280,490,300),
            ('2 Pens',20,310,250,330),('20.00 SR',400,310,490,330),
            ('Total:',200,340,300,360),('30.00',400,340,490,360),
            ('Grand Total:',200,370,320,390),('32.00',400,370,490,390),
            ('Net Total:',200,400,320,420),('32.00',400,400,490,420),
            ('GST Summary',20,460,180,480),('TOTAL:',200,520,300,540),
            ('30.00',400,520,490,540),('2.00',520,520,570,540)])

    def test_title_free_pos_receipt_uses_independent_evidence_without_settlement(self):
        doc=self.retail_bill();original=deepcopy(doc)
        raw,mapped=contract(doc)
        self.assertEqual(raw['document_type']['value'],'receipt')
        self.assertEqual(raw['document_type']['reason'],'RETAIL_TRANSACTION_EVIDENCE')
        expected={'merchant':'Example Shop Sdn Bhd','merchant_address':'10 Printed Road,\nCity 12345, State',
                  'receipt_number':'TX-123','transaction_date':'2018-03-25','total_amount':'32.00'}
        for key,value in expected.items():
            with self.subTest(field=key):self.assertEqual(raw['fields'][key]['value'],value)
        self.assertIsNone(raw['fields']['payment_method']['value'])
        self.assertNotIn('amount_paid',raw['fields'])
        self.assertEqual(
            next(f for f in mapped['fields'] if f['field_key'] == 'merchant_address')['current_value'],
            expected['merchant_address']
        )
        self.assertNotIn('supplier_address', [f['field_key'] for f in mapped['fields']])
        self.assertTrue(raw['review_required']);self.assertFalse(raw['accounting_approval_performed'])
        self.assertEqual(doc,original)

    def test_title_free_pos_needs_cashier_timestamp_and_repeated_rows(self):
        from services.extraction.classification.classify_document import classify_document
        for remove in ('Cashier','Date:','2 Pens'):
            with self.subTest(remove=remove):
                doc=self.retail_bill();doc['m2_text_lines']=[x for x in doc['m2_text_lines'] if not x['text'].startswith(remove)]
                self.assertEqual(classify_document(doc)['value'],'unknown')

    def test_invoice_item_total_header_with_merged_qty_item_is_not_document_total(self):
        raw,_=contract(layout('TAX INVOICE',[('QTY ITEM',20,100,200,120),('TOTAL',400,100,490,120),
            ('2 Example goods',20,140,200,160),('25.40',400,140,490,160),
            ('1 Other goods',20,180,200,200),('12.50',400,180,490,200)]))
        self.assertIsNone(raw['fields']['total_amount']['value'])

    def test_receipt_short_year_is_not_expanded_or_assumed(self):
        doc=self.retail_bill();doc['m2_text_lines'][0]['text']='RECEIPT'
        doc['m2_text_lines'][6]['text']='20-11-17'
        raw,_=contract(doc)
        self.assertIsNone(raw['fields']['transaction_date']['value'])
        self.assertEqual(raw['fields']['transaction_date']['state'],'ambiguous')
        self.assertIn('20-11-17',{c['value'] for c in raw['fields']['transaction_date']['candidates']})

    def test_receipt_competing_merchants_are_not_selected_arbitrarily(self):
        doc=self.retail_bill();doc['m2_text_lines'][0]['text']='RECEIPT'
        extra=deepcopy(doc['m2_text_lines'][1]);extra.update(text='Other Shop Sdn Bhd',self_ref='#/competing-shop')
        doc['m2_text_lines'].append(extra)
        raw,_=contract(doc)
        self.assertIsNone(raw['fields']['merchant']['value'])
        self.assertEqual(raw['fields']['merchant']['state'],'ambiguous')
        self.assertNotIn('merchant_address',raw['fields'])

    def test_receipt_qty_boundary_preserves_merchant_punctuation_and_multiline_address(self):
        doc=layout('RECEIPT',[('North Star Services Sdn. Bhd.',20,60,380,80),
            ('47 Example Road,',30,78,300,98),('Source District,',40,96,290,116),
            ('12345 Source City.',30,114,300,134),('Tel: 03-1234 5678',20,136,350,156),
            ('Qty',400,180,450,200)])
        original=deepcopy(doc)
        raw,canonical=contract(doc)
        name=raw['fields']['merchant'];address=raw['fields']['merchant_address']
        self.assertEqual(name['value'],'North Star Services Sdn. Bhd.')
        self.assertEqual(name['raw_text'],doc['m2_text_lines'][1]['text'])
        self.assertEqual(address['value'],'47 Example Road,\nSource District,\n12345 Source City.')
        self.assertEqual(address['raw_text'],address['value'])
        self.assertEqual({e['source_ref'] for e in address['evidence']},
                         {f'#/m2_text_lines/{i}' for i in (1,2,3,4)})
        for field in (name,address):
            self.assertTrue(field['review_required'])
            self.assertTrue(field['candidates'])
            for ev in field['evidence']:
                source=next(x for x in doc['m2_text_lines'] if x['self_ref']==ev['source_ref'])
                self.assertEqual(ev['text'],source['text'])
                self.assertEqual(ev['bbox_original'],source['prov'][0]['bbox'])
                self.assertEqual(ev['ocr_score'],source['ocr_score'])
        stored={f['field_key']:f for f in canonical['fields']}
        self.assertEqual(stored['merchant_address']['current_value'],address['value'])
        self.assertNotIn('supplier_address',stored)
        self.assertEqual(doc,original)

    def test_receipt_header_requires_boundary_geometry_and_an_unambiguous_merchant(self):
        for problem in ('no_boundary','other_page','no_geometry','competing'):
            with self.subTest(problem=problem):
                doc=layout('RECEIPT',[('North Star Services Sdn. Bhd.',20,60,380,80),
                    ('47 Example Road',30,84,300,104),('Qty',400,180,450,200)])
                if problem=='no_boundary': doc['m2_text_lines'].pop()
                elif problem=='other_page':
                    doc['pages']['2']=deepcopy(doc['pages']['1'])
                    doc['m2_text_lines'][-1]['prov'][0]['page_no']=2
                elif problem=='no_geometry': doc['pages']={}
                else:
                    extra=deepcopy(doc['m2_text_lines'][1])
                    extra.update(text='Other Services Sdn. Bhd.',self_ref='#/competing')
                    doc['m2_text_lines'].append(extra)
                raw,_=contract(doc)
                self.assertIsNone(raw['fields']['merchant']['value'])
                self.assertNotIn('merchant_address',raw['fields'])
                if problem=='competing':
                    self.assertEqual(raw['fields']['merchant']['state'],'ambiguous')
                    self.assertEqual(len(raw['fields']['merchant']['candidates']),2)

    def test_receipt_customer_delivery_and_payment_regions_are_not_merchant_headers(self):
        for role in ('Customer:','Bill To:','Delivery Address:','Shipping:','Payment Details:','Bank:'):
            with self.subTest(role=role):
                doc=layout('RECEIPT',[(role,20,40,300,60),
                    ('Other Services Sdn. Bhd.',20,80,380,100),
                    ('47 Unrelated Road',30,104,300,124),('Qty',400,180,450,200)])
                raw,_=contract(doc)
                self.assertIsNone(raw['fields']['merchant']['value'])
                self.assertNotIn('merchant_address',raw['fields'])

                doc=layout('RECEIPT',[('North Star Services Sdn. Bhd.',20,40,380,60),
                    (role,20,80,300,100),('47 Unrelated Road',30,104,300,124),
                    ('Qty',400,180,450,200)])
                raw,_=contract(doc)
                self.assertEqual(raw['fields']['merchant']['value'],'North Star Services Sdn. Bhd.')
                self.assertNotIn('merchant_address',raw['fields'])

    def test_receipt_conflicting_final_totals_and_unreliable_geometry_remain_unresolved(self):
        doc=self.retail_bill();doc['m2_text_lines'][19]['text']='33.00'
        raw,_=contract(doc)
        self.assertIsNone(raw['fields']['total_amount']['value'])
        self.assertEqual(raw['fields']['total_amount']['state'],'ambiguous')
        doc=self.retail_bill();doc['m2_text_lines'][0]['text']='RECEIPT';doc['pages']={}
        raw,_=contract(doc)
        self.assertIsNone(raw['fields']['merchant']['value'])
        self.assertNotIn('merchant_address',raw['fields'])

    def test_receipt_explicit_final_total_labels_use_printed_amounts_and_evidence(self):
        for label in ('Total Rounded','Total Amt Payable:'):
            with self.subTest(label=label):
                doc=layout('RECEIPT',[('Subtotal',20,80,180,100),('100.00',400,80,500,100),
                    (label,20,120,240,140),('106.00',400,120,500,140),
                    ('Cash',20,160,180,180),('200.00',400,160,500,180),
                    ('Change',20,200,180,220),('94.00',400,200,500,220)])
                original=deepcopy(doc)
                raw,canonical=contract(doc)
                field=raw['fields']['total_amount']
                self.assertEqual(field['value'],'106.00')
                self.assertEqual(field['raw_text'],label+'\n106.00')
                self.assertEqual({e['source_ref'] for e in field['evidence']},
                                 {'#/m2_text_lines/3','#/m2_text_lines/4'})
                for ev in field['evidence']:
                    source=next(x for x in doc['m2_text_lines'] if x['self_ref']==ev['source_ref'])
                    self.assertEqual(ev['text'],source['text'])
                    self.assertEqual(ev['bbox_original'],source['prov'][0]['bbox'])
                    self.assertEqual(ev['ocr_score'],source['ocr_score'])
                self.assertEqual(next(f['current_value'] for f in canonical['fields'] if f['field_key']=='total'),'106.00')
                self.assertNotIn('amount_paid',raw['fields'])
                self.assertNotIn('amount_due',raw['fields'])
                self.assertTrue(raw['review_required'])
                self.assertFalse(raw['accounting_approval_performed'])
                self.assertEqual(doc,original)

    def test_receipt_final_total_labels_reject_adjustments_and_unsupported_geometry(self):
        for problem in ('adjustment','rounding_only','tax_summary','other_page','no_geometry','bad_amount'):
            with self.subTest(problem=problem):
                doc=layout('RECEIPT',[('Total Rounded',20,120,240,140),('106.00',400,120,500,140)])
                if problem=='adjustment': doc['m2_text_lines'][1]['text']='Rounding Adjustment'
                elif problem=='rounding_only': doc['m2_text_lines'][1]['text']='Rounding'
                elif problem=='tax_summary':
                    extra=layout('unused',[('GST Summary',20,80,240,100)])['m2_text_lines'][1]
                    extra['self_ref']='#/tax-summary';doc['m2_text_lines'].append(extra)
                elif problem=='other_page':
                    doc['pages']['2']=deepcopy(doc['pages']['1'])
                    doc['m2_text_lines'][2]['prov'][0]['page_no']=2
                elif problem=='no_geometry': doc['pages']={}
                else: doc['m2_text_lines'][2]['text']='10X.00'
                raw,_=contract(doc)
                self.assertIsNone(raw['fields']['total_amount']['value'])

    def test_receipt_competing_explicit_final_totals_stay_ambiguous(self):
        doc=layout('RECEIPT',[('Grand Total',20,80,240,100),('105.00',400,80,500,100),
            ('Total Rounded',20,120,240,140),('106.00',400,120,500,140)])
        raw,_=contract(doc)
        field=raw['fields']['total_amount']
        self.assertIsNone(field['value'])
        self.assertEqual(field['state'],'ambiguous')
        self.assertEqual({c['value'] for c in field['candidates']},{'105.00','106.00'})
    def test_party_spacing_requires_agreement_and_preserves_characters_and_boundaries(self):
        from services.extraction.layout.layout_headers import verified_spacing
        for original,readings,expected in [
            ('NorthStar Ltd',['North Star Ltd','North Star Ltd'],'North Star Ltd'),
            ('1234Main Road',['1234 Main Road','1234 Main Road'],'1234 Main Road'),
            ('NorthStar Ltd',['North Star Ltd','NorthStar Ltd'],None),
            ('NorthStar Ltd',['North Stars Ltd','North Stars Ltd'],None),
            ('North Star Ltd',['NorthStar Ltd','NorthStar Ltd'],None),
            ('NorthStar Ltd',['Northstar Ltd','Northstar Ltd'],None),
            ('NorthStar Ltd',['North-Star Ltd','North-Star Ltd'],None),
            ('NorthStar Ltd',[],None)]:
            with self.subTest(original=original,readings=readings):
                self.assertEqual(verified_spacing(original,readings),expected)

    def test_party_spacing_preserves_raw_source_geometry_and_review_contract(self):
        from services.extraction.layout.layout_headers import SPACING_POLICY
        doc=self.party_details()
        for index,original,corrected in [(4,'BlueRiver Ltd','Blue River Ltd'),
                                         (5,'10Printed Street Suite 5','10 Printed Street Suite 5')]:
            doc['m2_text_lines'][index]['text']=original
            doc['m2_text_lines'][index]['ocr_spacing']={'policy_version':SPACING_POLICY,'value':corrected,
                'readings':[corrected,corrected],'crop_box_pixels':[20,100,200,150],'page_image_size':[600,900]}
        original=deepcopy(doc)
        raw,mapped=contract(doc)
        self.assertEqual(raw['fields']['buyer_name']['value'],'Blue River Ltd')
        self.assertEqual(raw['fields']['supplier_address']['value'],'10 Printed Street Suite 5\nSource City 10000')
        self.assertIn('10Printed Street',raw['fields']['supplier_address']['raw_text'])
        ev=next(e for e in raw['fields']['supplier_address']['evidence'] if e['source_ref']=='#/m2_text_lines/5')
        self.assertEqual(ev['text'],'10Printed Street Suite 5')
        self.assertEqual(ev['bbox_original'],doc['m2_text_lines'][5]['prov'][0]['bbox'])
        self.assertEqual(ev['ocr_spacing']['readings'],['10 Printed Street Suite 5']*2)
        self.assertTrue(raw['review_required']); self.assertFalse(raw['accounting_approval_performed'])
        self.assertEqual(doc,original)

    def test_unverified_or_disagreeing_spacing_does_not_change_party_values(self):
        from services.extraction.layout.layout_headers import SPACING_POLICY
        for readings in (['Blue River Ltd','BlueRiver Ltd'],['Blue Rivers Ltd']*2):
            with self.subTest(readings=readings):
                doc=self.party_details(); item=doc['m2_text_lines'][4]; item['text']='BlueRiver Ltd'
                item['ocr_spacing']={'policy_version':SPACING_POLICY,'value':'Blue River Ltd','readings':readings}
                raw,_=contract(doc)
                self.assertEqual(raw['fields']['buyer_name']['value'],'BlueRiver Ltd')

    def party_details(self):
        return layout('INVOICE', [('Seller:',20,100,80,120),('Client:',320,100,380,120),
            ('North Star Ltd',20,140,230,160),('Blue River Ltd',320,140,550,160),
            ('10 Printed Street Suite 5',20,162,260,180),('20 Buyer Road',320,162,530,180),
            ('Source City 10000',20,182,250,200),('Destination City 20000',320,182,550,200),
            ('Tax Id: 001-23-4567',20,230,240,250),('Tax Id: BUYER-002',320,230,550,250),
            ('IBAN: GB00UNRELATED1234',20,260,260,280),('ITEMS',20,310,100,330)])

    def test_invoice_party_addresses_and_ids_keep_original_lines_geometry_and_roles(self):
        doc=self.party_details(); original=deepcopy(doc)
        raw,mapped=contract(doc)
        expected={'supplier_address':'10 Printed Street Suite 5\nSource City 10000',
                  'customer_address':'20 Buyer Road\nDestination City 20000',
                  'supplier_tax_id':'001-23-4567','customer_tax_id':'BUYER-002'}
        stored={f['field_key']:f for f in mapped['fields']}
        for key,value in expected.items():
            with self.subTest(field=key):
                field=raw['fields'][key]
                self.assertEqual(field['value'],value)
                self.assertEqual(stored[key]['current_value'],value)
                self.assertTrue(field['review_required'])
                for ev in field['evidence']:
                    source=next(x for x in doc['m2_text_lines'] if x['self_ref']==ev['source_ref'])
                    self.assertEqual(ev['bbox_original'],source['prov'][0]['bbox'])
                    self.assertEqual(ev['text'],source['text'])
        self.assertEqual(doc,original)
        self.assertNotIn('IBAN',raw['fields']['supplier_address']['value'])

    def test_invoice_party_detail_competing_names_do_not_select_an_address(self):
        doc=self.party_details()
        doc['m2_text_lines'].append({'text':'Vendor: Competing Company Ltd','self_ref':'#/competing',
            'prov':[{'page_no':1,'bbox':dict(l=20,t=40,r=300,b=60,coord_origin='TOPLEFT')}]})
        raw,_=contract(doc)
        self.assertIsNone(raw['fields']['supplier_name']['value'])
        self.assertNotIn('supplier_address',raw['fields'])
        self.assertNotIn('supplier_tax_id',raw['fields'])
        self.assertEqual(raw['fields']['customer_tax_id']['value'],'BUYER-002')

    def test_invoice_party_details_need_same_page_reliable_geometry(self):
        for problem in ('missing_page','missing_prov','zero_height','other_page'):
            with self.subTest(problem=problem):
                doc=self.party_details()
                if problem=='missing_page': doc['pages']={}
                elif problem=='missing_prov': doc['m2_text_lines'][1]['prov']=[]
                elif problem=='zero_height': doc['m2_text_lines'][1]['prov'][0]['bbox']['b']=100
                else:
                    doc['pages']['2']=deepcopy(doc['pages']['1'])
                    for x in doc['m2_text_lines'][5:]:
                        x['prov'][0]['page_no']=2
                raw,_=contract(doc)
                self.assertNotIn('supplier_address',raw['fields'])
                self.assertNotIn('supplier_tax_id',raw['fields'])

    def test_invoice_competing_printed_tax_ids_are_ambiguous_not_replaced_by_bank_number(self):
        doc=self.party_details()
        doc['m2_text_lines'].append({'text':'Tax Id: OTHER-003','self_ref':'#/second-tax',
            'prov':[{'page_no':1,'bbox':dict(l=20,t=282,r=240,b=300,coord_origin='TOPLEFT')}]})
        raw,_=contract(doc); field=raw['fields']['supplier_tax_id']
        self.assertIsNone(field['value']); self.assertEqual(field['state'],'ambiguous')
        self.assertEqual({c['value'] for c in field['candidates']},{'001-23-4567','OTHER-003'})

    def test_invoice_overlapping_address_lines_require_review(self):
        doc=self.party_details()
        doc['m2_text_lines'].append({'text':'99 Other Street','self_ref':'#/other-address',
            'prov':[{'page_no':1,'bbox':dict(l=20,t=162,r=260,b=180,coord_origin='TOPLEFT')}]})
        raw,_=contract(doc); field=raw['fields']['supplier_address']
        self.assertIsNone(field['value']); self.assertEqual(field['state'],'ambiguous')
        self.assertIn('99 Other Street',{c['value'] for c in field['candidates']})

    def test_invoice_duplicate_address_lines_do_not_duplicate_public_text(self):
        doc=self.party_details()
        duplicate=deepcopy(doc['m2_text_lines'][5]); duplicate['self_ref']='#/duplicate'
        doc['m2_text_lines'].append(duplicate)
        raw,_=contract(doc)
        self.assertEqual(raw['fields']['supplier_address']['value'],'10 Printed Street Suite 5\nSource City 10000')
        self.assertTrue(any(e['source_ref']=='#/duplicate' for e in raw['fields']['supplier_address']['evidence']))

    def test_invoice_unlabelled_ids_and_arbitrary_prose_are_not_party_details(self):
        doc=self.party_details()
        for x in doc['m2_text_lines']:
            if x['text'].startswith('Tax Id:'): x['text']=x['text'].split(':',1)[1].strip()
            elif x['text'].startswith('10 Printed'): x['text']='Customer support department'
        raw,_=contract(doc)
        self.assertNotIn('supplier_tax_id',raw['fields']); self.assertNotIn('customer_tax_id',raw['fields'])
        self.assertNotIn('supplier_address',raw['fields'])

    def test_invoice_separate_tax_id_label_keeps_neighbour_source(self):
        doc=self.party_details()
        doc['m2_text_lines'][9]['text']='Tax Id:'
        doc['m2_text_lines'].append({'text':'001-23-4567','self_ref':'#/tax-value',
            'prov':[{'page_no':1,'bbox':dict(l=20,t=252,r=240,b=270,coord_origin='TOPLEFT')}]})
        raw,_=contract(doc)
        self.assertEqual(raw['fields']['supplier_tax_id']['value'],'001-23-4567')
        self.assertTrue(any(e['source_ref']=='#/tax-value' for e in raw['fields']['supplier_tax_id']['evidence']))

    def test_invoice_tax_id_label_and_value_on_same_row(self):
        doc=self.party_details(); doc['m2_text_lines'][9]['text']='Tax Id:'
        doc['m2_text_lines'][9]['prov'][0]['bbox']['r']=90
        doc['m2_text_lines'].append({'text':'001-23-4567','self_ref':'#/tax-value',
            'prov':[{'page_no':1,'bbox':dict(l=100,t=230,r=240,b=250,coord_origin='TOPLEFT')}]})
        raw,_=contract(doc)
        self.assertEqual(raw['fields']['supplier_tax_id']['value'],'001-23-4567')

    def test_invoice_postal_box_address_preserves_printed_postal_line(self):
        doc=self.party_details()
        doc['m2_text_lines'][6]['text']='PSC 1234, Box 5678'
        doc['m2_text_lines'][8]['text']='APO AP 12345'
        raw,_=contract(doc)
        self.assertEqual(raw['fields']['customer_address']['value'],'PSC 1234, Box 5678\nAPO AP 12345')

    def test_invoice_tax_identifier_does_not_reuse_amount_or_gstin(self):
        doc=self.party_details(); doc['m2_text_lines'][9]['text']='Tax Id: 100.00'
        doc['m2_text_lines'][10]['text']='GSTIN: EXPLICIT-002'
        raw,_=contract(doc)
        self.assertNotIn('supplier_tax_id',raw['fields'])
        self.assertNotIn('customer_tax_id',raw['fields'])

    def test_invoice_explicit_party_details_without_geometry_stay_null(self):
        doc=layout('INVOICE',[('Supplier address: 10 Printed Street',20,60,300,80),
                              ('Customer tax id: SOURCE-001',20,100,300,120)])
        doc['pages']={}
        raw,mapped=contract(doc)
        for key in ('supplier_address','customer_tax_id'):
            self.assertIsNone(raw['fields'][key]['value'])
            self.assertEqual(raw['fields'][key]['state'],'missing')
            self.assertTrue(raw['fields'][key]['candidates'])

    def test_invoice_issue_date_keeps_ambiguous_printed_value_and_geometry(self):
        for value in ('04/01/2017', '12/08/2013', '04/09/2014'):
            with self.subTest(value=value):
                doc=layout('INVOICE', [('Date of issue:',60,60,165,80),
                                       (value,380,60,475,80)])
                original=deepcopy(doc)
                raw,mapped=contract(doc)
                field=raw['fields']['invoice_date']
                self.assertIsNone(field['value'])
                self.assertEqual(field['state'],'ambiguous')
                self.assertEqual(field['candidates'][0]['value'],value)
                self.assertEqual([e['source_ref'] for e in field['evidence']],
                                 ['#/m2_text_lines/1','#/m2_text_lines/2'])
                self.assertEqual(field['evidence'][1]['bbox_original'],
                                 doc['m2_text_lines'][2]['prov'][0]['bbox'])
                self.assertIsNone(next(f for f in mapped['fields'] if f['field_key']=='date')['current_value'])
                self.assertEqual(doc,original)

    def test_invoice_issue_date_unambiguous_values_still_normalize(self):
        for value,expected in (('25/09/2023','2023-09-25'),('06/25/2014','2014-06-25'),
                               ('02/02/2024','2024-02-02'),('2024-02-29','2024-02-29')):
            with self.subTest(value=value):
                raw,_=contract(layout('INVOICE', [('Date of issue:',60,60,165,80),
                                                 (value,380,60,475,80)]))
                self.assertEqual(raw['fields']['invoice_date']['value'],expected)

    def test_invoice_issue_date_conflicting_values_require_review(self):
        raw,_=contract(layout('INVOICE', [('Date of issue:',60,60,165,80),
                                         ('25/09/2023',380,60,475,80),
                                         ('Invoice date: 2023-09-26',60,100,350,120)]))
        field=raw['fields']['invoice_date']
        self.assertIsNone(field['value'])
        self.assertEqual(field['state'],'ambiguous')
        self.assertEqual({c['value'] for c in field['candidates']},{'2023-09-25','2023-09-26'})

    def test_invoice_structured_four_columns_preserve_values_and_evidence(self):
        doc=item_table(['Description','Qty','Unit Price','Line Total'],[['Multiword office service','2','5.00','10.00']])
        original=deepcopy(doc)
        raw,mapped=contract(doc)
        self.assertEqual(len(mapped['line_items']),1)
        row=raw['line_items'][0]
        self.assertEqual({k:v['value'] for k,v in row.items()},
            {'description':'Multiword office service','quantity':'2','unit_price':'5.00','line_total':'10.00'})
        ev=row['line_total']['evidence'][-1]
        self.assertEqual(ev['source_ref'],'#/tables/0/data/table_cells/7')
        self.assertEqual(ev['bbox_original'],doc['tables'][0]['data']['table_cells'][7]['bbox'])
        self.assertEqual(ev['page'],1)
        self.assertEqual(doc,original)
        self.assertTrue(raw['review_required']); self.assertFalse(raw['accounting_approval_performed'])

    def test_invoice_two_columns_do_not_invent_quantity_or_unit_price(self):
        raw,_=contract(item_table(['Particulars','Amount'],[['Printed service','USD 10.00']]))
        row=raw['line_items'][0]
        self.assertEqual(row['line_total']['value'],'10.00')
        self.assertIn('USD',row['line_total']['raw_text'])
        self.assertIsNone(row['quantity']['value']); self.assertIsNone(row['unit_price']['value'])

    def test_invoice_currency_qualified_columns_and_net_total_stop(self):
        doc=item_table(['Description','Qty','Unit Price (EUR)','Tax Rate','Amount (EUR)'],[
            ['Consumption','1000.0','0.12','23%','120.00'],
            ['Connection charge','1.0','50.00','23%','50.00'],
            ['Adjustment (credit)','1.0','-20.00','23%','-20.00'],
            ['Net Total','','','','150.00'],['€11.13 €5.40','','','','34.50']])
        raw,_=contract(doc)
        self.assertEqual(len(raw['line_items']),3)
        self.assertEqual([r['line_total']['value'] for r in raw['line_items']],['120.00','50.00','-20.00'])
        self.assertEqual(raw['line_items'][0]['unit_price']['value'],'0.12')
        self.assertTrue(any(e.get('column_heading')=='Amount (EUR)' for e in raw['line_items'][0]['line_total']['evidence']))

    def test_invoice_ocr_currency_header_preserves_amount_and_excludes_net_total(self):
        doc=layout('INVOICE',[('Description',20,100,200,120),('Qty',250,100,290,120),
            ('Unit Price (EUR)',320,100,410,120),('Amount (EUR)',450,100,570,120),
            ('Consumption',20,140,200,160),('2',250,140,290,160),('5.00',320,140,410,160),
            ('10.00',450,140,570,160),('Net Total',20,180,200,200),('10.00',450,180,570,200)])
        raw,_=contract(doc)
        self.assertEqual(len(raw['line_items']),1)
        self.assertEqual(raw['line_items'][0]['line_total']['value'],'10.00')

    def test_invoice_net_vat_gross_keeps_printed_net_basis(self):
        doc=item_table(['No.','Description','Qty','UM','Net Price','Net Worth','VAT %','Gross Worth'],
            [['1.','Multiword product','2,00','pcs','5,00','10,00','10%','11,00']])
        raw,_=contract(doc);row=raw['line_items'][0]
        self.assertEqual(row['quantity']['value'],'2.00')
        self.assertEqual(row['unit_price']['value'],'5.00')
        self.assertEqual(row['line_total']['value'],'10.00')
        self.assertTrue(any(e.get('amount_basis')=='net_excluding_tax' for e in row['line_total']['evidence']))

    def test_invoice_structured_multiline_description_uses_same_source_row(self):
        doc=item_table(['Item','Quantity','Rate','Amount'],
            [['First product\nwith continuation','2','5.00','10.00'],['Next product','3','7.00','21.00']])
        doc['tables'][0]['data']['table_cells'][4]['bbox']['b']=174
        raw,_=contract(doc)
        self.assertEqual(raw['line_items'][0]['description']['value'],'First product with continuation')
        self.assertEqual(raw['line_items'][0]['line_total']['value'],'10.00')
        self.assertEqual(raw['line_items'][1]['line_total']['value'],'21.00')

    def test_invoice_merged_cell_keeps_other_cells_and_other_rows(self):
        doc=item_table(['Description','Qty','Unit Price','Amount'],
            [['First product','2','5.00 10.00',''],['Second product','3','7.00','21.00']])
        cells=doc['tables'][0]['data']['table_cells'];cells[6]['col_span']=2
        raw,_=contract(doc)
        self.assertEqual(len(raw['line_items']),2)
        row=raw['line_items'][0]
        self.assertEqual(row['quantity']['value'],'2')
        self.assertIsNone(row['unit_price']['value']);self.assertIsNone(row['line_total']['value'])
        self.assertEqual(row['unit_price']['state'],'ambiguous')
        self.assertEqual(row['unit_price']['candidates'][0]['raw_text'],'5.00 10.00')
        self.assertEqual(raw['line_items'][1]['line_total']['value'],'21.00')

    def test_invoice_missing_cell_is_partial_not_calculated(self):
        doc=item_table(['Description','Qty','Unit Price','Amount'],[['Printed product','2','5.00','']])
        raw,_=contract(doc)
        self.assertEqual(len(raw['line_items']),1)
        self.assertIsNone(raw['line_items'][0]['line_total']['value'])
        self.assertEqual(raw['line_items'][0]['line_total']['state'],'missing')

    def test_invoice_duplicate_streams_and_tables_do_not_duplicate_rows(self):
        doc=item_table(['Description','Qty','Unit Price','Amount'],[['Product','2','5.00','10.00']])
        for i,c in enumerate(doc['tables'][0]['data']['table_cells']):
            doc['m2_text_lines'].append({'text':c['text'],'self_ref':f'#/m2_text_lines/{i+1}',
                'prov':[{'page_no':1,'bbox':deepcopy(c['bbox'])}]})
        duplicate=deepcopy(doc['tables'][0]);duplicate['self_ref']='#/tables/1';doc['tables'].append(duplicate)
        raw,_=contract(doc)
        self.assertEqual(len(raw['line_items']),1)
        refs={e['source_ref'] for e in raw['line_items'][0]['line_total']['evidence']}
        self.assertIn('#/m2_text_lines/8',refs)
        self.assertIn('#/tables/0/data/table_cells/7',refs)
        self.assertIn('#/tables/1/data/table_cells/7',refs)
        doc['m2_text_lines'][-1]['text']='12.00'
        raw,_=contract(doc)
        self.assertEqual(len(raw['line_items']),1)
        field=raw['line_items'][0]['line_total']
        self.assertIsNone(field['value'])
        self.assertEqual({c['value'] for c in field['candidates']},{'10.00','12.00'})

    def test_invoice_bad_retained_merger_does_not_erase_structured_row(self):
        doc=item_table(['Description','Qty','Unit Price','Amount'],[['Product','2','5.00','10.00']])
        c=doc['tables'][0]['data']['table_cells'][7]
        doc['m2_text_lines'].append({'text':'5.00 10.00','self_ref':'#/m2_text_lines/99',
            'prov':[{'page_no':1,'bbox':deepcopy(c['bbox'])}]})
        raw,_=contract(doc)
        self.assertEqual(raw['line_items'][0]['line_total']['value'],'10.00')

    def test_invoice_equivalent_numeric_representations_are_not_conflicts(self):
        doc=item_table(['Description','Amount'],[['Product','10.00']])
        second=deepcopy(doc['tables'][0]);second['self_ref']='#/tables/1'
        second['data']['table_cells'][-1]['text']='10';doc['tables'].append(second)
        raw,_=contract(doc)
        self.assertEqual(len(raw['line_items']),1)
        field=raw['line_items'][0]['line_total']
        self.assertEqual(field['state'],'resolved')
        self.assertEqual(field['value'],'10.00')
        self.assertEqual({c['value'] for c in field['candidates']},{'10','10.00'})

    def test_invoice_multi_page_structured_rows_remain_separate(self):
        doc=item_table(['Description','Amount'],[['Same printed product','10.00']])
        doc['pages']['2']=deepcopy(doc['pages']['1'])
        second=deepcopy(doc['tables'][0]);second['self_ref']='#/tables/1';second['prov'][0]['page_no']=2
        second['data']['table_cells'][-1]['text']='20.00';doc['tables'].append(second)
        raw,_=contract(doc)
        self.assertEqual([r['line_total']['value'] for r in raw['line_items']],['10.00','20.00'])
        self.assertEqual({r['line_total']['evidence'][-1]['page'] for r in raw['line_items']},{1,2})

    def test_invoice_total_and_summary_tables_are_not_items(self):
        doc=item_table(['Description','Amount'],[['Actual product','10.00'],['Total','10.00'],['SUMMARY','10.00']])
        tax=item_table(['Net Worth','VAT','Gross Worth'],[['10.00','1.00','11.00']])['tables'][0]
        tax['self_ref']='#/tables/1';doc['tables'].append(tax)
        raw,_=contract(doc)
        self.assertEqual(len(raw['line_items']),1)
        self.assertEqual(raw['line_items'][0]['description']['value'],'Actual product')

    def test_invoice_unsupported_geometry_or_currency_stays_null(self):
        for problem in ['no_pages','missing_cell_box','zero_height','currency_conflict','monetary_quantity']:
            with self.subTest(problem=problem):
                doc=item_table(['Description','Qty','Unit Price','Amount'],[['Product','2','5.00','10.00']])
                if problem=='no_pages':doc['pages']={}
                elif problem=='missing_cell_box':doc['tables'][0]['data']['table_cells'][7]['bbox']=None
                elif problem=='zero_height':doc['tables'][0]['data']['table_cells'][7]['bbox']['b']=140
                elif problem=='currency_conflict':
                    doc['m2_text_lines'].append({'text':'Currency: EUR','self_ref':'#/m2_text_lines/1','prov':[]})
                    doc['tables'][0]['data']['table_cells'][7]['text']='USD 10.00'
                else:doc['tables'][0]['data']['table_cells'][5]['text']='USD 2'
                raw,_=contract(doc)
                if problem=='no_pages':self.assertEqual(raw['line_items'],[])
                else:self.assertIsNone(raw['line_items'][0]['quantity' if problem=='monetary_quantity' else 'line_total']['value'])

    def test_invoice_ocr_net_columns_and_partial_rows_use_same_row(self):
        doc=layout('INVOICE',[('Description',20,100,200,120),('Qty',250,100,290,120),
            ('Net Price',320,100,390,120),('Net Worth',420,100,490,120),('Gross Worth',510,100,590,120),
            ('Product with words',20,140,200,160),('2,00',250,140,290,160),('5,00',320,140,390,160),
            ('10,00',420,140,490,160),('11,00',510,140,590,160),
            ('Partial product',20,180,200,200),('3,00',250,180,290,200),
            ('SUMMARY',20,240,200,260),('100,00',420,280,490,300)])
        raw,_=contract(doc)
        self.assertEqual(len(raw['line_items']),2)
        self.assertEqual(raw['line_items'][0]['line_total']['value'],'10.00')
        self.assertEqual(raw['line_items'][1]['quantity']['value'],'3.00')
        self.assertIsNone(raw['line_items'][1]['line_total']['value'])

    def test_summary_column_semantics_and_decimal_comma_do_not_guess_dollar_currency(self):
        doc = financial_summary()
        original = deepcopy(doc)
        raw, canonical = contract(doc)
        expected = {'total_excluding_tax':'100.00', 'tax_amount':'10.00',
                    'total_including_tax':'110.00', 'total_amount':'110.00'}
        for key, value in expected.items(): self.assertEqual(raw['fields'][key]['value'], value)
        self.assertIsNone(raw['fields']['subtotal']['value'])
        self.assertIsNone(raw['fields']['currency']['value'])
        self.assertNotIn('amount_due', raw['fields'])
        total = raw['fields']['total_amount']
        self.assertEqual({e['source_ref'] for e in total['evidence']},
                         {'#/m2_text_lines/5','#/m2_text_lines/10','#/m2_text_lines/13'})
        self.assertEqual(total['evidence'][-1]['bbox_original'], doc['m2_text_lines'][13]['prov'][0]['bbox'])
        self.assertEqual(total['evidence'][-1]['currency_qualifier'], '$')
        self.assertEqual(doc, original)
        self.assertTrue(raw['review_required']); self.assertFalse(raw['accounting_approval_performed'])

    def test_summary_iso_and_unambiguous_currency_symbols_preserve_units(self):
        for unit, code in [('INR','INR'), ('USD','USD'), ('€','EUR'), ('₹','INR')]:
            with self.subTest(unit=unit):
                raw, _ = contract(financial_summary(unit, '1,000.00','100.00','1,100.00'))
                self.assertEqual(raw['fields']['currency']['value'], code)
                self.assertEqual(raw['fields']['total_amount']['value'], '1100.00')

    def test_summary_zero_tax_is_a_printed_amount_not_a_rate(self):
        doc=financial_summary('EUR','100,00','0,00','100,00')
        doc['m2_text_lines'][6]['text']='0%'
        raw,_=contract(doc)
        self.assertEqual(raw['fields']['tax_amount']['value'],'0.00')
        self.assertEqual(raw['fields']['tax_amount']['state'],'resolved')

    def test_summary_malformed_values_are_not_silently_repaired(self):
        for amount in ['1,23,45', '10%', 'EUR 10 USD', 'not printed']:
            with self.subTest(amount=amount):
                doc=financial_summary('EUR')
                doc['m2_text_lines'][-1]['text']=amount
                raw,_=contract(doc)
                self.assertIsNone(raw['fields']['total_amount']['value'])

    def test_summary_does_not_copy_total_into_settlement_or_adjustment_fields(self):
        doc=financial_summary('EUR')
        extra=layout('INVOICE',[('Amount due: 80.00',20,260,250,280),
            ('Prepaid amount: 30.00',20,300,250,320),('Charges: 5.00',20,340,250,360),
            ('Previous balance: 15.00',20,380,250,400)])['m2_text_lines'][1:]
        for i,x in enumerate(extra): x['self_ref']=f'#/m2_text_lines/{90+i}'
        doc['m2_text_lines'].extend(extra)
        raw,_=contract(doc)
        for key,value in [('amount_due','80.00'),('prepaid_amount','30.00'),
                          ('document_charges','5.00'),('total_amount','110.00')]:
            self.assertEqual(raw['fields'][key]['value'],value)
        self.assertNotIn('previous_balance',raw['fields'])

    def test_summary_due_is_distinct_from_document_total_and_inclusive_total(self):
        raw,_=contract(financial_summary('EUR',footer='Total Amount Due'))
        self.assertEqual(raw['fields']['amount_due']['value'],'110.00')
        self.assertEqual(raw['fields']['total_including_tax']['value'],'110.00')
        self.assertIsNone(raw['fields']['total_amount']['value'])
        raw,_=contract(layout('INVOICE',[('Total Amount Due',20,100,220,120),('20.00',20,140,100,160)]))
        self.assertEqual(raw['fields']['amount_due']['value'],'20.00')
        self.assertIsNone(raw['fields']['total_amount']['value'])

    def test_summary_missing_value_is_not_manufactured_by_arithmetic(self):
        doc=financial_summary('EUR')
        doc['m2_text_lines']=[x for i,x in enumerate(doc['m2_text_lines']) if i not in {9,13}]
        raw,_=contract(doc)
        self.assertEqual(raw['fields']['tax_amount']['value'],'10.00')
        self.assertIsNone(raw['fields']['total_amount']['value'])
        self.assertIsNone(raw['fields'].get('total_including_tax',{}).get('value'))

    def test_summary_requires_context_and_credible_geometry(self):
        for problem in ['no_summary','missing_column','no_total','no_page_size','wrong_page']:
            with self.subTest(problem=problem):
                doc=financial_summary('EUR')
                if problem=='no_summary': doc['m2_text_lines'][1]['text']='Other information'
                elif problem=='missing_column': doc['m2_text_lines'][4]['text']='Unknown heading'
                elif problem=='no_total': doc['m2_text_lines'][10]['text']='Other row'
                elif problem=='no_page_size': doc['pages']={}
                else:
                    doc['pages']['2']=doc['pages']['1']
                    for x in doc['m2_text_lines'][10:]: x['prov'][0]['page_no']=2
                raw,_=contract(doc)
                self.assertIsNone(raw['fields']['total_amount']['value'])
                self.assertIsNone(raw['fields'].get('total_excluding_tax',{}).get('value'))

    def test_summary_conflicting_totals_and_currency_remain_ambiguous(self):
        doc=financial_summary('EUR')
        doc['m2_text_lines'].extend(layout('INVOICE',[('Total Amount: 999.00',20,240,300,260)])['m2_text_lines'][1:])
        doc['m2_text_lines'][-1]['self_ref']='#/m2_text_lines/99'
        raw,_=contract(doc)
        self.assertIsNone(raw['fields']['total_amount']['value'])
        self.assertEqual({c['value'] for c in raw['fields']['total_amount']['candidates']},{'110.00','999.00'})
        doc=financial_summary('INR')
        doc['m2_text_lines'][-1]['text']='EUR 110,00'
        raw,_=contract(doc)
        self.assertIsNone(raw['fields']['currency']['value'])
        self.assertIsNone(raw['fields']['total_amount']['value'])
        self.assertIsNone(raw['fields']['total_excluding_tax']['value'])

    def test_summary_repeated_gross_columns_retain_competing_candidates(self):
        doc=financial_summary('EUR')
        extra=layout('INVOICE',[('Gross Worth',560,100,590,120),('EUR 120,00',560,180,590,200)])['m2_text_lines'][1:]
        for i,x in enumerate(extra): x['self_ref']=f'#/m2_text_lines/{90+i}'
        doc['m2_text_lines'].extend(extra)
        raw,_=contract(doc)
        self.assertIsNone(raw['fields']['total_amount']['value'])
        self.assertEqual({c['value'] for c in raw['fields']['total_amount']['candidates']},{'110.00','120.00'})

    def test_explicit_financial_columns_can_have_values_directly_beneath_labels(self):
        for tax_label,total_label in [('Tax Amount','Total Amount'),('TOTAL TAX','GRAND TOTAL')]:
            with self.subTest(tax_label=tax_label,total_label=total_label):
                doc=layout('INVOICE',[('Subtotal',20,100,120,120),(tax_label,220,100,320,120),
                    (total_label,420,100,550,120),('EUR 100,00',20,140,120,160),
                    ('EUR 10,00',220,140,320,160),('EUR 110,00',420,140,550,160)])
                original=deepcopy(doc)
                raw,_=contract(doc)
                for key,value in [('subtotal','100.00'),('tax_amount','10.00'),('total_amount','110.00')]:
                    self.assertEqual(raw['fields'][key]['value'],value)
                total=raw['fields']['total_amount']
                self.assertEqual({e['source_ref'] for e in total['evidence']},
                                 {'#/m2_text_lines/3','#/m2_text_lines/6'})
                self.assertEqual(total['evidence'][-1]['bbox_original'],doc['m2_text_lines'][6]['prov'][0]['bbox'])
                self.assertNotIn('amount_due',raw['fields'])
                self.assertNotIn('amount_paid',raw['fields'])
                self.assertTrue(raw['review_required'])
                self.assertFalse(raw['accounting_approval_performed'])
                self.assertEqual(doc,original)

    def test_total_tax_and_grand_total_keep_summary_guards(self):
        for problem in ('dollar','missing_value','wrong_page','no_geometry','item_header','conflict'):
            with self.subTest(problem=problem):
                doc=layout('TAX INVOICE',[('TOTAL TAX',220,100,320,120),
                    ('GRAND TOTAL',420,100,550,120),('EUR 10.00',220,140,320,160),
                    ('EUR 110.00',420,140,550,160)])
                if problem=='dollar':
                    for item in doc['m2_text_lines'][3:]: item['text']=item['text'].replace('EUR','$')
                elif problem=='missing_value': doc['m2_text_lines'].pop()
                elif problem=='wrong_page':
                    doc['pages']['2']=deepcopy(doc['pages']['1'])
                    doc['m2_text_lines'][-1]['prov'][0]['page_no']=2
                elif problem=='no_geometry': doc['pages']={}
                else:
                    text='Description' if problem=='item_header' else 'Total Amount: 999.00'
                    extra=layout('INVOICE',[(text,20,100,200,120)])['m2_text_lines'][1]
                    extra['self_ref']='#/m2_text_lines/99'
                    doc['m2_text_lines'].append(extra)
                raw,_=contract(doc)
                if problem=='dollar':
                    self.assertEqual(raw['fields']['total_amount']['value'],'110.00')
                    self.assertIsNone(raw['fields']['currency']['value'])
                else:
                    self.assertIsNone(raw['fields']['total_amount']['value'])
                if problem=='conflict':
                    self.assertEqual(raw['fields']['total_amount']['state'],'ambiguous')
                    self.assertEqual({c['value'] for c in raw['fields']['total_amount']['candidates']},
                                     {'110.00','999.00'})

    def test_summary_structured_cells_recover_split_values_without_using_merged_text(self):
        doc=financial_summary('INR')
        cells=[]
        for i,x in enumerate(doc['m2_text_lines'][2:]):
            cells.append({'text':x['text'],'row_span':1,'col_span':1,'bbox':deepcopy(x['prov'][0]['bbox']),
                          'start_row_offset_idx':i//4, 'start_col_offset_idx':i%4})
        doc['tables']=[{'self_ref':'#/tables/0','prov':[{'page_no':1}],
                        'data':{'table_cells':cells,'num_cols':4,'num_rows':3}}]
        doc['texts']=[deepcopy(doc['m2_text_lines'][1])]
        doc['texts'][0]['self_ref']='#/texts/0'
        doc['m2_text_lines'][11]['text']='INR 100,00 INR 10,00'
        doc['m2_text_lines'][12]['text']='INR'
        raw,_=contract(doc)
        self.assertEqual(raw['fields']['tax_amount']['value'],'10.00')
        self.assertTrue(any(e['source_ref'].startswith('#/tables/0/data/table_cells/')
                            for e in raw['fields']['tax_amount']['evidence']))
        doc['tables'][0]['data']['table_cells'][-2]['text']='INR 10'
        raw,_=contract(doc)
        self.assertEqual(raw['fields']['tax_amount']['value'],'10.00')

    def test_item_table_headers_cannot_become_document_summary_fields(self):
        doc=financial_summary('EUR')
        doc['m2_text_lines'][1]['text']='ITEMS'
        doc['m2_text_lines'][2]['text']='Description'
        raw,_=contract(doc)
        self.assertIsNone(raw['fields'].get('total_excluding_tax',{}).get('value'))
        self.assertIsNone(raw['fields'].get('total_including_tax',{}).get('value'))

    def test_invoice_party_columns_keep_names_and_original_geometry(self):
        doc = layout('INVOICE', [('Seller:',20,100,80,120), ('Client:',320,100,380,120),
            ('North Star Trading',20,140,230,160), ('Blue River Services',320,140,550,160),
            ('12 Buyer Avenue',320,165,550,185)])
        original = deepcopy(doc)
        raw, canonical = contract(doc)
        self.assertEqual(raw['fields']['supplier_name']['value'], 'North Star Trading')
        self.assertEqual(raw['fields']['buyer_name']['value'], 'Blue River Services')
        supplier = raw['fields']['supplier_name']
        self.assertEqual({e['source_ref'] for e in supplier['evidence']},
                         {'#/m2_text_lines/1', '#/m2_text_lines/3'})
        self.assertEqual(supplier['evidence'][1]['bbox_original'], doc['m2_text_lines'][3]['prov'][0]['bbox'])
        self.assertEqual(doc, original)
        self.assertTrue(raw['review_required'])
        self.assertFalse(raw['accounting_approval_performed'])

    def test_invoice_multiline_company_suffix_keeps_both_source_lines(self):
        raw, _ = contract(layout('INVOICE', [('Seller:',20,100,80,120),
            ('North Star Trading',20,140,230,160), ('Sdn Bhd',20,165,120,185),
            ('12 Supplier Road',20,190,230,210)]))
        field = raw['fields']['supplier_name']
        self.assertEqual(field['value'], 'North Star Trading\nSdn Bhd')
        self.assertEqual(len(field['evidence']), 3)

    def test_invoice_merged_party_labels_without_positions_stay_missing(self):
        raw, _ = contract(layout('INVOICE', [('Seller:\nClient:',20,100,380,120),
            ('North Star Ltd',20,140,200,160), ('Blue River Ltd',320,140,550,160)]))
        self.assertIsNone(raw['fields']['supplier_name']['value'])
        self.assertIsNone(raw['fields'].get('buyer_name', {}).get('value'))

    def test_invoice_merged_explicit_party_values_remain_distinct(self):
        raw, _ = contract(layout('INVOICE', [('Seller: North Star Ltd\nClient: Blue River Ltd',20,100,550,180)]))
        self.assertEqual(raw['fields']['supplier_name']['value'], 'North Star Ltd')
        self.assertEqual(raw['fields']['buyer_name']['value'], 'Blue River Ltd')

    def test_invoice_client_is_not_promoted_to_missing_supplier(self):
        for cells in [[('Client:',320,100,380,120), ('Blue River Ltd',320,140,550,160)],
                      [('Client: Blue River Ltd',20,100,550,120)],
                      [('Seller: Client',20,100,180,120)]]:
            with self.subTest(cells=cells):
                raw, _ = contract(layout('INVOICE', cells))
                self.assertIsNone(raw['fields']['supplier_name']['value'])

    def test_invoice_competing_supplier_neighbours_require_review(self):
        raw, _ = contract(layout('INVOICE', [('Seller:',20,100,80,120),
            ('Right Company Ltd',100,100,290,120), ('Below Company Ltd',20,140,240,160)]))
        field = raw['fields']['supplier_name']
        self.assertIsNone(field['value'])
        self.assertEqual(field['state'], 'ambiguous')
        self.assertEqual({c['value'] for c in field['candidates']}, {'Right Company Ltd', 'Below Company Ltd'})

    def test_invoice_duplicate_supplier_roles_retain_conflicting_names(self):
        raw, _ = contract(layout('INVOICE', [('Seller: First Company Ltd',20,100,300,120),
            ('Vendor: Second Company Ltd',20,150,300,170)]))
        self.assertIsNone(raw['fields']['supplier_name']['value'])
        self.assertEqual(raw['fields']['supplier_name']['state'], 'ambiguous')

    def test_invoice_standalone_party_requires_reliable_same_page_geometry(self):
        for problem in ['missing_page_size', 'missing_prov', 'zero_height', 'other_page']:
            with self.subTest(problem=problem):
                doc = layout('INVOICE', [('Seller:',20,100,80,120), ('North Star Ltd',20,140,230,160)])
                if problem == 'missing_page_size': doc['pages'] = {}
                elif problem == 'missing_prov': doc['m2_text_lines'][1]['prov'] = []
                elif problem == 'zero_height': doc['m2_text_lines'][1]['prov'][0]['bbox']['b'] = 100
                else:
                    doc['pages']['2'] = doc['pages']['1']
                    doc['m2_text_lines'][2]['prov'][0]['page_no'] = 2
                raw, _ = contract(doc)
                self.assertIsNone(raw['fields']['supplier_name']['value'])

    def test_invoice_bill_to_is_buying_party_only(self):
        raw, _ = contract(layout('INVOICE', [('Bill To:',320,100,400,120),
            ('Blue River Ltd',320,140,550,160)]))
        self.assertEqual(raw['fields']['buyer_name']['value'], 'Blue River Ltd')
        self.assertIsNone(raw['fields']['supplier_name']['value'])

    def test_invoice_previous_explicit_and_letterhead_layouts_preserved(self):
        for cells in [[('Supplier: North Star Ltd',20,100,300,120)],
                      [('North Star Ltd',20,50,300,70)]]:
            with self.subTest(cells=cells):
                raw, _ = contract(layout('INVOICE', cells))
                self.assertEqual(raw['fields']['supplier_name']['value'], 'North Star Ltd')

    def test_title_free_receipt_needs_item_geometry_total_and_cash(self):
        doc=layout('Unidentified print',[('Item A',20,100,200,120),('17500',400,100,570,120),
            ('Item B',20,140,200,160),('46000',400,140,570,160),('TOTAL',20,180,200,200),('63500',400,180,570,200),
            ('CASH',20,220,200,240),('63500',400,220,570,240)])
        raw,mapped=contract(doc)
        self.assertEqual(raw['document_type']['value'],'receipt')
        self.assertIsNone(raw['fields']['currency']['value'])
        self.assertEqual([r['amount'] for r in mapped['line_items']],['17500','46000'])
        doc['m2_text_lines']=[line for line in doc['m2_text_lines'] if line['text']!='CASH']
        raw,_=contract(doc)
        self.assertEqual(raw['document_type']['value'],'unknown')

    def test_invoice_explicit_columns_roundtrip_without_financial_floats(self):
        doc = layout('INVOICE', [('Currency: MYR',10,40,180,60),('Description',10,100,140,120),
            ('Qty',250,100,290,120),('Unit Price',330,100,410,120),('Amount',480,100,570,120),
            ('Synthetic service',10,140,180,160),('2',250,140,290,160),('25.1250',330,140,410,160),('50.25',480,140,570,160),
            ('TOTAL',10,190,100,210),('50.25',480,190,570,210)])
        original = deepcopy(doc)
        raw, mapped = contract(doc)
        self.assertEqual(len(mapped['line_items']), 1)
        row = mapped['line_items'][0]
        self.assertEqual((row['description'],row['quantity'],row['unit_price'],row['amount']), ('Synthetic service','2','25.1250','50.25'))
        self.assertEqual(row['evidence']['amount'][0]['source_ref'], '#/m2_text_lines/9')
        self.assertEqual(doc,original)

    def test_receipt_ordinary_item_lines_do_not_invent_quantity_or_price(self):
        doc = layout('RECEIPT', [('Currency: MYR',10,40,200,60),
            ('Synthetic shirt',20,100,200,120),('200000001',20,125,180,145),('RM 20.00 N',400,140,570,160),
            ('Synthetic trousers',20,170,220,190),('200000002',20,195,180,215),('RM 30.00 N',400,210,570,230),
            ('Subtotal',20,250,160,270),('RM 50.00',400,250,570,270),
            ('Cash',20,300,100,320),('RM 100.00',400,300,570,320)])
        raw,mapped=contract(doc)
        self.assertEqual([r['amount'] for r in mapped['line_items']], ['20.00','30.00'])
        self.assertTrue(all(r['quantity'] is None and r['unit_price'] is None for r in mapped['line_items']))
        self.assertNotIn('200000001', mapped['line_items'][0]['description'])

    def test_single_unlabelled_amount_does_not_invent_receipt_row(self):
        _,mapped=contract(layout('RECEIPT',[('Merchant: Synthetic',10,50,250,70),('RM 20.00',400,90,570,110)]))
        self.assertEqual(mapped['line_items'],[])

    def test_ordinary_receipt_tilt_and_explicit_quantity_prefix(self):
        doc=layout('RECEIPT',[('1 Synthetic soup',20,105,250,125),('35000',400,100,570,120),
            ('2 Synthetic tea',20,145,250,165),('40000',400,140,570,160),
            ('SUBTOTAL',20,190,200,210),('75000',400,190,570,210)])
        _,mapped=contract(doc)
        self.assertEqual([r['description'] for r in mapped['line_items']],['Synthetic soup','Synthetic tea'])
        self.assertEqual([r['quantity'] for r in mapped['line_items']],['1','2'])
        self.assertTrue(all(r['unit_price'] is None for r in mapped['line_items']))

    def test_charge_breakdown_multiplier_is_explicit_and_missing_cells_stay_null(self):
        doc=layout('BILL',[('Currency: EUR',10,40,200,60),('Nome do Artigo',30,100,220,120),('Valor',480,100,570,120),
            ('13% Synthetic service',30,140,280,160),('50,00',480,140,570,160),('4,00 × 12,50',130,165,300,185),
            ('13% Other service',30,205,280,225),('8,00',480,205,570,225),('TOTAL EUR',30,260,220,280),('58,00',480,260,570,280)])
        _,mapped=contract(doc)
        self.assertEqual(len(mapped['line_items']),2)
        first,second=mapped['line_items']
        self.assertEqual((first['description'],first['quantity'],first['unit_price'],first['amount']),('Synthetic service','4.00','12.50','50.00'))
        self.assertIsNone(second['quantity']);self.assertIsNone(second['unit_price'])
        self.assertEqual(first['evidence']['quantity'][0]['text'],'4,00 × 12,50')

    def test_two_amounts_on_same_row_remain_conflicting(self):
        doc=layout('INVOICE',[('Currency: MYR',10,40,200,60),('Description',10,100,180,120),('Amount',450,100,570,120),
            ('Service',10,140,180,160),('20.00',450,140,500,160),('21.00',510,140,570,160)])
        raw,mapped=contract(doc)
        self.assertEqual(len(mapped['line_items']),1)
        self.assertIsNone(mapped['line_items'][0]['amount'])
        self.assertEqual(mapped['line_items'][0]['field_states']['amount'],'ambiguous')
        self.assertEqual(len(mapped['line_items'][0]['candidates']['amount']),2)

    def test_unknown_document_cannot_gain_rows_from_financial_layout(self):
        raw,mapped=contract(layout('Meeting agenda',[('Description',10,100,180,120),('Amount',450,100,570,120),
            ('Service',10,140,180,160),('20.00',450,140,570,160)]))
        self.assertEqual(mapped['line_items'],[])

    def test_bill_total_due_and_paid_remain_separate(self):
        raw,mapped=contract(layout('UTILITY BILL', [('Issuer: Synthetic Utility',10,40,350,60),
            ('Currency: MYR',10,70,200,90),('Total: 80.00',10,100,240,120),
            ('Amount due: 120.00',10,140,300,160),('Amount paid: 10.00',10,180,300,200),
            ('Previous balance: 50.00',10,220,350,240)]))
        values={f['field_key']:f['current_value'] for f in mapped['fields']}
        self.assertEqual(
            (values['total'], values['amount_due'], values['payment_amount']),
            ('80.00', '120.00', '10.00')
        )
        self.assertNotIn('amount_paid', values)

        # The original M2 extraction still retains its source field.
        self.assertEqual(raw['fields']['amount_paid']['value'], '10.00')
        self.assertTrue(raw['fields']['amount_paid']['evidence'])
        self.assertNotIn('previous_balance',values)

    def test_explicit_card_network_normalizes_method_without_settlement(self):
        raw,mapped=contract(layout('RECEIPT',[('Payment method: VISA',10,100,350,120)]))
        method=next(f for f in mapped['fields'] if f['field_key']=='payment_method')
        self.assertEqual(method['current_value'],'card')
        self.assertIn('VISA',method['raw_value'])
        self.assertNotIn('payment_status',{f['field_key'] for f in mapped['fields']})

    def test_bill_ambiguous_date_keeps_raw_candidate(self):
        raw,_=contract(layout('BILL',[('Data: 08-10-2020 Hora: 15:53',10,100,400,120)]))
        date=raw['fields']['bill_date']
        self.assertIsNone(date['value']); self.assertEqual(date['state'],'ambiguous')
        self.assertEqual(date['candidates'][0]['value'],'08-10-2020')

    def test_explicit_bill_due_amount_is_not_a_missing_obligation_or_invented_total(self):
        from validation.documents.document_validation import evaluate_document
        raw,mapped=contract(layout('BILL',[('Issuer: Synthetic Utility',10,40,350,60),
            ('Bill reference: SYN-B',10,70,350,90),('Bill date: 2026-10-01',10,100,350,120),
            ('Currency: MYR',10,140,250,160),('Amount due: 0.00',10,180,350,200)]))
        values={f['field_key']:f['current_value'] for f in mapped['fields']}
        self.assertIsNone(values['total']);self.assertEqual(values['amount_due'],'0.00')
        result=evaluate_document({'kind':'bill','category':'accounts_payable'},{'raw_json':raw},mapped['fields'],mapped['line_items'])
        self.assertFalse(any(e['field']=='total' and e['code'].endswith('REQUIRED') for e in result['errors']))
        self.assertFalse(result['coverage_complete'])
        self.assertTrue(result['review_required']);self.assertFalse(result['accounting_approval_performed'])

    def test_missing_coordinate_evidence_does_not_create_geometry_rows(self):
        doc=layout('INVOICE',[('Description',10,100,180,120),('Amount',450,100,570,120),
            ('Service',10,140,180,160),('20.00',450,140,570,160)])
        doc['pages']={}
        _,mapped=contract(doc)
        self.assertEqual(mapped['line_items'],[])

    def test_page_boundaries_prevent_cross_page_amount_join(self):
        doc=layout('INVOICE',[('Description',10,100,180,120),('Amount',450,100,570,120),
            ('Service',10,140,180,160),('20.00',450,140,570,160)])
        doc['pages']['2']=doc['pages']['1']
        doc['m2_text_lines'][-1]['prov'][0]['page_no']=2
        _,mapped=contract(doc)
        self.assertEqual(mapped['line_items'],[])
    def test_bill_receipt_paid_amount_adapter(self):
        raw, mapped = contract(layout('UTILITY BILL', [
            ('Amount paid: 10.00', 10, 40, 300, 60)
        ]))

        original = deepcopy(raw['fields']['amount_paid'])
        bill_fields = {f['field_key']: f for f in mapped['fields']}

        self.assertEqual(
            bill_fields['payment_amount']['current_value'], '10.00'
        )
        self.assertEqual(
            bill_fields['payment_amount']['evidence'],
            original['evidence']
        )
        self.assertNotIn('amount_paid', bill_fields)

        # Synthetic adapter-only receipt case:
        # the same M2 field must retain its receipt identity.
        receipt_raw = deepcopy(raw)
        receipt_raw['document_type']['value'] = 'receipt'
        receipt_raw['fields'] = {'amount_paid': deepcopy(original)}

        receipt = map_m2_result(receipt_raw, 'SYN-PROFILE')
        receipt_fields = {
            f['field_key']: f for f in receipt['fields']
        }

        self.assertEqual(
            receipt_fields['amount_paid']['current_value'], '10.00'
        )
        self.assertEqual(
            receipt_fields['amount_paid']['evidence'],
            original['evidence']
        )
        self.assertNotIn('payment_amount', receipt_fields)

        # Never silently overwrite two aliases for one bill field.
        duplicate = deepcopy(raw)
        duplicate['fields']['payment_amount'] = deepcopy(original)

        with self.assertRaisesRegex(ValueError, 'Conflicting aliases'):
            map_m2_result(duplicate, 'SYN-PROFILE')



if __name__=='__main__': unittest.main()
