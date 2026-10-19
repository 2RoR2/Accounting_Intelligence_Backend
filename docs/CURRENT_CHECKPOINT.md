# CURRENT CHECKPOINT — COS40005 V2

Date: 2026-10-19
Repository: Backend_V2 / feature/accounting-v2 / HEAD 851c3de, ahead 2; initially clean. M3B changes uncommitted.
Active: M3B supported bill/receipt checks implemented and live verified; bill amount-due reconciliation remains NOT_CHECKED.
STOP after M3B. No frontend edits, migrations/bootstrap/destructive SQL, installs/downloads, Qwen, approvals, merges, pushes or deployment.

## Preserved environment and data
- Reused WSL Python 3.12.14 /home/donut99/.venvs/accounting-intelligence and existing read-only SSD CPU Docling assets.
- P scripts/d1a.py check -> exit 0: isolated PostgreSQL 18.0 at 127.0.0.1:55434/accounting_intelligence_v2_demo, fingerprint 7698223516621660199; authenticated Redis 127.0.0.1:6382.
- Only verified idle V2 API/worker gracefully reloaded; final API PID 7427, sequential worker PID 7460. Native cluster/shared services/unrelated containers preserved.
- Existing additive startup review-revision check remains on its already-present column; no schema initialization or new migration.
- Fresh synthetic tenants/catalogue fixtures via M3A test provisioning; real password/TOTP sessions, no injected auth/parser results.
- All 12 prior D2.1/D2.2 and 19 M3A document/source/extraction/prediction/line/revision/exception datasets hash-verified unchanged.
- Backend /api/health and Windows frontend http://127.0.0.1:3000/api/health returned 200. API verified; no new browser walkthrough claimed.

## M3B implementation
- Source-classified invoice -> unchanged M3A engine; bill/receipt -> validation/financial_documents.py; unknown/conflicting classification stays high EX-002/unsupported with no invented fields.
- BILL/RECEIPT-REQUIRED/FORMAT check current effective issuer/merchant, date, explicit currency and amount; bill needs reference or account. Receipt identifier/customer/due/PO/payment method are not required.
- BILL-DATES checks due >= bill date and valid ordered ISO billing-period dates. Unsupported period formats remain NOT_CHECKED.
- Type-prefixed LINE_DETAILS/LINE_MATH/SUBTOTAL use available descriptions, Decimal quantity × price and independently evidenced line sum/subtotal; existing 0.01 tolerance. No missing quantity=1 or monetary=0 defaults.
- RECEIPT-TOTAL checks explicit subtotal + explicit tax, less supported explicit discount, against total. Missing components/unsupported tables/observed unsupported adjustment evidence defer checking.
- RECEIPT-RETURNS: signed amounts/nonpositive quantities require a returns policy -> NOT_CHECKED; no invented business policy or payment completion.
- BILL-AMOUNT_DUE always NOT_CHECKED: canonical total aliases amount due, without typed payments/credits/carry-forward components. Never equate due with original charges; bills retain REVIEW_REQUIRED.
- Medium missing/format/date findings -> WARNING, EXCEPTION/REVIEW_REQUIRED; high arithmetic findings -> FAIL/blocking. NOT_CHECKED -> WARNING. No new FAILED validation enum; processing FAILED unchanged.
- Complete supported simple receipt -> PASS/coverageComplete=true. Incomplete/failed scope -> false. Human review always required, no automatic approval/standardisation.
- Effective corrections/NUMERIC lines used, raw/normalized/candidate/evidence/reviewer/revision/history preserved. Superseded VALIDATION:* rows retained resolved with timestamps/notes.
- Fixed severity lookup for new codes; standardisation checks current M2 errors/coverage plus existing human/revision gates. No schema changes or approved snapshot rewrites.
- DTO adds only optional required:boolean on check rows and new rule strings; see docs/M3B_FRONTEND_HANDOFF.md. Review: Aina/Rachel/Angeline; Aleeya schema unchanged.

