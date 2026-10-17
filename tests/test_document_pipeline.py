"""
Comprehensive integration tests for the database-backed document workflow:
1. Safe versioned migration system and rollback.
2. Document management, private containment, validation, and atomic outbox creation.
3. Trusted document resolution with tenant scoping and SHA-256 integrity verification.
4. M2 extraction persistence matching Aleeya's canonical schema and switch_current_extraction.
5. Recoverable M3 validation workflow evaluating accountant corrections and preserving exceptions.
6. Dispatcher leasing with SELECT FOR UPDATE SKIP LOCKED, stable RQ job IDs, and crash sweeper.
7. Worker claim fencing, discrete transaction boundaries, and end-to-end execution.
8. Authenticated tenant-isolated REST API endpoints (upload, download, result, patch correction, approve).
9. Full PostgreSQL integration tests against live cluster verifying canonical triggers, functions, and constraints.
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import app as api
from auth.security import create_access_token
from database.migrations import (
    MIGRATIONS_DIR,
    apply_migrations,
    ensure_schema_migrations_table,
    get_applied_migrations,
    get_available_migrations,
    rollback_latest_migration,
)
from infrastructure.queue.dispatcher import dispatch_pending_jobs, sweep_stale_jobs
from infrastructure.queue.jobs import (
    ExtractionExecutionError,
    ExtractionJobError,
    ExtractionResolutionError,
    ExtractionUnauthorizedError,
    process_document_extraction_job,
    reset_test_extraction_environment,
    set_test_extraction_environment,
)
from services.documents.manager import (
    MAX_FILE_SIZE_BYTES,
    get_document_storage_root,
    save_uploaded_document,
    validate_pdf_content,
)
from services.documents.resolver import resolve_trusted_document_path
from services.extraction.persistence import persist_extraction_result
from services.validation.workflow import run_document_validation


SYN_001_PATH = (
    Path(__file__).resolve().parent
    / "extraction"
    / "data"
    / "development"
    / "synthetic_invoice_001.pdf"
)


class MockDbResult:
    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class MemoryDatabase:
    """
    In-memory stateful mock database simulating PostgreSQL semantics for
    document management, pending_jobs, canonical extractions, field predictions,
    switch_current_extraction, validations, and API auth.
    """

    def __init__(self):
        self.schema_migrations = []
        self.tenants = {}
        self.users = {}
        self.web_sessions = {}
        self.documents = {}
        self.pending_jobs = {}
        self.raw_extractions = {}
        self.field_predictions = {}
        self.field_corrections = []
        self.line_items = {}
        self.exceptions = []
        self.vendors = []
        self.standardised_records = {}
        self.audit_logs = []
        self._doc_seq = 100001
        self._rec_seq = 10001

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def execute(self, query: str, params: tuple = ()):
        norm_query = " ".join(query.strip().upper().split())

        # Schema migrations table creation
        if "CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATIONS" in norm_query:
            return MockDbResult()

        if "SELECT NAME FROM SCHEMA_MIGRATIONS" in norm_query:
            return MockDbResult([{"name": m} for m in sorted(self.schema_migrations)])

        if "INSERT INTO SCHEMA_MIGRATIONS" in norm_query:
            self.schema_migrations.append(params[0])
            return MockDbResult()

        if "DELETE FROM SCHEMA_MIGRATIONS WHERE NAME = %S" in norm_query:
            self.schema_migrations = [m for m in self.schema_migrations if m != params[0]]
            return MockDbResult()

        # Sweeper query (has no parameters)
        if "UPDATE PENDING_JOBS" in norm_query and "PROCESSING_EXPIRES_AT < NOW()" in norm_query:
            swept = []
            for j in self.pending_jobs.values():
                if j["status"] in ("processing", "dispatched") and j.get("is_stale", False):
                    if j["attempt_count"] >= j["max_attempts"]:
                        j["status"] = "failed"
                        j["finished_at"] = "2026-10-16T12:05:00+00:00"
                    else:
                        j["status"] = "pending"
                    j["claim_token"] = None
                    j["dispatch_token"] = None
                    j["last_error"] = "Job lease timed out (dispatch lost or worker crash/OOM detected)."
                    swept.append(j)
            return MockDbResult(swept)

        # Raw DDL or migration script execution without parameters
        if not params:
            return MockDbResult()

        # Session user auth query
        if "FROM WEB_SESSIONS S JOIN USERS U" in norm_query:
            sess_id, user_id = str(params[0]), str(params[1])
            user = self.users.get(user_id)
            tenant = self.tenants.get(str(user["tenant_id"])) if user and user.get("tenant_id") else None
            if user:
                row = dict(user)
                row["tenant_code"] = tenant["tenant_code"] if tenant else None
                row["tenant_status"] = tenant["status"] if tenant else None
                return MockDbResult([row])
            return MockDbResult([])

        # Documents insertion
        if "INSERT INTO DOCUMENTS" in norm_query:
            doc_id = str(params[0])
            tenant_id = str(params[1])
            owner_id = str(params[2])
            orig_name = str(params[3])
            kind = str(params[4])
            category = str(params[5])
            storage_path = str(params[6])
            file_size = int(params[7])
            file_hash = str(params[8])
            code = f"{kind.upper()}_{self._doc_seq}"
            self._doc_seq += 1

            row = {
                "id": doc_id,
                "document_code": code,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "original_filename": orig_name,
                "kind": kind,
                "category": category,
                "storage_path": storage_path,
                "mime_type": "application/pdf",
                "file_size_bytes": file_size,
                "file_hash_sha256": file_hash,
                "status": "uploaded",
                "uploaded_at": "2026-10-16T12:00:00+00:00",
                "updated_at": "2026-10-16T12:00:00+00:00",
                "processing_seconds": None,
            }
            self.documents[doc_id] = row
            return MockDbResult([dict(row)])

        # Pending jobs insertion
        if "INSERT INTO PENDING_JOBS" in norm_query:
            doc_id = str(params[0])
            tenant_id = str(params[1])
            job_id = str(uuid.uuid4())
            row = {
                "id": job_id,
                "document_id": doc_id,
                "tenant_id": tenant_id,
                "status": "pending",
                "dispatch_count": 0,
                "attempt_count": 0,
                "max_attempts": 3,
                "next_attempt_at": 0,
                "dispatch_token": None,
                "dispatched_at": None,
                "dispatch_expires_at": None,
                "claim_token": None,
                "claimed_at": None,
                "heartbeat_at": None,
                "processing_expires_at": None,
                "last_error": None,
                "finished_at": None,
                "created_at": "2026-10-16T12:00:00+00:00",
                "updated_at": "2026-10-16T12:00:00+00:00",
            }
            self.pending_jobs[job_id] = row
            return MockDbResult([dict(row)])

        # Document lookup by (id, tenant_id)
        if "FROM DOCUMENTS" in norm_query and "WHERE ID = %S AND TENANT_ID = %S" in norm_query:
            doc_id, tenant_id = str(params[0]), str(params[1])
            doc = self.documents.get(doc_id)
            if doc and doc["tenant_id"] == tenant_id:
                row = {
                    "id": doc["id"],
                    "document_code": doc.get("document_code", "INVOICE_100001"),
                    "tenant_id": doc["tenant_id"],
                    "storage_path": doc.get("storage_path"),
                    "mime_type": doc.get("mime_type", "application/pdf"),
                    "file_hash_sha256": doc.get("file_hash_sha256"),
                    "original_filename": doc.get("original_filename", "invoice.pdf"),
                    "kind": doc.get("kind", "invoice"),
                    "category": doc.get("category", "accounts_payable"),
                    "status": doc.get("status", "uploaded"),
                    "file_size_bytes": doc.get("file_size_bytes", 100),
                    "processing_seconds": doc.get("processing_seconds"),
                    "uploaded_at": doc.get("uploaded_at", "2026-10-16T12:00:00+00:00"),
                    "processing_started_at": doc.get("processing_started_at"),
                    "processing_completed_at": doc.get("processing_completed_at"),
                    "updated_at": doc.get("updated_at", "2026-10-16T12:00:00+00:00"),
                }
                return MockDbResult([row])
            return MockDbResult([])

        if "SELECT ID, TENANT_ID, STORAGE_PATH, FILE_HASH_SHA256 FROM DOCUMENTS WHERE ID = %S" in norm_query:
            doc = self.documents.get(str(params[0]))
            return MockDbResult([doc] if doc else [])

        # Max run query for raw_extractions
        if "MAX(RUN_NUMBER)" in norm_query and "FROM RAW_EXTRACTIONS" in norm_query:
            doc_id = str(params[0])
            runs = [r["run_number"] for r in self.raw_extractions.values() if r["document_id"] == doc_id]
            max_r = max(runs) if runs else 0
            return MockDbResult([{"max_run": max_r}])

        # Raw extractions insertion
        if "INSERT INTO RAW_EXTRACTIONS" in norm_query:
            doc_id = str(params[0])
            run_no = int(params[1])
            routing = str(params[2])
            service_id = params[3]
            schema_ver = params[4]
            job_id = params[5]
            raw_json = json.loads(params[6]) if isinstance(params[6], str) else params[6]
            ext_id = str(uuid.uuid4())
            row = {
                "id": ext_id,
                "document_id": doc_id,
                "run_number": run_no,
                "is_current": False,
                "routing_decision": routing,
                "service_run_id": service_id,
                "schema_version": schema_ver,
                "pending_job_id": job_id,
                "raw_json": raw_json,
                "created_at": "2026-10-16T12:01:00+00:00",
            }
            self.raw_extractions[ext_id] = row
            return MockDbResult([{"id": ext_id, "run_number": run_no, "created_at": row["created_at"]}])

        # Field predictions insertion
        if "INSERT INTO FIELD_PREDICTIONS" in norm_query:
            ext_id = str(params[0])
            doc_id = str(params[1])
            f_key = str(params[2])
            f_label = str(params[3])
            ext_state = str(params[4])
            resolution = params[5]
            raw_v = params[6]
            norm_v = params[7]
            curr_v = params[8]
            conf = params[9]
            evidence = json.loads(params[10]) if isinstance(params[10], str) else params[10]
            candidates = json.loads(params[11]) if isinstance(params[11], str) else params[11]
            method = str(params[12])
            m_name = params[13]
            m_ver = params[14]
            s_ver = params[15]

            key = (ext_id, f_key)
            self.field_predictions[key] = {
                "id": str(uuid.uuid4()),
                "extraction_id": ext_id,
                "document_id": doc_id,
                "field_key": f_key,
                "field_label": f_label,
                "extraction_state": ext_state,
                "resolution": resolution,
                "raw_value": raw_v,
                "normalized_value": norm_v,
                "current_value": curr_v,
                "confidence": conf,
                "evidence": evidence,
                "candidates": candidates,
                "extraction_method": method,
                "model_name": m_name,
                "model_version": m_ver,
                "schema_version": s_ver,
                "corrected_at": None,
                "corrected_by": None,
            }
            return MockDbResult()

        # Line items insertion
        if "INSERT INTO LINE_ITEMS" in norm_query:
            ext_id = str(params[0])
            doc_id = str(params[1])
            line_no = int(params[2])
            desc = params[3]
            qty = params[4]
            price = params[5]
            amount = params[6]
            states = json.loads(params[7]) if isinstance(params[7], str) else params[7]
            evidence = json.loads(params[8]) if isinstance(params[8], str) else params[8]
            candidates = json.loads(params[9]) if isinstance(params[9], str) else params[9]

            key = (ext_id, line_no)
            self.line_items[key] = {
                "id": str(uuid.uuid4()),
                "extraction_id": ext_id,
                "document_id": doc_id,
                "line_number": line_no,
                "description": desc,
                "quantity": qty,
                "unit_price": price,
                "amount": amount,
                "field_states": states,
                "evidence": evidence,
                "candidates": candidates,
            }
            return MockDbResult()

        # Switch current extraction simulation
        if "SWITCH_CURRENT_EXTRACTION" in norm_query and params:
            new_ext_id = str(params[0])
            new_ext = self.raw_extractions.get(new_ext_id)
            carried = 0
            if new_ext:
                doc_id = new_ext["document_id"]
                # Find old current extraction
                old_ext = next(
                    (r for r in self.raw_extractions.values() if r["document_id"] == doc_id and r.get("is_current") and r["id"] != new_ext_id),
                    None,
                )
                if old_ext:
                    old_ext["is_current"] = False
                    # Carry forward corrections
                    for (e_id, f_k), pred in list(self.field_predictions.items()):
                        if e_id == old_ext["id"] and pred.get("corrected_at") is not None:
                            new_key = (new_ext_id, f_k)
                            if new_key in self.field_predictions:
                                self.field_predictions[new_key]["current_value"] = pred["current_value"]
                                self.field_predictions[new_key]["corrected_by"] = pred["corrected_by"]
                                self.field_predictions[new_key]["corrected_at"] = pred["corrected_at"]
                                carried += 1
                new_ext["is_current"] = True
            return MockDbResult([{"carried": carried}])

        # Update documents status
        if "UPDATE DOCUMENTS" in norm_query and "SET STATUS = 'EXTRACTED'" in norm_query:
            doc_id = str(params[1])
            if doc_id in self.documents:
                self.documents[doc_id]["status"] = "extracted"
                self.documents[doc_id]["processing_seconds"] = params[0]
            return MockDbResult()

        if "UPDATE DOCUMENTS" in norm_query and "SET STATUS = 'PROCESSING'" in norm_query:
            doc_id = str(params[0])
            if doc_id in self.documents:
                self.documents[doc_id]["status"] = "processing"
            return MockDbResult()

        if "UPDATE DOCUMENTS" in norm_query and "SET STATUS = 'QUEUED'" in norm_query:
            doc_id = str(params[0])
            if doc_id in self.documents and self.documents[doc_id]["status"] == "uploaded":
                self.documents[doc_id]["status"] = "queued"
            return MockDbResult()

        if "UPDATE DOCUMENTS" in norm_query and "SET STATUS = 'VALIDATED'" in norm_query:
            doc_id = str(params[0])
            if doc_id in self.documents:
                self.documents[doc_id]["status"] = "validated"
            return MockDbResult()

        if "UPDATE DOCUMENTS" in norm_query and "SET STATUS = 'CORRECTED'" in norm_query:
            doc_id = str(params[0])
            if doc_id in self.documents:
                self.documents[doc_id]["status"] = "corrected"
            return MockDbResult()

        if "UPDATE DOCUMENTS" in norm_query and "SET STATUS = 'STANDARDISED'" in norm_query:
            doc_id = str(params[0])
            if doc_id in self.documents:
                self.documents[doc_id]["status"] = "standardised"
            return MockDbResult()

        if "UPDATE DOCUMENTS" in norm_query and "SET STATUS = 'EXCEPTION'" in norm_query:
            doc_id = str(params[0])
            if doc_id in self.documents:
                self.documents[doc_id]["status"] = "exception"
            return MockDbResult()

        if "UPDATE DOCUMENTS" in norm_query and "SET STATUS = 'FAILED'" in norm_query:
            doc_id = str(params[0])
            if doc_id in self.documents:
                self.documents[doc_id]["status"] = "failed"
            return MockDbResult()

        # Raw extraction retrieval for validation (is_current or order by run_number)
        if "FROM RAW_EXTRACTIONS" in norm_query and "IS_CURRENT = TRUE" in norm_query:
            doc_id = str(params[0])
            curr = next(
                (v for v in self.raw_extractions.values() if v["document_id"] == doc_id and v.get("is_current")),
                None,
            )
            return MockDbResult([curr] if curr else [])

        if "FROM RAW_EXTRACTIONS" in norm_query and "ORDER BY RUN_NUMBER DESC" in norm_query:
            doc_id = str(params[0])
            matching = [v for v in self.raw_extractions.values() if v["document_id"] == doc_id]
            matching.sort(key=lambda r: r["run_number"], reverse=True)
            return MockDbResult([matching[0]] if matching else [])

        # Field predictions for extraction_id (validation lookup)
        if "FROM FIELD_PREDICTIONS" in norm_query and "WHERE EXTRACTION_ID = %S" in norm_query:
            ext_id = str(params[0])
            matching = [v for k, v in self.field_predictions.items() if k[0] == ext_id]
            return MockDbResult(matching)

        # Line items for extraction_id
        if "FROM LINE_ITEMS" in norm_query and "WHERE EXTRACTION_ID = %S" in norm_query:
            ext_id = str(params[0])
            matching = [v for k, v in self.line_items.items() if k[0] == ext_id]
            matching.sort(key=lambda l: l["line_number"])
            return MockDbResult(matching)

        # Vendors retrieval
        if "FROM VENDORS" in norm_query and "WHERE TENANT_ID = %S" in norm_query:
            tenant_id = str(params[0])
            matching = [v for v in self.vendors if v["tenant_id"] == tenant_id]
            return MockDbResult(matching)

        # Resolve open exceptions
        if "UPDATE EXCEPTIONS" in norm_query and "SET STATUS = 'RESOLVED'" in norm_query:
            doc_id = str(params[0])
            for e in self.exceptions:
                if e["document_id"] == doc_id and e["status"] == "open":
                    e["status"] = "resolved"
                    e["resolved_at"] = "2026-10-16T12:05:00+00:00"
            return MockDbResult()

        # Insert exception
        if "INSERT INTO EXCEPTIONS" in norm_query:
            doc_id = str(params[0])
            tenant_id = str(params[1])
            exc_type = str(params[2])
            if len(params) == 4:
                sev = "high"
                msg = str(params[3])
            else:
                sev = str(params[3])
                msg = str(params[4])
            exc_id = str(uuid.uuid4())
            self.exceptions.append({
                "id": exc_id,
                "document_id": doc_id,
                "tenant_id": tenant_id,
                "type": exc_type,
                "severity": sev,
                "status": "open",
                "message": msg,
                "note": None,
                "created_at": "2026-10-16T12:02:00+00:00",
                "resolved_at": None,
            })
            return MockDbResult()

        # Dispatcher query with WITH claimable AS (...)
        if "WITH CLAIMABLE AS" in norm_query:
            batch_size = int(params[0])
            dispatched = []
            for j in self.pending_jobs.values():
                if j["status"] in ("pending", "dispatched") and len(dispatched) < batch_size:
                    j["status"] = "dispatched"
                    j["dispatch_token"] = str(uuid.uuid4())
                    j["dispatched_at"] = "2026-10-16T12:00:00+00:00"
                    j["dispatch_count"] = j.get("dispatch_count", 0) + 1
                    dispatched.append(j)
            return MockDbResult(dispatched)


        # Worker claim query
        if "UPDATE PENDING_JOBS" in norm_query and "SET STATUS = 'PROCESSING'" in norm_query:
            job_id, dispatch_token = str(params[0]), str(params[1])
            job = self.pending_jobs.get(job_id)
            if job and job["status"] == "dispatched":
                if dispatch_token and job["dispatch_token"] == dispatch_token:
                    job["status"] = "processing"
                    job["claim_token"] = str(uuid.uuid4())
                    job["dispatch_token"] = None
                    job["attempt_count"] += 1
                    job["claimed_at"] = "2026-10-16T12:01:00+00:00"
                    job["heartbeat_at"] = "2026-10-16T12:01:00+00:00"
                    return MockDbResult([dict(job)])
            return MockDbResult([])

        # Worker lease verification before commit
        if "FROM PENDING_JOBS" in norm_query and "WHERE ID = %S" in norm_query and "CLAIM_TOKEN = %S" in norm_query:
            job_id, claim_token = str(params[0]), str(params[1])
            job = self.pending_jobs.get(job_id)
            if job and job["status"] == "processing" and job.get("claim_token") == claim_token:
                return MockDbResult([{
                    "id": job["id"],
                    "document_id": job.get("document_id", "doc-1"),
                    "tenant_id": job["tenant_id"],
                }])
            return MockDbResult([])

        # Check existing standardised record for duplicate approval check
        if "FROM STANDARDISED_RECORDS" in norm_query and "WHERE DOCUMENT_ID = %S" in norm_query:
            doc_id = str(params[0])
            matching = [r for r in self.standardised_records.values() if r["document_id"] == doc_id]
            return MockDbResult([matching[0]] if matching else [])

        # Worker mark succeeded
        if "UPDATE PENDING_JOBS" in norm_query and "SET STATUS = 'SUCCEEDED'" in norm_query:
            job_id, claim_token = str(params[0]), str(params[1])
            job = self.pending_jobs.get(job_id)
            if job and job["status"] == "processing" and job.get("claim_token") == claim_token:
                job["status"] = "succeeded"
                job["claim_token"] = None
                job["finished_at"] = "2026-10-16T12:05:00+00:00"
                return MockDbResult([dict(job)])
            return MockDbResult([])

        # Worker mark failed unconditionally (e.g. security mismatch)
        if "UPDATE PENDING_JOBS" in norm_query and "SET STATUS = 'FAILED'" in norm_query:
            err_msg, job_id, claim_token = params[0], str(params[1]), str(params[2])
            job = self.pending_jobs.get(job_id)
            if job:
                job["last_error"] = str(err_msg)
                job["status"] = "failed"
                job["finished_at"] = "2026-10-16T12:05:00+00:00"
                job["claim_token"] = None
            return MockDbResult()

        # Worker mark failed
        if "UPDATE PENDING_JOBS" in norm_query and "SET STATUS = CASE WHEN ATTEMPT_COUNT >= MAX_ATTEMPTS" in norm_query:
            err_msg, job_id, claim_token = params[0], str(params[1]), str(params[2])
            job = self.pending_jobs.get(job_id)
            if job and job.get("claim_token") == claim_token:
                job["last_error"] = str(err_msg)
                if job["attempt_count"] >= job["max_attempts"]:
                    job["status"] = "failed"
                    job["finished_at"] = "2026-10-16T12:05:00+00:00"
                else:
                    job["status"] = "pending"
                job["claim_token"] = None
            return MockDbResult()

        # Update field prediction (PATCH correction)
        if "UPDATE FIELD_PREDICTIONS" in norm_query and "SET CURRENT_VALUE = %S" in norm_query:
            val, user_id, ext_id, f_key = params[0], str(params[1]), str(params[2]), str(params[3])
            key = (ext_id, f_key)
            pred = self.field_predictions.get(key)
            if pred:
                old_val = pred["current_value"]
                pred["current_value"] = val
                pred["corrected_by"] = user_id
                pred["corrected_at"] = "2026-10-16T12:05:00+00:00"
                self.field_corrections.append({
                    "id": str(uuid.uuid4()),
                    "field_prediction_id": pred["id"],
                    "old_value": old_val,
                    "new_value": val,
                    "corrected_by": user_id,
                    "corrected_at": pred["corrected_at"],
                })
                return MockDbResult([pred])
            return MockDbResult([])

        # Check open exceptions count for approval
        if "SELECT COUNT(*) AS CNT FROM EXCEPTIONS WHERE DOCUMENT_ID = %S AND STATUS = 'OPEN'" in norm_query:
            doc_id = str(params[0])
            cnt = sum(1 for e in self.exceptions if e["document_id"] == doc_id and e["status"] == "open")
            return MockDbResult([{"cnt": cnt}])

        # Insert standardised record
        if "INSERT INTO STANDARDISED_RECORDS" in norm_query:
            doc_id = str(params[0])
            tenant_id = str(params[1])
            rec_id = str(uuid.uuid4())
            rec_code = f"REC-{self._rec_seq}"
            self._rec_seq += 1
            row = {
                "id": rec_id,
                "record_code": rec_code,
                "document_id": doc_id,
                "tenant_id": tenant_id,
                "created_at": "2026-10-16T12:15:00+00:00",
            }
            self.standardised_records[rec_id] = row
            return MockDbResult([row])

        # Insert audit log
        if "INSERT INTO AUDIT_LOGS" in norm_query:
            self.audit_logs.append(params)
            return MockDbResult()

        # List documents API query
        if "FROM DOCUMENTS" in norm_query and "ORDER BY UPLOADED_AT DESC" in norm_query:
            tenant_id = str(params[0])
            limit = int(params[1]) if len(params) > 1 else 50
            offset = int(params[2]) if len(params) > 2 else 0
            matching = [d for d in self.documents.values() if d["tenant_id"] == tenant_id]
            return MockDbResult(matching[offset : offset + limit])

        # Exceptions for document
        if "FROM EXCEPTIONS" in norm_query and "WHERE DOCUMENT_ID = %S" in norm_query:
            doc_id = str(params[0])
            matching = [e for e in self.exceptions if e["document_id"] == doc_id]
            return MockDbResult(matching)

        # Pending jobs for document
        if "FROM PENDING_JOBS" in norm_query and "WHERE DOCUMENT_ID = %S" in norm_query:
            doc_id = str(params[0])
            matching = [j for j in self.pending_jobs.values() if j["document_id"] == doc_id]
            return MockDbResult(matching)

        return MockDbResult()


class TestMigrations(unittest.TestCase):
    """Verifies safe, versioned PostgreSQL migration scripts and runner."""

    def test_migration_discovery_order(self):
        migrations = get_available_migrations()
        self.assertGreaterEqual(len(migrations), 2)
        names = [m.name for m in migrations]
        self.assertEqual(names[0], "0001_initial_schema.sql")
        self.assertEqual(names[1], "0002_pending_jobs.sql")

    def test_rollback_script_pairing(self):
        down_script = MIGRATIONS_DIR / "0002_pending_jobs.down.sql"
        self.assertTrue(down_script.is_file())

    def test_migration_apply_and_rollback_flow(self):
        db = MemoryDatabase()

        # Apply migrations
        applied = apply_migrations(db)
        self.assertIn("0001_initial_schema.sql", applied)
        self.assertIn("0002_pending_jobs.sql", applied)

        # Second apply should be idempotent and apply 0
        second_applied = apply_migrations(db)
        self.assertEqual(len(second_applied), 0)

        # Rollback latest
        rolled_back = rollback_latest_migration(db)
        self.assertEqual(rolled_back, "0002_pending_jobs.sql")
        self.assertEqual(db.schema_migrations, ["0001_initial_schema.sql"])


class TestDocumentManager(unittest.TestCase):
    """Verifies document upload validation, private containment, and atomic outbox creation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage_root = Path(self.temp_dir) / "storage" / "documents"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_pdf_content_validation_rejects_empty(self):
        with self.assertRaises(ValueError) as ctx:
            validate_pdf_content(b"")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_pdf_content_validation_rejects_non_pdf(self):
        with self.assertRaises(ValueError) as ctx:
            validate_pdf_content(b"NOT_A_PDF_DOCUMENT")
        self.assertIn("only pdf", str(ctx.exception).lower())

    def test_pdf_content_validation_rejects_oversized(self):
        oversized = b"%PDF-" + (b"0" * (MAX_FILE_SIZE_BYTES + 10))
        with self.assertRaises(ValueError) as ctx:
            validate_pdf_content(oversized)
        self.assertIn("exceeds maximum allowed size", str(ctx.exception).lower())

    def test_pdf_content_validation_accepts_valid_pdf(self):
        valid_pdf = b"%PDF-1.4 synthetic invoice content"
        sha = validate_pdf_content(valid_pdf)
        self.assertEqual(sha, hashlib.sha256(valid_pdf).hexdigest())

    def test_save_uploaded_document_atomic(self):
        db = MemoryDatabase()
        user = {"id": "user-uuid-1", "tenant_id": "tenant-uuid-1"}
        pdf_bytes = SYN_001_PATH.read_bytes()

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            meta = save_uploaded_document(
                db=db,
                user=user,
                file_bytes=pdf_bytes,
                original_filename="SYN-001.pdf",
                kind="invoice",
                category="accounts_payable",
                project_root=Path(self.temp_dir),
            )

        self.assertEqual(meta["tenant_id"], "tenant-uuid-1")
        self.assertTrue(meta["document_code"].startswith("INVOICE_"))
        self.assertEqual(meta["original_filename"], "SYN-001.pdf")
        self.assertEqual(meta["file_size_bytes"], len(pdf_bytes))
        self.assertEqual(meta["pending_job"]["status"], "pending")

        # Verify filesystem storage containment
        stored_file = self.storage_root / "tenant-uuid-1" / f"{meta['document_id']}.pdf"
        self.assertTrue(stored_file.is_file())
        self.assertEqual(stored_file.read_bytes(), pdf_bytes)

    def test_save_uploaded_document_cleanup_on_db_failure(self):
        broken_db = MagicMock()
        broken_db.execute.side_effect = RuntimeError("Database connection interrupted")
        user = {"id": "user-uuid-1", "tenant_id": "tenant-uuid-1"}
        pdf_bytes = b"%PDF-1.4 sample content"

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            with self.assertRaises(RuntimeError):
                save_uploaded_document(
                    db=broken_db,
                    user=user,
                    file_bytes=pdf_bytes,
                    original_filename="fail.pdf",
                    kind="invoice",
                    category="accounts_payable",
                    project_root=Path(self.temp_dir),
                )

        # File must NOT remain on disk after rollback
        tenant_storage = self.storage_root / "tenant-uuid-1"
        files = list(tenant_storage.glob("*.pdf")) if tenant_storage.exists() else []
        self.assertEqual(len(files), 0, "No orphaned file must remain after DB failure")


