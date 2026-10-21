# D3 — inference blocked; existing website contract preserved

2026-10-19, Backend_V2 / feature/accounting-v2, base e9d100b. Application code, schema and Frontend_V2 unchanged. D3 is **BLOCKED**, not an AI integration success. Following the user instruction to stop inference when no operational Qwen endpoint/model exists, no server/model was started or reconfigured.

## Exact serving dependency

- Existing WSL `/usr/local/bin/ollama` server PID 183, Ollama 0.35.0, answers at `http://127.0.0.1:11434`.
- `GET /api/tags` and `/api/ps` return HTTP 200 `{"models":[]}`.
- Read-only `POST /api/show` with `{"model":"qwen2.5vl:7b"}` returns HTTP **404** `{"error":"model 'qwen2.5vl:7b' not found"}`. This is a metadata request, not inference.
- Prototype `http://127.0.0.1:11435` refuses connections. Both Windows and actual WSL runtime checks confirm the endpoint gap.
- Historical SSD store exists at `/mnt/d/Jonathan Files/AccountingIntelligence_SSD_Starter/AccountingIntelligence/models/ollama`. Manifest: `manifests/registry.ollama.ai/library/qwen2.5vl/7b`, SHA256 `5ced39dfa4bac325dc183dd1e4febaa1c46b3ea28bce48896c8e69c1e79611cc`.
- All six referenced blobs exist with matching manifest sizes: 5,969,245,856 bytes total. Historical model details identify qwen25vl, 8.3B parameters, Q4_K_M, vision capable. Full weight-file hashes and current inference health were **not** verified. Old SSD outputs are historical evidence only, not current D3 model calls.
- SSD mount is **read-only** and was preserved. No pull, model download, package install, model load, service restart or Docker/WSL resource change.
- Snapshot: RTX 3060 Laptop GPU 6,144 MiB total / 856 MiB used; Windows free RAM 2.54 GiB; WSL 7,793 MiB total / about 5,718 MiB available, 2,048 MiB swap unused. No inference resource/latency measurement exists.

Next dependency: expose the **already present** SSD Qwen model through an isolated loopback Ollama instance while preserving the historical read-only store and existing 11434 service. Use separate writable V2 home/state; one loaded model, one request at a time, bounded context/timeouts and immediate unloading. Confirm current host RAM headroom before loading. No model download is needed on the evidence available; asset health and read-only-store serving compatibility still require verification. This setup was not executed under the explicit unavailable-endpoint stop condition.

Read-only recheck from Backend PowerShell:
```powershell
wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B runs/d3-preflight.py
```
The ignored probe checks only manifest/blob sizes, mount flags, memory and local metadata; sanitized output is `runs/d3/preflight.json`. It does not send document images or invoke a model.

## Reused prototype and remaining integration boundary

Current `services/extraction/{route_extraction,run_selective_qwen,qwen_provider,reconcile_selective,document_intelligence_service,module2_public_result}.py` already mirrors the historical SSD `project/scripts/` prototype. It renders an actual verified, hash-bound single PDF page through pypdfium2; sends base64 image to Ollama `/api/chat`; validates exact requested string/null JSON keys; keeps parser and AI values separate. Existing provider timeout is 120 seconds, context 4096, keep-alive 5 minutes, fixed qwen2.5vl:7b. These defaults were not activated or changed.

This is not yet the requested D3 integration: routing is invoice-header-only, ambiguous/non-invoice classifications are human-review blockers, queue execution defaults to `allow_vlm_inference=False`, and structured candidate model/page/run identity and classification suggestions still need the focused contract extension. Historical candidate helpers and mocked tests are not proof of an API-persisted real Qwen result. Genuine ambiguous-field recovery, missing-field abstention, classification suggestions, live provider-error fallback and candidate reload remain **BLOCKED/unverified**.

## Frontend contract

**No new DTO or frontend work is activated.** Continue using D2.2/M3A/M3B authenticated extraction/validation/correction APIs. Existing extraction prediction `candidates`, `rawValue`, `normalizedValue`, `currentValue`, evidence, nullable confidence and contentRevision are unchanged. No candidate, bounding box, source citation, confidence, approval or PASS was manufactured for D3.

After backend inference is operational and candidate persistence is verified, Angeline will need a separate candidate-review presentation showing actual model identity, page/image evidence, routing reason, abstention/error status and run identity. Acceptance must call the existing authorized field correction endpoint with `expectedContentRevision`, handle 409, then POST revalidate. AI classification must stay a suggestion alongside the original deterministic classification. Exact additional DTO names must be published from that implemented/verified backend contract, not invented by the frontend now. Owners: Jonathan (M2), Rachel (API/worker), Aina (validation), Angeline (review UI); no schema proposal/applied migration here.

## Executed regression evidence

P = `wsl -d Ubuntu --cd /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend_V2 --exec /home/donut99/.venvs/accounting-intelligence/bin/python -B`.

- `P -m unittest tests.test_bill_receipt_validation tests.test_m3_validation tests.test_accounting_contract tests.test_field_corrections tests.test_document_validation tests.extraction.test_multidocument -q`: exit 0, 100 passed, no failures/errors/skips, 0.494s.
- `P runs/c1-pg-offline-extraction.py`: exit 0, 142 passed, no failures/errors/skips, 0.131s; sockets blocked, includes mocked selective-Qwen/routing tests. Overlaps 12 profile tests; existing fixture-blocked test_execution.py excluded. Mocked candidate/abstention/error tests are **not real Qwen proof**.
- `P runs/m3b-live-tests.py`: exit 0, 20 passed, no failures/errors/skips, 161.309s, 37 actual Docling PDF uploads to fresh isolated synthetic tenants. Invoice 50.00/PASS, 99.00/EX-005 then correction/PASS, invoice B 48.00/PASS, bill 80.00/REVIEW_REQUIRED, receipt 10.60/PASS, unknown/EXCEPTION; M3A EX-001–EX-008 and M3B corrections/tenant denial preserved. Qwen remained disabled in the live queue path.
- Original D2.1/D2.2 and prior M3A stored data hashes unchanged. Sanitized regression responses copied to `runs/d3/baseline-live-evidence.json`. API and Windows frontend-proxy health HTTP 200. No new browser/model inference claim.
- `P runs/d3-preflight.py` and `git diff --check`: exit 0. Pytest is absent and was not installed/rerun. No model inference calls, output, abstention, latency or candidate persistence were demonstrated in D3.

Only documentation and an ignored read-only probe were added. Application behavior remains the working Docling/M3B baseline. STOP at this blocker; do not interpret a successful regression run as D3 implementation completion.
