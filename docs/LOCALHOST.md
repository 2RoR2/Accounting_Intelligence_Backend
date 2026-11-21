# Run Accounting Intelligence on your laptop

One backend process (FastAPI, which also runs extraction), one local PostgreSQL, and the
Next.js frontend. No Redis, no worker process, no GPU. AI is off unless you turn it on.

## 1. Prerequisites

| Tool | Version | Used for |
|---|---|---|
| Python | 3.12 or 3.13 | backend |
| Docker Desktop | any recent | local PostgreSQL (or install PostgreSQL 16 yourself) |
| Node.js | 20 or newer | frontend |
| Git | any | both repositories |

Disk: about 400 MB for the backend virtual environment. RAM: the API uses about 75 MB idle and
roughly 300-400 MB while it reads a scanned page.

## 2. Backend setup

```powershell
cd Accounting_Intelligence_Backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env
```

`requirements-dev.txt` installs the runtime plus test tools. `requirements.txt` alone is enough to
run the API.

Edit `.env` (it is ignored by git; never commit it):

| Variable | Set it to |
|---|---|
| `DB_PASSWORD` | any password for the local database container |
| `DATABASE_URL` | `postgresql://postgres:<DB_PASSWORD>@127.0.0.1:5433/accounting_db` |
| `AUTH_SECRET` | a random string of 32+ characters: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `FRONTEND_ORIGINS` | `http://localhost:3000` |
| `COOKIE_SECURE` | `false` on a laptop (plain http); `true` only behind https |
| `S3_BUCKET` | leave empty on a laptop so uploads stay in `storage/` and never reach the shared bucket |
| `TEST_DATABASE_URL` | optional: a second, disposable database for the test suite (never the one you use) |

Use only a database on your own machine here. Do not point `DATABASE_URL` at the shared team
database from a laptop: the API now runs extraction itself and would pick up shared jobs. If you
must, limit it with `QUEUE_TENANT_ID` / `QUEUE_OWNER_ID` / `QUEUE_MIN_UPLOADED_AT`.

## 3. Start PostgreSQL and create the schema

```powershell
docker compose up -d db
.venv\Scripts\python services\admin\manage.py migrate
```

`docker compose up -d db` starts only PostgreSQL on `127.0.0.1:5433` (Redis and the old worker are
behind the opt-in `rq` profile). `manage.py migrate` loads `database/PostgreSQL Database schema.sql`
into an empty database and refuses to touch a non-empty one.

Demo data (optional, fictional; run once on a fresh schema; the demo password is in the file
header), then align the document-code counters with the seeded codes, otherwise the first upload
fails with a duplicate `document_code`:

```powershell
Get-Content "database\mock.data.sql" | docker compose exec -T db psql -U postgres -d accounting_db -v ON_ERROR_STOP=1
.venv\Scripts\python -c "import main; main.apply_compatibility_migrations()"
```

### One-command demo company (plan v2, A3)

A fuller demo: one fictional Malaysian company with three months of books, loaded through the
same code paths an accountant uses (CSV imports, document upload, bank import):

```powershell
.venv\Scripts\python services\admin\manage.py seed-demo-company --pack trading
```

`--pack` is `trading`, `services` or `fnb`; add `--month 2026-08` for one month only (faster).
It refuses to run unless `APP_ENV=development`, `DATABASE_URL` points at `127.0.0.1` or
`localhost`, and `S3_BUCKET` is empty. It:

1. generates the pack under `ai/synthetic/output/packs/<pack>/` if it is not there yet
   (`docs/COMPANY_PACKS.md`) and aligns the document-code counters;
2. creates the company and three users, `accountant@<pack>.demo.test`, `admin@<pack>.demo.test`
   (local admin) and `superadmin@demo.test`, and prints their generated passwords **once** in the
   console (nothing is written to a file; `--prompt-password` lets you type one instead, and
   existing users keep their passwords unless you add `--reset-passwords`);
3. creates the monthly accounting periods and imports the chart of accounts, contacts and opening
   balances through the CSV imports (`/api/chart-of-accounts/import`, `/api/contacts/import`,
   `/api/opening-balances/import`; templates at `.../import/template`);
