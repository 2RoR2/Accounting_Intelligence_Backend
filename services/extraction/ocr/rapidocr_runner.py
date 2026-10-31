"""One-shot offline CPU OCR child. Run only with the configured AI Python (-I).

No application imports, database access, environment loading or model fetching.
The parent bounds lifetime and supplies an owned input snapshot/output path.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import socket
import sys
import time
import warnings

PROTOCOL = 'image-ocr/0.1'
WEIGHTS = {'det': 'PP-OCRv6_det_small.onnx', 'cls': 'ch_ppocr_mobile_v2.0_cls_mobile.onnx',
           'rec': 'PP-OCRv6_rec_small.onnx'}


class InvalidImage(ValueError):
    pass


def _offline():
    def denied(*args, **kwargs):
        raise RuntimeError('OFFLINE_NETWORK_DENIED')
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied


def _load_image(path, max_bytes, max_pixels, max_dimension):
    from PIL import Image, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = max_pixels
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    if path.suffix.lower() not in {'.jpg', '.jpeg', '.png'}:
        raise InvalidImage('UNSUPPORTED_IMAGE_TYPE')
    if not 0 < path.stat().st_size <= max_bytes:
        raise InvalidImage('IMAGE_SIZE_LIMIT')
    try:
        with Image.open(path) as image:
            expected = 'PNG' if path.suffix.lower() == '.png' else 'JPEG'
            if image.format != expected:
                raise InvalidImage('IMAGE_FORMAT_MISMATCH')
            width, height = image.size
            if (not 0 < width <= max_dimension or not 0 < height <= max_dimension
                    or width * height > max_pixels):
                raise InvalidImage('IMAGE_PIXEL_LIMIT')
            if getattr(image, 'n_frames', 1) != 1:
                raise InvalidImage('MULTIFRAME_IMAGE_UNSUPPORTED')
            image.verify()
        with Image.open(path) as image:
            if image.getexif().get(274, 1) != 1:
                raise InvalidImage('ROTATED_EXIF_UNSUPPORTED')
            image.load()
            if 'A' in image.getbands() or image.info.get('transparency') is not None:
                rgba = image.convert('RGBA')
                rgb = Image.new('RGB', image.size, 'white')
                rgb.paste(rgba, mask=rgba.getchannel('A'))
                preprocessing = 'alpha flattened on white; original pixel coordinates'
            else:
                rgb = image.convert('RGB')
                preprocessing = 'RGB; original pixel coordinates; no EXIF rotation'
            return rgb, preprocessing
    except InvalidImage:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise InvalidImage('INVALID_OR_UNSAFE_IMAGE') from exc


def run(args):
    _offline()
    started = time.perf_counter()
    image, preprocessing = _load_image(args.input, args.max_bytes, args.max_pixels, args.max_dimension)
    image_hash = hashlib.sha256(args.input.read_bytes()).hexdigest()
    if image_hash != args.sha256:
        raise InvalidImage('SOURCE_HASH_MISMATCH')
    from rapidocr import RapidOCR
    import onnxruntime
    if 'CPUExecutionProvider' not in onnxruntime.get_available_providers():
        raise RuntimeError('CPU_PROVIDER_UNAVAILABLE')
    paths = {k: args.models / name for k, name in WEIGHTS.items()}
    if not all(p.is_file() for p in paths.values()):
        raise RuntimeError('LOCAL_OCR_WEIGHTS_MISSING')
    params = {f'{key.title()}.model_path': str(path) for key, path in paths.items()}
    params.update({'EngineConfig.onnxruntime.use_cuda': False,
                   'EngineConfig.onnxruntime.use_dml': False,
                   'EngineConfig.onnxruntime.intra_op_num_threads': 4,
                   'EngineConfig.onnxruntime.inter_op_num_threads': 1})
    engine = RapidOCR(params=params)
    before_inference = time.perf_counter()
    result = engine(image)  # RapidOCR performs its documented PIL RGB -> BGR conversion.
    inference_seconds = round(time.perf_counter() - before_inference, 6)
    rows = [] if result.boxes is None else [
        {'order': i, 'text': str(text), 'box_polygon_pixels': box.tolist(), 'engine_score': float(score)}
        for i, (box, text, score) in enumerate(zip(result.boxes, result.txts, result.scores))]
    if len(rows) > 1000 or any(len(r['text']) > 2000 for r in rows):
        raise RuntimeError('OCR_RESULT_LIMIT')
    # Empty OCR is a genuine result, never substituted from a saved fixture.
    return {'protocol': PROTOCOL, 'document_id': args.document_id,
            'python_prefix': sys.prefix, 'python_executable': sys.executable,
            'versions': {name: importlib.metadata.version(name)
                         for name in ('rapidocr', 'onnxruntime', 'numpy', 'pillow')},
            'weights': {key: {'name': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                        for key, p in paths.items()},
            'preprocessing': preprocessing, 'inference_seconds': inference_seconds,
            'total_seconds': round(time.perf_counter() - started, 6),
            'ocr': {'status': 'PASS', 'method': 'direct RapidOCR; not Docling pipeline',
                    'annotation_input': False, 'device': 'cpu', 'rows': rows,
                    'image_sha256': image_hash,
                    'image_size': {'width': image.width, 'height': image.height}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('input', 'output', 'models'):
        parser.add_argument('--' + flag, type=Path, required=True)
    for flag in ('document-id', 'sha256'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--max-bytes', type=int, required=True)
    parser.add_argument('--max-pixels', type=int, required=True)
    parser.add_argument('--max-dimension', type=int, required=True)
    args = parser.parse_args()
    # Only the already-created owned run directory is writable by this child.
    if args.output.exists() or args.input.parent != args.output.parent:
        raise SystemExit('INVALID_OWNED_OUTPUT')
    try:
        result = run(args)
    except InvalidImage as exc:
        result = {'protocol': PROTOCOL, 'error_code': str(exc)}
    except Exception as exc:
        # No image text, paths or provider exception values in normal logs/errors.
        result = {'protocol': PROTOCOL, 'error_code': 'OCR_RUNTIME_FAILURE',
                  'exception_type': type(exc).__name__}
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, allow_nan=False)
    return 0 if 'ocr' in result else 2


if __name__ == '__main__':
    raise SystemExit(main())
