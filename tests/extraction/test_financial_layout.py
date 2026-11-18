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

    def test_receipt_short_year_is_read_only_when_the_day_cannot_be_a_month(self):
        doc=self.retail_bill();doc['m2_text_lines'][0]['text']='RECEIPT'
        doc['m2_text_lines'][6]['text']='20-11-17'
        raw,_=contract(doc)
        self.assertEqual(raw['fields']['transaction_date']['value'],'2017-11-20')
        doc=self.retail_bill();doc['m2_text_lines'][0]['text']='RECEIPT'
        doc['m2_text_lines'][6]['text']='04-11-17'
        raw,_=contract(doc)
        self.assertIsNone(raw['fields']['transaction_date']['value'])
        self.assertEqual(raw['fields']['transaction_date']['state'],'ambiguous')
        self.assertIn('04-11-17',{c['value'] for c in raw['fields']['transaction_date']['candidates']})

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

    def test_unknown_document_rows_remain_unselected_source_candidates(self):
        raw,mapped=contract(layout('Meeting agenda',[('Description',10,100,180,120),('Amount',450,100,570,120),
            ('Service',10,140,180,160),('20.00',450,140,570,160)]))
        self.assertEqual(raw['document_type']['value'], 'unknown')
        self.assertEqual(len(mapped['line_items']), 1)
        row = mapped['line_items'][0]
        self.assertIsNone(row['amount'])
        self.assertIsNone(row['description'])
        self.assertEqual(row['candidates']['amount'][0]['value'], '20.00')
        self.assertTrue(row['evidence']['amount'][0]['source_ref'])

    def receipt(self, *lines):
        return contract(layout('RECEIPT', [('Acme Trading Sdn Bhd', 10, 10, 300, 28), ('Date: 15/03/2018', 10, 40, 200, 58)] + list(lines) +
                               [('Change', 10, 900, 80, 918), ('0.00', 450, 900, 500, 918)]))

    def test_receipt_rows_need_amounts_that_add_up_to_a_printed_value(self):
        head = [('Qty Description', 10, 100, 200, 118), ('Amount', 450, 100, 520, 118)]
        rows = [('1 GREEN TEA @ 1.00', 60, 140, 300, 158), ('1.00 SR', 450, 140, 520, 158), ('2 GYOZA @ 7.50', 60, 170, 300, 188), ('15.00 SR', 450, 170, 520, 188)]
        raw, mapped = self.receipt(*head, *rows, ('Total', 300, 300, 360, 318), ('16.00', 450, 300, 520, 318))
        self.assertEqual(raw['document_type']['value'], 'receipt')
        self.assertEqual([(r['description'], r['quantity'], r['unit_price'], r['amount']) for r in mapped['line_items']],
                         [('GREEN TEA', '1', '1.00', '1.00'), ('GYOZA', '2', '7.50', '15.00')])
        self.assertTrue(mapped['line_items'][0]['evidence']['amount'][0]['source_ref'])
        _, unbalanced = self.receipt(*head, *rows, ('Total', 300, 300, 360, 318), ('99.00', 450, 300, 520, 318))
        self.assertEqual(unbalanced['line_items'], [])

    def test_receipt_unit_price_is_left_empty_when_it_does_not_explain_the_amount(self):
        head = [('Item', 10, 100, 60, 118), ('Qty', 200, 100, 240, 118), ('U/P', 280, 100, 320, 118), ('AMOUNLT', 450, 100, 520, 118)]
        row = [('9557950605126', 10, 140, 180, 158), ('5', 210, 140, 225, 158), ('2.74', 285, 140, 320, 158), ('13.00*', 450, 140, 520, 158), ('- CAMIS BOOK', 20, 165, 200, 183)]
        _, mapped = self.receipt(*head, *row, ('Sub-Total (Ex)', 10, 300, 160, 318), ('13.00', 450, 300, 520, 318))
        line = mapped['line_items'][0]
        self.assertEqual((line['description'], line['quantity'], line['amount']), ('CAMIS BOOK', '5', '13.00'))
        self.assertIsNone(line['unit_price'])

    def test_receipt_rows_ignore_a_gst_summary_heading_on_a_tilted_scan(self):
        lines = [('Date: 20/11/2017', 10, 40, 200, 58), ('RM34.90 s', 450, 100, 520, 118), ('069 MILO 2KG', 10, 108, 200, 126),
                 ('RM4.10 s', 450, 130, 520, 148), ('2709 OAT WHEAT', 10, 138, 200, 156), ('39.00', 450, 170, 520, 188),
                 ('Total Sales (Inclusive GST) RM', 10, 178, 300, 196), ('Tax(RM) Amount(RM)', 10, 260, 300, 278)]
        _, mapped = self.receipt(*lines)
        self.assertEqual([(r['description'], r['amount']) for r in mapped['line_items']], [('069 MILO 2KG', '34.90'), ('2709 OAT WHEAT', '4.10')])

    def test_receipt_heading_survives_summary_words_above_and_inside_it(self):
        lines = [('CASH', 10, 70, 80, 88), ('QTY ITEM', 10, 100, 120, 118), ('TOTAL', 450, 100, 520, 118),
                 ('2 M Fries', 10, 140, 200, 158), ('25.40', 450, 140, 520, 158), ('1 Small Cone', 10, 170, 200, 188), ('1.00', 450, 170, 520, 188),
                 ('Total Rounded', 10, 220, 160, 238), ('26.40', 450, 220, 520, 238)]
        _, mapped = self.receipt(*lines)
        self.assertEqual([(r['description'], r['quantity'], r['amount']) for r in mapped['line_items']],
                         [('M Fries', '2', '25.40'), ('Small Cone', '1', '1.00')])

    def test_receipt_with_no_amount_rows_keeps_no_rows(self):
        _, mapped = self.receipt(('Thank you', 10, 100, 200, 118), ('Total', 300, 300, 360, 318), ('16.00', 450, 300, 520, 318))
        self.assertEqual(mapped['line_items'], [])

    def test_gst_summary_total_is_not_the_receipt_total(self):
        raw, _ = self.receipt(('Total', 10, 200, 80, 218), ('27.35', 450, 200, 520, 218), ('GST Summary', 10, 300, 160, 318),
                              ('Total :', 10, 340, 80, 358), ('25.80', 300, 340, 360, 358), ('1.55', 450, 340, 520, 358))
        self.assertEqual(raw['fields']['total_amount']['value'], '27.35')

    def test_printed_none_is_not_a_customer(self):
        for printed in ('None', 'n/a', 'NIL', '-'):
            with self.subTest(printed=printed):
                raw, _ = contract(layout('UTILITY BILL', [('Issuer: Synthetic Utility', 10, 40, 350, 60), ('CUSTOMER', 10, 80, 120, 98), (printed, 10, 102, 120, 120),
                                                          ('Bill Date: 2023-07-15', 10, 140, 300, 158), ('Total: 10.00', 10, 170, 200, 188)]))
                self.assertIsNone((raw['fields'].get('customer_name') or {}).get('value'))

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


def net_worth_invoice(lines=(('Item A','100,00'),('Item B','50,00')), net='150,00', tax='15,00', gross='165,00',
                      unit='$', extra=(), net_label='Net worth'):
    """Items and summary in the real Net worth/VAT/Gross worth layout; synthetic values."""
    cells=[('Description',20,60,160,80),('Net worth',300,60,370,80),('Gross worth',470,60,560,80)]
    for index,(name,amount) in enumerate(lines):
        top=90+index*30
        cells+=[(name,20,top,160,top+20),(amount,300,top,370,top+20)]
    cells+=[('SUMMARY',20,240,160,260),('VAT [%]',140,280,200,300),(net_label,250,280,320,300),
            ('VAT',360,280,400,300),('Gross worth',450,280,550,300),('10%',160,320,200,340),
            (net,250,320,320,340),(tax,360,320,400,340),(gross,450,320,550,340),
            ('Total',20,360,120,380),(unit+' '+net,240,360,320,380),(unit+' '+tax,350,360,400,380),
            (unit+' '+gross,440,360,550,380),*extra]
    return layout('INVOICE',cells)