4. uploads every pack document through the normal upload, one per free extraction worker, and
   waits for extraction (the whole seed took 1.3-2.5 minutes for 58-62 documents on a laptop with
   AI off), then imports the bank statements through the bank import and confirms them;
5. prints the documents per state and how many bank lines the reconciliation suggestions match
   correctly against the pack's `expected_matches.csv` (`--report file.json` saves these numbers,
   never the passwords).

Running it again skips what is already there. With AI off (the default) the extraction reads the
text but does not recognise the document kind, so every document lands in "needs fixing" (EX-002);
set `AI_EXTRACTION=text` with a provider key (section 7) to see documents reach review. The F&B
pack's bank layout (`dr_cr`) is not readable by the bank CSV parser yet (task C5), so the seed
imports those rows through the bank import's JSON form instead.

### Re-read the demo documents after turning AI on

Re-running the seed skips documents that already exist, and "Read again" in the app only takes
failed documents. After you set `AI_EXTRACTION=text` and a provider key (section 7), read the
stored documents again with the new settings:

```powershell
.venv\Scripts\python services\admin\manage.py reread-documents --pack trading --dry-run
.venv\Scripts\python services\admin\manage.py reread-documents --pack trading
```

`--pack` names the seeded company; use `--company "<exact name>"` for any other company. The
command has the same local-only guard as the seed (`APP_ENV=development`, a `127.0.0.1`/`localhost`
`DATABASE_URL`, no `S3_BUCKET`). It prints one line per document and a summary; `--dry-run`
changes nothing. By default it re-reads documents in `exception` ("needs fixing"), `extracted` or
`failed`; narrow that with `--status exception,failed` and `--limit 5`. It never touches a document
that someone corrected, submitted, reviewed or approved, one that already has an open job, or one
past review. Each re-read is a new extraction attempt in the normal queue (earlier attempts are
kept and the audit log records `extraction.reread_scheduled`). The command then runs the reading
in its own process with its own settings and waits (`--timeout-minutes`, default 30). Until then
it holds the jobs, so a backend that is running with other settings does not take them. Finally
it prints the new statuses with the average and p95 reading time of the documents it read.
It dispatches only the documents it listed, never other waiting jobs of the company. At the
timeout (or Ctrl+C) it stops handing out work, closes the unfinished jobs, marks those documents
`failed`, lists them and exits with code 3 without waiting for a reading in progress; run it
again with `--status failed` to finish them. The timeout counts from the moment the first job is
queued, and nothing new starts after it. A document that cannot be handed to a worker is listed
and marked `failed` while the others carry on. `--no-wait` queues the jobs without a hold and
leaves them to the running backend.

### UAT test accounts: each teammate signs in with their own email (plan v3, E1)

Real sign-in codes sent to real team addresses, against a local database. The addresses live only
in your `.env` (git-ignored) and in the local database, never in the repository.

Add to `.env` (the variable names are in `.env.example`):

| Variable | Set it to |
|---|---|
| `APP_ENV` | `development` |
| `EMAIL_PROVIDER` | `brevo` |
| `BREVO_API_KEY`, `BREVO_FROM_EMAIL`, `BREVO_FROM_NAME` | the team's Brevo key and a sender address verified in Brevo |
| `UAT_TEST_EMAILS` | comma separated `email` or `email:role`. The role is `accountant` (the default), `local_admin` or `super_admin`, and at least one entry must be a `local_admin`. Shape: `you@example.com:local_admin,teammate@example.com` |
| `UAT_TEST_PASSWORD` | optional: the password new test users get (8+ characters, upper and lower case, a number and a symbol). Without it the command asks for one. |

From an empty database to a signed-in teammate:

```powershell
docker compose up -d db
.venv\Scripts\python services\admin\manage.py migrate
.venv\Scripts\python services\admin\manage.py seed-demo-company --pack trading
.venv\Scripts\python services\admin\manage.py create-test-users
.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Then start the frontend in a second terminal (section 5, with `BACKEND_URL=http://127.0.0.1:8000`).

`create-test-users` adds every `UAT_TEST_EMAILS` address to the trading demo company. Use
`--pack services` or `--pack fnb` for the other demo companies, or `--company "Exact name"` for any
company. It prints each address masked (`f***@e***.com`) with what it did: `create`, `update` or
`unchanged`.

