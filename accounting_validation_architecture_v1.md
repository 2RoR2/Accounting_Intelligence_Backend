# Accounting Validation Architecture v1

## 1. Purpose

This document defines the architecture for the Accounting Validation component of the Accounting Intelligence Backend.

The validation layer ensures that structured invoice data extracted from documents is mathematically and structurally valid before it is trusted by downstream accounting processes.

The architecture follows a clear boundary:

- AI/extraction components interpret information from the invoice.
- Deterministic validation rules enforce accounting controls.
- Invalid data is rejected or routed for exception handling.
- Validated data can proceed to downstream accounting workflows.

---

## 2. Architecture Overview

The Accounting Validation flow is:

```text
Invoice Document
       |
       v
Module 2 - Extraction Engine
       |
       v
Structured Invoice Data
       |
       v
Module 3 - Validation Engine
       |
       +-----------------------------+
       |                             |
       v                             v
Core Invoice Validation       Additional Validation
       |                             |
       |                    +--------+--------+--------+
       |                    |        |        |        |
       v                    v        v        v        v
Schema / Type          Duplicate  Vendor     PO     Exception
Validation             Detection  Matching  Matching  Handling
       |
       +-----------------------------+
                     |
                     v
              VALID / REVIEW_REQUIRED
                 /             \
                /               \
               v                 v
        Downstream          Human Review /
        Workflow            Correction
```

The validation engine acts as a deterministic control boundary between
AI-extracted invoice data and downstream accounting processes.

Validation does not replace AI extraction. Instead, it checks whether
the extracted information is structurally, mathematically, and
operationally acceptable before the data proceeds downstream.

---

## 3. Component Responsibilities

### 3.1 Module 2 - Extraction Engine

The Extraction Engine is responsible for interpreting the invoice document and producing structured invoice data.

Its responsibilities include:

- Extracting invoice fields.
- Extracting vendor information.
- Extracting invoice dates.
- Extracting line items.
- Extracting monetary values.
- Producing structured data that follows the agreed invoice schema.

The extraction stage may use AI/OCR because invoice documents can contain unstructured or ambiguous information.

The Extraction Engine does not determine whether the extracted accounting values are mathematically valid.

---

### 3.2 Module 3 - Validation Engine

The Validation Engine is responsible for deterministic validation and
matching checks on structured invoice data.

Its responsibilities include:

- Checking required fields.
- Checking field types and formats.
- Validating line item arithmetic.
- Validating invoice subtotal arithmetic.
- Validating invoice total arithmetic.
- Validating date relationships.
- Detecting duplicate invoices.
- Matching extracted vendors against known vendors.
- Matching invoices against supplied purchase-order data.
- Assigning exception severity.
- Returning a consolidated validation result.

The validation rules should produce predictable and explainable
results for the same input.
---

### 3.3 Exception Handling

When a validation or matching check cannot be safely accepted, the
engine creates a review-required exception.

Current exception categories include:

- EX-001 — Missing Required Field.
- EX-002 — Invalid Field Type or Format.
- EX-003 — Line Item Arithmetic Mismatch.
- EX-004 — Subtotal Arithmetic Mismatch.
- EX-005 — Total Arithmetic Mismatch.
- EX-006 — Invalid Date Relationship.
- EX-007 — Duplicate Invoice.
- EX-008 — Vendor Matching Requires Review.
- EX-009 — Purchase Order Matching Requires Review.

Exceptions are assigned a severity level and returned as part of the
consolidated validation result.

The exception taxonomy is defined separately in
`exception_taxonomy_v1.md`.

---

### 3.4 Downstream Accounting Record

Only data that passes the required validation checks should be considered trusted for downstream accounting processing.

Invalid data should not be silently accepted as a valid accounting record.

---

## 4. Validation Layers

The validation architecture is divided into several deterministic layers.

### Layer 1 - Schema and Type Validation

This layer checks whether the extracted invoice data has the expected structure and basic data types.

Examples:

- `invoice_number` must be a non-empty string.
- `vendor_name` must be a non-empty string.
- `invoice_date` must be a valid date.
- `line_items` must contain at least one item.
- Monetary values must be valid decimal values.
- `currency` must contain exactly three alphabetic characters.

