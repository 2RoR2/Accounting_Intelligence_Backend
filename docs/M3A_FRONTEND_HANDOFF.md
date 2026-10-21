# M3A — live invoice validation handoff

Verified 2026-10-19, Backend_V2 `feature/accounting-v2`, base `92575bc`. No frontend, schema, extraction mapper or model changes. Review owners: Aina (rules/severity), Rachel (API/persistence), Angeline (optional UI metadata); Aleeya's schema is unchanged.

## Live behaviour and compatibility

Existing authenticated upload, extraction, field correction, revalidate, original-source and record APIs remain. M3 now runs Aina's `validate_invoice_document` from the persisted-document adapter, inside the existing extraction persistence/revalidation transaction. Standardisation rechecks current references and retains human approval/revision gates.
Values come from the current extraction and effective predictions: human current values override parser values; candidates/raw text never become facts by fallback. Current NUMERIC line values are used, including saved line corrections. Decimal math and the existing 0.01 tolerance remain.

| Code | Supported invoice check | Severity / result | Observed state before correction |
| --- | --- | --- | --- |
| EX-001 | Missing required invoice field | medium / WARNING | EXCEPTION, REVIEW_REQUIRED, blocking=false |
| EX-002 | Invalid format/type or unresolved format candidates | medium / WARNING | EXCEPTION, REVIEW_REQUIRED, blocking=false |
| EX-003 | Quantity × price versus line amount | high / FAIL | EXCEPTION, REVIEW_REQUIRED, blocking=true |
| EX-004 | Sum of line amounts versus subtotal | high / FAIL | EXCEPTION, REVIEW_REQUIRED, blocking=true |
| EX-005 | Subtotal + tax − discount versus total | high / FAIL | EXCEPTION, REVIEW_REQUIRED, blocking=true |
| EX-006 | Due date earlier than invoice date | medium / WARNING | EXCEPTION, REVIEW_REQUIRED, blocking=false |
| EX-007 | Same-tenant business duplicate | high / FAIL | EXCEPTION, REVIEW_REQUIRED, blocking=true |
| EX-008 | Fuzzy/no vendor name match against an available tenant catalogue | medium / WARNING | EXCEPTION, REVIEW_REQUIRED, blocking=false |

Severity follows Aina's map, rather than storing every finding as high. Medium findings still require review and block standardisation through invoice validation; `blocking=false` does not mean approved or acceptable for record creation. Unknown/source-classification guards retain their existing high-severity EX-002 and blocked behaviour.
Bills/receipts retain REVIEW_REQUIRED, coverageComplete=false and their type-specific NOT_CHECKED rows. No invoice-only required-field rules run for them. Unknowns still produce no invented fields.

## Reference rules

- Duplicate key: trimmed/casefolded supplier name plus trimmed, case-preserved invoice number, following the existing deterministic rule. Neither null nor an unsupported type becomes the text `None`.
- SQL restricts candidates by target tenant, another document ID, `kind=invoice`, usable processing status and `raw_extractions.is_current=true`; M2 source classification must also be classified invoice. Reference predictions use effective current values, including reviewer corrections.
- The engine compares each reference to the target only; unrelated historical duplicate pairs do not flag a unique target. Missing key/context means NOT_CHECKED. A successful reference scan can return an EX-007 PASS row.
- Vendors are read only from `vendors WHERE tenant_id = authenticated_document_tenant`. No catalogue/unresolved supplier means EX-008 NOT_CHECKED, not an invented mismatch. AR customer resolution remains a separate policy; supplier-catalogue matching is not applied to AR.
- Name matching uses Aina's normalized-name/SequenceMatcher rule and unchanged 0.80 threshold. A normalized exact-name result `ACCEPT` is not an approved vendor identity. No vendor-match decision, extracted confidence, approval or accounting record is written automatically.
- EX-009 stays available in the standalone engine with an explicit synthetic/provided PO. Live APIs supply no PO reference and return NOT_CHECKED. No database-backed PO matching was implemented or claimed.

Repeated synthetic invoice numbers in the original demo tenants now legitimately produce EX-007. Existing source/field/revision/exception rows were not rewritten. For a clean PASS demonstration use the fresh M3A tenant and verified saved records below, or a genuinely unique synthetic business invoice number; do not suppress the duplicate finding to manufacture PASS.

## Exact additive response fields

