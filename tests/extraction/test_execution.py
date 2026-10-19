"""Tests for Module 2 extraction execution boundary and parser injection.

Proves:
- Case A: Default parser is real Docling; fails closed when models missing.
- Case B: Controlled injected parser produces synthetic run artifacts.
- Case C: Service output is converted to strict public result.
- Case D: Identity, schema version, provenance, and source references are consistent.
- Case E: AI candidates never become approved fields.
- Case F: Missing/unresolved fields preserve review-required status.
- Case G: Parser/contract failure returns genuine failure, never an approved result.
- Case H: Offline safety: zero external network/database/model inference.
- Case K: Downstream adaptation test showing adapt_m2_to_m3 accepts complete and blocks unresolved.
"""

import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from services.extraction.adapter import adapt_m2_to_m3
from services.extraction.document_intelligence_service import DocumentIntelligenceService
import services.extraction.document_intelligence_service as dis
from services.extraction.execution import execute_document_extraction


def load_baseline_docling_doc():
    path = Path(__file__).resolve().parents[2] / "baseline" / "runs" / "docling-20261006T173339Z-d3d82235" / "document.json"
    return json.loads(path.read_text(encoding="utf-8"))


def make_injected_parser(document_dict, status="success"):
    def _parser(input_pdf, output_parent, project_root=None, verbose=False):
        output = output_parent / f"docling-test-{Path(input_pdf).stem}"
        output.mkdir(parents=True, exist_ok=True)
        (output / "document.json").write_text(json.dumps(document_dict), encoding="utf-8")
        (output / "run.json").write_text(json.dumps({
            "status": status,
            "mode": "test_injected_parser",
            "source": str(input_pdf),
            "source_sha256": "0" * 64,
        }), encoding="utf-8")
        return output, {"status": status}
    return _parser


class TestExtractionExecutionBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

        self.pdf_path = self.root / "test_invoice.pdf"
        self.pdf_path.write_bytes(b"%PDF-1.4 test invoice file content")
        self.doc_dict = load_baseline_docling_doc()

    def tearDown(self):
        self.tmp.cleanup()

    def test_case_a_default_real_parser_fails_when_models_missing(self):
        """Case A: Real Docling is the default and fails closed if models are missing."""
        service = DocumentIntelligenceService(project_root=self.root)
        # Models directory does not exist in root
        with self.assertRaises(ValueError) as ctx:
            service.process_document(
                document_id="DOC-FAIL-001",
                file_path=self.pdf_path,
            )
        self.assertIn("Docling model directory not found", str(ctx.exception))

    def test_case_b_controlled_injected_parser_produces_artifacts(self):
        """Case B: Injected parser generates expected synthetic run artifacts."""
        parser = make_injected_parser(self.doc_dict)
        service = DocumentIntelligenceService(
            project_root=self.root,
            parser=parser,
        )
        summary_path, summary = service.process_document(
            document_id="DOC-INJECT-001",
            file_path=self.pdf_path,
        )
        self.assertTrue(summary_path.is_file())
        self.assertEqual(summary["document_id"], "DOC-INJECT-001")
        self.assertEqual(summary["status"], "parser_only_needs_review")
        self.assertTrue(Path(summary["artifacts"]["document_json"]).is_file())
        self.assertTrue(Path(summary["artifacts"]["mapped_result"]).is_file())
        self.assertTrue(Path(summary["artifacts"]["classified_extraction"]).is_file())

    def test_case_c_extraction_output_converted_to_strict_public_result(self):
        """Case C: Output is converted to strict public result via execute_document_extraction."""
        parser = make_injected_parser(self.doc_dict)
        result = execute_document_extraction(
            document_id="DOC-PUB-001",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=parser,
        )
        self.assertEqual(result["execution_status"], "EXTRACTED")
        self.assertEqual(result["document_id"], "DOC-PUB-001")
        self.assertEqual(result["extraction_status"], "parser_only_needs_review")
        self.assertTrue(result["review_required"])
        self.assertFalse(result["accounting_approval_performed"])
        self.assertIsNone(result["error"])

        public_path = Path(result["public_result_path"])
        self.assertTrue(public_path.is_file())
        public_data = json.loads(public_path.read_text(encoding="utf-8"))
        self.assertEqual(public_data["mode"], "module2_public_result")
        self.assertEqual(public_data["schema_version"], "0.1.0-proposed")

    def test_case_d_identity_and_provenance_consistency(self):
        """Case D: Document ID, schema version, provenance, and hashes remain consistent."""
        parser = make_injected_parser(self.doc_dict)
        result = execute_document_extraction(
            document_id="DOC-PROV-001",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=parser,
        )
        public_data = json.loads(Path(result["public_result_path"]).read_text(encoding="utf-8"))

        self.assertEqual(public_data["document_id"], "DOC-PROV-001")
        self.assertEqual(public_data["schema_version"], "0.1.0-proposed")
        prov = public_data["provenance"]
        self.assertIn("service_run_id", prov)
        self.assertIn("classified_extraction_run_id", prov)
        self.assertEqual(len(prov["service_result_sha256"]), 64)
        self.assertEqual(len(prov["classified_extraction_sha256"]), 64)

    def test_case_e_ai_candidate_never_becomes_approved_field(self):
        """Case E: AI candidate is strictly separated from parser values and never auto-accepted."""
        # Create a document where invoice_number is missing in parser, triggering VLM route
        modified_doc = json.loads(json.dumps(self.doc_dict))
        # Remove invoice number from texts to force missing
        modified_doc["texts"] = [
            t for t in modified_doc["texts"]
            if not t.get("text", "").lower().startswith("invoice number:")
        ]

        parser = make_injected_parser(modified_doc)
        service = DocumentIntelligenceService(project_root=self.root, parser=parser)

        def fake_run_attempt(combined_path, project_root):
            comb_bytes = Path(combined_path).read_bytes()
            comb = json.loads(comb_bytes)
            import hashlib
            h = hashlib.sha256(comb_bytes).hexdigest()
            selective_dir = self.root / "runs" / "selective-test"
            selective_dir.mkdir(parents=True, exist_ok=True)
            (selective_dir / "prediction.json").write_text(json.dumps({"invoice_number": "INV-AI-99999"}))
            routing_data = {
                "schema_version": "0.1.0-proposed",
                "document_id": comb["document_id"],
                "extraction_run_id": comb["extraction_run_id"],
                "source_result_path": str(combined_path),
                "source_result_sha256": h,
                "decision": "vlm_candidate",
                "requested_fields": ["invoice_number"],
                "candidate_reasons": [{"field": "invoice_number", "reason": "UNMAPPED_FIELD"}],
                "blocking_reasons": [],
            }
            (selective_dir / "routing.json").write_text(json.dumps(routing_data))
            record = {
                "schema_version": "0.1.0-proposed",
                "selective_run_id": "sel-1",
                "status": "response_structure_valid_needs_review",
                "decision": "vlm_candidate",
                "ai_request_attempted": True,
                "ai_response_received": True,
                "source_result_path": str(combined_path),
                "source_result_sha256": h,
                "requested_fields": ["invoice_number"],
                "document_id": comb["document_id"],
                "extraction_run_id": comb["extraction_run_id"],
            }
            (selective_dir / "run.json").write_text(json.dumps(record))
            return selective_dir, record

        with patch("services.extraction.document_intelligence_service.run_attempt", side_effect=fake_run_attempt):
            result = execute_document_extraction(
                document_id="DOC-AI-001",
                file_path=self.pdf_path,
                project_root=self.root,
                parser=parser,
                allow_vlm_inference=True,
            )

        self.assertEqual(result["execution_status"], "EXTRACTED")
        self.assertEqual(result["extraction_status"], "candidate_for_review")
        public_data = json.loads(Path(result["public_result_path"]).read_text(encoding="utf-8"))

        # Safety checks: parser value must remain None; AI candidate is separate
        field = public_data["fields"]["invoice_number"]
        self.assertIsNone(field["value"])
        self.assertEqual(field["ai_candidate"], "INV-AI-99999")
        self.assertEqual(field["resolution"], "candidate_for_review")
        self.assertTrue(field["review_required"])
        self.assertFalse(public_data["safety"]["ai_candidate_auto_accepted"])
        self.assertFalse(public_data["safety"]["automatic_accounting_approval"])
        self.assertFalse(public_data["accounting_approval_performed"])

    def test_case_e2_vlm_disabled_by_default_fails_closed_to_human_review(self):
        """Case E2: VLM candidate fails closed to human_review when allow_vlm_inference=False."""
        modified_doc = json.loads(json.dumps(self.doc_dict))
        modified_doc["texts"] = [
            t for t in modified_doc["texts"]
            if not t.get("text", "").lower().startswith("invoice number:")
        ]

        parser = make_injected_parser(modified_doc)

        with patch("services.extraction.document_intelligence_service.run_attempt") as mock_attempt:
            # allow_vlm_inference defaults to False
            result = execute_document_extraction(
                document_id="DOC-VLM-DIS-001",
                file_path=self.pdf_path,
                project_root=self.root,
                parser=parser,
            )
            # Live VLM attempt must NOT be called
            mock_attempt.assert_not_called()

        self.assertEqual(result["execution_status"], "EXTRACTED")
        self.assertEqual(result["extraction_status"], "human_review_required")
        public_data = json.loads(Path(result["public_result_path"]).read_text(encoding="utf-8"))

        self.assertEqual(public_data["status"], "human_review_required")
        field = public_data["fields"]["invoice_number"]
        self.assertIsNone(field["value"])
        self.assertIsNone(field["ai_candidate"])
        self.assertEqual(field["resolution"], "unresolved")
        self.assertTrue(field["review_required"])
        self.assertFalse(public_data["safety"]["ai_candidate_auto_accepted"])
        self.assertFalse(public_data["safety"]["automatic_accounting_approval"])
        self.assertFalse(public_data["accounting_approval_performed"])

    def test_case_f_missing_unresolved_fields_require_review(self):
        """Case F: Missing/unresolved fields preserve review-required status."""
        parser = make_injected_parser(self.doc_dict)
        result = execute_document_extraction(
            document_id="DOC-REV-001",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=parser,
        )
        self.assertTrue(result["review_required"])
        self.assertFalse(result["accounting_approval_performed"])

    def test_case_g_parser_or_contract_failure_returns_genuine_failure(self):
        """Case G: Infrastructure and structural failures return error status, never an approved result."""
        # 1. Non-existent file -> INFRASTRUCTURE_ERROR
        missing_file = self.root / "non_existent.pdf"
        res1 = execute_document_extraction("DOC-ERR-001", missing_file, project_root=self.root)
        self.assertEqual(res1["execution_status"], "INFRASTRUCTURE_ERROR")
        self.assertIn("not found", res1["error"])
        self.assertFalse(res1["accounting_approval_performed"])

        # 2. Blank document_id -> STRUCTURAL_FAILURE
        res2 = execute_document_extraction("   ", self.pdf_path, project_root=self.root)
        self.assertEqual(res2["execution_status"], "STRUCTURAL_FAILURE")
        self.assertIn("non-empty", res2["error"])

        # 3. Crashing parser -> INFRASTRUCTURE_ERROR
        def crashing_parser(*args, **kwargs):
            raise RuntimeError("Parser process out of memory")

        res3 = execute_document_extraction(
            "DOC-CRASH-001",
            self.pdf_path,
            project_root=self.root,
            parser=crashing_parser,
        )
        self.assertEqual(res3["execution_status"], "INFRASTRUCTURE_ERROR")
        self.assertIn("RuntimeError", res3["error"])

    def test_case_h_offline_safety(self):
        """Case H: Execution runs offline without socket or external service access."""
        parser = make_injected_parser(self.doc_dict)
        # Block network access via mock
        with patch("socket.socket") as mock_socket:
            result = execute_document_extraction(
                document_id="DOC-OFFLINE-001",
                file_path=self.pdf_path,
                project_root=self.root,
                parser=parser,
            )
            self.assertEqual(result["execution_status"], "EXTRACTED")
            mock_socket.assert_not_called()

    def test_case_k_adapter_accepts_complete_and_blocks_unresolved(self):
        """Case K: Downstream adapt_m2_to_m3 accepts complete invoice and blocks unresolved.

        Demonstrates that Rachel's downstream step can safely adapt the published extraction
        artifact without coupling the extraction worker to M3 validation.
        """
        parser = make_injected_parser(self.doc_dict)
        res = execute_document_extraction(
            document_id="DOC-ADAPT-001",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=parser,
        )
        self.assertEqual(res["execution_status"], "EXTRACTED")
        public_data = json.loads(Path(res["public_result_path"]).read_text(encoding="utf-8"))

        # 1. Complete invoice adapts to READY_FOR_M3
        adapted = adapt_m2_to_m3(public_data)
        self.assertEqual(adapted["status"], "READY_FOR_M3")
        self.assertEqual(len(adapted["structural_issues"]), 0)
        m3_input = adapted["m3_input"]
        self.assertEqual(m3_input["vendor_name"], "Example Stationery (Synthetic)")
        self.assertEqual(m3_input["invoice_number"], "SYN-001")
        self.assertEqual(m3_input["currency"], "MYR")
        self.assertEqual(m3_input["total_amount"], Decimal("50.00"))

        # 2. Incomplete invoice (e.g. unresolved invoice_number) is blocked
        incomplete_public = json.loads(json.dumps(public_data))
        incomplete_public["fields"]["invoice_number"] = {
            "raw_text": None,
            "value": None,
            "state": "missing",
            "method": None,
            "evidence": [],
            "candidates": [],
            "ai_candidate": "INV-AI-99999",
            "resolution": "candidate_for_review",
            "review_required": True,
        }
        blocked = adapt_m2_to_m3(incomplete_public)
        self.assertEqual(blocked["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertIsNone(blocked["m3_input"])
        issue_fields = [iss["field"] for iss in blocked["structural_issues"]]
        self.assertIn("invoice_number", issue_fields)

    def test_case_l1_custom_models_dir_fails_closed_when_missing(self):
        """Case L1: Providing a non-existent models_dir fails closed with clear error."""
        missing_models = self.root / "nonexistent_models_dir"
        result = execute_document_extraction(
            document_id="DOC-FAIL-MODELS",
            file_path=self.pdf_path,
            project_root=self.root,
            models_dir=missing_models,
        )
        self.assertEqual(result["execution_status"], "INFRASTRUCTURE_ERROR")
        self.assertIn("Docling model directory not found", result["error"])

    def test_case_l2_models_dir_forwarding(self):
        """Case L2: models_dir parameter is forwarded through execution boundary to parser."""
        received_kwargs = {}

        def mock_parser_with_models(
            input_pdf, output_parent, project_root=None, verbose=False, models_dir=None
        ):
            received_kwargs["models_dir"] = models_dir
            output = output_parent / f"docling-test-{Path(input_pdf).stem}"
            output.mkdir(parents=True, exist_ok=True)
            (output / "document.json").write_text(
                json.dumps(self.doc_dict), encoding="utf-8"
            )
            (output / "run.json").write_text(
                json.dumps({
                    "status": "success",
                    "mode": "test_injected_parser",
                    "source": str(input_pdf),
                    "source_sha256": "0" * 64,
                }),
                encoding="utf-8",
            )
            return output, {"status": "success"}

        custom_models = self.root / "custom_models"
        custom_models.mkdir()

        result = execute_document_extraction(
            document_id="DOC-MODELS-FWD",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=mock_parser_with_models,
            models_dir=custom_models,
        )
        self.assertEqual(result["execution_status"], "EXTRACTED")
        self.assertEqual(
            received_kwargs.get("models_dir"),
            custom_models.resolve(),
        )
