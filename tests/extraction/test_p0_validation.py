"""Regression tests for the six confirmed P0 boundary defects; no AI calls."""
import json
import unittest

from . import test_reconcile_selective as reconcile_tests
from . import test_module2_public_result as public_tests


def write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


class P0ValidationTests(unittest.TestCase):
    def reconciliation_case(self):
        tmp, extraction, selective, output = (
            reconcile_tests.ReconcileSelectiveTests().prepare_case(
                vlm_value="INV-RECOVER-001"
            )
        )
        self.addCleanup(tmp.cleanup)
        return extraction, selective, output

    def public_case(self):
        helper = public_tests.Module2PublicResultTests()
        helper.setUp()
        self.addCleanup(helper.tearDown)
        extraction, _ = helper.make_extraction("SYN-001", "resolved")
        service = helper.make_service(extraction, "parser_only_needs_review", "parser_only")
        return helper, extraction, service

    def test_m2_val_01_missing_requested_prediction_key_rejected(self):
        extraction, selective, output = self.reconciliation_case()
        write(selective / "prediction.json", {})
        with self.assertRaises(ValueError):
            reconcile_tests.reconcile(extraction, selective, output)
        self.assertFalse(output.exists())

    def test_m2_val_02_numeric_candidate_rejected(self):
        extraction, selective, output = self.reconciliation_case()
        write(selective / "prediction.json", {"invoice_number": 123})
        with self.assertRaises(ValueError):
            reconcile_tests.reconcile(extraction, selective, output)
        self.assertFalse(output.exists())

    def test_m2_val_03_failed_selective_status_rejected(self):
        extraction, selective, output = self.reconciliation_case()
        run = json.loads((selective / "run.json").read_text())
        run["status"] = "failed"
        write(selective / "run.json", run)
        with self.assertRaises(ValueError):
            reconcile_tests.reconcile(extraction, selective, output)
        self.assertFalse(output.exists())

    def test_m2_val_04_empty_vlm_request_rejected(self):
        extraction, selective, output = self.reconciliation_case()
        for name in ("run.json", "routing.json"):
            data = json.loads((selective / name).read_text())
            data["requested_fields"] = []
            write(selective / name, data)
        write(selective / "prediction.json", {})
        with self.assertRaises(ValueError):
            reconcile_tests.reconcile(extraction, selective, output)
        self.assertFalse(output.exists())

    def test_m2_val_05_empty_extraction_headers_rejected(self):
        helper, extraction, service = self.public_case()
        payload = json.loads(extraction.read_text())
        payload["fields"] = {}
        write(extraction, payload)
        with self.assertRaises(ValueError):
            public_tests.build_public_result(service, project_root=helper.root)
        self.assertEqual(list(helper.runs.glob("module2-public-*")), [])

    def test_m2_val_06_unknown_service_status_rejected(self):
        helper, extraction, service = self.public_case()
        payload = json.loads(service.read_text())
        payload["status"] = "unrecognised_status"
        write(service, payload)
        with self.assertRaises(ValueError):
            public_tests.build_public_result(service, project_root=helper.root)
        self.assertEqual(list(helper.runs.glob("module2-public-*")), [])

    def ai_public_case(self, candidate="INV-RECOVER-001", state="missing"):
        helper = public_tests.Module2PublicResultTests()
        helper.setUp()
        self.addCleanup(helper.tearDown)
        extraction, payload = helper.make_extraction(None, state)
        if state == "ambiguous":
            payload["fields"]["invoice_number"].update(
                raw_text="Invoice number: A / B",
                candidates=[{"raw_text": "Invoice number: " + value, "value": value,
                             "evidence": [{"page": 1, "source_ref": "#/texts/1", "text": value}]}
                            for value in ("A", "B")],
            )
            write(extraction, payload)
        reconciliation = helper.make_reconciliation(extraction, candidate)
        service = helper.make_service(extraction,
            "candidate_for_review" if candidate is not None else "unresolved_needs_review",
            "vlm_candidate", reconciliation)
        return helper, extraction, reconciliation, service

    def test_invalid_candidate_types_and_blank_strings_rejected(self):
        for candidate in (True, 0, 1.5, [], {}, "", "   "):
            with self.subTest(candidate=candidate):
                extraction, selective, output = self.reconciliation_case()
                write(selective / "prediction.json", {"invoice_number": candidate})
                with self.assertRaises(ValueError):
                    reconcile_tests.reconcile(extraction, selective, output)

    def test_missing_unknown_and_incomplete_run_statuses_rejected(self):
        for status in (None, "preparing", "success", "unknown"):
            with self.subTest(status=status):
                extraction, selective, output = self.reconciliation_case()
                run = json.loads((selective / "run.json").read_text())
                run["status"] = status
                write(selective / "run.json", run)
                with self.assertRaises(ValueError):
                    reconcile_tests.reconcile(extraction, selective, output)

    def test_duplicate_unsupported_and_malformed_request_scope_rejected(self):
        for requested in (["invoice_number", "invoice_number"], ["unapproved"], "invoice_number", None):
            with self.subTest(requested=requested):
                extraction, selective, output = self.reconciliation_case()
                for name in ("run.json", "routing.json"):
                    payload = json.loads((selective / name).read_text())
                    payload["requested_fields"] = requested
                    write(selective / name, payload)
                with self.assertRaises(ValueError):
                    reconcile_tests.reconcile(extraction, selective, output)

    def test_extra_prediction_key_and_scope_disagreement_rejected(self):
        for mutation in ("prediction", "scope"):
            with self.subTest(mutation=mutation):
                extraction, selective, output = self.reconciliation_case()
                if mutation == "prediction":
                    write(selective / "prediction.json", {"invoice_number": None, "total_amount": "999"})
                else:
                    run = json.loads((selective / "run.json").read_text())
                    run["requested_fields"] = ["total_amount"]
                    write(selective / "run.json", run)
                with self.assertRaises(ValueError):
                    reconcile_tests.reconcile(extraction, selective, output)

    def test_routing_and_selective_identity_guards_preserved(self):
        mutations = (("run.json", "document_id", "OTHER"),
                     ("routing.json", "document_id", "OTHER"),
                     ("routing.json", "extraction_run_id", "OTHER"),
                     ("run.json", "extraction_run_id", "OTHER"),
                     ("routing.json", "decision", "human_review"),
                     ("run.json", "decision", "parser_only"),
                     ("run.json", "ai_request_attempted", False),
                     ("run.json", "ai_response_received", False))
        for name, key, value in mutations:
            with self.subTest(file=name, key=key):
                extraction, selective, output = self.reconciliation_case()
                payload = json.loads((selective / name).read_text())
                payload[key] = value
                write(selective / name, payload)
                with self.assertRaises(ValueError):
                    reconcile_tests.reconcile(extraction, selective, output)

    def test_reconciliation_source_path_mismatch_rejected(self):
        extraction, selective, output = self.reconciliation_case()
        run = json.loads((selective / "run.json").read_text())
        other = extraction.with_name("other.json")
        other.write_bytes(extraction.read_bytes())
        run["source_result_path"] = str(other)
        write(selective / "run.json", run)
        with self.assertRaises(ValueError):
            reconcile_tests.reconcile(extraction, selective, output)

    def test_reconciliation_rejects_changed_extraction_bytes(self):
        extraction, selective, output = self.reconciliation_case()
        payload = json.loads(extraction.read_text())
        payload["fields"]["supplier_name"]["value"] = "CHANGED"
        write(extraction, payload)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            reconcile_tests.reconcile(extraction, selective, output)

    def test_resolved_parser_value_and_all_source_bytes_preserved(self):
        tmp, extraction, selective, output = reconcile_tests.ReconcileSelectiveTests().prepare_case(
            parser_value="PARSER-001", parser_state="resolved", vlm_value="AI-999")
        self.addCleanup(tmp.cleanup)
        sources = [extraction] + list(selective.iterdir())
        before = {path: path.read_bytes() for path in sources}
        _, result = reconcile_tests.reconcile(extraction, selective, output)
        field = result["fields"]["invoice_number"]
        self.assertEqual(field["final_value"], "PARSER-001")
        self.assertEqual(field["vlm"]["candidate"], "AI-999")
        self.assertTrue(result["review_required"])
        self.assertTrue(all(value is False for value in result["safety"].values()))
        self.assertEqual(before, {path: path.read_bytes() for path in sources})

    def test_public_rejects_valid_status_with_wrong_routing(self):
        helper, extraction, service = self.public_case()
        payload = json.loads(service.read_text())
        payload["status"] = "candidate_for_review"
        write(service, payload)
        with self.assertRaises(ValueError):
            public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_processing_failed_and_missing_status(self):
        for status in ("processing", "failed_needs_review", None):
            with self.subTest(status=status):
                helper, extraction, service = self.public_case()
                payload = json.loads(service.read_text())
                payload["status"] = status
                write(service, payload)
                with self.assertRaises(ValueError):
                    public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_false_review_and_automatic_approval(self):
        for key, value in (("review_required", False), ("accounting_approval_performed", True)):
            with self.subTest(key=key):
                helper, extraction, service = self.public_case()
                payload = json.loads(service.read_text())
                payload[key] = value
                write(service, payload)
                with self.assertRaises(ValueError):
                    public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_skipped_status_with_attempted_ai(self):
        helper, extraction, service = self.public_case()
        payload = json.loads(service.read_text())
        payload["ai_assistance"]["attempted"] = True
        write(service, payload)
        with self.assertRaises(ValueError):
            public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_tampered_parser_only_extraction(self):
        helper, extraction, service = self.public_case()
        payload = json.loads(extraction.read_text())
        payload["fields"]["invoice_number"]["value"] = "ALTERED"
        write(extraction, payload)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_malformed_line_item_and_field_state(self):
        for mutation in ("row", "state"):
            with self.subTest(mutation=mutation):
                helper, extraction, service = self.public_case()
                payload = json.loads(extraction.read_text())
                if mutation == "row":
                    del payload["line_items"][0]["quantity"]
                else:
                    payload["fields"]["invoice_number"]["state"] = "missing"
                write(extraction, payload)
                with self.assertRaises(ValueError):
                    public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_tampered_prediction_artifact(self):
        helper, extraction, reconciliation, service = self.ai_public_case()
        write(helper.runs / "selective-test" / "prediction.json", {"invoice_number": "ALTERED"})
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_tampered_selective_run_status_and_source(self):
        for key, value in (("status", "failed"), ("source_result_sha256", "0" * 64),
                           ("document_id", "OTHER")):
            with self.subTest(key=key):
                helper, extraction, reconciliation, service = self.ai_public_case()
                run_path = helper.runs / "selective-test" / "run.json"
                payload = json.loads(run_path.read_text())
                payload[key] = value
                write(run_path, payload)
                with self.assertRaises(ValueError):
                    public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_success_status_without_successful_ai(self):
        for key, value in (("attempted", False), ("response_received", False), ("prediction", None)):
            with self.subTest(key=key):
                helper, extraction, reconciliation, service = self.ai_public_case()
                payload = json.loads(service.read_text())
                payload["ai_assistance"][key] = value
                write(service, payload)
                with self.assertRaises(ValueError):
                    public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_reconciled_evidence_change_and_auto_acceptance(self):
        for mutation in ("evidence", "final_value", "resolution", "review_required"):
            with self.subTest(mutation=mutation):
                helper, extraction, reconciliation, service = self.ai_public_case()
                payload = json.loads(reconciliation.read_text())
                field = payload["fields"]["invoice_number"]
                if mutation == "evidence":
                    field["parser"]["evidence"] = [{"text": "fabricated"}]
                elif mutation == "final_value":
                    field["final_value"] = "INV-RECOVER-001"
                elif mutation == "resolution":
                    field["resolution"] = "approved"
                else:
                    field["review_required"] = False
                write(reconciliation, payload)
                with self.assertRaises(ValueError):
                    public_tests.build_public_result(service, project_root=helper.root)

    def test_public_rejects_contradictory_reconciliation_outcome(self):
        helper, extraction, reconciliation, service = self.ai_public_case()
        payload = json.loads(service.read_text())
        payload["status"] = "unresolved_needs_review"
        payload["reconciliation"]["status"] = "unresolved_needs_review"
        write(service, payload)
        with self.assertRaises(ValueError):
            public_tests.build_public_result(service, project_root=helper.root)

    def test_public_ambiguous_state_evidence_and_inputs_preserved(self):
        helper, extraction, reconciliation, service = self.ai_public_case(candidate=None, state="ambiguous")
        before = {path: path.read_bytes() for path in helper.runs.rglob("*.json")}
        original = json.loads(extraction.read_text())
        _, result = public_tests.build_public_result(service, project_root=helper.root)
        for name, source in original["fields"].items():
            for key in ("value", "state", "raw_text", "evidence", "candidates", "method", "review_required"):
                self.assertEqual(result["fields"][name][key], source[key])
        self.assertEqual(result["fields"]["invoice_number"]["state"], "ambiguous")
        for before_row, after_row in zip(original["line_items"], result["line_items"]):
            for name, source in before_row.items():
                for key in ("value", "state", "raw_text", "evidence", "candidates", "method", "review_required"):
                    self.assertEqual(after_row[name][key], source[key])
        self.assertEqual(result["issues"], original["issues"])
        self.assertEqual(result["provenance"]["classified_extraction_sha256"], public_tests.sha256(extraction))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_provider_failure_remains_publishable_review_outcome(self):
        helper = public_tests.Module2PublicResultTests()
        helper.setUp()
        self.addCleanup(helper.tearDown)
        extraction, _ = helper.make_extraction(None, "missing")
        service = helper.make_service(extraction, "ai_attempt_failed_needs_review", "vlm_candidate")
        _, result = public_tests.build_public_result(service, project_root=helper.root)
        self.assertIsNone(result["fields"]["invoice_number"]["value"])
        self.assertIsNone(result["fields"]["invoice_number"]["ai_candidate"])
        self.assertTrue(result["review_required"])
        self.assertFalse(result["accounting_approval_performed"])

    def test_human_review_blocker_remains_publishable_without_ai(self):
        helper, extraction, service = self.public_case()
        payload = json.loads(extraction.read_text())
        payload["fields"]["tax_amount"].update(value=None, state="missing")
        write(extraction, payload)
        service = helper.make_service(extraction, "human_review_required", "human_review")
        _, result = public_tests.build_public_result(service, project_root=helper.root)
        self.assertEqual(result["status"], "human_review_required")
        self.assertTrue(result["review_required"])


if __name__ == "__main__":
    unittest.main()
