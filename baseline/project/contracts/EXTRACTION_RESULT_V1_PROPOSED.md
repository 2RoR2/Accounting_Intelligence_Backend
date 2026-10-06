# ExtractionResult v1 — proposed, not team-approved

This additive checkpoint adapts the real draft-0.2 mapper output. It does not
replace the mapper or CONTRACT.md. The old contract's claim that the starter
only produces mocks is historical: real synthetic parser/mapping runs now exist.

## Identity and scope

- `document_id`: explicit backend ID, or an explicitly supplied synthetic test ID.
- `legacy_document_id`: original filename-based ID, retained without reinterpretation.
- `extraction_run_id`: unique ID for this adaptation; original mapper run ID retained.
- `document_type`: `unknown`, `not_run`. No classifier has been executed.
- `field_schema`: invoice headers and simple four-column rows only. This is the
  mapper's scope, not a classification decision. Bill/receipt schemas remain future work.
- `schema_version`: `1.0.0-proposed`. Do not publish as the agreed integration contract.

## Field states

- `resolved`: mapper selected a non-empty string, still requiring human review.
- `missing`: no selected value; does not prove the original document lacks the field.
- `ambiguous`: interpretation is unresolved; candidates are retained.
- `conflict`: reserved for explicitly established incompatible candidates;
  no selected value, at least two distinct candidate values required structurally.
- `unsupported`, `unreadable`: reserved states; this adapter does not infer them.

The adapter maps `mapped` to `resolved`, and preserves `missing` / `ambiguous`.
It does NOT turn legacy `CONFLICTING_VALUES` into an established cross-method
conflict. That issue remains intact for later reconciliation logic. Amount-format
and currency concerns remain ambiguous. Formatting differences such as `MYR 50.00`
versus `50.00` do not themselves become conflicts.

All fields carry `legacy_mapping_status`, raw text, selected value, candidates,
complete evidence dictionaries and method `parser_mapping`. Header fields and
line-item fields use the same wrapper. Monetary values stay strings or null.

## Preservation and limits

All existing issues and provenance are copied. The adapter adds its version,
source result path and SHA-256. Both original and top-left bounding boxes,
page dimensions, source references and null evidence are preserved exactly.
Evidence geometry is NOT validated or visually verified by this checkpoint.
Unknown legacy fields/statuses cause failure rather than silent information loss.
Empty rows must retain a NO_MAPPED_LINE_ITEMS explanation.

Every output requires review. A resolved field is not an approved accounting value.
No fabricated confidence, classification, OCR, AI inference, selective routing,
accounting validation or backend integration is added here.

## Use from the SSD project root

```
python -m unittest discover -s project/tests -p test_extraction_v1.py -v
python project/scripts/extraction_v1.py runs/mapped-20261006T040403Z-a1b5f47e/result.json --document-id DOC-SYN-001
```

The command writes a new `runs/extraction-.../result.json` and `schema.json`.
The original mapped result is never rewritten. DOC-SYN-001 is a lab test ID,
not evidence that backend ID integration is complete.

Next: verify preserved data on the lab result, agree the team interface, then add
classification rules and document-specific schemas with meaningful tests.


