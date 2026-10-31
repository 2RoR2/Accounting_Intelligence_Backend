#!/usr/bin/env python3

import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services.extraction.parsing.combine_extraction import ClassifiedExtractionResult
from services.extraction.ai.qwen_provider import (
    parse_json, validate_response, prepare_request, validate_fields,
    validate_prediction as validate_provider_prediction,
)


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
        return parse_json(f.read())


def validate_requested_fields(fields, profile=None):
    """Validate stored routing scope independently of the provider."""
    require(isinstance(fields, list) and bool(fields), "Expected a nonempty requested-field list.")
    validate_fields(fields, profile)
    require(len(set(fields)) == len(fields), "Requested fields must be unique.")


def validate_prediction(prediction, requested_fields, profile=None):
    validate_requested_fields(requested_fields, profile)
    require(isinstance(prediction, dict) and set(prediction) == set(requested_fields),
            "Prediction must contain exactly the requested fields.")
    require(all(value is None or (isinstance(value, str) and bool(value.strip()))
                for value in prediction.values()),
            "Prediction values must be nonempty strings or null.")
    validate_provider_prediction(prediction, requested_fields, profile)


def verify_profile_recovery(extraction, run, routing, directory, root, source_path, authorized):
    """Recheck the source-bound plan; a successful JSON response proves no field."""
    from services.extraction.ai.run_selective_qwen import verified_pdf, recovery_context
    from services.extraction.classification.route_extraction import plan_field_recovery, decide_route
    require(authorized is True and root is not None, 'Current source authorization and project root are required.')
    profile = extraction['document_type']['value']
    require(run.get('profile') == profile and run.get('inference_permission') is True
            and run.get('source_access_authorized') is True, 'Recovery profile or explicit permissions differ.')
    fields = run.get('requested_fields')
    validate_requested_fields(fields, profile)
    expected_route = decide_route(extraction, allow_vlm_inference=True)
    require(all(routing.get(k) == v for k, v in expected_route.items()), 'Original document review route changed.')
    plan_path = directory / 'recovery-plan.json'
    require(plan_path.is_file() and sha256_file(plan_path) == run.get('recovery_plan_sha256'), 'Recovery plan hash mismatch.')
    saved = load_json(plan_path)
    configuration = run.get('model_configuration')
    require(isinstance(configuration, dict) and configuration.get('model') == run.get('model')
            and configuration.get('profile') == profile and configuration.get('render_scale') == 1.5
            and configuration.get('request_contract') == 'profile_fields/1', 'Model/configuration identity mismatch.')
    plan = plan_field_recovery(extraction, root, source_path=source_path,
        source_access_authorized=authorized, model_configuration=configuration)
    require(plan['source_verified'] and not plan['blocking_reasons']
            and all(plan['fields'].get(name, {}).get('eligible') for name in fields), 'Recovery eligibility/source no longer valid.')
    plan.update(profile=profile, selected_fields=fields, source_result_sha256=run['source_result_sha256'])
    require(saved == plan and run.get('configuration_sha256') == plan['configuration_sha256'], 'Recovery plan/configuration changed.')
    _, pdf_bytes = verified_pdf(extraction, root, source_path=source_path, source_access_authorized=authorized)
    require(hashlib.sha256(pdf_bytes).hexdigest() == run.get('source_pdf_sha256') and run.get('page') == 1,
            'Recovery PDF/page identity mismatch.')
    image_path = Path(run['image']).resolve()
    require(image_path.parent == directory and sha256_file(image_path) == run.get('image_sha256'), 'Recovery image hash/path mismatch.')
    request = prepare_request(image_path.read_bytes(), fields, model=run['model'], profile=profile,
                              source_context=recovery_context(extraction, plan, fields, root))
    settings = directory / 'request-settings.json'; prompt = directory / 'prompt.txt'
    require(sha256_file(settings) == run.get('request_settings_sha256')
            and load_json(settings) == {k:request[k] for k in ('model','format','stream','keep_alive','options')}
            and configuration.get('options') == request['options'], 'Requested schema/configuration mismatch.')
    require(sha256_file(prompt) == run.get('prompt_sha256')
            and prompt.read_text(encoding='utf-8') == request['messages'][0]['content'], 'Source context/prompt changed.')
    return profile, fields, plan


