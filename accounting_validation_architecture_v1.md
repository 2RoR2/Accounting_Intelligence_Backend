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
Schema / Type Validation       Accounting Validation
       |                             |
       |                       +-----+-----+---------+
       |                       |           |         |
       |                       v           v         v
       |                  Line Item    Subtotal    Total
       |                  Arithmetic   Validation  Validation
       |                       |
       |                       v
       |                  Date Validation
       |                             |
       +-------------+---------------+
                     |
              VALID / INVALID
                 /         \
                /           \
               v             v
        Downstream        Exception
        Accounting        Handling
        Workflow
```

The validation engine acts as a control boundary between extracted invoice data and downstream accounting processes.

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

The Validation Engine is responsible for deterministic validation of the structured invoice data.

Its responsibilities include:

- Checking required fields.
- Checking field types and formats.
- Validating line item arithmetic.
- Validating invoice subtotal arithmetic.
- Validating invoice total arithmetic.
- Validating date relationships.
- Returning a validation result.

The validation rules should produce predictable results for the same input.

---

### 3.3 Exception Handling

When validation fails, the failure should be recorded as an accounting validation exception.

Examples include:

- Missing required field.
- Invalid field type or format.
- Line item arithmetic mismatch.
- Subtotal arithmetic mismatch.
- Total arithmetic mismatch.
- Invalid date relationship.

The exception taxonomy is defined separately in `exception_taxonomy_v1.md`.

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

The validation engine produces two main outcomes:

### VALID

The extracted invoice data satisfies the required validation rules.

The data can proceed to the next accounting workflow stage.

### INVALID

One or more validation rules fail.

The invoice should not be trusted as a valid accounting record and the corresponding validation failure should be recorded for exception handling.

---

## 7. Current Sprint 1 Scope

The Sprint 1 architecture covers:

- Accounting validation architecture.
- Schema and basic type validation.
- Line item arithmetic validation.
- Subtotal validation.
- Total validation.
- Date relationship validation.
- Validation test fixtures.
- Validation workflow definition.
- Exception taxonomy definition.

Advanced accounting matching and reconciliation are outside the current Sprint 1 validation architecture and can be developed in later stages.

---

## 8. Design Principle

The Accounting Validation component is designed around the following principle:

> AI is responsible for extracting and interpreting information, while deterministic validation rules are responsible for enforcing accounting correctness.

This creates a clear and auditable boundary between AI-based document processing and accounting control logic.