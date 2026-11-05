# Extraction recovery — 6 November 2026

## Completion evidence

The live batch is still being processed. The authoritative checkpoint is
`.diagnostics/extraction-status-latest.json`, refreshed by `scripts/extraction_status.py`.
Its `real_persisted` count requires a current raw extraction linked to a **succeeded
PostgreSQL attempt**. Dispatch or Redis acceptance is never counted as extraction.

All 14 originally failed document IDs now have persisted, current results.
The batch contains 180 processing documents; 10 previously validated sample notes
are separate and have not been overwritten or re-approved. Human review and
approval remain required. An `exception` document with a succeeded extraction
attempt is reviewable with validation findings, not an extraction failure.

## Verified runtime

- Python: `C:\AI-Python312\python.exe`.
- RQ: **2.12.0**, `WindowsWorker` (in-process `SimpleWorker` subclass),
  `TimerDeathPenalty`, **JSONSerializer**, queue **cpu_tasks**, Redis **database 0**.
- Docling **2.147.0**, RapidOCR **3.10.0**; local assets: `ai/models/docling`.
- `ENABLE_IMAGE_OCR=false` disables the separate POSIX-only bridge, **not OCR**.
  Docling/RapidOCR still processes images through the supported image-to-PDF path.
- The user explicitly selected **OCR without Qwen for this batch**. Dedicated
  `accounting-batch-ocr-*` workers use `--qwen-mode off`; the normal `.env` setting
  was not globally disabled.
- API: `http://127.0.0.1:8000`; the frontend's same-origin rewrite targets this port.

## Errors and fixes

1. **Current historical failures lost their cause.** All 14 original failed-job
   tracebacks end in `ExtractionExecutionError: Extraction attempt failed.`
   Earlier code used `raise ... from None`, so the original underlying causes
   cannot honestly be reconstructed for each historical attempt. Full chained
   exceptions are now logged privately; authorization denials remain redacted
   in Redis/API output. The original tracebacks are retained in
   `.diagnostics/extraction-audit.json`.
2. **Unsupported Windows bridge:** the real S3 image resolution/execution path
   reproduced `IMAGE_OCR_REQUIRES_LOCAL_POSIX_AI_ENV`. Workers now use installed
   Docling/RapidOCR with local model assets. A representative PDF and image then
   completed through real RQ and persisted to PostgreSQL; subsequently all 14
   original failure documents did so.
3. **Delivery/worker mismatch:** old competing workers and incompatible payloads
   produced `rq.exceptions.DeserializationError`, `STALE_DELIVERY`,
   `RQ_DELIVERY_MISMATCH`, and expired-dispatch errors. Entry points now use one
   serializer, explicit dispatch identities, database-fenced claims, and real
   claim/processing states. The outbox dispatches against actual idle-worker
   capacity rather than flooding Redis and allowing waiting deliveries to expire.
4. **Credit/debit note review:** reproduced
   `ValueError: Unsupported stored field: document_kind` in the extraction DTO.
   The legacy field alias and raw document-type fallback are supported. UBL JSON
   notes have an exact-pointer source parser and readable source viewer; missing
   or placeholder source values are not fabricated or approved.
5. **Windows shutdown/heartbeat:** RQ's default shutdown command sends
   `os.kill(SIGINT)`, which terminated two Windows workers rather than draining.
   Both confirmed-dead attempts were retained as `WORKER_EXITED` failures and
   received new fenced attempts. The custom worker now drains without a signal
   and renews both RQ execution and PostgreSQL claim heartbeats. Recovery only
   treats a local PID as dead when Windows positively confirms it; access denial
   is not death proof. No old attempt counters or identities are reset.
6. **Persistence hardening:** processing duration starts at actual worker claim,
   not upload/queue time. Source-object renaming copies before SQL commit and
   keeps the old object; a same-key rename cannot delete the source. Persisting
   the same attempt is idempotent and cannot create a second current extraction.
7. **Seeding:** source imports use the normal transactional outbox and worker
   processing path. Uploads accept a durable pending receipt without falsely
   calling it dispatch or extraction success. The earlier script that invented
   accounting totals/approvals has been disabled; existing immutable accounting
   history was not silently changed and has not been independently certified.

## Historical Redis inventory

The initial registry contained **947 historical failed deliveries**, not 947
currently failed documents. Major retained groups were 304 deserialization
errors, 287 missing/expired dispatch errors, 189 stale deliveries, 49 delivery
identity mismatches, and 37 generic extraction failures. Other old failures
include missing `onnxruntime`/`docling`/`dateutil`, old model paths, corrupt or
missing model files, stale trusted paths, and abandoned executions. The audit
contains every captured traceback; these old failures were not blindly replayed.

The registry subsequently gained the two genuine `WORKER_EXITED` failures noted
above. **Redis was not cleared, documents were not deleted, and no job was
marked successful merely because dispatch succeeded.** Redis finished results
have a one-hour TTL; durable completion counts must come from PostgreSQL.

## Verification

- Backend extraction/queue/accounting suites: **87 passed**, plus **16 subtests**.
- Frontend unit suites: **87 passed**, no failures. Three default-port smoke
  checks were separately run against the actual API on port 8000: **3 passed**.
- Frontend TypeScript check passed.
- After the API fix, 11 stored credit/debit notes returned **HTTP 200 in 44
  document/extraction/validation/source-file checks** against the live database
  and object storage, with owner identity supplied only by the test harness.
  This does not claim an end-to-end MFA/browser sign-in test.
- Live checks include duplicate active attempts, duplicate current extraction
  rows, current failures, lease heartbeats, and each original failure's saved run.

## Remaining operational cautions

- Optional Qwen inference previously reproduced `TimeoutError: timed out` after
  about 120 seconds. This batch intentionally runs without it, as requested.
  Missing/conflicting values still need human review.
- Object storage currently has the pre-existing `S3_TLS_VERIFY=false` setting.
  Reads work, but certificate verification must be restored with an appropriate
  trusted CA configuration before treating this as production-secure storage.
- Public sample documents can contain placeholders. They are test sources, not
  proof of actual business transactions, payment, approval, or settlement.
