"""Turn a source file into positioned text rows, one list per page.

Digital PDF pages are read from their own text layer (no model, no OCR error).
Scanned pages, photos and images go through RapidOCR on the CPU in this process.
Nothing here contacts a network service.

Every row is {'order', 'page', 'text', 'box_polygon_pixels', 'engine_score',
'from_ocr'}. Coordinates are pixels with a top-left origin, in the page image the
row was read from: the prepared photo for images, or the page at RENDER_SCALE for
PDFs (text-layer boxes are converted to that same frame so both sources overlay
the same rendering).
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import io
import math
import os
import threading
import time
from pathlib import Path

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}
PDF_SUFFIXES = {'.pdf'}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES
RENDER_SCALE = 2.0  # 144 DPI: enough for 8 pt print without a huge bitmap.
# A page is trusted to its text layer only when the layer is substantial and the
# page is not mostly one picture (a scan with a hidden, often poor, OCR layer).
MIN_TEXT_LAYER_CHARACTERS = 40
MAX_TEXT_LAYER_IMAGE_FRACTION = 0.5

_engine = None
_engine_lock = threading.Lock()


class PageError(ValueError):
    """Controlled failure code; never carries document text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _int_env(name, default, low, high):
    text = os.getenv(name, str(default)).strip()
    if not text.isdecimal() or not low <= int(text) <= high:
        raise PageError('INVALID_LITE_CONFIGURATION')
    return int(text)


def _package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'


def engine_versions():
    return {name: _package_version(name) for name in ('rapidocr', 'onnxruntime', 'pypdfium2', 'pillow')}


def _model_paths():
    """Explicit local weights when a models directory is configured, else RapidOCR's own."""
    root = os.getenv('RAPIDOCR_MODELS_DIR') or os.getenv('IMAGE_OCR_MODELS_DIR')
    if not root:
        return {}
    root = Path(root).expanduser().resolve()
    names = {'Det.model_path': os.getenv('RAPIDOCR_DET_MODEL', 'PP-OCRv6_det_small.onnx'),
             'Rec.model_path': os.getenv('RAPIDOCR_REC_MODEL', 'PP-OCRv6_rec_small.onnx')}
    paths = {}
    for key, name in names.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size < 1024:
            raise PageError('OCR_MODEL_MISSING')
        paths[key] = str(path)
    return paths


def _get_engine():
    """One OCR engine per process, created on first use so the API starts light."""
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR
        threads = _int_env('OCR_THREADS', 2, 1, 16)
        params = {'Global.use_cls': False,
                  'EngineConfig.onnxruntime.use_cuda': False,
                  'EngineConfig.onnxruntime.use_dml': False,
                  'EngineConfig.onnxruntime.intra_op_num_threads': threads,
                  'EngineConfig.onnxruntime.inter_op_num_threads': 1}
        params.update(_model_paths())
        _engine = RapidOCR(params=params)
    return _engine


def warm_engine():
    """Load the OCR weights now (optional) so the first upload does not pay for it."""
    with _engine_lock:
        started = time.perf_counter()
        _get_engine()
        return round(time.perf_counter() - started, 3)


def ocr_image(image):
    """OCR one prepared RGB PIL image. Returns (rows without order/page, seconds).

    Calls are serialised: OCR is CPU-bound and one inference at a time bounds
    memory on a small host.
    """
    width, height = image.size
    with _engine_lock:
        engine = _get_engine()
        started = time.perf_counter()
        result = engine(image)  # RapidOCR performs its documented PIL RGB -> BGR conversion.
        seconds = round(time.perf_counter() - started, 3)
    rows = []
    if getattr(result, 'boxes', None) is None:
        return rows, seconds
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        text = ' '.join(str(text).split())
        if not text:
            continue
        polygon = [[min(max(float(x), 0.0), float(width)), min(max(float(y), 0.0), float(height))]
                   for x, y in box.tolist()]
        xs, ys = zip(*polygon)
        if len(polygon) != 4 or min(xs) >= max(xs) or min(ys) >= max(ys):
            continue
        score = float(score)
        if not math.isfinite(score):
            continue
        rows.append({'text': text[:2000], 'box_polygon_pixels': polygon,
                     'engine_score': min(max(score, 0.0), 1.0), 'from_ocr': True})
    return rows, seconds


