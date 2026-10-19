# CURRENT CHECKPOINT — COS40005 V2

Date: 2026-10-19
Repository: Backend_V2 / feature/accounting-v2; application baseline e9d100b. Existing D3 notes preserved.
Active: D4-A completed; features integration preflight NO-GO. D3 remains BLOCKED; no actual Qwen inference claimed.
STOP before any features merge/push/deployment. See docs/FEATURES_INTEGRATION_PREFLIGHT.md and frontend docs/DEMO_RUNBOOK.md.

## D4-A final / integration preflight
- Chrome 20/20, frontend npm tests 15/15, typecheck/lint PASS; recovered production build exit 0, 40/40 pages. No browser rerun. 71 screenshots preserved; native PDF visual rendering remains unverified despite authenticated HTTP/hash/popup success.
- Inspected and executed read-only runs/d4a-after.py: exit 0, 68 protected document datasets unchanged; D4-A tenants have 20 synthetic documents, 0 approvals/standardised records. Both local health endpoints HTTP 200.
- Latest remote backend features 2e63075 diverges (2 team-only/5 V2-only), includes bank route absent in V2; cached origin/features is stale. All 16 auth handlers and origin guard unchanged; six offline origin cases passed. Teammate's actual 403 headers/environment unverified.
- Vercel production branch/settings/deployed backend compatibility UNVERIFIED. Preserve team bank/auth work, inspect clean default build and use isolated HTTPS preview resources before integration approval. No origin weakening, schema changes, merges, pushes or deployments.

## Preserved working baseline
- D1A/D2/D2.1/D2.2/M3A/M3B remain implemented. No application/frontend/schema changes, approvals, installs, downloads, model loads, service restarts, Docker/WSL changes, merges or pushes in D3.
- Existing WSL Python 3.12.14, CPU Docling and read-only SSD assets reused; V2 API/worker left running.
- Only fingerprint-guarded isolated PostgreSQL 18.0 at 127.0.0.1:55434/accounting_intelligence_v2_demo and authenticated Redis 127.0.0.1:6382 used for regression fixtures. Shared/native/unrelated resources untouched.
- Invoice 50.00 and invoice B 48.00 PASS; invoice 99.00 EX-005 then corrected 50.00/PASS. M3A EX-001–EX-008 corrections/reference isolation reconfirmed.
- Receipt 10.60 genuine Decimal line/subtotal/total PASS, review still required. Bill 80.00 executes field/date/period checks but amount-due balance/payment/credit reconciliation stays NOT_CHECKED/REVIEW_REQUIRED.
- Receipt/bill missing/format/math/date/zero/null/return cases, correction revisions/audits and cross-tenant 404 reconfirmed. Unknown remains EXCEPTION, no accounting fields.
- All 12 original D2.1/D2.2 and 19 previous M3A stored document/extraction/field/line/revision/exception datasets hash-verified unchanged. New synthetic regression tenants/documents only.
- Backend http://127.0.0.1:8002/api/health and Windows frontend http://127.0.0.1:3000/api/health returned 200; no new browser acceptance claimed.

## D3 exact blocker and resources
- Existing WSL /usr/local/bin/ollama serve PID 183 / version 0.35.0 at 127.0.0.1:11434: /api/tags and /api/ps HTTP 200, both models=[].
- Metadata POST /api/show for qwen2.5vl:7b -> HTTP 404: model 'qwen2.5vl:7b' not found. No inference/document-image request made.
- Prototype endpoint 127.0.0.1:11435 refuses connections, confirmed from Windows and WSL.
- SSD /mnt/d/Jonathan Files/AccountingIntelligence_SSD_Starter/AccountingIntelligence/models/ollama has the qwen2.5vl:7b manifest and all 6 referenced blobs, matching sizes totaling 5,969,245,856 bytes.
- Manifest SHA256 5ced39dfa4bac325dc183dd1e4febaa1c46b3ea28bce48896c8e69c1e79611cc. Historical record: vision-capable qwen25vl 8.3B / Q4_K_M. Full weight hashes/current inference health NOT verified.
- SSD mount verified ro; no historical files overwritten. Prototype project/scripts and selective-* outputs inspected read-only; historical success is not current D3 proof.
- Snapshot: RTX 3060 Laptop 6144 MiB total / 856 MiB used; Windows free RAM 2.54 GiB; WSL 7793 MiB total / about 5718 MiB available, 2048 MiB swap unused. Inference usage/latency unknown because zero calls.
- Dependency: separately serve existing SSD model on an isolated loopback endpoint with writable V2 home/state, preserving read-only model assets/current daemon; verify memory headroom and bounded single-model/single-request behavior first. No download indicated by existing assets.

## Existing prototype boundary, not yet D3
- services/extraction/{route_extraction,run_selective_qwen,qwen_provider,reconcile_selective,document_intelligence_service,module2_public_result}.py reuse the historical verified-PDF/image and strict string/null JSON candidate workflow.
- Current prototype is invoice-header/single-page only, routes ambiguous/non-invoice kinds to human review; execution/queue path defaults inference off. Provider default port 11435, fixed qwen2.5vl:7b, timeout 120s, context 4096, keep-alive 5m.
- Ambiguous classification suggestions, type-aware selective targets, bounded runtime admission and full per-candidate model/page/run metadata still require focused implementation after serving works.
- D3 actual model calls=0, source images sent=0, candidates produced/persisted=0. Missing-field abstention, visible-field recovery and live provider-failure fallback are BLOCKED/unverified; mocked tests do not prove them.
- No frontend DTO change activated. D2.2/M3A/M3B APIs remain; docs/D3_FRONTEND_HANDOFF.md records exact blocker and pending review integration. Never infer approval/payment/confidence or promote candidates automatically.

## Actual D3 verification
- P = wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B
- P runs/d3-preflight.py -> exit 0; read-only local metadata/manifest/blob-size/mount/memory probe, no inference. Saved runs/d3/preflight.json. Initial sandbox-denied WSL launches retried with approved execution.
- P -m unittest tests.test_bill_receipt_validation tests.test_m3_validation tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation tests.extraction.test_multidocument -q -> exit 0, 100 passed, 0 failures/errors/skips, 0.494s.
- P runs/c1-pg-offline-extraction.py -> exit 0, 142 passed, 0 failures/errors/skips, 0.131s. Sockets blocked, includes mocked selective-Qwen/routing/publication tests; overlaps 12 profile tests, excludes previously fixture-blocked test_execution.py. Not real inference.
- P runs/m3b-live-tests.py -> exit 0, 20 passed, 37 fresh real CPU Docling PDFs, 0 failures/errors/skips, 161.309s; M3A/M3B baseline, not Qwen integration proof.
- Sanitized D3 regression copy: runs/d3/baseline-live-evidence.json. Private account path remains in ignored evidence; no credentials printed. Pytest still unavailable; not rerun/installed/claimed passed.
- git diff --check -> exit 0. API/worker startup unchanged: P scripts/d1a.py api / P scripts/d1a.py worker, separate terminals only if existing services are stopped.

## Next smallest task / limits
- Resolve only the installed-model serving dependency, verify model inventory/load under RAM limits, then resume D3 actual source-image candidates/abstention and persistence tests. Do not claim D3 complete or start later gates.
- Existing M3B layout/returns/bill-reconciliation limits, unavailable live PO data, full C1-PG ownership/immutability and concurrency gaps remain. Review owners: Jonathan/Rachel/Aina/Angeline; schema unchanged.