- **Running it again:** after you change the list, it creates the new addresses and applies role
  changes; everyone else is left alone. Taking an address out of the list does not delete its
  account.
- **Passwords:** existing users keep their password unless you add `--reset-passwords`, which also
  signs them out.
- **Work to do:** an accountant sees only documents they own or are assigned, and the seeded
  documents belong to the demo accountant. So the command shares the company's unassigned
  documents among the test accountants, in turn. Add `--no-assign` to skip this. Documents
  someone already holds are left alone.
- **Limits:** it never moves an account that belongs to another company, and it raises the
  company's seat limit when the list needs more seats. It refuses to run against anything but a
  local database, with the same check as `seed-demo-company`.

Then open <http://localhost:3000> and sign in with your email and the test password:

- **Accountant:** a 6-digit code arrives by email from the Brevo sender. It is valid for 60
  seconds; use "Resend code" if it expires. Check the spam folder the first time.
- **Local admin or super admin:** the first sign-in shows a QR code. Scan it with an authenticator
  app (Google Authenticator, Microsoft Authenticator, 1Password, Bitwarden, ...) and type the
  6-digit code within 60 seconds. Later sign-ins ask for the app's current code. The codes depend
  on the clock: if they are refused, check that the phone and the laptop set their time
  automatically.

For a lost or new phone, or an authenticator that stopped working, reset the authenticator. One
cause: `AUTH_SECRET` encrypts the stored authenticator secrets, so changing it breaks them all.

```powershell
.venv\Scripts\python services\admin\manage.py reset-mfa --email <their email>
```

This clears the authenticator and signs that user out everywhere; the next sign-in shows a new QR
code. It works only for active local admins and super admins (accountants have no authenticator).

If an accountant sees "Unable to send sign-in code" (HTTP 503), the email settings are wrong or
Brevo refused the message. Check `EMAIL_PROVIDER` and `BREVO_API_KEY`, check that
`BREVO_FROM_EMAIL` is a verified sender, and look for the API log line
`Login OTP delivery failed: ... (HTTP <status>)`.

Or create your own accounts:

```powershell
.venv\Scripts\python services\admin\manage.py create-admin --email you@example.com --name "Your Name"
```

