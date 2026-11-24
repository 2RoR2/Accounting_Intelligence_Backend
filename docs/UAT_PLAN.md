# Accounting Intelligence: User Acceptance Test Plan

Version: draft for UAT handover, Friday 27 November 2026. Acceptance script source: `docs/MASTER_PLAN.md` section 10.3. Success criteria source: `docs/MASTER_PLAN.md` sections 1 and 10.1.

## 1. Purpose, scope and environment

### 1.1 Purpose

Confirm, with the client's testers, that the proof of concept supports an accountant's daily and monthly work on a Malaysian SME's books, and record evidence for the five success criteria (SC1 to SC5) from the brief.

### 1.2 Scope

| In scope | Out of scope |
|---|---|
| AI extraction of supplier bills and customer invoices, review and approval | Live LHDN / MyInvois e-invoice submission |
| Posting approved documents to the internal ledger | Real client documents or real bank statements |
| Bank statement import and reconciliation | Tax computation or filing (the recorded tax summary shows printed tax only and is "not a tax return") |
| Month-end checklist, period lock, reports, month pack | Performance tuning during the session |
| Sales and purchase flows, user and company administration | Changes to code during the session (defects are logged, not fixed live) |

### 1.3 Environment

| Item | Value |
|---|---|
| System under test | Hosted staging copy of Accounting Intelligence (separate `uat` database branch; the team database is not used) |
| Test data | Seeded demo company, "trading" pack; synthetic documents only |
| Accounts | One test account per role (Accountant, Company admin, Super admin), issued by the facilitator on the day; test credentials only |
| Browser | Current Chrome or Edge on a laptop; phone camera optional for upload |
| Reset | Demo data reset the night before the session; do not reset during the session |
| Evidence | Screenshots named `UAT-<scenario ID>-<n>.png`; defect log in section 7 |

### 1.4 Schedule

| Item | Date / time |
|---|---|
| Feature freeze (fixes from the walkthrough only after this) | Thursday 26 Nov 2026, 12:00 |
| Demo data reset and smoke check | Thursday 26 Nov 2026, evening (time to be confirmed by Jonathan) |
| UAT session start | Friday 27 Nov 2026, __:__ (to be confirmed by Jonathan) |
| Effort study (section 6) | ____ (to be confirmed by Jonathan) |
| SUS questionnaires completed | End of UAT session |
| Defect triage and results summary | ____ (to be confirmed by Jonathan) |

### 1.5 Sign-off

| Role | Name | Decision (Accept / Accept with conditions / Reject) | Signature | Date |
|---|---|---|---|---|
| Client representative (SAIC) | | | | |
| Client representative (SAIC) | | | | |
| Project lead | | | | |
| Facilitator | | | | |

Conditions or comments:

______________________________________________________________________

______________________________________________________________________

## 2. Tester roles

| Role | Who plays it | Responsibilities |
|---|---|---|
| Accountant | Client tester(s) | Daily scenarios, monthly scenarios, sales and purchases; completes SUS; effort study participant |
| Company admin (local admin) | Client tester or project team member | Lock period, month pack, user and company settings, catalog |
| Super admin (platform) | Project team member | System page and evaluation page; platform-level checks |
| Observer / facilitator | Project team member | Issues accounts, runs the stopwatch, scores each scenario, records defects, does not operate the system for the tester |

Scoring is done by the facilitator from what the tester did: 0 = fail (the outcome was not reached), 1 = works with help (reached after a hint from the facilitator), 2 = works unaided.

| Scenario group | Accountant | Company admin | Super admin |
|---|---|---|---|
| Daily (D1 to D8) | D1 to D8 | D1 | D1 |
| Monthly (M1 to M4) | M1, M3 | M2, M4 | |
| Sales and purchases (S1 to S3) | S1 to S3 | | |
| Admin (A1 to A4) | | A1 to A3 | A4 |

## 3. Scenarios

Severity, used in the scenario sheets and the defect log:

