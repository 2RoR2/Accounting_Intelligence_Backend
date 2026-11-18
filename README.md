# Accounting Intelligence Frontend

Next.js App Router, React, strict TypeScript, Tailwind CSS v4, and Lucide. The frontend uses the FastAPI backend as its source of truth through the typed service layer in `src/lib/api`. It does not provide database security or replace backend authorization.

## Run

```sh
cd Accounting_Intelligence_Frontend
npm install
npm run dev
```

Open http://localhost:3000. On Windows PowerShell, use `npm.cmd` if script execution policy blocks `npm`.

The backend must be running on `http://127.0.0.1:8000` unless `BACKEND_URL` is set in `.env.local`. The Next.js proxy forwards browser `/api/*` requests to that backend.

## Sample accounts

At `/login`, enter a seeded company email and password. Users who require MFA stay on the login page while the six-digit OTP form is displayed inline; there is no separate `/verify-otp` route. A successful verification opens `/welcome` before the user continues to their role dashboard. There is no account or role dropdown: the entered email determines the workspace. Passwords are never saved in this frontend package. Real email OTP delivery is handled by the backend.

All development seed accounts use the password `DemoPass!2026`.

| Account | Company | Role |
| --- | --- | --- |
| `platform.demo@saic.example` | Platform | Super Admin |
| `reviewer.demo@saic.example` | Demo Company | Local Admin |
| `accountant.demo@saic.example` | Demo Company | Accountant |
| `ai.adminportal@gmail.com` | Demo Company | Accountant |
| `102784694@students.swinburne.edu.my` | ABC Sdn Bhd | Accountant |

## Walkthrough

1. Request company access from the landing page.
2. Log in as SAIC. Review the new request under Pending Approvals, approve it, and choose its seat limit.
3. Open its tenant → Users → View → Open activation. Activate the Local Admin with a password meeting the displayed checklist.
4. Sign in as that Local Admin. Invite an accountant from Users. Active users and pending invitations both reserve seats.
5. Open the accountant’s activation link and activate the account. Sign in as that accountant.
6. Upload a PDF/image/XLSX. The backend creates a processing job; the upload screen polls `GET /api/processing/jobs/{job_id}` and displays the real backend status. The browser does not advance document statuses locally.
7. Open the document. Save Total Amount as `848.00`. Save the reference `PO-2192` to confirm its low-confidence value. Enter a correction note and select Save & revalidate.
8. Open Validation, then Create standardised record. Open Standardised Record to review the immutable snapshot. Use the single Export action for the approved-record export and Export history to review previous exports.
9. Switch to Local Admin to see company documents, records, account usage, and reports.
10. Switch to SAIC to inspect monitoring, metadata, and performance. Enter Support View with a reason to access a read-only financial record; the event is audited.
11. Switch to Sarah at XYZ. ABC documents and records are filtered out.

Development data comes from the backend PostgreSQL seed in `Accounting_Intelligence_Backend/database/mock.data.sql`. Do not use live customer data in local development.

Uploaded documents are stored by the backend, not by the browser. When extraction detects a reliable invoice, bill, receipt, credit-note, or debit-note identifier, the document list updates its visible filename while preserving the original extension. Files that are queued, failed, or missing a reliable identifier retain their upload name until processing succeeds.

## Architecture and integration

- `src/types`: shared domain contracts.
- `src/lib/api`: authenticated FastAPI service functions consumed by UI features.
- `src/lib/permissions`: role and tenant access checks for the UI layer.
- `src/features/superadmin`, `localadmin`, and `users`: role-owned route composition and role-specific workflows.
- `src/features/common`: reusable dashboard, document, exception, record, monitoring, settings, hooks, and user-management workflows.
- `src/features/auth`: unauthenticated onboarding and account-access pages.
- `src/components/common`: reusable tables, dialogs, layouts, charts, and document preview components.
- `src/hooks/use-resource.ts`: loading, error, reload, and cross-tab refresh handling.

Frontend filtering shapes the UI only. Real tenant isolation is enforced by the backend API. Frontend route guards and role checks are convenience boundaries, not security boundaries. Authentication cookies, authorization, workflow transitions, and audit logging are backend responsibilities.

SAIC record APIs return metadata by default. Financial detail is retrieved through `openSupportView(reason)` and displayed without editing controls. Audit metadata hides extracted financial values for SAIC.

Dashboard counts and processing data are loaded through backend services. Preferences are saved as workspace settings. The upload flow uses backend job polling with an absolute two-minute observation deadline; each request has its own 15-second timeout, transient failures use bounded retry, and polling never cancels the backend job.

The queue worker is responsible for extraction status and persistence. `SUCCEEDED` must only be reported after extraction and required database writes commit. PDF extraction uses the configured Docling/parser runtime. Image extraction requires the backend image-OCR settings described in the backend README. Qwen/Ollama remains optional and environment-dependent.

## Validation

```sh
npm run typecheck
npm run lint
npm test
npm run build
```

`tests/workflows.test.ts` runs backend API smoke checks against `API_BASE_URL` (default `http://127.0.0.1:8000`) and skips cleanly when the backend is not running. Browser tests use installed Chrome with the current `playwright.config.ts` against a running app at `http://localhost:3000`. The active suite contains the current `tests/browser/columns.spec.ts` and `tests/browser/layout.spec.ts` checks. Set `PLAYWRIGHT_BASE_URL` to test another local port.

Run `npm run typecheck` and `npm run lint` before handoff. Backend integration tests require the backend test dependencies, PostgreSQL, Redis/RQ, and configured provider services; they are not replaced by frontend typechecking.

The interface uses navy `#061433`, blue `#022fa2`, white, and light blue `#eaf5ff`. Success indicators use blue; warning and error labels retain distinct text and semantic accents. Wide tables scroll within their panels and keep action columns visible.

## MFA recovery and benchmark APIs

On the privileged MFA screen, `Lost authenticator key?` opens a full-screen recovery dialog. The user retypes the existing sign-in password; the email already entered on the login form is reused. No backup-code or second email field is shown. The backend returns a new QR/manual TOTP key, and the user must verify a code from that new authenticator before login completes.

The typed service layer also exposes the benchmark APIs:

```ts
getDemoCatalog(user, datasetId)
getDemoAccuracyPreview(user, datasetId)
```

They call `/api/demo-datasets/{dataset_id}/catalog` and `/api/demo-datasets/{dataset_id}/accuracy-preview`. The accuracy UI must render `NOT_RUN` and null metrics without inventing percentages. The backend remains the authority for tenant access and demo registration.

## Styling

PostCSS is configured in `postcss.config.mjs` with the Tailwind CSS v4 plugin:

```js
const config = { plugins: { "@tailwindcss/postcss": {} } };
export default config;
```

Do not add a legacy `tailwindcss` PostCSS plugin configuration; use the installed `@tailwindcss/postcss` package.

## v3.1+ review and evaluation integration

Publish together with the backend v3.1+ integration. The review screen uses the
backend's AI Assist and validation-review endpoints, keeps source candidates apart
from model suggestions, and saves accepted values through the existing correction
API. Currency decisions retain human attribution. Report totals remain separated
by currency and preserve decimal values; job success is not field accuracy.

`public/evaluation/summary.json` is the historical 3 November 2026 development
snapshot used by the Evaluation page. It is not recomputed by a frontend build or
by new uploads. Its conditions, sample sizes, label limitations and unmeasured
criteria must remain visible; it does not establish independent pilot acceptance
or fresh measurements of the integrated branches. The accompanying backend PR
contains the coordinated API, dependency and database compatibility notes.
