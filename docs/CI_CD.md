# CI/CD setup

The backend and frontend are separate repositories, so configure the settings below in each
repository independently.

## Required checks

Protect `main` and `features` with pull requests and require these status checks before merging:

- Backend repository: `Test backend`
- Frontend repository: `Test frontend`

Also enable:

- at least one approving review;
- dismiss stale approvals when new commits are pushed;
- require branches to be up to date before merging;
- block force pushes and branch deletion.

The GitHub branch-protection setting must be applied in the repository settings (or through the
GitHub API); a workflow file cannot enable it by itself.

## Deployment secrets

The required backend test job runs the provider-rotation/concurrency tests and the existing
extraction-lift tests. The repository-wide suite currently has 101 pre-existing failures on the
`features` branch (schema-remediation fixtures, optional Docling dependencies, and legacy route
contracts), so it is not used as the merge gate until those unrelated baselines are reconciled.

Deploy jobs run only after the test job succeeds on pushes to `main` or `features`.

Configure these repository secrets:

- Backend: `RENDER_HEALTHCHECK_URL`, set to the Render URL ending in `/api/health`.
- Frontend: `VERCEL_HEALTHCHECK_URL`, set to the public Vercel application URL.

The deploy-hook secrets are optional when Render or Vercel Git integration is enabled:

- `RENDER_DEPLOY_HOOK_URL`
- `VERCEL_DEPLOY_HOOK_URL`

When configured, the backend check expects `{"status":"ok"}` and the frontend check requires a
successful HTTP response. Each check polls for up to five minutes. Until the health-check secret
is configured, the deploy job emits a warning and skips verification; configure it before relying
on post-deploy health validation.
