"""Offline image boundary tests. Synthetic child replies are explicit test doubles.

Run ImageBoundaryTests in the lightweight backend Python, ImageDecodeTests in
standalone AI Python. Decoder tests do not execute OCR. Real JPEG acceptance
is a separately authorized one-shot run, never a replay pretending to infer.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zlib

from services.extraction.workflow.execution import execute_document_extraction
from services.extraction.ocr.image_parser import execute_image_extraction
from tests.extraction.test_ocr_receipt import source, VERSIONS


def png(width=2, height=2):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\xff\xff' * width) * height)) + chunk(b'IEND', b''))


class ImageBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / 'models'; self.models.mkdir()
        self.python = self.root / 'ai-env/bin/python'; self.python.parent.mkdir(parents=True)
        self.python.write_text('not executed; subprocess is explicitly mocked')
        self.python.chmod(0o700)
        self.image = self.root / 'input.jpg'; self.image.write_bytes(b'\xff\xd8\xffsynthetic-routing-only')
        self.env = {'ENABLE_IMAGE_OCR': 'true', 'IMAGE_OCR_PYTHON': str(self.python),
                    'IMAGE_OCR_MODELS_DIR': str(self.models)}
        self.guard = patch.dict(os.environ, self.env, clear=True); self.guard.start(); self.addCleanup(self.guard.stop)
        self.calls = []
        self.reply_transform = lambda reply: reply

    def child(self, command, **kwargs):
        self.calls.append((command, kwargs))
        def value(name): return command[command.index(name) + 1]
        artifact = source()
        artifact['image_sha256'] = value('--sha256')
        reply = {'protocol': 'image-ocr/0.1', 'document_id': value('--document-id'),
                 'python_executable': str(self.python), 'python_prefix': str(self.python.parent.parent),
                 'versions': VERSIONS, 'weights': {}, 'inference_seconds': 0.01, 'ocr': artifact}
        reply = self.reply_transform(reply)
        Path(value('--output')).write_text(json.dumps(reply))
        return SimpleNamespace(returncode=0)

    def execute(self, path=None):
        return execute_document_extraction('SYN-IMAGE', path or self.image, project_root=self.root)

    def test_jpeg_routes_through_isolated_child_to_existing_public_contract(self):
        with patch('services.extraction.ocr.image_parser.subprocess.run', side_effect=self.child):
            result = self.execute()
        self.assertEqual(result['execution_status'], 'EXTRACTED')
        public = json.loads(Path(result['public_result_path']).read_text())
        self.assertEqual(public['document_id'], 'SYN-IMAGE')
        self.assertEqual(public['document_type']['value'], 'receipt')
        self.assertEqual(public['fields']['total_amount']['value'], '12.00')
        self.assertEqual(public['provenance']['service_run_id'], result['service_run_id'])
        self.assertTrue(result['review_required'])
        self.assertFalse(result['accounting_approval_performed'])
        self.assertNotIn('fields', result)  # No financial values in queue metadata.

    def test_png_routes_and_never_uses_pdf_parser(self):
        path = self.root / 'input.png'; path.write_bytes(png())
        with patch('services.extraction.ocr.image_parser.subprocess.run', side_effect=self.child), patch(
                'services.extraction.workflow.execution.DocumentIntelligenceService', side_effect=AssertionError('PDF path used')):
            self.assertEqual(self.execute(path)['execution_status'], 'EXTRACTED')

    def test_disabled_by_default_with_no_subprocess_or_run(self):
        os.environ.pop('ENABLE_IMAGE_OCR')
        with patch('services.extraction.ocr.image_parser.subprocess.run') as child:
            # The optional direct adapter is disabled; the public execution
            # boundary retains the newer default Docling image path.
            self.assertEqual(execute_image_extraction('SYN-IMAGE', self.image, self.root)['error_code'], 'IMAGE_OCR_DISABLED')
        child.assert_not_called()
        self.assertFalse((self.root / 'runs').exists())

    def test_missing_ai_configuration_cannot_fall_back_to_backend_python(self):
        os.environ.pop('IMAGE_OCR_PYTHON')
        self.assertEqual(self.execute()['error_code'], 'IMAGE_OCR_ENVIRONMENT_NOT_CONFIGURED')

    def test_rejects_same_python_environment(self):
        os.environ['IMAGE_OCR_PYTHON'] = sys.executable
        self.assertEqual(self.execute()['error_code'], 'IMAGE_OCR_ENVIRONMENT_NOT_CONFIGURED')

    def test_unsupported_type_rejected_before_child(self):
        path = self.root / 'input.gif'; path.write_bytes(b'GIF89a')
        with patch('services.extraction.ocr.image_parser.subprocess.run') as child:
            result = execute_image_extraction('SYN', path, self.root)
        self.assertEqual(result['error_code'], 'INVALID_IMAGE_SOURCE'); child.assert_not_called()

    def test_signature_mismatch_rejected_without_child(self):
        self.image.write_bytes(png())
        with patch('services.extraction.ocr.image_parser.subprocess.run') as child:
            self.assertEqual(self.execute()['error_code'], 'IMAGE_FORMAT_MISMATCH')
        child.assert_not_called()

    def test_encoded_byte_limit_applied_before_child(self):
        os.environ['IMAGE_OCR_MAX_BYTES'] = '2'
        self.assertEqual(self.execute()['error_code'], 'IMAGE_SIZE_LIMIT')

    def test_outside_project_path_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            path = Path(other) / 'outside.jpg'; path.write_bytes(self.image.read_bytes())
            self.assertEqual(execute_image_extraction('SYN', path, self.root)['error_code'], 'INVALID_IMAGE_SOURCE')

    def test_timeout_is_deliberate_failure_without_public_result(self):
        with patch('services.extraction.ocr.image_parser.subprocess.run', side_effect=subprocess.TimeoutExpired(['owned-child'], 1)):
            result = self.execute()
        self.assertEqual(result['error_code'], 'IMAGE_OCR_TIMEOUT')
        self.assertIsNone(result['public_result_path'])
        self.assertTrue(list((self.root / 'runs').glob('*/failure.json')))

    def test_child_environment_has_no_service_credentials_or_python_injection(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'must-not-inherit', 'REDIS_URL': 'must-not-inherit',
                                    'AUTH_SECRET': 'must-not-inherit', 'PYTHONPATH': 'must-not-inherit',
                                    'HTTPS_PROXY': 'must-not-inherit'}), patch(
                'services.extraction.ocr.image_parser.subprocess.run', side_effect=self.child):
            self.execute()
        command, options = self.calls[0]
        self.assertEqual(command[:3], [str(self.python), '-I', '-B'])
        self.assertFalse(options['shell'])
        for key in ['DATABASE_URL', 'REDIS_URL', 'AUTH_SECRET', 'PYTHONPATH', 'HTTPS_PROXY']:
            self.assertNotIn(key, options['env'])
        self.assertEqual(options['env']['HF_HUB_OFFLINE'], '1')
        self.assertEqual(options['timeout'], 60)

    def test_private_payment_observations_do_not_enter_persistable_public_result(self):
        with patch('services.extraction.ocr.image_parser.subprocess.run', side_effect=self.child):
            result = self.execute()
        public = Path(result['public_result_path']).read_text()
        self.assertNotIn('20.00', public)
        self.assertNotIn('cash_tendered', public)
        self.assertNotIn('receipt_observations', public)
        self.assertNotIn('native_ocr_result', public)
        private = json.loads((Path(result['public_result_path']).parent / 'mapping.private.json').read_text())
        self.assertEqual(private['receipt_observations']['cash_tendered']['value'], '20.00')
        self.assertIsNone(private['fields']['merchant']['value'])
        self.assertIsNone(private['receipt_observations']['change']['value'])

    def test_foreign_document_or_source_hash_is_rejected(self):
        for key in ['document_id', 'hash']:
            def alter(reply):
                if key == 'document_id': reply['document_id'] = 'FOREIGN'
                else: reply['ocr']['image_sha256'] = '0' * 64
                return reply
            self.reply_transform = alter
            with patch('services.extraction.ocr.image_parser.subprocess.run', side_effect=self.child):
                self.assertEqual(self.execute()['error_code'], 'IMAGE_OCR_IDENTITY_MISMATCH')

    def test_child_invalid_or_corrupt_image_error_is_not_success(self):
        self.reply_transform = lambda reply: {'protocol': 'image-ocr/0.1', 'error_code': 'INVALID_OR_UNSAFE_IMAGE'}
        with patch('services.extraction.ocr.image_parser.subprocess.run', side_effect=self.child):
            self.assertEqual(self.execute()['error_code'], 'INVALID_OR_UNSAFE_IMAGE')

    def test_empty_ocr_never_replays_a_saved_fixture(self):
        def empty(reply): reply['ocr']['rows'] = []; return reply
        self.reply_transform = empty
        with patch('services.extraction.ocr.image_parser.subprocess.run', side_effect=self.child):
            self.assertEqual(self.execute()['error_code'], 'IMAGE_OCR_NO_TEXT')

    def test_unknown_document_keeps_review_required_without_financial_guesses(self):
        def unknown(reply): reply['ocr']['rows'][4]['text'] = 'OTHER DOCUMENT'; return reply
        self.reply_transform = unknown
        with patch('services.extraction.ocr.image_parser.subprocess.run', side_effect=self.child):
            result = self.execute()
        public = json.loads(Path(result['public_result_path']).read_text())
        self.assertEqual(public['document_type']['value'], 'unknown')
        self.assertEqual(public['fields'], {})
        self.assertEqual(public['status'], 'human_review_required')


@unittest.skipUnless(importlib.util.find_spec('PIL'), 'Decoder tests require standalone AI Python; backend intentionally has no Pillow.')
class ImageDecodeTests(unittest.TestCase):
    """Real Pillow decoding only, in the standalone AI environment; no OCR."""
    def setUp(self):
        from PIL import Image
        # The production decoder runs in a one-shot child. Its safety settings
        # must not leak into other tests sharing this interpreter.
        self.enterContext(patch.object(Image, 'MAX_IMAGE_PIXELS', Image.MAX_IMAGE_PIXELS))
        self.enterContext(warnings.catch_warnings())
        from services.extraction.ocr.rapidocr_runner import _load_image, InvalidImage
        self.load, self.error = _load_image, InvalidImage
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_png_decode(self):
        path = self.root / 'small.png'; path.write_bytes(png())
        image, _ = self.load(path, 1024, 100, 100)
        self.assertEqual(image.size, (2, 2))

    def test_corrupt_jpeg_with_valid_magic_is_rejected(self):
        path = self.root / 'bad.jpg'; path.write_bytes(b'\xff\xd8\xffbad')
        with self.assertRaises(self.error): self.load(path, 1024, 100, 100)

    def test_png_content_with_jpeg_suffix_is_rejected(self):
        path = self.root / 'wrong.jpg'; path.write_bytes(png())
        with self.assertRaises(self.error): self.load(path, 1024, 100, 100)

    def test_pixel_limit_precedes_full_decode(self):
        path = self.root / 'large.png'; path.write_bytes(png(20, 20))
        with self.assertRaises(self.error): self.load(path, 4096, 100, 100)

    def test_dimension_limit(self):
        path = self.root / 'wide.png'; path.write_bytes(png(20, 2))
        with self.assertRaises(self.error): self.load(path, 4096, 1000, 10)


if __name__ == '__main__':
    unittest.main()
