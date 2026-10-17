"""
Persistence service for Module 2 extraction results.
Persists versioned raw JSONB runs, individual field predictions uniquely keyed by (extraction_id, field_key),
canonical line items with nullable numeric fields, and enforces accountant correction preservation
via the authoritative database function 'switch_current_extraction(UUID)'.
"""

import json
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from services.extraction.adapter import convert_numeric

logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "supplier_name": "Supplier Name",
    "invoice_number": "Invoice Number",
    "invoice_date": "Invoice Date",
    "due_date": "Due Date",
    "currency": "Currency",
    "subtotal": "Subtotal",
    "tax_amount": "Tax Amount",
    "total_amount": "Total Amount",
}

VALID_EXTRACTION_STATES = {
    "resolved",
    "missing",
    "ambiguous",
    "conflict",
    "unsupported",
    "unreadable",
}

VALID_RESOLUTIONS = {
    "parser_preserved",
    "candidate_for_review",
    "unresolved_missing",
}


def persist_extraction_result(
    db: Any,
    document_id: str,
    tenant_id: str,
    public_result: Dict[str, Any],
    processing_seconds: float = 0.0,
    pending_job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Persists the complete M2 public result into relational tables matching Aleeya's canonical schema:
    1. 'raw_extractions': Versioned per-run audit record (run_number = max + 1, is_current = FALSE initially).
    2. 'field_predictions': Per-field state, evidence, candidates, and nullable genuine confidence.
       CRITICAL (P0-04): No fabricated confidence (1.0/0.5) or synthetic page numbers.
    3. 'line_items': Extracted tabular rows with nullable Decimals.
       CRITICAL (P0-04): Unknown quantities/amounts remain NULL; never manufactured as 1 or 0.00.
    4. Calls 'switch_current_extraction(extraction_id)' to flip is_current=TRUE and carry forward
       previous accountant corrections safely in one transaction.
    5. 'documents': Advances status to 'extracted'.
    """
    # 1. Determine run_number
    max_run_row = db.execute(
        "SELECT COALESCE(MAX(run_number), 0) AS max_run FROM raw_extractions WHERE document_id = %s",
        (document_id,),
    ).fetchone()
    next_run_number = (max_run_row["max_run"] if max_run_row else 0) + 1

    # 2. Extract routing, model, and schema metadata
    routing_data = public_result.get("routing") or {}
    routing_decision = routing_data.get("decision") or "parser_only"
    if routing_decision not in ("parser_only", "vlm_candidate", "human_review"):
        routing_decision = "parser_only"

    service_run_id = public_result.get("service_run_id")
    schema_version = public_result.get("schema_version", "0.1.0-proposed")
    model_name = public_result.get("provenance", {}).get("model_name") or "docling-2.132.0"
    model_version = public_result.get("provenance", {}).get("model_version")

    raw_json_str = json.dumps(public_result, ensure_ascii=False)

    # 3. Insert into raw_extractions
    raw_row = db.execute(
        """
        INSERT INTO raw_extractions (
            document_id, run_number, is_current, routing_decision,
            service_run_id, schema_version, pending_job_id, raw_json
        ) VALUES (
            %s, %s, FALSE, %s, %s, %s, %s, %s::jsonb
        )
        RETURNING id, run_number, created_at
        """,
        (
            document_id,
            next_run_number,
            routing_decision,
            service_run_id,
            schema_version,
            pending_job_id,
            raw_json_str,
        ),
    ).fetchone()

    extraction_id = str(raw_row["id"])

    # 4. Persist field predictions uniquely keyed by (extraction_id, field_key)
    fields = public_result.get("fields", {})
    fields_saved = 0

    for field_key, field_data in fields.items():
        if not isinstance(field_data, dict):
            continue

        raw_val = field_data.get("raw_text") or field_data.get("value")
        norm_val = field_data.get("value")
        state = field_data.get("state", "missing")
        if state not in VALID_EXTRACTION_STATES:
            # Map legacy 'unresolved' to 'missing'
            state = "missing" if state == "unresolved" else "ambiguous"

        resolution = field_data.get("resolution")
        if resolution not in VALID_RESOLUTIONS:
            resolution = None

        # CRITICAL: If resolution is candidate_for_review, current_value must stay NULL until a person corrects it
        if resolution == "candidate_for_review":
            curr_val = None
        elif state == "resolved":
            curr_val = norm_val
        else:
            curr_val = None

        # CRITICAL (P0-04): Do NOT invent confidence! Nullable when extractor gives no numeric score.
        raw_conf = field_data.get("confidence")
        conf_decimal = None
        if raw_conf is not None:
            try:
                c_val = Decimal(str(raw_conf))
                if Decimal("0.0") <= c_val <= Decimal("1.0"):
                    conf_decimal = c_val
            except Exception:
                conf_decimal = None

        label = FIELD_LABELS.get(field_key, field_key.replace("_", " ").title())
        evidence_list = field_data.get("evidence", [])
        if not isinstance(evidence_list, list):
            evidence_list = []

        candidates_list = field_data.get("candidates", [])
        if not isinstance(candidates_list, list):
            candidates_list = []

        method = field_data.get("extraction_method", "PARSER")
        if method not in ("VLM", "OCR", "PARSER", "MANUAL_CORRECTION"):
            method = "PARSER"

        db.execute(
            """
            INSERT INTO field_predictions (
                extraction_id, document_id, field_key, field_label,
                extraction_state, resolution, raw_value, normalized_value, current_value,
                confidence, evidence, candidates, extraction_method, model_name,
                model_version, schema_version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s
            )
            """,
            (
                extraction_id,
                document_id,
                field_key,
                label,
                state,
                resolution,
                str(raw_val) if raw_val is not None else None,
                str(norm_val) if norm_val is not None else None,
                str(curr_val) if curr_val is not None else None,
                conf_decimal,
                json.dumps(evidence_list),
                json.dumps(candidates_list),
                method,
                model_name,
                model_version,
                "1.0",
            ),
        )
        fields_saved += 1

    # 5. Persist line items uniquely keyed by (extraction_id, line_number)
    line_items = public_result.get("line_items", [])
    lines_saved = 0

    for idx, item in enumerate(line_items):
        if not isinstance(item, dict):
            continue

        line_number = idx + 1
        desc = item.get("description", {}).get("value")

        # CRITICAL (P0-04): Missing or malformed amounts remain NULL; NEVER manufacture 1 or 0.00
        qty_raw = item.get("quantity", {}).get("value")
        price_raw = item.get("unit_price", {}).get("value")
        amount_raw = item.get("line_total", {}).get("value") or item.get("amount", {}).get("value")

        qty = convert_numeric(qty_raw)
        unit_price = convert_numeric(price_raw)
        amount = convert_numeric(amount_raw)

        # Capture cell field_states, evidence, and candidates
        cell_states = {}
        cell_evidence = {}
        cell_candidates = {}

        for cell_col in ("description", "quantity", "unit_price", "amount", "line_total"):
            cell_data = item.get(cell_col)
            if isinstance(cell_data, dict):
                col_name = "amount" if cell_col == "line_total" else cell_col
                if "state" in cell_data:
                    cell_states[col_name] = cell_data["state"]
                if "evidence" in cell_data:
                    cell_evidence[col_name] = cell_data["evidence"]
                if "candidates" in cell_data:
                    cell_candidates[col_name] = cell_data["candidates"]

        db.execute(
            """
            INSERT INTO line_items (
                extraction_id, document_id, line_number, description,
                quantity, unit_price, amount, field_states, evidence, candidates
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb
            )
            """,
            (
                extraction_id,
                document_id,
                line_number,
                desc,
                qty,
                unit_price,
                amount,
                json.dumps(cell_states),
                json.dumps(cell_evidence),
                json.dumps(cell_candidates),
            ),
        )
        lines_saved += 1

    # 6. Switch current extraction and carry forward previous accountant corrections
    carried_corrections = 0
    switch_res = db.execute(
        "SELECT switch_current_extraction(%s) AS carried",
        (extraction_id,),
    ).fetchone()
    if switch_res and switch_res["carried"] is not None:
        carried_corrections = int(switch_res["carried"])

    # 7. Update document status to 'extracted'
    db.execute(
        """
        UPDATE documents
        SET status = 'extracted',
            processing_completed_at = now(),
            processing_seconds = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (processing_seconds if processing_seconds > 0 else None, document_id),
    )

    logger.info(
        "Persisted extraction run %d (id=%s) for document %s: %d fields, %d lines, %d corrections carried forward.",
        next_run_number,
        extraction_id,
        document_id,
        fields_saved,
        lines_saved,
        carried_corrections,
    )

    return {
        "extraction_id": extraction_id,
        "document_id": document_id,
        "run_number": next_run_number,
        "fields_saved": fields_saved,
        "lines_saved": lines_saved,
        "carried_corrections": carried_corrections,
        "routing_decision": routing_decision,
    }