Sign-in rules: super administrators and local administrators use password + an authenticator app
(TOTP; the first sign-in shows a QR code). Accountants get a one-time code by email, which needs
`EMAIL_PROVIDER` with Brevo (`BREVO_API_KEY`) or SMTP (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`)
in `.env`. Without email settings, test with an administrator account.

## 4. Start the backend

```powershell
.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Check: <http://127.0.0.1:8000/api/health> and the API docs at <http://127.0.0.1:8000/docs>.
The first scanned upload loads the OCR model (a few seconds); set `OCR_WARM_ON_START=true` to load
it at startup instead.

## 5. Start the frontend

In the frontend repository (`Accounting_Intelligence_Frontend`, branch `features`):

```powershell
npm ci
# point the frontend at the local backend, e.g. in .env.local:  BACKEND_URL=http://127.0.0.1:8000
npm run dev -- --port 3000
```

Open <http://localhost:3000> and sign in.

## 6. Upload a sample and see the result

1. Upload a PDF or JPG from `ai/assets/invoice`, `ai/assets/bill` or `ai/assets/receipt`
   (public sample documents), or a synthetic one after running `python ai/synthetic/generate.py`.
2. The document goes `queued` -> `processing` -> review within seconds; a scanned page takes 3-10 s
   on a laptop CPU, a digital PDF well under a second.
3. Open the document to review the fields. With AI off, most fields are empty and the read text
   is ready for manual entry; with AI on, printed values are pre-filled and still need review.
4. Processing status for administrators: `GET /api/processing/health` (`queueBackend: inline`).
5. Files on disk: uploads under `storage/` (unless S3 is configured), extraction runs under
   `runs/lite-*` (`public-result.json`, plus `ocr.private.json` with the full read text; both
   stay on this machine).

## 7. Turn AI on or off, change provider

AI is off by default: nothing leaves your machine. To turn it on, add to `.env` and restart the API:

```ini
AI_EXTRACTION=text
AI_PROVIDERS=gemini          # or qwen, openrouter, ollama, custom
GEMINI_API_KEY=<your key>    # per provider: DASHSCOPE_API_KEY (qwen), OPENROUTER_API_KEY, ...
```

Only masked OCR text is sent, never the image. For an OpenAI-compatible service such as Groq use
`AI_PROVIDERS=custom` with `AI_BASE_URL`, `AI_MODEL` and `AI_API_KEY`. `AI_BASE_URL`, `AI_MODEL`,
`AI_API_KEY` and `AI_EXTRA_BODY` always apply to the **first** provider in `AI_PROVIDERS`: clear them
when you switch the first provider, or they will be used for the new one. For a model on your own
machine use `AI_PROVIDERS=ollama` with `AI_MODEL=<model>`. Free API tiers may keep and use what you
send: use a paid key for real client documents.

Measure accuracy without a database: `python scripts\lite_benchmark.py --ai off` (public set) or
`--manifest ai/synthetic/output/tuning` (synthetic set).

## 8. Return to the legacy path (Docling + Redis/RQ)

```powershell
.venv\Scripts\python -m pip install -r requirements-docling.txt -r requirements-rq.txt
docker compose --profile rq up -d        # PostgreSQL + Redis + RQ worker container
```

and in `.env`: `QUEUE_BACKEND=rq`, `EXTRACTION_ENGINE=docling`, `REDIS_URL=redis://127.0.0.1:6379/0`.
Then run `python scripts\run_worker.py` and `python scripts\run_queue_supervisor.py` next to the API,
as before. Without `QUEUE_BACKEND=rq` those two scripts now stop with a message, on purpose.

## 9. Common errors

| Symptom | Cause and fix |
|---|---|
| `Configure a random AUTH_SECRET of at least 32 characters` | Set `AUTH_SECRET` in `.env` (the old `JWT_SECRET` name is not read). |
| API exits at startup with a schema error | Run `manage.py migrate` against an empty database, or rebuild the local database. |
| `connection refused` on port 5433 | `docker compose up -d db` was not run, or Docker Desktop is not started. |
| Browser shows "Request origin is not allowed" | Add the frontend URL to `FRONTEND_ORIGINS`. |
| Document stays `processing` after you restarted the API | A job left by a stopped API is released after `INLINE_STALE_LEASE_SECONDS` (90 s) and retried, but only with `INLINE_RECOVER_ON_START=true`; with `false` it waits for its 30-minute lease to expire. |
| First upload after loading demo data returns 503 | Run `python -c "import main; main.apply_compatibility_migrations()"` once (step 3). |
| Signed in but the session does not stick | `COOKIE_SECURE=true` on plain http: set it to `false` locally. |
| Validation says "Supplier matching requires review" (EX-008) | The supplier is not in this company's vendor list; add the vendor or accept the finding in review. |
| Accountant sign-in returns 503 | Accountants get an emailed code: configure Brevo or SMTP, or test with an administrator. |
| Frontend on port 3001 or 3002 | Fine: the API accepts 3000-3002; for other ports add them to `FRONTEND_ORIGINS`. |
| Spreadsheet upload fails with `UNSUPPORTED_EXTRACTION_FORMAT` | `.xlsx`/`.csv` are stored but not extracted by the lite engine. |
| `AI_UNAVAILABLE` / `AI_AUTH_REJECTED` on a document | Wrong or missing key, or the wrong first-provider settings; the document still goes to manual review. |
| `The queue supervisor is part of the legacy Redis/RQ setup` | You started the old worker or supervisor; stop it, or set `QUEUE_BACKEND=rq`. |
| Tests behave differently on your machine | Run them with `PYTHON_DOTENV_DISABLED=1` so your `.env` (AI keys, queue settings) does not leak in. |

## Browser tests without a sign-in code

Browser specs (`B4A_COOKIES`, `B4B_COOKIES`) need a signed-in session. On a local development database, write one for a synthetic demo user instead of pasting cookies:

```powershell
.venv\Scripts\python.exe services\admin\manage.py browser-session --out $env:TEMP\ai-accountant-cookies.json
# local admin: --email admin@trading.demo.test
```

It uses seed-demo-company's local-only guard, signs in only `*.demo.test` accounts, and writes the cookie values to the file only. The refresh cookie lasts the normal 8-hour session. Keep the file outside the repository.