class TestTrustedDocumentResolver(unittest.TestCase):
    """Verifies tenant containment and SHA-256 integrity verification."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage_root = Path(self.temp_dir) / "storage" / "documents"
        self.tenant_dir = self.storage_root / "tenant-1"
        self.tenant_dir.mkdir(parents=True, exist_ok=True)

        self.valid_pdf = self.tenant_dir / "doc-1.pdf"
        self.pdf_bytes = b"%PDF-1.4 test invoice content"
        self.valid_pdf.write_bytes(self.pdf_bytes)
        self.valid_hash = hashlib.sha256(self.pdf_bytes).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_resolve_valid_document(self):
        db = MemoryDatabase()
        db.documents["doc-1"] = {
            "id": "doc-1",
            "tenant_id": "tenant-1",
            "storage_path": str(self.valid_pdf),
            "file_hash_sha256": self.valid_hash,
        }

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            resolved = resolve_trusted_document_path(
                db=db,
                document_id="doc-1",
                tenant_id="tenant-1",
                project_root=Path(self.temp_dir),
            )
        self.assertEqual(resolved, self.valid_pdf.resolve())

    def test_resolve_rejects_tenant_mismatch(self):
        db = MemoryDatabase()
        db.documents["doc-1"] = {
            "id": "doc-1",
            "tenant_id": "tenant-1",
            "storage_path": str(self.valid_pdf),
            "file_hash_sha256": self.valid_hash,
        }

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            with self.assertRaises(ExtractionResolutionError) as ctx:
                resolve_trusted_document_path(
                    db=db,
                    document_id="doc-1",
                    tenant_id="wrong-tenant",
                    project_root=Path(self.temp_dir),
                )
        self.assertIn("not found or unauthorized", str(ctx.exception))

    def test_resolve_rejects_missing_file_on_disk(self):
        db = MemoryDatabase()
        missing_path = self.tenant_dir / "missing.pdf"
        db.documents["doc-2"] = {
            "id": "doc-2",
            "tenant_id": "tenant-1",
            "storage_path": str(missing_path),
            "file_hash_sha256": self.valid_hash,
        }

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            with self.assertRaises(ExtractionResolutionError) as ctx:
                resolve_trusted_document_path(
                    db=db,
                    document_id="doc-2",
                    tenant_id="tenant-1",
                    project_root=Path(self.temp_dir),
                )
        self.assertIn("does not exist", str(ctx.exception))

    def test_resolve_rejects_tampered_file_hash(self):
        db = MemoryDatabase()
        tampered_pdf = self.tenant_dir / "tampered.pdf"
        tampered_pdf.write_bytes(b"%PDF-1.4 modified bytes")
        db.documents["doc-3"] = {
            "id": "doc-3",
            "tenant_id": "tenant-1",
            "storage_path": str(tampered_pdf),
            "file_hash_sha256": self.valid_hash,
        }

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            with self.assertRaises(ExtractionResolutionError) as ctx:
                resolve_trusted_document_path(
                    db=db,
                    document_id="doc-3",
                    tenant_id="tenant-1",
                    project_root=Path(self.temp_dir),
                )
        self.assertIn("integrity verification failed", str(ctx.exception))


class TestExtractionPersistence(unittest.TestCase):
    """Verifies relational persistence and human correction preservation across runs."""

    def test_persist_extraction_and_protect_human_corrections(self):
        db = MemoryDatabase()
        doc_id = "doc-persist-1"
        tenant_id = "tenant-persist-1"
        db.documents[doc_id] = {"id": doc_id, "tenant_id": tenant_id, "status": "processing"}

        sample_public_result = {
            "schema_version": "0.1.0-proposed",
            "routing": {"decision": "parser_only"},
            "fields": {
                "supplier_name": {"value": "Stationery Store", "state": "resolved"},
                "invoice_number": {"value": "INV-1001", "state": "resolved"},
                "total_amount": {"value": "150.00", "state": "resolved"},
            },
            "line_items": [
                {
                    "description": {"value": "Pen Pack"},
                    "quantity": {"value": "3"},
                    "unit_price": {"value": "50.00"},
                    "line_total": {"value": "150.00"},
                }
            ],
        }

        # 1. First persistence run
        res1 = persist_extraction_result(
            db=db,
            document_id=doc_id,
            tenant_id=tenant_id,
            public_result=sample_public_result,
            processing_seconds=1.25,
        )
        self.assertEqual(res1["run_number"], 1)
        self.assertEqual(res1["fields_saved"], 3)
        self.assertEqual(res1["lines_saved"], 1)

        ext_id_1 = res1["extraction_id"]
        key1 = (ext_id_1, "total_amount")
        self.assertEqual(db.field_predictions[key1]["current_value"], "150.00")
        self.assertIsNone(db.field_predictions[key1]["confidence"])  # P0-04: No invented confidence!

        # 2. Simulate human correction by accountant on the first run
        db.field_predictions[key1]["current_value"] = "155.00"
        db.field_predictions[key1]["corrected_at"] = "2026-10-16T12:05:00+00:00"
        db.field_predictions[key1]["corrected_by"] = "accountant-uuid"

        # 3. Re-run extraction (adds run_number 2)
        res2 = persist_extraction_result(
            db=db,
            document_id=doc_id,
            tenant_id=tenant_id,
            public_result=sample_public_result,
            processing_seconds=1.10,
        )
        self.assertEqual(res2["run_number"], 2)
        self.assertEqual(res2["carried_corrections"], 1)

        ext_id_2 = res2["extraction_id"]
        key2 = (ext_id_2, "total_amount")

        # Verify that run 2 carried forward the human correction from run 1
        self.assertEqual(
            db.field_predictions[key2]["current_value"],
            "155.00",
            "Human correction must be carried forward to run 2 via switch_current_extraction!",
        )
        # Fresh prediction remains visible in normalized_value
        self.assertEqual(db.field_predictions[key2]["normalized_value"], "150.00")


class TestValidationWorkflow(unittest.TestCase):
    """Verifies recoverable M3 validation execution and taxonomy exception logging."""

    def test_validation_workflow_valid_invoice(self):
        from tests.extraction.test_adapter import create_mock_m2_result

        db = MemoryDatabase()
        doc_id = "doc-val-1"
        tenant_id = "tenant-val-1"
        db.documents[doc_id] = {"id": doc_id, "tenant_id": tenant_id, "status": "extracted"}

        # Store known vendor matching supplier name
        db.vendors.append({"id": "v-1", "tenant_id": tenant_id, "vendor_name": "Acme Corp"})

        perfect_m2 = create_mock_m2_result()
        db.raw_extractions["ext-1"] = {
            "id": "ext-1",
            "document_id": doc_id,
            "run_number": 1,
            "is_current": True,
            "raw_json": perfect_m2,
        }

        outcome = run_document_validation(db=db, document_id=doc_id, tenant_id=tenant_id)
        self.assertEqual(outcome["workflow_status"], "M3_VALIDATION_COMPLETE")
        self.assertEqual(outcome["validation_status"], "VALID")
        self.assertEqual(db.documents[doc_id]["status"], "validated")
        self.assertEqual(len(db.exceptions), 0)

    def test_validation_workflow_evaluates_accountant_corrections(self):
        from tests.extraction.test_adapter import create_mock_m2_result

        db = MemoryDatabase()
        doc_id = "doc-val-corr"
        tenant_id = "tenant-val-1"
        db.documents[doc_id] = {"id": doc_id, "tenant_id": tenant_id, "status": "extracted"}

        db.vendors.append({"id": "v-1", "tenant_id": tenant_id, "vendor_name": "Acme Corp"})

        # Initial M2 has an arithmetic mismatch: total 1,500 != subtotal 1000 + tax 100
        mismatch_m2 = create_mock_m2_result({
            "fields": {
                "total_amount": {"value": "1,500.00", "state": "resolved"},
            }
        })
        db.raw_extractions["ext-corr"] = {
            "id": "ext-corr",
            "document_id": doc_id,
            "run_number": 1,
            "is_current": True,
            "raw_json": mismatch_m2,
        }
        # Simulate that accountant corrected total_amount to 1100.00 in field_predictions
        db.field_predictions[("ext-corr", "total_amount")] = {
            "field_key": "total_amount",
            "current_value": "1,100.00",
            "extraction_state": "resolved",
            "corrected_at": "2026-10-16T12:05:00+00:00",
        }

        outcome = run_document_validation(db=db, document_id=doc_id, tenant_id=tenant_id)
        # Because validation evaluates current_value (1100.00), it now passes!
        self.assertEqual(outcome["workflow_status"], "M3_VALIDATION_COMPLETE")
        self.assertEqual(outcome["validation_status"], "VALID")
        self.assertEqual(db.documents[doc_id]["status"], "validated")

    def test_validation_workflow_arithmetic_mismatch_exception(self):
        from tests.extraction.test_adapter import create_mock_m2_result

        db = MemoryDatabase()
        doc_id = "doc-val-2"
        tenant_id = "tenant-val-1"
        db.documents[doc_id] = {"id": doc_id, "tenant_id": tenant_id, "status": "extracted"}

        db.vendors.append({"id": "v-1", "tenant_id": tenant_id, "vendor_name": "Acme Corp"})

        mismatch_m2 = create_mock_m2_result({
            "fields": {
                "total_amount": {"value": "1,500.00", "state": "resolved"},
            }
        })
        db.raw_extractions["ext-2"] = {
            "id": "ext-2",
            "document_id": doc_id,
            "run_number": 1,
            "is_current": True,
            "raw_json": mismatch_m2,
        }

        outcome = run_document_validation(db=db, document_id=doc_id, tenant_id=tenant_id)
        self.assertEqual(outcome["workflow_status"], "M3_VALIDATION_COMPLETE")
        self.assertEqual(outcome["validation_status"], "REVIEW_REQUIRED")
        self.assertEqual(db.documents[doc_id]["status"], "exception")
        self.assertGreaterEqual(len(db.exceptions), 1)

        exc_types = [e["type"] for e in db.exceptions]
        self.assertIn("EX-005", exc_types)

    def test_validation_workflow_structural_block(self):
        db = MemoryDatabase()
        doc_id = "doc-val-3"
        tenant_id = "tenant-val-1"
        db.documents[doc_id] = {"id": doc_id, "tenant_id": tenant_id, "status": "extracted"}

        broken_m2 = {"schema_version": "0.1.0-proposed", "broken": True}
        db.raw_extractions["ext-3"] = {
            "id": "ext-3",
            "document_id": doc_id,
            "run_number": 1,
            "is_current": True,
            "raw_json": broken_m2,
        }

        outcome = run_document_validation(db=db, document_id=doc_id, tenant_id=tenant_id)
        self.assertEqual(outcome["workflow_status"], "BLOCKED_BY_STRUCTURE")
        self.assertEqual(outcome["document_status"], "exception")
        self.assertGreaterEqual(len(db.exceptions), 1)
        self.assertTrue(db.exceptions[0]["type"].startswith("M2-STRUCTURAL-"))


class TestDispatcherAndSweeper(unittest.TestCase):
    """Verifies outbox leasing, dispatch tokens, and stale sweeper recovery."""

    def test_dispatch_pending_jobs(self):
        db = MemoryDatabase()
        db.documents["doc-1"] = {"id": "doc-1", "status": "uploaded"}
        db.pending_jobs["job-1"] = {
            "id": "job-1",
            "document_id": "doc-1",
            "tenant_id": "tenant-1",
            "status": "pending",
            "attempt_count": 0,
            "max_attempts": 3,
        }

        mock_queue = MagicMock()
        dispatched = dispatch_pending_jobs(db, queue=mock_queue, batch_size=5)

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["job_id"], "job-1")
        self.assertEqual(dispatched[0]["rq_job_id"], dispatched[0]["dispatch_token"])
        self.assertIsNotNone(dispatched[0]["dispatch_token"])

        mock_queue.enqueue.assert_called_once()
        self.assertEqual(db.pending_jobs["job-1"]["status"], "dispatched")
        self.assertEqual(db.documents["doc-1"]["status"], "queued")

    def test_dispatch_with_none_queue_never_dispatches(self):
        db = MemoryDatabase()
        db.documents["doc-pending"] = {"id": "doc-pending", "status": "uploaded"}
        db.pending_jobs["job-pending"] = {
            "id": "job-pending",
            "document_id": "doc-pending",
            "tenant_id": "tenant-1",
            "status": "pending",
            "attempt_count": 0,
            "max_attempts": 3,
        }

        # Passing queue=None must NEVER mark jobs as dispatched
        dispatched = dispatch_pending_jobs(db, queue=None, batch_size=5)
        self.assertEqual(len(dispatched), 0)
        self.assertEqual(db.pending_jobs["job-pending"]["status"], "pending")
        self.assertEqual(db.documents["doc-pending"]["status"], "uploaded")

    def test_sweep_stale_jobs_recovers_crashed_worker(self):
        db = MemoryDatabase()
        db.documents["doc-crash"] = {"id": "doc-crash", "status": "processing"}
        db.pending_jobs["job-crash"] = {
            "id": "job-crash",
            "document_id": "doc-crash",
            "status": "processing",
            "attempt_count": 1,
            "max_attempts": 3,
            "is_stale": True,
        }

        swept_count = sweep_stale_jobs(db)
        self.assertEqual(swept_count, 1)
        self.assertEqual(db.pending_jobs["job-crash"]["status"], "pending")
        self.assertIn("lease timed out", db.pending_jobs["job-crash"]["last_error"])

    def test_sweep_stale_jobs_marks_failed_on_exhausted_attempts(self):
        db = MemoryDatabase()
        db.documents["doc-fail"] = {"id": "doc-fail", "status": "processing"}
        db.pending_jobs["job-fail"] = {
            "id": "job-fail",
            "document_id": "doc-fail",
            "status": "processing",
            "attempt_count": 3,
            "max_attempts": 3,
            "is_stale": True,
        }

        swept_count = sweep_stale_jobs(db)
        self.assertEqual(swept_count, 1)
        self.assertEqual(db.pending_jobs["job-fail"]["status"], "failed")
        self.assertEqual(db.documents["doc-fail"]["status"], "failed")


class TestWorkerFencingAndExecution(unittest.TestCase):
    """Verifies worker lease fencing, trusted execution, and fail-closed safety."""

    def setUp(self):
        reset_test_extraction_environment()

    def tearDown(self):
        reset_test_extraction_environment()

    def test_worker_requires_job_id_and_dispatch_token(self):
        with self.assertRaises(ExtractionUnauthorizedError):
            process_document_extraction_job(document_id="doc-no-token", job_id=None, dispatch_token=None)

    def test_worker_fencing_prevents_stale_token(self):
        db = MemoryDatabase()
        db.pending_jobs["job-fence"] = {
            "id": "job-fence",
            "document_id": "doc-fence",
            "tenant_id": "tenant-1",
            "status": "dispatched",
            "dispatch_token": "valid-token-123",
            "attempt_count": 0,
        }

        with patch("database.connection.connect", return_value=db):
            result = process_document_extraction_job(
                document_id="doc-fence",
                job_id="job-fence",
                dispatch_token="stale-token-999",
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "Job lease not acquired")

    def test_worker_document_identity_mismatch_fails_closed(self):
        db = MemoryDatabase()
        db.pending_jobs["job-mismatch"] = {
            "id": "job-mismatch",
            "document_id": "doc-actual-123",
            "tenant_id": "tenant-1",
            "status": "dispatched",
            "dispatch_token": "valid-token-777",
            "attempt_count": 0,
        }

        with patch("database.connection.connect", return_value=db):
            with self.assertRaises(ExtractionJobError) as ctx:
                process_document_extraction_job(
                    document_id="doc-wrong-456",
                    job_id="job-mismatch",
                    dispatch_token="valid-token-777",
                )
            self.assertIn("mismatch", str(ctx.exception).lower())
            # Job must be marked as failed due to security mismatch
            self.assertEqual(db.pending_jobs["job-mismatch"]["status"], "failed")


class TestDocumentsAPI(unittest.TestCase):
    """Verifies authenticated, tenant-isolated REST API endpoints including download, patch, and approval."""

    def setUp(self):
        self.auth_secret_patch = patch.dict(os.environ, {"AUTH_SECRET": "0123456789abcdef0123456789abcdef"})
        self.auth_secret_patch.start()
        self.db = MemoryDatabase()
        self.temp_dir = tempfile.mkdtemp()
        self.storage_root = Path(self.temp_dir) / "storage" / "documents"

        self.tenant_a = {"id": "tenant-a", "tenant_code": "T-AAA", "status": "active"}
        self.tenant_b = {"id": "tenant-b", "tenant_code": "T-BBB", "status": "active"}
        self.db.tenants["tenant-a"] = self.tenant_a
        self.db.tenants["tenant-b"] = self.tenant_b

        self.user_a = {
            "id": "user-a",
            "tenant_id": "tenant-a",
            "name": "Alice User",
            "email": "alice@example.com",
            "role": "accountant",
            "status": "active",
        }
        self.user_b = {
            "id": "user-b",
            "tenant_id": "tenant-b",
            "name": "Bob User",
            "email": "bob@example.com",
            "role": "accountant",
            "status": "active",
        }
        self.db.users["user-a"] = self.user_a
        self.db.users["user-b"] = self.user_b

        self.connect_patch = patch("database.connection.connect", return_value=self.db)
        self.connect_patch.start()
        self.router_connect_patch = patch("routers.documents.connect", return_value=self.db)
        self.router_connect_patch.start()

        self.client = TestClient(
            api.app,
            headers={
                "X-Requested-With": "AccountingIntelligence",
                "Origin": "http://localhost:3000",
            },
        )

    def tearDown(self):
        self.client.close()
        self.router_connect_patch.stop()
        self.connect_patch.stop()
        self.auth_secret_patch.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _login_cookies(self, user_id: str, session_id: str = "sess-1") -> dict:
        token = create_access_token(user_id, session_id)
        return {api.ACCESS: token, api.SESSION: session_id}

    def test_upload_synthetic_pdf_success(self):
        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)
        pdf_bytes = SYN_001_PATH.read_bytes()

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            resp = self.client.post(
                "/api/documents/upload",
                content=pdf_bytes,
                headers={
                    "Content-Type": "application/pdf",
                    "X-File-Name": "SYN-001.pdf",
                    "X-Document-Kind": "invoice",
                    "X-Document-Category": "accounts_payable",
                },
            )

        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        self.assertTrue(data["document_code"].startswith("INVOICE_"))
        self.assertEqual(data["tenant_id"], "tenant-a")
        self.assertEqual(data["original_filename"], "SYN-001.pdf")
        self.assertEqual(data["status"], "uploaded")
        self.assertEqual(data["pending_job"]["status"], "pending")

    def test_download_document_success(self):
        # Create a sample document file on disk and in db
        tenant_dir = self.storage_root / "tenant-a"
        tenant_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = tenant_dir / "sample.pdf"
        pdf_bytes = b"%PDF-1.4 sample content"
        pdf_file.write_bytes(pdf_bytes)

        import hashlib
        doc_id = "doc-dl-1"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100001",
            "tenant_id": "tenant-a",
            "original_filename": "sample.pdf",
            "storage_path": str(pdf_file),
            "mime_type": "application/pdf",
            "file_hash_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        }

        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            resp = self.client.get(f"/api/documents/{doc_id}/download")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, pdf_bytes)

    def test_download_document_rejects_tampered_pdf(self):
        tenant_dir = self.storage_root / "tenant-a"
        tenant_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = tenant_dir / "tampered.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 legitimate original bytes")

        doc_id = "doc-tampered-1"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100001",
            "tenant_id": "tenant-a",
            "original_filename": "tampered.pdf",
            "storage_path": str(pdf_file),
            "mime_type": "application/pdf",
            "file_hash_sha256": "expected_hash_that_does_not_match_bytes",
        }

        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            resp = self.client.get(f"/api/documents/{doc_id}/download")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("tamper", resp.json()["detail"].lower())

    def test_download_document_rejects_wrong_tenant(self):
        tenant_dir = self.storage_root / "tenant-a"
        tenant_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = tenant_dir / "private.pdf"
        pdf_bytes = b"%PDF-1.4 private data"
        pdf_file.write_bytes(pdf_bytes)

        import hashlib
        doc_id = "doc-private-1"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100001",
            "tenant_id": "tenant-a",
            "original_filename": "private.pdf",
            "storage_path": str(pdf_file),
            "mime_type": "application/pdf",
            "file_hash_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        }

        # Login as user-b from tenant-b
        cookies = self._login_cookies("user-b")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)

        with patch.dict(os.environ, {"DOCUMENT_STORAGE_DIR": str(self.storage_root)}):
            resp = self.client.get(f"/api/documents/{doc_id}/download")
        self.assertEqual(resp.status_code, 404)

    def test_patch_field_correction(self):
        doc_id = "doc-patch-1"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100002",
            "tenant_id": "tenant-a",
            "status": "extracted",
        }
        ext_id = "ext-patch-1"
        self.db.raw_extractions[ext_id] = {
            "id": ext_id,
            "document_id": doc_id,
            "run_number": 1,
            "is_current": True,
        }
        self.db.field_predictions[(ext_id, "total_amount")] = {
            "id": "fp-1",
            "extraction_id": ext_id,
            "field_key": "total_amount",
            "current_value": "100.00",
            "normalized_value": "100.00",
            "corrected_at": None,
            "corrected_by": None,
        }

        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)

        resp = self.client.patch(
            f"/api/documents/{doc_id}/fields/total_amount",
            json={"value": "105.00"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["new_value"], "105.00")
        self.assertEqual(data["document_status"], "corrected")
        self.assertEqual(self.db.documents[doc_id]["status"], "corrected")

    def test_patch_correction_rejects_standardised_document(self):
        doc_id = "doc-std-1"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100002",
            "tenant_id": "tenant-a",
            "status": "standardised",
        }
        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)

        resp = self.client.patch(
            f"/api/documents/{doc_id}/fields/total_amount",
            json={"value": "999.00"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("standardised", resp.json()["detail"].lower())

    def test_approve_document(self):
        doc_id = "doc-app-1"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100003",
            "tenant_id": "tenant-a",
            "status": "validated",
            "kind": "invoice",
            "category": "accounts_payable",
        }
        ext_id = "ext-app-1"
        self.db.raw_extractions[ext_id] = {
            "id": ext_id,
            "document_id": doc_id,
            "is_current": True,
            "run_number": 1,
        }
        self.db.field_predictions[(ext_id, "supplier_name")] = {
            "field_key": "supplier_name",
            "current_value": "Acme Corp",
            "normalized_value": "Acme Corp",
            "corrected_at": None,
        }
        self.db.field_predictions[(ext_id, "invoice_number")] = {
            "field_key": "invoice_number",
            "current_value": "INV-2026-001",
            "normalized_value": "INV-2026-001",
            "corrected_at": None,
        }
        self.db.field_predictions[(ext_id, "invoice_date")] = {
            "field_key": "invoice_date",
            "current_value": "2026-10-16",
            "normalized_value": "2026-10-16",
            "corrected_at": None,
        }
        self.db.field_predictions[(ext_id, "total_amount")] = {
            "field_key": "total_amount",
            "current_value": "100.00",
            "normalized_value": "100.00",
            "corrected_at": None,
        }

        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)

        resp = self.client.post(f"/api/documents/{doc_id}/approve")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["document_status"], "standardised")
        self.assertTrue(data["record_code"].startswith("REC-"))
        self.assertEqual(self.db.documents[doc_id]["status"], "standardised")

    def test_approve_document_rejects_corrected_status_before_revalidation(self):
        doc_id = "doc-app-corrected"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100003",
            "tenant_id": "tenant-a",
            "status": "corrected",
            "kind": "invoice",
            "category": "accounts_payable",
        }
        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)

        resp = self.client.post(f"/api/documents/{doc_id}/approve")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("validated", resp.json()["detail"].lower())

    def test_approve_document_rejects_missing_mandatory_fields(self):
        doc_id = "doc-app-missing-fields"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100003",
            "tenant_id": "tenant-a",
            "status": "validated",
            "kind": "invoice",
            "category": "accounts_payable",
        }
        ext_id = "ext-app-missing"
        self.db.raw_extractions[ext_id] = {
            "id": ext_id,
            "document_id": doc_id,
            "is_current": True,
            "run_number": 1,
        }
        # Only total_amount present, supplier_name missing!
        self.db.field_predictions[(ext_id, "total_amount")] = {
            "field_key": "total_amount",
            "current_value": "100.00",
            "normalized_value": "100.00",
            "corrected_at": None,
        }

        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)

        resp = self.client.post(f"/api/documents/{doc_id}/approve")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("supplier_name", resp.json()["detail"].lower())

    def test_approve_document_rejects_duplicate_approval(self):
        doc_id = "doc-app-dup"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100003",
            "tenant_id": "tenant-a",
            "status": "validated",
            "kind": "invoice",
            "category": "accounts_payable",
        }
        ext_id = "ext-app-dup"
        self.db.raw_extractions[ext_id] = {
            "id": ext_id,
            "document_id": doc_id,
            "is_current": True,
            "run_number": 1,
        }
        for k, v in [
            ("supplier_name", "Acme"),
            ("invoice_number", "INV-1"),
            ("invoice_date", "2026-10-16"),
            ("total_amount", "100.00"),
        ]:
            self.db.field_predictions[(ext_id, k)] = {
                "field_key": k,
                "current_value": v,
                "normalized_value": v,
                "corrected_at": None,
            }

        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)

        # First approval succeeds
        resp1 = self.client.post(f"/api/documents/{doc_id}/approve")
        self.assertEqual(resp1.status_code, 200)

        # Second approval rejected as duplicate
        resp2 = self.client.post(f"/api/documents/{doc_id}/approve")
        self.assertEqual(resp2.status_code, 400)
        self.assertIn("already", resp2.json()["detail"].lower())

    def test_get_document_result_structure(self):
        doc_id = "doc-res-1"
        self.db.documents[doc_id] = {
            "id": doc_id,
            "document_code": "INVOICE_100004",
            "tenant_id": "tenant-a",
            "status": "validated",
        }
        ext_id = "ext-res-1"
        self.db.raw_extractions[ext_id] = {
            "id": ext_id,
            "document_id": doc_id,
            "run_number": 1,
            "is_current": True,
            "routing_decision": "parser_only",
            "service_run_id": "run-abc",
            "schema_version": "1.0",
        }
        self.db.field_predictions[(ext_id, "total_amount")] = {
            "extraction_id": ext_id,
            "field_key": "total_amount",
            "field_label": "Total Amount",
            "extraction_state": "resolved",
            "resolution": "parser_preserved",
            "raw_value": "100.00",
            "normalized_value": "100.00",
            "current_value": "100.00",
            "confidence": None,
            "evidence": [{"page": 1}],
            "candidates": [],
            "extraction_method": "PARSER",
            "corrected_at": None,
        }
        self.db.line_items[(ext_id, 1)] = {
            "extraction_id": ext_id,
            "line_number": 1,
            "description": "Widget",
            "quantity": Decimal("2"),
            "unit_price": Decimal("50.00"),
            "amount": Decimal("100.00"),
            "field_states": {},
            "evidence": {},
            "candidates": {},
        }

        cookies = self._login_cookies("user-a")
        self.client.cookies.clear()
        self.client.cookies.update(cookies)
        resp = self.client.get(f"/api/documents/{doc_id}/result")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["document_id"], doc_id)
        self.assertEqual(data["status"], "validated")
        self.assertEqual(len(data["fields"]), 1)
        self.assertEqual(data["fields"][0]["field_key"], "total_amount")
        self.assertIsNone(data["fields"][0]["confidence"])  # P0-04
        self.assertEqual(len(data["line_items"]), 1)
        self.assertEqual(data["line_items"][0]["description"], "Widget")


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to run PostgreSQL integration tests")
class TestPostgreSQLIntegration(unittest.TestCase):
    """
    Live PostgreSQL integration tests running against an isolated dynamic test schema.
    Applies forward migrations, tests outbox table, documents constraint, locking,
    triggers (immutability, correction logging), switch_current_extraction, and rollback.
    """

    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row

        cls.url = os.environ["TEST_DATABASE_URL"]
        cls.schema = "pipeline_test_" + uuid.uuid4().hex
        with psycopg.connect(cls.url, autocommit=True) as db:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))

        def _connect():
            return psycopg.connect(cls.url, row_factory=dict_row, autocommit=True, options=f"-c search_path={cls.schema}")

        cls._connect_factory = staticmethod(_connect)
        with cls._connect_factory() as db:
            apply_migrations(db)

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql

        with psycopg.connect(cls.url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def connect(self):
        return self._connect_factory()

    def test_schema_migrations_applied_and_tables_exist(self):
        with self.connect() as db:
            applied = get_applied_migrations(db)
            self.assertIn("0001_initial_schema.sql", applied)
            self.assertIn("0002_pending_jobs.sql", applied)

            rows = db.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = current_schema()
            """).fetchall()
            names = {r["table_name"] for r in rows}
            self.assertIn("tenants", names)
            self.assertIn("documents", names)
            self.assertIn("pending_jobs", names)
            self.assertIn("raw_extractions", names)
            self.assertIn("field_predictions", names)
            self.assertIn("field_corrections", names)
            self.assertIn("line_items", names)
            self.assertIn("exceptions", names)
            self.assertIn("standardised_records", names)

    def _create_tenant_and_user(self, db, prefix: str):
        t_id = db.execute(f"""
            INSERT INTO tenants (tenant_code, name, registration_number, admin_email)
            VALUES ('T-TEST-{prefix}', 'Test Corp {prefix}', 'REG-{prefix}-1', 'admin@{prefix}.test')
            RETURNING id
        """).fetchone()["id"]
        u_id = db.execute(f"""
            INSERT INTO users (tenant_id, name, email, role, status)
            VALUES (%s, 'User {prefix}', 'user@{prefix}.test', 'accountant', 'active')
            RETURNING id
        """, (t_id,)).fetchone()["id"]
        return t_id, u_id

    def _create_document(self, db, t_id, u_id, prefix: str):
        return db.execute("""
            INSERT INTO documents (
                tenant_id, owner_id, original_filename, kind, category,
                file_size_bytes, storage_path, mime_type, file_hash_sha256
            ) VALUES (
                %s, %s, 'test.pdf', 'invoice', 'accounts_payable',
                100, 'test.pdf', 'application/pdf', 'hash_' || %s
            ) RETURNING id
        """, (t_id, u_id, prefix)).fetchone()["id"]

    def test_pending_jobs_status_check_constraint(self):
        import psycopg
        with self.connect() as db:
            t_id, u_id = self._create_tenant_and_user(db, "STATUS")
            doc_id = self._create_document(db, t_id, u_id, "STATUS")

            for valid_status in ["pending", "succeeded"]:
                db.execute("DELETE FROM pending_jobs WHERE document_id = %s", (doc_id,))
                db.execute("""
                    INSERT INTO pending_jobs (document_id, tenant_id, status, finished_at)
                    VALUES (%s, %s, %s, CASE WHEN %s = 'succeeded' THEN now() ELSE NULL END)
                """, (doc_id, t_id, valid_status, valid_status))

            with self.assertRaises(psycopg.errors.CheckViolation):
                db.execute("""
                    INSERT INTO pending_jobs (document_id, tenant_id, status)
                    VALUES (%s, %s, 'invalid_status')
                """, (doc_id, t_id))

    def test_pending_jobs_unique_open_document_constraint(self):
        import psycopg
        with self.connect() as db:
            t_id, u_id = self._create_tenant_and_user(db, "UQ")
            doc_id = self._create_document(db, t_id, u_id, "UQ")

            db.execute("""
                INSERT INTO pending_jobs (document_id, tenant_id, status)
                VALUES (%s, %s, 'pending')
            """, (doc_id, t_id))

            with self.assertRaises(psycopg.errors.UniqueViolation):
                db.execute("""
                    INSERT INTO pending_jobs (document_id, tenant_id, status, dispatch_token, dispatch_expires_at)
                    VALUES (%s, %s, 'dispatched', gen_random_uuid(), now() + interval '2 minutes')
                """, (doc_id, t_id))

    def test_pending_jobs_on_delete_restrict(self):
        import psycopg
        with self.connect() as db:
            t_id, u_id = self._create_tenant_and_user(db, "DEL")
            doc_id = self._create_document(db, t_id, u_id, "DEL")

            db.execute("""
                INSERT INTO pending_jobs (document_id, tenant_id, status)
                VALUES (%s, %s, 'pending')
            """, (doc_id, t_id))

            with self.assertRaises((psycopg.errors.ForeignKeyViolation, psycopg.errors.RestrictViolation)):
                db.execute("DELETE FROM documents WHERE id = %s", (doc_id,))

    def test_real_postgresql_worker_claim_and_token_fencing(self):
        with self.connect() as db:
            t_id, u_id = self._create_tenant_and_user(db, "CLAIM")
            doc_id = self._create_document(db, t_id, u_id, "CLAIM")

            dispatch_token = str(uuid.uuid4())
            job_id = db.execute("""
                INSERT INTO pending_jobs (document_id, tenant_id, status, dispatch_token, dispatch_expires_at, dispatch_count)
                VALUES (%s, %s, 'dispatched', %s, now() + interval '2 minutes', 1)
                RETURNING id
            """, (doc_id, t_id, dispatch_token)).fetchone()["id"]

            claim_token = str(uuid.uuid4())
            claimed = db.execute("""
                UPDATE pending_jobs
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    claim_token = %s,
                    claimed_at = now(),
                    heartbeat_at = now(),
                    dispatch_token = NULL,
                    dispatch_expires_at = NULL,
                    processing_expires_at = now() + interval '5 minutes'
                WHERE id = %s
                  AND status = 'dispatched'
                  AND dispatch_token = %s
                  AND dispatch_expires_at > now()
                RETURNING id, document_id, tenant_id, attempt_count
            """, (claim_token, job_id, dispatch_token)).fetchone()

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["attempt_count"], 1)

            stale_token = str(uuid.uuid4())
            res_stale = db.execute("""
                UPDATE pending_jobs
                SET status = 'succeeded',
                    claim_token = NULL,
                    processing_expires_at = NULL,
                    finished_at = now()
                WHERE id = %s
                  AND status = 'processing'
                  AND claim_token = %s
                  AND processing_expires_at > now()
            """, (job_id, stale_token))
            self.assertEqual(res_stale.rowcount, 0)

            res_valid = db.execute("""
                UPDATE pending_jobs
                SET status = 'succeeded',
                    claim_token = NULL,
                    processing_expires_at = NULL,
                    finished_at = now()
                WHERE id = %s
                  AND status = 'processing'
                  AND claim_token = %s
                  AND processing_expires_at > now()
            """, (job_id, claim_token))
            self.assertEqual(res_valid.rowcount, 1)

    def test_real_postgresql_switch_current_extraction_and_field_corrections(self):
        import psycopg
        with self.connect() as db:
            t_id, u_id = self._create_tenant_and_user(db, "SWITCH")
            doc_id = self._create_document(db, t_id, u_id, "SWITCH")

            # Run 1
            ext1 = db.execute("""
                INSERT INTO raw_extractions (document_id, run_number, is_current, routing_decision, raw_json)
                VALUES (%s, 1, FALSE, 'parser_only', '{"fields": {}}'::jsonb)
                RETURNING id
            """, (doc_id,)).fetchone()["id"]

            pred1 = db.execute("""
                INSERT INTO field_predictions (
                    extraction_id, document_id, field_key, field_label,
                    extraction_state, raw_value, normalized_value, current_value
                ) VALUES (%s, %s, 'total_amount', 'Total Amount', 'resolved', '100.00', '100.00', '100.00')
                RETURNING id
            """, (ext1, doc_id)).fetchone()["id"]

            # Switch to run 1 as current
            db.execute("SELECT switch_current_extraction(%s)", (ext1,))

            # Verify protect_field_predictions: updating immutable column fails
            with self.assertRaises(psycopg.errors.RaiseException):
                db.execute("UPDATE field_predictions SET raw_value = '999.00' WHERE id = %s", (pred1,))

            # Updating current_value succeeds and fires log_field_correction trigger
            db.execute("""
                UPDATE field_predictions
                SET current_value = '105.00',
                    corrected_by = %s,
                    corrected_at = now()
                WHERE id = %s
            """, (u_id, pred1))

            # Verify field_corrections row was created by trigger
            corr = db.execute("""
                SELECT old_value, new_value, corrected_by FROM field_corrections
                WHERE field_prediction_id = %s
            """, (pred1,)).fetchone()
            self.assertIsNotNone(corr)
            self.assertEqual(corr["old_value"], "100.00")
            self.assertEqual(corr["new_value"], "105.00")

            # Verify field_corrections is append-only (prevent_correction_change trigger)
            with self.assertRaises(psycopg.errors.RaiseException):
                db.execute("UPDATE field_corrections SET new_value = '999.00' WHERE field_prediction_id = %s", (pred1,))

            # Run 2: Automated re-extraction adds new run
            ext2 = db.execute("""
                INSERT INTO raw_extractions (document_id, run_number, is_current, routing_decision, raw_json)
                VALUES (%s, 2, FALSE, 'parser_only', '{"fields": {}}'::jsonb)
                RETURNING id
            """, (doc_id,)).fetchone()["id"]

            pred2 = db.execute("""
                INSERT INTO field_predictions (
                    extraction_id, document_id, field_key, field_label,
                    extraction_state, raw_value, normalized_value, current_value
                ) VALUES (%s, %s, 'total_amount', 'Total Amount', 'resolved', '100.00', '100.00', '100.00')
                RETURNING id
            """, (ext2, doc_id)).fetchone()["id"]

            # Call switch_current_extraction(ext2) -> carries forward the human correction!
            carried = db.execute("SELECT switch_current_extraction(%s) AS cnt", (ext2,)).fetchone()["cnt"]
            self.assertEqual(carried, 1)

            row2 = db.execute("SELECT current_value, corrected_by FROM field_predictions WHERE id = %s", (pred2,)).fetchone()
            self.assertEqual(row2["current_value"], "105.00")
            self.assertEqual(row2["corrected_by"], u_id)

    def test_migration_rollback_and_reapply(self):
        with self.connect() as db:
            applied_before = get_applied_migrations(db)
            self.assertIn("0002_pending_jobs.sql", applied_before)

            rolled_back = rollback_latest_migration(db)
            self.assertEqual(rolled_back, "0002_pending_jobs.sql")

            applied_after_rollback = get_applied_migrations(db)
            self.assertNotIn("0002_pending_jobs.sql", applied_after_rollback)

            rows = db.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_name = 'pending_jobs'
            """).fetchall()
            self.assertEqual(len(rows), 0)

            newly_applied = apply_migrations(db)
            self.assertIn("0002_pending_jobs.sql", newly_applied)
            applied_final = get_applied_migrations(db)
            self.assertIn("0002_pending_jobs.sql", applied_final)

    def test_migration_rollback_refuses_when_jobs_exist(self):
        import psycopg
        with self.connect() as db:
            t_id, u_id = self._create_tenant_and_user(db, "ROLLREFUSE")
            doc_id = self._create_document(db, t_id, u_id, "ROLLREFUSE")

            # Insert an active/historical pending job
            db.execute("""
                INSERT INTO pending_jobs (document_id, tenant_id, status)
                VALUES (%s, %s, 'pending')
            """, (doc_id, t_id))

            # Down migration must refuse destructive rollback when processing history exists
            with self.assertRaises(psycopg.errors.RaiseException) as ctx:
                rollback_latest_migration(db)
            self.assertIn("refusing to drop pending_jobs", str(ctx.exception).lower())

            # Cleanup
            db.execute("DELETE FROM pending_jobs WHERE document_id = %s", (doc_id,))


if __name__ == "__main__":
    unittest.main()
