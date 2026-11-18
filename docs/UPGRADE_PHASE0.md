# Upgrading to the Phase 0 backend (branch `phase0/slim-private-extraction`)

What changes for a teammate who pulls this branch, and what to do. No database schema change:
no migration, no new table or column.

## Read this first: hosted deployment

`render.yaml` now defines **one** web service. If Render deploys `features` automatically from this
blueprint, merging will:

- remove the `accounting-intelligence-rq-worker` and `accounting-intelligence-queue-supervisor`
  services (the API now runs extraction itself);
- stop using `REDIS_URL` and `ENABLE_IMAGE_OCR`;
- ask for `AUTH_SECRET` (the variable the code has always read for sessions and TOTP; the old
  `JWT_SECRET` entry was never read and is gone). Keep the existing `AUTH_SECRET` value, or every
  user is signed out and TOTP secrets cannot be decrypted;
- switch extraction to the lite engine (`EXTRACTION_ENGINE=lite`) with AI off unless
  `AI_EXTRACTION=text` and a key are set.

Check the Render settings before merging, or merge with auto-deploy paused.

## New defaults

| Setting | Before | Now | To keep the old behaviour |
|---|---|---|---|
| Queue transport | Redis + RQ worker + supervisor (3 processes) | `QUEUE_BACKEND=inline`: the API process dispatches and runs extraction; PostgreSQL `pending_jobs` is the queue | `QUEUE_BACKEND=rq` and `pip install -r requirements-rq.txt` |
| Extraction | Docling parser (PyTorch) | `EXTRACTION_ENGINE=lite`: PDF text layer + RapidOCR on CPU, optional text model | `EXTRACTION_ENGINE=docling` and `pip install -r requirements-docling.txt` |
| AI | Qwen paths per `QWEN_*` settings | Text model only when `AI_EXTRACTION=text`; masked OCR text, never the image | unchanged for the Docling path |
| `requirements.txt` | everything incl. docling, pandas, rq, redis, pytest | runtime only | `requirements-dev.txt` (tests), `requirements-docling.txt`, `requirements-rq.txt` |

## What to do after pulling

1. `pip install -r requirements-dev.txt` (adds pytest, fakeredis, pypdf and the RQ add-on the legacy
   tests use). Docling is no longer installed by default.
2. Rename `JWT_SECRET` to `AUTH_SECRET` in your `.env` if you only had the former.
3. Run tests with `PYTHON_DOTENV_DISABLED=1` so your `.env` (AI keys, queue settings) does not leak
   into them.
4. If you start `scripts/run_worker.py` or `scripts/run_queue_supervisor.py`: they now stop with a
   message unless `QUEUE_BACKEND=rq`. Either stop running them, or set `QUEUE_BACKEND=rq` to keep the
   old setup.
5. **Shared database warning.** If your local `.env` points at the shared team database, your local
   API will now pick up and run pending jobs from that database on its own (before, that needed a
   separate worker and supervisor). Use a local database (`docs/LOCALHOST.md`), or limit the queue
   with `QUEUE_TENANT_ID` / `QUEUE_OWNER_ID` / `QUEUE_MIN_UPLOADED_AT`, or set
   `INLINE_SUPERVISOR=false` on machines that should never process jobs.

## Behaviour changes visible in the app

- Bills extracted by the lite engine store the printed number as `bill_number` (was
  `bill_reference`); standardisation already preferred `bill_number` for the record reference.
- Addresses keep their printed line breaks.
- Every field in the extraction DTO and approved-record snapshot has a new `reviewSignal`
  (`verified`, `check`, `suggestion`, `missing`). Additive; no existing key changed.
- `GET /api/processing/health` in inline mode reports `queueBackend: "inline"`, `redis: "not_used"`,
  `workers`, `busyWorkers`, `pendingJobs`. A UI that shows Redis status will show "not_used".
- Spreadsheet uploads (`.xlsx`, `.csv`) are stored as before but fail extraction at once with
  `UNSUPPORTED_EXTRACTION_FORMAT` under the lite engine.
- AI Assist (accountant-requested second pass) still works only on Docling runs and reports
  `EXTRACTION_ARTIFACTS_NOT_ON_THIS_MACHINE` for lite runs (no error page).
- Failure codes from the lite engine (`INVALID_OR_UNREADABLE_PDF`, `INVALID_OR_UNSAFE_IMAGE`,
  `IMAGE_PIXEL_LIMIT`, `LITE_EXTRACTION_FAILED`, `INVALID_LITE_CONFIGURATION`) are now stored as-is
  in `pending_jobs.last_error` instead of `EXTRACTION_FAILED`.
- A job left behind by a stopped API is released after 90 s without a heartbeat and retried
  (`INLINE_STALE_LEASE_SECONDS`), only within the configured queue scope.

## Not verified yet

See the "Not verified" list in the PR and `docs/MASTER_PLAN.md` section 3.
