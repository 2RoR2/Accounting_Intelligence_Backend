# Features integration preflight — 19 October 2026

**NO-GO for production integration.** Local D4-A passed; latest team bank work and deployed contracts/configuration still need a controlled integration review. No features merge, push, deployment, database initialization or shared-data mutation performed.

## Verified local acceptance
- Chrome Playwright: 20/20 passed; npm tests: 15/15 passed; typecheck/lint exit 0. Recovered production build session 97552: exit 0, 40/40 pages; no repeat browser suite/build.
- Six real PDF workflows, EX-001–EX-008, bill/receipt coverage states, correction/reload/original retention/stale 409, mobile and private source-byte delivery passed. Zero page errors/unexpected console messages; known development warnings recorded separately.
- 71 screenshots and sanitized API evidence remain ignored in Frontend_V2 `test-results/d4a/2026-10-19T12-46-30-864Z/`. Native PDF screenshot is blank: authenticated HTTP 200/hash/popup verified, visual native rendering unverified.
- Inspected `runs/d4a-after.py` and helpers before executing: SELECTs, Redis PING and local report writes only. Exit 0: all 68 protected document datasets unchanged, 20 D4-A synthetic documents, 0 approvals and 0 standardised records in D4-A tenants. Report: `runs/d4a/post-browser-database-check.json`.
- Final health refresh: frontend 3000 and backend 8002 `/api/health` both HTTP 200. No production authentication or bank mutation tested.

## Exact branch comparison
- Backend V2 reviewed at `e9d100b6ac1482558fddb48d0389aeb7c031061a`. Cached `origin/features` is stale at `4c4a030d91a9dd12441fe3a552f360788dd7655b`; do not treat it as the current integration target.
- Read-only remote lookup/GitHub comparison: latest backend features `2e63075e6aa6515cd1039097b62077241b50d3b4`, diverged with 2 features-only and 5 V2-only commits. No fetch/pull/merge was performed.
- New team changes add `POST /api/bank-reconciliation`, `validation/bank_reconciliation.py`, `validation/bank_matching.py` and their tests. **V2 lacks that route/modules.** A replacement/file overwrite would lose teammate functionality. Merge conflict status remains unverified.
- Frontend features `a21cc50b806bc3d0e91fda0f3849298296accf3b`; V2 reviewed at `d52ba9587e6aeff74b2504c150a0d0503c138cd0`, 3 V2-only commits, no features-only commits at inspection.
- Frontend auth UI/service/middleware, dependency manifests, next.config.mjs and vercel.json unchanged against features. Backend 16 auth handlers and origin middleware AST-identical to latest features; auth/security and consolidated schema unchanged. This does not prove production users/sessions/deployment compatibility.

## Teammate origin 403
- `app.py:131` checks every non-GET/HEAD/OPTIONS request before the route: `X-Requested-With` must equal `AccountingIntelligence`; any supplied Origin must exactly match `FRONTEND_ORIGINS` (legacy `FRONTEND_ORIGIN`). Fallback permits localhost:3000, not 127.0.0.1:3000. CORS defaults are broader than this mutation guard.
- Exact unchanged middleware tested offline with a stub response: 6/6 expected outcomes, including allowed localhost, configured loopback, missing header, mismatched host/port and unapproved origin. No live request was sent to teammate port 8000; actual failing headers/environment remain unknown.
- Existing frontend API client supplies the custom header and same-origin credentials. Page Origin, not the backend destination port alone, determines allowlist matching. No evidence V2 introduced the reported guard failure.
- Smallest investigation: owning teammate compares failing browser Origin/custom header with the environment of the actual port-8000 process. Add only the explicit approved frontend origin if missing, or use the existing API client header. Keep origin protection; no wildcard workaround. Do not redirect bank requests to V2: its route is absent.

## Deployment contracts and environment
- Same paths do not guarantee compatibility: V2 extraction supplies classification, evidence/raw/normalized/current values, decimal strings/nulls and contentRevision; PATCH corrections require expectedContentRevision; validation reports carry real checkStatus/coverage/review semantics. Latest features backend lacks this verified V2 behavior. Actual deployed backend version/DTO/schema is **UNVERIFIED**.
- Database schema files are unchanged, but production catalogue/review-revision compatibility is unverified. Existing app startup executes an additive review_submissions ALTER TABLE; do not start it against production to inspect compatibility. No production schema operation authorized/performed.
- Preserve existing users/tenant/role/session data and stable AUTH_SECRET (JWT/TOTP encryption key). Verify secure HTTPS cookies, same-origin proxy, explicit approved origins, role access and MFA before rollout. Never copy local synthetic identities or secrets into production.
- Frontend requires server-side BACKEND_URL in Vercel; optional NEXT_PUBLIC_API_URL is also used by the existing absolute XLSX download helper in `src/lib/api/workflow.service.ts`. The browser's normal API calls use relative URLs. Review export URL/auth behavior separately before production approval.
- Relevant backend names: DATABASE_URL, AUTH_SECRET, COOKIE_SECURE, FRONTEND_ORIGINS (or legacy FRONTEND_ORIGIN), REDIS_URL, DOCUMENT_STORAGE_PATH, DOCLING_MODELS_DIR; preserve existing notification-provider configuration. Frontend demo flag: NEXT_PUBLIC_DEMO_DIGITAL_PDF_ONLY. NEXT_BUILD_DIR is a local build-output setting. Values/secrets were not printed.
- next-env.d.ts/tsconfig.json have no pending edits but committed custom .next-d22-build and other local type paths. Custom-dir build passed; a clean default-output CI build/typecheck remains unverified. No speculative config edits made.
- CPU Docling/worker/model/private-storage runtime must exist on the deployed backend. Laptop 127.0.0.1:8002 is unreachable from Vercel; a publicly reachable HTTPS backend is required there.

## Vercel evidence and smallest next action
- vercel.json specifies only Next.js. No accessible linked-project settings/production branch/environment inventory. A successful Vercel Git status exists for frontend features SHA a21cc50 (Rachel's project), but **production branch, deployed backend URL/version and preview configuration remain UNVERIFIED**.
- Vercel supports configurable production branches and separate preview environments ([Git deployments](https://vercel.com/docs/git), [environment variables](https://vercel.com/docs/environment-variables)); this project's readiness/permissions were not verified. No preview or production deployment was created.
- Next: Rachel/Jonathan/Angeline review latest features bank commits plus V2 contract diffs; Aina reviews bank/validation regressions, Aleeya production schema compatibility. Prepare an isolated candidate integration preserving both histories only after explicit merge authorization. No merge into features now.
- First prove a separate HTTPS preview backend/worker/private storage and isolated database, exact preview BACKEND_URL/origin/cookie settings, stable auth key policy, clean default build, login/TOTP/roles, bank API/page, upload/validation/corrections and exports. Then request approval with backend-before-frontend rollout and previous backend image/frontend deployment rollback; incompatible frontend must not run against the old backend. No rollback SQL/reset proposed.
- Working local demo remains usable; Qwen D3 is independently blocked as documented in D3_FRONTEND_HANDOFF.md. Do not start it as part of this preflight.
