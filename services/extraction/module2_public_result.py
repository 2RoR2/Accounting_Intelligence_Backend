"""Build the stable public Module 2 result from a service run.

This is the boundary intended for downstream backend integration.

It deliberately separates:
- parser-extracted values;
- AI candidates;
- reconciliation state;
- downstream accounting approval.

An AI candidate is never silently promoted into the parser value.
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from services.extraction.combine_extraction import ClassifiedExtractionResult
from services.extraction.extraction_v1 import StrictModel
from services.extraction.reconcile_selective import validate_prediction, validate_requested_fields
from services.extraction.route_extraction import decide_route


class RoutingState(StrictModel):
    decision: Literal["parser_only", "human_review", "vlm_candidate"]
    requested_fields: list[str]
    candidate_reasons: list[dict[str, Any]]
    blocking_reasons: list[dict[str, Any]]

    @model_validator(mode="after")
    def valid_scope(self):
        if self.decision == "vlm_candidate":
            validate_requested_fields(self.requested_fields)
            if self.blocking_reasons:
                raise ValueError("A blocked route cannot authorise selective inference.")
        elif self.requested_fields:
            raise ValueError("Skipped routes must not request AI fields.")
        if self.decision == "parser_only" and (self.candidate_reasons or self.blocking_reasons):
            raise ValueError("Parser-only routing must have no candidate/blocking reasons.")
        if self.decision == "human_review" and not self.blocking_reasons:
            raise ValueError("Human-review routing must identify a blocker.")
        return self


class AIAssistance(StrictModel):
    attempted: bool
    response_received: bool
    status: Literal["skipped_parser_only", "skipped_human_review", "failed",
                    "response_structure_valid_needs_review"]
    prediction: dict[str, str | None] | None
    model: str | None = None
    route_decision: str | None = None


class ReconciliationSummary(StrictModel):
    status: Literal["candidate_for_review", "unresolved_needs_review", "parser_preserved"]
    review_required: bool
    fields: dict[str, dict[str, Any]]


class ServiceEnvelope(BaseModel):
    """Validate the publishable service boundary; retain unrelated run metadata."""
    model_config = ConfigDict(strict=True, extra="allow")
    schema_version: Literal["0.1.0-proposed"]
    service_run_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_type: dict[str, Any]
    status: Literal["parser_only_needs_review", "human_review_required",
                    "ai_attempt_failed_needs_review", "candidate_for_review",
                    "unresolved_needs_review", "parser_preserved"]
    review_required: bool
    accounting_approval_performed: bool
    artifacts: dict[str, str]
    routing: RoutingState
    ai_assistance: AIAssistance
    reconciliation: ReconciliationSummary | None

    @model_validator(mode="after")
    def consistent_status(self):
        if not self.document_id.strip() or not self.service_run_id.strip():
            raise ValueError("Service/document identities must not be blank.")
        if not self.review_required or self.accounting_approval_performed:
            raise ValueError("Module 2 publication requires review and no accounting approval.")
        ai, route = self.ai_assistance, self.routing
        if ai.response_received and not ai.attempted:
            raise ValueError("A received AI response requires an attempted request.")
        has_reconciliation = bool(self.artifacts.get("reconciliation_result"))
        if self.status in {"parser_only_needs_review", "human_review_required"}:
            expected_route = "parser_only" if self.status == "parser_only_needs_review" else "human_review"
            if (route.decision != expected_route or ai.status != "skipped_" + expected_route
                    or ai.attempted or ai.response_received or ai.prediction is not None
                    or self.reconciliation is not None or has_reconciliation
                    or "selective_run" in self.artifacts):
                raise ValueError("Skipped service status contradicts routing/AI/artifacts.")
        else:
            if route.decision != "vlm_candidate":
                raise ValueError("AI outcomes require an authorised vlm_candidate route.")
            if self.status == "ai_attempt_failed_needs_review":
                if (ai.status != "failed" or ai.prediction is not None
                        or self.reconciliation is not None or has_reconciliation):
                    raise ValueError("Failed AI outcome cannot publish predictions or reconciliation.")
            else:
                if (ai.status != "response_structure_valid_needs_review" or not ai.attempted
                        or not ai.response_received or self.reconciliation is None
                        or not has_reconciliation or not self.artifacts.get("selective_run")):
                    raise ValueError("Reconciled status requires successful AI and reconciliation evidence.")
                validate_prediction(ai.prediction, route.requested_fields)
                if self.reconciliation.status != self.status or not self.reconciliation.review_required:
                    raise ValueError("Service and reconciliation status/review requirements differ.")
        return self


def validate_reconciled_fields(reconciliation, extraction, service):
    """Check state/evidence against the parser; never infer acceptance from status."""
    requested = service["routing"]["requested_fields"]
    fields = reconciliation.get("fields")
    require(isinstance(fields, dict) and set(fields) == set(requested),
            "Reconciliation must contain exactly the requested fields.")
    require(reconciliation.get("schema_version") == "0.1.0-proposed"
            and reconciliation.get("review_required") is True,
            "Expected review-required reconciliation schema.")
    require(reconciliation.get("safety") == {
        "parser_result_overwritten": False, "ai_candidate_auto_accepted": False,
        "automatic_accounting_approval": False}, "Invalid reconciliation safety flags.")
    resolutions = []
    for name in requested:
        original, field = extraction["fields"][name], fields[name]
        require(isinstance(field, dict), "Invalid reconciled field.")
        parser = {key: original[key] for key in
                  ("value", "state", "method", "raw_text", "evidence", "candidates")}
        require(field.get("parser") == parser, "Reconciliation changed parser values/state/evidence.")
        vlm = field.get("vlm")
        require(isinstance(vlm, dict), "Missing VLM candidate record.")
        candidate = vlm.get("candidate")
        validate_prediction({name: candidate}, [name])
        require("candidate" in vlm and vlm.get("requested") is True
                and vlm.get("selective_run_id") == reconciliation.get("selective_run_id")
                and candidate == service["ai_assistance"]["prediction"][name],
                "Reconciliation candidate/source differs from the service prediction.")
        if original["value"] is not None:
            resolution, final_value = "parser_preserved", original["value"]
        elif candidate is None:
            resolution, final_value = "unresolved_missing", None
        else:
            resolution, final_value = "candidate_for_review", None
        require(field.get("resolution") == resolution and field.get("final_value") == final_value
                and "final_value" in field and field.get("review_required") is True,
                "Reconciliation resolution must preserve parser values and require review.")
        resolutions.append(resolution)
    expected = ("candidate_for_review" if "candidate_for_review" in resolutions
                else "unresolved_needs_review" if "unresolved_missing" in resolutions
                else "parser_preserved")
    require(reconciliation.get("status") == expected == service["status"],
            "Reconciliation/service status does not describe the field outcomes.")
    require(service["reconciliation"] == {key: reconciliation[key] for key in
            ("status", "review_required", "fields")}, "Reconciliation summary differs from its artifact.")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_SCHEMA_VERSION = "0.1.0-proposed"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def checked_path(
    path: Path,
    project_root: Path,
    label: str,
) -> Path:
    root = Path(project_root).resolve()
    resolved = Path(path).resolve()

    if not resolved.is_relative_to(root):
        raise ValueError(
            f"{label} must remain inside project root: "
            f"{resolved}"
        )

    return resolved


def load_json(path: Path):
    with path.open(
        "r",
        encoding="utf-8",
    ) as handle:
        return json.load(handle)


def require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


def public_field(
    parser_field: dict,
    reconciliation_field: dict | None = None,
):
    """Expose parser evidence plus any separate VLM candidate."""

    parser_value = parser_field.get("value")
    parser_state = parser_field.get("state")

    ai_candidate = None
    resolution = None

    if reconciliation_field is not None:
        vlm = reconciliation_field.get("vlm") or {}

        ai_candidate = vlm.get("candidate")
        resolution = reconciliation_field.get(
            "resolution"
        )

        #
        # Safety check:
        # reconciliation must describe the same parser value
        # rather than silently replacing it.
        #
        reconciled_parser = (
            reconciliation_field.get("parser")
            or {}
        )

        require(
            reconciled_parser.get("value")
            == parser_value,
            "Reconciliation parser value differs from "
            "classified extraction.",
        )

    elif parser_state == "resolved":
        resolution = "parser_resolved"

    else:
        resolution = "unresolved"

    return {
        "raw_text": parser_field.get("raw_text"),
        "value": parser_value,
        "state": parser_state,
        "method": parser_field.get("method"),
        "evidence": parser_field.get(
            "evidence"
        )
        or [],
        "candidates": parser_field.get(
            "candidates"
        )
        or [],
        "ai_candidate": ai_candidate,
        "resolution": resolution,
        "review_required": parser_field.get(
            "review_required",
            True,
        ),
    }


def build_public_result(
    service_result_path: Path,
    project_root: Path = PROJECT_ROOT,
    output_parent: Path | None = None,
):
    root = Path(project_root).resolve()

    service_path = checked_path(
        service_result_path,
        root,
        "Service result",
    )

    service_raw = service_path.read_bytes()
    service = json.loads(service_raw)
    ServiceEnvelope.model_validate(service)

    require(
        service.get("status")
        not in {
            None,
            "processing",
            "failed_needs_review",
        },
        "Cannot publish an incomplete or failed service run.",
    )

    document_id = service.get("document_id")

    require(
        isinstance(document_id, str)
        and document_id.strip(),
        "Service result has no valid document_id.",
    )

    artifacts = service.get("artifacts") or {}

    classified_path_raw = artifacts.get(
        "classified_extraction"
    )

    require(
        classified_path_raw,
        "Service result has no classified extraction artifact.",
    )

    classified_path = checked_path(
        Path(classified_path_raw),
        root,
        "Classified extraction",
    )

    extraction_raw = classified_path.read_bytes()
    extraction = ClassifiedExtractionResult.model_validate_json(extraction_raw).model_dump()

    classified_extraction_sha256 = hashlib.sha256(
        extraction_raw
    ).hexdigest()

    require(
        extraction.get("document_id")
        == document_id,
        "Service and extraction document IDs differ.",
    )

    require(
        extraction.get("schema_version")
        == "1.1.0-proposed",
        "Public result requires classified "
        "1.1.0-proposed extraction.",
    )

    require(service["document_type"] == extraction["document_type"],
            "Service classification differs from the classified extraction.")
    # The service already writes this source-bound routing artifact. Checking it
    # also binds parser-only public output to the bytes that were routed.
    require(bool(artifacts.get("routing_result")), "Service has no routing artifact.")
    route = load_json(checked_path(Path(artifacts["routing_result"]), root, "Routing result"))
    require(isinstance(route, dict), "Routing artifact must be an object.")
    require(route.get("source_result_sha256") == classified_extraction_sha256,
            "Routing extraction SHA-256 does not match the public source.")
    require(route.get("document_id") == document_id
            and route.get("extraction_run_id") == extraction["extraction_run_id"],
            "Routing document/extraction identity differs.")
    require(isinstance(route.get("source_result_path"), str)
            and checked_path(Path(route["source_result_path"]), root, "Routing source") == classified_path,
            "Routing refers to a different extraction path.")
    require({key: route.get(key) for key in service["routing"]} == service["routing"],
            "Service routing summary differs from its artifact.")
    allow_vlm = service.get("routing", {}).get("decision") == "vlm_candidate"
    expected_route = decide_route(extraction, allow_vlm_inference=allow_vlm)
    require({key: expected_route[key] for key in service["routing"]} == service["routing"],
            "Service routing contradicts the classified extraction and current policy.")

    reconciliation = None
    reconciliation_path = None

    reconciliation_path_raw = artifacts.get(
        "reconciliation_result"
    )

    if reconciliation_path_raw:
        reconciliation_path = checked_path(
            Path(reconciliation_path_raw),
            root,
            "Reconciliation result",
        )

        reconciliation = load_json(
            reconciliation_path
        )

        require(isinstance(reconciliation, dict), "Reconciliation must be an object.")

        require(
            reconciliation.get("document_id")
            == document_id,
            "Service and reconciliation document IDs differ.",
        )

        require(
            reconciliation.get(
                "source_extraction_run_id"
            )
            == extraction.get(
                "extraction_run_id"
            ),
            "Reconciliation refers to a different "
            "classified extraction run.",
        )

        require(
            reconciliation.get(
                "source_extraction_sha256"
            )
            == classified_extraction_sha256,
            "Reconciliation extraction SHA-256 does not "
            "match the classified extraction being published.",
        )

        reconciliation_source = reconciliation.get(
            "source_extraction_result"
        )

        require(
            isinstance(reconciliation_source, str)
            and reconciliation_source,
            "Reconciliation does not identify its source "
            "extraction result.",
        )

        require(
            checked_path(
                Path(reconciliation_source),
                root,
                "Reconciliation source extraction",
            )
            == classified_path,
            "Reconciliation refers to a different "
            "classified extraction path.",
        )

        selective = checked_path(Path(artifacts["selective_run"]), root, "Selective run")
        require(selective.name == reconciliation.get("selective_run_id"), "Selective run identity differs.")
        prediction_raw = (selective / "prediction.json").read_bytes()
        prediction = json.loads(prediction_raw)
        validate_prediction(prediction, service["routing"]["requested_fields"])
        provenance = reconciliation.get("provenance")
        require(isinstance(provenance, dict)
                and provenance.get("prediction_sha256") == hashlib.sha256(prediction_raw).hexdigest(),
                "Reconciliation prediction SHA-256 does not match the selective artifact.")
        require(prediction == service["ai_assistance"]["prediction"],
                "Service prediction differs from the selective artifact.")
        run = load_json(selective / "run.json")
        require(isinstance(run, dict) and run.get("status") == "response_structure_valid_needs_review"
                and run.get("ai_request_attempted") is True and run.get("ai_response_received") is True
                and run.get("decision") == "vlm_candidate"
                and run.get("document_id") == document_id
                and run.get("requested_fields") == service["routing"]["requested_fields"],
                "Selective run status, identity or scope contradicts the service.")
        require(run.get("source_result_sha256") == classified_extraction_sha256,
                "Selective extraction SHA-256 does not match the public source.")
        require(isinstance(run.get("source_result_path"), str)
                and checked_path(Path(run["source_result_path"]), root, "Selective source") == classified_path,
                "Selective run refers to a different extraction path.")
        validate_reconciled_fields(reconciliation, extraction, service)

    reconciliation_fields = (
        reconciliation.get("fields", {})
        if reconciliation
        else {}
    )

    fields = {}

    for name, field in (
        extraction.get("fields") or {}
    ).items():
        fields[name] = public_field(
            field,
            reconciliation_fields.get(name),
        )

    line_items = []

    for row in (
        extraction.get("line_items") or []
    ):
        public_row = {}

        for name, field in row.items():
            public_row[name] = public_field(
                field,
                None,
            )

        line_items.append(public_row)

    document_type = (
        extraction.get("document_type")
        or {}
    )

    routing = service.get("routing") or {}

    result = {
        "schema_version": (
            PUBLIC_SCHEMA_VERSION
        ),
        "mode": "module2_public_result",
        "field_schema": extraction.get("field_schema"),
        "document_id": document_id,
        "document_type": {
            "classifier_version": document_type.get("classifier_version"),
            "candidates": document_type.get("candidates") or [],
            "unsupported_headings": document_type.get("unsupported_headings") or [],
            "value": document_type.get(
                "value"
            ),
            "status": document_type.get(
                "status"
            ),
            "method": document_type.get(
                "method"
            ),
            "reason": document_type.get(
                "reason"
            ),
        },
        "status": service.get(
            "status"
        ),
        "review_required": service.get(
            "review_required",
            True,
        ),
        "accounting_approval_performed": False,
        "fields": fields,
        "line_items": line_items,
        "issues": extraction.get(
            "issues"
        )
        or [],
        "routing": {
            "decision": routing.get(
                "decision"
            ),
            "requested_fields": routing.get(
                "requested_fields"
            )
            or [],
            "candidate_reasons": routing.get(
                "candidate_reasons"
            )
            or [],
            "blocking_reasons": routing.get(
                "blocking_reasons"
            )
            or [],
        },
        "provenance": {
            "service_run_id": service.get(
                "service_run_id"
            ),
            "service_result_sha256": (
                hashlib.sha256(
                    service_raw
                ).hexdigest()
            ),
            "classified_extraction_run_id": (
                extraction.get(
                    "extraction_run_id"
                )
            ),
            "classified_extraction_sha256": (
                classified_extraction_sha256
            ),
            "selective_run_id": (
                reconciliation.get(
                    "selective_run_id"
                )
                if reconciliation
                else None
            ),
            "reconciliation_result_sha256": (
                sha256_file(
                    reconciliation_path
                )
                if reconciliation_path
                else None
            ),
        },
        "safety": {
            "ai_candidate_auto_accepted": False,
            "automatic_accounting_approval": False,
        },
    }

    if output_parent is None:
        output_parent = root / "runs"

    output_parent = checked_path(
        output_parent,
        root,
        "Output parent",
    )

    output_parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    out = (
        output_parent
        / (
            "module2-public-"
            + uuid4().hex
        )
    )

    out.mkdir(
        parents=False,
        exist_ok=False,
    )

    result_path = out / "result.json"

    result_path.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    return result_path, result


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "service_result",
        type=Path,
    )

    args = parser.parse_args()

    try:
        path, result = (
            build_public_result(
                args.service_result
            )
        )

    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
    ) as error:
        parser.error(str(error))

    print(
        "Document:",
        result["document_id"],
    )

    print(
        "Document type:",
        result["document_type"]["value"],
    )

    print(
        "Status:",
        result["status"],
    )

    print(
        "Routing:",
        result["routing"]["decision"],
    )

    print(
        "Review required:",
        result["review_required"],
    )

    print(
        "Accounting approval performed:",
        result[
            "accounting_approval_performed"
        ],
    )

    print(
        "Saved:",
        path.parent,
    )


if __name__ == "__main__":
    main()