| Severity | Meaning |
|---|---|
| Critical | Blocks a critical scenario or gives wrong accounting figures; no workaround |
| Major | A function fails or misleads, but a workaround exists |
| Minor | Works, but is awkward, slow or unclear |
| Cosmetic | Wording, layout or appearance only |

### 3.1 Daily

#### D1 Log in

| Role | Critical | Preconditions |
|---|---|---|
| All roles | Yes | Test account issued; staging URL open |

Steps:
1. Open the staging URL and enter the test account email.
2. Enter the login code or test password issued for staging; complete the second step if prompted.
3. Confirm the demo trading company is shown.

Expected result: the tester reaches the Dashboard of the demo trading company with their own role; a wrong code is refused with a clear message.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### D2 Dashboard overview

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | No | D1 passed |

Steps:
1. Read the Dashboard.
2. Open one item from each list (to review, ready to approve, needs fixing, bank lines to match, bills due in 7 days, customers overdue, journals waiting to post).

Expected result: each count is shown and links to the matching list; the Dashboard does not show all zeros for the demo company.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### D3 Upload a batch

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | Yes | D1 passed; a folder of synthetic bills and invoices (PDFs and phone photos) supplied by the facilitator |

Steps:
1. Open Upload.
2. Drop the whole batch at once (or take one photo with the camera).
3. Watch the progress of each file.

Expected result: every file is accepted, the document type is detected without being chosen by hand, and each file shows its progress until it is ready for review or flagged.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### D4 Review side by side

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | Yes | D3 passed; at least one document waiting for review |

Steps:
1. Open a document from the review list.
2. Check only the fields marked "Please check".
3. Click a field and confirm the matching area of the document is highlighted.
4. Correct one wrong value and save.
5. Use the keyboard to accept a field and move to the next.

Expected result: document on the left and fields on the right; the corrected value is kept after saving; the document moves forward for approval.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### D5 Approve

| Role | Critical | Preconditions |
|---|---|---|
| Accountant, then local admin | Yes | D4 passed; documents in the ready lane and at least one in needs fixing; the facilitator prepares one needs-fixing document whose supplier name is a small misspelling of an existing supplier (e.g. "Elektrical" for "Elektrikal"), so a suggested fix exists |

The app keeps separation of duties: the accountant submits, a different person (the local admin) approves.

Steps:
1. As the accountant, open the ready lane, select all and submit for approval; as the local admin, open Approvals, select all and approve.
2. Open the needs-fixing lane and read the reason on one item.
3. Apply the one-click suggested fix, then approve that item.

Expected result: selected documents change to approved; each needs-fixing item shows its reason and a suggested fix; no document is approved without passing review.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### D6 Post to ledger