def prepare_image(image):
    """Upright RGB at an OCR-friendly size. Returns (image, transform record).

    Phone photos carry an EXIF orientation; it is applied here so text is upright.
    Very large photos are reduced and very small receipts enlarged, both uniformly,
    so evidence boxes keep the source aspect ratio.
    """
    from PIL import Image, ImageOps
    orientation = int(image.getexif().get(274, 1) or 1)
    upright = ImageOps.exif_transpose(image)
    if 'A' in upright.getbands() or upright.info.get('transparency') is not None:
        rgba = upright.convert('RGBA')
        flat = Image.new('RGB', rgba.size, 'white')
        flat.paste(rgba, mask=rgba.getchannel('A'))
        upright = flat
    else:
        upright = upright.convert('RGB')
    original = upright.size
    limit = _int_env('LITE_OCR_MAX_SIDE', 1280, 800, 4096)
    scale = 1.0
    if max(original) > limit:
        scale = limit / max(original)
    elif min(original) < 600:
        scale = min(3.0, math.ceil(800 / min(original)), limit / max(original))
        scale = max(scale, 1.0)
    if scale != 1.0:
        size = (max(1, round(original[0] * scale)), max(1, round(original[1] * scale)))
        upright = upright.resize(size, Image.Resampling.LANCZOS)
    return upright, {'exif_orientation': orientation, 'oriented_size': list(original),
                     'page_size': list(upright.size), 'scale': round(scale, 6)}


def _finish_page(number, source, size, rows, seconds, transform):
    return {'page': number, 'source': source, 'size': {'width': int(size[0]), 'height': int(size[1])},
            'coordinate_units': 'pixels', 'rows': rows, 'seconds': seconds, 'transform': transform}


def _image_pages(data, max_pages, max_pixels):
    from PIL import Image, ImageSequence, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = max_pixels
    pages = []
    truncated = False
    try:
        with Image.open(io.BytesIO(data)) as image:
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                if index >= max_pages:
                    truncated = True
                    break
                if frame.width * frame.height > max_pixels:
                    raise PageError('IMAGE_PIXEL_LIMIT')
                prepared, transform = prepare_image(frame)
                try:
                    rows, seconds = ocr_image(prepared)
                    pages.append(_finish_page(index + 1, 'ocr', prepared.size, rows, seconds, transform))
                finally:
                    prepared.close()
    except PageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError) as exc:
        raise PageError('INVALID_OR_UNSAFE_IMAGE') from exc
    return pages, truncated


def _largest_image_fraction(page, area):
    largest = 0.0
    for obj in page.get_objects():
        if obj.type == 3:  # FPDF_PAGEOBJ_IMAGE
            left, bottom, right, top = obj.get_bounds()
            largest = max(largest, max(0.0, right - left) * max(0.0, top - bottom) / area)
    return largest


