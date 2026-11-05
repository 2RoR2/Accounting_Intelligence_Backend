"""Single-entry workflow service for Module 2 document intelligence.

This service composes the existing verified stages:

PDF
 -> Docling parsing
 -> deterministic mapping
 -> ExtractionResult adaptation
 -> document classification
 -> classified extraction combination
 -> routing
 -> optional selective Qwen attempt
 -> optional reconciliation

Important:
- AI output is never automatically approved.
- Parser values are not silently overwritten by VLM candidates.
- Accounting validation is outside Module 2.
"""

import hashlib
import inspect
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from services.extraction.parsing.baseline_parser import parse_document
from services.extraction.parsing.map_invoice import map_document, with_recovery_placeholders
from services.extraction.parsing.extraction_v1 import ExtractionResultV1, adapt_result
from services.extraction.classification.classify_document import classify_document
from services.extraction.parsing.combine_extraction import combine_files
from services.extraction.classification.route_extraction import assess_extraction, decide_route
from services.extraction.ai.run_selective_qwen import run_attempt
from services.extraction.classification.reconcile_selective import reconcile


SERVICE_SCHEMA_VERSION = "0.1.0-proposed"
MAX_TARGETED_RETRIES = 1
# One model call takes about 1 to 5 s once loaded, which keeps a retry inside the 20 s target.
RETRY_LATENCY_BUDGET_SECONDS = 12.0
RECOVERABLE_ATTEMPT_ERRORS = {"ValueError", "JSONDecodeError"}
# Validation names a value by its stored key; the model request uses extraction field names.
VALIDATION_TO_RECOVERY = {
    "supplier": {"supplier_name", "issuer", "merchant"}, "supplier_address": {"supplier_address", "provider_address", "merchant_address"},
    "date": {"invoice_date", "bill_date", "transaction_date"}, "invoice_date": {"invoice_date"}, "invoice": {"invoice_number"},
    "reference": {"bill_reference", "receipt_number"}, "total": {"total_amount"}, "tax": {"tax_amount"}, "due": {"due_date"},
    "amount_due": {"amount_due"}, "account_number": {"account_number"},
}


def usable_candidates(reconciliation) -> int:
    fields = (reconciliation or {}).get("fields") or {}
    return sum(field["vlm"].get("candidate") is not None for field in fields.values())


