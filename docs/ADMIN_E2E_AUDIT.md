# Local admin end to end, and set-aside guarantees (25 Nov 2026)

Local stack: backend this branch on `features` 677af54 (port 8040), frontend branch `claude/admin-e2e-set-aside-fe` on
`features` 7e7f8aa (port 3010), a disposable local PostgreSQL 16 with `migrate` → `seed-knt` (tally PASS) →
`team-inbox` (KNT accountant, 12 documents) and 8 uploads from `demo_upload/`. Nothing hosted was touched.

Sign-in: the KNT local admin signed in through the real login page (seeded test password + TOTP enrolment). The
Playwright runs then used `manage.py browser-session` sessions (local, `*.demo.test` only) for the admin, the
accountant and the invitee. Every action below was clicked in the real UI; the database and a reload were checked
after each one. "Accountant refused" was checked with the accountant's own session: the page (UI) and the API call.

## Action table

| Action | Result | Evidence | Fix |
|---|---|---|---|
| Approve one, with a note | Works | `POST /documents/{id}/review/decision` 200 + `POST /records/from-document/{id}` 201; after reload the card is gone; DB `review_decisions.note` = the typed note, status `completed`, 1 record. Accountant: page "off limits", API 403 | — |
| Bulk approve (2) | Works | modal confirm → `POST /documents/review/bulk-decision` 200 + 2× from-document 201; both `completed` with the note. Accountant: `/review/bulk-approve` and `/documents/review/bulk-decision` 403 | — |
| Return to accountant | Works | decision `returned`, note kept, status `exception` (back in the accountant's To review) | — |
| Reject | Works, but same state as Return | decision `rejected`, status `exception`. A rejected document returns to the accountant exactly like a returned one; the natural next step is Set aside (now a row action) | Not changed (product decision, see open items) |
| Posting preview → Post | Works; **bug fixed** | Approved records → Preview journal → Post: `POST /erp/scanned/{id}/post` 200; journal 462.00 = 462.00 posted; row leaves "to post" after reload. Accountant: post / post-bulk / post-payment 403 | The preview dialog (560px, overflow hidden) cut off the **Credit** column; widened (FE `journal-preview.tsx`, `common.css`) |
| Bank match: confirm, then unmatch | Works | Money out → line → Find payments → Match to PV-2026-00001 → Confirm match: `match-document` 200, `bank_document_matches` row; Unmatch… with reason: `unmatch-document` 200, `unmatched_by/at/reason` stored, line back to `imported`. Accountants may also match/unmatch (UI and API agree, by design) | — |
| Invite a user, accept as that user | Works; **bug fixed** | `POST /users/invitations` 201; no mail provider locally, so the one-time link is shown; Resend → new link; invitee opens it in a fresh browser, sets a password → `POST /auth/activate` 200, user `active`. Accountant: invite / resend 403, page off limits | "Invited by" showed the inviter's raw UUID; now the name (FE `inviterName`). **No audit row** was written for invite/resend/disable/rename/permission; now audited (BE `admin/routes.py`) |
| Users & roles: change a role | **Not available** | No endpoint or UI lets a local admin change a role (only status, name, temporary password; a super admin picks the role when adding a user). Not built here: promoting to local admin affects separation of duties | Open item |
| Users & roles: deactivate | Works | Disable → `PATCH /users/{id}/status` 200, row DISABLED after reload; the user's live session gets 401 on the next call. Accountant: 403 | Now audited (`user.status_changed`, from/to) |
| Set aside, then restore | Works; **row action added** | Exceptions and All documents rows → "Set aside…" → reason → `POST /documents/{id}/exclude` 200; row gone at once and after reload; Today counts drop (To review 18→16, Needs fixing 12→11, month-end total 34→32). Set-aside list shows date, **by whom** and reason; Restore → `POST …/restore` 200, audited with the undone reason. Accountant: no Restore button, API 403 | Part C (FE) + who on the list, restore audit detail (BE) |
| Approved records view | Works | `/records` lists the approved records; set-aside documents' records now excluded | Filter added (BE `list_records`) |
| Check rules | Works; **audit added** | Low-confidence threshold 80 → 85, `PUT /validation-rules` 200, 85 after reload. Accountant: page off limits, API 403 | Change now audited (`validation_rules.updated`) |
| Audit trail | Works; **leak fixed** | Page lists set aside / restore with reason, person, time; rule change; bank match/unmatch. But `GET /api/audit` gave an **accountant the whole company log** (the nav said admin-only) | Accountants now get only their own actions and the history of documents they own or are assigned (document page history still works) |
| System page | Works | Processing health, pipeline counts, reading attempts, unusual bank amounts all from live APIs (`/processing/health`, `/processing/trends`, `/documents`, `/bank/transactions`). Accountant: page off limits, `/processing/health` 403 | — |

Every admin page's network requests were recorded with the admin session; each calls a real API and none returns
mock data. Accountant refusals: 14 admin-only writes return 403 (`test_admin_only_actions_refuse_an_accountant`), and
`/company/*` plus `/workspace/{approvals,users,audit,system}` show "This area is off limits", with no Admin menu.

## Set-aside guarantees (part B)

| Guarantee | Proof |
|---|---|
| 1. Leaves every list and count | `test_a_set_aside_document_leaves_every_list_and_count`: All documents (admin and accountant), To review, Needs fixing, Exceptions (both roles), review lanes, accountant inbox, workload list, workload report, search, Today counts, month-end progress, accounting report, documents and exceptions exports. Browser: the set-aside receipt is absent from All documents, To review, Needs fixing, Exceptions, Approvals, Dashboard, Reports, System |
| 2. Kept, with reason, person and time in the audit trail | `test_a_set_aside_document_is_kept_with_reason_person_and_time_and_restored_by_an_admin`; browser: Audit trail page and Set-aside list |
| 3. Listed under Set aside; only a local admin restores; restore audited | same test (accountant 403, admin 200, `document.restored` with `previousReason`); browser: admin Restore, accountant has none |
| 4. Nothing posts from it, nothing else is required | `test_nothing_can_be_submitted_approved_or_edited_while_set_aside` (submit, bulk submit ×2, field edit, line item, approve, bulk approve ×2, save to records → 409 "This document is set aside…") and `test_a_record_of_a_set_aside_document_does_not_post_or_match` (post preview, post, bulk post, supplier-bill post, records lists, match search, match-many, confirming a suggestion → refused; rejecting it still works) |

Queries fixed to leave set-aside documents out: workload list (`/api/workload`), approved records (`/api/records`),
documents and exceptions exports, records export (CSV and XLSX), workload report, processing trends, search
(approved records), ERP scanned purchase/sales lists, bank match search and suggestions. Actions now refusing them:
every document action that locks the document (`_lock_workspace_document`: submit, decide, retry, line items, findings,
type choice), field correction, save to records, scanned post/preview/bulk, ledger posting from a record, every
settlement allocation (match-many, manual allocation) and confirming a bank suggestion.

Also fixed: `GET /api/accountant/inbox` without filters returned 500 (untyped NULL parameters).

## Open items (not changed here)

- No role change for a local admin (see table). Decide whether it should exist and who may promote to local admin.
- Reject and Return lead to the same state; a rejected document could go straight to Set aside instead.
- The document page (owned by the review session) still shows editable fields on a set-aside document; the API
  refuses every change with a clear message. Reported to that session.
- Audit trail page shows resource UUIDs and raw JSON details (cosmetic).
- Super-admin actions (company approval, seat limits, tenant status) are still not audited.
