"""Synthetic adapter regression tests; these are not document accuracy tests."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from extraction_v1 import HEADERS, ROW_FIELDS, FieldResult, adapt_result

def field(value='sample'):
    return {'raw_text': value, 'value': value, 'evidence': [{'page': 1, 'bbox': None}],
            'mapping_status': 'mapped', 'review_required': True, 'candidates': []}

def fixture():
    return {'schema_version': 'draft-0.2', 'mode': 'real_parser_mapping',
            'document_id': 'synthetic.pdf', 'status': 'needs_review',
            'fields': {k: field() for k in HEADERS},
            'line_items': [{k: field() for k in ROW_FIELDS}],
            'issues': [{'code': 'HUMAN_REVIEW_REQUIRED', 'detail': 'Synthetic test'}],
            'provenance': {'parser_run': {'source_sha256': 'a' * 64}}, 'run_id': 'mapped-test'}

def adapt(data):
    return adapt_result(data, 'DOC-SYN-001', 'synthetic/result.json', 'b' * 64)

class AdapterTests(unittest.TestCase):
    def test_preserve_data_without_mutating_input(self):
        old = fixture()
        original = deepcopy(old)
        result = adapt(old)
        self.assertEqual(old, original)
        self.assertEqual(result.provenance, old['provenance'])
        self.assertEqual(result.issues, old['issues'])
        self.assertEqual(result.fields['supplier_name'].evidence, old['fields']['supplier_name']['evidence'])
        self.assertEqual(result.line_items[0]['quantity'].value, 'sample')
        self.assertEqual(result.document_id, 'DOC-SYN-001')
        self.assertEqual(result.legacy_document_id, 'synthetic.pdf')
        self.assertEqual(result.document_type.status, 'not_run')
        self.assertTrue(result.review_required)

    def test_missing_stays_null(self):
        old = fixture()
        old['fields']['invoice_number'].update(value=None, raw_text=None, mapping_status='missing')
        result = adapt(old)
        self.assertEqual(result.fields['invoice_number'].state, 'missing')
        self.assertIsNone(result.fields['invoice_number'].value)

    def test_ambiguity_preserves_candidates(self):
        old = fixture()
        candidates = [{'raw_text': v, 'value': v, 'evidence': []} for v in ('INV-A', 'INV-B')]
        old['fields']['invoice_number'].update(value=None, mapping_status='ambiguous', candidates=candidates)
        result = adapt(old)
        f = result.fields['invoice_number']
        self.assertEqual(f.state, 'ambiguous')
        self.assertEqual([c.model_dump() for c in f.candidates], candidates)

    def test_amount_format_difference_is_not_conflict(self):
        old = fixture()
        old['fields']['total_amount'].update(raw_text='Total: MYR 50.00', value='50.00',
            candidates=[{'raw_text': 'Total: MYR 50.00', 'value': 'MYR 50.00', 'evidence': []}])
        self.assertEqual(adapt(old).fields['total_amount'].state, 'resolved')

    def test_unknown_legacy_status_rejected(self):
        old = fixture()
        old['fields']['invoice_number']['mapping_status'] = 'new_status'
        with self.assertRaises(ValueError): adapt(old)

    def test_unexpected_legacy_information_rejected(self):
        old = fixture()
        old['fields']['invoice_number']['new_evidence'] = 'keep me'
        with self.assertRaises(ValueError): adapt(old)

    def test_missing_header_rejected(self):
        old = fixture()
        del old['fields']['currency']
        with self.assertRaises(ValueError): adapt(old)

    def test_unresolved_value_rejected(self):
        old = fixture()
        old['fields']['invoice_number']['mapping_status'] = 'missing'
        with self.assertRaises(ValueError): adapt(old)

    def test_empty_rows_need_explanation(self):
        old = fixture()
        old['line_items'] = []
        with self.assertRaises(ValueError): adapt(old)
        old['issues'].append({'code': 'NO_MAPPED_LINE_ITEMS', 'detail': 'Unsupported table'})
        self.assertEqual(adapt(old).line_items, [])

    def test_conflict_requires_distinct_candidates_and_no_selected_value(self):
        f = adapt(fixture()).fields['invoice_number'].model_dump()
        f.update(state='conflict', value=None)
        with self.assertRaises(ValueError): FieldResult.model_validate(f)
        f['candidates'] = [{'raw_text': v, 'value': v, 'evidence': []} for v in ('INV-A', 'INV-B')]
        self.assertEqual(FieldResult.model_validate(f).state, 'conflict')
        f['value'] = 'INV-A'
        with self.assertRaises(ValueError): FieldResult.model_validate(f)

    def test_numeric_amount_rejected(self):
        old = fixture()
        old['fields']['total_amount']['value'] = 50.0
        with self.assertRaises(ValueError): adapt(old)

    def test_each_adaptation_has_unique_run_identity(self):
        self.assertNotEqual(adapt(fixture()).extraction_run_id, adapt(fixture()).extraction_run_id)

if __name__ == '__main__':
    unittest.main()