def field(doc,key):
    from services.extraction.parsing.map_invoice import map_document
    return map_document(doc)['fields'][key]


class CurrencyEvidenceTests(unittest.TestCase):
    def amounts(self,*cells):
        return layout('INVOICE',[(text,20,60+i*30,200,80+i*30) for i,text in enumerate(cells)])

    def test_explicit_codes_resolve_with_their_source_text(self):
        for text,code in (('RM 10.00','MYR'),('MYR 10.00','MYR'),('USD 10.00','USD'),('10.00 USD','USD'),
                          ('SGD 10.00','SGD'),('S$ 10.00','SGD'),('US$ 10.00','USD'),('GBP 10.00','GBP'),
                          ('£ 10.00','GBP'),('€ 10,00','EUR'),('10,00 €','EUR'),('Amount (USD)','USD'),('JPY','JPY')):
            with self.subTest(text=text):
                cell=field(self.amounts(text),'currency')
                self.assertEqual((cell['mapping_status'],cell['value']),('mapped',code))
                self.assertIn(text,cell['candidates'][0]['raw_text'])

    def test_bare_dollar_stays_unresolved_and_keeps_source_text(self):
        for text in ('$ 725,37','725,37 $','$'):
            with self.subTest(text=text):
                cell=field(self.amounts(text),'currency')
                self.assertEqual((cell['mapping_status'],cell['value']),('ambiguous',None))
                self.assertEqual(cell['candidates'][0]['value'],None)
                self.assertEqual(cell['candidates'][0]['evidence'][0]['text'],text)

    def test_symbol_between_digits_is_an_ocr_misread_not_a_currency(self):
        doc=self.amounts('36€30','$ 12 973,88')
        cell=field(doc,'currency')
        self.assertEqual((cell['mapping_status'],cell['value']),('ambiguous',None))
        self.assertNotIn('EUR',{c['value'] for c in cell['candidates']})

    def test_conflicting_explicit_currencies_keep_all_candidates(self):
        cell=field(self.amounts('USD 10.00','SGD 12.00'),'currency')
        self.assertEqual((cell['mapping_status'],cell['value']),('ambiguous',None))
        self.assertEqual({c['value'] for c in cell['candidates']},{'USD','SGD'})

    def test_explicit_code_is_not_displaced_by_a_dollar_sign(self):
        self.assertEqual(field(self.amounts('USD 10.00','$ 10.00'),'currency')['value'],'USD')

    def test_no_currency_is_assumed_from_address_or_country(self):
        cell=field(self.amounts('Kuala Lumpur, Malaysia','FARM ROAD','10.00'),'currency')
        self.assertEqual((cell['mapping_status'],cell['value'],cell['candidates']),('missing',None,[]))

    def test_ordinary_words_are_not_iso_codes(self):
        cell=field(self.amounts('TRY AGAIN','PHP Developer services','SAR Trading'),'currency')
        self.assertEqual((cell['value'],cell['candidates']),(None,[]))

    def test_ambiguous_currency_reaches_review_with_guidance_and_no_value(self):
        raw,mapped=contract(self.amounts('$ 10,00'))
        row=next(f for f in mapped['fields'] if f['field_key']=='currency')
        self.assertEqual((row['extraction_state'],row['current_value']),('ambiguous',None))
        from services.extraction.contracts.reliability import field_guidance
        self.assertEqual([w['code'] for w in field_guidance(row)['warnings']][-1],'AMBIGUOUS_CURRENCY_SYMBOL')


class MonetaryLabelTests(unittest.TestCase):
    def labelled(self,*rows):
        cells=[]
        for i,(label,value) in enumerate(rows):
            cells+=[(label,20,60+i*30,180,80+i*30),(value,200,60+i*30,300,80+i*30)]
        return layout('INVOICE',cells)

    def test_decimal_comma_amounts_need_no_currency(self):
        doc=self.labelled(('Total Amount:','1.234,56'),('Tax Amount:','725,37'))
        self.assertEqual(field(doc,'total_amount')['value'],'1234.56')
        self.assertEqual(field(doc,'tax_amount')['value'],'725.37')

    def test_three_digit_comma_group_is_not_a_decimal(self):
        self.assertEqual(field(self.labelled(('Total Amount:','1,234')),'total_amount')['value'],'1234')

    def test_aliases_keep_distinct_accounting_meanings(self):
        for label,key in (('Net Amount','total_excluding_tax'),('Total Before Tax','total_excluding_tax'),
                          ('VAT','tax_amount'),('GST','tax_amount'),('Amount Payable','amount_due'),
                          ('Total Due','amount_due'),('Invoice Amount','total_amount'),('Final Total','total_amount')):
            with self.subTest(label=label):
                result=field(self.labelled((label,'10.00')),key)
                self.assertEqual((result['mapping_status'],result['value']),('mapped','10.00'))

    def test_amount_due_is_not_copied_into_total(self):
        self.assertEqual(field(self.labelled(('Amount Payable','10.00')),'total_amount')['value'],None)

    def test_tax_registration_numbers_are_not_tax_amounts(self):
        for value in ('002132546123','12345678'):
            with self.subTest(value=value):
                self.assertEqual(field(self.labelled(('GST',value)),'tax_amount')['value'],None)
        self.assertEqual(field(layout('INVOICE',[('GST:002132546123',20,60,200,80)]),'tax_amount')['value'],None)

    def test_tax_rate_is_not_a_tax_amount(self):
        self.assertEqual(field(self.labelled(('VAT','10%')),'tax_amount')['value'],None)

    def test_conflicting_totals_stay_unresolved_with_candidates(self):
        cell=field(self.labelled(('Total Amount:','10.00'),('Total Amount:','12.00')),'total_amount')
        self.assertEqual((cell['mapping_status'],cell['value']),('ambiguous',None))
        self.assertEqual({c['value'] for c in cell['candidates']},{'10.00','12.00'})

    def test_disagreeing_currency_qualifier_is_flagged(self):
        doc=self.labelled(('Currency:','USD'),('Total Amount:','SGD 10.00'))
        cell=field(doc,'total_amount')
        self.assertEqual((cell['mapping_status'],cell['value']),('ambiguous',None))

    def test_payment_and_change_are_not_totals(self):
        doc=self.labelled(('Cash','50.00'),('Change','40.00'))
        self.assertEqual(field(doc,'total_amount')['value'],None)

    def test_missing_subtotal_is_not_calculated_from_total_and_tax(self):
        doc=self.labelled(('Total Amount:','110.00'),('Tax Amount:','10.00'))
        cell=field(doc,'subtotal')
        self.assertEqual((cell['value'],cell['candidates']),(None,[]))


