"""Bounded preprocessing only; no OCR weights, real documents or database."""
import unittest
try:
    from PIL import Image
except ImportError:
    Image=None
from services.extraction.workflow.execution import prepare_image_frame


@unittest.skipIf(Image is None,'Pillow required for image preprocessing')
class SmallImagePreprocessingTests(unittest.TestCase):
    def test_small_image_scales_uniformly_and_preserves_input(self):
        with Image.new('RGB',(280,720),'white') as source:
            before=source.tobytes();prepared,meta=prepare_image_frame(source)
            try:
                self.assertEqual(prepared.size,(840,2160))
                self.assertEqual(meta['scale'],3)
                self.assertEqual(meta['original_size'],[280,720])
                self.assertEqual(source.size,(280,720));self.assertEqual(source.tobytes(),before)
            finally:prepared.close()

    def test_normal_documents_keep_their_dimensions(self):
        with Image.new('RGB',(1200,1600)) as source:
            prepared,meta=prepare_image_frame(source)
            try:self.assertEqual(prepared.size,source.size);self.assertEqual(meta['scale'],1)
            finally:prepared.close()

    def test_long_thin_document_is_not_unboundedly_enlarged(self):
        with Image.new('RGB',(200,5000)) as source:
            prepared,meta=prepare_image_frame(source)
            try:self.assertEqual(prepared.size,source.size);self.assertEqual(meta['scale'],1)
            finally:prepared.close()


if __name__=='__main__':unittest.main()
