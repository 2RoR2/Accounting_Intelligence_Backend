"""Local Docling PDF parser used by Module 2 document intelligence.

The public function is parse_document(). The CLI remains available for
manual lab/demo use.

This parser:
- uses already-downloaded Docling models;
- disables OCR;
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

    This function performs Docling parsing only. It does not perform field
    mapping, classification, routing, Qwen inference, reconciliation, or
    accounting approval.
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

    options.do_ocr = False
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
        "ocr_enabled": False,
        "timing_scope": (
            "converter creation and first conversion; excludes exports"
        ),
    }

    started = time.perf_counter()

    try:
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=options
                )
            }
        )

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

        document_json.write_text(
            json.dumps(
                document.export_to_dict(),
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