class NetWorthCandidateTests(unittest.TestCase):
    def test_valid_aggregate_is_offered_but_never_applied(self):
        cell=field(net_worth_invoice(),'subtotal')
        self.assertEqual((cell['mapping_status'],cell['value'],cell['raw_text'],cell['evidence']),('missing',None,None,[]))
        self.assertEqual(len(cell['candidates']),1)
        candidate=cell['candidates'][0]
        self.assertEqual(candidate['value'],'150.00')
        source=candidate['evidence']
        self.assertTrue(all(e['candidate_basis']=='printed_tax_exclusive_total' for e in source))
        self.assertTrue(all(e['printed_label']=='Net worth' and e['printed_value']=='$ 150,00' for e in source))
        self.assertTrue(all(e['page']==1 and e['bbox'] for e in source))
        self.assertIn('$ 150,00',[e['text'] for e in source])
        self.assertTrue(all(e['extraction_method']=='OCR' and e['ocr_score']==.8 for e in source))

    def test_printed_label_capitalisation_is_preserved(self):
        cell=field(net_worth_invoice(net_label='Net Worth'),'subtotal')
        self.assertEqual(cell['candidates'][0]['evidence'][0]['printed_label'],'Net Worth')

    def test_inv00014_style_invoice_with_european_commas(self):
        doc=net_worth_invoice(lines=(('Wild West Wine','54,00'),('Press Wine','558,00'),('Wine Rack','56,25'),('Tool','23,12'),('VTG Rack','34,00')),
                              net='725,37',tax='72,54',gross='797,91')
        self.assertEqual(field(doc,'subtotal')['value'],None)
        self.assertEqual(field(doc,'subtotal')['candidates'][0]['value'],'725.37')
        self.assertEqual(field(doc,'total_excluding_tax')['value'],'725.37')
        self.assertEqual(field(doc,'tax_amount')['value'],'72.54')
        self.assertEqual(field(doc,'total_amount')['value'],'797.91')
        self.assertEqual(field(doc,'currency')['value'],None)

    def test_total_excluding_tax_and_its_evidence_are_unchanged(self):
        doc=net_worth_invoice()
        cell=field(doc,'total_excluding_tax')
        self.assertEqual((cell['mapping_status'],cell['value']),('mapped','150.00'))
        self.assertFalse(any('candidate_basis' in e for e in cell['evidence']))

    def test_european_grouped_decimal_commas_are_compared_exactly(self):
        doc=net_worth_invoice(lines=(('Item A','10 560,00'),('Item B','1 234,44')),net='11 794,44',tax='1 179,44',gross='12 973,88')
        self.assertEqual(field(doc,'subtotal')['candidates'][0]['value'],'11794.44')

    def assert_no_candidate(self,doc):
        cell=field(doc,'subtotal')
        self.assertEqual((cell['value'],cell['candidates']),(None,[]))

    def test_lines_that_do_not_sum_block_the_offer(self):
        self.assert_no_candidate(net_worth_invoice(lines=(('Item A','100,00'),('Item B','49,99'))))

    def test_totals_that_do_not_reconcile_block_the_offer(self):
        self.assert_no_candidate(net_worth_invoice(gross='165,01'))

    def test_missing_line_evidence_blocks_the_offer(self):
        self.assert_no_candidate(net_worth_invoice(lines=()))
        doc=net_worth_invoice(lines=(('Item A','100,00'),('Item B','5O,OO')))
        self.assert_no_candidate(doc)

    def test_adjustments_block_the_offer(self):
        for label in ('Discount','Shipping','Rounding','Allowances','Deposit'):
            with self.subTest(label=label):
                self.assert_no_candidate(net_worth_invoice(extra=[(label,20,400,120,420),('1,00',440,400,550,420)]))

    def test_discount_column_anywhere_blocks_the_offer(self):
        self.assert_no_candidate(net_worth_invoice(extra=[('Disc.',200,60,240,80)]))

    def test_several_tax_rate_rows_block_the_offer(self):
        self.assert_no_candidate(net_worth_invoice(extra=[('20%',160,342,200,358)]))

    def test_printed_subtotal_is_used_instead_of_any_offer(self):
        doc=net_worth_invoice(extra=[('Subtotal:',20,400,120,420),('150,00',140,400,230,420)])
        cell=field(doc,'subtotal')
        self.assertEqual((cell['mapping_status'],cell['value']),('mapped','150.00'))
        self.assertFalse(any('candidate_basis' in e for c in cell['candidates'] for e in c['evidence']))

    def test_conflicting_net_worth_values_block_the_offer(self):
        doc=net_worth_invoice()
        doc['m2_text_lines'].append({'text':'$ 151,00','self_ref':'#/m2_text_lines/99','label':'text','from_ocr':True,'ocr_score':.8,
            'prov':[{'page_no':1,'bbox':dict(l=240,t=361,r=320,b=379,coord_origin='TOPLEFT')}]})
        self.assert_no_candidate(doc)

    def test_bills_never_receive_the_offer(self):
        doc=net_worth_invoice()
        doc['m2_text_lines'][0]['text']='UTILITY BILL'
        self.assertEqual(field(doc,'subtotal')['candidates'],[])

    def test_candidate_never_becomes_the_effective_value(self):
        from services.extraction.contracts.accounting_contract import record_values
        raw,mapped=contract(net_worth_invoice())
        self.assertIsNone(record_values([next(f for f in mapped['fields'] if f['field_key']=='subtotal')])['subtotal'])

    def test_guidance_explains_the_printed_tax_exclusive_total(self):
        from services.extraction.contracts.reliability import field_guidance
        raw,mapped=contract(net_worth_invoice())
        row={**next(f for f in mapped['fields'] if f['field_key']=='subtotal'),'corrected_by':None,'corrected_at':None}
        warning=next(w for w in field_guidance(row)['warnings'] if w['code']=='SUBTOTAL_TAX_EXCLUSIVE_CANDIDATE')
        self.assertIn('tax-exclusive total (Net worth)',warning['message'])
        self.assertIn('does not label a subtotal',warning['message'])
        self.assertEqual(row['confidence'],None)

    def test_non_numeric_candidate_skips_the_offer_without_failing_extraction(self):
        from services.extraction.parsing.map_invoice import mapped_field,pretax_structure
        evidence=[{'bbox':{'l':1,'t':1,'r':2,'b':2},'page':1,'text':'Net worth','source_ref':'#/a'}]
        fields={'subtotal':mapped_field(),'total_excluding_tax':mapped_field('n','1.00',evidence,'mapped',[{'raw_text':'n','value':'EUR 1.00','evidence':evidence}])}
        result=pretax_structure({'pages':{},'texts':[]},fields,[{'line_total':mapped_field('1','1.00',[{'amount_basis':'net_excluding_tax','bbox':{}}],'mapped')}])
        self.assertFalse(result['eligible'])
        self.assertIn('UNSUPPORTED_TAX_EXCLUSIVE_VALUE',result['blockers'])


