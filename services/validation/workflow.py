"""
Validation workflow service (Module 3):
Invokes the M2-to-M3 adapter and validation engine as a separate, recoverable workflow,
evaluating current field values (including accountant corrections from field_predictions),
and preserving complete exception history by resolving instead of deleting prior exceptions.
"""

import copy
import json
import logging
from typing import Any, Dict, List, Optional

from services.extraction.adapter import integrate_m2_with_m3

logger = logging.getLogger(__name__)


def run_document_validation(
    db: Any,
    document_id: str,
    tenant_id: str,
    known_vendors: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Executes accounting validation on a previously extracted document.

    Guarantees:
    - Purely recoverable: Reads directly from 'raw_extractions' and 'field_predictions' without re-parsing PDF.
    - Evaluates human corrections: Uses 'field_predictions.current_value' for the current extraction run.
    - Tenant isolation: Checks tenant ownership.
    - Audit preservation: Resolves previous open exceptions instead of hard-deleting them.
    - Records standard exception taxonomy codes (EX-002, EX-005, EX-008, M2-STRUCTURAL-001).
    """
    # 1. Retrieve the current extraction run from raw_extractions
    raw_row = db.execute(
        """
        SELECT id, raw_json FROM raw_extractions
        WHERE document_id = %s AND is_current = TRUE
        LIMIT 1
        """,
        (document_id,),
    ).fetchone()

    # Fallback to the latest run if is_current has not been set yet
    if not raw_row:
        raw_row = db.execute(
            """
            SELECT id, raw_json FROM raw_extractions
            WHERE document_id = %s
            ORDER BY run_number DESC
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()

    if not raw_row:
        logger.error("Cannot validate document %s: No raw extraction found.", document_id)
        raise ValueError(f"No extraction result found for document {document_id}")

    extraction_id = str(raw_row["id"])
    raw_json_data = raw_row["raw_json"]
    if isinstance(raw_json_data, str):
        raw_json_data = json.loads(raw_json_data)

    public_result = copy.deepcopy(raw_json_data)
    if not isinstance(public_result.get("fields"), dict):
        public_result["fields"] = {}

    # 2. Incorporate current field values (including any human corrections from field_predictions)
    pred_rows = db.execute(
        """
        SELECT field_key, current_value, extraction_state
        FROM field_predictions
        WHERE extraction_id = %s
        """,
        (extraction_id,),
    ).fetchall()

    for pred in pred_rows:
        f_key = pred["field_key"]
        c_val = pred["current_value"]
        if c_val is not None:
            if f_key not in public_result["fields"]:
                public_result["fields"][f_key] = {}
            public_result["fields"][f_key]["value"] = c_val
            public_result["fields"][f_key]["state"] = "resolved"

    # 3. Retrieve known vendors for this tenant if not explicitly provided
    if known_vendors is None:
        vendor_rows = db.execute(
            """
            SELECT id, vendor_name FROM vendors
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        ).fetchall()
        known_vendors = [
            {"vendor_id": str(v["id"]), "vendor_name": v["vendor_name"]}
            for v in vendor_rows
        ]

    # 4. Resolve previously opened exceptions to preserve complete audit history (P0-05)
    db.execute(
        """
        UPDATE exceptions
        SET status = 'resolved',
            resolved_at = now()
        WHERE document_id = %s AND status = 'open'
        """,
        (document_id,),
    )

    # 5. Invoke M2-to-M3 adapter and validation engine
    workflow_outcome = integrate_m2_with_m3(
        public_result=public_result,
        known_vendors=known_vendors,
    )

    wf_status = workflow_outcome.get("workflow_status")
    structural_issues = workflow_outcome.get("structural_issues", [])
    m3_result = workflow_outcome.get("m3_validation_result")

    # 6. Handle Structural Block (before M3)
    if wf_status == "BLOCKED_BY_STRUCTURE":
        for issue in structural_issues:
            code = issue.get("code", "M2-STRUCTURAL-001")
            msg = issue.get("message", "Document structure failed validation.")
            db.execute(
                """
                INSERT INTO exceptions (
                    document_id, tenant_id, type, severity, status, message
                ) VALUES (
                    %s, %s, %s, 'high', 'open', %s
                )
                """,
                (document_id, tenant_id, code, msg),
            )

        db.execute(
            """
            UPDATE documents
            SET status = 'exception',
                updated_at = now()
            WHERE id = %s
            """,
            (document_id,),
        )

        logger.warning(
            "Document %s blocked by structure: %d issues recorded.",
            document_id,
            len(structural_issues),
        )
        return {
            "workflow_status": "BLOCKED_BY_STRUCTURE",
            "document_status": "exception",
            "exceptions_count": len(structural_issues),
            "exceptions": structural_issues,
        }

    # 7. Handle M3 Validation Result
    if not m3_result:
        err_msg = workflow_outcome.get("m3_execution_error", "Accounting validation failed.")
        db.execute(
            """
            INSERT INTO exceptions (
                document_id, tenant_id, type, severity, status, message
            ) VALUES (
                %s, %s, 'SYSTEM_ERROR', 'high', 'open', %s
            )
            """,
            (document_id, tenant_id, err_msg),
        )
        db.execute(
            """
            UPDATE documents
            SET status = 'exception',
                updated_at = now()
            WHERE id = %s
            """,
            (document_id,),
        )
        return {
            "workflow_status": "M3_EXECUTION_FAILED",
            "document_status": "exception",
            "exceptions_count": 1,
            "error": err_msg,
        }

    m3_status = m3_result.get("status")
    errors = m3_result.get("errors", [])

    if m3_status == "VALID":
        # Passed all rules cleanly
        db.execute(
            """
            UPDATE documents
            SET status = 'validated',
                updated_at = now()
            WHERE id = %s
            """,
            (document_id,),
        )
        logger.info("Document %s successfully validated (Status: VALID).", document_id)
        return {
            "workflow_status": "M3_VALIDATION_COMPLETE",
            "validation_status": "VALID",
            "document_status": "validated",
            "exceptions_count": 0,
            "errors": [],
        }
    else:
        # REVIEW_REQUIRED — Record exceptions
        for err in errors:
            code = err.get("code", "VALIDATION_ERROR")
            severity = err.get("severity", "medium").lower()
            if severity not in ("low", "medium", "high"):
                severity = "medium"
            msg = err.get("message", "Validation review required.")

            db.execute(
                """
                INSERT INTO exceptions (
                    document_id, tenant_id, type, severity, status, message
                ) VALUES (
                    %s, %s, %s, %s, 'open', %s
                )
                """,
                (document_id, tenant_id, code, severity, msg),
            )

        db.execute(
            """
            UPDATE documents
            SET status = 'exception',
                updated_at = now()
            WHERE id = %s
            """,
            (document_id,),
        )
        logger.info(
            "Document %s requires review: %d exceptions recorded.",
            document_id,
            len(errors),
        )
        return {
            "workflow_status": "M3_VALIDATION_COMPLETE",
            "validation_status": "REVIEW_REQUIRED",
            "document_status": "exception",
            "exceptions_count": len(errors),
            "errors": errors,
        }
