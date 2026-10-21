"""Transactional persistence for extraction worker results."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

from database.connection import connect
from services.extraction.accounting_contract import map_m2_result


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
    mapped = map_m2_result(public_result, document_id)
    if (public_result.get('provenance') or {}).get('service_run_id') != service_run_id:
        raise ValueError('Extraction run identity does not match its artifact.')

    routing = public_result.get("routing") or {}
    routing_decision = routing.get("decision") or "human_review"
    if routing_decision not in {"parser_only", "vlm_candidate", "human_review"}:
        routing_decision = "human_review"

    with connect() as db:
        document = db.execute('SELECT id,status FROM documents WHERE id=%s FOR UPDATE', (document_id,)).fetchone()
        if not document:
            raise ValueError('Document not found.')
        if document['status'] in {'completed', 'standardised'}:
            raise ValueError('Approved document snapshots cannot be reprocessed in place.')
        if db.execute('SELECT 1 FROM standardised_records WHERE document_id=%s LIMIT 1', (document_id,)).fetchone():
            raise ValueError('An approved historical record exists; use an amendment.')
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

        # Re-extraction must not silently hide reviewed values/history. A future
        # amendment workflow can explicitly carry them across extraction runs.
        if db.execute("""SELECT 1 FROM field_predictions fp JOIN raw_extractions re ON re.id=fp.extraction_id
            WHERE fp.document_id=%s AND re.is_current=true AND fp.corrected_at IS NOT NULL LIMIT 1""",
            (document_id,)).fetchone() or db.execute(
                'SELECT 1 FROM line_item_corrections WHERE document_id=%s LIMIT 1', (document_id,)).fetchone():
            raise ValueError('Reviewed corrections exist; re-extraction requires an explicit amendment.')

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

        for field in mapped['fields']:
            db.execute(
                """INSERT INTO field_predictions
                   (extraction_id, document_id, field_key, field_label, extraction_state, resolution,
                    raw_value, normalized_value, current_value, confidence, evidence, candidates,
                    extraction_method, model_name, model_version, schema_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (extraction_id, document_id, field['field_key'], field['field_label'], field['extraction_state'],
                 field['resolution'], field['raw_value'], field['normalized_value'], field['current_value'],
                 field['confidence'], json.dumps(field['evidence']), json.dumps(field['candidates']),
                 field['extraction_method'], field['model_name'], field['model_version'], field['schema_version']),
            )

        for row in mapped['line_items']:
            def decimal(name: str):
                return Decimal(row[name]) if row[name] is not None else None
            db.execute(
                """INSERT INTO line_items
                   (extraction_id, document_id, line_number, description, quantity, unit_price, amount, field_states, evidence, candidates)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (extraction_id, document_id, row['line_number'], row['description'], decimal('quantity'),
                 decimal('unit_price'), decimal('amount'), json.dumps(row['field_states']),
                 json.dumps(row['evidence']), json.dumps(row['candidates'])),
            )

        db.execute("UPDATE documents SET status='extracted', content_revision=content_revision+1, processing_seconds=EXTRACT(EPOCH FROM (now() - uploaded_at)), processing_completed_at=now(), updated_at=now() WHERE id=%s", (document_id,))
        classification = mapped['document_kind']
        if classification.get('status') == 'classified' and classification.get('value') in {'invoice', 'bill', 'receipt', 'credit_note', 'debit_note'}:
            db.execute('UPDATE documents SET kind=%s WHERE id=%s AND tenant_id=%s',
                       (classification['value'], document_id, job['tenant_id']))
        from validation.document_validation import validate_document
        validate_document(db, document_id)
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