`GET /api/documents/{id}/validation` preserves the D2.2 envelope and result fields. Invoice `results[]` additionally exposes `code`, `severity`, `checkStatus`, optional `duplicate_of`, and optional `vendor_candidate` on findings. Codes/fields/messages remain the actual engine output. Medium findings render WARNING; high findings render FAIL.

Actual duplicate finding (initial response; later corrected):
```json
{"code":"EX-007","severity":"high","field":"invoice_number","message":"Duplicate invoice detected for Synthetic M3 Supplies Ltd / M3A-BUSINESS-DUP","duplicate_of":"fa988d37-b865-481b-8541-b663ca330d91","rule":"EX-007","result":"FAIL","checkStatus":"CHECKED"}
```
Its document `10b68d4b-474e-415f-b5bb-27eb6e2c182d` initially returned status EXCEPTION, validationStatus REVIEW_REQUIRED and blocking=true.

Actual fuzzy vendor finding (initial response; later corrected):
```json
{"code":"EX-008","severity":"medium","field":"vendor_name","message":"Vendor matching requires review: REVIEW_REQUIRED","vendor_candidate":{"vendor_id":"bbb0b3fe-5dd7-4152-b5ec-1b8fb6df2076","vendor_name":"Synthetic M3 Supplies Ltd","similarity":0.913,"score_method":"normalized_name_sequence_matcher"},"rule":"EX-008","result":"WARNING","checkStatus":"CHECKED"}
```
The report also adds `vendorMatch` with `{status,vendor_id,vendor_name,similarity,score_method}`. Status is ACCEPT, REVIEW_REQUIRED, NO_MATCH or NOT_CHECKED. IDs/names/scores are nullable. **Similarity is not model extraction confidence**; the live extraction field confidences remained NULL even after matching/correction.

Actual no-catalogue result:
```json
{"rule":"EX-008","result":"WARNING","checkStatus":"NOT_CHECKED","message":"No applicable tenant supplier catalogue or resolved supplier name; vendor matching was not checked."}
```
The top-level vendorMatch is `{"status":"NOT_CHECKED","vendor_id":null,"vendor_name":null,"similarity":null,"score_method":null}`.

An invoice can have `validationStatus=PASS`, `coverageComplete=true`, `reviewRequired=true`, `accountingApprovalPerformed=false`, while optional vendor/PO rows say NOT_CHECKED. Here coverageComplete retains its core-invoice-policy meaning; it does not claim that unavailable reference checks ran. The legacy `passed` value remains an aggregate flag, not an accuracy metric or a count of every reference rule.
`POST /revalidate` retains errors/checks/coverageComplete. It resolves superseded VALIDATION:* findings as history, inserts current findings with correct severity, and leaves the document EXTRACTED or EXCEPTION. It never sets VALIDATED/approved/paid.
GET evaluates current values/reference data without writing exceptions. After a correction or catalogue change, POST revalidate to persist the new findings and resolve previous ones. Stored document status may reflect the previous validation until that call.

## Live evidence versus offline evidence

Final `runs/m3a/latest-live-evidence.json` records 12 real HTTP/TOTP/RQ/CPU Docling/PostgreSQL integration tests, 19 fresh PDF uploads, and preserved-data hashes. No auth/parser mocks or approval fixtures. All EX-001–EX-008 scenarios were observed through real APIs, persisted, corrected and revalidated. Initial finding snapshots remain in that ignored evidence file; the scenario documents are now corrected.

| Scenario | Actual document ID | After test |
| --- | --- | --- |
| SYN-001, 50.00 | 3e3ddf73-327c-46cf-bcfd-99bdfd8b7652 | EXTRACTED/PASS; catalogue/PO NOT_CHECKED |
| SYN-BAD-001, original 99.00 | b83c2811-cd3d-409d-bf84-18b56a67f999 | corrected 50.00, revision 3, PASS; original 99.00 retained |
| EX-001 missing invoice number | 13a21041-855b-45d2-b12e-65adbeaad1fc | corrected, PASS, old medium exception resolved |
| EX-002 malformed currency | 6b639965-ef44-431e-8891-f70ddcd9bff3 | corrected RM→MYR, PASS; parser ambiguity/raw preserved |
| EX-003 line mismatch | 9cc4973b-9c3a-44f9-b938-4d5be32eb402 | line/header corrections, PASS |
| EX-004 subtotal mismatch | 47fd2e1a-f0ea-4299-a08c-c385c23ef8c9 | corrected, PASS |
| EX-005 total mismatch | fcd393fc-7986-4514-8873-0b498af72aff | corrected, PASS |
| EX-006 date order | 54f267ab-d893-41b5-9b85-ac562becead3 | corrected, PASS |
| EX-007 business duplicate | 10b68d4b-474e-415f-b5bb-27eb6e2c182d | corrected to unique number, PASS; high finding resolved |
| EX-008 fuzzy / no-match | 9874614d-74da-4a1e-a401-d93ecb42f7a5 / 22d4bfbc-81e3-48cf-b6fc-97968dd05b77 | corrected supplier, exact-name check, PASS; medium findings resolved |

