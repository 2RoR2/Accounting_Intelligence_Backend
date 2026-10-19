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
from typing import Any, Callable, Dict, Optional

from services.extraction.document_intelligence_service import DocumentIntelligenceService
from services.extraction.module2_public_result import build_public_result


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

    service = DocumentIntelligenceService(
        project_root=project_root,
        parser=parser,
        allow_vlm_inference=allow_vlm_inference,
        models_dir=models_dir,
    )

    # 1. Run DocumentIntelligenceService
    try:
        summary_path, summary = service.process_document(
            document_id=document_id,
            file_path=path,
            verbose=verbose,
            allow_vlm_inference=allow_vlm_inference,
            models_dir=models_dir,
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
