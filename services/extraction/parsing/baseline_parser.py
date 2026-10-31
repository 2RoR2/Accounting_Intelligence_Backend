"""Local Docling PDF parser used by Module 2 document intelligence.

The public function is parse_document(). The CLI remains available for
manual lab/demo use.

This parser:
- uses already-downloaded Docling models;
- enables local RapidOCR for scanned pages;
- enables table structure;
- disables remote services;
- runs on CPU;
- records source hashes and run metadata;
- writes each conversion to a unique run directory.
"""

import argparse
import hashlib
import json
import os
import time
import threading
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4


# Require already-downloaded models for this workflow.
os.environ["HF_HUB_OFFLINE"] = "1"



PROJECT_ROOT = Path(__file__).resolve().parents[2]


def sha256_file(path: Path) -> str:
    """Return SHA-256 for a file without loading the entire file at once."""
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def checked_project_path(
    path: Path,
    project_root: Path,
    label: str,
) -> Path:
    """Resolve a path and require it to remain inside the project root."""
    resolved = Path(path).expanduser().resolve()
    root = Path(project_root).resolve()

    if not resolved.is_relative_to(root):
        raise ValueError(
            f"{label} must be inside the SSD project: {root}"
        )

    return resolved


def recover_party_spacing(document, reader, page_image):
    """Bounded OCR retry on already mapped party lines; no labels or identity input.

    Keep original text/boxes intact. Store independent native/doubled crop reads
    only when they agree on whitespace insertions without changing characters.
    Other fields and line-item mapping continue to consume original OCR lines.
    """
    import math
    import numpy as np
    from PIL import Image
    from services.extraction.parsing.map_invoice import map_document
    from services.extraction.layout.layout_headers import SPACING_POLICY, verified_spacing
    mapped = map_document(document)
    refs = set()
    for key in ('supplier_name', 'buyer_name', 'supplier_address', 'customer_address'):
        field = mapped['fields'].get(key, {})
        values = (field.get('value') or '').splitlines()
        refs.update(e['source_ref'] for e in field.get('evidence', []) if e.get('text', '').strip() in values)
    lines = [x for x in document.get('m2_text_lines', []) if x.get('self_ref') in refs and x.get('from_ocr') is True]
    stats = {'policy_version': SPACING_POLICY, 'attempted_lines': 0, 'accepted_lines': 0, 'failed_lines': 0}
    if len(lines) > 64:
        return {**stats, 'status': 'region_limit_originals_preserved'}
    images = {}
    # RapidOCR 3.x persists call-time switches on the reader. Recognition-only
    # crops must not disable detection/classification for the next conversion.
    reader_flags = {name: getattr(reader, name) for name in
                    ('use_det', 'use_cls', 'use_rec') if hasattr(reader, name)}
    try:
        for item in lines:
            stats['attempted_lines'] += 1
            try:
                prov = item.get('prov', [])
                if len(prov) != 1:
                    continue
                page = prov[0]['page_no']; b = prov[0]['bbox']; size = document['pages'][str(page)]['size']
                if b['coord_origin'] != 'TOPLEFT' or not (0 <= b['l'] < b['r'] <= size['width'] and 0 <= b['t'] < b['b'] <= size['height']):
                    continue
                if page not in images:
                    images[page] = page_image(page)
                image = images[page]; sx = image.width / size['width']; sy = image.height / size['height']
                crop_box = (max(0, math.floor((b['l']-3)*sx)), max(0, math.floor((b['t']-3)*sy)),
                            min(image.width, math.ceil((b['r']+3)*sx)), min(image.height, math.ceil((b['b']+3)*sy)))
                with image.crop(crop_box) as crop:
                    readings = []
                    for scale in (1, 2):
                        sample = crop if scale == 1 else crop.resize((crop.width*2, crop.height*2), Image.Resampling.LANCZOS)
                        try:
                            result = reader(np.array(sample.convert('RGB'))[:, :, ::-1].copy(),
                                            use_det=False, use_cls=False, use_rec=True)
                            texts = getattr(result, 'txts', None)
                            readings.append(texts[0] if texts is not None and len(texts) == 1 else '')
                        finally:
                            if sample is not crop:
                                sample.close()
                value = verified_spacing(item['text'], readings)
                if value:
                    item['ocr_spacing'] = {'policy_version': SPACING_POLICY, 'value': value,
                                           'readings': readings, 'crop_box_pixels': list(crop_box),
                                           'page_image_size': [image.width, image.height]}
                    stats['accepted_lines'] += 1
            except Exception:
                # Retry failure must not erase/fail the original parser result.
                stats['failed_lines'] += 1
    finally:
        for name, value in reader_flags.items():
            setattr(reader, name, value)
        for image in images.values():
            image.close()
    return {**stats, 'status': 'completed_with_originals_preserved'}


