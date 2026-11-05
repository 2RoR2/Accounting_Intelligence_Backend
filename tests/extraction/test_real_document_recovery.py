"""Unit/contract checks of recovery rules; not real-document accuracy claims."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from services.extraction.classification.classify_document import classify_document
from services.extraction.parsing.map_invoice import map_document
from services.extraction.layout.layout_headers import normalized_amount, normalized_date
from services.extraction.workflow.execution import execute_document_extraction


def document(*lines):
    return {'schema_name': 'DoclingDocument', 'pages': {'1': {'size': {'width': 600, 'height': 800}}},
            'texts': [{'text': text, 'self_ref': f'#/texts/{i}', 'label': 'text',
                       'prov': [{'page_no': 1, 'bbox': {'l': 10, 't': 10 + i * 30,
                          'r': 250, 'b': 30 + i * 30, 'coord_origin': 'TOPLEFT'}}]}
                      for i, text in enumerate(lines)]}


class RecoveryTests(unittest.TestCase):
    def test_spacing_retry_uses_only_mapped_ocr_party_lines_and_keeps_originals(self):
        from PIL import Image
        from services.extraction.parsing.baseline_parser import recover_party_spacing
        from tests.extraction.test_financial_layout import layout
        doc=layout('INVOICE',[('Seller:',20,100,80,120),('NorthStar Ltd',20,140,230,160),
                              ('Tax Id: ID-1234',20,200,230,220)])
        calls=[]
        def reader(image,**kwargs):
            calls.append((image.shape,kwargs)); return SimpleNamespace(txts=['North Star Ltd'])
        stats=recover_party_spacing(doc,reader,lambda _:Image.new('RGB',(600,900),'white'))
        self.assertEqual(stats['attempted_lines'],1); self.assertEqual(stats['accepted_lines'],1)
        self.assertEqual(len(calls),2)
        self.assertTrue(all(c[1]=={'use_det':False,'use_cls':False,'use_rec':True} for c in calls))
        self.assertEqual(doc['m2_text_lines'][2]['text'],'NorthStar Ltd')
        self.assertEqual(doc['m2_text_lines'][2]['ocr_spacing']['value'],'North Star Ltd')
        self.assertNotIn('ocr_spacing',doc['m2_text_lines'][3])
        self.assertEqual(map_document(doc)['fields']['supplier_name']['value'],'North Star Ltd')

    def test_spacing_retry_failure_disagreement_and_native_text_preserve_originals(self):
        from PIL import Image
        from services.extraction.parsing.baseline_parser import recover_party_spacing
        from tests.extraction.test_financial_layout import layout
        for problem in ('reader_failure','disagreement','native','no_geometry'):
            with self.subTest(problem=problem):
                doc=layout('INVOICE',[('Seller:',20,100,80,120),('NorthStar Ltd',20,140,230,160)])
                calls=[]
                if problem=='native': doc['m2_text_lines'][2]['from_ocr']=False
                elif problem=='no_geometry': doc['m2_text_lines'][2]['prov']=[]
                def reader(*args,**kwargs):
                    calls.append(1)
                    if problem=='reader_failure': raise RuntimeError('synthetic recognition failure')
                    return SimpleNamespace(txts=['North Star Ltd' if len(calls)==1 else 'NorthStar Ltd'])
                stats=recover_party_spacing(doc,reader,lambda _:Image.new('RGB',(600,900),'white'))
                self.assertEqual(stats['accepted_lines'],0)
                self.assertNotIn('ocr_spacing',doc['m2_text_lines'][2])
                self.assertEqual(doc['m2_text_lines'][2]['text'],'NorthStar Ltd')

    def test_merged_supported_heading_keeps_current_credit_debit_kinds(self):
        for heading, kind in [('CREDIT NOTE', 'credit_note'), ('DEBIT NOTE', 'debit_note')]:
            with self.subTest(kind=kind):
                result = classify_document(document(heading + ' Reference: N1 Currency: MYR'))
                self.assertEqual((result['value'], result['status']), (kind, 'classified'))

    def test_spacing_retry_restores_persistent_reader_flags_even_on_failure(self):
        from PIL import Image
        from services.extraction.parsing.baseline_parser import recover_party_spacing
        from tests.extraction.test_financial_layout import layout
        for fail, classify in ((False, True), (True, True), (False, False)):
            with self.subTest(fail=fail, classify=classify):
                class Reader:
                    use_det, use_cls, use_rec = True, classify, True
                    def __call__(self, image, **flags):
                        for name, value in flags.items(): setattr(self, name, value)
                        if fail: raise RuntimeError('explicit retry failure fixture')
                        return SimpleNamespace(txts=['North Star Ltd'])
                reader = Reader()
                doc = layout('INVOICE', [('Seller:',20,100,80,120),
                    ('NorthStar Ltd',20,140,230,160)])
                stats = recover_party_spacing(doc, reader,
                    lambda _: Image.new('RGB', (600,900), 'white'))
                self.assertEqual((reader.use_det, reader.use_cls, reader.use_rec), (True,classify,True))
                self.assertEqual(stats['accepted_lines'], 0 if fail else 1)

    def test_merged_unsupported_heading_and_conflicting_headings_stay_unresolved(self):
        result = classify_document(document('PRO-FORMA INVOICE Invoice Number: A1 Currency: MYR'))
        self.assertEqual((result['value'], result['status']), ('unknown', 'unsupported'))
        result = classify_document(document('INVOICE\nRECEIPT\nInvoice Number: A1 Currency: MYR'))
        self.assertEqual((result['value'], result['status']), ('unknown', 'ambiguous'))

    def test_missing_offline_ocr_weights_fail_without_downloading(self):
        from services.extraction.workflow.runtime import rapidocr_options
        with TemporaryDirectory() as folder, patch.dict('os.environ', {'RAPIDOCR_MODELS_DIR': folder}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'OCR_MODEL_MISSING'):
                rapidocr_options()

    def test_multilingual_headings_and_attached_reference(self):
        for heading, kind in [('RESIT', 'receipt'), ('INVOIS CUKAI', 'invoice'),
                              ('Fatura Simplificada FS/123', 'invoice'), ('BIL ELEKTRIK', 'bill')]:
            with self.subTest(heading=heading):
                self.assertEqual(classify_document(document(heading))['value'], kind)

    def test_title_free_retail_requires_three_signals(self):
        self.assertEqual(classify_document(document('TOTAL 9.00', 'CASH 10.00', 'CHANGE 1.00'))['value'], 'receipt')
        for lines in [('TOTAL 9.00',), ('TOTAL 9.00', 'CASH 10.00'), ('Invoice number: A1', 'Bill to: Person')]:
            self.assertEqual(classify_document(document(*lines))['value'], 'unknown')

    def test_layout_amount_date_currency_and_evidence_survive(self):
        result = map_document(document('RESIT', 'Merchant ACME SDN BHD', 'Tarikh 25/12/2018', 'TOTAL RM 9.00'))
        self.assertEqual(result['fields']['total_amount']['value'], '9.00')
        self.assertEqual(result['fields']['currency']['value'], 'MYR')
        self.assertEqual(result['fields']['transaction_date']['value'], '2018-12-25')
        self.assertEqual(result['fields']['merchant']['evidence'][0]['source_ref'], '#/texts/1')

    def test_conflicting_totals_are_never_arbitrarily_selected(self):
        result = map_document(document('RESIT', 'TOTAL 9.00', 'TOTAL 10.00'))
        self.assertIsNone(result['fields']['total_amount']['value'])
        self.assertEqual(result['fields']['total_amount']['mapping_status'], 'ambiguous')

    def test_separate_currency_region_cannot_conflict_with_amount(self):
        doc = document('RESIT', 'TOTAL', 'RM', '399.40', 'Payment Method')
        boxes = [(10, 10, 120, 30), (10, 100, 120, 120), (300, 100, 330, 120),
                 (350, 100, 430, 120), (10, 135, 180, 155)]
        for item, (left, top, right, bottom) in zip(doc['texts'], boxes):
            item['prov'][0]['bbox'].update(l=left, t=top, r=right, b=bottom)
        result = map_document(doc)
        self.assertEqual(result['fields']['total_amount']['value'], '399.40')
        self.assertEqual(result['fields']['currency']['value'], 'MYR')

    def test_locale_normalization_and_ambiguous_dates(self):
        self.assertEqual(normalized_amount('1.234,50 €', 'EUR'), '1234.50')
        self.assertEqual(normalized_amount('Rp 91.000', 'IDR'), '91000')
        with self.assertRaises(ValueError): normalized_date('03/04/2026')
        with self.assertRaises(ValueError): normalized_amount('1.234', None)

    def test_one_vat_rate_row_is_not_document_total_tax(self):
        result = map_document(document('BILL', 'IVA 0.37', 'IVA 0.74'))
        self.assertIsNone(result['fields']['tax_amount']['value'])

    def test_external_stored_pdf_is_staged_without_removing_root_guard(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as storage:
            source = Path(storage) / 'stored.pdf'
            source.write_bytes(b'%PDF-stored-test')
            seen = []
            def processing(**kwargs):
                path = kwargs['file_path']
                self.assertTrue(path.is_relative_to(Path(root)))
                self.assertEqual(path.read_bytes(), source.read_bytes())
                seen.append(path)
                return Path(root) / 'summary.json', {'service_run_id': 'test'}
            public = {'document_id': 'doc', 'schema_version': '0.1.0-proposed', 'mode': 'module2_public_result',
                      'review_required': True, 'accounting_approval_performed': False,
                      'safety': {'ai_candidate_auto_accepted': False, 'automatic_accounting_approval': False}}
            with patch('services.extraction.workflow.execution.DocumentIntelligenceService') as service, \
                 patch('services.extraction.workflow.execution.build_public_result', return_value=(Path(root) / 'public.json', public)):
                service.return_value.project_root = Path(root)
                service.return_value.process_document.side_effect = processing
                result = execute_document_extraction('doc', source, project_root=Path(root), allow_vlm_inference=False)
            self.assertEqual(result['execution_status'], 'EXTRACTED')
            self.assertTrue(source.exists())
            self.assertFalse(seen[0].exists())


class ConverterReuseTests(unittest.TestCase):
    """Resource reuse tests use explicit factories; no model inference is mocked
    in the separate real-document latency/semantic regression benchmark.
    """
    def setUp(self):
        import threading
        from services.extraction.parsing import baseline_parser as parser
        self.parser = parser
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.asset = self.root / 'model.onnx'; self.asset.write_bytes(b'local-unit-fixture')
        self.configuration = ['config-a']
        self.options = SimpleNamespace(artifacts_path=self.root,
            ocr_options=SimpleNamespace(det_model_path=self.asset,
                cls_model_path=self.asset, rec_model_path=self.asset),
            model_dump_json=lambda: self.configuration[0])
        self.factory_calls = []
        def factory(**kwargs):
            result = SimpleNamespace(options=kwargs)
            self.factory_calls.append(result); return result
        self.factory = factory
        self.guard = patch.object(parser, '_converter_cache', threading.local())
        self.guard.start(); self.addCleanup(self.guard.stop)

    def get(self):
        return self.parser._get_converter(self.options, self.factory,
            SimpleNamespace(PDF='pdf'), lambda **kwargs: kwargs)

    def test_identical_resources_reused_without_document_cache(self):
        a, first = self.get(); b, second = self.get()
        self.assertIs(a, b); self.assertFalse(first); self.assertTrue(second)
        self.assertEqual(len(self.factory_calls), 1)
        self.assertNotIn('document', self.parser._converter_cache.__dict__)

    def test_options_change_replaces_single_cached_converter(self):
        a, _ = self.get(); self.configuration[0] = 'config-b'; b, reused = self.get()
        self.assertIsNot(a, b); self.assertFalse(reused)
        self.assertIs(self.parser._converter_cache.converter, b)

    def test_model_metadata_change_invalidates_cache(self):
        import os
        a, _ = self.get(); stat = self.asset.stat()
        os.utime(self.asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        b, reused = self.get(); self.assertIsNot(a, b); self.assertFalse(reused)

    def test_fork_pid_change_invalidates_cache(self):
        a, _ = self.get()
        with patch.object(self.parser.os, 'getpid', return_value=-1):
            b, reused = self.get()
        self.assertIsNot(a, b); self.assertFalse(reused)

    def test_threads_do_not_share_converter(self):
        import threading
        a, _ = self.get(); result = []
        thread = threading.Thread(target=lambda: result.append(self.get()[0]))
        thread.start(); thread.join()
        self.assertIsNot(a, result[0]); self.assertIs(self.get()[0], a)

    def test_constructor_failure_is_not_cached(self):
        calls = []
        def factory(**kwargs):
            calls.append(1)
            if len(calls) == 1: raise RuntimeError('explicit fixture failure')
            return object()
        self.factory = factory
        with self.assertRaises(RuntimeError): self.get()
        self.assertNotIn('key', self.parser._converter_cache.__dict__)
        self.assertFalse(self.get()[1]); self.assertEqual(len(calls), 2)

    def test_conversion_failure_invalidates_model_session(self):
        source = self.root / 'input.pdf'; source.write_bytes(b'%PDF-fixture')
        self.get()
        converter = SimpleNamespace(convert=lambda _: (_ for _ in ()).throw(RuntimeError('fixture failure')))
        with patch.dict('os.environ', {'RAPIDOCR_MODELS_DIR': str(self.root)}), \
             patch('services.extraction.workflow.runtime.rapidocr_options', return_value={}), \
             patch.object(self.parser, '_get_converter', return_value=(converter, True)):
            with self.assertRaises(RuntimeError):
                self.parser.parse_document(source, self.root / 'runs', self.root, self.root)
        self.assertEqual(self.parser._converter_cache.__dict__, {})

    def test_profile_signature_preserves_values_evidence_and_guards(self):
        from ai.evaluation.benchmark import semantic_prediction
        from copy import deepcopy
        native = {'document_id': 'local', 'fields': {'total': {'value': '0',
            'evidence': [{'source_ref': '#/texts/1', 'bbox': {'l': 4}}]}},
            'review_required': True, 'provenance': {'run_id': 'a'}}
        same = deepcopy(native); same['provenance']['run_id'] = 'b'
        self.assertEqual(semantic_prediction(native), semantic_prediction(same))
        for key in ('value', 'evidence'):
            changed = deepcopy(native); changed['fields']['total'][key] = None
            self.assertNotEqual(semantic_prediction(native), semantic_prediction(changed))
        changed = deepcopy(native); changed['review_required'] = False
        self.assertNotEqual(semantic_prediction(native), semantic_prediction(changed))


if __name__ == '__main__':
    unittest.main()