class PretaxBasisValidationTests(unittest.TestCase):
    """validation-v2.1: a printed tax-exclusive total can be the arithmetic basis."""
    IDENTITY=[('Supplier: Acme Trading',20,420,300,440),('Invoice number: X-42',20,450,300,470),('Date: 2020-01-31',20,480,300,500)]

    def evaluate(self,doc,change=None):
        from validation.documents.document_validation import evaluate_document
        for i,(text,l,t,r,b) in enumerate(self.IDENTITY):
            doc['m2_text_lines'].append({'text':text,'self_ref':f'#/m2_text_lines/id{i}','label':'text','from_ocr':True,'ocr_score':.8,
                'prov':[{'page_no':1,'bbox':dict(l=l,t=t,r=r,b=b,coord_origin='TOPLEFT')}]})
        raw,mapped=contract(doc)
        fields=[{**row,'corrected_by':None,'corrected_at':None} for row in mapped['fields']]
        # Printed quantity/price cells are not what these tests vary; field errors
        # would otherwise stop the model's arithmetic validators from running at all.
        items=[{**row,'id':str(i),'quantity':'1.000','unit_price':row['amount']} for i,row in enumerate(mapped['line_items'])]
        if change: change(raw,fields,items)
        result=evaluate_document({'id':'SYN-PROFILE','kind':'invoice','category':'accounts_payable'},{'raw_json':raw},fields,items,None)
        return result,fields

    def codes(self,result):
        return {(e['code'],e['field']) for e in result['errors']}

    def basis_check(self,result):
        return next((c for c in result['checks'] if c['rule']=='PRETAX-BASIS'),None)

    def test_supported_structure_satisfies_the_pretax_requirement(self):
        result,fields=self.evaluate(net_worth_invoice(unit='USD'))
        self.assertNotIn(('EX-001','subtotal'),self.codes(result))
        self.assertEqual(self.basis_check(result)['result'],'PASS')
        row=next(f for f in fields if f['field_key']=='subtotal')
        self.assertEqual((row['current_value'],row['normalized_value'],row['extraction_state']),(None,None,'missing'))
        self.assertNotIn('EX-002',{c for c,_ in self.codes(result)})

    def test_lines_that_disagree_still_fail_as_arithmetic(self):
        result,_=self.evaluate(net_worth_invoice(unit='USD',lines=(('Item A','100,00'),('Item B','40,00'))))
        found={c:f for c,f in self.codes(result)}
        self.assertEqual(found.get('EX-004'),'total_excluding_tax')
        message=next(e['message'] for e in result['errors'] if e['code']=='EX-004')
        self.assertIn('printed tax-exclusive total',message)

    def test_total_that_disagrees_still_fails_as_arithmetic(self):
        result,_=self.evaluate(net_worth_invoice(unit='USD',gross='170,00'))
        self.assertIn('EX-005',{c for c,_ in self.codes(result)})

    def assert_requirement_stays(self,result):
        self.assertIn(('EX-001','subtotal'),self.codes(result))
        self.assertEqual(self.basis_check(result)['checkStatus'],'NOT_CHECKED')

    def test_printed_adjustments_keep_the_requirement(self):
        for label in ('Discount','Shipping','Rounding','Allowances','Deposit'):
            with self.subTest(label=label):
                self.assert_requirement_stays(self.evaluate(net_worth_invoice(unit='USD',extra=[(label,20,400,120,420),('1,00',440,400,550,420)]))[0])

    def assert_subtotal_derived_from_lines(self,result):
        # D5: complete line amounts that reach the total exactly support the invoice. The stored subtotal stays empty.
        self.assertNotIn(('EX-001','subtotal'),self.codes(result))
        self.assertEqual(self.basis_check(result)['checkStatus'],'NOT_CHECKED')
        self.assertTrue(any(c['rule']=='SUBTOTAL-DERIVED' for c in result['checks']))

    def test_several_tax_rows_no_longer_block_when_the_lines_reach_the_total(self):
        self.assert_subtotal_derived_from_lines(self.evaluate(net_worth_invoice(unit='USD',extra=[('20%',160,342,200,358)]))[0])

    def test_missing_line_items_keep_the_requirement(self):
        self.assert_requirement_stays(self.evaluate(net_worth_invoice(unit='USD',lines=()))[0])

    def test_conflicting_net_worth_keeps_the_requirement(self):
        doc=net_worth_invoice(unit='USD')
        doc['m2_text_lines'].append({'text':'USD 151,00','self_ref':'#/m2_text_lines/99','label':'text','from_ocr':True,'ocr_score':.8,
            'prov':[{'page_no':1,'bbox':dict(l=240,t=361,r=320,b=379,coord_origin='TOPLEFT')}]})
        self.assertIn(('EX-001','subtotal'),self.codes(self.evaluate(doc)[0]))

    def test_comma_amount_on_an_adjustment_label_does_not_fail_the_contract(self):
        raw,mapped=contract(net_worth_invoice(extra=[('Allowances:',20,400,120,420),('1,00',140,400,230,420)]))
        self.assertEqual(next(f for f in mapped['fields'] if f['field_key']=='allowances')['current_value'],'1.00')

    def test_later_correction_adding_a_discount_revokes_the_basis(self):
        def change(raw,fields,items):
            fields.append({'field_key':'discount','field_label':'Discount','extraction_state':'missing','resolution':'unresolved_missing',
                           'current_value':'5.00','normalized_value':None,'corrected_by':'actor','corrected_at':'now','evidence':[],'candidates':[]})
        self.assert_requirement_stays(self.evaluate(net_worth_invoice(unit='USD'),change)[0])

    def test_unresolved_line_amount_keeps_the_requirement(self):
        def change(raw,fields,items):
            items[0]['amount']=None
        self.assert_requirement_stays(self.evaluate(net_worth_invoice(unit='USD'),change)[0])

    def test_tax_exclusive_total_without_source_evidence_falls_back_to_the_lines(self):
        def change(raw,fields,items):
            next(f for f in fields if f['field_key']=='total_excluding_tax')['evidence']=[]
        self.assert_subtotal_derived_from_lines(self.evaluate(net_worth_invoice(unit='USD'),change)[0])

    def test_tampered_or_older_extraction_without_structure_falls_back_to_the_lines(self):
        def change(raw,fields,items):
            raw.pop('pretax_structure')
        self.assert_subtotal_derived_from_lines(self.evaluate(net_worth_invoice(unit='USD'),change)[0])

    def test_printed_subtotal_is_still_the_basis_when_present(self):
        doc=net_worth_invoice(unit='USD',extra=[('Subtotal:',20,400,120,420),('150,00',140,400,230,420)])
        result,_=self.evaluate(doc)
        self.assertIsNone(self.basis_check(result))
        self.assertNotIn(('EX-001','subtotal'),self.codes(result))

    def test_human_confirmed_subtotal_is_used_directly(self):
        def change(raw,fields,items):
            row=next(f for f in fields if f['field_key']=='subtotal')
            row.update(current_value='150.00',corrected_by='actor',corrected_at='now')
        result,fields=self.evaluate(net_worth_invoice(unit='USD'),change)
        self.assertIsNone(self.basis_check(result))
        self.assertNotIn(('EX-001','subtotal'),self.codes(result))

    def test_ambiguous_currency_still_blocks_after_the_basis(self):
        result,_=self.evaluate(net_worth_invoice())
        self.assertEqual(self.basis_check(result)['result'],'PASS')
        currency=[e for e in result['errors'] if e['field']=='currency']
        self.assertEqual(len(currency),1)
        self.assertEqual((currency[0]['code'],currency[0]['severity'],currency[0]['relatedRules']),('EX-002','high',['PROFILE-REQUIRED']))

    def test_arithmetic_still_runs_when_only_currency_is_unresolved(self):
        result,_=self.evaluate(net_worth_invoice(lines=(('Item A','100,00'),('Item B','40,00'))))
        self.assertIn('EX-004',{c for c,_ in self.codes(result)})
        self.assertEqual(next(e for e in result['errors'] if e['field']=='currency')['cause'],'UNCERTAIN_SOURCE_VALUE')

    def test_arithmetic_runs_and_passes_with_unresolved_currency(self):
        result,_=self.evaluate(net_worth_invoice())
        passed={c['rule'] for c in result['checks'] if c['checkStatus']=='CHECKED' and c['result']=='PASS'}
        self.assertTrue({'EX-004','EX-005'}<=passed)

    def test_conflicting_currencies_stop_cross_amount_comparison(self):
        doc=net_worth_invoice(unit='USD',extra=[('SGD 1.00',20,400,120,420)])
        result,_=self.evaluate(doc)
        self.assertEqual(next(e for e in result['errors'] if e['field']=='currency')['cause'],'CONFLICTING_VALUES')
        check=next(c for c in result['checks'] if c['rule']=='ARITHMETIC-PREREQ')
        self.assertEqual(check['checkStatus'],'NOT_CHECKED')

    def test_causes_keep_unlike_findings_apart(self):
        from validation.documents.document_validation import exception_cause as c
        self.assertEqual(c('EX-001','currency','Field required'),'MISSING_SOURCE_VALUE')
        self.assertEqual(c('EX-002','date','Dates require YYYY-MM-DD.'),'INVALID_FORMAT')
        self.assertEqual(c('EX-004','subtotal','x'),'ARITHMETIC_FAILURE')
        self.assertEqual(c('BR-CO-13','total_excluding_tax','x'),'ARITHMETIC_FAILURE')
        self.assertEqual(c('EX-008','supplier','x'),'UNKNOWN_VENDOR')
        self.assertEqual(c('EX-007','invoice','x'),'POSSIBLE_DUPLICATE')
        self.assertEqual(c('EX-002','document_type','x'),'CLASSIFICATION_MISMATCH')
        self.assertEqual(c('BILL-REQUIRED','date','x'),'MISSING_SOURCE_VALUE')

    def blocking_checks(self,result):
        return {c['rule'] for c in result['checks'] if c['checkStatus']=='NOT_CHECKED' and c.get('required',True)}

    def test_checks_with_nothing_on_the_document_do_not_block_approval(self):
        result,_=self.evaluate(net_worth_invoice(unit='USD'))
        self.assertEqual(self.blocking_checks(result)&{'BR-CO-10','BR-CO-14','BR-CO-16','EX-009'},set())
        self.assertEqual({c['rule'] for c in result['checks'] if c['rule']=='EX-009'},{'EX-009'})

    def test_vendor_control_still_blocks_without_a_catalogue(self):
        self.assertIn('EX-008',self.blocking_checks(self.evaluate(net_worth_invoice(unit='USD'))[0]))

    def test_printed_po_that_cannot_be_found_still_fails(self):
        def change(raw,fields,items):
            fields.append({'field_key':'purchase_order_number','field_label':'PO','extraction_state':'resolved','resolution':'parser_preserved',
                           'current_value':'PO-1','normalized_value':'PO-1','corrected_by':None,'corrected_at':None,'evidence':[],'candidates':[]})
        result,_=self.evaluate(net_worth_invoice(unit='USD'),change)
        # With no database in this test the order data is unavailable, which is not the same as no such order.
        check=next(c for c in result['checks'] if c['rule']=='EX-009')
        self.assertEqual((check['checkStatus'],check['required']),('NOT_CHECKED',True))

    def test_consolidation_keeps_unrelated_errors_and_highest_severity(self):
        from validation.documents.document_validation import consolidate_errors
        errors=[{'code':'EX-001','field':'vendor_name','severity':'medium','message':'m'},
                {'code':'PROFILE-REQUIRED','field':'supplier','severity':'high','message':'p'},
                {'code':'PROFILE-REQUIRED','field':'date','severity':'high','message':'alone'},
                {'code':'EX-004','field':'subtotal','severity':'high','message':'x'}]
        result=consolidate_errors(errors)
        self.assertEqual([(e['code'],e['field'],e['severity']) for e in result],
                         [('EX-001','vendor_name','high'),('PROFILE-REQUIRED','date','high'),('EX-004','subtotal','high')])
        self.assertEqual(result[0]['relatedRules'],['PROFILE-REQUIRED'])


