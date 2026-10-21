import re
from typing import Any, Dict, List, Optional
from decimal import Decimal, InvalidOperation

# Regex for strict numeric strings:
# Allows digits, optional proper thousands commas, and optional decimal.
STRICT_NUMERIC_REGEX = re.compile(r"^-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")

def convert_numeric(value_str: Any) -> Optional[Decimal]:
    """Safely convert a strict string/int value to Decimal, rejecting floats and bad grouping."""
    if value_str is None:
        return None
    try:
        if isinstance(value_str, str):
            value_str = value_str.strip()
            if not STRICT_NUMERIC_REGEX.match(value_str):
                return None
            clean = value_str.replace(",", "")
            val = Decimal(clean)
        elif isinstance(value_str, int) and not isinstance(value_str, bool):
            val = Decimal(value_str)
        elif isinstance(value_str, Decimal):
            val = value_str
        else:
            # Reject float to prevent binary float conversion issues
            return None
            
        if not val.is_finite():
            return None
        return val
    except (InvalidOperation, ValueError, TypeError):
        return None

def adapt_m2_to_m3(public_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adapt a validated M2 public result for M3 accounting validation.
    
    This function:
    1. Validates top-level schema version, mode, identity, and invariants.
    2. Checks if all M3-required fields are present and resolved.
    3. Does NOT guess or invent missing values.
    4. Leaves AI candidates as unaccepted unless explicitly resolved.
    5. Converts amounts and quantities safely to Decimal using strict formats.
    6. Returns structural issues if required fields are missing or malformed.
    
    Returns a dictionary with:
    - status: "READY_FOR_M3" or "STRUCTURAL_REVIEW_REQUIRED"
    - m3_input: The mapped dictionary for M3 (or None if structural error)
    - structural_issues: List of issues (empty if valid)
    - m2_provenance: Extracted provenance metadata
    """
    
    if not isinstance(public_result, dict):
        return {
            "status": "STRUCTURAL_REVIEW_REQUIRED",
            "m3_input": None,
            "structural_issues": [{"field": "root", "code": "M2-STRUCTURAL-005", "message": "Result is not a dictionary."}],
            "m2_provenance": None
        }

    structural_issues = []
    
    schema = public_result.get("schema_version")
    if schema != "0.1.0-proposed":
        structural_issues.append({
            "field": "schema_version",
            "code": "M2-STRUCTURAL-005",
            "message": f"Unsupported schema version: {schema}"
        })
        
    mode = public_result.get("mode")
    if mode != "module2_public_result":
        structural_issues.append({
            "field": "mode",
            "code": "M2-STRUCTURAL-006",
            "message": f"Unsupported mode: {mode}"
        })

    doc_id = public_result.get("document_id")
    if not isinstance(doc_id, str) or not doc_id.strip():
        structural_issues.append({
            "field": "document_id",
            "code": "M2-STRUCTURAL-008",
            "message": "Missing or malformed document_id"
        })

    doc_type = public_result.get("document_type")
    if not isinstance(doc_type, dict) or doc_type.get("value") != "invoice" or doc_type.get("status") != "classified":
        structural_issues.append({
            "field": "document_type",
            "code": "M2-STRUCTURAL-007",
            "message": "Missing or unsupported document_type"
        })

    status = public_result.get("status")
    PERMITTED_STATUSES = {
        "parser_only_needs_review",
        "human_review_required",
        "ai_attempt_failed_needs_review",
        "candidate_for_review",
        "unresolved_needs_review",
        "parser_preserved"
    }
    if status not in PERMITTED_STATUSES:
        structural_issues.append({
            "field": "status",
            "code": "M2-STRUCTURAL-009",
            "message": f"Missing or unpermitted extraction status: {status}"
        })

    if public_result.get("review_required") is not True:
        structural_issues.append({
            "field": "review_required",
            "code": "M2-STRUCTURAL-010",
            "message": "review_required must be explicitly True"
        })

    safety = public_result.get("safety")
    if not isinstance(safety, dict):
        structural_issues.append({
            "field": "safety",
            "code": "M2-STRUCTURAL-010",
            "message": "Missing or malformed safety metadata"
        })
    else:
        if safety.get("ai_candidate_auto_accepted") is not False:
            structural_issues.append({
                "field": "safety.ai_candidate_auto_accepted",
                "code": "M2-STRUCTURAL-010",
                "message": "ai_candidate_auto_accepted must be explicitly False"
            })
        if safety.get("automatic_accounting_approval") is not False:
            structural_issues.append({
                "field": "safety.automatic_accounting_approval",
                "code": "M2-STRUCTURAL-010",
                "message": "automatic_accounting_approval must be explicitly False"
            })
            
    if public_result.get("accounting_approval_performed") is not False:
        structural_issues.append({
            "field": "accounting_approval_performed",
            "code": "M2-STRUCTURAL-011",
            "message": "accounting_approval_performed must be explicitly False"
        })

    m3_input: Dict[str, Any] = {}
    
    provenance = public_result.get("provenance")
    if not isinstance(provenance, dict):
        provenance = None
        
    fields = public_result.get("fields")
    m2_line_items = public_result.get("line_items")

    if not isinstance(fields, dict):
        structural_issues.append({
            "field": "fields",
            "code": "M2-STRUCTURAL-004",
            "message": "Missing or malformed 'fields' dictionary."
        })
        fields = {}

    if not isinstance(m2_line_items, list):
        structural_issues.append({
            "field": "line_items",
            "code": "M2-STRUCTURAL-004",
            "message": "Missing or malformed 'line_items' list."
        })
        m2_line_items = []

    def extract_field(m2_field_name: str, m3_field_name: str, required: bool = False, is_numeric: bool = False):
        field_data = fields.get(m2_field_name)
        if not isinstance(field_data, dict):
            if required:
                structural_issues.append({
                    "field": m3_field_name,
                    "code": "M2-STRUCTURAL-001",
                    "message": f"Required field '{m3_field_name}' (from '{m2_field_name}') is missing or malformed."
                })
            return None
            
        value = field_data.get("value")
        state = field_data.get("state")
        
        if value is None or state != "resolved":
            if required:
                structural_issues.append({
                    "field": m3_field_name,
                    "code": "M2-STRUCTURAL-001",
                    "message": f"Required field '{m3_field_name}' (from '{m2_field_name}') is missing or unresolved. State: {state}"
                })
            return None
            
        if is_numeric:
            decimal_val = convert_numeric(value)
            if decimal_val is None:
                if required:
                    structural_issues.append({
                        "field": m3_field_name,
                        "code": "M2-STRUCTURAL-002",
                        "message": f"Required amount '{m3_field_name}' cannot be converted to finite Decimal: {value}"
                    })
                return None
            return decimal_val
            
        return value

    # M3 top-level fields
    m3_input["vendor_name"] = extract_field("supplier_name", "vendor_name", required=True)
    m3_input["invoice_number"] = extract_field("invoice_number", "invoice_number", required=True)
    m3_input["invoice_date"] = extract_field("invoice_date", "invoice_date", required=True)
    
    due_date = extract_field("due_date", "due_date", required=False)
    if due_date is not None:
        m3_input["due_date"] = due_date
        
    m3_input["currency"] = extract_field("currency", "currency", required=True)
    
    m3_input["subtotal"] = extract_field("subtotal", "subtotal", required=True, is_numeric=True)
    m3_input["tax_amount"] = extract_field("tax_amount", "tax_amount", required=True, is_numeric=True)
    m3_input["total_amount"] = extract_field("total_amount", "total_amount", required=True, is_numeric=True)

    # Line items
    m3_line_items = []
    
    if not m2_line_items:
        structural_issues.append({
            "field": "line_items",
            "code": "M2-STRUCTURAL-001",
            "message": "At least one line item is required."
        })
    else:
        for idx, row in enumerate(m2_line_items):
            if not isinstance(row, dict):
                structural_issues.append({
                    "field": f"line_items[{idx}]",
                    "code": "M2-STRUCTURAL-004",
                    "message": f"Line item {idx} is malformed."
                })
                continue
                
            m3_row: Dict[str, Any] = {}
            
            def extract_row_field(m2_col: str, m3_col: str, is_numeric: bool = False):
                cell = row.get(m2_col)
                if not isinstance(cell, dict):
                    structural_issues.append({
                        "field": f"line_items[{idx}].{m3_col}",
                        "code": "M2-STRUCTURAL-001",
                        "message": f"Line item {idx} missing or malformed field '{m3_col}'."
                    })
                    return None
                    
                val = cell.get("value")
                state = cell.get("state")
                
                if val is None or state != "resolved":
                    structural_issues.append({
                        "field": f"line_items[{idx}].{m3_col}",
                        "code": "M2-STRUCTURAL-001",
                        "message": f"Line item {idx} missing or unresolved required field '{m3_col}'."
                    })
                    return None
                    
                if is_numeric:
                    decimal_val = convert_numeric(val)
                    if decimal_val is None:
                        structural_issues.append({
                            "field": f"line_items[{idx}].{m3_col}",
                            "code": "M2-STRUCTURAL-002" if m3_col != "quantity" else "M2-STRUCTURAL-003",
                            "message": f"Line item {idx} amount/quantity '{m3_col}' cannot be converted to finite Decimal: {val}"
                        })
                        return None
                    return decimal_val
                        
                return val

            m3_row["description"] = extract_row_field("description", "description")
            m3_row["quantity"] = extract_row_field("quantity", "quantity", is_numeric=True)
            m3_row["unit_price"] = extract_row_field("unit_price", "unit_price", is_numeric=True)
            m3_row["amount"] = extract_row_field("line_total", "amount", is_numeric=True)
            
            m3_line_items.append(m3_row)
            
    if m3_line_items:
        m3_input["line_items"] = m3_line_items

    if structural_issues:
        return {
            "status": "STRUCTURAL_REVIEW_REQUIRED",
            "m3_input": None,
            "structural_issues": structural_issues,
            "m2_provenance": provenance
        }
        
    return {
        "status": "READY_FOR_M3",
        "m3_input": m3_input,
        "structural_issues": [],
        "m2_provenance": provenance
    }


def integrate_m2_with_m3(
    public_result: Dict[str, Any],
    known_vendors: Optional[List[Dict[str, Any]]] = None,
    existing_invoices: Optional[List[Dict[str, Any]]] = None,
    purchase_order: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Isolated workflow to connect validated M2 public result to M3 validation engine.
    Does not access databases or perform real-world side effects.
    
    Returns a dictionary with:
    - workflow_status: "M3_VALIDATION_COMPLETE", "BLOCKED_BY_STRUCTURE", or "M3_EXECUTION_FAILED"
    - m3_validation_result: Output from M3 validation engine (or None)
    - structural_issues: List of issues if blocked
    - m3_execution_error: String describing execution failure if applicable (no sensitive details)
    - m2_provenance: Extracted provenance metadata
    - original_m2_result: Original input
    """
    
    # 1. Adapt M2 to M3 input
    adapter_result = adapt_m2_to_m3(public_result)
    
    if adapter_result["status"] == "STRUCTURAL_REVIEW_REQUIRED":
        return {
            "workflow_status": "BLOCKED_BY_STRUCTURE",
            "structural_issues": adapter_result["structural_issues"],
            "m3_validation_result": None,
            "m2_provenance": adapter_result["m2_provenance"],
            "original_m2_result": public_result
        }
        
    # 2. Invoke M3 validation engine safely
    from validation.engine import validate_invoice_document
    
    try:
        m3_result = validate_invoice_document(
            invoice_data=adapter_result["m3_input"],
            known_vendors=known_vendors,
            existing_invoices=existing_invoices,
            purchase_order=purchase_order
        )
    except Exception:
        # Catch unforeseen failures in the engine mapping
        # Return a distinct status for system errors so it isn't treated as a completed validation.
        # Do not expose the raw exception details.
        return {
            "workflow_status": "M3_EXECUTION_FAILED",
            "structural_issues": [],
            "m3_validation_result": None,
            "m3_execution_error": "An unexpected system error occurred during accounting validation.",
            "m2_provenance": adapter_result["m2_provenance"],
            "original_m2_result": public_result
        }

    # The isolated adapter must not claim a complete validation when it has no
    # tenant supplier catalogue context. The production API supplies that
    # context separately and may apply its configured validation rules.
    if not known_vendors:
        m3_result = {**m3_result,
                     "status": "REVIEW_REQUIRED",
                     "errors": [*m3_result.get("errors", []), {
                         "code": "EX-008",
                         "severity": "medium",
                         "field": "vendor_name",
                         "message": "Supplier verification requires tenant vendor context.",
                     }]}
    
    return {
        "workflow_status": "M3_VALIDATION_COMPLETE",
        "structural_issues": [],
        "m3_validation_result": m3_result,
        "m2_provenance": adapter_result["m2_provenance"],
        "original_m2_result": public_result
    }