| Role | Critical | Preconditions |
|---|---|---|
| Local admin | Yes | D5 passed; approved documents with proposed journals (the accountant can open the preview; posting is the local admin's) |

Steps:
1. Open the journals waiting to post.
2. Open one proposed journal and check the accounts (supplier default account, printed tax, payables).
3. Post it on its own.
4. Select the rest and post in bulk.

Expected result: each journal balances (debits equal credits) and appears in the general ledger; posted journals leave the waiting list.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### D7 Bank import and match

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | Yes | D6 passed; synthetic bank statement file supplied by the facilitator |

Steps:
1. Open Reconciliation and import the statement (use the saved column mapping).
2. Confirm one suggested exact match.
3. Confirm one partial or combined (one-to-many) payment match.
4. Create an expense for a bank fee line.

Expected result: statement lines import without manual column setup; confirmed matches show as reconciled; the fee is recorded as an expense and matched.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### D8 Supplier contact page

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | No | D4 passed |

Steps:
1. Open Contacts and choose a supplier used in D4.
2. Read its defaults, open items, documents and learned corrections.

Expected result: the supplier page shows its default account, tax code and terms, its open items and documents, and the correction made in D4.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

### 3.2 Monthly

#### M1 Month-end checklist

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | No | Daily scenarios completed for the demo month |

Steps:
1. Open the month-end checklist for the demo month.
2. Open one item that is not green.

Expected result: every item is green or links to what is left to do.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### M2 Lock period

| Role | Critical | Preconditions |
|---|---|---|
| Company admin | Yes | M1 done; D7 done for the period |

Steps:
1. Open the reconciliation statement for the period and check it.
2. Lock the period.
3. As the Accountant, try to post a journal dated in the locked period.

Expected result: the period shows as locked; posting into it is refused with a clear message.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### M3 Reports

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | No | D6 passed |

Steps:
1. Open each report for the demo month: profit and loss, balance sheet, trial balance, general ledger, payables and receivables ageing, recorded tax summary.
2. Compare one total with the journals posted in D6.

Expected result: each report opens for the chosen period; the trial balance balances; the balance sheet balances; the recorded tax summary is labelled "not a tax return".

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### M4 Month pack and export

| Role | Critical | Preconditions |
|---|---|---|
| Company admin | No | M3 passed |

Steps:
1. Generate the month pack for the demo month.
2. Open the downloaded Excel workbook.

Expected result: one workbook with all reports for the period and the open-exceptions list; figures match the on-screen reports.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

### 3.3 Sales and purchases

#### S1 Quotation to invoice to customer payment

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | Yes | D1 passed; a customer exists in the demo company |

Steps:
1. Create a quotation for a customer.
2. Convert the quotation to an invoice and post it.
3. Record the customer payment against the invoice.
4. Open the customer's receivables.

Expected result: the invoice carries the quotation's lines; after payment the invoice shows as paid and the customer's balance falls by the amount paid.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### S2 Bill to supplier payment

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | Yes | D6 passed; at least one posted, unpaid supplier bill |

Steps:
1. Open an unpaid supplier bill.
2. Record the supplier payment from a company bank account.
3. Open the supplier's payables.

Expected result: the bill shows as paid; payables fall by the amount paid; the payment is posted to the chosen bank account.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### S3 Void a posted invoice

| Role | Critical | Preconditions |
|---|---|---|
| Accountant | No | A posted, unpaid customer invoice in an open period |

Steps:
1. Open the posted invoice and void it.
2. Open the general ledger for the invoice's accounts.

Expected result: the invoice shows as void; a reversing journal is posted; the original journal is kept, not deleted; receivables return to the earlier balance.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

### 3.4 Admin

#### A1 Users and roles

| Role | Critical | Preconditions |
|---|---|---|
| Company admin | No | D1 passed as Company admin |

Steps:
1. Open Users.
2. Invite a test user as Accountant.
3. Change that user's role, then deactivate the user.

Expected result: each change is saved and shown in the user list; the deactivated user cannot sign in.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### A2 Company settings

| Role | Critical | Preconditions |
|---|---|---|
| Company admin | No | D1 passed as Company admin |

Steps:
1. Open company settings.
2. Change one non-financial detail (for example the company address) and save.
3. Reload the page.

Expected result: the change is kept after reload.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### A3 Catalog: payment terms, currencies, bank accounts, tags

| Role | Critical | Preconditions |
|---|---|---|
| Company admin | No | D1 passed as Company admin |

Steps:
1. Add a payment term.
2. Add or enable a currency.
3. Add a bank account.
4. Add a tag.
5. As the Accountant, open a new invoice and check the new items can be chosen.

Expected result: each item is saved and is offered where it is used.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

#### A4 System page and evaluation page

| Role | Critical | Preconditions |
|---|---|---|
| Super admin | No | D1 passed as Super admin |

Steps:
1. Open the System page and read processing health.
2. Open the evaluation page and read the five criteria tiles.
3. As the Accountant, check that the System page is not in the menu.

Expected result: processing health is shown to the admin only; each criterion tile shows a measured value or "Not measured", never an estimate.

| Score (0/1/2) | Defect ID | Severity | Notes |
|---|---|---|---|
| | | | |

### 3.5 Score summary

| ID | Scenario | Critical | Score (0/1/2) |
|---|---|---|---|
| D1 | Log in | Yes | |
| D2 | Dashboard overview | No | |
| D3 | Upload a batch | Yes | |
| D4 | Review side by side | Yes | |
| D5 | Approve | Yes | |
| D6 | Post to ledger | Yes | |
| D7 | Bank import and match | Yes | |
| D8 | Supplier contact page | No | |
| M1 | Month-end checklist | No | |
| M2 | Lock period | Yes | |
| M3 | Reports | No | |
| M4 | Month pack and export | No | |
| S1 | Quotation to invoice to customer payment | Yes | |
| S2 | Bill to supplier payment | Yes | |
| S3 | Void a posted invoice | No | |
| A1 | Users and roles | No | |
| A2 | Company settings | No | |
| A3 | Catalog | No | |
| A4 | System page and evaluation page | No | |
| | **Average score (sum / 19)** | | |
| | **All critical scenarios scored 2 (Yes / No)** | | |

## 4. Success-criteria sheet

Targets are taken from the brief as stated in `docs/MASTER_PLAN.md` sections 1 and 10.1. SC1 to SC4 follow the labels used in `scripts/saic_evaluate.py`; SC5 is the fifth criterion in the same table. A criterion without a measurement is reported as "Not measured", never estimated.

| ID | Criterion | Target | Measurement method | Evidence location | Measured value | Met (Yes / No) |
|---|---|---|---|---|---|---|
| SC1 | Field extraction accuracy | At least 95% on the pilot set (strict figure) | Hold-out and public-set benchmark run with the chosen provider; SAIC pilot set when supplied. Tuning-set results do not count | Benchmark report file from the I1 run; Evaluation page accuracy tile | | |
| SC2 | Manual data entry reduction | At least 70% | Effort study (section 6): time saved, manual vs system, same documents; touchless-field rate = fields approved unchanged / all fields approved | Effort-study export (`accordai-effort-study.csv`) and paper timing sheets; Evaluation page | | |
| SC3 | Processing time per document | 20 s or less | p95 of `processing_seconds` on the hosted UAT environment (and localhost) | Evaluation page processing tile; load test report | | |
| SC4 | Exception rate, all exceptions routed | Below 10%, every exception routed | Exception rate on the demo month; check that every exception is visible with a suggested fix | Evaluation page exception tile; Exceptions page screenshots | | |
| SC5 | High-volume concurrent submissions | To be confirmed by Jonathan (the repo specifies the test, a 50-document concurrent load test, but no pass threshold) | 50 documents uploaded concurrently; report throughput, failures, p95 seconds and provider rate-limit events | Load test report file from the I5 run; Evaluation page | | |

## 5. System Usability Scale (SUS)

Each tester (Accountant, Company admin, Super admin) completes one sheet at the end of the session, alone, before discussion. The Observer / facilitator does not complete one. Print one copy of the sheet below per tester.

Scoring rule:
- Odd items (1, 3, 5, 7, 9): item score = response - 1.
- Even items (2, 4, 6, 8, 10): item score = 5 - response.
- SUS score = (sum of the ten item scores) x 2.5, giving 0 to 100.
- Mean SUS = average of all testers' SUS scores.
- Every item must be answered; if a tester is unsure, they mark 3.

Source: Brooke, J. (1996). SUS: A "quick and dirty" usability scale. In P. W. Jordan, B. Thomas, B. A. Weerdmeester and I. L. McClelland (Eds.), *Usability Evaluation in Industry* (pp. 189-194). London: Taylor and Francis.

### SUS sheet (one per tester)

Tester role: ____________________  Date: ____________

Mark one number per statement: 1 = strongly disagree, 5 = strongly agree.

| No. | Statement | 1 | 2 | 3 | 4 | 5 | Item score |
|---|---|---|---|---|---|---|---|
| 1 | I think that I would like to use this system frequently. | | | | | | |
| 2 | I found the system unnecessarily complex. | | | | | | |
| 3 | I thought the system was easy to use. | | | | | | |
| 4 | I think that I would need the support of a technical person to be able to use this system. | | | | | | |
| 5 | I found the various functions in this system were well integrated. | | | | | | |
| 6 | I thought there was too much inconsistency in this system. | | | | | | |
| 7 | I would imagine that most people would learn to use this system very quickly. | | | | | | |
| 8 | I found the system very cumbersome to use. | | | | | | |
| 9 | I felt very confident using the system. | | | | | | |
| 10 | I needed to learn a lot of things before I could get going with this system. | | | | | | |
| | **Sum of item scores** | | | | | | |
| | **SUS score (sum x 2.5)** | | | | | | |

### SUS summary

| Tester role | SUS score |
|---|---|
| | |
| | |
| | |
| **Mean SUS** | |

## 6. Effort study protocol (evidence for SC2)

### 6.1 Materials

- 20 synthetic documents (supplier bills and customer invoices), split into Set A (10) and Set B (10) of similar mix and length. Source of the 20-document set: to be confirmed by Jonathan.
- An answer key with the correct value of every field to be entered, prepared before the session.
- A spreadsheet template with one row per document and one column per field (the manual method).
- The system, signed in as the Accountant (the system method).
- A stopwatch and the timing sheet below.

### 6.2 Procedure

1. Each tester processes 10 documents manually (typing the fields into the spreadsheet) and 10 with the system (upload, review, correct, approve).
2. Counterbalance method order and document set across testers:

| Tester | First block | Second block |
|---|---|---|
| Tester 1 | Set A, manual | Set B, system |
| Tester 2 | Set A, system | Set B, manual |
| Tester 3 (if any) | Set B, manual | Set A, system |
| Tester 4 (if any) | Set B, system | Set A, manual |

3. Before each block, give a two-minute practice on one document not in the study.
4. Time each document with the stopwatch, from opening the document to the last field entered (manual) or approved (system). For the system method, include upload, processing wait and review time; if the processing wait is noticeable, also note it separately in Notes.
5. After each block, compare the entered or approved values with the answer key and count field errors per document (a wrong, missing or extra value is one error).
6. Do not help the tester with field values. Help with the interface is noted in Notes.

### 6.3 Timing sheet (one per tester)

Tester role: ____________________  Order: Tester 1 / 2 / 3 / 4

| Doc no. | Set | Method (Manual / System) | Minutes | Field errors | Fields checked | Notes |
|---|---|---|---|---|---|---|
| 1 | | | | | | |
| 2 | | | | | | |
| 3 | | | | | | |
| 4 | | | | | | |
| 5 | | | | | | |
| 6 | | | | | | |
| 7 | | | | | | |
| 8 | | | | | | |
| 9 | | | | | | |
| 10 | | | | | | |
| 11 | | | | | | |
| 12 | | | | | | |
| 13 | | | | | | |
| 14 | | | | | | |
| 15 | | | | | | |
| 16 | | | | | | |
| 17 | | | | | | |
| 18 | | | | | | |
| 19 | | | | | | |
| 20 | | | | | | |

### 6.4 Calculation

- Mean manual time = total manual minutes / number of manual documents (all testers).
- Mean system time = total system minutes / number of system documents (all testers).
- Time saved (%) = (mean manual time - mean system time) / mean manual time x 100. This is the SC2 figure, compared with the 70% target.
- Error rate (%) per method = total field errors / total fields checked x 100, reported separately for manual and system.
- Report also the touchless-field rate from the Evaluation page as supporting evidence.
- Report the number of testers and documents with every figure. Do not drop documents; if one could not be finished, record it in Notes and report it.

| Measure | Manual | System |
|---|---|---|
| Documents processed | | |
| Total minutes | | |
| Mean minutes per document | | |
| Total field errors | | |
| Total fields checked | | |
| Error rate (%) | | |
| **Time saved (%)** | | |

### 6.5 Where results go

- Signed timing sheets: kept by the facilitator with this plan.
- Where the system's effort-study module is used, its results export (`accordai-effort-study.csv`).
- Final figures: the Evaluation page (SC2 tile), section 4 of this plan, and the technical report.

### 6.6 UAT workload study (what the system records)

This adds the workload figures from section 6 of `docs/COMPETITOR_AND_AI_ROADMAP.md` (backend PR #50) to the
timed study above. The Workload panel on the Evaluation page (`GET /api/reports/workload`) reads them from records
the system already keeps. It never estimates a figure: anything without a record shows as "Not measured".

**Material.**
- Two matched sets of 20 synthetic KNT documents (Set A, Set B): the same mix of invoices, bills, receipts, credit or
  debit notes and phone photos. Today's KNT pack holds 14 scanned purchase documents, so both 20-document sets still
  have to be generated before the session (open item).
- One 30-line KNT bank statement. Time it on the timing sheet. Bank work is not yet counted in the Workload panel.

**Participants.** At least 2 SAIC accountants. The project team does not count. Record participant ids
(P01, P02, ...), never names, on every sheet and in the effort-study module.

**Design.** Counterbalanced AB/BA, the same rule the effort-study module uses:

| Participant id | First block | Second block |
|---|---|---|
| Odd (P01, P03, ...) | Set A, manual | Set B, AccordAI |
| Even (P02, P04, ...) | Set A, AccordAI | Set B, manual |

"Manual" means typing into AccordAI's typed forms with AI off, on the same screens, so the comparison is fair.

**Recording.**
- In-app timers and counters only. No screen recording without consent.
- Run each participant's AccordAI block in a fresh copy of the KNT company, or note the block's start and end dates
  and use the panel's date filter. The panel groups figures by company and upload date, not by participant.
- Note the n and document codes per block, plus every failed, abandoned or excluded document with the reason.

**What to read from the Workload panel (AccordAI block).**

| Figure | Definition |
|---|---|
| Touches per document | Field edits + line-item changes + type choices + decisions (submit, approve, return, finding accepted, AI suggestion or currency decision). Median, range, n |
| Corrected fields per document | Distinct fields with a human correction (`corrected_by` set). Median, range, n, and the most-corrected field keys |
| First-pass exception rate | Documents whose status just before the first human touch was "exception", over documents with a system result. Documents still processing or failed are listed as excluded (supporting evidence for SC4) |
| Ready without any correction | Approved documents with no field edit, line-item change or type choice, over approved documents |
| Review time | Active seconds from the optional review opened/closed event. Shows "Not measured" until the review page sends it (follow-up) |

**Reporting.**
- Report the median and range of each figure, the n, and every exclusion with its reason.
- Do not extrapolate beyond the tested sets. If a criterion is not met, say so.
- SC2 (at least 70% less manual entry) is taken only from the effort study (sections 6.2 to 6.4, or the effort-study
  module). It counts as valid only with at least 2 paired participants covering both orders.
- The panel shows that result next to the touch figures. Touches have no manual baseline, so they are never turned
  into a percentage reduction.

## 7. Defect log

Defect IDs run UAT-001, UAT-002 and so on. Status values: Open, In progress, Fixed, Retest passed, Won't fix, Deferred.

| ID | Date | Scenario | Tester role | Steps to reproduce | Expected | Actual | Severity | Screenshot ref | Status | Fixed in |
|---|---|---|---|---|---|---|---|---|---|---|
| UAT-001 | | | | | | | | | | |
| UAT-002 | | | | | | | | | | |
| UAT-003 | | | | | | | | | | |
| UAT-004 | | | | | | | | | | |
| UAT-005 | | | | | | | | | | |
| UAT-006 | | | | | | | | | | |
| UAT-007 | | | | | | | | | | |
| UAT-008 | | | | | | | | | | |
| UAT-009 | | | | | | | | | | |
| UAT-010 | | | | | | | | | | |

"Fixed in" records the commit or release tag that contains the fix.

## 8. Pass rule

UAT passes only when all four conditions hold:

1. Every critical scenario scores 2.
2. The average scenario score is 1.6 or higher.
3. The mean SUS score is 68 or higher.
4. SC1 to SC5 are all measured, and each is reported as met or not met with its evidence.

| Condition | Result | Pass (Yes / No) |
|---|---|---|
| Every critical scenario scores 2 (D1, D3, D4, D5, D6, D7, M2, S1, S2) | | |
| Average scenario score >= 1.6 | | |
| Mean SUS >= 68 | | |
| SC1 to SC5 all measured, each reported met or not met with evidence | | |
| **UAT result** | | |
