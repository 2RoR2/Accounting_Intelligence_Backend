import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from .test_route_extraction import fixture


from services.extraction.classification.reconcile_selective import reconcile


def sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReconcileSelectiveTests(unittest.TestCase):

    def prepare_case(
        self,
        parser_value=None,
        parser_state="missing",
        vlm_value=None,
    ):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)

        extraction_dir = root / "extraction-test"
        extraction_dir.mkdir()

        extraction_path = extraction_dir / "result.json"

        extraction = fixture()
        extraction.update(document_id="DOC-TEST", extraction_run_id="extraction-test")
        extraction["fields"]["invoice_number"] = {
            "raw_text": parser_value,
            "value": parser_value,
            "evidence": [],
            "candidates": [],
            "state": parser_state,
            "method": "parser_mapping",
            "legacy_mapping_status": "mapped" if parser_state == "resolved" else parser_state,
            "review_required": True,
        }

        extraction_path.write_text(
            json.dumps(extraction),
            encoding="utf-8",
        )

        selective = root / "selective-test"
        selective.mkdir()

        routing = {
            "policy_version": "0.1.0-proposed",
            "document_id": "DOC-TEST",
            "extraction_run_id": "extraction-test",
            "decision": "vlm_candidate",
            "requested_fields": ["invoice_number"],
        }

        run = {
            "status": "response_structure_valid_needs_review",
            "document_id": "DOC-TEST",
            "decision": "vlm_candidate",
            "ai_request_attempted": True,
            "ai_response_received": True,
            "source_result_path": str(extraction_path.resolve()),
            "source_result_sha256": sha256(extraction_path),
            "requested_fields": ["invoice_number"],
            "model": "qwen2.5vl:7b",
            "source_pdf": "synthetic.pdf",
            "source_pdf_sha256": "a" * 64,
            "image": "page-1.png",
            "image_sha256": "b" * 64,
            "page": 1,
        }

        prediction = {
            "invoice_number": vlm_value,
        }

        (selective / "routing.json").write_text(
            json.dumps(routing),
            encoding="utf-8",
        )

        (selective / "run.json").write_text(
            json.dumps(run),
            encoding="utf-8",
        )

        (selective / "prediction.json").write_text(
            json.dumps(prediction),
            encoding="utf-8",
        )

        return tmp, extraction_path, selective, root / "runs"

    def test_missing_parser_value_with_vlm_candidate_requires_review(self):
        tmp, extraction, selective, output = self.prepare_case(
            parser_value=None,
            parser_state="missing",
            vlm_value="INV-RECOVER-001",
        )

        try:
            _, result = reconcile(
                extraction,
                selective,
                output,
            )

            field = result["fields"]["invoice_number"]

            self.assertEqual(
                field["resolution"],
                "candidate_for_review",
            )

            self.assertIsNone(field["parser"]["value"])

            self.assertEqual(
                field["vlm"]["candidate"],
                "INV-RECOVER-001",
            )

            self.assertIsNone(field["final_value"])

            self.assertTrue(field["review_required"])

            self.assertFalse(
                result["safety"]["parser_result_overwritten"]
            )

            self.assertFalse(
                result["safety"]["ai_candidate_auto_accepted"]
            )

        finally:
            tmp.cleanup()

    def test_missing_parser_value_with_null_vlm_remains_unresolved(self):
        tmp, extraction, selective, output = self.prepare_case(
            parser_value=None,
            parser_state="missing",
            vlm_value=None,
        )

        try:
            _, result = reconcile(
                extraction,
                selective,
                output,
            )

            field = result["fields"]["invoice_number"]

            self.assertEqual(
                field["resolution"],
                "unresolved_missing",
            )

            self.assertIsNone(field["vlm"]["candidate"])
            self.assertIsNone(field["final_value"])
            self.assertTrue(field["review_required"])

        finally:
            tmp.cleanup()

    def test_existing_parser_value_is_never_overwritten(self):
        tmp, extraction, selective, output = self.prepare_case(
            parser_value="INV-PARSER-001",
            parser_state="resolved",
            vlm_value="INV-AI-999",
        )

        try:
            _, result = reconcile(
                extraction,
                selective,
                output,
            )

            field = result["fields"]["invoice_number"]

            self.assertEqual(
                field["resolution"],
                "parser_preserved",
            )

            self.assertEqual(
                field["parser"]["value"],
                "INV-PARSER-001",
            )

            self.assertEqual(
                field["final_value"],
                "INV-PARSER-001",
            )

            self.assertNotEqual(
                field["final_value"],
                field["vlm"]["candidate"],
            )

            self.assertFalse(
                result["safety"]["parser_result_overwritten"]
            )

        finally:
            tmp.cleanup()

    def test_hash_mismatch_is_rejected(self):
        tmp, extraction, selective, output = self.prepare_case(
            parser_value=None,
            parser_state="missing",
            vlm_value="INV-RECOVER-001",
        )

        try:
            run_path = selective / "run.json"
            run = json.loads(run_path.read_text())
            run["source_result_sha256"] = "0" * 64
            run_path.write_text(json.dumps(run))

            with self.assertRaisesRegex(
                ValueError,
                "SHA-256",
            ):
                reconcile(
                    extraction,
                    selective,
                    output,
                )

        finally:
            tmp.cleanup()

    def test_conflicting_parser_candidates_are_preserved_beside_unverified_ai(self):
        tmp,extraction,selective,output=self.prepare_case(parser_state='ambiguous',vlm_value='AI-THIRD')
        try:
            original=json.loads(extraction.read_text())
            candidates=[{'raw_text':v,'value':v,'evidence':[]} for v in ('PARSER-A','PARSER-B')]
            original['fields']['invoice_number']['candidates']=candidates
            extraction.write_text(json.dumps(original))
            run=json.loads((selective/'run.json').read_text());run['source_result_sha256']=sha256(extraction)
            (selective/'run.json').write_text(json.dumps(run))
            _,result=reconcile(extraction,selective,output)
            field=result['fields']['invoice_number']
            self.assertEqual(field['parser']['candidates'],candidates)
            self.assertEqual(field['vlm']['candidate'],'AI-THIRD')
            self.assertIsNone(field['final_value']);self.assertTrue(field['review_required'])
            self.assertEqual(json.loads(extraction.read_text()),original)
        finally:tmp.cleanup()

    def test_duplicate_stored_predictions_are_rejected(self):
        tmp,extraction,selective,output=self.prepare_case(vlm_value='A')
        try:
            (selective/'prediction.json').write_text('{"invoice_number":"A","invoice_number":"B"}')
            with self.assertRaisesRegex(ValueError,'Duplicate JSON field'):reconcile(extraction,selective,output)
        finally:tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