def recoverable_fields(summary, elapsed_seconds, attempt_error=None) -> list:
    """Fields validation still needs where the model's answer was unusable, not where it found nothing.

    A model that read the page and returned null has already answered. A timeout
    or an unreachable server would only spend the latency budget again.
    """
    asked = (summary.get("selective_recovery") or {}).get("requested_fields") or []
    if not asked or elapsed_seconds > RETRY_LATENCY_BUDGET_SECONDS:
        return []
    needed = set()
    for finding in summary["validation_precheck"]["findings"]:
        needed |= {finding.get("field")} | VALIDATION_TO_RECOVERY.get(finding.get("field"), set())
    reconciled = (summary.get("reconciliation") or {}).get("fields")
    if reconciled is None:
        return [name for name in asked if name in needed] if attempt_error in RECOVERABLE_ATTEMPT_ERRORS else []
    return [name for name in asked if name in needed and (reconciled.get(name) or {}).get("field_rejection")]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class DocumentIntelligenceService:
    """Public Module 2 workflow interface."""

    def __init__(
        self,
        project_root: Path | None = None,
        parser=None,
        allow_vlm_inference: bool = False,
        models_dir: Path | None = None,
    ):
        self.project_root = (
            Path(project_root)
            if project_root is not None
            else Path(__file__).resolve().parents[3]
        ).resolve()

        self.runs_dir = self.project_root / "runs"
        self._parser = parser
        self.allow_vlm_inference = allow_vlm_inference
        self.models_dir = (
            Path(models_dir).resolve()
            if models_dir is not None
            else None
        )

    def _checked_path(self, path: Path, label: str) -> Path:
        resolved = Path(path).expanduser().resolve()

        if not resolved.is_relative_to(self.project_root):
            raise ValueError(
                f"{label} must remain inside project root: "
                f"{self.project_root}"
            )

        return resolved

    def _save_mapping(
        self,
        document_json_path: Path,
        parser_run_dir: Path,
    ) -> Path:
        source = self._checked_path(
            document_json_path,
            "Docling document",
        )

        raw = source.read_bytes()
        document = json.loads(raw)

        started = time.perf_counter()

        result = with_recovery_placeholders(map_document(document))

        result["provenance"]["mapping_seconds"] = round(
            time.perf_counter() - started,
            6,
        )

        result["provenance"]["source_json_sha256"] = (
            sha256_bytes(raw)
        )

        result["provenance"]["source_document_json"] = str(source)

        parser_run_file = parser_run_dir / "run.json"

        if not parser_run_file.is_file():
            raise ValueError(
                f"Parser run metadata missing: {parser_run_file}"
            )

        result["provenance"]["parser_run"] = json.loads(
            parser_run_file.read_text(encoding="utf-8")
        )

        result["run_id"] = (
            "mapped-"
            + datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            + "-"
            + uuid4().hex[:8]
        )

        out = self.runs_dir / result["run_id"]
        out.mkdir(parents=True, exist_ok=False)

        result_path = out / "result.json"
        write_json(result_path, result)

        return result_path

    def _save_extraction(
        self,
        mapped_result_path: Path,
        document_id: str,
    ) -> Path:
        source = self._checked_path(
            mapped_result_path,
            "Mapped result",
        )

        raw = source.read_bytes()

        result = adapt_result(
            json.loads(raw),
            document_id,
            source,
            sha256_bytes(raw),
        )

        out = self.runs_dir / result.extraction_run_id
        out.mkdir(parents=True, exist_ok=False)

        result_path = out / "result.json"

        result_path.write_text(
            result.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

        (out / "schema.json").write_text(
            json.dumps(
                ExtractionResultV1.model_json_schema(),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        return result_path

    def _save_classification(
        self,
        document_json_path: Path,
    ) -> Path:
        source = self._checked_path(
            document_json_path,
            "Classification source",
        )

        raw = source.read_bytes()

        result = classify_document(
            json.loads(raw)
        )

        result["source_document_json"] = str(source)

        result["source_json_sha256"] = (
            sha256_bytes(raw)
        )

        out = (
            self.runs_dir
            / ("classification-" + uuid4().hex)
        )

        out.mkdir(
            parents=True,
            exist_ok=False,
        )

        result_path = out / "result.json"

        write_json(
            result_path,
            result,
        )

        return result_path

    def _save_combined(
        self,
        extraction_result_path: Path,
        classification_result_path: Path,
    ) -> Path:
        result = combine_files(
            extraction_result_path,
            classification_result_path,
            self.project_root,
        )

        out = (
            self.runs_dir
            / result.extraction_run_id
        )

        out.mkdir(
            parents=True,
            exist_ok=False,
        )

        result_path = out / "result.json"

        result_path.write_text(
            result.model_dump_json(indent=2)
            + "\n",
            encoding="utf-8",
        )

        return result_path

    def _save_routing(
        self,
        classified_extraction_path: Path,
        allow_vlm_inference: bool = False,
    ):
        source = self._checked_path(
            classified_extraction_path,
            "Classified extraction",
        )

        raw = source.read_bytes()

        decision = decide_route(
            json.loads(raw),
            allow_vlm_inference=allow_vlm_inference,
        )

        decision["source_result_path"] = str(source)

        decision["source_result_sha256"] = (
            sha256_bytes(raw)
        )

        out = (
            self.runs_dir
            / ("routing-" + uuid4().hex)
        )

        out.mkdir(
            parents=True,
            exist_ok=False,
        )

        result_path = out / "decision.json"

        write_json(
            result_path,
            decision,
        )

        return result_path, decision

    def _record_ai_outcome(self, summary, combined_result_path, selective_dir, selective_record,
                           source_path, source_access_authorized):
        """Reconcile a successful attempt. Parser values and fields are never changed."""
        summary["artifacts"]["selective_run"] = str(selective_dir)
        summary["ai_assistance"]["attempted"] = selective_record.get("ai_request_attempted", False)
        summary["ai_assistance"]["response_received"] = selective_record.get("ai_response_received", False)
        summary["ai_assistance"]["status"] = selective_record.get("status")
        if selective_record.get("status") != "response_structure_valid_needs_review":
            # Provider/model/response failure must not silently become an approved result.
            summary["status"] = "ai_attempt_failed_needs_review"
            return
        try:
            summary["ai_assistance"]["prediction"] = json.loads(
                (selective_dir / "prediction.json").read_text(encoding="utf-8"))
            reconciliation_path, result = reconcile(
                combined_result_path, selective_dir, self.runs_dir, project_root=self.project_root,
                source_path=source_path, source_access_authorized=source_access_authorized)
        except Exception as error:
            # A candidate that cannot be reconciled is dropped; the parser result stands.
            summary["ai_assistance"].update(status="failed", prediction=None)
            summary["status"] = "ai_attempt_failed_needs_review"
            summary["recovery_failure"] = type(error).__name__
            return
        summary["artifacts"]["reconciliation_result"] = str(reconciliation_path)
        summary["reconciliation"] = {"status": result["status"], "review_required": result["review_required"],
                                     "fields": result["fields"]}
        summary["status"] = result["status"]

    def _record_stage(self, context, name, started, **details):
        """Stage name, time and decision only; never source text or values."""
        context["summary"].setdefault("stage_trace", []).append(
            {"stage": name, "seconds": round(time.perf_counter() - started, 3), **details})

    def stage_verify_source(self, context):
        """Bind the parser input to its recorded hash and keep the derived copy private."""
        source_metadata, summary = context["source_metadata"], context["summary"]
        if source_metadata is None:
            return
        source_pdf = context["source_pdf"]
        if sha256_bytes(source_pdf.read_bytes()) != source_metadata['parser_input']['sha256']:
            raise ValueError('Converted parser input changed before extraction')
        if source_metadata['conversion']['operation'] == 'pillow_image_to_pdf':
            # Keep this derived representation once, in the existing
            # private service run. Original images stay in their storage.
            retained = context["service_dir"] / 'parser-input.private.pdf'
            with retained.open('xb') as handle:
                handle.write(source_pdf.read_bytes())
            context["source_pdf"] = retained
            summary['source_pdf'] = str(retained)

    def stage_primary_extract(self, context):
        """Docling with RapidOCR."""
        parser, models_dir, summary = context["parser"], context["models_dir"], context["summary"]
        parser_fn = parser if parser is not None else (self._parser if self._parser is not None else parse_document)
        effective_models_dir = Path(models_dir).resolve() if models_dir is not None else self.models_dir
        parser_kwargs = {"input_pdf": context["source_pdf"], "output_parent": self.runs_dir,
                         "project_root": self.project_root, "verbose": context["verbose"]}
        if effective_models_dir is not None:
            parser_kwargs["models_dir"] = effective_models_dir
        # If a custom/mock parser is used, only pass models_dir if supported
        if parser is not None or self._parser is not None:
            if effective_models_dir is not None:
                try:
                    sig = inspect.signature(parser_fn)
                    if "models_dir" not in sig.parameters and not any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                    ):
                        parser_kwargs.pop("models_dir", None)
                except (ValueError, TypeError):
                    pass
        parser_run_dir, _ = parser_fn(**parser_kwargs)
        context["parser_run_dir"] = parser_run_dir
        context["document_json_path"] = parser_run_dir / "document.json"
        summary["artifacts"]["parser_run"] = str(parser_run_dir)
        summary["artifacts"]["document_json"] = str(context["document_json_path"])

    def stage_classify_and_map(self, context):
        """Deterministic mapping, classification and source-bound combination."""
        summary, document_json_path = context["summary"], context["document_json_path"]
        mapped_result_path = self._save_mapping(document_json_path, context["parser_run_dir"])
        summary["artifacts"]["mapped_result"] = str(mapped_result_path)
        extraction_result_path = self._save_extraction(mapped_result_path, context["document_id"])
        summary["artifacts"]["intermediate_extraction"] = str(extraction_result_path)
        classification_result_path = self._save_classification(document_json_path)
        summary["artifacts"]["classification_result"] = str(classification_result_path)
        combined_result_path = self._save_combined(extraction_result_path, classification_result_path)
        summary["artifacts"]["classified_extraction"] = str(combined_result_path)
        combined_payload = json.loads(combined_result_path.read_text(encoding="utf-8"))
        source_metadata = context["source_metadata"]
        if source_metadata is not None:
            from services.extraction.workflow.execution import bind_source_transformation
            pointer = bind_source_transformation(source_metadata, combined_payload, context["service_dir"], context["service_run_id"])
            combined_payload['provenance']['source_transformation'] = pointer
            write_json(combined_result_path, combined_payload)
            summary['artifacts']['image_preprocessing'] = pointer['path']
        summary["document_type"] = combined_payload.get("document_type")
        context["combined_result_path"] = combined_result_path
        context["combined_payload"] = combined_payload

    def stage_assess_field_evidence(self, context):
        """The document route, plus a per-field explanation. Never calls a model."""
        summary = context["summary"]
        routing_path, decision = self._save_routing(context["combined_result_path"], allow_vlm_inference=context["vlm_allowed"])
        summary["artifacts"]["routing_result"] = str(routing_path)
        summary["routing"] = {"decision": decision["decision"], "requested_fields": decision["requested_fields"],
                              "candidate_reasons": decision["candidate_reasons"], "blocking_reasons": decision["blocking_reasons"]}
        context["decision"] = decision
        try:
            assessment = assess_extraction(
                context["combined_payload"], self.project_root, allow_vlm_inference=context["vlm_allowed"],
                source_access_authorized=context["source_access_authorized"], source_path=context["source_path"],
                document=json.loads(context["document_json_path"].read_text(encoding="utf-8")))
        except Exception as error:
            # Parser-only output survives; recovery simply is not offered.
            assessment = {"policy_version": "extraction-assessment/2", "decision": "HUMAN_REVIEW_REQUIRED",
                          "reasons": ["ASSESSMENT_UNAVAILABLE:" + type(error).__name__], "fields": {},
                          "recovery_fields": [], "review_required": True, "ai_call_performed": False}
        summary["assessment"] = assessment
        context["assessment"] = assessment

    def stage_select_recovery(self, context):
        """Decide the outcome of the route and whether a model attempt is allowed."""
        summary, decision, assessment = context["summary"], context["decision"], context["assessment"]
        vlm_allowed = context["vlm_allowed"]
        context["recovery"] = None
        if decision["decision"] == "parser_only":
            summary["status"] = "parser_only_needs_review"
            summary["ai_assistance"] = {"attempted": False, "response_received": False, "status": "skipped_parser_only",
                                        "model": None, "route_decision": "parser_only", "prediction": None}
        elif decision["decision"] == "human_review":
            summary["status"] = "human_review_required"
            summary["ai_assistance"] = {"attempted": False, "response_received": False, "status": "skipped_human_review",
                                        "model": None, "route_decision": "human_review", "prediction": None}
            # The document still goes to a person. A recovery attempt can only
            # add reviewable candidates for the fields the assessment named.
            if vlm_allowed and context["source_access_authorized"] is True and assessment["recovery_fields"]:
                context["recovery"] = {"fields": assessment["recovery_fields"]}
                summary["selective_recovery"] = {"scope": "profile_fields/1", "requested_fields": assessment["recovery_fields"],
                                                 "profile": context["combined_payload"]["document_type"]["value"]}
                summary["ai_assistance"] = {"attempted": False, "response_received": False, "status": "failed",
                                            "prediction": None, "route_decision": "human_review"}
        elif decision["decision"] == "vlm_candidate":
            if vlm_allowed:
                context["recovery"] = {"fields": None}
            else:
                summary["status"] = "human_review_required"
                summary["routing"]["decision"] = "human_review"
                summary["routing"]["requested_fields"] = []
                blocker = {"code": "VLM_INFERENCE_DISABLED", "field": None}
                if blocker not in summary["routing"]["blocking_reasons"]:
                    summary["routing"]["blocking_reasons"].append(blocker)
                summary["ai_assistance"] = {"attempted": False, "response_received": False, "status": "skipped_human_review",
                                            "model": None, "route_decision": "human_review", "prediction": None}
        else:
            raise ValueError("Unsupported routing decision: " + repr(decision["decision"]))

    def stage_run_qwen_recovery(self, context):
        """One bounded local attempt. Any failure leaves the parser result in place."""
        summary, recovery = context["summary"], context["recovery"]
        keywords = {"inference_permission": context["vlm_allowed"], "source_access_authorized": context["source_access_authorized"],
                    "source_path": context["source_path"], "attempts": {}}
        if recovery["fields"] is not None:
            keywords["selected_fields"] = recovery["fields"]
        if context.get("previous_problems"):
            keywords["previous_problems"] = context["previous_problems"]
        try:
            context["selective"] = run_attempt(context["combined_result_path"], self.project_root, **keywords)
        except Exception as error:
            context["selective"] = None
            summary["ai_assistance"].update(status="failed", prediction=None)
            summary["status"] = "ai_attempt_failed_needs_review"
            summary["recovery_failure"] = type(error).__name__

    def stage_reconcile_candidates(self, context):
        """Check model values against the source and arbitrate. Parser values stay."""
        summary = context["summary"]
        if context.get("selective") is not None:
            selective_dir, selective_record = context["selective"]
            self._record_ai_outcome(summary, context["combined_result_path"], selective_dir, selective_record,
                                    context["source_path"], context["source_access_authorized"])
        before_retry = context.pop("before_retry", None)
        if before_retry is None:
            return
        # A retry may only improve on the first attempt. Otherwise the first result stands.
        if context.get("selective") is not None and (
                usable_candidates(summary.get("reconciliation")) > usable_candidates(before_retry["reconciliation"])):
            summary["targeted_retry"]["result"] = "retry_used"
        else:
            summary["targeted_retry"]["unused_run"] = (summary.get("artifacts") or {}).get("selective_run")
            summary.update(before_retry)
            summary["targeted_retry"]["result"] = "first_attempt_kept"

    def stage_precheck_validation(self, context):
        """Run the existing rules on a draft, without a database. The stored validation stays authoritative."""
        summary = context["summary"]
        context["retry_fields"] = []
        context["precheck_count"] = context.get("precheck_count", 0) + 1
        try:
            # Advisory only: nothing here, imports included, may stop an extraction.
            from services.extraction.contracts.accounting_contract import map_m2_result
            from services.extraction.contracts.module2_public_result import build_public_result
            from validation.documents.document_validation import evaluate_document
            draft = context["service_dir"] / "service-draft.json"
            write_json(draft, summary)
            _, public = build_public_result(draft, self.project_root,
                                            output_parent=context["service_dir"] / f"precheck-{context['precheck_count']}")
            mapped = map_m2_result(public, context["document_id"])
            kind = public["document_type"]["value"]
            result = evaluate_document({"kind": kind, "category": "accounts_payable"}, {"raw_json": public},
                                       mapped["fields"], mapped["line_items"])
            errors = result.get("errors") or []
            summary["validation_precheck"] = {
                "status": "findings" if errors else "clear",
                "findings": [{"rule": e.get("code"), "field": e.get("field"), "cause": e.get("cause")} for e in errors],
                "coverage_complete": result.get("coverage_complete"),
                "scope": "Rules needing the database, such as supplier and duplicate checks, run when the result is stored."}
        except Exception as error:
            summary["validation_precheck"] = {"status": "unavailable", "findings": [], "reason": type(error).__name__}
            return
        if context.get("retries_used", 0) >= MAX_TARGETED_RETRIES:
            return
        context["retry_fields"] = recoverable_fields(summary, time.perf_counter() - context["started"],
                                                     (context.get("selective") or (None, {}))[1].get("error_type"))

    def stage_targeted_retry(self, context):
        """Ask once more about the same fields. Reached at most once, so it cannot loop."""
        summary = context["summary"]
        context["retries_used"] = context.get("retries_used", 0) + 1
        context["before_retry"] = {key: json.loads(json.dumps(summary.get(key)))
                                   for key in ("status", "artifacts", "ai_assistance", "reconciliation")}
        asked = summary["selective_recovery"]["requested_fields"]
        context["recovery"] = {"fields": list(asked)}
        # Say what was wrong with the last answer. At temperature 0 the same request would return the same answer.
        reconciled = (summary.get("reconciliation") or {}).get("fields") or {}
        context["previous_problems"] = {name: reconciled[name]["field_rejection"] for name in asked
                                        if (reconciled.get(name) or {}).get("field_rejection")} or None
        summary["targeted_retry"] = {"needed_fields": list(context["retry_fields"]), "requested_fields": list(asked)}

    def stage_final_route(self, context):
        """Where this document goes next. Every route ends with a person; none approves anything."""
        summary = context["summary"]
        classification = context["combined_payload"]["document_type"]
        candidates = [name for name, field in ((summary.get("reconciliation") or {}).get("fields") or {}).items()
                      if field.get("resolution") == "candidate_for_review"]
        if classification.get("status") != "classified":
            route = "DOCUMENT_TYPE_REVIEW"
        elif summary["validation_precheck"]["findings"]:
            route = "ACCOUNTANT_ACTION_REQUIRED"
        elif candidates:
            route = "MODEL_CANDIDATES_FOR_REVIEW"
        else:
            route = "ACCOUNTANT_CONFIRMATION"
        summary["final_route"] = route

    def process_document(
        self,
        document_id: str,
        file_path: Path,
        verbose: bool = False,
        parser=None,
        allow_vlm_inference: bool | None = None,
        models_dir: Path | None = None,
        source_metadata: dict | None = None,
        source_path: Path | None = None,
        source_access_authorized: bool = False,
    ):
        """Process one PDF through the current Module 2 pipeline.

        This returns a service summary plus immutable artifact paths.

        It does NOT:
        - validate accounting rules;
        - persist a canonical accounting record;
        - approve an AI candidate;
        - approve an invoice.
        """

        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError(
                "document_id must be a non-empty string"
            )

        source_pdf = self._checked_path(
            file_path,
            "Input PDF",
        )

        vlm_allowed = (
            allow_vlm_inference
            if allow_vlm_inference is not None
            else self.allow_vlm_inference
        )
        if not isinstance(vlm_allowed, bool):
            raise ValueError('Inference permission must be an explicit boolean')

        started = time.perf_counter()

        service_run_id = (
            "document-intelligence-"
            + uuid4().hex
        )

        service_dir = (
            self.runs_dir
            / service_run_id
        )

        service_dir.mkdir(
            parents=True,
            exist_ok=False,
        )

        summary = {
            "schema_version": SERVICE_SCHEMA_VERSION,
            "service_run_id": service_run_id,
            "document_id": document_id,
            "created_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "source_pdf": str(source_pdf),
            "status": "processing",
            "review_required": True,
            "accounting_approval_performed": False,
            "allow_vlm_inference": vlm_allowed,
            "artifacts": {},
            "routing": None,
            "ai_assistance": {
                "attempted": False,
                "response_received": False,
                "status": "not_required",
                "prediction": None,
            },
            "reconciliation": None,
        }

        summary_path = (
            service_dir
            / "service-result.json"
        )

        context = {
            "summary": summary, "document_id": document_id, "source_pdf": source_pdf, "service_dir": service_dir,
            "service_run_id": service_run_id, "source_metadata": source_metadata, "parser": parser,
            "models_dir": models_dir, "verbose": verbose, "vlm_allowed": vlm_allowed,
            "source_path": source_path, "source_access_authorized": source_access_authorized, "started": started,
        }

        try:
            from services.extraction.workflow.extraction_graph import run_stages
            run_stages(self, context)

            summary["elapsed_seconds"] = round(
                time.perf_counter()
                - started,
                3,
            )

            write_json(
                summary_path,
                summary,
            )

            if verbose:
                print()
                print(
                    "Document Intelligence Service"
                )
                print(
                    "Document:",
                    document_id,
                )
                print(
                    "Status:",
                    summary["status"],
                )
                print(
                    "Routing:",
                    summary["routing"][
                        "decision"
                    ],
                )
                print(
                    "AI attempted:",
                    summary[
                        "ai_assistance"
                    ][
                        "attempted"
                    ],
                )
                print(
                    "Review required:",
                    summary[
                        "review_required"
                    ],
                )
                print(
                    "Saved:",
                    service_dir,
                )

            return summary_path, summary

        except Exception as error:
            summary["status"] = (
                "failed_needs_review"
            )

            summary["error"] = {
                "type": (
                    type(error).__name__
                ),
                "message": str(error),
            }

            summary["elapsed_seconds"] = round(
                time.perf_counter()
                - started,
                3,
            )

            write_json(
                summary_path,
                summary,
            )

            raise


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Run the complete current Module 2 "
            "document-intelligence pipeline."
        )
    )

    parser.add_argument(
        "input_pdf",
        type=Path,
    )

    parser.add_argument(
        "--document-id",
        required=True,
    )

    args = parser.parse_args()

    service = DocumentIntelligenceService()

    try:
        result_path, result = (
            service.process_document(
                document_id=args.document_id,
                file_path=args.input_pdf,
                verbose=True,
            )
        )

    except Exception as error:
        print(
            "Service failed:",
            type(error).__name__,
            str(error),
        )
        raise SystemExit(1)

    print()
    print(
        "Service result:",
        result_path,
    )

    print(
        "Final Module 2 status:",
        result["status"],
    )


if __name__ == "__main__":
    main()
