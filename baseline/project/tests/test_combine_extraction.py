"""Synthetic integration checks for source identity and artifact preservation."""
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from extraction_v1 import HEADERS, ROW_FIELDS, adapt_result
from classify_document import classify_document
from combine_extraction import combine_files

class CombineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'document.json'
        self.extraction = self.root / 'extraction.json'
        self.classification = self.root / 'classification.json'
        self.prepare('INVOICE - SYNTHETIC TEST DATA')

    def write(self, path, data):
        path.write_text(json.dumps(data), encoding='utf-8')

    def prepare(self, title):
        doc = {'schema_name': 'DoclingDocument', 'texts': [{'text': title,
               'label': 'title', 'self_ref': '#/texts/0', 'prov': [{'page_no': 1}]}]}
        self.write(self.source, doc)
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        def field():
            return {'raw_text': 'sample', 'value': 'sample', 'evidence': [],
                    'candidates': [], 'mapping_status': 'mapped', 'review_required': True}
        legacy = {'schema_version': 'draft-0.2', 'mode': 'real_parser_mapping',
                  'document_id': 'synthetic.pdf', 'run_id': 'mapped-test',
                  'status': 'needs_review', 'fields': {k: field() for k in HEADERS},
                  'line_items': [{k: field() for k in ROW_FIELDS}],
                  'issues': [{'code': 'HUMAN_REVIEW_REQUIRED', 'detail': 'test'}],
                  'provenance': {'source_document_json': str(self.source),
                                 'source_json_sha256': digest}}
        result = adapt_result(legacy, 'DOC-SYN-001', 'mapped.json', 'a' * 64)
        self.write(self.extraction, result.model_dump())
        classification = classify_document(doc)
        classification.update(source_document_json=str(self.source), source_json_sha256=digest)
        self.write(self.classification, classification)

    def combine(self):
        return combine_files(self.extraction, self.classification, self.root)

    def test_combination_preserves_fields_and_inputs(self):
        before = {p: p.read_bytes() for p in (self.extraction, self.classification, self.source)}
        old = json.loads(before[self.extraction])
        result = self.combine().model_dump()
        for key in ('document_id', 'fields', 'line_items', 'issues', 'provenance',
                    'adapter', 'legacy_document_id', 'legacy_run_id', 'review_required'):
            self.assertEqual(result[key], old[key])
        self.assertEqual(result['document_type'], json.loads(before[self.classification]))
        self.assertNotEqual(result['extraction_run_id'], old['extraction_run_id'])
        self.assertEqual(result['classification_attachment']['parent_extraction_run_id'], old['extraction_run_id'])
        self.assertTrue(all(p.read_bytes() == raw for p, raw in before.items()))

    def test_recorded_hash_mismatch_rejected(self):
        data = json.loads(self.classification.read_text())
        data['source_json_sha256'] = '0' * 64
        self.write(self.classification, data)
        with self.assertRaisesRegex(ValueError, 'hashes differ'): self.combine()

    def test_modified_source_rejected(self):
        self.source.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'changed'): self.combine()

    def test_different_path_even_same_bytes_rejected(self):
        other = self.root / 'other.json'
        other.write_bytes(self.source.read_bytes())
        data = json.loads(self.classification.read_text())
        data['source_document_json'] = str(other)
        self.write(self.classification, data)
        with self.assertRaisesRegex(ValueError, 'paths differ'): self.combine()

    def test_changed_classification_evidence_rejected(self):
        data = json.loads(self.classification.read_text())
        data['candidates'][0]['evidence']['text'] = 'fabricated heading'
        self.write(self.classification, data)
        with self.assertRaisesRegex(ValueError, 'does not match'): self.combine()

    def test_bill_and_receipt_not_assigned_invoice_schema(self):
        for title in ('BILL', 'RECEIPT'):
            with self.subTest(title=title):
                self.prepare(title)
                with self.assertRaisesRegex(ValueError, 'not implemented'): self.combine()

    def test_unknown_remains_reviewable_unknown(self):
        self.prepare('Unrecognised heading')
        result = self.combine()
        self.assertEqual(result.document_type.value, 'unknown')
        self.assertEqual(result.document_type.status, 'unknown')
        self.assertEqual(result.status, 'needs_review')

    def test_source_outside_project_rejected(self):
        data = json.loads(self.classification.read_text())
        data['source_document_json'] = str(self.root.parent / 'outside.json')
        self.write(self.classification, data)
        with self.assertRaisesRegex(ValueError, 'within the project'): self.combine()

    def test_input_outside_project_rejected(self):
        with self.assertRaisesRegex(ValueError, 'within the project'):
            combine_files(self.root.parent / 'outside.json', self.classification, self.root)

    def test_bad_status_pair_rejected(self):
        data = json.loads(self.classification.read_text())
        data['status'] = 'unknown'
        self.write(self.classification, data)
        with self.assertRaisesRegex(ValueError, 'classified status'): self.combine()

    def test_wrong_classifier_version_rejected(self):
        data = json.loads(self.classification.read_text())
        data['classifier_version'] = 'unverified-version'
        self.write(self.classification, data)
        with self.assertRaises(ValueError): self.combine()

if __name__ == '__main__':
    unittest.main()