The six-layout regressions also reconfirmed invoice B 48.00/PASS, bill 80.00/REVIEW_REQUIRED, receipt 10.60/REVIEW_REQUIRED and unknown/EXCEPTION. Cross-tenant reads/revalidation return 404. One explicit synthetic non-current reference extraction tested the SQL exclusion; that fixture is not represented as live Docling output. Every current extraction was real Docling. All 12 earlier D2.1/D2.2 documents' stored source/extraction/fields/lines/revisions/status/exceptions were hash-verified unchanged.
Private synthetic accounts: `runs/m3a/3afa9d94da9342c6ba0853367179114c/accounts.json` (first = main cases, second = foreign tenant); `vendor-account.json` in the same folder is the separate synthetic catalogue tenant. Use existing password/TOTP procedure; do not copy credentials into Git/logs. No existing demo accounts were modified.

## Commands and remaining frontend work

P = `wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B`

- `P -m unittest tests.test_m3_validation tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation tests.extraction.test_multidocument -q`: exit 0, **76 passed**, 0 failures/errors/skips.
- `P runs/c1-pg-offline-extraction.py`: exit 0, **142 passed**, 0 failures/errors/skips, sockets blocked; overlaps the same 12 profile tests and excludes the previously fixture-blocked test_execution.py.
- `P runs/m3a-po-unit.py`: exit 0, **4 existing PO test functions passed** via unittest.FunctionTestCase. Independent M3 unit coverage also verifies EX-009; no DB PO reference is used.
- `P runs/m3a-live-tests.py`: exit 0, **12 passed**, 88.506s, 0 failures/errors/skips. Ignored runner sets D1A_LIVE=1/M3A_LIVE=1 and loads tracked tests.test_m3a_live; it creates fresh synthetic tenants, never resets previous data. Equivalent WSL shell command from the repository: `D1A_LIVE=1 M3A_LIVE=1 /home/donut99/.venvs/accounting-intelligence/bin/python -B -m unittest tests.test_m3a_live -v`.
- `P -m pytest tests/test_validation.py tests/test_po_matching.py -q`: **BLOCKED**, exit 1, `No module named pytest`; no dependency installed. The original pytest validation suite was not executed. Independent unittest rule coverage is not reported as a pytest pass.
- Earlier live attempts failed on exact-file deduplication, a repeated synthetic tenant name and a WSL→Windows localhost probe. Test fixtures now use unique physical PDF comments/names, and frontend health is checked from Windows. Final full suite passed.
- Final API/worker are left running with `P scripts/d1a.py api` / `P scripts/d1a.py worker`. Isolated PG18 is 127.0.0.1:55434/accounting_intelligence_v2_demo (fingerprint 7698223516621660199); authenticated Redis remains 127.0.0.1:6382. No shared/native/unrelated resources changed. Existing additive startup compatibility check remains; no schema bootstrap/destructive SQL/new migration was run.

D2.2 can already show code/field/severity/message, WARNING/FAIL and NOT_CHECKED with the existing DTO. Windows frontend proxy and backend health were 200. **M3A was API-verified, not newly browser-verified.**
Next small Angeline task, if exposing richer M3 details: add optional `code`, `duplicate_of`, `vendor_candidate` and `vendorMatch` types; render name similarity separately from extraction confidence; use a unique rule-row key (rule + field + reference/index) because multiple findings can share an EX code. Clarify core PASS versus optional reference NOT_CHECKED; do not infer vendor approval/payment or hide duplicate findings. No frontend edit is needed merely to retain current corrections/revision handling.
Still not verified: concurrent duplicate insertion/correction races, full C1-PG owner/assignee/immutability suite, arbitrary document layouts, live PO matching and bill/receipt policies. Standalone PO retains its legacy response representation; no database PO precision/authorization claim is made. STOP after M3A.
