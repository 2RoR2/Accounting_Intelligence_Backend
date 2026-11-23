# Deploy hosted UAT

Use the approved backend and frontend commits from `features`. Use a dedicated UAT database, object
bucket and tester accounts. Keep connection strings, keys, passwords and sign-in codes in the
service dashboards or the team's secret store.

## 1. Rotate credentials and prepare Neon

Before going live, replace and revoke the credentials that were previously exposed, including the
Neon password, S3 access keys, Brevo key, Gmail app password, `AUTH_SECRET` and any legacy
`JWT_SECRET`, and AI provider keys. `AUTH_SECRET` signs sessions and encrypts authenticator secrets.
Rotating it signs users out and requires re-enrollment for accounts with existing TOTP secrets.

1. In Neon, open **Branches → Create branch**. Name it `uat` and choose an approved parent with
   only synthetic UAT data or an empty database. Record the parent and the starting point for rollback.
2. Open **Connect** on `uat`. Select the UAT database and a role that can apply schema migrations.
   Use the direct connection with TLS. Verify the selected branch again before using its URL as
   `DATABASE_URL`. [Neon branch management](https://neon.com/docs/manage/branches)

## 2. Configure Brevo, Render and Vercel

1. In Brevo, open **Settings → Senders, Domains, IPs → Senders** and add the UAT sender. Verify the
   sender or authenticate its domain. Set `BREVO_API_KEY` and `BREVO_FROM_EMAIL` in Render.
   The Blueprint selects Brevo and supplies `BREVO_FROM_NAME`. [Brevo sender setup](https://help.brevo.com/hc/en-us/articles/208836149-Create-a-new-sender-From-name-and-From-email)
2. In Render, choose **New → Blueprint**, select the backend repository and the approved branch
   containing `render.yaml`, and review the single Python web service. The Blueprint selects Starter
   for pre-deploy command support. The build installs the slim runtime and checks the packaged OCR
   weights. The pre-deploy command runs `python services/admin/manage.py migrate` before Uvicorn
   starts. Startup checks schema compatibility and `/api/health` checks database access. On a plan
   without pre-deploy support, remove `preDeployCommand` in the Blueprint and use
   `python services/admin/manage.py migrate && exec uvicorn main:app --host 0.0.0.0 --port $PORT`
   as `startCommand`. [Render deploy commands](https://render.com/docs/deploys)
3. Add the `uat` connection as `DATABASE_URL` and a fresh random `AUTH_SECRET` of at least 32
   characters. `JWT_SECRET` is reserved for legacy setup and may be left unset. Supply `S3_BUCKET`
   for a private UAT bucket and `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for a role or key
   scoped to that bucket. Set `S3_ENDPOINT_URL` for an S3-compatible provider and confirm `AWS_REGION`
   matches the bucket. Use one endpoint setting. The bucket keeps document files available across
   deploys. In an existing Blueprint, add new `sync: false` entries manually in the Render dashboard.
   [Render Blueprint environment variables](https://render.com/docs/blueprint-spec#prompting-for-secret-values)
4. Set Render `FRONTEND_ORIGINS` to the exact HTTPS origins of the Vercel UAT frontend and the Render
   service, separated by a comma with no paths. Start with the Vercel origin if the Render URL has
   yet to be assigned, then add the Render origin and redeploy. Use a stable Vercel UAT alias if
   deployment URLs change. Set `FRONTEND_ORIGIN` to the Vercel UAT origin if using the admin CLI to
   generate activation links.
5. In the frontend's Vercel project, set `BACKEND_URL` to the Render HTTPS origin for the environment
   serving UAT. Deploy the approved frontend commit with that setting. Confirm the Render service is
   live and `GET /api/health` returns HTTP 200 with `status` equal to `ok`.
   [Vercel environment variables](https://vercel.com/docs/environment-variables)

## 3. Initialize the UAT database

Render runs the initial `migrate` on `uat` during the first deploy and checks for pending migrations
on later deploys. If initializing it manually before the first deploy, use the backend repository
root in a private operator terminal with the runtime dependencies installed and the verified `uat`
connection in that terminal's `DATABASE_URL`. Run `python services/admin/manage.py migrate` and
check for `Schema prerequisites verified.`

The intended one-time fixture order on `uat` is `migrate`, `seed-knt`, then `tally`. **Hosted KNT
initialization is blocked in the current code.** This backend revision has no KNT commands. The
Plan M5 implementation provides `seed-knt`, `tally` and `reset-knt` behind a shared guard that
requires `APP_ENV=development`, a loopback PostgreSQL connection and local document storage. Neon
is a hosted database. Keep the guard enabled and use an approved hosted fixture provisioning path
before treating the KNT tally and expected dashboard figures as UAT evidence.

For a local rehearsal after M5 is integrated, use the backend repository root with a disposable
loopback database, `APP_ENV=development` and an empty `S3_BUCKET`:

```sh
python services/admin/manage.py migrate
python services/admin/manage.py seed-knt --prompt-password
python services/admin/manage.py tally
```

Use a workspace provisioned for UAT and an accountant account with an inbox the tester can access.
The operator can create an accountant in an existing active workspace from the backend repository
root using the following command and set a password at the prompt:

```sh
python services/admin/manage.py create-account --email "<tester-email>" --name "<tester-name>" --tenant "<workspace-name>" --role accountant
```

Use a workspace admin for actions requiring approval or posting privileges. The `browser-session`
test helper is a local-development CLI for synthetic accounts. The M5 `reset-knt` command is a
local-only CLI action.

## 4. Smoke-test the hosted stack

- [ ] Sign in from the Vercel UAT URL with the accountant account, receive the Brevo code and
  complete sign-in. Refresh the page and confirm the session persists. Check admin sign-in with TOTP.
- [ ] Compare the dashboard figures for the selected company and period with the approved fixture
  totals. Record the period and expected and actual figures.
- [ ] Upload three synthetic documents, such as a digital PDF, a scanned PDF and a receipt image.
  Open each stored preview and check its extraction status. With AI off, choose the kind and enter
  missing fields manually as needed.
- [ ] Submit a document for review, approve it and post it. Verify its state and the resulting journal.
- [ ] Issue a sales invoice, download its PDF and check the number, customer and totals.
- [ ] Import or open the UAT bank statement, confirm one match and check the resulting reconciliation.
- [ ] Download and open a month pack. Confirm it covers the selected workspace and period.

In the UAT run record, copy the full backend commit SHA and deployment ID from the live Render
deploy and the full frontend commit SHA and deployment ID from the matching Vercel deployment.
Record UTC time, the Neon branch, migration and tally outcomes, and the smoke-test results.

## 5. Roll back

1. Pause automatic deploys. If the database schema is compatible with the previous backend, use
   **Manual Deploy → Deploy a specific commit** in Render and select the last known-good backend
   SHA. Redeploy the corresponding Vercel commit to the UAT environment if the frontend changed.
   Check `/api/health` and rerun the affected smoke tests. [Render deploys](https://render.com/docs/deploys)
   [Vercel Git deployments](https://vercel.com/docs/git)
2. If the database also needs a reset, pause UAT traffic and the Render service before touching the
   branch. Preserve the current UAT state if needed. In Neon, select `uat` and use **Reset from
   parent** only if the parent's current state is the intended baseline. For a specific earlier
   state, use **Restore** and choose the recorded point within the restore window. A reset discards
   later UAT database changes and leaves the separate S3 bucket intact. Check that the restored rows
   and object files still agree. Confirm Render still points to `uat`, then redeploy the approved
   commits so the migration runs before the API resumes. Record the resulting SHAs and rerun the
   smoke tests. [Neon reset and restore](https://neon.com/docs/manage/branches)