class OcrLabelAndRoleTests(unittest.TestCase):
    def identity(self,date_label='Dateof issue:'):
        return {'schema_name':'DoclingDocument','pages':{'1':{'size':{'width':600,'height':900}}},'texts':[],'m2_text_lines':[
            {'text':text,'self_ref':f'#/m2_text_lines/{i}','label':'text','from_ocr':True,'ocr_score':.9,
             'prov':[{'page_no':1,'bbox':dict(l=l,t=t,r=r,b=b,coord_origin='TOPLEFT')}]}
            for i,(text,l,t,r,b) in enumerate([('Invoice no: 12345',20,20,200,40),(date_label,20,50,140,70),('04/13/2013',250,50,340,70),
                ('Seller:',20,90,90,110),('Client:',300,90,370,110),('Acme Trading',20,120,150,140),('Beta Stores',300,120,420,140),
                ('Description',20,200,120,220),('Qty',200,200,240,220),('Net price',260,200,330,220),('Net worth',350,200,420,220),
                ('Widget',20,230,120,250),('1,00',200,230,240,250),('10,00',260,230,330,250),('10,00',350,230,420,250),
                ('Total',20,300,80,320),('$ 10,00',350,300,420,320)])]}

    def test_dropped_word_gap_keeps_classification_and_fields(self):
        from services.extraction.classification.classify_document import classify_document
        doc=self.identity()
        self.assertEqual(classify_document(doc)['value'],'invoice')
        self.assertEqual(field(doc,'invoice_date')['value'],'2013-04-13')
        self.assertEqual(field(doc,'invoice_number')['value'],'12345')

    def test_supplier_and_customer_roles_stay_separate(self):
        doc=self.identity()
        self.assertEqual(field(doc,'supplier_name')['value'],'Acme Trading')
        self.assertEqual(field(doc,'buyer_name')['value'],'Beta Stores')

    def test_identifier_alone_does_not_classify(self):
        from services.extraction.classification.classify_document import classify_document
        doc=self.identity()
        doc['m2_text_lines']=doc['m2_text_lines'][:1]
        self.assertEqual(classify_document(doc)['value'],'unknown')

    def test_letterhead_above_title_is_the_issuer(self):
        doc=layout('Alpha Supplies Sdn Bhd',[('TAX INVOICE',200,50,400,70),('Beta Retail Sdn Bhd',20,90,240,110)])
        self.assertEqual(field(doc,'supplier_name')['value'],'Alpha Supplies Sdn Bhd')

    def test_two_entities_above_title_stay_in_conflict(self):
        doc=layout('Alpha Supplies Sdn Bhd',[('Beta Retail Sdn Bhd',20,40,240,58),('TAX INVOICE',200,70,400,90)])
        cell=field(doc,'supplier_name')
        self.assertEqual((cell['mapping_status'],cell['value']),('ambiguous',None))

    def test_item_number_header_is_not_an_invoice_number(self):
        doc=layout('INVOICE',[('No.',20,60,60,80),('1.',20,90,60,110),('No. : 12',20,120,100,140)])
        self.assertIsNone(field(doc,'invoice_number')['value'])

    def test_printed_no_label_supplies_a_missing_invoice_number(self):
        doc=layout('TAX INVOICE',[('No. : I-000010',300,10,500,30)])
        self.assertEqual(field(doc,'invoice_number')['value'],'I-000010')

    def test_missing_invoice_number_stays_missing(self):
        cell=field(layout('INVOICE',[('Total Amount:',20,60,180,80),('10.00',200,60,300,80)]),'invoice_number')
        self.assertEqual((cell['mapping_status'],cell['value']),('missing',None))

    def test_ambiguous_day_month_order_stays_unresolved(self):
        cell=field(layout('INVOICE',[('Date of issue:',20,60,140,80),('04/01/2017',160,60,260,80)]),'invoice_date')
        self.assertEqual((cell['mapping_status'],cell['value']),('ambiguous',None))
        self.assertEqual(cell['candidates'][0]['value'],'04/01/2017')

    def test_unreadable_empty_ocr_produces_no_values(self):
        from services.extraction.classification.classify_document import classify_document
        doc={'schema_name':'DoclingDocument','pages':{'1':{'size':{'width':600,'height':900}}},'texts':[],'m2_text_lines':[]}
        self.assertEqual(classify_document(doc)['value'],'unknown')
        self.assertEqual(field(layout('INVOICE',[]),'total_amount')['value'],None)

    def test_high_ocr_score_does_not_change_field_assignment_or_confidence(self):
        doc=layout('INVOICE',[('Total Amount:',20,60,180,80),('Page 1',200,60,300,80)])
        for line in doc['m2_text_lines']:
            line['ocr_score']=.999
        cell=field(doc,'total_amount')
        self.assertEqual(cell['value'],None)
        raw,mapped=contract(doc)
        self.assertTrue(all(row['confidence'] is None for row in mapped['fields']))


