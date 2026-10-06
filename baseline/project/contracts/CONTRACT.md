# Proposed Module 2 contract — draft 0.1

This is a discussion contract and mock fixture format, NOT an accepted final backend schema.

- schema_version, document_id, run_id: identity/version tracking.
- mode: mock or real. This starter only produces mock.
- status: extracted, needs_review, unsupported, failed. Extracted means extraction finished, never accounting approval.
- fields: each has raw_text (string/null), value (string/null), evidence (list), review_required (boolean). Monetary values remain decimal strings; M4 owns agreed canonical normalisation.
- evidence entry: page (1-based integer), text, bbox (null until available). A non-null bbox must declare coordinates, page dimensions and origin convention before integration. No fabricated locations.
- line_items: list of row objects using the same field wrapper. Empty must be explained in issues; it does not prove there are no rows.
- issues: list of code/detail objects; optional missing fields are not automatically failures.
- provenance: path, model identifier or null, software/configuration details and timings when measured.

Initial header names proposed: supplier_name, invoice_number, invoice_date, currency, subtotal, tax_amount, total_amount. Initial row names: description, quantity, unit_price, line_total. Confirm requiredness, date/currency rules and supported invoice types with the team.

Mock evidence is hand-authored from invented samples. It is not proof of parser or model grounding. All mock results require review. Accountant corrections, approval and revision history belong to the integrated workflow.
