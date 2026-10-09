"""Single-entry orchestration service for Module 2 document intelligence.

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
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from services.extraction.baseline_parser import parse_document
from services.extraction.map_invoice import map_document
from services.extraction.extraction_v1 import ExtractionResultV1, adapt_result
from services.extraction.classify_document import classify_document
from services.extraction.combine_extraction import combine_files
from services.extraction.route_extraction import decide_route
from services.extraction.run_selective_qwen import run_attempt
from services.extraction.reconcile_selective import reconcile


SERVICE_SCHEMA_VERSION = "0.1.0-proposed"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class DocumentIntelligenceService:
    """Public Module 2 orchestration interface."""

    def __init__(self, project_root: Path | None = None):
        self.project_root = (
            Path(project_root)
            if project_root is not None
            else Path(__file__).resolve().parents[2]
        ).resolve()

        self.runs_dir = self.project_root / "runs"

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

        result = map_document(document)

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
    ):
        source = self._checked_path(
            classified_extraction_path,
            "Classified extraction",
        )

        raw = source.read_bytes()

        decision = decide_route(
            json.loads(raw)
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

    def process_document(
        self,
        document_id: str,
        file_path: Path,
        verbose: bool = False,
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

        try:
            #
            # 1. DOCLING PARSER
            #
            parser_run_dir, parser_record = (
                parse_document(
                    input_pdf=source_pdf,
                    output_parent=self.runs_dir,
                    project_root=self.project_root,
                    verbose=verbose,
                )
            )

            document_json_path = (
                parser_run_dir
                / "document.json"
            )

            summary["artifacts"]["parser_run"] = (
                str(parser_run_dir)
            )

            summary["artifacts"]["document_json"] = (
                str(document_json_path)
            )

            #
            # 2. DETERMINISTIC MAPPING
            #
            mapped_result_path = (
                self._save_mapping(
                    document_json_path,
                    parser_run_dir,
                )
            )

            summary["artifacts"]["mapped_result"] = (
                str(mapped_result_path)
            )

            #
            # 3. EXTRACTION V1 ADAPTER
            #
            extraction_result_path = (
                self._save_extraction(
                    mapped_result_path,
                    document_id,
                )
            )

            summary["artifacts"][
                "intermediate_extraction"
            ] = str(
                extraction_result_path
            )

            #
            # 4. CLASSIFICATION
            #
            classification_result_path = (
                self._save_classification(
                    document_json_path,
                )
            )

            summary["artifacts"][
                "classification_result"
            ] = str(
                classification_result_path
            )

            #
            # 5. COMBINE + SOURCE VERIFICATION
            #
            combined_result_path = (
                self._save_combined(
                    extraction_result_path,
                    classification_result_path,
                )
            )

            summary["artifacts"][
                "classified_extraction"
            ] = str(
                combined_result_path
            )

            combined_payload = json.loads(
                combined_result_path.read_text(
                    encoding="utf-8"
                )
            )

            summary["document_type"] = (
                combined_payload.get(
                    "document_type"
                )
            )

            #
            # 6. ROUTING
            #
            routing_path, decision = (
                self._save_routing(
                    combined_result_path
                )
            )

            summary["artifacts"][
                "routing_result"
            ] = str(
                routing_path
            )

            summary["routing"] = {
                "decision": decision[
                    "decision"
                ],
                "requested_fields": decision[
                    "requested_fields"
                ],
                "candidate_reasons": decision[
                    "candidate_reasons"
                ],
                "blocking_reasons": decision[
                    "blocking_reasons"
                ],
            }

            #
            # 7A. PARSER-ONLY PATH
            #
            if decision["decision"] == "parser_only":
                summary["status"] = (
                    "parser_only_needs_review"
                )

                summary["ai_assistance"] = {
                    "attempted": False,
                    "response_received": False,
                    "status": "skipped_parser_only",
                    "prediction": None,
                }

            #
            # 7B. HUMAN-REVIEW BLOCKER PATH
            #
            elif decision["decision"] == "human_review":
                summary["status"] = (
                    "human_review_required"
                )

                summary["ai_assistance"] = {
                    "attempted": False,
                    "response_received": False,
                    "status": (
                        "skipped_human_review"
                    ),
                    "prediction": None,
                }

            #
            # 7C. SELECTIVE VLM PATH
            #
            elif decision["decision"] == "vlm_candidate":
                selective_dir, selective_record = (
                    run_attempt(
                        combined_result_path,
                        self.project_root,
                    )
                )

                summary["artifacts"][
                    "selective_run"
                ] = str(
                    selective_dir
                )

                summary["ai_assistance"][
                    "attempted"
                ] = selective_record.get(
                    "ai_request_attempted",
                    False,
                )

                summary["ai_assistance"][
                    "response_received"
                ] = selective_record.get(
                    "ai_response_received",
                    False,
                )

                summary["ai_assistance"][
                    "status"
                ] = selective_record.get(
                    "status"
                )

                if (
                    selective_record.get(
                        "status"
                    )
                    == "response_structure_valid_needs_review"
                ):
                    prediction_path = (
                        selective_dir
                        / "prediction.json"
                    )

                    prediction = json.loads(
                        prediction_path.read_text(
                            encoding="utf-8"
                        )
                    )

                    summary["ai_assistance"][
                        "prediction"
                    ] = prediction

                    reconciliation_path, reconciliation_result = (
                        reconcile(
                            combined_result_path,
                            selective_dir,
                            self.runs_dir,
                        )
                    )

                    summary["artifacts"][
                        "reconciliation_result"
                    ] = str(
                        reconciliation_path
                    )

                    summary["reconciliation"] = {
                        "status": (
                            reconciliation_result[
                                "status"
                            ]
                        ),
                        "review_required": (
                            reconciliation_result[
                                "review_required"
                            ]
                        ),
                        "fields": (
                            reconciliation_result[
                                "fields"
                            ]
                        ),
                    }

                    summary["status"] = (
                        reconciliation_result[
                            "status"
                        ]
                    )

                else:
                    #
                    # Provider/model/response failure must not
                    # silently become an approved result.
                    #
                    summary["status"] = (
                        "ai_attempt_failed_needs_review"
                    )

            else:
                raise ValueError(
                    "Unsupported routing decision: "
                    + repr(
                        decision["decision"]
                    )
                )

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