## Executed commands/results
- P = wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B
- P -m unittest tests.test_bill_receipt_validation tests.test_m3_validation tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation tests.extraction.test_multidocument -q -> exit 0, 100 passed (24 new + 76 prior), 0 failures/errors/skips, final 0.838s.
- P runs/c1-pg-offline-extraction.py -> exit 0, 142 passed, 0 failures/errors/skips, 0.121s; sockets blocked; overlaps 12 profile tests, excludes previously fixture-blocked test_execution.py.
- P runs/m3a-po-unit.py -> exit 0, 4 original standalone PO functions passed; no database PO matching.
- P runs/m3b-live-tests.py -> exit 0, 20 passed, 37 actual PDFs, 0 failures/errors/skips, 161.441s. Includes all 12 M3A live regressions/EX-001–EX-008 and six original layouts.
- Earlier live M3B run: 18 passed/144.378s; final run adds final receipt line/subtotal correction, incomplete-line and signed-return guards.
- Sandbox-denied initial WSL launches retried with approved execution; no failed executed assertions. Pytest absent, original pytest suite not rerun/claimed passed; no install.
- git diff --check -> exit 0. Startup remains P scripts/d1a.py api / P scripts/d1a.py worker in separate terminals only if existing verified services are stopped.

## Real evidence
- Sanitized API/PG assertions: runs/m3b/latest-live-evidence.json. Inputs: runs/m3b/d6139df4a9ef40669faf2bffaa296031/pdfs/; four original multi-document fixture bytes SHA256-verified identical.
- Private accounts: runs/m3a/d6139df4a9ef40669faf2bffaa296031/accounts.json (main then foreign); vendor-account.json is separate controlled catalogue. No credentials/codes printed.
- Bill 2a04deed-92aa-4e0f-a44c-08b63a51f657: 80.00, correct reference/period/due, null subtotal/tax/quantity/unit price; supported checks executed, reconciliation NOT_CHECKED/REVIEW_REQUIRED.
- Receipt a3c09473-1480-4164-96de-9e3f82c9b46d: 2 × 5.00 = 10.00; 10.00 + 0.60 = 10.60, EXTRACTED/PASS, human review, real boxes/spans intact.
- Bad receipt d927cd8b-a3aa-444f-be0f-093306283fa1: 99.00 -> RECEIPT-TOTAL high/FAIL, corrected 10.60/PASS, revision +1, stale 409; original/evidence/reviewer/audit preserved, prior issue resolved.
- Receipt line/subtotal and bill line/subtotal/due/period issues each persisted with intended severity, resolved after real field/line corrections. Bills remain REVIEW_REQUIRED after observed errors are fixed.
- Missing issuer/merchant/date/currency, malformed receipt date/time, explicit zero totals/balance, missing tax/quantity and signed return verified live. Foreign reads/files/validation/PATCH/revalidate 404, unchanged data.
- Invoice 2046d8fa-6d77-464f-bac2-ff78a3366a8c: 50.00/PASS; bad 67682f3a-c656-4e13-a696-b91a96e1acb2: 99.00/EX-005 then saved 50.00/PASS. Invoice B 48.00/PASS; unknown empty fields/EXCEPTION.

## Remaining / next smallest task
- Tested labelled digital-PDF/simple-table layouts only; no arbitrary scans, tax-law checks, currency catalogue verification, returns policy, complex adjustments or bill balance reconciliation claimed.
- Optional invoice vendor/PO references remain NOT_CHECKED when unavailable. No bill/receipt duplicate/vendor-reference or live PO matching added. Full C1-PG owner/assignee/immutability and concurrency gaps remain outside this checkpoint.
- Next: Aina review bounded rule/severity/coverage semantics; Angeline may show optional required scope and unique rule+field+index keys. Complete bills first need an agreed typed evidence contract for balances/payments/credits. Do not proceed automatically.
