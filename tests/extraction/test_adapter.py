import copy
import math
import unittest
from decimal import Decimal
from typing import Any, Dict

from services.extraction.contracts.adapter import adapt_m2_to_m3, integrate_m2_with_m3


def create_mock_m2_result(overrides: Dict[str, Any] = None) -> Dict[str, Any]:
    base = {
        "schema_version": "0.1.0-proposed",
        "mode": "module2_public_result",
        "document_id": "TEST-001",
        "document_type": {"value": "invoice", "status": "classified"},
        "status": "candidate_for_review",
        "review_required": True,
        "accounting_approval_performed": False,
        "safety": {
            "ai_candidate_auto_accepted": False,
            "automatic_accounting_approval": False
        },
        "provenance": {"service_run_id": "run-123"},
        "fields": {
            "supplier_name": {"value": "Acme Corp", "state": "resolved"},
            "invoice_number": {"value": "INV-100", "state": "resolved"},
            "invoice_date": {"value": "2026-01-01", "state": "resolved"},
            "due_date": {"value": "2026-01-31", "state": "resolved"},
            "currency": {"value": "USD", "state": "resolved"},
            "subtotal": {"value": "1,000.00", "state": "resolved"},
            "tax_amount": {"value": "100.00", "state": "resolved"},
            "total_amount": {"value": "1,100.00", "state": "resolved"},
        },
        "line_items": [
            {
                "description": {"value": "Widgets", "state": "resolved"},
                "quantity": {"value": "10", "state": "resolved"},
                "unit_price": {"value": "100.00", "state": "resolved"},
                "line_total": {"value": "1,000.00", "state": "resolved"},
            }
        ]
    }
    
    if overrides:
        if "fields" in overrides:
            if isinstance(overrides["fields"], dict) and isinstance(base.get("fields"), dict):
                base["fields"].update(overrides["fields"])
            else:
                base["fields"] = overrides["fields"]
        if "line_items" in overrides:
            base["line_items"] = overrides["line_items"]
        for k, v in overrides.items():
            if k not in ("fields", "line_items"):
                base[k] = v
            
    return base