def require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


def reconcile(
    extraction_result_path: Path,
    selective_run_dir: Path,
    output_parent: Path = DEFAULT_RUNS_DIR,
    *, project_root=None, source_path=None, source_access_authorized=False,
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

    scoped = run.get('recovery_scope') == 'profile_fields/1'
    profile, plan = None, None
    if scoped:
        profile, routing_requested, plan = verify_profile_recovery(extraction, run, routing, selective_run_dir,
            project_root, source_path, source_access_authorized)
        require(run.get('decision') == routing.get('decision'), 'Document review decision changed.')
    else:
        require('recovery_scope' not in run and routing.get('decision') == run.get('decision') == 'vlm_candidate',
                'Reconciliation expects an authorized supported selective scope.')
        routing_requested = routing.get('requested_fields')

    require(
        run.get("ai_request_attempted") is True,
        "Selective AI request was not attempted.",
    )

    require(
        run.get("ai_response_received") is True,
        "Selective AI response was not received.",
    )

    run_requested = run.get("requested_fields")
    validate_requested_fields(routing_requested, profile)
    validate_requested_fields(run_requested, profile)

    require(
        routing_requested == run_requested,
        "Requested fields differ between routing and selective run.",
    )

    validate_prediction(prediction, routing_requested, profile)
    if 'prediction_sha256' in run:
        require(run['prediction_sha256'] == sha256_file(prediction_path),
                'Selective prediction SHA-256 mismatch.')
    if 'raw_response_sha256' in run:
        raw_response = selective_run_dir / 'raw_response.txt'
        require(raw_response.is_file() and sha256_file(raw_response) == run['raw_response_sha256'],
                'Selective raw-response SHA-256 mismatch.')
        require(validate_response(parse_json(raw_response.read_text(encoding='utf-8')),
                                  routing_requested, model=run.get('model'), profile=profile) == prediction,
                'Prediction differs from the validated raw response.')
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
                "profile": profile or extraction['document_type']['value'],
                "field": field_name,
                "verification": {
                    "status": "unverified",
                    "reasons": ['MODEL_VALUE_NOT_VERIFIED_AGAINST_SOURCE']
                        + (['MODEL_RETURNED_NULL'] if vlm_candidate is None else [])
                        + (['PARSER_CANDIDATES_REQUIRE_REVIEW'] if parser_state in {'ambiguous','conflict'} else [])
                        + (['PARSER_VALUE_PRESERVED'] if parser_value is not None else [])
                        + (['MODEL_DISAGREES_WITH_PARSER'] if vlm_candidate is not None and parser_value is not None
                           and vlm_candidate != parser_value else [])
                        + (['PARTY_ROLE_NOT_VERIFIED'] if field_name in {'merchant','merchant_address','supplier_name',
                            'supplier_address','issuer','provider_address'} else []),
                },
                # A page image is provenance, not verified field localization.
                "confidence": None,
                "evidence": [],
                "provenance": {
                    "source_extraction_sha256": extraction_sha256,
                    "document_id": document_id,
                    "extraction_run_id": extraction_run_id,
                    "configuration_sha256": run.get('configuration_sha256'),
                    "source_transformation": extraction['provenance'].get('source_transformation'),
                    "source_json_sha256": extraction['provenance'].get('source_json_sha256'),
                    "attempt_id": plan['fields'][field_name]['attempt_id'] if plan else None,
                    "source_context_refs": plan['fields'][field_name]['source_refs'] if plan else [],
                    "source_pdf_sha256": run.get("source_pdf_sha256"),
                    "image": run.get("image"),
                    "image_sha256": run.get("image_sha256"),
                    "page": run.get("page"),
                    "prediction_sha256": sha256_file(prediction_path),
                    "raw_response_sha256": run.get("raw_response_sha256"),
                    "grounding": "unverified_page_image",
                },
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
            "document_review_decision": routing.get('decision'),
            "document_review_blockers": routing.get('blocking_reasons', []),
            "recovery_scope": run.get('recovery_scope'),
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
