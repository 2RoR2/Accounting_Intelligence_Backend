# Accounting Validation Workflow v1

## 1. Purpose

This document defines the first version of the accounting validation workflow for the Accounting Intelligence system.

The workflow ensures that invoice data extracted by Module 2 is validated before it is allowed to continue to downstream accounting and database processes.

The main principle is:

> AI/extraction interprets the document; deterministic validation rules enforce accounting controls.

---

## 2. Workflow

```text
Invoice Document
       |
       v
Module 2: Extraction Engine
       |
       v
Structured Invoice Data
       |
       v
Module 3: Validation Engine
       |
       +----------------------+
       |                      |
       v                      v
   VALID                  INVALID
       |                      |
       v                      v
Continue to              Create validation
downstream workflow       exception
       |
       v
Database / Accounting
Record

---

## 3. Validation Stages

### Stage 1 — Schema and Type Validation

The Validation Engine checks that the extracted invoice data contains the required fields and that each field has the expected type and format.

Examples:

- `invoice_number` must be a non-empty string.
- `vendor_name` must be a non-empty string.
- `invoice_date` must be a valid ISO date (`YYYY-MM-DD`).
- `line_items` must contain at least one item.
- Monetary fields are converted to `Decimal`.
- `currency` must contain exactly 3 letters.

---

### Stage 2 — Line Item Validation

Each line item is validated independently.

Rules:

- `description` must be non-empty.
- `quantity` must be greater than 0.
- `unit_price` must be greater than 0.
- `amount` must equal `quantity × unit_price` within the ±0.01 tolerance.

Example:

```text
quantity × unit_price = expected amount
10 × 12.50 = 125.00
```

If the calculated amount does not match the extracted amount, the invoice is rejected by the validation layer.


---

## 4. Validation Result

The workflow produces one of two high-level outcomes:

### VALID

The invoice has passed the required validation rules.

The invoice may continue to downstream processing.

### INVALID

One or more deterministic validation rules have failed.

The invoice should not continue as a trusted accounting record until the validation issue has been handled.

The specific validation failure should be recorded so that it can be reviewed and traced back to the relevant rule.

---

## 5. Current Sprint 1 Scope

Sprint 1 focuses on the foundation of the validation layer:

- Accounting arithmetic validation
- Basic type and format validation
- Invoice and line-item validation
- Validation test fixtures
- Initial workflow definition
- Initial exception taxonomy

Advanced matching and reconciliation are outside the current Sprint 1 implementation scope.

Future workflow stages may include:

- Duplicate invoice detection
- Vendor validation
- PO/GRN matching
- Bank matching
- Reconciliation
- Variance detection
- AI confidence-based exception routing

---

## 6. Design Principle

The Validation Engine should use deterministic rules for accounting controls.

AI may be used upstream to extract or interpret information from invoices, but accounting calculations and control checks should be deterministic and reproducible.

This separation improves:

- Auditability
- Consistency
- Explainability
- Testability
- Error tracing