If the basic structure or type is invalid, the invoice cannot proceed to accounting validation.

---

### Layer 2 - Line Item Validation

Each line item is validated independently.

The main accounting rule is:

```text
quantity × unit_price = amount
```

Example:

```text
10 × 12.50 = 125.00
```

A tolerance of ±0.01 is applied to monetary arithmetic validation.

If the calculated amount does not match the extracted amount within the accepted tolerance, the line item fails validation.

---

### Layer 3 - Invoice-Level Arithmetic Validation

After individual line items pass validation, invoice-level arithmetic is checked.

#### Subtotal Validation

```text
sum(line item amounts) = subtotal
```

The difference must be within the accepted ±0.01 tolerance.

#### Total Validation

```text
subtotal + tax_amount = total_amount
```

The difference must also be within the accepted ±0.01 tolerance.

---

### Layer 4 - Date Validation

Invoice date relationships are checked to prevent logically invalid accounting data.

For example:

```text
due_date >= invoice_date
```

A due date earlier than the invoice date is treated as a validation failure.

---

### Layer 5 - Duplicate Detection

The validation engine checks whether the invoice appears to duplicate
an existing invoice.

Duplicate detection considers invoice information such as the vendor
and invoice number and returns a review exception when a duplicate is
identified.

A detected duplicate is classified as:

EX-007 — Duplicate Invoice

---

### Layer 6 - Vendor Matching

The extracted vendor name is compared against the known vendor list.

Exact matches can be accepted automatically. Fuzzy or unresolved
matches require review rather than being silently accepted.

A vendor matching issue is classified as:

EX-008 — Vendor Matching Requires Review

---

### Layer 7 - Purchase Order Matching

When purchase-order data is available, the invoice can be compared
against the purchase order.

The current PO matching component checks:

- Supplier/vendor match.
- Line-item description match.
- Line-item quantity variance.
- Unit-price variance.
- PO amount variance.

A successful comparison returns `MATCHED`.

A mismatch returns `REVIEW_REQUIRED` and is classified as:

EX-009 — Purchase Order Matching Requires Review.

The current implementation is a standalone matching component. It does
not create or depend on purchase-order database tables yet.

---

## 5. AI vs Deterministic Boundary

The architecture separates interpretation from accounting control.

### AI / Extraction Responsibilities

AI or extraction technology may be used for:

- Reading invoice documents.
- Identifying fields.
- Interpreting document layouts.
- Extracting vendor and invoice information.
- Extracting line items and monetary values.

### Deterministic Validation Responsibilities

Deterministic rules are used for:

- Required field validation.
- Type and format validation.
- Arithmetic validation.
- Date relationship validation.
- Validation result classification.

The principle is:

```text
AI interprets the document.
Deterministic rules enforce accounting controls.
```

This separation reduces the risk of relying on probabilistic AI output for financial calculations and accounting controls.

---

## 6. Validation Result Flow

The validation engine produces a consolidated result.

### VALID

The invoice passes the applicable validation and matching checks.

The data can proceed to the next accounting workflow stage.

### REVIEW_REQUIRED

One or more validation or matching checks require human review.

Examples include:

- Duplicate invoice detected.
- Vendor cannot be safely resolved.
- Purchase order mismatch.
- Arithmetic or date validation failure.

The relevant exception code, severity, field, and message are returned
so that the issue can be reviewed and corrected.

---

## 7. Current Sprint 1 Scope

The current validation implementation covers:

- Accounting validation architecture.
- Schema and basic type validation.
- Line item arithmetic validation.
- Subtotal validation.
- Total validation.
- Date relationship validation.
- Duplicate invoice detection.
- Vendor matching.
- Purchase order matching.
- Exception severity classification.
- Validation and PO matching test coverage.

Bank transaction matching and full bank reconciliation are not yet
implemented because the required transaction data layer is not yet
available.

Purchase-order database integration and goods-receipt database
integration are also outside the current implementation because the
required database tables are not yet available.

---

## 8. Design Principle

The Accounting Validation component is designed around the following principle:

> AI is responsible for extracting and interpreting information, while deterministic validation rules are responsible for enforcing accounting correctness.

This creates a clear and auditable boundary between AI-based document processing and accounting control logic.