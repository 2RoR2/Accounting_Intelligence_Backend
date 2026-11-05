"""Synthetic routing-policy tests, not document accuracy evaluation."""
from copy import deepcopy
import hashlib
import json
from tempfile import TemporaryDirectory
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from services.extraction.parsing.extraction_v1 import HEADERS, ROW_FIELDS, adapt_result
from services.extraction.classification.route_extraction import decide_route, plan_field_recovery, reserve_recovery_attempt

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

class RecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.configuration={'model':'qwen2.5vl:7b','options':{'temperature':0},'render_scale':1.5}

    def bind(self,data,doc):
        from services.extraction.classification.classify_document import classify_document
        path=self.root/'document.json';path.write_text(json.dumps(doc))
        hashed=hashlib.sha256(path.read_bytes()).hexdigest()
        data['document_type']={**classify_document(doc),'source_document_json':str(path),'source_json_sha256':hashed}
        data['provenance'].update(source_document_json=str(path),source_json_sha256=hashed,
            parser_run={'status':'success','source':str(self.root/'source.pdf'),
                        'source_sha256':hashlib.sha256((self.root/'source.pdf').read_bytes()).hexdigest()})

    def case(self,kind='invoice',extra=None):
        from tests.extraction.test_financial_layout import layout
        from services.extraction.parsing.map_invoice import map_document
        title={'invoice':'INVOICE','bill':'UTILITY BILL','receipt':'RECEIPT','credit_note':'CREDIT NOTE','unknown':'Unidentified document'}[kind]
        party={'invoice':'Supplier','bill':'Issuer','receipt':'Merchant'}.get(kind,'Supplier')
        lines=[party+': Example Trading','Date: 25/12/2025','Invoice number: X-42','Total: 40.00']
        if extra:lines.append(extra)
        doc=layout(title,[(text,20,60+i*60,350,80+i*60) for i,text in enumerate(lines)])
        (self.root/'source.pdf').write_bytes(b'known parser input bytes; no renderer or model used')
        mapped=map_document(doc);mapped['run_id']='test-plan'
        data=adapt_result(mapped,'PLAN-TEST','mapped.json','a'*64).model_dump()
        data['schema_version']='1.1.0-proposed'
        data['classification_attachment']=fixture()['classification_attachment']
        self.bind(data,doc)
        return data,doc

    def missing(self,data,name):
        data['fields'][name].update(state='missing',value=None,raw_text=None,evidence=[],candidates=[])

    def plan(self,data,**kwargs):
        return plan_field_recovery(data,self.root,source_access_authorized=True,
                                   model_configuration=self.configuration,**kwargs)

    def test_missing_party_recovery_is_independent_of_document_review(self):
        for kind,field in [('invoice','supplier_name'),('bill','issuer'),('receipt','merchant')]:
            with self.subTest(kind=kind):
                data,_=self.case(kind);self.missing(data,field);before=deepcopy(data)
                result=self.plan(data)
                self.assertTrue(result['fields'][field]['eligible'])
                self.assertTrue(result['document_review_required']);self.assertFalse(result['inference_authorized'])
                self.assertFalse(result['ai_call_performed']);self.assertTrue(result['source_verified'])
                self.assertEqual(decide_route(data,False)['decision'],'human_review')
                self.assertEqual(data,before)

    def test_resolved_values_and_unsupported_fields_are_protected(self):
        data,_=self.case();plan=self.plan(data)
        self.assertEqual(plan['fields']['supplier_name']['reason'],'RESOLVED_PARSER_VALUE_PROTECTED')
        self.assertFalse(plan['fields']['currency']['eligible'])
        data['fields']['supplier_name'].update(state='unsupported',value=None)
        self.assertEqual(self.plan(data)['fields']['supplier_name']['reason'],'UNSUPPORTED_OR_UNREADABLE_STATE')

    def test_ambiguous_printed_totals_retain_candidates_and_no_arithmetic(self):
        data,_=self.case('receipt','Total: 41.00');before=deepcopy(data)
        self.assertEqual(data['fields']['total_amount']['state'],'ambiguous')
        self.assertTrue(self.plan(data)['fields']['total_amount']['eligible'])
        data['fields']['total_amount']['state']='conflict'
        self.assertTrue(self.plan(data)['fields']['total_amount']['eligible'])
        data['fields']['total_amount']['state']='ambiguous'
        self.assertEqual(data,before)

    def test_weak_resolved_field_is_comparison_only(self):
        data,_=self.case();data['fields']['supplier_name']['evidence']=[]
        data['issues'].append({'code':'INCOMPLETE_EVIDENCE','detail':'supplier_name'})
        before=deepcopy(data);field=self.plan(data)['fields']['supplier_name']
        self.assertTrue(field['eligible']);self.assertTrue(field['comparison_only'])
        self.assertEqual(data,before)

    def test_missing_wrapper_needs_printed_value_and_credible_geometry(self):
        for text in ('Supplier:','Supplier: N/A','Unrelated heading'):
            with self.subTest(text=text):
                data,doc=self.case();doc['m2_text_lines'][1]['text']=text;self.bind(data,doc)
                self.missing(data,'supplier_name')
                self.assertEqual(self.plan(data)['fields']['supplier_name']['reason'],'NO_VERIFIABLE_PRINTED_FIELD_CUE')
        data,doc=self.case();self.missing(data,'supplier_name');doc['pages']={};self.bind(data,doc)
        self.assertFalse(self.plan(data)['fields']['supplier_name']['eligible'])

    def test_authorization_corrupt_missing_and_relocated_sources(self):
        from unittest.mock import patch
        data,_=self.case();self.missing(data,'supplier_name')
        with patch('services.extraction.ai.run_selective_qwen.verified_pdf') as verify:
            result=plan_field_recovery(data,self.root,model_configuration=self.configuration)
            verify.assert_not_called();self.assertIn('SOURCE_ACCESS_NOT_AUTHORIZED',result['blocking_reasons'])
        replacement=self.root/'relocated.pdf';replacement.write_bytes((self.root/'source.pdf').read_bytes())
        (self.root/'source.pdf').unlink()
        self.assertFalse(self.plan(data)['fields']['supplier_name']['eligible'])
        self.assertTrue(self.plan(data,source_path=replacement)['fields']['supplier_name']['eligible'])
        replacement.write_bytes(b'different input')
        self.assertFalse(self.plan(data,source_path=replacement)['fields']['supplier_name']['eligible'])
        (self.root/'document.json').write_text('{}')
        self.assertFalse(self.plan(data,source_path=replacement)['source_verified'])

    def test_unknown_notes_and_nonextraction_issues_never_trigger_recovery(self):
        for kind in ('unknown','credit_note'):
            data,_=self.case(kind)
            self.assertIn('DOCUMENT_TYPE_REQUIRES_REVIEW',self.plan(data)['blocking_reasons'])
        data,_=self.case();self.missing(data,'supplier_name')
        data['issues'].append({'code':'BANK_RECONCILIATION_DISAGREEMENT','detail':'supplier_name'})
        self.assertIn('UNHANDLED_OR_NONEXTRACTION_ISSUE',self.plan(data)['blocking_reasons'])

    def test_attempt_identity_and_execution_budget_are_deterministic_and_local(self):
        data,_=self.case()
        for name in ('supplier_name','invoice_number','invoice_date','total_amount'):self.missing(data,name)
        before=deepcopy(data);plan=self.plan(data);attempts={}
        self.assertEqual(plan,self.plan(data));self.assertEqual(attempts,{})
        self.assertTrue(reserve_recovery_attempt(plan,'supplier_name',attempts))
        self.assertFalse(reserve_recovery_attempt(plan,'supplier_name',attempts))
        self.configuration['options']['temperature']=1
        second=self.plan(data)
        self.assertNotEqual(plan['fields']['supplier_name']['attempt_id'],second['fields']['supplier_name']['attempt_id'])
        self.assertFalse(reserve_recovery_attempt(second,'supplier_name',attempts))
        self.assertEqual(self.plan(data,prior_attempts=attempts)['fields']['supplier_name']['reason'],'ATTEMPT_ALREADY_RESERVED')
        self.assertTrue(reserve_recovery_attempt(plan,'invoice_number',attempts))
        self.assertTrue(reserve_recovery_attempt(plan,'invoice_date',attempts))
        self.assertFalse(reserve_recovery_attempt(plan,'total_amount',attempts))
        self.assertEqual(data,before)

if __name__ == '__main__':
    unittest.main()


