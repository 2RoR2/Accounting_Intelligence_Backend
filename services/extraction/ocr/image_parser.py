"""Opt-in JPEG/PNG execution bridge to a separate local CPU OCR Python.

ENABLE_IMAGE_OCR=true, IMAGE_OCR_PYTHON and IMAGE_OCR_MODELS_DIR are required.
No heavyweight imports in the application environment. No queue/DB operations.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from services.extraction.contracts.accounting_contract import map_m2_result
from services.extraction.ocr.ocr_documents import map_rapidocr_document

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png'}
MAX_RESULT_BYTES = 4 * 1024 * 1024


class ImageExtractionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _limit(name, default, maximum):
    text = os.getenv(name, str(default))
    if not text.isdecimal() or not 1 <= int(text) <= maximum:
        raise ImageExtractionError('INVALID_IMAGE_OCR_CONFIGURATION')
    return int(text)


def _write(path, data):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)


def execute_image_extraction(document_id, file_path, project_root=None):
    """Return the existing extraction metadata boundary; never claim approval.

    The existing worker retains responsibility for tenant/job claim fencing.
    Supplemental payment observations and full OCR remain private local files,
    excluded from the public artifact consumed by database persistence.
    """
    base = {'document_id': document_id, 'public_result_path': None, 'service_run_id': None,
            'review_required': True, 'accounting_approval_performed': False}
    run = None
    try:
        if os.getenv('ENABLE_IMAGE_OCR', '').lower() != 'true':
            raise ImageExtractionError('IMAGE_OCR_DISABLED')
        if os.name != 'posix':
            raise ImageExtractionError('IMAGE_OCR_REQUIRES_LOCAL_POSIX_AI_ENV')
        root = (Path(project_root) if project_root else Path(__file__).resolve().parents[2]).resolve()
        path = Path(file_path).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            raise ImageExtractionError('INVALID_IMAGE_SOURCE')
        python = Path(os.getenv('IMAGE_OCR_PYTHON', ''))
        models = Path(os.getenv('IMAGE_OCR_MODELS_DIR', ''))
        if (not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK)
                or not models.is_absolute() or not models.is_dir()
                or python.parent.parent == Path(sys.prefix)):
            raise ImageExtractionError('IMAGE_OCR_ENVIRONMENT_NOT_CONFIGURED')
        max_bytes = _limit('IMAGE_OCR_MAX_BYTES', 20 * 1024 * 1024, 20 * 1024 * 1024)
        max_pixels = _limit('IMAGE_OCR_MAX_PIXELS', 10_000_000, 20_000_000)
        max_dimension = _limit('IMAGE_OCR_MAX_DIMENSION', 8192, 16384)
        timeout = _limit('IMAGE_OCR_TIMEOUT_SECONDS', 60, 180)
        # Read a bounded immutable snapshot; never hand the child a DB/queue path.
        with path.open('rb') as handle:
            data = handle.read(max_bytes + 1)
        if not 0 < len(data) <= max_bytes:
            raise ImageExtractionError('IMAGE_SIZE_LIMIT')
        correct_magic = (data.startswith(b'\x89PNG\r\n\x1a\n') if path.suffix.lower() == '.png'
                         else data.startswith(b'\xff\xd8\xff'))
        if not correct_magic:
            raise ImageExtractionError('IMAGE_FORMAT_MISMATCH')
        run = root / 'runs' / ('image-ocr-' + uuid4().hex)
        if not run.resolve().is_relative_to(root):
            raise ImageExtractionError('INVALID_IMAGE_OUTPUT_ROOT')
        run.mkdir(parents=True, mode=0o700, exist_ok=False)
        snapshot = run / ('input' + path.suffix.lower())
        with snapshot.open('xb') as handle:
            handle.write(data)
        source_hash = hashlib.sha256(data).hexdigest()
        output = run / 'runner.json'
        script = Path(__file__).with_name('rapidocr_runner.py').resolve()
        command = [str(python), '-I', '-B', str(script), '--input', str(snapshot),
                   '--output', str(output), '--models', str(models), '--document-id', document_id,
                   '--sha256', source_hash, '--max-bytes', str(max_bytes),
                   '--max-pixels', str(max_pixels), '--max-dimension', str(max_dimension)]
        # Allowlist environment: no database/Redis/email/auth credentials, .env,
        # Python path injection, network proxies, or inherited model endpoints.
        environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'HOME': str(run),
                       'HF_HOME': str(run / 'cache'), 'XDG_CACHE_HOME': str(run / 'cache'),
                       'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                       'HF_HUB_DISABLE_TELEMETRY': '1', 'OMP_NUM_THREADS': '4',
                       'OPENBLAS_NUM_THREADS': '4', 'CUDA_VISIBLE_DEVICES': ''}
        with (run / 'child.private.log').open('xb') as log:
            process = subprocess.run(command, shell=False, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, cwd=run,
                                     env=environment, timeout=timeout, check=False)
        if not output.is_file() or not 0 < output.stat().st_size <= MAX_RESULT_BYTES:
            raise ImageExtractionError('IMAGE_OCR_INVALID_RESULT')
        envelope = json.loads(output.read_text(encoding='utf-8'))
        if envelope.get('protocol') != 'image-ocr/0.1':
            raise ImageExtractionError('IMAGE_OCR_PROTOCOL_MISMATCH')
        if process.returncode != 0 or 'error_code' in envelope:
            known = {'UNSUPPORTED_IMAGE_TYPE', 'IMAGE_SIZE_LIMIT', 'IMAGE_FORMAT_MISMATCH',
                     'IMAGE_PIXEL_LIMIT', 'MULTIFRAME_IMAGE_UNSUPPORTED', 'ROTATED_EXIF_UNSUPPORTED',
                     'INVALID_OR_UNSAFE_IMAGE', 'SOURCE_HASH_MISMATCH', 'OCR_RUNTIME_FAILURE'}
            code = envelope.get('error_code')
            raise ImageExtractionError(code if code in known else 'IMAGE_OCR_CHILD_FAILED')
        if (envelope.get('document_id') != document_id
                or envelope.get('python_executable') != str(python)
                or envelope.get('python_prefix') == sys.prefix
                or envelope.get('ocr', {}).get('image_sha256') != source_hash):
            raise ImageExtractionError('IMAGE_OCR_IDENTITY_MISMATCH')
        ocr = envelope['ocr']
        if not ocr.get('rows'):
            raise ImageExtractionError('IMAGE_OCR_NO_TEXT')
        public = map_rapidocr_document(ocr, document_id, envelope['versions'])
        public['provenance'].update({'service_run_id': run.name, 'model_weights': envelope['weights'],
                                     'source_ocr_result_path': str(output),
                                     'source_ocr_result_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                                     'runner_script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
                                     'ocr_seconds': envelope['inference_seconds']})
        _write(run / 'mapping.private.json', public)
        # No new cash/change persistence contract. Retain full raw OCR locally;
        # publish only canonical field evidence and an immutable source pointer.
        public = deepcopy(public)
        public.pop('receipt_observations', None)
        public['provenance'].pop('native_ocr_result', None)
        map_m2_result(public, document_id)
        public_path = run / 'public-result.json'
        _write(public_path, public)
        return {**base, 'execution_status': 'EXTRACTED', 'extraction_status': public['status'],
                'public_result_path': str(public_path), 'service_run_id': run.name, 'error': None}
    except subprocess.TimeoutExpired:
        code = 'IMAGE_OCR_TIMEOUT'
    except ImageExtractionError as exc:
        code = exc.code
    except Exception:
        code = 'IMAGE_OCR_FAILED'
    if run:
        _write(run / 'failure.json', {'error_code': code, 'review_required': True})
    return {**base, 'execution_status': 'STRUCTURAL_FAILURE' if code in {
                'IMAGE_OCR_DISABLED', 'INVALID_IMAGE_SOURCE', 'IMAGE_SIZE_LIMIT',
                'IMAGE_FORMAT_MISMATCH', 'IMAGE_PIXEL_LIMIT', 'MULTIFRAME_IMAGE_UNSUPPORTED',
                'ROTATED_EXIF_UNSUPPORTED', 'INVALID_OR_UNSAFE_IMAGE', 'IMAGE_OCR_NO_TEXT'}
            else 'INFRASTRUCTURE_ERROR', 'error': code, 'error_code': code}