_converter_cache = threading.local()


def _get_converter(options, converter_type, format_type, pdf_option_type):
    """Reuse only model resources, never document results, within one thread/PID.

    Keep one configuration per thread. Changing options, model paths or local
    asset metadata replaces it; a fork must initialize its own model resources.
    Docling owns per-conversion state and its existing initialized-pipeline cache.
    """
    assets = {p for p in Path(options.artifacts_path).rglob("*") if p.is_file()}
    assets.update(Path(getattr(options.ocr_options, name)) for name in (
        "det_model_path", "cls_model_path", "rec_model_path"))
    signature = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                      for p in sorted(assets))
    key = (os.getpid(), converter_type, options.model_dump_json(), signature)
    reused = getattr(_converter_cache, "key", None) == key
    if not reused:
        _converter_cache.__dict__.clear()
        converter = converter_type(format_options={
            format_type.PDF: pdf_option_type(pipeline_options=options)})
        _converter_cache.converter = converter
        _converter_cache.key = key
    return _converter_cache.converter, reused


def parse_document(
    input_pdf: Path,
    output_parent: Path,
    project_root: Path = PROJECT_ROOT,
    models_dir: Path | None = None,
    verbose: bool = False,
):
    """Parse one local PDF with Docling and write immutable run artifacts.

    Returns:
        tuple[Path, dict]:
            output run directory and parser run metadata.

    The generated run directory contains:
        document.json
        document.md
        document.txt
        run.json

    Docling parsing includes a bounded party-region spacing retry using the
    existing mapper to locate source lines. No Qwen, reconciliation or approval.
    """

    root = Path(project_root).resolve()

    source = checked_project_path(
        input_pdf,
        root,
        "Input",
    )

    output_parent = checked_project_path(
        output_parent,
        root,
        "Output",
    )

    if models_dir is not None:
        models = Path(models_dir).expanduser().resolve()
    elif os.getenv("DOCLING_MODELS_DIR"):
        models = Path(os.environ["DOCLING_MODELS_DIR"]).expanduser().resolve()
    else:
        models = (root / "models" / "docling").resolve()

    if not source.is_file():
        raise ValueError(f"PDF not found: {source}")

    if source.suffix.lower() != ".pdf":
        raise ValueError("Input must be a PDF file.")

    if not models.is_dir():
        raise ValueError(
            f"Docling model directory not found: {models}"
        )

    if output_parent.exists() and not output_parent.is_dir():
        raise ValueError(
            f"Output parent is not a directory: {output_parent}"
        )

    output_parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    source_sha256 = sha256_file(source)

    run_id = datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    output = (
        output_parent
        / f"docling-{run_id}-{uuid4().hex[:8]}"
    )

    output.mkdir(
        parents=True,
        exist_ok=False,
    )

    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import (
        ConversionStatus,
        InputFormat,
    )
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import (
        DocumentConverter,
        PdfFormatOption,
    )

    options = PdfPipelineOptions(
        artifacts_path=models
    )

    from docling.datamodel.pipeline_options import RapidOcrOptions
    from services.extraction.workflow.runtime import rapidocr_options

    options.do_ocr = True
    options.ocr_options = RapidOcrOptions(**rapidocr_options())
    options.generate_parsed_pages = True
    options.document_timeout = 180.0
    options.do_table_structure = True
    options.enable_remote_services = False

    options.accelerator_options = AcceleratorOptions(
        device=AcceleratorDevice.CPU,
        num_threads=4,
    )

    record = {
        "mode": "real_parser",
        "source": str(source),
        "source_sha256": source_sha256,
        "output_directory": str(output),
        "docling_version": version("docling"),
        "torch_version": version("torch"),
        "device": "cpu",
        "ocr_enabled": True,
        "ocr_engine": "rapidocr/onnxruntime",
        "rapidocr_version": version("rapidocr"),
        "onnxruntime_version": version("onnxruntime"),
        "timing_scope": (
            "converter acquisition and conversion; excludes imports and exports; "
            "same-thread/PID models may be reused"
        ),
    }

    started = time.perf_counter()

    try:
        converter, record["converter_reused"] = _get_converter(
            options, DocumentConverter, InputFormat, PdfFormatOption)

        result = converter.convert(source)

        record["elapsed_seconds"] = round(
            time.perf_counter() - started,
            3,
        )

        record["status"] = result.status.value
        record["errors"] = [
            str(error)
            for error in result.errors
        ]

        if result.status != ConversionStatus.SUCCESS:
            raise RuntimeError(
                "Conversion did not fully succeed: "
                f"{result.status}"
            )

        document = result.document

        document_json = output / "document.json"
        document_md = output / "document.md"
        document_txt = output / "document.txt"

        exported = document.export_to_dict()
        # Layout/table assembly can merge a total with a payment or adjacent
        # line. Preserve parser line cells as an explicitly named extension of
        # this private artifact; public field evidence points to these exact
        # source lines, not invented sub-boxes of a merged paragraph.
        lines = []
        for page_index, page in enumerate(result.pages, start=1):
            parsed = page.parsed_page
            if parsed is None:
                continue
            for cell in parsed.textline_cells:
                text = cell.text.strip()
                if not text:
                    continue
                lines.append({'self_ref': f'#/m2_text_lines/{len(lines)}', 'text': text,
                              'label': 'text', 'content_layer': 'body', 'from_ocr': cell.from_ocr,
                              'ocr_score': cell.confidence if cell.from_ocr else None,
                              'prov': [{'page_no': page_index,
                                        'bbox': cell.rect.to_bounding_box().model_dump(mode='json')}]})
        exported['m2_text_lines'] = lines
        record['retained_text_lines'] = len(lines)
        readers = [getattr(getattr(pipeline, 'ocr_model', None), 'reader', None)
                   for pipeline in getattr(converter, 'initialized_pipelines', {}).values()]
        readers = [reader for reader in readers if reader is not None]
        if len(readers) == 1 and any(line['from_ocr'] for line in lines):
            try:
                import pypdfium2
                with pypdfium2.PdfDocument(source) as pdf:
                    def page_image(number):
                        page = pdf[number-1]
                        try:
                            bitmap = page.render(scale=2)
                            try:
                                return bitmap.to_pil().copy()
                            finally:
                                bitmap.close()
                        finally:
                            page.close()
                    record['party_spacing'] = recover_party_spacing(exported, readers[0], page_image)
            except Exception:
                record['party_spacing'] = {'status': 'retry_unavailable_originals_preserved'}
        document_json.write_text(
            json.dumps(
                exported,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        document_md.write_text(
            document.export_to_markdown(),
            encoding="utf-8",
        )

        # Preserve the existing workflow's text-export behaviour for now.
        # Docling currently warns that strict_text is deprecated.
        document_txt.write_text(
            document.export_to_markdown(
                strict_text=True
            ),
            encoding="utf-8",
        )

        if verbose:
            print("Conversion completed.")
            print(
                "Elapsed seconds:",
                record["elapsed_seconds"],
            )
            print(
                "Saved results:",
                output,
            )

        return output, record

    except Exception as error:
        # A failed conversion/export must not leave a questionable model session
        # available to the next document. The original error/review path remains.
        _converter_cache.__dict__.clear()
        record["error"] = str(error)

        record.setdefault(
            "elapsed_seconds",
            round(
                time.perf_counter() - started,
                3,
            ),
        )

        if verbose:
            print(
                "Run details saved in:",
                output,
            )

        raise

    finally:
        (output / "run.json").write_text(
            json.dumps(
                record,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Parse a synthetic digital PDF using local Docling models."
        )
    )

    parser.add_argument(
        "input_pdf",
        nargs="?",
        type=Path,
        default=(
            PROJECT_ROOT
            / "data"
            / "development"
            / "synthetic_invoice_001.pdf"
        ),
        help=(
            "Input PDF; defaults to the original synthetic invoice."
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "runs",
        help=(
            "Parent folder for a new, unique Docling run folder."
        ),
    )

    parser.add_argument(
        "--models-dir",
        type=Path,
        default=None,
        help="Custom local directory containing Docling models.",
    )

    args = parser.parse_args()

    try:
        parse_document(
            input_pdf=args.input_pdf,
            output_parent=args.output_dir,
            project_root=PROJECT_ROOT,
            models_dir=args.models_dir,
            verbose=True,
        )

    except (
        OSError,
        ValueError,
        RuntimeError,
    ) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
