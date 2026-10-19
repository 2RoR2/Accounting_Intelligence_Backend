#!/usr/bin/env python3

import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services.extraction.combine_extraction import ClassifiedExtractionResult
from services.extraction.extraction_v1 import HEADERS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs"

RECONCILIATION_SCHEMA_VERSION = "0.1.0-proposed"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_requested_fields(fields):
    """Validate stored routing scope independently of the provider."""
    require(isinstance(fields, list) and bool(fields), "Expected a nonempty requested-field list.")
    require(all(isinstance(name, str) and name in HEADERS for name in fields),
            "Requested fields must be supported invoice headers.")
    require(len(set(fields)) == len(fields), "Requested fields must be unique.")


def validate_prediction(prediction, requested_fields):
    validate_requested_fields(requested_fields)
    require(isinstance(prediction, dict) and set(prediction) == set(requested_fields),
            "Prediction must contain exactly the requested fields.")
    require(all(value is None or (isinstance(value, str) and bool(value.strip()))
                for value in prediction.values()),
            "Prediction values must be nonempty strings or null.")


def require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


def reconcile(
    extraction_result_path: Path,
    selective_run_dir: Path,
    output_parent: Path = DEFAULT_RUNS_DIR,
):
    extraction_result_path = extraction_result_path.resolve()
    selective_run_dir = selective_run_dir.resolve()
    output_parent = output_parent.resolve()

    require(
        extraction_result_path.is_file(),
        f"Extraction result not found: {extraction_result_path}",
    )

    run_path = selective_run_dir / "run.json"
    routing_path = selective_run_dir / "routing.json"
    prediction_path = selective_run_dir / "prediction.json"

    for p in (run_path, routing_path, prediction_path):
        require(p.is_file(), f"Required selective-run artifact missing: {p}")

    extraction_raw = extraction_result_path.read_bytes()
    extraction = ClassifiedExtractionResult.model_validate_json(extraction_raw).model_dump()
    run = load_json(run_path)
    routing = load_json(routing_path)
    prediction = load_json(prediction_path)

    require(isinstance(run, dict) and isinstance(routing, dict),
            "Selective run and routing must be JSON objects.")
    require(run.get("status") == "response_structure_valid_needs_review",
            "Selective run must have response_structure_valid_needs_review status.")

    require(
        extraction.get("schema_version") == "1.1.0-proposed",
        "Reconciliation requires a classified 1.1.0-proposed extraction result.",
    )

    document_id = extraction.get("document_id")
    extraction_run_id = extraction.get("extraction_run_id")
    require(bool(document_id.strip()) and bool(extraction_run_id.strip()),
            "Document and extraction-run IDs must not be blank.")

    require(
        document_id == routing.get("document_id"),
        "Document ID mismatch between extraction and routing result.",
    )

    require(
        document_id == run.get("document_id"),
        "Document ID mismatch between extraction and selective run.",
    )

    require(
        extraction_run_id == routing.get("extraction_run_id"),
        "Extraction run ID mismatch between extraction and routing result.",
    )

    extraction_sha256 = hashlib.sha256(extraction_raw).hexdigest()

    require(
        extraction_sha256 == run.get("source_result_sha256"),
        "Extraction-result SHA-256 does not match the selective run.",
    )

    source_result_path = run.get("source_result_path")
    if source_result_path:
        require(
            Path(source_result_path).resolve() == extraction_result_path,
            "Selective run refers to a different extraction-result path.",
        )

    require(
        routing.get("decision") == "vlm_candidate",
        "Reconciliation expects a vlm_candidate routing decision.",
    )

    require(
        run.get("decision") == "vlm_candidate",
        "Selective run was not created from a vlm_candidate decision.",
    )

    require(
        run.get("ai_request_attempted") is True,
        "Selective AI request was not attempted.",
    )

    require(
        run.get("ai_response_received") is True,
        "Selective AI response was not received.",
    )

    routing_requested = routing.get("requested_fields")
    run_requested = run.get("requested_fields")
    validate_requested_fields(routing_requested)
    validate_requested_fields(run_requested)

    require(
        routing_requested == run_requested,
        "Requested fields differ between routing and selective run.",
    )

    validate_prediction(prediction, routing_requested)
    if "extraction_run_id" in run:
        require(run["extraction_run_id"] == extraction_run_id,
                "Extraction run ID mismatch in selective run.")

    extraction_fields = extraction.get("fields") or {}

    reconciled_fields = {}

    for field_name in routing_requested:
        require(
            field_name in extraction_fields,
            f"Requested field does not exist in extraction result: {field_name}",
        )

        parser_field = extraction_fields[field_name]
        parser_value = parser_field.get("value")
        parser_state = parser_field.get("state")
        parser_method = parser_field.get("method")

        vlm_candidate = prediction.get(field_name)

        if parser_value not in (None, ""):
            resolution = "parser_preserved"
            final_value = parser_value
            review_required = True

        elif vlm_candidate is None:
            resolution = "unresolved_missing"
            final_value = None
            review_required = True

        else:
            resolution = "candidate_for_review"
            final_value = None
            review_required = True

        reconciled_fields[field_name] = {
            "parser": {
                "value": parser_value,
                "state": parser_state,
                "method": parser_method,
                "raw_text": parser_field.get("raw_text"),
                "evidence": parser_field.get("evidence") or [],
                "candidates": parser_field.get("candidates") or [],
            },
            "vlm": {
                "candidate": vlm_candidate,
                "model": run.get("model"),
                "selective_run_id": selective_run_dir.name,
                "requested": True,
            },
            "resolution": resolution,
            "final_value": final_value,
            "review_required": review_required,
        }

    overall_review_required = any(
        field["review_required"] for field in reconciled_fields.values()
    )

    if any(
        field["resolution"] == "candidate_for_review"
        for field in reconciled_fields.values()
    ):
        status = "candidate_for_review"

    elif any(
        field["resolution"] == "unresolved_missing"
        for field in reconciled_fields.values()
    ):
        status = "unresolved_needs_review"

    else:
        status = "parser_preserved"

    result = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "mode": "selective_vlm_reconciliation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "document_id": document_id,
        "source_extraction_run_id": extraction_run_id,
        "source_extraction_result": str(extraction_result_path),
        "source_extraction_sha256": extraction_sha256,
        "selective_run_id": selective_run_dir.name,
        "status": status,
        "review_required": overall_review_required,
        "fields": reconciled_fields,
        "provenance": {
            "routing_policy_version": routing.get("policy_version"),
            "model": run.get("model"),
            "source_pdf": run.get("source_pdf"),
            "source_pdf_sha256": run.get("source_pdf_sha256"),
            "image": run.get("image"),
            "image_sha256": run.get("image_sha256"),
            "page": run.get("page"),
            "requested_fields": routing_requested,
            "prediction_sha256": sha256_file(prediction_path),
            "selective_run_status": run.get("status"),
        },
        "safety": {
            "parser_result_overwritten": False,
            "ai_candidate_auto_accepted": False,
            "automatic_accounting_approval": False,
        },
    }

    output_parent.mkdir(parents=True, exist_ok=True)

    output_dir = output_parent / f"reconciliation-{uuid.uuid4().hex}"
    output_dir.mkdir(parents=False)

    result_path = output_dir / "result.json"
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return result_path, result


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile a classified parser result with a selective VLM "
            "prediction without overwriting parser evidence."
        )
    )

    parser.add_argument(
        "extraction_result",
        help="Path to a 1.1.0-proposed classified extraction result.",
    )

    parser.add_argument(
        "selective_run",
        help="Path to a selective-* run directory.",
    )

    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_RUNS_DIR),
        help="Parent folder for reconciliation-* run directories.",
    )

    args = parser.parse_args()

    try:
        result_path, result = reconcile(
            Path(args.extraction_result),
            Path(args.selective_run),
            Path(args.output_dir),
        )
    except Exception as exc:
        print(f"Status: failed")
        print(f"Error: {exc}")
        raise SystemExit(1)

    print(f"Document: {result['document_id']}")
    print(f"Status: {result['status']}")
    print(f"Review required: {result['review_required']}")

    for field_name, field in result["fields"].items():
        print()
        print(f"Field: {field_name}")
        print(f"Parser value: {field['parser']['value']!r}")
        print(f"Parser state: {field['parser']['state']}")
        print(f"VLM candidate: {field['vlm']['candidate']!r}")
        print(f"Resolution: {field['resolution']}")
        print(f"Final value: {field['final_value']!r}")

    print()
    print("Parser result overwritten: False")
    print("AI candidate auto-accepted: False")
    print(f"Saved: {result_path.parent}")


if __name__ == "__main__":
    main()
