import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "project" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


import services.extraction.workflow.document_extraction_service as dis


class DocumentIntelligenceServiceTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        (self.root / "runs").mkdir()

        self.pdf = self.root / "invoice.pdf"
        self.pdf.write_bytes(b"%PDF-test")

        self.service = dis.DocumentIntelligenceService(
            project_root=self.root
        )

        self.parser_dir = self.root / "runs" / "docling-test"
        self.parser_dir.mkdir()

        self.combined = self.root / "runs" / "combined.json"

        self.routing_path = (
            self.root / "runs" / "routing.json"
        )

        self.mapped = self.root / "runs" / "mapped.json"
        self.intermediate = (
            self.root / "runs" / "intermediate.json"
        )
        self.classification = (
            self.root / "runs" / "classification.json"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def prepare_common_mocks(self, decision):
        self.combined.write_text(
            json.dumps({
                "document_type": {
                    "value": "invoice",
                    "status": "classified",
                }
            }),
            encoding="utf-8",
        )

        patcher_parse = patch.object(
            dis,
            "parse_document",
            return_value=(
                self.parser_dir,
                {"status": "success"},
            ),
        )

        patcher_map = patch.object(
            self.service,
            "_save_mapping",
            return_value=self.mapped,
        )

        patcher_extract = patch.object(
            self.service,
            "_save_extraction",
            return_value=self.intermediate,
        )

        patcher_classify = patch.object(
            self.service,
            "_save_classification",
            return_value=self.classification,
        )

        patcher_combine = patch.object(
            self.service,
            "_save_combined",
            return_value=self.combined,
        )

        patcher_route = patch.object(
            self.service,
            "_save_routing",
            return_value=(
                self.routing_path,
                decision,
            ),
        )

        patchers = [
            patcher_parse,
            patcher_map,
            patcher_extract,
            patcher_classify,
            patcher_combine,
            patcher_route,
        ]

        mocks = [p.start() for p in patchers]

        self.addCleanup(
            lambda: [p.stop() for p in reversed(patchers)]
        )

        return mocks

    def test_parser_only_skips_ai(self):
        decision = {
            "decision": "parser_only",
            "requested_fields": [],
            "candidate_reasons": [],
            "blocking_reasons": [],
        }

        self.prepare_common_mocks(decision)

        with patch.object(dis, "run_attempt") as ai:
            _, result = self.service.process_document(
                document_id="DOC-PARSER",
                file_path=self.pdf,
            )

        self.assertEqual(
            result["status"],
            "parser_only_needs_review",
        )

        self.assertEqual(
            result["routing"]["decision"],
            "parser_only",
        )

        self.assertFalse(
            result["ai_assistance"]["attempted"]
        )

        self.assertIsNone(
            result["ai_assistance"]["prediction"]
        )

        self.assertTrue(
            result["review_required"]
        )

        self.assertFalse(
            result["accounting_approval_performed"]
        )

        ai.assert_not_called()

    def test_human_review_blocker_skips_ai(self):
        decision = {
            "decision": "human_review",
            "requested_fields": [],
            "candidate_reasons": [],
            "blocking_reasons": [
                {
                    "code": "UNRESOLVED_FIELD_REQUIRES_REVIEW",
                    "field": "subtotal",
                }
            ],
        }

        self.prepare_common_mocks(decision)

        with patch.object(dis, "run_attempt") as ai:
            _, result = self.service.process_document(
                document_id="DOC-BLOCKED",
                file_path=self.pdf,
            )

        self.assertEqual(
            result["status"],
            "human_review_required",
        )

        self.assertFalse(
            result["ai_assistance"]["attempted"]
        )

        ai.assert_not_called()

    def test_vlm_candidate_is_reconciled_but_not_auto_accepted(self):
        decision = {
            "decision": "vlm_candidate",
            "requested_fields": ["invoice_number"],
            "candidate_reasons": [
                {
                    "code": "MISSING_REQUIRED_HEADER",
                    "field": "invoice_number",
                }
            ],
            "blocking_reasons": [],
        }

        self.prepare_common_mocks(decision)

        selective_dir = (
            self.root / "runs" / "selective-test"
        )
        selective_dir.mkdir()

        (selective_dir / "prediction.json").write_text(
            json.dumps({
                "invoice_number": "INV-RECOVER-001"
            }),
            encoding="utf-8",
        )

        selective_record = {
            "status": "response_structure_valid_needs_review",
            "ai_request_attempted": True,
            "ai_response_received": True,
        }

        reconciliation_result = {
            "status": "candidate_for_review",
            "review_required": True,
            "fields": {
                "invoice_number": {
                    "parser": {
                        "value": None,
                    },
                    "vlm": {
                        "candidate": "INV-RECOVER-001",
                    },
                    "resolution": "candidate_for_review",
                    "final_value": None,
                    "review_required": True,
                }
            },
        }

        reconciliation_path = (
            self.root / "runs" / "reconciliation.json"
        )

        with (
            patch.object(
                dis,
                "run_attempt",
                return_value=(
                    selective_dir,
                    selective_record,
                ),
            ) as ai,
            patch.object(
                dis,
                "reconcile",
                return_value=(
                    reconciliation_path,
                    reconciliation_result,
                ),
            ) as reconcile,
        ):
            _, result = self.service.process_document(
                document_id="DOC-RECOVERY",
                file_path=self.pdf,
                allow_vlm_inference=True,
            )

        self.assertEqual(
            result["status"],
            "candidate_for_review",
        )

        self.assertTrue(
            result["ai_assistance"]["attempted"]
        )

        self.assertTrue(
            result["ai_assistance"]["response_received"]
        )

        self.assertEqual(
            result["ai_assistance"]["prediction"],
            {
                "invoice_number": "INV-RECOVER-001"
            },
        )

        field = result["reconciliation"][
            "fields"
        ]["invoice_number"]

        self.assertEqual(
            field["vlm"]["candidate"],
            "INV-RECOVER-001",
        )

        self.assertEqual(
            field["resolution"],
            "candidate_for_review",
        )

        self.assertIsNone(
            field["final_value"]
        )

        self.assertFalse(
            result["accounting_approval_performed"]
        )

        ai.assert_called_once()
        reconcile.assert_called_once()

    def test_failed_ai_attempt_requires_review(self):
        decision = {
            "decision": "vlm_candidate",
            "requested_fields": ["invoice_number"],
            "candidate_reasons": [],
            "blocking_reasons": [],
        }

        self.prepare_common_mocks(decision)

        selective_dir = (
            self.root / "runs" / "selective-failed"
        )
        selective_dir.mkdir()

        selective_record = {
            "status": "failed",
            "ai_request_attempted": True,
            "ai_response_received": False,
        }

        with patch.object(
            dis,
            "run_attempt",
            return_value=(
                selective_dir,
                selective_record,
            ),
        ):
            _, result = self.service.process_document(
                document_id="DOC-AI-FAIL",
                file_path=self.pdf,
                allow_vlm_inference=True,
            )

        self.assertEqual(
            result["status"],
            "ai_attempt_failed_needs_review",
        )

        self.assertTrue(
            result["ai_assistance"]["attempted"]
        )

        self.assertFalse(
            result["ai_assistance"]["response_received"]
        )

        self.assertTrue(
            result["review_required"]
        )

        self.assertFalse(
            result["accounting_approval_performed"]
        )


if __name__ == "__main__":
    unittest.main()
