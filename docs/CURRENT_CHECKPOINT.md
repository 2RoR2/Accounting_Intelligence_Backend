# CURRENT CHECKPOINT — COS40005 V2

Date: 2026-10-19
Repository: Backend_V2 / feature/accounting-v2 / base HEAD 92575bc, ahead 1; initially clean. M3A changes uncommitted.
Active: M3A PASS for live supported invoice EX-001–EX-008 integration. Bill/receipt policies and live PO matching remain NOT_CHECKED.
STOP after M3A. No frontend edits, schema/bootstrap/destructive SQL, installs, downloads, Qwen, approvals, merges, pushes or deployment.

## Preserved runtime and data
- Existing WSL Python 3.12.14 /home/donut99/.venvs/accounting-intelligence and read-only SSD Docling assets reused.
- PostgreSQL 18.0: 127.0.0.1:55434, accounting_intelligence_v2_demo, cluster saic_backend_v2_d1a, fingerprint 7698223516621660199; configure() verified.
- Dedicated authenticated Redis 127.0.0.1:6382 and sequential cpu_tasks worker retained; native cluster/unrelated containers untouched.
- Only verified idle V2 API/worker were gracefully reloaded. Final API http://127.0.0.1:8002 and frontend proxy http://127.0.0.1:3000/api/health returned 200 from Windows.
- Existing additive startup review-revision check remains on its already-present column; no new schema change/migration applied.
- All 6 prior D2.1 + 6 D2.2 documents' stored documents/raw extraction/fields/lines/status/revisions/exceptions hash-verified unchanged.
- Fresh M3A synthetic tenants and vendor fixtures only; every current extraction is an actual uploaded PDF/CPU Docling result. One explicitly synthetic non-current reference row tests obsolete-run exclusion.

## Implemented M3A contracts
- validation/document_validation.py now uses Aina's validation.engine with effective corrected predictions, actual current NUMERIC lines, classified kind, tenant/document identity and real tenant references.
- New validation/references.py applies tenant predicates, current-run filtering, current-document exclusion, verified M2 invoice classification and corrected effective reference values.
- Duplicate helper no longer converts null to None text; engine compares target only, not unrelated historical duplicate pairs. Incomplete key/context means NOT_CHECKED.
- EX-001/002/006/008 retain medium severity, WARNING, EXCEPTION/REVIEW_REQUIRED, blocking=false; EX-003/004/005/007 are high/FAIL/blocking=true. Unknown classification's existing high EX-002 guard remains.
- Format ambiguity is EX-002 rather than fabricated/missing defaults. Human corrections take precedence without rewriting raw/normalized/evidence/state/history; financial math stays Decimal with existing tolerance.
- Tenant vendor catalogue absent/unresolved supplier -> EX-008 NOT_CHECKED. Fuzzy/no-match -> review; actual name similarity is exposed separately, never written as extraction confidence or an entity approval. AR customer resolution remains separate.
- Standalone EX-009 preserved; live PO references unavailable -> NOT_CHECKED. Bills/receipts retain unsupported type checks NOT_CHECKED; unknowns map no financial fields.
- Invoice PASS covers core + available references, not unavailable vendor/PO checks. coverageComplete keeps core-policy meaning; reviewRequired=true/accountingApprovalPerformed=false throughout.
- Revalidation resolves superseded VALIDATION:* rows as history and inserts current findings with real severity transactionally. Standardisation rechecks references and existing human/revision gates.
- GET validation adds code/severity/checkStatus, optional duplicate_of/vendor_candidate, and vendorMatch. D2.2 fields/statuses/correction DTOs remain compatible; see docs/M3A_FRONTEND_HANDOFF.md.

## Actual executed verification
- P = wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B
- P scripts/d1a.py check -> exit 0. Final startup remains P scripts/d1a.py api / P scripts/d1a.py worker (separate terminals; reuse existing listeners).
- P -m unittest tests.test_m3_validation tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation tests.extraction.test_multidocument -q -> exit 0, 76 passed, 0 failed/errors/skipped, 0.593s.
- P runs/c1-pg-offline-extraction.py -> exit 0, 142 passed, 0 failed/errors/skipped, 0.180s; sockets blocked, overlaps 12 profile tests; existing fixture-blocked test_execution.py excluded.
- P runs/m3a-po-unit.py -> exit 0, 4 original standalone PO functions passed via unittest.FunctionTestCase, no database matching.
- P -m pytest tests/test_validation.py tests/test_po_matching.py -q -> BLOCKED, exit 1: pytest absent. No install; original pytest validation suite not executed, independent rule tests ran via unittest.
- P runs/m3a-live-tests.py -> exit 0, 12 real HTTP/TOTP/RQ/Docling/PostgreSQL tests passed, 19 fresh PDFs, 0 failed/errors/skipped, 88.506s.
- Initial live fixture/probe failures corrected (exact-file 409, unique tenant name, WSL→Windows localhost); final suite passed. No parser/auth mocks, injected sessions or fake approvals.
- git diff --check -> exit 0. API and Windows frontend-proxy health 200. M3A is API verified, not a new browser acceptance run.

## Real acceptance evidence
- Live 50.00 SYN-001: 3e3ddf73-327c-46cf-bcfd-99bdfd8b7652, EXTRACTED/PASS; vendor/PO NOT_CHECKED. Invalid 99.00 b83c2811-cd3d-409d-bf84-18b56a67f999 produced EX-005, corrected 50.00/revision 3; original 99.00 + audit retained, stale 409.
- Real EX-001–EX-006 PDFs each produced only its intended core code, correct severity/blocking state and persisted exception; saved field/line corrections then revalidation resolved history and returned PASS, still human-review required.
- Genuine same-tenant duplicate 10b68d4b-474e-415f-b5bb-27eb6e2c182d referenced fa988d37-b865-481b-8541-b663ca330d91, EX-007 high/blocking. Correcting number removed/resolved the finding.
- Identical foreign-tenant keys, obsolete extraction reference and corrected reference's stale raw number did not falsely flag current unique invoices; cross-tenant GET/revalidate 404.
- Separate synthetic catalogue: fuzzy vendor 9874614d-74da-4a1e-a401-d93ecb42f7a5 similarity 0.913 and no-match 22d4bfbc-81e3-48cf-b6fc-97968dd05b77 produced EX-008 medium/review. Correcting supplier returned exact-name/PASS; extraction confidence stayed NULL.
- Invoice B 48.00 PASS, bill 80.00 REVIEW_REQUIRED/NOT_CHECKED, receipt 10.60 REVIEW_REQUIRED/NOT_CHECKED and unknown EXCEPTION/no fields reconfirmed. No approved records created.
- Sanitized API/PG evidence: runs/m3a/latest-live-evidence.json. Private fresh accounts: runs/m3a/3afa9d94da9342c6ba0853367179114c/accounts.json and vendor-account.json; credentials never printed.

## Remaining limits and next review
- Repeated old demo business invoice numbers now correctly trigger EX-007. Use the fresh M3A tenant/verified unique records for clean PASS; never suppress genuine duplicates for a demo.
- Bill/receipt policies, authorized live PO data/matching, concurrent duplicate races and full C1-PG owner/assignee/immutable-record verification remain unavailable/unverified. Existing immutable-trigger limitation remains.
- Next small Angeline task: optional M3 candidate/reference metadata and unique validation row keys; current D2.2 workflow/HTTP contract remains usable. Aina/Rachel review rule/severity/reference semantics. Do not proceed automatically.
