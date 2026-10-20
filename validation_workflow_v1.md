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
 Core Validation       Additional Checks
       |                      |
       |                +-----+-----+------+
       |                |     |     |      |
       v                v     v     v      v
 Schema/Type       Duplicate Vendor  PO   Exception
 Line Arithmetic   Detection Matching Matching Handling
 Subtotal/Total
 Date
       |
       +----------------------+
                    |
                    v
           VALID / REVIEW_REQUIRED
              /             \
             /               \
            v                 v
     Downstream           Human Review /
     Workflow             Correction

```
---

## 3. Validation Stage

### Stage 1 — Schema and Type Validation

The Validation Engine checks that the extracted invoice data contains the required fields and that each field has the expected type and format.

Examples:
- invoice_number must be a non-empty string.
- vendor_name must be a non-empty string.
- invoice_date must be a valid ISO date (YYYY-MM-DD).
- line_items must contain at least one item.
- Monetary fields are converted to Decimal.
- currency must contain exactly 3 letters.

### Stage 2 — Line Item Validation

Each line item is validated independently.

Rules:
- description must be non-empty.
- quantity must be greater than 0.
- unit_price must be greater than 0.
- amount must equal quantity × unit_price within the ±0.01 tolerance.

Example:
quantity × unit_price = expected amount
10 × 12.50 = 125.00

If the calculated amount does not match the extracted amount, the invoice is routed to REVIEW_REQUIRED.

### Stage 3 — Invoice-Level Arithmetic Validation

The Validation Engine checks that invoice-level amounts are internally consistent.

Rules:
- The subtotal must match the sum of line-item amounts.
- The total amount must equal subtotal + tax_amount.
- Monetary comparisons use a ±0.01 tolerance.

### Stage 4 — Date Validation

The Validation Engine checks invoice date relationships.

Rule:
- due_date cannot be earlier than invoice_date.

### Stage 5 — Duplicate Invoice Detection

The Validation Engine checks whether the invoice appears to be a duplicate of an existing invoice.

Duplicate detection is based on invoice information such as:
- Vendor name
- Invoice number

A detected duplicate is routed for review rather than treated as a trusted record.

### Stage 6 — Vendor Matching

The Validation Engine compares the extracted vendor against known vendor information.

Vendor matching supports:
- Exact matching
- Fuzzy matching
- Review when the match is not confidently accepted

A vendor match that requires review is recorded as an exception.

### Stage 7 — Purchase Order Matching

The Validation Engine can compare an invoice against a supplied purchase order.

The PO matching component checks:
- Supplier/vendor
- Line-item descriptions
- Quantities
- Unit prices
- Total amount

Possible outcomes are:
- MATCHED
- REVIEW_REQUIRED

PO matching is currently implemented as a standalone validation component and does not depend on PO database tables.

---

## 4. Validation Result

The workflow produces one of two high-level outcomes:

VALID
The invoice has passed the required validation and matching checks.
The invoice may continue to downstream processing.

REVIEW_REQUIRED
One or more validation or matching checks require attention.

The invoice should not continue as a trusted accounting record until the issue has been handled.

The specific exception should be recorded with its exception code, severity, message, and relevant validation details.

---

## 5. Current Implementation Scope

The current validation implementation covers:
- Accounting arithmetic validation
- Basic type and format validation
- Invoice and line-item validation
- Date validation
- Duplicate invoice detection
- Vendor matching
- Purchase order matching
- Exception severity mapping
- Validation and PO matching tests

The current PO matching implementation is standalone because PO database integration is not yet available.

Future workflow stages include:
- Bank matching
- Money-In / Money-Out reconciliation
- Reconciliation variance detection
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