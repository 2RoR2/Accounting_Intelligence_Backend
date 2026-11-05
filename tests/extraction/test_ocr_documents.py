"""Small invented OCR regions; no private transcripts, images or annotations."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from services.extraction.contracts.accounting_contract import map_m2_result
from services.extraction.ocr.ocr_documents import _money, map_rapidocr_document
from services.extraction.ocr.ocr_receipt import map_rapidocr_receipt
from tests.extraction.test_ocr_receipt import source, ordinary_source, VERSIONS


def invoice():
    entries = [('Supplier: Synthetic Paper Ltd', 20, 10, 320, 30),
               ('-TAX INVOICE-', 80, 60, 300, 85),
               ('Invoice No: INV-00027', 20, 100, 290, 120),
               ('Date: 2026-10-25', 20, 135, 290, 155),
               ('Description', 20, 200, 140, 220), ('Qty', 160, 200, 190, 220),
               ('Price', 220, 200, 280, 220), ('Amount', 380, 200, 450, 220),
               ('1 X 12.00', 150, 255, 300, 275),
               ('Subtotal: 12.00', 20, 320, 260, 340),
               ('Tax: 0.00', 20, 355, 260, 375),
               ('Total Incl. GST@6%', 20, 400, 280, 420),
               ('RM 12.00', 330, 400, 450, 420),
               ('Cash', 20, 450, 90, 470), ('RM 20.00', 330, 450, 450, 470),
               ('Change', 20, 490, 110, 510), ('RM 8.00', 330, 490, 450, 510)]
    art = source();art['rows'] = [
        {'order': i, 'text': text, 'engine_score': .97,
         'box_polygon_pixels': [[l,t],[r,t],[r,b],[l,b]]}
        for i,(text,l,t,r,b) in enumerate(entries)]
    return art


def run(art=None):
    return map_rapidocr_document(invoice() if art is None else art, 'SYN-DISPATCH', VERSIONS)


def replace(art, index, text):
    art['rows'][index]['text'] = text
    return art


def receipt_time(text=None):
    art=invoice();replace(art,1,'CASH RECEIPT');replace(art,2,'Receipt number: RC-00027')
    replace(art,0,'Merchant: Synthetic Paper Ltd')
    if text is not None:
        art['rows'].append({'order':len(art['rows']),'text':text,'engine_score':.97,
            'box_polygon_pixels':[[20,550],[300,550],[300,575],[20,575]]})
    return art


class OcrDocumentTests(unittest.TestCase):
    def test_strong_tax_invoice_dispatch_and_existing_profile_labels(self):
        result = run()
        self.assertEqual(result['document_type']['value'], 'invoice')
        self.assertEqual(result['fields']['supplier_name']['value'], 'Synthetic Paper Ltd')
        self.assertEqual(result['fields']['invoice_number']['value'], 'INV-00027')
        self.assertEqual(result['fields']['invoice_date']['value'], '2026-10-25')
        self.assertEqual(result['fields']['total_amount']['value'], '12.00')

    def test_unfamiliar_cash_receipt_uses_transaction_structure(self):
        art = invoice();replace(art,1,'CASH RECÉIPT');replace(art,2,'CB#: RC-00027')
        result = run(art)
        self.assertEqual(result['document_type']['value'], 'receipt')
        self.assertEqual(result['fields']['receipt_number']['value'], 'RC-00027')
        self.assertEqual(result['fields']['transaction_date']['value'], '2026-10-25')

    def test_plain_receipt_without_original_column_shape(self):
        art = invoice();replace(art,1,'RECEIPT');replace(art,2,'Receipt number: RC-27')
        self.assertEqual(run(art)['document_type']['value'], 'receipt')

    def test_mixed_invoice_receipt_semantics_remain_ambiguous(self):
        art = invoice();replace(art,1,'Fatura/Recibo')
        result = run(art)
        self.assertEqual(result['document_type']['status'],'ambiguous')
        self.assertEqual(result['fields'],{})
        self.assertTrue(result['document_type']['candidates'])

    def test_missing_heading_and_prose_do_not_classify(self):
        for text in ['Please keep this tax invoice', 'GENERAL INFORMATION', 'Invoice No: AA-27']:
            with self.subTest(text=text):
                self.assertEqual(run(replace(invoice(),1,text))['document_type']['value'],'unknown')

    def test_contradictory_heading_remains_unknown(self):
        result = run(replace(invoice(),0,'RECEIPT'))
        self.assertEqual(result['document_type']['status'],'ambiguous')
        self.assertEqual(result['fields'],{})

    def test_unsupported_types_and_bills_not_newly_enabled(self):
        for title in ['QUOTATION','CREDIT NOTE','PRO FORMA INVOICE','UTILITY BILL']:
            with self.subTest(title=title):
                self.assertEqual(run(replace(invoice(),1,title))['document_type']['value'],'unknown')

    def test_low_quality_or_incomplete_heading_cannot_classify(self):
        art=invoice();art['rows'][1]['engine_score']=.5
        self.assertEqual(run(art)['document_type']['value'],'unknown')
        self.assertEqual(run(replace(invoice(),1,'Ja0 xl'))['document_type']['value'],'unknown')

    def test_heading_without_item_structure_is_insufficient(self):
        art=invoice()
        for i in [4,5,6,7,8]:replace(art,i,'General text')
        self.assertEqual(run(art)['document_type']['value'],'unknown')

    def test_late_heading_cannot_classify(self):
        art=invoice();art['rows'][1]['box_polygon_pixels']=[[20,550],[250,550],[250,575],[20,575]]
        self.assertEqual(run(art)['document_type']['value'],'unknown')

    def test_missing_invoice_number_does_not_erase_supported_fields(self):
        result=run(replace(invoice(),2,'Unlabelled reference'))
        self.assertEqual(result['document_type']['value'],'invoice')
        self.assertEqual(result['fields']['invoice_number']['state'],'missing')
        self.assertEqual(result['fields']['total_amount']['value'],'12.00')

    def test_multiple_reference_candidates_remain_conflicting(self):
        result=run(replace(invoice(),0,'Invoice number: INV-00028'))
        self.assertEqual(result['fields']['invoice_number']['state'],'conflict')
        self.assertIsNone(result['fields']['invoice_number']['value'])

    def test_missing_monetary_fields_do_not_become_zero(self):
        art=invoice()
        for i in [9,10,11,12]:replace(art,i,'Not supplied')
        result=run(art)
        self.assertEqual(result['document_type']['value'],'invoice')
        for key in ['subtotal','tax_amount','total_amount']:
            self.assertIsNone(result['fields'][key]['value'])

    def test_explicit_zero_tax_is_distinct_from_absence(self):
        self.assertEqual(run()['fields']['tax_amount']['value'],'0.00')
        self.assertEqual(run(replace(invoice(),10,'No tax label'))['fields']['tax_amount']['state'],'missing')

    def test_total_never_replaced_by_cash_or_change(self):
        result=run()
        self.assertEqual(result['fields']['total_amount']['value'],'12.00')
        self.assertEqual(result['receipt_observations']['cash_tendered']['value'],'20.00')
        canonical=map_m2_result(result,'SYN-DISPATCH')
        self.assertNotIn('cash_tendered',{f['field_key'] for f in canonical['fields']})

    def test_unlabelled_company_is_not_verified_supplier(self):
        result=run(replace(invoice(),0,'Unlabelled Example SDN BHD'))
        self.assertIsNone(result['fields']['supplier_name']['value'])

    def test_no_country_currency_inference(self):
        art=invoice();replace(art,12,'12.00');replace(art,14,'20.00');replace(art,16,'8.00')
        self.assertIsNone(run(art)['fields']['currency']['value'])

    def test_conflicting_currencies_and_totals_remain_reviewable(self):
        result=run(replace(invoice(),14,'USD 20.00'))
        self.assertEqual(result['fields']['currency']['state'],'conflict')
        result=run(replace(invoice(),0,'Total: 99.00'))
        self.assertEqual(result['fields']['total_amount']['state'],'conflict')

    def test_low_quality_amount_stays_ambiguous(self):
        art=invoice();art['rows'][12]['engine_score']=.6
        self.assertEqual(run(art)['fields']['total_amount']['state'],'ambiguous')

    def test_low_quality_corroboration_cannot_supply_missing_context(self):
        art=invoice();replace(art,2,'No reference');replace(art,3,'No date')
        art['rows'][12]['engine_score']=.4
        self.assertEqual(run(art)['document_type']['value'],'unknown')

    def test_damaged_explicit_identity_and_invalid_currency_remain_unresolved(self):
        art=invoice();replace(art,0,'Supplier: Synthetic _ Ltd');replace(art,9,'Currency: UNKNOWN')
        result=run(art)
        self.assertIsNone(result['fields']['supplier_name']['value'])
        self.assertIsNone(result['fields']['currency']['value'])
        self.assertTrue(result['fields']['currency']['evidence'])

    def test_ambiguous_date_and_two_digit_year_not_guessed(self):
        for text in ['Date: 03/06/2017','Date: 24-03-18','Date: 2026-02-30']:
            with self.subTest(text=text):
                self.assertIsNone(run(replace(invoice(),3,text))['fields']['invoice_date']['value'])

    def test_unambiguous_named_month_header_date(self):
        art=invoice();replace(art,3,'14May/2017 02.56:39')
        self.assertEqual(run(art)['fields']['invoice_date']['value'],'2017-05-14')

    def test_header_date_boxes_may_overlap_heading_but_not_items(self):
        art=invoice();replace(art,3,'14May/2017 02.56:39')
        art['rows'][3]['box_polygon_pixels']=[[310,80],[480,80],[480,105],[310,105]]
        self.assertEqual(run(art)['fields']['invoice_date']['value'],'2017-05-14')
        art['rows'][3]['box_polygon_pixels']=[[310,205],[480,205],[480,230],[310,230]]
        self.assertIsNone(run(art)['fields']['invoice_date']['value'])

    def test_evidence_scores_order_and_original_source_retained(self):
        art=invoice();before=deepcopy(art);result=run(art)
        ev=result['fields']['total_amount']['evidence'][-1]
        self.assertEqual(ev['bbox_original'],art['rows'][12]['box_polygon_pixels'])
        self.assertEqual(ev['source_ref'],'#/rows/12')
        self.assertEqual(ev['engine_score'],.97)
        self.assertIsNone(result['fields']['total_amount']['confidence'])
        self.assertEqual(result['provenance']['native_ocr_result'],before)
        self.assertEqual(art,before)
        self.assertNotIn('DoclingDocument',str(result))

    def test_existing_receipt_results_exactly_preserved(self):
        for art in [source(),ordinary_source()]:
            with self.subTest(source=art['rows'][0]['text']):
                self.assertEqual(run(art),map_rapidocr_receipt(art,'SYN-DISPATCH',VERSIONS))

    def test_partial_results_do_not_invent_items_or_approval(self):
        result=run()
        self.assertEqual(result['line_items'],[])
        self.assertTrue(result['review_required'])
        self.assertFalse(result['accounting_approval_performed'])

    def test_pure_mapping_has_no_database_or_network_writes(self):
        with patch('psycopg.connect',side_effect=AssertionError('DB forbidden')), patch('socket.socket.connect',side_effect=AssertionError('Network forbidden')):
            result=run()
        self.assertEqual(result['document_type']['value'],'invoice')

    def test_conflicting_attached_currency_qualifiers_do_not_resolve(self):
        result=run(replace(invoice(),12,'MYR 12.00 USD'))
        self.assertIsNone(result['fields']['total_amount']['value'])
        self.assertEqual(result['fields']['total_amount']['state'],'ambiguous')
        self.assertIsNone(result['fields']['currency']['value'])

    def test_invalid_optional_time_retains_partial_canonical_fields(self):
        art=receipt_time('Time: not-a-time')
        result=run(art)
        self.assertIsNone(result['fields']['transaction_time']['value'])
        canonical=map_m2_result(result,'SYN-DISPATCH')
        fields={f['field_key']:f['current_value'] for f in canonical['fields']}
        self.assertEqual(fields['supplier'],'Synthetic Paper Ltd')
        self.assertEqual(fields['date'],'2026-10-25')
        self.assertEqual(fields['currency'],'MYR')
        self.assertEqual(fields['total'],'12.00')
        self.assertEqual(fields['receipt_number'],'RC-00027')

    def test_conflicting_currency_variants_retain_raw_field_evidence(self):
        for text in ['MYR 12.00 USD','USD 12.00 MYR','RM 12.00 USD','MYR USD 12.00']:
            with self.subTest(text=text):
                art=replace(invoice(),12,text);before=deepcopy(art);result=run(art)
                field=result['fields']['total_amount']
                self.assertIsNone(_money(text))
                self.assertEqual(field['state'],'ambiguous')
                self.assertIsNone(field['value'])
                self.assertEqual(field['raw_text'],text)
                self.assertEqual(field['evidence'][-1]['bbox_original'],art['rows'][12]['box_polygon_pixels'])
                self.assertEqual(result['fields']['currency']['state'],'conflict')
                self.assertEqual(art,before)
                self.assertTrue(result['review_required'])

    def test_inline_currency_contradiction_is_not_lost(self):
        art=replace(invoice(),11,'Total: MYR 12.00 USD');replace(art,12,'Not a monetary region')
        result=run(art)
        self.assertIsNone(result['fields']['total_amount']['value'])
        self.assertEqual(result['fields']['currency']['state'],'conflict')
        self.assertIn('MYR 12.00 USD',result['fields']['currency']['raw_text'])

    def test_single_consistent_currency_qualifiers_remain_valid(self):
        for code in ['MYR','RM','USD']:
            with self.subTest(code=code):
                art=invoice()
                for i,value in [(12,'12.00'),(14,'20.00'),(16,'8.00')]:replace(art,i,code+' '+value)
                result=run(art)
                self.assertEqual(result['fields']['total_amount']['value'],'12.00')
                self.assertEqual(result['fields']['currency']['value'],'MYR' if code=='RM' else code)

    def test_repeated_consistent_qualifiers_and_rm_alias(self):
        for text in ['MYR 12.00 MYR','RM 12.00 MYR','MYR MYR 12.00 MYR','12.00 RM']:
            with self.subTest(text=text):
                result=run(replace(invoice(),12,text))
                self.assertEqual(_money(text),'12.00')
                self.assertEqual(result['fields']['total_amount']['value'],'12.00')
                self.assertEqual(result['fields']['currency']['value'],'MYR')

    def test_independent_currency_amounts_do_not_ambiguate_each_amount(self):
        art=replace(invoice(),14,'USD 20.00');replace(art,16,'USD 8.00')
        result=run(art)
        self.assertEqual(result['fields']['total_amount']['value'],'12.00')
        self.assertEqual(result['receipt_observations']['cash_tendered']['value'],'20.00')
        self.assertEqual(result['receipt_observations']['change']['value'],'8.00')
        # Existing document-level single-currency contract still requires review.
        self.assertEqual(result['fields']['currency']['state'],'conflict')
        self.assertIsNone(result['fields']['currency']['value'])

    def test_unrelated_currency_prose_does_not_poison_amounts(self):
        art=receipt_time('USD exchange rates are informational')
        result=run(art)
        self.assertEqual(result['fields']['currency']['value'],'MYR')
        self.assertEqual(result['fields']['total_amount']['value'],'12.00')

    def test_malformed_optional_time_candidates_remain_ambiguous(self):
        for value in ['not-a-time','25:61','24:00','12:00:60','01:15 PM']:
            with self.subTest(value=value):
                art=receipt_time('Time: '+value);result=run(art)
                cell=result['fields']['transaction_time']
                self.assertEqual(cell['state'],'ambiguous')
                self.assertIsNone(cell['value'])
                self.assertEqual(cell['raw_text'],value)
                self.assertEqual(cell['evidence'][0]['source_ref'],'#/rows/17')
                canonical=map_m2_result(result,'SYN-DISPATCH')
                self.assertEqual(next(f['current_value'] for f in canonical['fields'] if f['field_key']=='total'),'12.00')

    def test_valid_canonical_24_hour_times_resolve(self):
        for value in ['00:00','23:59','07:15:09']:
            with self.subTest(value=value):
                result=run(receipt_time('Time: '+value))
                self.assertEqual(result['fields']['transaction_time']['value'],value)
                canonical=map_m2_result(result,'SYN-DISPATCH')
                self.assertEqual(next(f['current_value'] for f in canonical['fields'] if f['field_key']=='transaction_time'),value)

    def test_missing_and_empty_optional_time_stay_missing(self):
        for art in [receipt_time(),receipt_time('Time:')]:
            result=run(art)
            self.assertEqual(result['fields']['transaction_time']['state'],'missing')
            self.assertIsNone(result['fields']['transaction_time']['value'])
            self.assertEqual(result['provenance']['native_ocr_result'],art)
            map_m2_result(result,'SYN-DISPATCH')

    def test_malformed_time_does_not_fail_image_boundary(self):
        from tests.extraction.test_image_parser import ImageBoundaryTests
        case=ImageBoundaryTests();case.setUp()
        try:
            art=receipt_time('Time: not-a-time')
            def reply(envelope):
                art['image_sha256']=envelope['ocr']['image_sha256'];envelope['ocr']=art;return envelope
            case.reply_transform=reply
            with patch('services.extraction.ocr.image_parser.subprocess.run',side_effect=case.child):
                result=case.execute()
            self.assertEqual(result['execution_status'],'EXTRACTED')
            self.assertIsNone(result['error'])
            self.assertTrue(result['review_required'])
        finally:case.doCleanups()


if __name__=='__main__':unittest.main()
