# Team demo data plan (draft for approval)

Status: **Phase 2 built and proven locally (24 Nov); hosted uat not touched.** The plan below is v2 (Jonathan's
answers of 24 Nov folded in, see §6). Commands and the hosted runbook are in [COMPANY_PACKS.md](COMPANY_PACKS.md#team-demo-companies-tkk-ehb-cda-and-team-work);
the per-role run-through is [demo/TEAM_TEST_SCRIPT.md](demo/TEAM_TEST_SCRIPT.md).

Where the build differs from the plan text, deliberately:
- **Opening position.** Each company opens with an opening-funds journal (bank, cash, share capital), as KNT does.
  There are no imported opening AR/AP items; open items come from the Sep–Nov documents instead.
- **Bank layouts.** Two layouts are used: signed amount (TKK, CDA) and Debit/Credit columns (EHB). The third
  `company_pack` layout needs saved column mappings first.
- **Catalogue size.** TKK has 11 items, EHB 13 and CDA 8. Every item appears on a document, which the tally checks.
- **How the team documents are made.** They are created by each company's two fixture logins through the app's own
  submit, approve and post steps, then assigned with the app's assignment function. Submitted documents therefore
  name the fixture accountant, so a teammate who is an admin can approve them.
- **Tally after the team Inbox.** The tally reads each team document's printed values from its read record. It
  counts the posted ones in P&L and AP, and requires every document to be in its planned lane.
Written 24 Nov 2026 on branch `claude/team-demo-data-31ccd9`. The base is backend `features` at 5ca59c3.

Goal: every teammate (Rachel, Angeline, Aina, Aleeya, Jonathan) can use and test every part of AccordAI with enough
realistic, fictional data. Today only Jonathan can do this, with KNT. The SAIC accountants (pai@, rabia@, brian@)
keep their KNT accountant access.

Owner's rules apply throughout:
- Tax is typed as printed, never calculated.
- All data is fictional.
- No secrets in the repository, logs or messages.
- Any hosted write needs Jonathan's explicit approval and a Neon snapshot branch first.
- Bulk data goes in as already-read records, so no AI quota is spent.

---

## 1. Permission model (from the code)

Line numbers refer to `features` 5ca59c3.

### 1.1 How roles and tenants work

| Fact | Evidence |
|---|---|
| A user row has one `tenant_id`. `super_admin` must have none; `local_admin` and `accountant` must have one. | `database/PostgreSQL Database schema.sql:125-146` |
| Email is unique across the whole platform. | `schema.sql:147` (`uq_users_email ON users (lower(email))`) |
| There is no membership, company-switch or user-tenant table. | grep for `membership`, `user_tenants` or `switch tenant`: no hits |
| Every request loads the user from the session and requires `status='active'` and an active company. Disabling a user or suspending a company takes effect on the next request. | `main.py:291-292` (`active`), `main.py:305-318` (`session_user`) |
| Role gate for every route. | `main.py:647-651` (`require_workspace_user`), `services/routes/erp/common.py:17-25` (`erp_user`: company workspace required, `local_admin`/`accountant`) |
| Tenant scope: `super_admin` gets `None` (platform-wide), everyone else gets their own `tenant_id`. | `main.py:664-665` (`tenant_scope`) |
| Local admins and super admins sign in with a password plus TOTP (enrolment happens at the first sign-in when no secret is stored). Accountants sign in with a password plus an emailed code. | `main.py:169` (`PRIVILEGED_ROLES`), `services/routes/security/routes.py:16-40` |

**Tenant isolation is enforced in the application, not in the database.** Each query adds
`tenant_id = <session user's tenant>`. There is no Postgres row-level security. A missed filter would leak data
across companies, so Phase 2 adds a cross-company isolation test (§4.6).

### 1.2 What a LOCAL_ADMIN can do to accountants in their company

| Action | Possible? | Evidence and notes |
|---|---|---|
| Invite (add) an accountant | Yes. It creates `status='invited'` with no password, expiring in 7 days, and it counts against the seat limit (active plus invited). | `services/routes/admin/routes.py:65-77` |
| Send an activation link | **No.** No token is created and no email is sent (see gap G1). | same; `activate` at `security/routes.py:359-373` needs an `auth_tokens` row that only the CLI `approve-company` creates (`services/admin/manage.py:181`) |
| Resend the invitation | Only refreshes the dates. Still no token or email. | `admin/routes.py:102-109` |
| Activate, deactivate or re-invite | Yes. `PATCH /api/users/{id}/status` accepts `active`, `disabled` or `invited`, for accountants of their own company only. | `admin/routes.py:79-85`, `main.py:403-404` |
| Reset access | Partly. The admin can type a new password for the accountant (`must_change_password=true`, sessions revoked). The accountant can also use **Forgot password**, but only while their status is `active`. | `admin/routes.py:87-100`; `security/routes.py:220-241`, `:272-285` |
| Rename an accountant | Yes, through the same credentials endpoint. | `admin/routes.py:87-100` |
| Change role (accountant ↔ local admin) | **No endpoint.** All management queries are restricted to `role='accountant'`. | `admin/routes.py:82`, `:90`, `:106` |
| Manage other local admins | **No.** Only the platform admin creates a local admin, through company-request approval. | `admin/routes.py:133-144` |
| Assign or reassign documents and workload | Yes, and only local admins can. The assignee must be an active user of the same company. It writes `documents.assigned_to`, a `workload_assignments` row, an audit row and a notification. | `documents/routes.py:1400-1450` (`_assign_documents`), `:1460-1461` (`assign_workload`), `:1556-1559` (bulk assign) |
| Grant granular permissions | Stored but **never enforced** (gap G5). | `admin/routes.py:156-169`; nothing reads `user_permissions` or `role_permissions` |
| Request more seats | Yes. The platform admin approves. | `admin/routes.py:38-56` |

### 1.3 What an ACCOUNTANT can see

- **Inbox (received documents): only documents they own or are assigned.** The filter
  `owner_id = me OR assigned_to = me` appears in:
  - the list: `documents/routes.py:162-163`;
  - detail and actions: `main.py:697`, `main.py:802`, `documents/routes.py:293`, `:335`, `:718`, `:854`, `:910`, `:1081`;
  - exceptions and queue: `queue/routes.py:69`, `:98`;
  - review lanes: `review/lanes.py:34-38`;
  - search: `search/routes.py:98`;
  - trace: `trace/routes.py:70`.

  The one exception is documents of an enabled benchmark-demo tenant, which are shown to all its accountants
  (`documents/routes.py:163`).
- **Everything else is company-wide.** This covers the ledger and trial balance (`accounting/routes.py:93`, `:726`,
  `:946`), typed Sales and Purchases (`erp/documents.py:33-34`), contacts (`erp/contacts.py:28-29`), products
  (`erp/catalog.py:126`), bank (`bank/routes.py:390`, `:412`, `:829`), reports (`reporting/routes.py:8`) and the user
  list (`admin/routes.py:58-63`).
- Therefore **Inbox work must be assigned per accountant**, or a teammate's Today and Documents pages are empty. This
  is why 14 KNT documents had to be reassigned to Jonathan on uat.

### 1.4 Who can approve, post, match and lock

| Action | Accountant | Local admin | Evidence |
|---|---|---|---|
| Submit an Inbox document for approval | ✔ (own or assigned only) | ✘ | `documents/routes.py:1217-1236` |
| Approve or return an Inbox document | ✘ | ✔ | `documents/routes.py:1245-1246`, `:1328-1329` |
| Post journals, bills, invoices, payments, adjustments; reverse a journal | ✘ | ✔ | `accounting/routes.py:168`, `:244`, `:428`, `:587`, `:614`, `:638` |
| Post scanned records; set posting accounts | ✘ | ✔ | `erp/documents.py:35`, `:1210`, `:1217`, `:1295` |
| Make a typed Sales or Purchases document ready (post it) | ✘, except money-in/out with bank evidence | ✔ | `erp/documents.py:621-624` |
| Create, edit or send typed documents; contacts; products | ✔ | ✔ | `erp/documents.py:34`, `erp/contacts.py:29`, `erp/catalog.py:191-204` |
| Chart of accounts, cost centres, tax codes, payment terms, CSV imports | ✘ | ✔ | `accounting/routes.py:1058-1110`, `erp/catalog.py:25`, `imports/routes.py:21` |
| Import a bank statement; match, ignore or code a line; record an expense | ✔ | ✔ | `bank/routes.py:519`, `:558`, `:733`, `:777`, `:864`, `:1053`, `:1069` |
| Post the payment behind a bank match | ✘ (confirm only) | ✔ | `bank/routes.py:765`, `:876-877` |
| Complete a bank reconciliation period / reopen it | ✔ / ✘ | ✔ / ✔ | `bank/routes.py:148`, `:162` |
| Create, close or reopen an accounting period (lock) | ✘ | ✔ | `accounting/routes.py:117`, `:153`, `:159` |
| Allocations and settlements | ✔ | ✔ | `accounting/routes.py:665`, `:680`, `:976`, `:1009` |

The platform admin (`super_admin`) has no company. ERP routes return 403 for them (`erp/common.py:21-23`). They
approve companies, seat limits and MFA recovery.

### 1.5 Gaps and bugs found in these flows

| # | Gap | Effect on this plan |
|---|---|---|
| G1 | **An invitation has no activation link.** `invite_user` creates an `invited` user with no token and no email. Forgot password refuses non-active users (`security/routes.py:226-229`). An invited accountant therefore can never sign in. | Workaround, already possible in the UI: the admin sets the user to **Active**, then the person uses **Forgot password** to set their own password. A proper fix (send a token-based activation email) is a separate PR. |
| G2 | **Approving a company request in the web app has the same dead end**: the invited local admin has no token (`admin/routes.py:139-143`). Only the CLI path prints a link (`manage.py:181-184`). | New companies' local admins are created by the seed command as `active` with no password, then use Forgot password (§4.4). |
| G3 | **There is no role change and no way for a company admin to add another local admin.** | Roles are set by the seed command, never changed afterwards. |
| G4 | **One email is one account in one company** (`schema.sql:147`, single `tenant_id`). | Each teammate needs a separate login per company (§2.3). |
| G5 | **Granular permissions are saved but not enforced.** Toggles on the permissions screen change nothing. | Not used by the demo data. Reported to the audit session. |
| G6 | **Disabling an accountant leaves their assigned documents hidden from every other accountant** until an admin reassigns them. | The seed never disables anyone. Mentioned in the tester notes. |
| G7 | Within one company, an accountant can bulk-tag documents they cannot see (`documents/routes.py:1561-1570` checks only the tenant) and can "save draft" on any document of the company (`:1195-1201`). Both are minor. | Reported to the audit session (`docs/ACCOUNTANT_FUNCTION_AUDIT.md`), not fixed here. |
| G8 | The admin can type a password for an accountant (`admin/routes.py:93-97`). | Not used. Passwords are never typed or committed by us. |

---

## 2. Company and user layout

### 2.1 Companies (all fictional)

| Code | Company | Trade | Place | Bank (fictional) | Printed tax |
|---|---|---|---|---|---|
| KNT | Ketapang Nibong Trading Sdn. Bhd. (exists) | building-material trading | Kuching, Sarawak | Public Bank ••9901 | SST 10% / 5% on goods, 8% on services |
| TKK | Teratai Kopi & Katering Sdn. Bhd. | café and corporate catering | Kuching, Sarawak | Maybank Current Account ••4402 | 6% service tax on F&B |
| EHB | Enggang Hardware & Building Supply Sdn. Bhd. | hardware, construction materials, equipment rental | Miri, Sarawak | RHB Current Account ••7715 | SST 10% on goods, 8% on rental |
| CDA | Cendana Advisory Sdn. Bhd. | accounting and business advisory (professional services) | Kota Kinabalu, Sabah | CIMB Current Account ••3360 | 8% service tax |

Names follow the KNT style: a Malaysian tree or bird name and a "Contoh" street address. The SSM numbers, TINs and
SST numbers are invented in KNT's fictional ranges. Emails end in `.test`, and every rendered page prints
"SYNTHETIC SAMPLE".

**Banks (decided 24 Nov):** each company names a real Malaysian bank in its bank-account label and statement file
name, as KNT does with Public Bank. Only the account numbers are invented. The statement CSVs keep the generic
layouts from `company_pack.py`. We do not copy any bank's logo, letterhead or statement design, so nothing could pass
for a real bank record.

### 2.2 Who holds which role

Each teammate gets at least one **local admin** login, so they can test approving, posting, locking and user
management, and is an **accountant** in at least two companies.

**Maker-checker rule (v2):** within one company, nobody both prepares and approves. Accountants prepare and submit;
local admins approve, post and lock periods. That is how the code already separates the roles (§1.4). v1 had Aleeya as
both an accountant and an admin of CDA, which broke the rule, so v2 moves her admin seat to EHB.

| Person | KNT | TKK (Kuching café) | EHB (Miri hardware) | CDA (KK advisory) |
|---|---|---|---|---|
| Jonathan | accountant (exists) + "Jonathan Company Admin" (exists) | accountant | accountant | — |
| Rachel | accountant (exists) | **local admin** | accountant | accountant |
| Angeline | accountant (exists) | accountant | **local admin** | — |
| Aina | accountant (exists) | — | accountant | **local admin** |
| Aleeya | accountant (exists) | accountant | **second local admin** | accountant |
| SAIC pai@/rabia@/brian@ | accountant (exist), Inbox set of their own | — | — | — |
| demo@accordai.local | local admin (exists) | — | — | — |

| Company | Admins | Accountants |
|---|---|---|
| TKK | 1 | 3 |
| EHB | 2 | 3 |
| CDA | 1 | 2 |

Seat limit 10 per company; KNT is unchanged. KNT is the one exception to the rule: Jonathan already has both an
accountant and an admin login there. In KNT demos, his admin login approves only what *other* accountants submitted.

### 2.3 One email per company: plus-addresses

The code allows one account per email, and that account belongs to one company (G4). Adding multi-company membership
would be a schema and auth change, out of scope. The plan uses **plus-addressing** instead, for example
`name+tkk@gmail.com`:
- The app treats it as a different unique email.
- The sign-in code and the Forgot-password code still arrive in the person's normal inbox.
- No schema change is needed.

Each teammate's existing KNT account stays as it is.

Real addresses never enter the repository. The pack defines **slots** such as `tkk.admin`, `tkk.accountant.1` and
`cda.admin.2`. A private roster outside the repository (same idea as `UAT_TEST_EMAILS`) maps slots to emails, and
the command prints emails masked (`r***@g***.com`), as `test_users.py:61-68` already does.

---

## 3. Data set per company

The new companies use the same shape as KNT, scaled to the trade. KNT keeps its books and gets only the per-accountant
Inbox split (§3.2).

### 3.1 Books (Sep–Nov 2026, cut-off 23 Nov, like KNT)

- **Opening balances at 1 Sep:** a trial balance that balances, with open AR and AP items behind the control
  accounts and a bank opening balance. These come in through the existing CSV imports (opening balances, chart of
  accounts, contacts), as `demo_company.py` does.
- **Chart of accounts:** the Malaysian SME chart used by KNT, plus trade accounts:
  - TKK: food cost, beverage cost, catering income, kitchen wages, rental;
  - EHB: rental income, equipment hire, delivery charges, stock;
  - CDA: fee income (advisory, tax, secretarial), disbursements recoverable, professional indemnity, staff claims.
- **Contacts and products:** about 15–20 customers and 10–15 suppliers, with SSM, TIN, SST number, terms and a
  default account (`fields`, as in `knt_extra.py`). Products and services:
  - TKK: about 20 menu and catering packages plus ingredients;
  - EHB: about 35 SKUs plus rental items;
  - CDA: about 12 service lines (retainer, tax filing, audit support…).
- **Typed Sales flow** through the Sales endpoints:
  - quotations → sales orders → delivery orders → invoices (EHB, and TKK catering);
  - retainer invoices (CDA);
  - 2 credit notes and 1–2 debit notes each, linked to their invoice;
  - open items in several ageing buckets (current, 1–30, 31–60, over 60 days).
- **Purchases:** typed supplier bills, plus Inbox-captured bills (§3.2), with payments made.
- **Payments and vouchers:** payments received and made, some part-paid and one combined (one transfer for two
  invoices). Cash in hand for counter takings (TKK).
- **Bank:** one statement per month in a different layout per company, using the three layouts `company_pack.py`
  already writes. Every line is either matched or deliberately left as a story line: fee, interest, payroll, an
  unidentified deposit, a card settlement. The statements must reconcile month by month.
- **Periods:** September closed (locked), October and November open, so teammates can test closing a period.

### 3.2 Inbox per accountant (stored as already-read records, no AI)

Created with the same method as `knt_seed.capture_purchase` (`services/admin/knt_seed.py:232-290`):
- the stored file;
- a `raw_extractions` row with mode `*_gold_fixture`;
- field predictions and a line typed from the gold values;
- the app's own `validate_document`.

Each document is then **assigned through `_assign_documents` acting as the company's local admin**, so the workload
row, audit row and notification exist exactly as in real use.

Per accountant, per company (about 12 documents each, so about 140 Inbox documents in total):

| Lane | Count | How the state is reached |
|---|---|---|
| To review | 3 | Extracted. One has a "confirm delivery" check that needs review. |
| Ready | 3 | Extracted, all checks pass, known supplier. |
| Waiting approval | 2 | Submitted by that accountant through the app (`/review`), waiting for the local admin. |
| Needs fixing / Exceptions | 3 | Open exception: **unknown supplier**, **duplicate invoice number** (same number and supplier as an earlier bill), **missing invoice number** (unreadable). |
| Done | 1–2 | Approved and posted, so trace, search and records have history. |

Each set also contains one supplier **credit note** and one **debit note** linked to a bill. The numbers are unique
per accountant, so duplicates only appear where they are meant to.

The **KNT** split adds the same 12 documents for each team accountant (Rachel, Angeline, Aina, Aleeya, Jonathan)
and leaves the 14 existing documents with Jonathan. The SAIC accountants get 6 each (one or two per lane), if
Jonathan agrees (Q4).

### 3.3 Fresh files for live upload tests (these do spend AI quota)

For each teammate and each company they work in, generated by a generalised `docs/demo/build_demo_pack.py`:
- a PDF supplier invoice;
- a scanned bill (JPG);
- a receipt photo (JPG);
- a credit note (PDF);
- a payment slip (JPG);
- a bank CSV with 6–8 lines that match these documents.

Each file has an `expected.json` with the values a reader should extract: supplier, number, dates, subtotal, tax
as printed, total, lines, and the expected Inbox lane after upload. A short `README.md` per pack explains what to
upload and what to check.

Numbers are unique per teammate, so two people uploading on the same day do not collide with each other through
duplicate detection. Cost: about 6 documents × 2.5K tokens ≈ 15K tokens per teammate per company, about 150K for
everyone. That is under one Groq key's 200K/day but would use most of it, so we should stagger uploads or rely on the
Gemini fallback (Q6). The files are generated on demand into `ai/synthetic/output/` (gitignored), not committed.

---

## 4. How it is built and applied

### 4.1 Company-pack generator

The plan generalises the KNT tooling into profiles. It does not write a second copy of it.

- `ai/synthetic/sme_profiles/` holds one profile module per company: identity, chart additions, contacts,
  products, tax labels, bank layout, monthly document script and Inbox script. KNT becomes a profile that wraps
  today's `knt_pack` and `knt_extra`. **A regression test checks that the KNT pack output stays byte-identical.**
- `ai/synthetic/sme_pack.py` provides `books(profile)` and `generate(profile, out)`. It reuses `generate.Generator`
  and `Page` and the `company_pack` CSV and bank writers. Same seed gives the same bytes.
- `ai/synthetic/team_inbox.py` builds the per-accountant Inbox sets from the roster slots.
- `docs/demo/build_demo_pack.py` gains `--company` and `--person-slot` and writes `expected.json`.

### 4.2 Seed, tally and reset per company

- `services/admin/company_seed.py`: the KNT seed steps with the company passed as a parameter:
  - `setup_company`, `create_contacts`, `setup_catalogue`;
  - `typed_sales`, `typed_quotes`, `typed_bills`, `typed_cash`;
  - `capture_purchases`, `import_and_match_bank`.

  It runs through the app in-process, as today. `knt_seed.py` becomes a thin wrapper, so `seed-knt`, `reset-knt`,
  `extend-knt` and `tally` keep working unchanged.
- `services/admin/company_tally.py`: `tally(db, profile)` checks the trial balance, P&L by account, AR/AP open items
  against the ageing reports, the bank reconciliation for each cut-off, contacts, products and quotation chains.
  It also counts each accountant's documents per lane.
- `services/admin/company_reset.py`: the `knt_reset.wipe` logic for one company. It keeps users and is **local only**.

### 4.3 Commands (`manage.py`)

Every writing command is **additive, idempotent and a dry run by default**. It prints the plan and writes only
with `--apply`.

| Command | Purpose |
|---|---|
| `build-pack --company TKK` | files only, no database |
| `seed-company --company TKK` | local: create the company and its books from empty |
| `reset-company --company TKK` | local only: wipe and rebuild that company |
| `apply-company --company TKK [--apply] [--allow-remote]` | add what is missing to an existing database (the hosted path), like `extend-knt` |
| `team-accounts --roster <private file> [--apply] [--allow-remote]` | create missing accounts for the roster slots (§4.4) |
| `team-inbox --company TKK [--apply] [--allow-remote]` | create and assign each accountant's Inbox set |
| `tally --company TKK` / `tally --all` | read-only check, safe on hosted |

Rules shared by all of them:
- **Idempotency.** Everything is keyed by company code, contact code, document number or `idempotencyKey`
  (`pack:<CODE>:<kind>:<number>`). A second run finds nothing to do. When existing rows conflict with the pack
  (same number, different amount), the command stops before writing, as `knt_extend.conflicts` does.
- **Remote guard.** A non-loopback database needs `--allow-remote` *and* `COMPANY_PACK_REMOTE_HOST` equal to the host
  libpq will actually use. This generalises `KNT_EXTEND_REMOTE_HOST` and the parsed-host check in
  `demo_company._remote_destination`.
- **Concurrency.** One advisory lock per company.
- **Acting user.** The command acts as the company's local admin through temporary web sessions that are deleted
  afterwards. The audit rows name that admin.

### 4.4 Creating accounts safely

- No password is typed, generated or committed for real people. `team-accounts` inserts missing users with
  `status='active'`, **no password hash**, the slot's role and company:
  - existing users are never changed, and a role or company mismatch stops the run;
  - the seat limit is checked first.

  A user without a hash cannot sign in, because login compares against a dummy hash
  (`security/routes.py:21-25`).
- Each person then uses **Forgot password** on the UAT sign-in page: email code, then their own password.
  - Accountants: password plus emailed code at every sign-in.
  - Local admins: enrol an authenticator (TOTP) at first sign-in.
  - This is the path already proven for the SAIC accountants on uat.
- Locally, the Phase 2 proof uses only `.test` fixture users. Their password comes from a seed or fixture file
  (`UAT_TEST_PASSWORD` / the existing generated-once demo password), never from a real person.
- Email delivery on hosted needs Brevo on Render, the same dependency as today.

### 4.5 Hosted apply (uat): only on Jonathan's explicit go-ahead

1. **Read-only dry run** against uat: `apply-company`, `team-accounts` and `team-inbox` without `--apply`. Show the
   plan and counts to Jonathan.
2. **Neon snapshot:** create a branch from `uat` named `uat-pre-team-demo-<yyyymmdd-hhmm>`. Record its ID in the
   hand-over note.
3. `--apply --allow-remote` for one company at a time. Then `tally --company X` read-only against uat, then an API
   smoke check as that company's admin and one accountant: documents per lane, trial balance, bank reconciliation.
4. **Rollback:** restore `uat` from the snapshot branch (Neon "restore from branch"). This also discards anything
   teammates did after the snapshot, so apply at a quiet time and tell the team first. Delete the snapshot branch
   only after Jonathan confirms.

**Inbox files on hosted:** the files must be in the S3 bucket for previews to work. The 14 existing KNT files are
still waiting for whoever holds the rotated bucket keys (Q7). The command writes through the app's object store, so
it needs the bucket variables in the shell that runs it. Without them it can still create the records, but previews
404, as for KNT today.

### 4.6 Tests (Phase 2)

- Unit tests:
  - each profile's pack: deterministic; trial balance balances; open items tie; bank opening plus movements equals
    closing; manifests validate; subtotal + typed tax = total;
  - KNT bytes unchanged;
  - roster parsing and email masking;
  - the remote guard.
- Postgres integration (`TEST_DATABASE_URL`):
  - `seed-company` then `tally` passes for every company;
  - a second `apply-company` and `team-inbox` run is a no-op;
  - `reset-company` rebuilds the company.
- **Role and visibility matrix** with fixture users, from a real sign-in through the app:
  - each accountant sees exactly their own lane counts;
  - the local admin sees all of the company's documents;
  - an accountant gets 403 on approve, post and period close;
  - **a user of company A gets 404 on a document, invoice and bank line of company B**, and lists never show other
    companies.

---

## 5. Effort and open questions

### 5.1 Effort (Claude working time, estimates)

| Piece | Estimate |
|---|---|
| Profiles and generator for TKK, EHB, CDA; KNT wrapped with the byte-identical test | 6–8 h |
| Generalised seed, tally and reset; KNT commands kept working | 4–6 h |
| `team-accounts` and `team-inbox` (lanes, assignment, edge cases) | 3–4 h |
| Upload packs with `expected.json` | 2–3 h |
| Tests (unit, integration, role/isolation matrix) | 3–4 h |
| Local proof on a fresh database, PR into `features` | 1–2 h |
| **Phase 2 total** | **about 19–27 h**, realistically 2–3 working sessions |
| Hosted apply after approval (per company: dry run, snapshot, apply, tally, smoke check) | about 1 h each, about 4 h with KNT's split |

### 5.2 Open questions for Jonathan

1. **Companies:** are TKK (Kuching café), EHB (Miri hardware) and CDA (Kota Kinabalu advisory) right, or only two
   of them? Should any names change?
2. **Role table (§2.2):** is it OK as proposed? In particular, are Rachel, Angeline, Aina and Aleeya the local
   admins of TKK, EHB, CDA and CDA (second)?
3. **Logins:** do plus-addresses (`name+tkk@…`) suit everyone? A non-Gmail address may not support them. If not,
   each person needs a second real address for each extra company.
4. **SAIC accountants:** should they get their own KNT Inbox sets (6 each) and/or access to a new company?
5. **Volume:** about 12 Inbox documents per accountant per company. Is that enough, or too much?
6. **Live-upload quota:** stagger the teammates' upload tests across days, or rely on the Gemini fallback?
7. **Bucket keys:** who will upload the Inbox files to the bucket on hosted, Rachel? Until then, previews 404.
8. **Fix G1 (invite with no activation email)** as a separate PR? It would let local admins invite people for real,
   instead of using the "set Active, then Forgot password" workaround.
9. **Hosted timing:** when is a quiet window to snapshot and apply, given that a rollback discards later activity?

---

## 6. Decisions and proposals after Jonathan's answers (24 Nov)

### 6.1 Banks and roles
Decided:
- Real Malaysian bank names with invented account numbers (§2.1).
- The maker-checker role table (§2.2).

### 6.2 Plus-addresses: yes, the code still arrives
- All five teammates' UAT logins are Gmail addresses (see the uat hand-over note).
- Gmail delivers `name+tkk@gmail.com` to the normal `name@gmail.com` inbox. Gmail ignores everything after `+`.
- The app accepts `+` in an email (`main.py:176-177`).
- Brevo sends to the full address.

Before any account is created, each person sends one email from their phone to `name+test@gmail.com` and checks that
it arrives. This takes 30 seconds and needs no system. The SAIC accountants keep their single KNT login and are not
affected.

### 6.3 SAIC accountants (proposal)
- They stay **KNT accountants only**, with nothing in the new companies until they ask after the meeting.
- Each gets a **starter Inbox set of 6**: one or two per lane, including one exception. Their first sign-in then shows
  real work, not an empty page.
- They sign in through Forgot password with their SAIC email, which is already proven on uat.
- A one-page "first sign-in" guide goes with it.

### 6.4 "Enough to run the whole system": coverage, not a count
Keep about 12 documents per accountant. Each set must cover a checklist, and `tally` verifies that every lane is
present:
- the 5 lanes;
- the 3 exception types;
- a credit note and a debit note;
- one approved and posted document;
- one document that is bank-matched.

`docs/demo/TEAM_TEST_SCRIPT.md` gives a run-through per role:
- **Accountant:** review → fix exception → submit; type a sales invoice and a credit note; match a bank line;
  statement of account; search and trace.
- **Local admin:** approve or return; post; post a bank payment; complete the reconciliation; close and reopen
  September; assign work; invite a user.

### 6.5 AI capacity (Q6): facts and proposal
Facts:
- With `AI_EXTRACTION=text`, **every** upload calls the model (`services/extraction/lite/pipeline.py:139-150`). The
  rule reader only runs as a fallback (`:156-166`).
- A document costs about 2.4–2.6K tokens (`docs/ACCURACY_LOG.md:49`, `:98-100`).
- One free Groq key at 200K tokens a day reads about 80 documents a day, at about 3 per minute (8K tokens per
  minute).

Ideas, best first:
1. **Rules first for known suppliers.** Once a supplier has a few approved documents, try the rule reader with the
   existing verification first, and call the model only when a required field is missing or the totals do not tie.
   Repeat suppliers make up most of an SME's volume, so most documents would cost 0 tokens. This is also a strong
   demo story: "AI only for new layouts; it learns from approvals."
2. **Provider chain.** It is already supported (`AI_PROVIDERS`, `services/extraction/ai/text_llm.py:9-13`): Groq,
   then Gemini Flash-Lite, then another free provider. Each has its own free daily quota.
3. **A fair daily budget per company and a "waiting for AI capacity" queue** instead of failing, with a quota gauge on
   the dashboard and catch-up overnight. Three accountants in one company can then never starve another company.
4. **Fewer tokens per document:** a shorter prompt, sending only the header and totals rows, no reasoning tokens. The
   saving is an estimate until measured.
5. **Production:** each client company brings its own API key, or uses a paid tier. At about 2.5K tokens per document
   the cost per document is very small, but check the providers' current prices first. This is paid usage, so it
   needs your approval.

Not recommended: opening many free accounts to multiply the quota. That goes against the providers' terms.

For team testing now:
- The seeded data spends 0 tokens.
- Live uploads are limited to each person's 6-file pack, with one upload slot per teammate per day.
- Ideas 1–4 are their own task (a separate session and PR).

### 6.6 Inbox files on hosted (Q7)
What the files are and why it matters:
- Each Inbox document keeps its original PDF or JPG so the preview pane can show it.
- Locally the files sit in `storage/`. On hosted they must go to the S3 bucket, because Render's disk is wiped on
  every deploy.
- If `S3_BUCKET` is not set on Render, even live uploads lose their files at the next deploy. This needs checking
  before the demo.
- Uploading through the interface is not the answer, because it re-runs extraction and spends quota.

Proposal:
- `team-inbox --apply` writes each file straight to the bucket through the app's object store. It reads the bucket
  keys from a private env file on Jonathan's machine (read-only, never printed).
- The same step uploads the 14 KNT files that are still waiting.
- This needs Rachel to confirm that `S3_BUCKET` is set on Render and to share the bucket keys privately, or to run
  that one command herself.
- Fallback: the records work without the files; only the preview shows the file as unavailable.

### 6.7 Invite link (Q8): a separate PR
1. **Local admin invites an accountant:** create a single-use activation token and email the link through Brevo. The
   page (`src/app/(public)/activate/page.tsx`) and the endpoint (`services/routes/security/routes.py:359`) already
   exist. "Resend" issues a new token and the old one stops working.
2. **Super admin (Group 14) approves a company:** the same email goes to the new local admin.
3. **Super admin "add user to company"** with a role (local admin or accountant), for SAIC and future clients.
4. If email delivery fails, the admin sees a copy-once link.
5. Tests cover expiry, reuse, the wrong company and seat limits.

Once this lands, `team-accounts` can use the same invite path instead of the "active with no password" step.

### 6.8 Timeline (Q9, proposal)
| When | What |
|---|---|
| 24–25 Nov (SAIC meeting tomorrow) | **No writes to uat.** It stays exactly as it is for the meeting. |
| 25–27 Nov | Phase 2 built and proven locally; PR into `features`. Invite-link PR in parallel. |
| Sat 28 Nov, evening | Tell the team, take a Neon snapshot of uat, apply TKK, EHB, CDA, accounts, Inbox sets and the KNT split; tally; smoke check. |
| Sun 29 Nov | Everyone sets their passwords (Forgot password), admins enrol TOTP, and each person runs `TEAM_TEST_SCRIPT.md`. |
| Until the final demo | Testing on uat; bug fixes deploy as code only (no data resets). |
| 2 days before the final demo | A second snapshot, `uat-pre-final-demo`, so a demo run can be reset in minutes. |
