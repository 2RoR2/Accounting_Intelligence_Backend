"""Transactional persistence for extraction worker results."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from database.connection import connect


def persist_extraction_result(document_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Persist one extraction run and mark its job succeeded atomically.

    The caller must only invoke this after the provider/extraction pipeline has
    produced a structurally valid public result. A duplicate service_run_id is
    treated as an idempotent replay and does not create duplicate rows.
    """
    service_run_id = result.get("service_run_id")
    public_path = result.get("public_result_path")
    if not service_run_id or not public_path:
        raise ValueError("Extraction result is missing its immutable run metadata.")

    with open(public_path, "r", encoding="utf-8") as handle:
        public_result = json.load(handle)
    if public_result.get("document_id") != document_id:
        raise ValueError("Extraction result document identity does not match the job.")

    routing = public_result.get("routing") or {}
    routing_decision = routing.get("decision") or "human_review"
    if routing_decision not in {"parser_only", "vlm_candidate", "human_review"}:
        routing_decision = "human_review"

    with connect() as db:
        job = db.execute(
            """SELECT id, tenant_id, status FROM pending_jobs
               WHERE document_id=%s AND status IN ('pending','dispatched','processing')
               ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
            (document_id,),
        ).fetchone()
        if not job:
            existing = db.execute(
                "SELECT id, tenant_id FROM pending_jobs WHERE document_id=%s ORDER BY created_at DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            if not existing:
                raise ValueError("No processing job exists for the document.")
            job = existing

        existing_run = db.execute(
            "SELECT id FROM raw_extractions WHERE service_run_id=%s",
            (service_run_id,),
        ).fetchone()
        if existing_run:
            db.execute(
                "UPDATE pending_jobs SET status='succeeded', finished_at=COALESCE(finished_at, now()), last_error=NULL, updated_at=now() WHERE id=%s",
                (job["id"],),
            )
            return {"extraction_id": str(existing_run["id"]), "job_id": str(job["id"]), "idempotent": True}

        run_number = db.execute(
            "SELECT COALESCE(MAX(run_number), 0) + 1 AS next_run FROM raw_extractions WHERE document_id=%s",
            (document_id,),
        ).fetchone()["next_run"]
        extraction_id = uuid4()
        db.execute("UPDATE raw_extractions SET is_current=FALSE WHERE document_id=%s AND is_current=TRUE", (document_id,))
        db.execute(
            """INSERT INTO raw_extractions
               (id, document_id, run_number, is_current, routing_decision, service_run_id, schema_version, pending_job_id, raw_json)
               VALUES (%s,%s,%s,TRUE,%s,%s,%s,%s,%s)""",
            (extraction_id, document_id, run_number, routing_decision, service_run_id,
             public_result.get("schema_version"), job["id"], json.dumps(public_result)),
        )

        for field_key, field in (public_result.get("fields") or {}).items():
            field = field if isinstance(field, dict) else {}
            state = field.get("state") if field.get("state") in {"resolved", "missing", "ambiguous", "conflict", "unsupported", "unreadable"} else "unreadable"
            resolution = field.get("resolution")
            if resolution not in {"parser_preserved", "candidate_for_review", "unresolved_missing"}:
                resolution = "unresolved_missing" if state == "missing" else "parser_preserved"
            db.execute(
                """INSERT INTO field_predictions
                   (extraction_id, document_id, field_key, field_label, extraction_state, resolution,
                    raw_value, normalized_value, current_value, confidence, evidence, candidates,
                    extraction_method, model_name, model_version, schema_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (extraction_id, document_id, field_key, field_key.replace("_", " ").title(), state, resolution,
                 field.get("raw_text"), field.get("value"),
                 None if resolution == "candidate_for_review" else field.get("value"),
                 field.get("confidence"), json.dumps(field.get("evidence") or []), json.dumps(field.get("candidates") or []),
                 str(field.get("method") or "PARSER").upper() if str(field.get("method") or "PARSER").upper() in {"VLM", "OCR", "PARSER", "MANUAL_CORRECTION"} else "PARSER",
                 field.get("model_name"), field.get("model_version"), public_result.get("schema_version") or "1.0"),
            )

        for index, row in enumerate(public_result.get("line_items") or [], start=1):
            row = row if isinstance(row, dict) else {}
            def value(name: str):
                cell = row.get(name)
                return cell.get("value") if isinstance(cell, dict) else None
            def decimal(name: str):
                raw = value(name)
                if raw in (None, ""):
                    return None
                try:
                    return Decimal(str(raw).replace(",", ""))
                except (InvalidOperation, ValueError):
                    return None
            db.execute(
                """INSERT INTO line_items
                   (extraction_id, document_id, line_number, description, quantity, unit_price, amount, field_states, evidence, candidates)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (extraction_id, document_id, index, value("description"), decimal("quantity"),
                 decimal("unit_price"), decimal("line_total"), json.dumps({}), json.dumps({}), json.dumps({})),
            )

        db.execute("UPDATE documents SET status='extracted', processing_seconds=EXTRACT(EPOCH FROM (now() - uploaded_at)), processing_completed_at=now(), updated_at=now() WHERE id=%s", (document_id,))
        db.execute(
            "UPDATE pending_jobs SET status='succeeded', finished_at=now(), last_error=NULL, updated_at=now() WHERE id=%s",
            (job["id"],),
        )
        return {"extraction_id": str(extraction_id), "job_id": str(job["id"]), "idempotent": False}


def mark_extraction_job_failed(document_id: str, message: str) -> None:
    with connect() as db:
        db.execute(
            """UPDATE pending_jobs SET status=CASE WHEN attempt_count < max_attempts THEN 'pending' ELSE 'failed' END,
               last_error=%s, updated_at=now(), finished_at=CASE WHEN attempt_count >= max_attempts THEN now() ELSE NULL END
               WHERE document_id=%s AND status IN ('pending','dispatched','processing')""",
            (message[:2000], document_id),
        )
        db.execute("UPDATE documents SET status='failed', processing_completed_at=now(), updated_at=now() WHERE id=%s", (document_id,))
