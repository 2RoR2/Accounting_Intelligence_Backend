# CURRENT CHECKPOINT — COS40005 V2

Date: 2026-10-19
Repository: Backend_V2 / feature/accounting-v2 / HEAD 879e384; prior pending work preserved.
Active: D1A PASS for real digital-PDF invoice-to-review backend. Full production/C1-PG verification remains incomplete.
Next: frontend agent integrates docs/D1A_FRONTEND_HANDOFF.md; do not begin C2 automatically.

## Runtime and dependency approval completed
- Verified sys.prefix/executable/pyvenv.cfg: /home/donut99/.venvs/accounting-intelligence, Python 3.12.14; base interpreter distinct.
- Approved ensurepip --upgrade installed pip 25.0.1; approved pip install --no-deps 'python-multipart>=0.0.9,<1' installed python-multipart 0.0.32, inside that venv only.
- pip check exit 0: No broken requirements found. No other dependencies installed/downgraded; SSD models reused.
- FastAPI 0.143.3 remains outside repository's older manifest range; actual import/upload/auth tested successfully.

## Isolated running services and schema evidence
- API http://127.0.0.1:8002; /api/health and /openapi.json verified 200 from Windows; /docs available.
- PostgreSQL 18.0 Debian: container saic-v2-d1a-postgres18, 127.0.0.1:55434, DB accounting_intelligence_v2_demo, role d1a_v2_demo.
- Server fingerprint 7698223516621660199; cluster saic_backend_v2_d1a; PGDATA /var/lib/postgresql/18/docker.
- Redis 7.2.16: saic-v2-d1a-redis, 127.0.0.1:6382, authenticated dedicated instance; one sequential CPU RQ worker.
- Dedicated volumes saic-v2-d1a-pg18-data / saic-v2-d1a-redis-data; credentials/config ignored under runs/d1a, never printed.
- Actual WSL backend connections to PG/Redis succeeded. Native V2 test cluster and four unrelated stopped containers preserved.
- Fresh target had zero objects/tables before bootstrap. Executed only schema creation section, transactionally; NO DROP/reset preamble executed.
- Consolidated SQL unchanged, SHA256 015118a83b6826f494bb865230905beb10cce889265a75594350951fc149b186.
- Creation-only artifact runs/d1a/schema-create-only.sql SHA256 839e84792c41dd2c773ba99616a35223c676909da356d65663ba0de01d80af31; 51 application tables created.

## Startup and executed commands
- Prefix P = wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B
- API: P scripts/d1a.py api; worker, separate terminal: P scripts/d1a.py worker. Both left running on final code.
- Existing services: docker start saic-v2-d1a-postgres18 saic-v2-d1a-redis. Fresh provisioning helper scripts/d1a-services.ps1 refuses collisions.
- Creation executed via runs/d1a-create-services.ps1; connectivity via P runs/d1a-connect.py; guarded non-destructive bootstrap via P runs/d1a-initialize-create-only.py; all exit 0.
- P scripts/d1a.py prepare-accounts created only synthetic identities, not extracted/approved financial records.
- Private accounts/TOTP seeds: runs/d1a/accounts.json. Lecturer demo uses second account; do not log/copy credentials.

## Implemented contract and financial behavior
- Upload -> isolated Redis/RQ -> real CPU Docling -> classification -> C1B mapping -> PG persistence -> deterministic validation -> review-ready API verified.
- Validation runs inside persistence transaction; errors create blocking exceptions. Raw/normalized/current, evidence/candidates and nullable confidence preserved.
- /revalidate now executes checks, preserves exception history and returns EXTRACTED or EXCEPTION; it never automatically approves/marks validated.
- Quantities and money use Decimal; zero valid; currency required, no MYR fallback; explicit discount/null distinction and existing Decimal tolerance retained.
- Standardization rechecks invoice math while retaining human approval/revision gates. No approvals or standardised records created.
- app.py, persistence.py, validation/models.py updated; new validation/document_validation.py; accounting contract test exception fixture aligned.
- New tests/test_document_validation.py (12), tests/test_d1a_live.py (5), scripts/d1a.py, scripts/d1a-services.ps1, scripts/d1a_demo_invoices.py and frontend handoff.

## Actual tests and API evidence
- P runs/d1a-upload-check.py: exit 0, 5 import/upload checks; 3 existing upload tests use mocked DB/auth/queue, no live startup SQL.
- P -m unittest tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation -q: exit 0, 50 passed, zero failures/errors/skips, 0.461s.
- P runs/c1-pg-offline-extraction.py: exit 0, 130 passed, zero failures/errors/skips, 0.142s; network blocked, excludes previously fixture-blocked test_execution.py.
- P runs/d1a-live-tests.py: exit 0, 5 real HTTP/RQ/CPU Docling/PG tests passed after final-code restart, 15.771s; no parser/auth mocks.
- Real TOTP login/session, tenant denial 404, stale revision 409, correction metadata/append-only audit and review gates verified; no human approval manufactured.
- P scripts/d1a_demo_invoices.py: exit 0, two further actual uploads in clean second tenant; complete 8c3a4df4-95d5-4ff5-b7ec-e9ad2b823233 = EXTRACTED/PASS/50.00.
- Incorrect 63ec0103-11d2-410f-8aa1-44588014d6c8 = EXCEPTION/EX-005/99.00, still uncorrected. Both require human review.
- Final PG query: jobs succeeded, line 2.000 * 25.0000 = 50.00 as Decimal/NUMERIC, JSONB evidence array, confidence NULL; zero approved decisions/standardised records.
- Windows health 200, credentialed CORS preflight 200, unauthenticated document 401. git diff --check and service-script parse passed.
- Evidence: runs/d1a/live-evidence.json and demo-invoices.json; fresh unuploaded PDFs in runs/d1a/demo-ready/; exact frontend API contract/startup in docs/D1A_FRONTEND_HANDOFF.md.

## Remaining limits — stop after D1A
- Full C1-PG race/owner-assignee/immutable-record suite not executed; its 20 previously skipped tests are not integration-verified by D1A's subset.
- Known re-extraction correction carry-forward loss and is_current/financial-column immutability-trigger loophole remain; no schema repair applied. Aleeya/Rachel review required.
- Demo scope: labelled digital invoices, OCR off/Qwen off; production crash recovery/retry guarantees and broader P0/P1 remain unverified. No frontend writes, commits, merges, resets, pushes or deployment. STOP after D1A.