class UtilityBillLayoutTests(unittest.TestCase):
    def bill(self,heading=(('TAX',500,20,540,40),('INVOICE',535,20,600,40)),account='CUST0012345'):
        return layout('EnerGov Utilities Ltd',[*heading,('INVOICE NO.',20,60,120,75),('DATE',200,60,250,75),('DUE DATE',320,60,400,75),
            ('ACCOUNT NO.',440,60,540,75),('INV-1',20,80,120,95),('15/06/2023',200,80,290,95),('15/07/2023',320,80,410,95),(account,440,80,540,95),
            ('Amount Due',400,120,500,140),('£153.15',400,145,500,170),('DESCRIPTION',20,200,140,220),('AMOUNT (GBP)',430,200,540,220),
            ('Electricity Supply Charge',20,230,240,250),('153.15',430,230,540,250),('Net Total',300,300,400,320),('153.15',430,300,540,320),
            ('GRAND TOTAL',430,350,540,370),('£153.15',430,375,540,395)])

    def test_split_tax_invoice_heading_is_compatible_with_utility_evidence(self):
        from services.extraction.classification.classify_document import classify_document
        result=classify_document(self.bill())
        self.assertEqual((result['value'],result['status']),('bill','classified'))

    def test_distant_tax_word_does_not_complete_the_heading(self):
        from services.extraction.classification.classify_document import classify_document
        result=classify_document(self.bill(heading=(('TAX',20,500,60,520),('INVOICE',535,20,600,40))))
        self.assertEqual((result['value'],result['status']),('unknown','ambiguous'))

    def test_net_total_is_the_bill_subtotal_with_its_printed_evidence(self):
        cell=field(self.bill(),'subtotal')
        self.assertEqual((cell['mapping_status'],cell['value']),('mapped','153.15'))
        self.assertTrue(cell['raw_text'].startswith('Net Total'))
        self.assertTrue(all(e['bbox'] for e in cell['evidence']))

    def test_bill_amount_meanings_stay_distinct(self):
        doc=self.bill()
        self.assertEqual(field(doc,'amount_due')['value'],'153.15')
        self.assertEqual(field(doc,'document_charges')['value'],None)

    def test_column_account_number_is_read_but_placeholder_is_not(self):
        self.assertEqual(field(self.bill(),'account_number')['value'],'CUST0012345')
        self.assertEqual(field(self.bill(account='N/A'),'account_number')['value'],None)

    def test_net_total_on_an_invoice_stays_tax_exclusive(self):
        doc=layout('INVOICE',[('Net Total',20,60,120,80),('10.00',200,60,300,80)])
        self.assertEqual(field(doc,'total_excluding_tax')['value'],'10.00')
        self.assertEqual(field(doc,'subtotal')['value'],None)


class ReceiptHeaderTests(unittest.TestCase):
    def receipt(self,*lines):
        cells=[(text,20,50+i*25,300,70+i*25) for i,text in enumerate(lines)]
        tail=len(lines)*25+100
        return layout('Example',cells+[('Cashier: Ann',20,tail,200,tail+20),('Date: 31/01/2020',20,tail+25,200,tail+45),('Total:',20,tail+50,120,tail+70),('10.00',200,tail+50,300,tail+70)])

    def test_registration_line_does_not_end_the_address(self):
        doc=self.receipt('OJC Marketing Sdn Bhd','ROC NO: 538358-H','No 2 & 4, Jalan Bayu 4,','Bandar Seri Alam,','Tel: 07-388 2218')
        doc['m2_text_lines'][0]['text']='RECEIPT'
        from services.extraction.classification.classify_document import classify_document
        doc['m2_text_lines'][0]['prov'][0]['bbox'].update(t=10,b=30)
        self.assertEqual(field(doc,'merchant')['value'],'OJC Marketing Sdn Bhd')
        self.assertEqual(field(doc,'merchant_address')['value'],'No 2 & 4, Jalan Bayu 4,\nBandar Seri Alam,')

    def test_former_name_is_not_the_merchant_or_part_of_the_address(self):
        doc=self.receipt('Gerbang Alaf Restaurants Sdn Bhd','(65351-M)','formerly known as','Golden Arches Restaurants Sdn Bhd','Licensee of McDonalds','Level6, Bangunan TH,','Damansara Uptown3','Tel No.: 03-4147')
        doc['m2_text_lines'][0]['text']='RECEIPT'
        self.assertEqual(field(doc,'merchant')['value'],'Gerbang Alaf Restaurants Sdn Bhd')
        self.assertEqual(field(doc,'merchant_address')['value'],'Level6, Bangunan TH,\nDamansara Uptown3')

    def test_former_name_with_overlapping_ocr_boxes_is_still_former(self):
        doc=layout('RECEIPT',[('Current Foods Sdn Bhd',20,50,300,68),('formerly known as',60,66,240,86),('Former Foods Sdn Bhd',20,82,300,102),
            ('12 Jalan Bayu',20,106,300,124),('Cashier: Ann',20,160,200,180),('Date: 31/01/2020',20,185,200,205),('Total:',20,210,120,230),('10.00',200,210,300,230)])
        self.assertEqual(field(doc,'merchant')['value'],'Current Foods Sdn Bhd')

    def test_two_current_names_still_stay_unresolved(self):
        doc=self.receipt('Alpha Stores Sdn Bhd','Beta Stores Sdn Bhd','12 Jalan Bayu')
        doc['m2_text_lines'][0]['text']='RECEIPT'
        cell=field(doc,'merchant')
        self.assertEqual((cell['mapping_status'],cell['value']),('ambiguous',None))


