import os
import json
import time
import argparse
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from importlib.metadata import version
from uuid import uuid4

# Require already-downloaded models for this test.
os.environ["HF_HUB_OFFLINE"] = "1"

from docling.datamodel.base_models import InputFormat, ConversionStatus
from docling.datamodel.accelerator_options import (
    AcceleratorDevice, AcceleratorOptions,
)
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

root = Path(__file__).resolve().parents[2]

parser = argparse.ArgumentParser(
    description="Parse a synthetic digital PDF using local Docling models."
)
parser.add_argument(
    "input_pdf",
    nargs="?",
    type=Path,
    default=root / "data/development/synthetic_invoice_001.pdf",
    help="Input PDF; defaults to the original synthetic invoice.",
)
parser.add_argument(
    "--output-dir",
    type=Path,
    default=root / "runs",
    help="Parent folder for a new, unique Docling run folder.",
)
args = parser.parse_args()

source = args.input_pdf.expanduser().resolve()
output_parent = args.output_dir.expanduser().resolve()
models = root / "models/docling"

# Keep this lab workflow's input and output inside the SSD project.
for label, path in (("Input", source), ("Output", output_parent)):
    if not path.is_relative_to(root):
        parser.error(f"{label} must be inside the SSD project: {root}")

if not source.is_file():
    parser.error(f"PDF not found: {source}")
if source.suffix.lower() != ".pdf":
    parser.error("Input must be a PDF file.")
if not models.is_dir():
    parser.error(f"Docling model directory not found: {models}")
if output_parent.exists() and not output_parent.is_dir():
    parser.error(f"Output parent is not a directory: {output_parent}")

source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
output = output_parent / f"docling-{run_id}-{uuid4().hex[:8]}"
output.mkdir(parents=True, exist_ok=False)
options = PdfPipelineOptions(artifacts_path=models)
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
    "timing_scope": "converter creation and first conversion; excludes exports",
}

started = time.perf_counter()
try:
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options)
        }
    )
    result = converter.convert(source)
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    record["status"] = result.status.value
    record["errors"] = [str(error) for error in result.errors]

    if result.status != ConversionStatus.SUCCESS:
        raise RuntimeError(f"Conversion did not fully succeed: {result.status}")

    document = result.document
    (output / "document.json").write_text(
        json.dumps(document.export_to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (output / "document.md").write_text(
        document.export_to_markdown(), encoding="utf-8"
    )
    (output / "document.txt").write_text(
        document.export_to_markdown(strict_text=True), encoding="utf-8"
    )
    print("Conversion completed.")
    print("Elapsed seconds:", record["elapsed_seconds"])
    print("Saved results:", output)

except Exception as error:
    record["error"] = str(error)
    record.setdefault("elapsed_seconds", round(time.perf_counter() - started, 3))
    print("Run details saved in:", output)
    raise

finally:
    (output / "run.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )
