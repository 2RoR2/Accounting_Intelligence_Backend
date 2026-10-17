# Accounting Intelligence Backend

The backend is a FastAPI service backed by PostgreSQL. It owns identity, authentication, MFA challenges, sessions, tenant authorization, document workflows, queue jobs, extraction persistence, validation, audit logging, and email delivery.

## Features

- Email and password authentication with Argon2id password hashing.
- Accountant MFA through a six-digit email OTP sent to the accountant's login email.
- TOTP authenticator enrollment and verification for privileged roles.
- Rotating access and refresh session cookies.
- Password activation and forced first-login password change.
- Password reset email flow with one-time reset grants.
- OTP controls: five attempts, invalidation after resend, immediate invalidation after successful use.
- Sign-in OTP expiry of 60 seconds with a 60-second resend interval.
- Password-reset OTP expiry of 10 minutes with a 60-second resend interval.
- Company registration and approval workflow.
- Tenant and role-aware workspace authorization.
- Administrative CLI commands for database setup, demo accounts, approvals, and MFA reset.
- Structured API errors and no password, OTP, or token values in responses or logs.
- Tenant-scoped document upload, review, correction, validation, approval, retry, and standardisation workflows.
- Queue-worker extraction with transactional persistence into `raw_extractions`, `field_predictions`, and `line_items`.
- Idempotent extraction persistence keyed by `service_run_id`.
- Direct client-controlled document status changes are disabled: `PATCH /api/documents/{document_id}/status` returns `405` after authentication. Clients must use dedicated workflow endpoints.
- Processing statuses are owned by trusted backend services and workers; frontend users cannot force `PROCESSING`, `EXTRACTED`, `FAILED`, `VALIDATED`, or `COMPLETED`.

## Project structure

```text
Accounting_Intelligence_Backend/
+-- app.py                    # FastAPI application and API routes
+-- manage.py                 # Administrative CLI
+-- requirements.txt          # Python dependencies
+-- .env.example              # Safe environment template
+-- auth/
¦   +-- security.py           # Password hashing, JWT, OTP hashing, TOTP helpers
¦   +-- __init__.py
+-- database/
¦   +-- connection.py         # PostgreSQL connection helper
¦   +-- database/PostgreSQL Database schema.sql # Complete database schema
¦   +-- migrations/            # Ordered database migrations
+-- notifications/
¦   +-- mailer.py             # SMTP OTP and reset email delivery
+-- validation/               # Request and domain validation helpers
+-- ai/                       # Backend AI-related services
+-- tests/                    # Unit and integration tests
```

## Run locally

From the monorepo root:

```powershell
& "C:\Program Files\PostgreSQL\18\pgAdmin 4\python\python.exe" -c "import sys;sys.path.insert(0,'Accounting_Intelligence_Backend/.venv/test-deps');sys.path.insert(0,'Accounting_Intelligence_Backend');from uvicorn import Config,Server;Server(Config('app:app',host='127.0.0.1',port=8000)).run()"
```

- Health check: `http://127.0.0.1:8000/api/health`
- OpenAPI documentation: `http://127.0.0.1:8000/docs`
- The root `/` route is intentionally not defined.

## Database setup

```powershell
cd Accounting_Intelligence_Backend
$env:PYTHONPATH=(Get-Location).Path
& ".venv/bin/python.exe" manage.py migrate
```

## Environment

Configure `DATABASE_URL`, `AUTH_SECRET`, `FRONTEND_ORIGINS`, cookie settings, and SMTP settings in `.env`. For the deployed frontend, use `FRONTEND_ORIGINS=https://accounting-intelligence-frontend.vercel.app` (add comma-separated custom domains when needed). Gmail SMTP requires an App Password:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=ai.adminportal@gmail.com
SMTP_FROM=ai.adminportal@gmail.com
SMTP_PASSWORD=your_app_password
```

Never commit `.env` or expose SMTP credentials.

## Useful commands

```powershell
python manage.py migrate
python manage.py seed-demo-accounts
python manage.py create-admin
python manage.py requests
python manage.py approve-company REQUEST_UUID
python manage.py reset-mfa --email user@example.com
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

The queue test suite supports isolated offline resolver/parser hooks. Those tests may use opaque synthetic document identifiers and intentionally skip PostgreSQL persistence. Production jobs resolve UUID document IDs from PostgreSQL and persist extraction results transactionally.

The complete Qwen/Ollama provider path is not currently claimed as operational. Live PostgreSQL, Redis/RQ worker, provider execution, validation persistence, retry, and end-to-end upload tests require the configured services and dependencies.






