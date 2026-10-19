# CURRENT CHECKPOINT — COS40005 V2

Date: 2026-10-19
Repository: Backend_V2 / feature/accounting-v2 / base HEAD 0b5d9c0; initially clean, D2.1 changes uncommitted.
Active: D2.1 PASS for supported digital-PDF extraction-to-review; bill/receipt validation deliberately PARTIAL.
STOP after D2.1. No frontend changes, schema changes, installs, model downloads, Qwen, approvals, merges, pushes or deployment.

## Runtime preserved
- Existing WSL Python 3.12.14: /home/donut99/.venvs/accounting-intelligence/bin/python; CPU Docling and SSD assets reused.
- D: existed in Windows but /mnt/d was unmounted. Verified empty mountpoint, mounted drvfs D: read-only; no SSD asset writes.
- Restarted only saic-v2-d1a-postgres18 / saic-v2-d1a-redis, which were stopped; unrelated containers/native V2 cluster preserved.
- PostgreSQL 18.0: 127.0.0.1:55434, accounting_intelligence_v2_demo, d1a_v2_demo; cluster saic_backend_v2_d1a.
- Fingerprint 7698223516621660199 verified by scripts/d1a.py configure(); no schema bootstrap/new migration command. Existing API startup retains its ADD COLUMN IF NOT EXISTS review revision check on the already-present column.
- Redis authenticated dedicated 127.0.0.1:6382; sequential cpu_tasks worker. API http://127.0.0.1:8002, Windows health 200.
- Private credentials/accounts/TOTP remain ignored in runs/d1a; never printed. Both API and worker left running on final source.

## Implemented D2.1 contracts
- Source headings classify invoice/bill/receipt; conflicting headings or Bill / Invoice remain unknown/ambiguous, with source references.
- Content profile selected before mapping: invoice aliases plus customer/due/discount, bill issuer/reference/account/period/due/amount due, receipt merchant/date/time/reference/payment method.
- Handles Docling merged label blocks with exact character spans and honest source-block boxes; short table headers use explicit first-row labels even when header flags are partial.
- Unknown documents map no accounting fields/lines, produce EXCEPTION; selected intake kind remains only because DB has no unknown enum.
- Accounting boundary accounting-fields/1.1; existing field_predictions TEXT keys/JSONB suffice; NUMERIC and Decimal strings retained, absent values NULL, confidence NULL.
- Confident source kind updates stored kind; intake document_code stays stable. AP/AR category is never inferred or changed by extraction; legacy intake AP default remains.
- Bills/receipts: EXTRACTED / REVIEW_REQUIRED, coverageComplete=false, passed=0; required-field/math checks explicitly NOT_CHECKED, standardisation blocked.
- Invoice Decimal checks unchanged: 50.00 PASS; 99.00 EX-005. Review/approval/payment remain distinct; no automatic approvals.
- Raw/normalized/current/evidence/candidates/revision/reviewer preserved. New extraction over human field/line corrections is refused pending amendment; replay remains idempotent.
- Extraction DTO adds classification; raw adds field_schema + classification evidence; validation adds coverageComplete/checkStatus. See docs/D2_1_FRONTEND_HANDOFF.md.

## Actual commands and results
- P = wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B
- Connectivity: P scripts/d1a.py check -> exit 0. Separate terminals: P scripts/d1a.py api / P scripts/d1a.py worker.
- P -m tests.extraction.synthetic_documents -> exit 0; four clearly synthetic digital PDFs written under tests/extraction/data/multidocument/.
- P -m unittest tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation tests.extraction.test_multidocument -q -> exit 0, 64 passed, 0 failed/errors/skipped, 0.554s; includes original 38 C1A/C1B tests.
- P runs/c1-pg-offline-extraction.py -> exit 0, 142 passed, 0 failed/errors/skipped, 0.187s; sockets blocked; includes same 12 new profile tests. Existing fixture-blocked test_execution.py excluded.
- P runs/d2-1-live-tests.py -> exit 0, 7 real HTTP/TOTP/RQ/CPU Docling/PG tests passed, 0 failed/errors/skipped, 30.590s; no parser/auth mocks.
- Live suite inherits unchanged five D1A regressions plus four profile subcases and correction re-extraction rollback guard; no Redis/Ollama/GPU substitutes.
- Initial live attempts failed due unmounted models, merged label blocks and partial table headers; fixed and rerun. Failed synthetic uploads retained.
- git diff --check -> exit 0. Actual API responses/PG assertions: ignored runs/d1a/d2-1-live-evidence.json.

## Fresh real source processing evidence
- SYN-001 / 535dcfae-ba56-4b7c-a201-464f713b29e1: invoice, 50.00, EXTRACTED/PASS, real line 2 * 25.00 = 50.00.
- SYN-BAD-001 / cd396203-4163-44c9-a068-305a2c5017fc: invoice, original 99.00, EXCEPTION/EX-005; corrected 50.00 at revision 3, stale 409, one real audit row, revalidation PASS still review-required.
- invoice_b.pdf / 99e77638-e665-44f9-a14e-31e6efe4797c: invoice, 50.00 - 5.00 + 3.00 = 48.00, EXTRACTED/PASS; supplier/customer/date/due and fractional line 2.5 * 20.00 preserved.
- utility_bill.pdf / 2c0b6be6-a0a5-45d6-b67c-724cd991c6a2: bill, account/reference/period/due, 80.00, EXTRACTED/REVIEW_REQUIRED; charge 80.00, quantity/unit price/subtotal/tax NULL.
- retail_receipt.pdf / ba304cac-d289-46d4-b2be-4decfdaa2e35: receipt, merchant/reference/date/time/Card, subtotal 10.00/tax 0.60/total 10.60, EXTRACTED/REVIEW_REQUIRED.
- unknown.pdf / 02431929-d43e-4b16-a0f3-b219bb5c3c3c: meeting agenda, unknown, EXCEPTION/REVIEW_REQUIRED; no fields/lines or fictional total despite source Total label.
- Real JSONB evidence/raw, NUMERIC Decimal lines, correction history, revision preservation, cross-tenant 404 and approval gate 422 verified; no approved snapshots created.

## Limits and next review
- Supported explicit English headings/colon labels, ISO dates/currency codes/decimal strings, simple unmerged/unrotated single-page 2–4-column tables; live examples cover 2/4 columns. OCR off; arbitrary/scanned layouts unsupported.
- Block-level field boxes are honest coarse evidence, not tight field boxes. Missing/unsupported content requires review; bill/receipt policies remain NOT_CHECKED.
- Full C1-PG concurrency/owner-assignee suite and historical-record immutability remain unverified; existing trigger loophole remains, no schema repair applied.
- Next smallest review: Angeline integrates D2.1 DTOs; Aina specifies bill/receipt required-field and Decimal math rules before record creation. Do not start further gates automatically.