def _text_layer_rows(textpage, width, height):
    """PDF text rectangles as rows, converted to the top-left RENDER_SCALE pixel frame."""
    rows = []
    for index in range(textpage.count_rects()):
        left, bottom, right, top = textpage.get_rect(index)
        text = ' '.join(textpage.get_text_bounded(left=left, bottom=bottom, right=right, top=top).split())
        if not text:
            continue
        x0, x1 = max(0.0, left) * RENDER_SCALE, min(width, right) * RENDER_SCALE
        y0, y1 = max(0.0, height - top) * RENDER_SCALE, min(height, height - bottom) * RENDER_SCALE
        if x0 >= x1 or y0 >= y1:
            continue
        rows.append({'text': text[:2000],
                     'box_polygon_pixels': [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                     'engine_score': 1.0, 'from_ocr': False})
    return rows


def _pdf_pages(data, max_pages, max_pixels):
    import pypdfium2 as pdfium
    pages = []
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as exc:
        raise PageError('INVALID_OR_UNREADABLE_PDF') from exc
    try:
        count = len(pdf)
        if count < 1:
            raise PageError('INVALID_OR_UNREADABLE_PDF')
        for index in range(min(count, max_pages)):
            page = pdf[index]
            try:
                width, height = page.get_size()
                if width <= 0 or height <= 0:
                    raise PageError('INVALID_OR_UNREADABLE_PDF')
                size = (math.ceil(width * RENDER_SCALE), math.ceil(height * RENDER_SCALE))
                started = time.perf_counter()
                textpage = page.get_textpage()
                try:
                    characters = len(textpage.get_text_range().strip())
                    native = (characters >= MIN_TEXT_LAYER_CHARACTERS
                              and _largest_image_fraction(page, width * height) < MAX_TEXT_LAYER_IMAGE_FRACTION)
                    rows = _text_layer_rows(textpage, width, height) if native else []
                finally:
                    textpage.close()
                if native and rows:
                    pages.append(_finish_page(index + 1, 'text_layer', size, rows,
                                              round(time.perf_counter() - started, 3),
                                              {'render_scale': RENDER_SCALE, 'pdf_size_points': [width, height]}))
                    continue
                if size[0] * size[1] > max_pixels:
                    raise PageError('IMAGE_PIXEL_LIMIT')
                bitmap = page.render(scale=RENDER_SCALE)
                try:
                    rendered = bitmap.to_pil().convert('RGB')
                finally:
                    bitmap.close()
                try:
                    rows, seconds = ocr_image(rendered)
                    pages.append(_finish_page(index + 1, 'ocr', rendered.size, rows, seconds,
                                              {'render_scale': RENDER_SCALE, 'pdf_size_points': [width, height]}))
                finally:
                    rendered.close()
            finally:
                page.close()
        return pages, count > max_pages
    finally:
        pdf.close()


def _reading_order(rows):
    """Top-to-bottom, then left-to-right, tolerant of small vertical jitter in a line."""
    def key(row):
        xs, ys = zip(*row['box_polygon_pixels'])
        return (round((min(ys) + max(ys)) / 2 / 8), min(xs))
    return sorted(rows, key=key)


def load_pages(path):
    """Read every page of a supported file into ordered rows.

    Returns {'source_sha256', 'source_format', 'page_count', 'truncated', 'pages',
    'rows', 'versions', 'seconds'} where 'rows' is the flat, globally ordered list.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise PageError('UNSUPPORTED_EXTRACTION_FORMAT')
    max_bytes = _int_env('IMAGE_OCR_MAX_BYTES', 20 * 1024 * 1024, 1, 20 * 1024 * 1024)
    max_pixels = _int_env('IMAGE_OCR_MAX_PIXELS', 20_000_000, 1, 40_000_000)
    max_pages = _int_env('LITE_MAX_PAGES', 10, 1, 50)
    with path.open('rb') as handle:
        data = handle.read(max_bytes + 1)
    if not 0 < len(data) <= max_bytes:
        raise PageError('SOURCE_SIZE_LIMIT')
    started = time.perf_counter()
    if suffix in PDF_SUFFIXES:
        if not data.lstrip()[:5].startswith(b'%PDF-'):
            raise PageError('SOURCE_FORMAT_MISMATCH')
        pages, truncated = _pdf_pages(data, max_pages, max_pixels)
    else:
        pages, truncated = _image_pages(data, max_pages, max_pixels)
    flat = []
    for page in pages:
        ordered = _reading_order(page['rows'])
        for row in ordered:
            row['page'] = page['page']
            row['order'] = len(flat)
            flat.append(row)
        page['rows'] = ordered
    if len(flat) > 4000:
        raise PageError('OCR_RESULT_LIMIT')
    return {'source_sha256': hashlib.sha256(data).hexdigest(), 'source_format': suffix,
            'page_count': len(pages), 'truncated': truncated, 'pages': pages, 'rows': flat,
            'versions': engine_versions(), 'seconds': round(time.perf_counter() - started, 3)}


def page_lines(page):
    """Group one page's rows into visual lines (lists of rows, left to right)."""
    lines = []
    for row in sorted(page['rows'], key=lambda r: (min(p[1] for p in r['box_polygon_pixels']),
                                                   min(p[0] for p in r['box_polygon_pixels']))):
        ys = [p[1] for p in row['box_polygon_pixels']]
        top, bottom = min(ys), max(ys)
        centre, height = (top + bottom) / 2, bottom - top
        for line in lines:
            if abs(line['centre'] - centre) <= 0.5 * min(line['height'], height):
                line['rows'].append(row)
                line['centre'] = sum((min(p[1] for p in r['box_polygon_pixels'])
                                      + max(p[1] for p in r['box_polygon_pixels'])) / 2
                                     for r in line['rows']) / len(line['rows'])
                line['height'] = max(line['height'], height)
                break
        else:
            lines.append({'centre': centre, 'height': height, 'rows': [row]})
    lines.sort(key=lambda line: line['centre'])
    return [sorted(line['rows'], key=lambda r: min(p[0] for p in r['box_polygon_pixels'])) for line in lines]


def layout_text(document):
    """Plain text that keeps the page's line and column structure for a text model.

    Returns (text, lines) where lines[i] is the list of rows printed on text line i
    (page headers have an empty list). Wide gaps on a line become ' | ' so labels
    and their values, or table columns, stay distinguishable.
    """
    text_lines, line_rows = [], []
    for page in document['pages']:
        if len(document['pages']) > 1:
            text_lines.append(f"=== Page {page['page']} ===")
            line_rows.append([])
        for rows in page_lines(page):
            parts = []
            previous_right = None
            for row in rows:
                xs = [p[0] for p in row['box_polygon_pixels']]
                left, right = min(xs), max(xs)
                character = (right - left) / max(1, len(row['text']))
                if previous_right is not None:
                    parts.append(' | ' if left - previous_right > 2.5 * character else ' ')
                parts.append(row['text'])
                previous_right = right
            text_lines.append(''.join(parts))
            line_rows.append(rows)
    return '\n'.join(text_lines), line_rows
