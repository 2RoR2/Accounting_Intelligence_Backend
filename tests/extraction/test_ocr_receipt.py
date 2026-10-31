"""Synthetic negative cases plus opt-in replay of private, real RapidOCR output.

No receipt image, transcript, personal data or released annotation is embedded.
Set SAIC_RECEIPT_OCR_ARTIFACT to the approved saved result for private replay.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest

from services.extraction.ocr.ocr_receipt import map_rapidocr_receipt
from services.extraction.contracts.accounting_contract import map_m2_result
from validation.documents.document_validation import evaluate_document

VERSIONS = {'rapidocr': '3.9.2', 'onnxruntime': '1.31.0'}


def source():
    entries = [
        ('Synthetic _ Retail SDN BHD', 20, 20, 300, 40),
        ('Document No : SYN-9', 20, 80, 250, 100),
        ('Date :', 20, 110, 90, 130), ('01/10/2026', 130, 110, 260, 130),
        ('CASH BILL', 150, 150, 290, 175),
        ('CODE/DESC', 20, 200, 120, 220), ('PRICE', 200, 200, 250, 220),
        ('AMOUNT', 380, 200, 450, 220), ('QTY', 60, 225, 110, 245),
        ('RM', 220, 225, 250, 245), ('RM', 420, 225, 450, 245),
        ('Synthetic item', 150, 265, 350, 285),
        ('1 PC', 70, 295, 115, 315), ('12.000', 200, 295, 250, 315),
        ('12.00', 405, 295, 450, 315),
        ('Total :', 240, 340, 300, 360), ('12.00', 405, 340, 450, 360),
        ('Cash', 200, 380, 250, 400), ('20.00', 405, 380, 450, 400),
        ('CHANGE', 200, 415, 280, 435), ('800', 405, 415, 450, 435),
    ]
    return {'status': 'PASS', 'method': 'direct RapidOCR; not Docling pipeline',
            'annotation_input': False, 'device': 'cpu', 'image_sha256': 'a' * 64,
            'image_size': {'width': 500, 'height': 600},
            'rows': [{'order': i, 'text': text, 'engine_score': .95,
                      'box_polygon_pixels': [[l, t], [r, t], [r, b], [l, b]]}
                     for i, (text, l, t, r, b) in enumerate(entries)]}


def mapped(artifact=None):
    return map_rapidocr_receipt(source() if artifact is None else artifact, 'SYN-OCR', VERSIONS)


class OcrReceiptTests(unittest.TestCase):
    def test_native_ocr_classifies_only_corroborated_cash_bill(self):
        result = mapped()
        self.assertEqual((result['document_type']['value'], result['document_type']['status']), ('receipt', 'classified'))
        self.assertEqual(result['provenance']['source_kind'], 'rapidocr_regions')
        self.assertNotIn('DoclingDocument', json.dumps(result))

    def test_prose_and_heading_without_transaction_cues_are_unknown(self):
        for text in ['Please retain this CASH BILL', 'CASH BILL']:
            artifact = source()
            artifact['rows'][4]['text'] = text
            artifact['rows'][6]['text'] = 'GENERAL INFORMATION'
            result = mapped(artifact)
            self.assertEqual(result['document_type']['value'], 'unknown')
            self.assertEqual(result['fields'], {})
            self.assertEqual(result['line_items'], [])

    def test_unrelated_cash_bill_prose_with_transaction_cues_is_unknown(self):
        artifact = source(); artifact['rows'][4]['text'] = 'Description: CASH BILL stationery'
        self.assertEqual(mapped(artifact)['document_type']['status'], 'unknown')

    def test_heading_must_precede_retail_columns_and_payment(self):
        artifact = source(); artifact['rows'][4]['box_polygon_pixels'] = [[150, 500], [290, 500], [290, 525], [150, 525]]
        self.assertEqual(mapped(artifact)['document_type']['status'], 'unknown')

    def test_unsupported_heading_vetoes_receipt(self):
        for title in ['QUOTATION', 'CREDIT NOTE', 'DEBIT NOTE', 'PRO FORMA INVOICE', 'PURCHASE ORDER']:
            with self.subTest(title=title):
                artifact = source(); artifact['rows'][0]['text'] = title
                self.assertEqual(mapped(artifact)['document_type']['status'], 'unsupported')

    def test_competing_invoice_or_bill_is_ambiguous(self):
        for title in ['INVOICE', 'UTILITY BILL']:
            artifact = source(); artifact['rows'][0]['text'] = title
            self.assertEqual(mapped(artifact)['document_type']['status'], 'ambiguous')

    def test_date_reference_currency_and_total(self):
        fields = mapped()['fields']
        self.assertEqual(fields['transaction_date']['value'], '2026-10-01')
        self.assertEqual(fields['receipt_number']['value'], 'SYN-9')
        self.assertEqual(fields['currency']['value'], 'MYR')
        self.assertEqual(fields['total_amount']['value'], '12.00')
        self.assertEqual(mapped()['receipt_observations']['cash_tendered']['value'], '20.00')

    def test_change_has_no_inferred_decimal_or_arithmetic_fallback(self):
        result = mapped()
        change = result['receipt_observations']['change']
        self.assertIsNone(change['value'])
        self.assertEqual(change['state'], 'ambiguous')
        self.assertEqual(change['raw_text'], '800')
        self.assertNotIn('change', result['fields'])
        self.assertNotIn('cash_tendered', result['fields'])

    def test_geometrically_oversized_handwritten_amount_not_assigned(self):
        artifact = source()
        artifact['rows'][20]['text'] = '12.00'
        artifact['rows'][20]['box_polygon_pixels'] = [[330, 410], [400, 410], [400, 470], [330, 470]]
        self.assertIsNone(mapped(artifact)['receipt_observations']['change']['value'])

    def test_well_aligned_explicit_change_can_be_observed_without_posting(self):
        artifact = source(); artifact['rows'][20]['text'] = '8.00'
        self.assertEqual(mapped(artifact)['receipt_observations']['change']['value'], '8.00')

    def test_unlabelled_merchant_is_candidate_not_verified(self):
        merchant = mapped()['fields']['merchant']
        self.assertEqual(merchant['state'], 'ambiguous')
        self.assertIsNone(merchant['value'])
        self.assertEqual(merchant['candidates'][0]['value'], 'Synthetic _ Retail SDN BHD')
        self.assertIsNone(merchant['confidence'])

    def test_missing_currency_is_not_defaulted(self):
        artifact = source()
        artifact['rows'][9]['text'] = artifact['rows'][10]['text'] = 'UNIT'
        self.assertEqual(mapped(artifact)['fields']['currency']['state'], 'missing')
        self.assertIsNone(mapped(artifact)['fields']['currency']['value'])

    def test_conflicting_currencies_not_silently_selected(self):
        artifact = source(); artifact['rows'][9]['text'] = 'USD'
        self.assertEqual(mapped(artifact)['fields']['currency']['state'], 'conflict')
        self.assertIsNone(mapped(artifact)['fields']['currency']['value'])

    def test_missing_reference_or_invalid_date_cannot_corroborate(self):
        for index, text in [(1, 'General information'), (3, '31/02/2026')]:
            artifact = source(); artifact['rows'][index]['text'] = text
            self.assertEqual(mapped(artifact)['fields'], {})

    def test_multiple_explicit_total_values_remain_conflicting(self):
        artifact = source()
        artifact['rows'][9]['text'] = 'Total: 99.00'
        artifact['rows'][9]['box_polygon_pixels'] = [[20, 365], [180, 365], [180, 378], [20, 378]]
        field = mapped(artifact)['fields']['total_amount']
        self.assertEqual(field['state'], 'conflict')
        self.assertIsNone(field['value'])
        self.assertEqual({c['value'] for c in field['candidates']}, {'12.00', '99.00'})

    def test_missing_merchant_is_not_guessed_from_person_or_address(self):
        artifact = source(); artifact['rows'][0]['text'] = 'Unlabelled header'
        field = mapped(artifact)['fields']['merchant']
        self.assertEqual(field['state'], 'missing')
        self.assertIsNone(field['value'])

    def test_cash_label_raw_text_preserved_without_payment_status(self):
        artifact = source(); artifact['rows'][17]['text'] = 'CASH TENDERED'
        result = mapped(artifact)
        self.assertEqual(result['fields']['payment_method']['raw_text'], 'CASH TENDERED')
        self.assertEqual(result['fields']['payment_method']['value'], 'Cash')
        self.assertNotIn('paid', result['fields'])

    def test_amount_overflow_does_not_invent_financial_value(self):
        artifact = source(); artifact['rows'][16]['text'] = '999999999999999.00'
        self.assertEqual(mapped(artifact)['fields'], {})

    def test_total_does_not_fall_back_to_cash_or_item_amount(self):
        artifact = source(); artifact['rows'][16]['text'] = 'NaN'
        self.assertEqual(mapped(artifact)['fields'], {})

    def test_item_columns_preserve_exact_decimals(self):
        line = mapped()['line_items'][0]
        self.assertEqual(line['quantity']['value'], '1')
        self.assertEqual(line['unit_price']['value'], '12.000')
        self.assertEqual(line['line_total']['value'], '12.00')
        self.assertEqual(line['description']['value'], 'Synthetic item')

    def test_incomplete_item_is_not_invented(self):
        artifact = source(); artifact['rows'][13]['text'] = 'UNREADABLE'
        line = mapped(artifact)['line_items'][0]
        self.assertIsNone(line['unit_price']['value'])
        self.assertEqual(line['unit_price']['state'], 'ambiguous')

    def test_input_evidence_and_provenance_are_preserved_without_mutation(self):
        artifact = source(); before = deepcopy(artifact); result = mapped(artifact)
        self.assertEqual(artifact, before)
        self.assertEqual(result['provenance']['native_ocr_result'], before)
        evidence = result['fields']['total_amount']['evidence']
        self.assertEqual(evidence[1]['bbox_original'], artifact['rows'][16]['box_polygon_pixels'])
        self.assertEqual(evidence[1]['source_ref'], '#/rows/16')
        self.assertEqual(evidence[1]['coordinate_units'], 'pixels')
        self.assertEqual(evidence[1]['page'], 1)

    def test_canonical_contract_retains_ocr_method_and_unresolved_merchant(self):
        raw = mapped(); canonical = map_m2_result(raw, 'SYN-OCR')
        fields = {f['field_key']: f for f in canonical['fields']}
        self.assertEqual(fields['total']['current_value'], '12.00')
        self.assertEqual(fields['date']['current_value'], '2026-10-01')
        self.assertIsNone(fields['supplier']['current_value'])
        self.assertEqual(fields['total']['extraction_method'], 'OCR')
        self.assertEqual(fields['total']['model_version'], '3.9.2')
        self.assertNotIn('cash_tendered', fields)
        self.assertIsNone(canonical['category'])
        check = evaluate_document({'kind': 'receipt'}, {'raw_json': raw}, canonical['fields'], canonical['line_items'])
        self.assertTrue(check['review_required'])
        self.assertFalse(check['accounting_approval_performed'])

    def test_rejects_annotation_replay_and_missing_identity(self):
        for key, value in [('annotation_input', True), ('method', 'released transcript'), ('image_sha256', '')]:
            artifact = source(); artifact[key] = value
            with self.assertRaises(ValueError): mapped(artifact)

    def test_rejects_invalid_coordinates_order_and_scores(self):
        for key, value in [('box_polygon_pixels', [[-1, 0]] * 4), ('order', 99), ('engine_score', float('nan'))]:
            artifact = source(); artifact['rows'][0][key] = value
            with self.assertRaises(ValueError): mapped(artifact)



def ordinary_source():
    """Invented ordinary receipt geometry; no real document transcript in Git."""
    entries = [
        ('Synthetic Corner Shop', 30, 25, 320, 45),
        ('RECEIPT', 170, 105, 290, 130),
        ('02/10/2026 14:35:20 #03', 25, 155, 300, 175),
        ('Desc/Item', 25, 215, 125, 235), ('Qty', 180, 215, 215, 235),
        ('Price', 245, 215, 300, 235), ('Amt(RM)', 380, 215, 460, 235),
        ('Synthetic pencil pack', 25, 260, 280, 280),
        ('12.00', 385, 290, 455, 310),
        ('TOTAL AMT....', 25, 345, 180, 365), ('12.00', 385, 345, 455, 365),
        ('CASH...', 25, 410, 130, 430), ('20.00', 385, 410, 455, 430),
        ('CHANGE...', 25, 455, 140, 475), ('8.00', 385, 455, 455, 475),
    ]
    result = source()
    result['rows'] = [{'order': i, 'text': value, 'engine_score': .95,
                       'box_polygon_pixels': [[l, t], [r, t], [r, b], [l, b]]}
                      for i, (value, l, t, r, b) in enumerate(entries)]
    return result


class OrdinaryReceiptTests(unittest.TestCase):
    def test_receipt_heading_requires_layout_and_transaction_evidence(self):
        raw = mapped(ordinary_source())
        self.assertEqual(raw['document_type']['value'], 'receipt')
        self.assertEqual(raw['document_type']['reason'], 'CORROBORATED_RECEIPT')
        self.assertEqual(raw['document_type']['candidates'][0]['matched_title'], 'receipt')
        self.assertEqual(raw['document_type']['candidates'][0]['evidence']['region_order'], 1)

    def test_unlabelled_header_date_and_explicit_amount_labels(self):
        raw = mapped(ordinary_source())
        self.assertEqual(raw['fields']['transaction_date']['value'], '2026-10-02')
        self.assertEqual(raw['fields']['total_amount']['value'], '12.00')
        self.assertEqual(raw['fields']['currency']['value'], 'MYR')
        self.assertEqual(raw['receipt_observations']['cash_tendered']['value'], '20.00')
        self.assertEqual(raw['receipt_observations']['change']['value'], '8.00')
        self.assertIsNone(raw['fields']['receipt_number']['value'])
        self.assertIsNone(raw['fields']['merchant']['value'])
        self.assertEqual(raw['line_items'], [])  # No observed quantity: never default to one.

    def test_prose_occurrences_and_missing_cues_remain_unknown(self):
        for index, value in [(1, 'Keep your receipt for returns'), (1, 'Receipt reference: SYN'),
                             (3, 'General notes'), (4, 'Counter'), (5, 'Reference'),
                             (6, 'Notes'), (2, '31/02/2026 14:35:20 #03'),
                             (9, 'Unlabelled summary')]:
            with self.subTest(index=index, value=value):
                art = ordinary_source(); art['rows'][index]['text'] = value
                self.assertEqual(mapped(art)['document_type']['value'], 'unknown')

    def test_header_date_outside_receipt_header_is_not_assigned(self):
        art = ordinary_source()
        art['rows'][2]['box_polygon_pixels'] = [[25, 480], [300, 480], [300, 500], [25, 500]]
        self.assertEqual(mapped(art)['document_type']['value'], 'unknown')

    def test_table_columns_must_align_and_follow_heading(self):
        for index, box in [(1, [[170, 480], [290, 480], [290, 505], [170, 505]]),
                           (4, [[180, 250], [215, 250], [215, 270], [180, 270]])]:
            art = ordinary_source(); art['rows'][index]['box_polygon_pixels'] = box
            self.assertEqual(mapped(art)['document_type']['value'], 'unknown')

    def test_other_headings_keep_ordinary_receipt_conservative(self):
        for title, status in [('INVOICE', 'ambiguous'), ('UTILITY BILL', 'ambiguous'),
                              ('QUOTATION', 'unsupported'), ('PRO FORMA INVOICE', 'unsupported'),
                              ('CREDIT NOTE', 'unsupported'), ('DEBIT NOTE', 'unsupported'),
                              ('PURCHASE ORDER', 'unsupported')]:
            with self.subTest(title=title):
                art = ordinary_source(); art['rows'][0]['text'] = title
                self.assertEqual(mapped(art)['document_type']['status'], status)
                self.assertEqual(mapped(art)['fields'], {})

    def test_rounding_blocks_total_without_inventing_adjusted_value(self):
        art = ordinary_source()
        art['rows'].append({'order': len(art['rows']), 'text': 'ROUNDING ADJ......',
                            'engine_score': .95, 'box_polygon_pixels': [[25, 375], [180, 375], [180, 395], [25, 395]]})
        raw = mapped(art)
        self.assertEqual(raw['document_type']['value'], 'receipt')
        self.assertEqual(raw['fields']['total_amount']['state'], 'ambiguous')
        self.assertIsNone(raw['fields']['total_amount']['value'])
        self.assertTrue(any('ROUNDING' in e['text'] for e in raw['fields']['total_amount']['evidence']))
        self.assertEqual(raw['receipt_observations']['cash_tendered']['value'], '20.00')

    def test_conflicting_totals_are_reviewable_not_silently_chosen(self):
        art = ordinary_source()
        art['rows'].append({'order': len(art['rows']), 'text': 'Total: 13.00',
                            'engine_score': .95, 'box_polygon_pixels': [[25, 375], [180, 375], [180, 395], [25, 395]]})
        field = mapped(art)['fields']['total_amount']
        self.assertEqual(field['state'], 'conflict')
        self.assertIsNone(field['value'])

    def test_native_regions_and_canonical_contract_preserved(self):
        art = ordinary_source(); before = deepcopy(art); raw = mapped(art)
        canonical = map_m2_result(raw, 'SYN-OCR')
        fields = {f['field_key']: f for f in canonical['fields']}
        self.assertEqual(fields['total']['current_value'], '12.00')
        self.assertEqual(fields['total']['extraction_method'], 'OCR')
        self.assertEqual(raw['provenance']['native_ocr_result'], before)
        self.assertEqual(art, before)
        self.assertEqual(fields['date']['evidence'][0]['bbox_original'], art['rows'][2]['box_polygon_pixels'])
        self.assertEqual(fields['date']['evidence'][0]['source_ref'], '#/rows/2')
        validation = evaluate_document({'kind': 'receipt'}, {'raw_json': raw}, canonical['fields'], canonical['line_items'])
        self.assertTrue(validation['review_required'])
        self.assertFalse(validation['coverage_complete'])


@unittest.skipUnless(os.getenv('SAIC_ORDINARY_RECEIPT_OCR_ARTIFACT'), 'Private ordinary OCR artifact not selected.')
class RealOrdinaryReceiptReplayTests(unittest.TestCase):
    def setUp(self):
        self.envelope = json.loads(Path(os.environ['SAIC_ORDINARY_RECEIPT_OCR_ARTIFACT']).read_text(encoding='utf-8-sig'))
        self.ocr = self.envelope['ocr']
        self.raw = map_rapidocr_receipt(self.ocr, 'PRIVATE-ORDINARY', self.envelope['versions'])

    def test_real_ordinary_receipt_classification_and_supported_fields(self):
        self.assertEqual(self.raw['document_type']['value'], 'receipt')
        self.assertEqual(self.raw['fields']['transaction_date']['value'], '2018-10-19')
        self.assertEqual(self.raw['fields']['currency']['value'], 'MYR')
        self.assertIsNone(self.raw['fields']['merchant']['value'])
        self.assertIsNone(self.raw['fields']['receipt_number']['value'])

    def test_real_rounding_and_missing_cash_label_remain_unresolved(self):
        self.assertEqual(self.raw['fields']['total_amount']['state'], 'ambiguous')
        self.assertIsNone(self.raw['fields']['total_amount']['value'])
        self.assertIsNone(self.raw['receipt_observations']['cash_tendered']['value'])
        self.assertEqual(self.raw['receipt_observations']['change']['value'], '10.00')
        self.assertEqual(self.raw['line_items'], [])

    def test_real_canonical_evidence_review_and_no_input_mutation(self):
        canonical = map_m2_result(self.raw, 'PRIVATE-ORDINARY')
        self.assertEqual(self.raw['provenance']['native_ocr_result'], self.ocr)
        fields = {f['field_key']: f for f in canonical['fields']}
        self.assertEqual(fields['date']['current_value'], '2018-10-19')
        self.assertIsNone(fields['total']['current_value'])
        self.assertTrue(fields['total']['evidence'])
        self.assertNotIn('cash_tendered', fields)
        validation = evaluate_document({'kind': 'receipt'}, {'raw_json': self.raw}, canonical['fields'], canonical['line_items'])
        self.assertTrue(validation['review_required'])
        self.assertFalse(validation['accounting_approval_performed'])
        self.assertFalse(validation['coverage_complete'])


@unittest.skipUnless(os.getenv('SAIC_RECEIPT_OCR_ARTIFACT'), 'Private real OCR artifact not selected; synthetic tests remain portable.')
class RealReceiptReplayTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ['SAIC_RECEIPT_OCR_ARTIFACT'])
        self.artifact = json.loads(path.read_text(encoding='utf-8-sig'))
        self.raw = map_rapidocr_receipt(self.artifact, 'PRIVATE-RECEIPT', VERSIONS)

    def test_real_classification_and_key_values(self):
        self.assertEqual(self.raw['document_type']['value'], 'receipt')
        expected = {'transaction_date': '2018-12-25', 'receipt_number': 'TD01167104', 'currency': 'MYR', 'total_amount': '9.00'}
        for name, value in expected.items():
            self.assertEqual(self.raw['fields'][name]['value'], value)
        self.assertEqual(self.raw['receipt_observations']['cash_tendered']['value'], '10.00')

    def test_real_uncertainty_and_item_row(self):
        self.assertIsNone(self.raw['fields']['merchant']['value'])
        self.assertIsNone(self.raw['receipt_observations']['change']['value'])
        self.assertEqual(self.raw['receipt_observations']['change']['raw_text'], '100')
        self.assertEqual(len(self.raw['line_items']), 1)
        self.assertEqual(self.raw['line_items'][0]['line_total']['value'], '9.00')
        self.assertEqual(self.raw['line_items'][0]['unit_price']['value'], '9.000')
        self.assertEqual(self.raw['line_items'][0]['quantity']['value'], '1')

    def test_real_canonical_evidence_and_validation(self):
        canonical = map_m2_result(self.raw, 'PRIVATE-RECEIPT')
        fields = {f['field_key']: f for f in canonical['fields']}
        self.assertEqual(fields['total']['current_value'], '9.00')
        self.assertTrue(all(f['evidence'] for f in canonical['fields']))
        self.assertEqual(self.raw['provenance']['native_ocr_result']['rows'], self.artifact['rows'])
        validation = evaluate_document({'kind': 'receipt'}, {'raw_json': self.raw}, canonical['fields'], canonical['line_items'])
        self.assertTrue(validation['review_required'])
        self.assertFalse(validation['coverage_complete'])


if __name__ == '__main__':
    unittest.main()