class PosDocumentTests(unittest.TestCase):
    """The printed layouts behind two reviewer reports, built from their OCR line positions."""

    @staticmethod
    def document(lines, width=425, height=691):
        entries = [{'self_ref': f'#/m2_text_lines/{i}', 'text': text, 'label': 'text', 'from_ocr': True,
                    'prov': [{'page_no': 1, 'bbox': {'l': l, 't': t, 'r': r, 'b': b, 'coord_origin': 'TOPLEFT'}}]}
                   for i, (l, t, r, b, text) in enumerate(lines)]
        return {'schema_name': 'DoclingDocument', 'version': '1.8.0', 'origin': {'filename': 'pos.pdf'},
                'pages': {'1': {'size': {'width': width, 'height': height}}}, 'texts': [], 'tables': [], 'm2_text_lines': entries}

    SL = [(0, 2, 88, 30, 'Invoice'), (0, 30, 149, 53, '-- Reprint Receipt --'), (0, 56, 336, 79, 'SL SOFTWARE SOLUTIONS SDN. BHD.'),
          (0, 78, 264, 106, '(201601010552(1181482-M))'), (0, 104, 228, 127, 'NO 231 A,JALAN PASIR PUTEH,'), (0, 126, 140, 149, '31650IPOH,PERAK'),
          (0, 200, 216, 220, 'DocumentNo.: T01-000008'), (0, 224, 48, 246, 'Date:'), (126, 224, 309, 246, '29/12/2023 10:22:56 AM'),
          (0, 294, 68, 316, 'Cashier :'), (126, 296, 184, 314, 'ADMIN'),
          (0, 330, 43, 352, 'DESC'), (127, 331, 196, 350, 'U. PRICE'), (236, 332, 274, 350, 'Disc'), (283, 332, 356, 350, 'AMOUNT'),
          (11, 355, 48, 378, 'QTY'), (167, 356, 196, 376, 'RM'), (326, 356, 356, 376, 'RM'),
          (0, 393, 148, 413, 'Chicken Floss Toast'), (26, 417, 86, 438, '1.00 PC'), (109, 419, 123, 432, '*'), (180, 418, 216, 438, '9.30'), (242, 418, 278, 438, '0.00'), (324, 418, 360, 438, '9.30'),
          (0, 443, 70, 468, 'Earl Grey'), (26, 469, 87, 490, '1.00 PC'), (108, 471, 123, 484, '*'), (180, 469, 217, 490, '8.90'), (242, 469, 278, 490, '0.00'), (324, 469, 360, 490, '8.90'),
          (134, 507, 218, 529, 'Sub Total :'), (320, 508, 364, 528, '18.20'), (96, 535, 218, 556, 'Service Charge :'), (328, 536, 363, 554, '1.82'),
          (44, 560, 218, 582, 'Rounding Adjustment:'), (321, 561, 363, 579, '-0.02'), (8, 589, 220, 618, 'Rounded Total (RM):'), (302, 591, 366, 614, '20.00'),
          (31, 665, 72, 686, 'Cash'), (333, 666, 378, 686, '20.00')]
    CROSS = [(74, 60, 265, 72, 'CROSS CHANNEL NETWORK SDN. BHD.'), (94, 72, 245, 86, '47, JALAN MERANTI 1, SEK. 3,'), (88, 97, 250, 111, '44300 BATANG KALI, SELANGOR.'),
             (68, 109, 270, 124, 'Tel : 03-6057 9688 Fax : 03-6057 9678'), (112, 122, 219, 135, 'GST ID :001151500288'), (128, 134, 202, 150, 'Tax Invoice'), (206, 142, 286, 156, 'No.:BTG-052332'),
             (214, 156, 236, 172, 'Qty'), (244, 156, 287, 170, 'RM Tax'), (42, 170, 204, 184, 'SCHNEIDER E15R 13A SWITCH'), (220, 170, 231, 183, '1'), (241, 170, 284, 184, '6.36 SR'),
             (42, 183, 184, 196, 'SOCKET OUTLET @ 6.0000'), (64, 199, 198, 214, 'Total Amt Incl. GST @ 6%:'), (212, 200, 236, 214, '6.36'),
             (86, 210, 198, 224, 'Rounding Adjustment :'), (208, 211, 236, 224, '-0.01'), (101, 221, 198, 235, 'Total Amt Payable:'), (212, 222, 236, 235, '6.35'),
             (126, 231, 198, 244, 'Paid Amount:'), (206, 232, 237, 244, '10.00'), (152, 240, 198, 254, 'Change:'), (211, 240, 237, 254, '3.65'),
             (66, 264, 137, 280, 'GST Summary'), (166, 266, 208, 280, 'Amount'), (254, 264, 276, 278, 'Tax'), (64, 292, 106, 308, 'SR @ A'), (182, 292, 207, 306, '6.00'), (250, 292, 276, 306, '0.36'),
             (60, 307, 95, 324, 'Total.'), (180, 308, 208, 322, '6.00'), (251, 310, 275, 321, '0.36'), (153, 322, 222, 336, 'THANK YOU'), (118, 372, 235, 386, '29/01/2018 4:40:40 PM')]

    def mapped(self, lines):
        from services.extraction.parsing.map_invoice import map_document
        return map_document(self.document(lines))

    def test_invoice_with_reprint_receipt_marker_stays_an_invoice_with_the_marker_kept(self):
        from services.extraction.classification.classify_document import classify_document
        result = classify_document(self.document(self.SL))
        self.assertEqual((result['value'], result['status']), ('invoice', 'classified'))
        self.assertEqual([c['value'] for c in result['candidates']], ['invoice'])
        self.assertEqual({(s['kind'], s['text']) for s in result['signals']},
                         {('copy_marker', '-- Reprint Receipt --'), ('payment_cue', 'Cash')})

    def test_copy_marker_alone_names_the_document(self):
        from services.extraction.classification.classify_document import classify_document
        result = classify_document(self.document([(0, 0, 200, 20, '-- Reprint Receipt --'), (0, 30, 200, 50, 'Some Shop')]))
        self.assertEqual(result['value'], 'receipt')

    def test_paid_invoice_heading_is_an_invoice(self):
        from services.extraction.classification.classify_document import classify_document
        self.assertEqual(classify_document(self.document([(0, 0, 200, 20, 'Paid Invoice')]))['value'], 'invoice')

    def test_pos_invoice_values_come_from_their_printed_labels(self):
        result = self.mapped(self.SL)
        values = {key: cell['value'] for key, cell in result['fields'].items()}
        self.assertEqual(values['invoice_number'], 'T01-000008')
        self.assertEqual(values['invoice_date'], '2023-12-29')
        self.assertEqual(values['subtotal'], '18.20')
        self.assertEqual(values['document_charges'], '1.82')
        self.assertEqual(values['rounding_amount'], '-0.02')
        self.assertEqual(values['total_amount'], '20.00')
        self.assertEqual(values['supplier_name'], 'SL SOFTWARE SOLUTIONS SDN. BHD.')
        self.assertEqual(values['supplier_address'], 'NO 231 A,JALAN PASIR PUTEH,\n31650IPOH,PERAK')
        self.assertIsNone(values['tax_amount'], 'No tax is printed, so none may be invented.')
        self.assertEqual(result['fields']['invoice_date']['raw_text'], 'Date:\n29/12/2023 10:22:56 AM')
        for key in ('invoice_number', 'invoice_date', 'total_amount', 'document_charges', 'rounding_amount'):
            self.assertTrue(all(e['bbox'] and e['source_ref'] for e in result['fields'][key]['evidence']), key)

    def test_stacked_rows_keep_their_printed_cells(self):
        rows = self.mapped(self.SL)['line_items']
        self.assertEqual([(r['description']['value'], r['quantity']['value'], r['unit_price']['value'], r['line_total']['value']) for r in rows],
                         [('Chicken Floss Toast', '1.00', '9.30', '9.30'), ('Earl Grey', '1.00', '8.90', '8.90')])

    def test_stacked_rows_are_not_published_if_a_row_does_not_multiply_out(self):
        lines = [row if row[4] != '8.90' or row[0] != 180 else (180, 469, 217, 490, '7.90') for row in self.SL]
        self.assertEqual(self.mapped(lines)['line_items'], [])

    def test_stacked_rows_are_not_published_if_they_miss_every_printed_total(self):
        lines = [(l, t, r, b, {'18.20': '18.25', '20.00': '20.05'}.get(text, text)) for l, t, r, b, text in self.SL]
        self.assertEqual(self.mapped(lines)['line_items'], [])

    def test_a_morning_or_ambiguous_timestamp_stays_honest(self):
        from services.extraction.layout.layout_headers import normalized_date
        self.assertEqual(normalized_date('29/12/2023 10:22:56 AM'), '2023-12-29')
        with self.assertRaises(ValueError):
            normalized_date('04/01/2017 10:22:56 AM')

    def test_receipt_number_beside_a_tax_invoice_heading_is_the_document_reference(self):
        result = self.mapped(self.CROSS)
        self.assertEqual(result['provenance']['document_profile'], 'receipt')
        cell = result['fields']['receipt_number']
        self.assertEqual(cell['value'], 'BTG-052332')
        self.assertEqual({e.get('printed_heading') for e in cell['evidence']}, {'Tax Invoice'})
        self.assertNotIn('invoice_number', result['fields'])

    def test_no_number_is_taken_without_a_heading_beside_it(self):
        lines = [row for row in self.CROSS if row[4] != 'Tax Invoice']
        self.assertIsNone(self.mapped(lines)['fields'].get('receipt_number', {}).get('value'))

    def test_two_numbers_beside_the_heading_are_not_chosen_between(self):
        lines = self.CROSS + [(128, 150, 200, 164, 'No.:BTG-999999')]
        self.assertIsNone(self.mapped(lines)['fields'].get('receipt_number', {}).get('value'))

    def test_receipt_time_comes_from_the_printed_timestamp(self):
        self.assertEqual(self.mapped(self.CROSS)['fields']['transaction_time']['value'], '16:40:40')

    def test_one_digit_hour_without_am_or_pm_is_left_unresolved(self):
        lines = [(l, t, r, b, text.replace(' 4:40:40 PM', ' 4:40:40')) for l, t, r, b, text in self.CROSS]
        self.assertIsNone(self.mapped(lines)['fields']['transaction_time']['value'])

    def test_receipt_components_stay_in_their_own_fields(self):
        values = {key: cell['value'] for key, cell in self.mapped(self.CROSS)['fields'].items()}
        self.assertEqual((values['total_excluding_tax'], values['tax_amount'], values['total_including_tax'],
                          values['rounding_amount'], values['total_amount'], values['tendered_amount'], values['change_given']),
                         ('6.00', '0.36', '6.36', '-0.01', '6.35', '10.00', '3.65'))
        self.assertIsNone(values['subtotal'], 'A GST summary base is not a printed subtotal.')


