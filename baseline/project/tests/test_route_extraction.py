"""Synthetic routing-policy tests, not document accuracy evaluation."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from extraction_v1 import HEADERS, ROW_FIELDS, adapt_result
from route_extraction import decide_route

def fixture():
    def field():
        return {'raw_text': 'sample', 'value': 'sample', 'mapping_status': 'mapped',
                'review_required': True, 'candidates': [],
                'evidence': [{'page': 1, 'source_ref': '#/texts/0',
                              'bbox': {'l': 0, 't': 0, 'r': 10, 'b': 10, 'coord_origin': 'TOPLEFT'}}]}
    legacy = {'schema_version': 'draft-0.2', 'mode': 'real_parser_mapping',
              'document_id': 'synthetic.pdf', 'run_id': 'mapped-test', 'status': 'needs_review',
              'fields': {k: field() for k in HEADERS},
              'line_items': [{k: field() for k in ROW_FIELDS}],
              'issues': [{'code': 'HUMAN_REVIEW_REQUIRED', 'detail': 'test'}], 'provenance': {}}
    data = adapt_result(legacy, 'DOC-SYN-TEST', 'mapped.json', 'a' * 64).model_dump()
    data['schema_version'] = '1.1.0-proposed'
    data['document_type'] = {'classifier_version': '0.1.0', 'method': 'explicit_heading_rules',
                             'value': 'invoice', 'status': 'classified', 'reason': 'EXPLICIT_DOCUMENT_HEADING',
                             'candidates': [], 'unsupported_headings': [], 'review_required': True,
                             'source_document_json': 'document.json', 'source_json_sha256': 'b' * 64}
    data['classification_attachment'] = {'version': '0.1.0', 'parent_extraction_run_id': 'parent',
        'extraction_result_path': 'extraction.json', 'extraction_result_sha256': 'c' * 64,
        'classification_result_path': 'classification.json', 'classification_result_sha256': 'd' * 64,
        'source_identity_verified': True, 'heading_rules_rechecked': True}
    return data

class RoutingTests(unittest.TestCase):
    def test_complete_invoice_parser_only_still_needs_review(self):
        d = decide_route(fixture())
        self.assertEqual(d['decision'], 'parser_only')
        self.assertTrue(d['review_required'])
        self.assertFalse(d['ai_call_performed'])

    def test_missing_number_is_candidate_and_stays_null(self):
        p = fixture()
        p['fields']['invoice_number'].update(value=None, state='missing', legacy_mapping_status='missing', evidence=[])
        p['issues'].append({'code': 'UNMAPPED_FIELD', 'detail': 'invoice_number: missing'})
        before = deepcopy(p)
        d = decide_route(p)
        self.assertEqual(d['decision'], 'vlm_candidate')
        self.assertEqual(d['requested_fields'], ['invoice_number'])
        self.assertEqual(p, before)

    def test_ambiguous_required_header_is_candidate(self):
        p = fixture()
        p['fields']['invoice_number'].update(value=None, state='ambiguous', legacy_mapping_status='ambiguous')
        p['issues'].append({'code': 'CONFLICTING_VALUES', 'detail': 'invoice_number: multiple labels'})
        self.assertEqual(decide_route(p)['decision'], 'vlm_candidate')

    def test_conflict_is_manual(self):
        p = fixture()
        p['fields']['invoice_number'].update(value=None, state='conflict', candidates=[
            {'raw_text': v, 'value': v, 'evidence': []} for v in ('A', 'B')])
        self.assertEqual(decide_route(p)['decision'], 'human_review')

    def test_amount_format_issue_is_manual(self):
        p = fixture()
        p['fields']['total_amount'].update(value=None, state='ambiguous')
        p['issues'].append({'code': 'AMOUNT_REQUIRES_REVIEW', 'detail': 'total_amount: currency disagreement'})
        self.assertEqual(decide_route(p)['decision'], 'human_review')

    def test_missing_optional_tax_is_not_ai_trigger(self):
        p = fixture()
        p['fields']['tax_amount'].update(value=None, state='missing')
        self.assertEqual(decide_route(p)['decision'], 'human_review')

    def test_unknown_classification_is_manual(self):
        p = fixture()
        p['document_type'].update(value='unknown', status='unknown')
        self.assertEqual(decide_route(p)['decision'], 'human_review')

    def test_incomplete_evidence_blocks_ai_candidate(self):
        p = fixture()
        p['fields']['invoice_number'].update(value=None, state='missing')
        p['fields']['supplier_name']['evidence'] = []
        d = decide_route(p)
        self.assertEqual(d['decision'], 'human_review')
        self.assertEqual(d['requested_fields'], [])

    def test_unresolved_row_is_manual(self):
        p = fixture()
        p['line_items'][0]['quantity'].update(value=None, state='missing')
        self.assertEqual(decide_route(p)['decision'], 'human_review')

    def test_unsupported_table_is_manual(self):
        p = fixture()
        p['issues'].append({'code': 'UNSUPPORTED_TABLE', 'detail': 'merged cells'})
        self.assertEqual(decide_route(p)['decision'], 'human_review')

    def test_unknown_issue_is_manual(self):
        p = fixture()
        p['issues'].append({'code': 'NEW_ISSUE', 'detail': 'unhandled'})
        self.assertEqual(decide_route(p)['decision'], 'human_review')

    def test_wrong_schema_rejected(self):
        p = fixture()
        p['schema_version'] = 'draft-0.2'
        with self.assertRaises(ValueError): decide_route(p)

if __name__ == '__main__':
    unittest.main()