class TestM2ToM3Adapter(unittest.TestCase):
    
    def test_correct_mapping_and_decimal_conversion(self):
        m2_res = create_mock_m2_result()
        res = adapt_m2_to_m3(m2_res)
        
        self.assertEqual(res["status"], "READY_FOR_M3")
        self.assertEqual(len(res["structural_issues"]), 0)
        
        m3_input = res["m3_input"]
        self.assertEqual(m3_input["vendor_name"], "Acme Corp")
        self.assertEqual(m3_input["invoice_number"], "INV-100")
        self.assertEqual(m3_input["invoice_date"], "2026-01-01")
        self.assertEqual(m3_input["due_date"], "2026-01-31")
        self.assertEqual(m3_input["currency"], "USD")
        
        self.assertEqual(m3_input["subtotal"], Decimal("1000.00"))
        self.assertEqual(m3_input["tax_amount"], Decimal("100.00"))
        self.assertEqual(m3_input["total_amount"], Decimal("1100.00"))
        
        self.assertEqual(len(m3_input["line_items"]), 1)
        item = m3_input["line_items"][0]
        self.assertEqual(item["description"], "Widgets")
        self.assertEqual(item["quantity"], 10.0)
        self.assertEqual(item["unit_price"], Decimal("100.00"))
        self.assertEqual(item["amount"], Decimal("1000.00"))

    def test_missing_supplier_name(self):
        m2_res = create_mock_m2_result({
            "fields": {"supplier_name": {"value": None, "state": "missing", "ai_candidate": "Acme Corp"}}
        })
        res = adapt_m2_to_m3(m2_res)
        
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertIsNone(res["m3_input"])
        issue = res["structural_issues"][0]
        self.assertEqual(issue["field"], "vendor_name")
        self.assertEqual(issue["code"], "M2-STRUCTURAL-001")

    def test_missing_line_item_property(self):
        m2_res = create_mock_m2_result()
        m2_res["line_items"][0]["unit_price"] = {"value": None, "state": "missing"}
        
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["field"], "line_items[0].unit_price")

    def test_empty_line_items(self):
        m2_res = create_mock_m2_result()
        m2_res["line_items"] = []
        
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["field"], "line_items")

    def test_bad_decimal_conversion(self):
        m2_res = create_mock_m2_result({
            "fields": {"subtotal": {"value": "abc", "state": "resolved"}}
        })
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-002")

    def test_ambiguous_conflicting_fields(self):
        m2_res = create_mock_m2_result({
            "fields": {"invoice_number": {"value": "123", "state": "conflict"}}
        })
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-001")

    def test_integration_valid_input(self):
        m2_res = create_mock_m2_result()
        res = integrate_m2_with_m3(m2_res, known_vendors=[{"vendor_name": "Acme Corp", "id": "V-100"}])
        
        self.assertEqual(res["workflow_status"], "M3_VALIDATION_COMPLETE")
        m3_res = res["m3_validation_result"]
        self.assertEqual(m3_res["status"], "VALID")

    def test_integration_passes_explicit_transaction_history_to_exception_validation(self):
        m2_res = create_mock_m2_result()
        history = [
            {"transaction_id": f"H{i}", "amount": str(amount), "currency": "USD"}
            for i, amount in enumerate((95, 98, 100, 102, 105), start=1)
        ]
        res = integrate_m2_with_m3(
            m2_res,
            known_vendors=[{"vendor_name": "Acme Corp", "id": "V-100"}],
            existing_invoices=[],
            reference_transactions=history,
        )
        validation = res["m3_validation_result"]
        self.assertEqual(res["workflow_status"], "M3_VALIDATION_COMPLETE")
        self.assertEqual(validation["transaction_anomaly"]["status"], "anomaly")
        self.assertTrue(any(error["code"] == "EX-010" for error in validation["errors"]))

    def test_integration_m3_math_failure(self):
        m2_res = create_mock_m2_result({
            "fields": {"total_amount": {"value": "9999.00", "state": "resolved"}}
        })
        res = integrate_m2_with_m3(m2_res)
        
        self.assertEqual(res["workflow_status"], "M3_VALIDATION_COMPLETE")
        m3_res = res["m3_validation_result"]
        self.assertEqual(m3_res["status"], "REVIEW_REQUIRED")
        self.assertTrue(any(e["code"] == "EX-005" for e in m3_res["errors"]))

    def test_integration_structural_block(self):
        m2_res = create_mock_m2_result({
            "fields": {"supplier_name": {"value": None, "state": "missing"}}
        })
        res = integrate_m2_with_m3(m2_res)
        
        self.assertEqual(res["workflow_status"], "BLOCKED_BY_STRUCTURE")
        self.assertIsNone(res["m3_validation_result"])

    # -------------------------------------------------------------
    # NEW REGRESSION & HARDENING TESTS
    # -------------------------------------------------------------
    
    def test_incorrect_schema_and_mode(self):
        m2_res = create_mock_m2_result({"schema_version": "1.0.0", "mode": "unknown"})
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-005")

        m2_res2 = create_mock_m2_result({"mode": "wrong"})
        res2 = adapt_m2_to_m3(m2_res2)
        self.assertEqual(res2["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res2["structural_issues"][0]["code"], "M2-STRUCTURAL-006")

    def test_missing_invalid_document_type(self):
        m2_res = create_mock_m2_result({"document_type": {"value": "receipt", "status": "classified"}})
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-007")
        
        m2_res2 = create_mock_m2_result({"document_type": "not_a_dict"})
        res2 = adapt_m2_to_m3(m2_res2)
        self.assertEqual(res2["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res2["structural_issues"][0]["code"], "M2-STRUCTURAL-007")

    def test_safety_invariants(self):
        m2_res = create_mock_m2_result({"review_required": False})
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-010")

        m2_res2 = create_mock_m2_result({"safety": {"ai_candidate_auto_accepted": True, "automatic_accounting_approval": False}})
        res2 = adapt_m2_to_m3(m2_res2)
        self.assertEqual(res2["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res2["structural_issues"][0]["code"], "M2-STRUCTURAL-010")

        m2_res3 = create_mock_m2_result({"accounting_approval_performed": True})
        res3 = adapt_m2_to_m3(m2_res3)
        self.assertEqual(res3["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res3["structural_issues"][0]["code"], "M2-STRUCTURAL-011")

        m2_res4 = create_mock_m2_result({"status": "unknown_status"})
        res4 = adapt_m2_to_m3(m2_res4)
        self.assertEqual(res4["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res4["structural_issues"][0]["code"], "M2-STRUCTURAL-009")

    def test_malformed_fields_rows_cells(self):
        m2_res = create_mock_m2_result({"fields": "not_a_dict"})
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-004")
        
        m2_res2 = create_mock_m2_result({"fields": {"subtotal": "not_a_dict"}})
        res2 = adapt_m2_to_m3(m2_res2)
        self.assertEqual(res2["structural_issues"][0]["code"], "M2-STRUCTURAL-001")
        
        m2_res3 = create_mock_m2_result()
        m2_res3["line_items"].append("not_a_dict")
        res3 = adapt_m2_to_m3(m2_res3)
        self.assertEqual(res3["structural_issues"][0]["code"], "M2-STRUCTURAL-004")

    def test_nan_infinity_amounts(self):
        m2_res = create_mock_m2_result({"fields": {"subtotal": {"value": "NaN", "state": "resolved"}}})
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-002")
        
        m2_res2 = create_mock_m2_result({"fields": {"subtotal": {"value": "Infinity", "state": "resolved"}}})
        res2 = adapt_m2_to_m3(m2_res2)
        self.assertEqual(res2["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res2["structural_issues"][0]["code"], "M2-STRUCTURAL-002")

    def test_nan_infinity_quantities(self):
        m2_res = create_mock_m2_result()
        m2_res["line_items"][0]["quantity"] = {"value": "NaN", "state": "resolved"}
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-003")

        m2_res["line_items"][0]["quantity"] = {"value": "Infinity", "state": "resolved"}
        res2 = adapt_m2_to_m3(m2_res)
        self.assertEqual(res2["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res2["structural_issues"][0]["code"], "M2-STRUCTURAL-003")

    def test_malformed_numeric_strings(self):
        m2_res = create_mock_m2_result({"fields": {"total_amount": {"value": "1,000.00.00", "state": "resolved"}}})
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-002")

    def test_unresolved_ai_candidates(self):
        # Even if ai_candidate is perfect, if state isn't resolved, it's blocked.
        m2_res = create_mock_m2_result({"fields": {"invoice_number": {"value": None, "state": "missing", "ai_candidate": "INV-123"}}})
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertIsNone(res["m3_input"])
        self.assertEqual(res["structural_issues"][0]["code"], "M2-STRUCTURAL-001")

    def test_evidence_and_provenance_retention(self):
        m2_res = create_mock_m2_result()
        res = integrate_m2_with_m3(m2_res, known_vendors=[{"vendor_name": "Acme Corp", "id": "V-100"}])
        self.assertEqual(res["m2_provenance"]["service_run_id"], "run-123")
        self.assertIn("original_m2_result", res)
        self.assertEqual(res["original_m2_result"]["document_id"], "TEST-001")
        # Ensure M3 VALID is retained but not confused with approval
        self.assertEqual(res["m3_validation_result"]["status"], "VALID")

    def test_original_input_not_mutated(self):
        m2_res = create_mock_m2_result()
        m2_res_copy = copy.deepcopy(m2_res)
        adapt_m2_to_m3(m2_res)
        self.assertEqual(m2_res, m2_res_copy)

    def test_invalid_dates_and_currencies_passed_safely(self):
        # Dates and currencies are strings, so they pass structural M2 validation,
        # but they should fail Pydantic M3 validation.
        m2_res = create_mock_m2_result({
            "fields": {
                "invoice_date": {"value": "not-a-date", "state": "resolved"},
                "currency": {"value": "US", "state": "resolved"}
            }
        })
        res = integrate_m2_with_m3(m2_res, known_vendors=[{"vendor_name": "Acme Corp", "id": "V-100"}])
        self.assertEqual(res["workflow_status"], "M3_VALIDATION_COMPLETE")
        self.assertEqual(res["m3_validation_result"]["status"], "REVIEW_REQUIRED")
        errors = [e["code"] for e in res["m3_validation_result"]["errors"]]
        self.assertIn("EX-002", errors) # Invalid format error

    def test_missing_tax_amount(self):
        m2_res = create_mock_m2_result({"fields": {"tax_amount": {"value": None, "state": "missing"}}})
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertEqual(res["structural_issues"][0]["field"], "tax_amount")

    def test_no_known_vendor_context(self):
        m2_res = create_mock_m2_result()
        # No vendor context passed
        res = integrate_m2_with_m3(m2_res)
        self.assertEqual(res["workflow_status"], "M3_VALIDATION_COMPLETE")
        self.assertEqual(res["m3_validation_result"]["status"], "VALID")
        self.assertEqual(res["m3_validation_result"]["errors"], [])
        self.assertEqual(res["m3_validation_result"]["vendor_match"]["status"], "NOT_CHECKED")

    def test_unexpected_downstream_validation_errors(self):
        # Mock M3 engine to throw a wild exception
        m2_res = create_mock_m2_result()
        import validation.documents.engine
        original_val = validation.documents.engine.validate_invoice_document
        
        def broken_val(*args, **kwargs):
            raise RuntimeError("Database exploded")
            
        validation.documents.engine.validate_invoice_document = broken_val
        try:
            res = integrate_m2_with_m3(m2_res)
            self.assertEqual(res["workflow_status"], "M3_EXECUTION_FAILED")
            self.assertIsNone(res["m3_validation_result"])
            self.assertEqual(res["m3_execution_error"], "An unexpected system error occurred during accounting validation.")
        finally:
            validation.documents.engine.validate_invoice_document = original_val



    def test_permitted_statuses(self):
        permitted = [
            "parser_only_needs_review",
            "human_review_required",
            "ai_attempt_failed_needs_review",
            "candidate_for_review",
            "unresolved_needs_review",
            "parser_preserved"
        ]
        for st in permitted:
            m2_res = create_mock_m2_result({"status": st})
            res = adapt_m2_to_m3(m2_res)
            self.assertEqual(res["status"], "READY_FOR_M3", f"Failed for valid status: {st}")

    def test_case_1_parser_only_resolved(self):
        """Case 1: parser-only, resolved invoice fields map to M3 successfully."""
        m2_res = create_mock_m2_result({"status": "parser_only_needs_review"})
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "READY_FOR_M3")
        self.assertIsNotNone(res["m3_input"])

    def test_case_2_unresolved_invoice_number(self):
        """Case 2: unresolved invoice number produces structural review instead of status failure."""
        m2_res = create_mock_m2_result({
            "status": "unresolved_needs_review",
            "fields": {"invoice_number": {"value": None, "state": "missing"}}
        })
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        # Ensure it failed because of the missing invoice_number, not because of status
        codes = [issue["code"] for issue in res["structural_issues"]]
        self.assertIn("M2-STRUCTURAL-001", codes)
        self.assertNotIn("M2-STRUCTURAL-009", codes)

    def test_case_3_unaccepted_ai_candidate(self):
        """Case 3: unaccepted AI candidate remains unresolved and cannot be promoted."""
        m2_res = create_mock_m2_result({
            "status": "candidate_for_review",
            "fields": {"invoice_number": {"value": None, "state": "missing", "ai_candidate": "INV-123"}}
        })
        res = adapt_m2_to_m3(m2_res)
        self.assertEqual(res["status"], "STRUCTURAL_REVIEW_REQUIRED")
        codes = [issue["code"] for issue in res["structural_issues"]]
        self.assertIn("M2-STRUCTURAL-001", codes)

if __name__ == "__main__":
    unittest.main()