class SourceFactTests(unittest.TestCase):
    """Facts extraction hands to validation. None of them is an accounting decision."""

    @staticmethod
    def document(lines):
        return {'schema_name': 'DoclingDocument', 'version': '1.8.0', 'origin': {'filename': 'x.pdf'}, 'pages': {'1': {'size': {'width': 500, 'height': 800}}},
                'texts': [], 'tables': [], 'm2_text_lines': [
                    {'self_ref': f'#/m2_text_lines/{i}', 'text': text, 'label': 'text', 'from_ocr': True,
                     'prov': [{'page_no': 1, 'bbox': {'l': l, 't': t, 'r': r, 'b': t + 15, 'coord_origin': 'TOPLEFT'}}]}
                    for i, (l, t, r, text) in enumerate(lines)]}

    def test_tax_wording_is_kept_apart(self):
        from services.extraction.layout.layout_headers import tax_statement
        self.assertEqual(tax_statement(self.document([(0, 0, 200, 'This supply is non-taxable')]))['status'], 'exempt')
        self.assertEqual(tax_statement(self.document([(0, 0, 200, 'Prices inclusive of SST')]))['status'], 'inclusive')
        self.assertIsNone(tax_statement(self.document([(0, 0, 200, 'Thank you')])))

    def test_item_structure(self):
        from services.extraction.layout.layout_rows import item_structure
        self.assertEqual(item_structure(self.document([]), [{'x': 1}])['status'], 'itemized')
        self.assertEqual(item_structure(self.document([(0, 0, 80, 'Description'), (300, 0, 400, 'Amount')]), [])['status'], 'unmapped_rows')
        self.assertEqual(item_structure(self.document([(0, 0, 80, 'Total'), (300, 0, 400, '10.00')]), [])['status'], 'not_itemized')

    def test_bill_components_keep_their_printed_signs(self):
        from services.extraction.layout.layout_headers import bill_components
        from services.extraction.parsing.map_invoice import location
        found = bill_components(self.document([
            (0, 0, 100, 'Previous Balance'), (300, 0, 400, '50.00'), (0, 20, 100, 'Payment Received'), (300, 20, 400, '(40.00)'),
            (0, 40, 100, 'Credits'), (300, 40, 400, '5.00 CR'), (0, 60, 100, 'Late Fee'), (300, 60, 400, '-1.00'), (0, 80, 100, 'Arrears'), (300, 80, 400, 'none')]), location)
        self.assertEqual({key: row[1] for key, row in found.items()},
                         {'previous_balance': '50.00', 'amount_paid': '-40.00', 'credit_total': '-5.00', 'late_fee': '-1.00'})


class TaxInclusiveReceiptTests(unittest.TestCase):
    """REC00006's OCR lines, tilt and misreads included. 169.78 already includes the 9.61 of GST."""

    LINES = [(120, 110, 220, 128, 'RECEIPT'), (64, 133, 262, 151, '99 SPEED MART S/B (S19537-X)'), (70, 150, 262, 172, 'LOT P.T. 2811, JALAN ANGSA,'), (82, 228, 260, 243, 'GST ID. NO: 000181747712'),
             (76, 266, 273, 280, 'INVOICE NO: 10222/102/T0341'), (257, 298, 318, 315, '20-11-17'), (36, 306, 92, 324, '03PM'), (154, 303, 202, 320, '562936'),
             (244, 336, 320, 353, 'RM117.90 s'), (39, 344, 232, 358, '2973 PEDIASURE COMPLETE S3S'), (252, 354, 320, 371, 'RM51.88 s'), (40, 364, 130, 380, '069 MILO 2KG'),
             (265, 448, 312, 466, '169.78'), (46, 460, 258, 470, 'Total Sales (Inclusive GST) RM'), (288, 466, 312, 485, '.02'), (100, 476, 260, 490, 'Rounding Adjustment RM'),
             (267, 486, 314, 504, '169.80'), (176, 492, 262, 508, 'Rounding RM'), (206, 508, 262, 526, 'CASH RM'), (268, 504, 316, 523, '200.00'), (194, 528, 264, 546, 'CHANGE RM'), (276, 524, 316, 542, '30.20'),
             (272, 561, 324, 579, 'Tax(RH)'), (162, 568, 236, 586, 'Amount(RH)'), (52, 576, 136, 594, 'GST Summary'), (292, 580, 325, 597, '9.61'), (190, 586, 238, 604, '160.17')]

    def mapped(self, lines=None):
        from services.extraction.parsing.map_invoice import map_document
        return map_document(PosDocumentTests.document(lines or self.LINES, 364, 748))

    def values(self, lines=None):
        return {key: cell['value'] for key, cell in self.mapped(lines)['fields'].items()}

    def test_each_printed_amount_keeps_its_own_meaning(self):
        values = self.values()
        self.assertEqual((values['total_excluding_tax'], values['tax_amount'], values['total_including_tax'], values['rounding_amount'],
                          values['total_amount'], values['tendered_amount'], values['change_given']),
                         ('160.17', '9.61', '169.78', '0.02', '169.80', '200.00', '30.20'))

    def test_no_subtotal_is_invented(self):
        self.assertIsNone(self.values()['subtotal'])

    def test_a_rounding_row_that_does_not_explain_the_totals_is_not_used(self):
        lines = [(l, t, r, b, '.05' if text == '.02' else text) for l, t, r, b, text in self.LINES]
        values = self.values(lines)
        self.assertEqual(values['total_amount'], '169.78')
        self.assertIsNone(values.get('rounding_amount'))

    def test_a_gst_summary_that_does_not_reach_the_total_is_not_used(self):
        lines = [(l, t, r, b, '150.00' if text == '160.17' else text) for l, t, r, b, text in self.LINES]
        values = self.values(lines)
        self.assertIsNone(values.get('total_excluding_tax'))
        self.assertIsNone(values.get('tax_amount'))

    def test_two_digit_year_with_a_day_that_cannot_be_a_month(self):
        result = self.mapped()
        self.assertEqual(result['fields']['transaction_date']['value'], '2017-11-20')
        self.assertTrue(any(e.get('date_basis') == 'day_first_two_digit_year' for e in result['fields']['transaction_date']['evidence']))

    def test_a_two_digit_year_that_could_be_swapped_stays_unresolved(self):
        lines = [(l, t, r, b, '04-11-17' if text == '20-11-17' else text) for l, t, r, b, text in self.LINES]
        self.assertIsNone(self.values(lines)['transaction_date'])

    def test_a_time_with_no_minutes_is_not_invented(self):
        self.assertIsNone(self.values()['transaction_time'])

    def test_a_readable_time_on_the_date_row_is_used(self):
        lines = [(l, t, r, b, '03:29PM' if text == '03PM' else text) for l, t, r, b, text in self.LINES]
        self.assertEqual(self.values(lines)['transaction_time'], '15:29')

    def test_the_model_reading_of_a_clipped_time_is_offered_and_never_verified(self):
        from services.extraction.classification.reconcile_selective import arbitrate, check_candidate_against_source
        document = PosDocumentTests.document(self.LINES, 364, 748)
        self.assertEqual(check_candidate_against_source('transaction_time', '15:29', document)['status'], 'source_near_match')
        self.assertEqual(arbitrate(None, '15:29', 'source_near_match'), 'UNVERIFIED_MODEL_SUGGESTION')
        self.assertEqual(check_candidate_against_source('transaction_time', '09:29', document)['status'], 'not_found_in_source')
        # The same line from a second OCR stream is still one fragment.
        document['texts'] = [{'self_ref': '#/texts/0', 'text': '03PM', 'label': 'text', 'prov': document['m2_text_lines'][6]['prov']}]
        self.assertEqual(check_candidate_against_source('transaction_time', '15:29', document)['status'], 'source_near_match')

    def test_it_reaches_validation_without_counting_tax_twice(self):
        from services.extraction.contracts.accounting_contract import map_m2_result
        from tests.extraction.test_multidocument import contract
        from validation.documents.document_validation import evaluate_document
        raw, mapped = contract(PosDocumentTests.document(self.LINES, 364, 748))
        result = evaluate_document({'kind': 'receipt', 'category': 'accounts_payable'}, {'raw_json': raw}, mapped['fields'], mapped['line_items'])
        self.assertNotIn('RECEIPT-TOTAL', {e['code'] for e in result['errors']})
        total = next(c for c in result['checks'] if c['rule'] == 'RECEIPT-TOTAL')
        self.assertIn('160.17 + 9.61 tax + 0.02 rounding = 169.80', total['message'])
        self.assertEqual(next(c for c in result['checks'] if c['rule'] == 'RCP-005')['checkStatus'], 'CHECKED')
