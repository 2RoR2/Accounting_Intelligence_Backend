# Exception Taxonomy v1

## 1. Purpose

This document defines the first version of the exception categories for the Accounting Intelligence validation workflow.

The purpose is to classify validation failures consistently so that errors can be identified, reviewed, and handled by the appropriate workflow.

---

## 2. Exception Categories

### EX-001 — Missing Required Field

A required invoice field is missing from the extracted data.

Examples:

- `invoice_number` missing
- `vendor_name` missing
- `invoice_date` missing
- `line_items` missing

---

### EX-002 — Invalid Field Type or Format

A field exists but does not follow the expected type or format.

Examples:

- Invalid date format
- Monetary amount cannot be parsed
- Currency is not exactly 3 letters
- Quantity is not a valid positive number

---

### EX-003 — Line Item Arithmetic Mismatch

A line item's extracted amount does not match:

```text
quantity × unit_price
```

within the allowed ±0.01 tolerance.

Example:

quantity = 10
unit_price = 12.50
expected amount = 125.00
extracted amount = 999.00

---

### EX-004 — Subtotal Arithmetic Mismatch

The invoice subtotal does not match the sum of its line item amounts.

Rule:

sum(line_items.amount) = subtotal

A tolerance of ±0.01 is allowed.

---

### EX-005 — Total Arithmetic Mismatch

The invoice total does not match:

subtotal + tax_amount

within the allowed ±0.01 tolerance.

---

### EX-006 — Invalid Date Relationship

A date relationship is logically invalid.

Current rule:

due_date >= invoice_date

If `due_date` is earlier than `invoice_date`, the invoice fails validation.

---

## 3. Exception Handling Levels

### Level 1 — Validation Failure

The extracted invoice fails one or more deterministic validation rules.

The invoice should not be treated as a fully trusted accounting record until the issue is resolved.

### Level 2 — Review Required

The invoice requires human review when the extracted information cannot be safely validated or when the issue requires additional context.

### Level 3 — Accepted

The invoice has passed the required validation rules and can continue to downstream processing.

---

## 4. AI vs Deterministic Boundary

Deterministic accounting rules should be used for:

- Arithmetic calculations
- Required-field checks
- Data type and format checks
- Date relationship checks
- Validation of extracted monetary values

AI/extraction may be used for:

- Reading invoice information
- Interpreting document content
- Extracting fields from unstructured documents

The AI layer should not replace deterministic accounting controls.

---

## 5. Current Sprint 1 Scope

The following exception categories are implemented or represented in the Sprint 1 validation fixtures:

- Missing required fields
- Invalid field types/formats
- Line item arithmetic mismatch
- Subtotal arithmetic mismatch
- Total arithmetic mismatch
- Invalid due date relationship

Advanced exception categories for future iterations may include:

- Duplicate invoice detection
- Vendor validation failure
- PO/GRN matching failure
- Bank matching failure
- Reconciliation variance
- Low OCR/extraction confidence

These are not part of the current Sprint 1 deterministic validation implementation.