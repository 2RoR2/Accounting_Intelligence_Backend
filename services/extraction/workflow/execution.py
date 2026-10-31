"""Module 2 extraction execution boundary.

This module composes DocumentIntelligenceService and build_public_result into
a single, safe execution boundary for internally trusted document processing.

Important:
- Extraction finishes at 'extracted' / review-required state.
- No M3 accounting validation is executed in this boundary.
- No canonical accounting record is created or approved.
- No database persistence or document-status transition is claimed.
- Minimal execution metadata is returned (no raw financial data or extracted PII).
"""

from pathlib import Path
from copy import deepcopy
from io import BytesIO
import hashlib
import json
import math
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, Optional

from services.extraction.workflow.document_extraction_service import DocumentIntelligenceService
from services.extraction.contracts.module2_public_result import build_public_result


def prepare_image_frame(frame):
    """Keep small receipt text legible to OCR, with bounded, uniform scaling.

    This changes only a transient parser input. Original pixels and their hash
    stay in private storage; page geometry retains the same aspect ratio.
    """
    from PIL import Image, ImageOps
    source_size = list(frame.size)
    orientation = int(frame.getexif().get(274, 1))
    if orientation not in range(1, 9):
        raise ValueError('Unsupported EXIF orientation')
    image = ImageOps.exif_transpose(frame).convert('RGB')
    original = image.size
    scale = 1
    if min(original) < 600:
        desired = min(3, math.ceil(800 / min(original)))
        scale = max(1, min(desired, 4096 // max(original), math.isqrt(20_000_000 // (image.width * image.height))))
    if scale > 1:
        enlarged = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
        image.close(); image = enlarged
    return image, {'method': 'lanczos_upscale' if scale > 1 else 'original_size',
                   'source_size': source_size, 'exif_orientation': orientation,
                   'original_size': list(original), 'parser_size': list(image.size), 'scale': scale}


def source_page_geometry(pdf_bytes, document, conversion):
    """Validate actual PDF/Docling page sizes; keep OCR evidence in PDF points."""
    import pypdfium2 as pdfium
    pages = []
    with pdfium.PdfDocument(pdf_bytes) as pdf:
        if set(document.get('pages', {})) != {str(i + 1) for i in range(len(pdf))}:
            raise ValueError('Source/OCR page identity mismatch')
        frames = conversion['frames']
        image_conversion = conversion['operation'] == 'pillow_image_to_pdf'
        if image_conversion and (len(frames) != len(pdf) or conversion['pdf_resolution_dpi'] != 150):
            raise ValueError('Invalid image conversion page count or resolution')
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                width, height = page.get_size()
            finally:
                page.close()
            size = document['pages'][str(index + 1)]['size']
            if not all(math.isclose(actual, float(size[key]), rel_tol=0, abs_tol=.05)
                       for actual, key in ((width, 'width'), (height, 'height'))):
                raise ValueError('Parser/OCR page geometry mismatch')
            entry = {'parser_page': index + 1, 'pdf_size_points': [width, height],
                     'ocr_units': 'pdf_points', 'ocr_origin_rule': 'preserve_bbox_coord_origin',
                     'mapping_origin': 'TOPLEFT',
                     'bottomleft_bbox_normalization': 'top=page_height-t; bottom=page_height-b; left/right unchanged'}
            if image_conversion:
                frame = frames[index]
                if not all(math.isclose(actual, pixel * 72 / 150, rel_tol=0, abs_tol=.05)
                           for actual, pixel in zip((width, height), frame['parser_size'])):
                    raise ValueError('Image/PDF page geometry mismatch')
                scale = frame['scale']
                entry.update(source_frame_index=index, source_orientation=frame['exif_orientation'],
                             pdf_points_to_prepared_pixels=[150 / 72, 0, 0, 150 / 72, 0, 0],
                             prepared_pixels_to_oriented_source_pixels=[1 / scale, 0, 0, 1 / scale, 0, 0])
            pages.append(entry)
    return pages


def bind_source_transformation(metadata, extraction, service_dir, service_run_id):
    """Finalize a conversion observed by this execution, before routing/publication."""
    record = deepcopy(metadata)
    provenance = extraction['provenance']
    parser_run_path = Path(provenance['source_document_json']).with_name('run.json')
    parser_run = json.loads(parser_run_path.read_bytes())
    parser_input = Path(parser_run['source'])
    pdf_bytes = parser_input.read_bytes()
    if (parser_run != provenance['parser_run'] or parser_run['status'] != 'success'
            or hashlib.sha256(pdf_bytes).hexdigest() != record['parser_input']['sha256']
            or parser_run['source_sha256'] != record['parser_input']['sha256']
            or hashlib.sha256(Path(record['original']['path']).read_bytes()).hexdigest() != record['original']['sha256']):
        raise ValueError('Source transformation/parser identity mismatch')
    doc_path = Path(provenance['source_document_json'])
    doc_bytes = doc_path.read_bytes()
    if hashlib.sha256(doc_bytes).hexdigest() != provenance['source_json_sha256']:
        raise ValueError('Source transformation/OCR hash mismatch')
    record['parser_input']['path'] = str(parser_input)
    record['pages'] = source_page_geometry(pdf_bytes, json.loads(doc_bytes), record['conversion'])
    record['binding'] = {'document_id': extraction['document_id'], 'extraction_run_id': extraction['extraction_run_id'],
                         'service_run_id': service_run_id, 'source_document_json': str(doc_path),
                         'source_json_sha256': provenance['source_json_sha256'],
                         'parser_run_path': str(parser_run_path),
                         'parser_run_sha256': hashlib.sha256(parser_run_path.read_bytes()).hexdigest()}
    record_path = service_dir / 'image-preprocessing.private.json'
    with record_path.open('x', encoding='utf-8') as handle:
        json.dump(record, handle, indent=2, ensure_ascii=False, allow_nan=False)
    return {'path': str(record_path), 'sha256': hashlib.sha256(record_path.read_bytes()).hexdigest(),
            'service_run_id': service_run_id}


def verify_source_transformation(extraction, root, *, source_path, source_access_authorized):
    """Integrity is not authorization: a current trusted resolver supplies the original."""
    from services.extraction.ai.run_selective_qwen import checked_path
    if source_access_authorized is not True or source_path is None:
        raise ValueError('An independently authorized original source reference is required')
    provenance = extraction['provenance']; pointer = provenance['source_transformation']
    record_path = checked_path(pointer['path'], root)
    record_bytes = record_path.read_bytes()
    if hashlib.sha256(record_bytes).hexdigest() != pointer['sha256']:
        raise ValueError('Source transformation metadata changed')
    record = json.loads(record_bytes); binding = record['binding']
    if (record['version'] != 'source-transformation/1'
            or binding['document_id'] != extraction['document_id']
            or binding['extraction_run_id'] != extraction['extraction_run_id']
            or binding['service_run_id'] != pointer['service_run_id']
            or record_path.parent.name != binding['service_run_id']
            or binding['source_document_json'] != provenance['source_document_json']
            or binding['source_json_sha256'] != provenance['source_json_sha256']):
        raise ValueError('Source transformation belongs to a different extraction')
    parser_run_path = checked_path(binding['parser_run_path'], root)
    if (hashlib.sha256(parser_run_path.read_bytes()).hexdigest() != binding['parser_run_sha256']
            or json.loads(parser_run_path.read_bytes()) != provenance['parser_run']
            or record['parser_input']['sha256'] != provenance['parser_run']['source_sha256']
            or record['parser_input']['path'] != provenance['parser_run']['source']):
        raise ValueError('Source transformation parser identity changed')
    original_path = Path(source_path).resolve()
    original = original_path.read_bytes()
    if hashlib.sha256(original).hexdigest() != record['original']['sha256']:
        raise ValueError('Authorized original source changed')
    conversion = record['conversion']
    if conversion['operation'] == 'pdf_identity':
        pdf_path, pdf_bytes = original_path, original
        if conversion['frames'] or record['original']['sha256'] != record['parser_input']['sha256']:
            raise ValueError('Invalid PDF identity conversion')
    elif conversion['operation'] == 'pillow_image_to_pdf':
        from PIL import Image, ImageSequence
        frames = []
        with Image.open(BytesIO(original)) as image:
            for frame in ImageSequence.Iterator(image):
                prepared, transform = prepare_image_frame(frame)
                prepared.close(); frames.append(transform)
        if frames != conversion['frames']:
            raise ValueError('Original image conversion parameters changed')
        pdf_path = checked_path(record['parser_input']['path'], root)
        pdf_bytes = pdf_path.read_bytes()
    else:
        raise ValueError('Unsupported source transformation')
    if hashlib.sha256(pdf_bytes).hexdigest() != record['parser_input']['sha256']:
        raise ValueError('Derived parser input changed')
    doc = json.loads(checked_path(provenance['source_document_json'], root).read_bytes())
    if source_page_geometry(pdf_bytes, doc, conversion) != record['pages']:
        raise ValueError('Source page/geometry mapping changed')
    return pdf_path, pdf_bytes


def execute_document_extraction(
    document_id: str,
    file_path: Path,
    project_root: Optional[Path] = None,
    parser: Optional[Callable] = None,
    verbose: bool = False,
    allow_vlm_inference: bool = False,
    models_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute extraction and strict public-result publication for one trusted document.

    Returns minimal execution metadata referencing the locally retained artifact.
    """
    if not isinstance(document_id, str) or not document_id.strip():
        return {
            "execution_status": "STRUCTURAL_FAILURE",
            "document_id": str(document_id) if document_id else "",
            "error": "document_id must be a non-empty string.",
            "public_result_path": None,
            "service_run_id": None,
            "review_required": True,
            "accounting_approval_performed": False,
        }

    path = Path(file_path).resolve()
    if not path.is_file():
        return {
            "execution_status": "INFRASTRUCTURE_ERROR",
            "document_id": document_id,
            "error": f"Source document file not found: {path}",
            "public_result_path": None,
            "service_run_id": None,
            "review_required": True,
            "accounting_approval_performed": False,
        }

    import os
    if parser is None and path.suffix.lower() in {'.jpg', '.jpeg', '.png'} and os.getenv('ENABLE_IMAGE_OCR', '').lower() == 'true':
        from services.extraction.ocr.image_parser import execute_image_extraction
        return execute_image_extraction(document_id, path, project_root)

    service = DocumentIntelligenceService(
        project_root=project_root,
        parser=parser,
        allow_vlm_inference=allow_vlm_inference,
        models_dir=models_dir,
    )

    # 1. Run DocumentIntelligenceService. The baseline parser consumes PDFs,
    # while the public upload API also accepts image documents. Convert images
    # to a temporary PDF at this boundary so JPG/PNG/TIFF uploads follow the
    # same trusted extraction and persistence path without changing the stored
    # original artifact.
    try:
        source_for_parser = path
        preprocessing = []
        original_bytes = path.read_bytes()
        source_metadata = {'version': 'source-transformation/1',
                           'original': {'path': str(path), 'sha256': hashlib.sha256(original_bytes).hexdigest(),
                                        'format': path.suffix.lower()},
                           'conversion': {'operation': 'pdf_identity', 'frames': []}}
        # The baseline parser intentionally rejects inputs outside its project
        # root. Keep the transient converted PDF inside that trusted root.
        temp_parent = service.project_root / ".extraction-tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="accounting-extraction-", dir=temp_parent) as temp_dir:
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
                try:
                    from PIL import Image, ImageSequence, __version__ as pillow_version
                    with Image.open(BytesIO(original_bytes)) as image:
                        frames = []
                        try:
                            for frame in ImageSequence.Iterator(image):
                                if len(frames) >= 50:
                                    raise ValueError("IMAGE_PAGE_LIMIT: maximum 50 frames")
                                if frame.width * frame.height > 20_000_000:
                                    raise ValueError("IMAGE_PIXEL_LIMIT: maximum 20 megapixels per page")
                                prepared, transform = prepare_image_frame(frame)
                                frames.append(prepared); preprocessing.append(transform)
                            source_for_parser = Path(temp_dir) / "source.pdf"
                            frames[0].save(source_for_parser, format="PDF", resolution=150.0,
                                           save_all=True, append_images=frames[1:])
                            source_metadata['conversion'] = {'operation': 'pillow_image_to_pdf',
                                'pillow_version': pillow_version, 'pdf_resolution_dpi': 150,
                                'orientation_operation': 'PIL.ImageOps.exif_transpose', 'color_mode': 'RGB',
                                'frames': preprocessing}
                        finally:
                            for frame in frames:
                                frame.close()
                except Exception as exc:
                    raise RuntimeError(f"Image-to-PDF conversion failed: {type(exc).__name__}: {exc}") from exc
            elif path.suffix.lower() == ".pdf":
                # S3 downloads and configured private storage can be outside the
                # parser root. Stage resolver-authorised bytes; retain the guard.
                source_for_parser = Path(temp_dir) / "source.pdf"
                source_for_parser.write_bytes(original_bytes)
            else:
                raise ValueError("UNSUPPORTED_EXTRACTION_FORMAT: expected PDF, PNG, JPEG or TIFF")

            source_metadata['parser_input'] = {'sha256': hashlib.sha256(source_for_parser.read_bytes()).hexdigest()}
            summary_path, summary = service.process_document(
                document_id=document_id,
                file_path=source_for_parser,
                verbose=verbose,
                allow_vlm_inference=allow_vlm_inference,
                models_dir=models_dir,
                source_metadata=source_metadata,
            )
    except Exception as exc:
        return {
            "execution_status": "INFRASTRUCTURE_ERROR",
            "document_id": document_id,
            "error": f"Extraction service processing failed: {type(exc).__name__}: {exc}",
            "public_result_path": None,
            "service_run_id": None,
            "review_required": True,
            "accounting_approval_performed": False,
        }

    # 2. Build strict public result
    try:
        public_result_path, public_result = build_public_result(
            service_result_path=summary_path,
            project_root=service.project_root,
        )
    except Exception as exc:
        return {
            "execution_status": "STRUCTURAL_FAILURE",
            "document_id": document_id,
            "error": f"Public result generation failed: {type(exc).__name__}: {exc}",
            "public_result_path": None,
            "service_run_id": summary.get("service_run_id"),
            "review_required": True,
            "accounting_approval_performed": False,
        }

    # 3. Verify safety invariants and identity
    try:
        _verify_public_safety_invariants(public_result, document_id)
    except Exception as exc:
        return {
            "execution_status": "STRUCTURAL_FAILURE",
            "document_id": document_id,
            "error": f"Public result safety invariant check failed: {type(exc).__name__}: {exc}",
            "public_result_path": str(public_result_path),
            "service_run_id": public_result.get("provenance", {}).get("service_run_id"),
            "review_required": True,
            "accounting_approval_performed": False,
        }

    # 4. Return minimal execution metadata (no financial fields or raw PII in result)
    return {
        "execution_status": "EXTRACTED",
        "document_id": document_id,
        "extraction_status": public_result.get("status"),
        "public_result_path": str(public_result_path),
        "service_run_id": public_result.get("provenance", {}).get("service_run_id"),
        "review_required": public_result.get("review_required", True),
        "accounting_approval_performed": False,
        "error": None,
    }


def _verify_public_safety_invariants(public_result: Dict[str, Any], expected_doc_id: str) -> None:
    if public_result.get("document_id") != expected_doc_id:
        raise ValueError(
            f"Document ID mismatch: expected {expected_doc_id}, got {public_result.get('document_id')}"
        )
    if public_result.get("schema_version") != "0.1.0-proposed":
        raise ValueError(f"Unexpected schema_version: {public_result.get('schema_version')}")
    if public_result.get("mode") != "module2_public_result":
        raise ValueError(f"Unexpected mode: {public_result.get('mode')}")
    if public_result.get("review_required") is not True:
        raise ValueError("review_required must be True")
    if public_result.get("accounting_approval_performed") is not False:
        raise ValueError("accounting_approval_performed must be False")
    safety = public_result.get("safety", {})
    if safety.get("ai_candidate_auto_accepted") is not False:
        raise ValueError("ai_candidate_auto_accepted must be False")
    if safety.get("automatic_accounting_approval") is not False:
        raise ValueError("automatic_accounting_approval must be False")
