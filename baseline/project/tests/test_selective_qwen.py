"""Provider and selective execution tests use a fake server, never real AI."""
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from classify_document import classify_document
from qwen_provider import prepare_request, validate_response
from run_selective_qwen import run_attempt, render_single_page
from test_route_extraction import fixture

class ProviderTests(unittest.TestCase):
    def response(self, content):
        return {'done': True, 'message': {'content': content}}

    def test_only_requested_field_in_schema(self):
        p = prepare_request(b'image', ['invoice_number'])
        self.assertEqual(set(p['format']['properties']), {'invoice_number'})
        self.assertEqual(p['options']['num_ctx'], 4096)
        self.assertFalse(p['stream'])

    def test_null_is_valid(self):
        self.assertEqual(validate_response(self.response('{"invoice_number":null}'), ['invoice_number']),
                         {'invoice_number': None})

    def test_extra_fields_rejected(self):
        with self.assertRaises(ValueError):
            validate_response(self.response('{"invoice_number":null,"total_amount":"1"}'), ['invoice_number'])

    def test_missing_fields_rejected(self):
        with self.assertRaises(ValueError): validate_response(self.response('{}'), ['invoice_number'])

    def test_duplicate_fields_rejected(self):
        with self.assertRaises(ValueError):
            validate_response(self.response('{"invoice_number":"A","invoice_number":"B"}'), ['invoice_number'])

    def test_bad_values_rejected(self):
        for value in (12, True, '', '   ', [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_response(self.response(json.dumps({'invoice_number': value})), ['invoice_number'])

    def test_incomplete_response_rejected(self):
        with self.assertRaises(ValueError): validate_response({'done': False}, ['invoice_number'])

    def test_unknown_and_duplicate_requests_rejected(self):
        for fields in ([], ['unsupported'], ['invoice_number', 'invoice_number']):
            with self.subTest(fields=fields), self.assertRaises(ValueError): prepare_request(b'image', fields)

class SelectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'extraction.json'
        self.pdf = self.root / 'missing_number.pdf'
        self.doc = self.root / 'document.json'
        self.pdf.write_bytes(b'synthetic bytes for mocked renderer')
        doc = {'schema_name': 'DoclingDocument', 'texts': [{'text': 'Invoice',
              'label': 'title', 'self_ref': '#/texts/0', 'prov': [{'page_no': 1}]}]}
        self.doc.write_text(json.dumps(doc))
        digest = hashlib.sha256(self.doc.read_bytes()).hexdigest()
        self.payload = fixture()
        classification = classify_document(doc)
        classification.update(source_document_json=str(self.doc), source_json_sha256=digest)
        self.payload['document_type'] = classification
        self.payload['provenance'] = {'source_document_json': str(self.doc), 'source_json_sha256': digest,
            'parser_run': {'status': 'success', 'source': str(self.pdf),
                           'source_sha256': hashlib.sha256(self.pdf.read_bytes()).hexdigest()}}
        self.provider = Mock()
        self.provider.metadata.return_value = {'models': []}
        self.provider.chat.return_value = json.dumps({'done': True, 'message': {'content': '{"invoice_number":null}'}})
        self.factory = Mock(return_value=self.provider)
        self.renderer = Mock(side_effect=self.render)

    def render(self, pdf, target):
        target.write_bytes(b'synthetic rendered image bytes')
        return [100, 100]

    def missing(self):
        self.payload['fields']['invoice_number'].update(value=None, raw_text=None,
            state='missing', legacy_mapping_status='missing', evidence=[])
        self.payload['issues'].append({'code': 'UNMAPPED_FIELD', 'detail': 'invoice_number: missing'})

    def run_case(self):
        self.source.write_text(json.dumps(self.payload))
        return run_attempt(self.source, self.root, self.factory, self.renderer)

    def test_parser_only_makes_no_provider_or_render_call(self):
        out, record = self.run_case()
        self.assertEqual(record['status'], 'skipped_parser_only')
        self.factory.assert_not_called()
        self.renderer.assert_not_called()
        self.assertTrue((out/'run.json').exists())

    def test_manual_review_makes_no_provider_call(self):
        self.payload['issues'].append({'code': 'UNSUPPORTED_TABLE', 'detail': 'test'})
        _, record = self.run_case()
        self.assertEqual(record['status'], 'skipped_human_review')
        self.factory.assert_not_called()

    def test_missing_number_calls_only_requested_field_and_preserves_null(self):
        self.missing()
        before = json.dumps(self.payload)
        out, record = self.run_case()
        self.assertEqual(record['status'], 'response_structure_valid_needs_review')
        self.provider.chat.assert_called_once()
        request = self.provider.chat.call_args.args[0]
        self.assertEqual(request['format']['required'], ['invoice_number'])
        self.assertEqual(json.loads((out/'prediction.json').read_text()), {'invoice_number': None})
        self.assertEqual(self.source.read_text(), before)
        self.assertTrue(record['review_required'])

    def test_changed_pdf_blocks_provider(self):
        self.missing()
        self.pdf.write_bytes(b'changed')
        _, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.factory.assert_not_called()

    def test_changed_document_blocks_provider(self):
        self.missing()
        self.doc.write_text('{}')
        _, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.factory.assert_not_called()

    def test_timeout_is_saved_without_retry(self):
        self.missing()
        self.provider.chat.side_effect = TimeoutError('synthetic timeout')
        out, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.assertTrue(record['ai_request_attempted'])
        self.assertFalse(record['ai_response_received'])
        self.provider.chat.assert_called_once()
        self.assertIn('timeout', (out/'run.json').read_text())

    def test_malformed_raw_response_saved_before_validation(self):
        self.missing()
        self.provider.chat.return_value = 'not JSON'
        out, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.assertEqual((out/'raw_response.txt').read_text(), 'not JSON')
        self.assertFalse((out/'prediction.json').exists())

    def test_renderer_failure_blocks_provider(self):
        self.missing()
        self.renderer.side_effect = ValueError('multiple pages not supported')
        _, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.factory.assert_not_called()

    def test_heading_tampering_blocks_provider(self):
        self.missing()
        self.payload['document_type']['candidates'][0]['evidence']['text'] = 'false heading'
        _, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.factory.assert_not_called()

if __name__ == '__main__':
    unittest.main()


