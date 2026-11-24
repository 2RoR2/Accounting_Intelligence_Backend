# AccordAI accountant function audit (24 Nov 2026)

A page-by-page check of what a Malaysian SME accountant (used to Bukku or Xero) expects on each page, what AccordAI has,
and what is broken or missing. **Nothing was fixed in this task.** It ends with a prioritised fix list and questions
for Jonathan.

## Scope and method

| Item | Value |
|---|---|
| Backend | `origin/features` **5ca59c3** (the commit on Render UAT) |
| Frontend | `origin/features` **4b7ff59** (the commit on Vercel production) |
| Roles | ACCOUNTANT (`/workspace/...`) and LOCAL_ADMIN (`/company/...`). Both use the same sidebar (see §1). |
| Code read | Every page component under `src/features/common/**`, the layout/navigation, the API services, and the backend routes for each page. All 242 backend routes were compared with the frontend's API paths (§17). |
| Local check | Own database `ai_audit_fn` on the local Postgres (127.0.0.1:5433): `migrate`, then `seed-knt` (tally PASS for 30 Sep, 31 Oct, 23 Nov). API at 8093 with synthetic `*.demo.test` sessions from `manage.py browser-session` (no real accounts, no hosted data). API calls only: no frontend dev server, so UI behaviour is from code. |
| Not touched | Hosted Neon `uat`, Render and Vercel settings. No secrets printed or committed. |

**Evidence format.** `FE` paths are in the frontend repo (`src/...`), the others are in this repo. *(obs)* means seen on
the local stack; *(note)* means reported by another session on 23–24 Nov and not re-checked here; everything else is
from reading the code.

**Status values.** OK = present and working · BROKEN = present but broken · MISSING = no button or UI ·
BE-ONLY = endpoint exists, no UI · UI-ONLY = UI without a backend. Fix size: S (under half a day), M (1–2 days),
L (3 or more days).

---

## 0. The six demo bugs (24 Nov): confirmed

| # | Bug | Confirmed? | Cause | Evidence | Fix |
|---|---|---|---|---|---|
| a | Review: accountant cannot change payment/credit terms or due date | **Yes** | The Review tab only renders fields the reader stored; nothing lets you add a field. `payment_terms` is only defined for invoices, so bills, receipts and notes never have it. The correction API returns 404 for a field that was not stored. "Only fields to check" (on by default) also hides fields read from the document. Scanned documents never use the supplier's payment terms to set a due date (only typed Sales/Purchases forms do). | FE `features/common/documents/document-review.tsx:462-487, 693-698` (renders `extraction.fields` only), `:326` (filter on by default); `services/extraction/contracts/field_catalog.py:235` (`payment_terms: ['invoice']`); `services/routes/documents/routes.py:886-887` (404 "Extraction field not found"); contact terms only used in `services/routes/erp/*`. *(obs)* the KNT bills store `due` but no `payment_terms`. | M |
| b | Admin: approve (and related actions) did not work | **Partly.** The backend path works: *(obs)* accountant submit → admin approve → Save to records returned 200/200/201 for PI-2611-03. The UI causes are confirmed in code. Not reproduced against the hosted data. | (1) The local admin's sidebar has **no Approvals entry** (it reuses the accountant menu). The queue is reachable only through Inbox → To review → "Approval queue" tab or a dashboard tile. (2) In the queue, **Approve is disabled with no reason** whenever validation is not PASS, coverage is incomplete or the admin submitted it. (3) On the document page the admin gets **only "Approve"**, and only when validation is PASS. There is no Return or Reject there. (4) The backend also refuses approval while any non-validation exception is open (422 "Resolve the current validation findings…"). The UI does not check for this beforehand. (5) After approval, saving to records can still fail on required fields or a high-severity exception. The message then tells the admin to open the document. | FE `components/common/layout/app-shell.tsx:282`, `components/common/layout/navigation.ts:22-70`; FE `features/common/documents/approval-queue.tsx:91, 205`; FE `document-review.tsx:779-783`; `services/routes/documents/routes.py:1204-1216` (`_require_approvable_document`), `:612-700` (standardise checks). | M |
| c | AI-detected credit note shows "Unknown / Needs Review", read-only, no Choose-the-type | **Yes** | `sourceKind` maps any classified value other than INVOICE/BILL/RECEIPT to `UNKNOWN`, so a classified CREDIT_NOTE/DEBIT_NOTE becomes UNKNOWN. UNKNOWN makes every field read-only and blocks Save to records. `needsType` is false because the classification status is `classified` and no EX-002 type check fails. So the type control is not shown either, and the accountant is stuck. | FE `lib/api/document-contract.ts:242-251`; FE `document-review.tsx:420` (UNKNOWN → read-only), `:511-512` (`needsType`), `:660` ("Unknown / Needs Review"); `standardisationAvailable` FE `document-contract.ts:399-402`. | S |
| d | Receipt with tax 206.00 and total 20.60 says "Nothing to check" while two checks block approval | **Yes (mechanism)** | "Nothing to check" depends only on field signals. A field is "Please check" only when a check has `result === FAIL` **and** names that field. Blocking results that are `NOT_CHECKED`/WARNING with `preventsApproval`, or that name no field (receipt rules `RECEIPT-*`, `EX-007`), never put a field on screen. No rule compares printed tax with the printed total, so 206.00 tax against a 20.60 total passes as "read from the document". | FE `document-review.tsx:425, 695`; FE `features/common/documents/review/review-logic.ts:61-69, 77, 97-99`; FE `document-contract.ts:352` (blocking includes `preventsApproval`); `validation/documents/financial_documents.py:266-269` (`check()` has no `field`), `:431`. *(obs)* KNT document "…_BILL_NONUMBER" has a blocking `EX-007 NOT_CHECKED` with no field. | S–M |
| e | Scanned bill's stored name reads NONUMBER although the bill number was read | **Yes** *(obs, executed)* | The rename after extraction picks the identifier from a per-kind key list that leaves out `bill_number` for bills, although bills store their printed number there. The list query does include `bill_number`, so lists can differ from the stored file name. | `services/extraction/persistence/persistence.py:19-38` (bill keys lack `bill_number`), `:65-75`; `services/naming.py:83`. Executed: bill with `bill_number=INV-88123` gives identifier `None` and the name `2026-11-03_PKH_BILL_NONUMBER.pdf`. Compare `services/routes/documents/routes.py:254` (list includes `bill_number`). | S |
| f | A bank line cannot be matched to a bill already settled by a payment voucher | **Yes** *(obs)* | Bank matching only allocates the line to approved scanned records. Once PV-2026-00001 settled REC-10012 (462.00), a match returned **409 "Record allocation exceeds the unmatched amount."** Payment vouchers, payments received and other typed money documents cannot be chosen as match targets: searching "PV" returned `[]`. The accountant cannot reconcile a bank line against the payment that already posted it. | `services/routes/bank/routes.py:668-686` (search reads `standardised_records` only), `:776-824` (`match-many`), `:944-952` (the same refusal on candidate confirm); FE `features/common/money/money-flow.tsx:722`, FE `features/common/money/bank-line-actions.tsx:299-341`. | M–L |

---

## 1. Navigation and roles (applies to every page)

**Purpose:** reach every job from the sidebar. **Links/reference:** the admin needs Approvals, Users, Audit and
Records one click away.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Accountant sidebar | Inbox, Sales, Purchases, Bank, Contacts, Accounting, Reports, Settings | OK | FE `components/common/layout/navigation.ts:22-70` | – |
| Admin-only entries in the sidebar | Approvals, Users & roles, Audit trail, Check rules, System, MyInvois, Approved records | MISSING (routes exist, no menu entry) | FE `app-shell.tsx:282` uses `jobNavigation()` for LOCAL_ADMIN; routes in FE `features/localadmin/route-content.tsx:50-58`; links only on Settings, FE `features/common/settings/settings.tsx:131-141` | S |
| Approved records (`/records`) | In the menu for both roles | MISSING from the menu (reachable from a dashboard tile only) | FE `lib/constants/routes.ts:8-16`; no `records` leaf in `navigation.ts` | S |
| Breadcrumb / back | Back to the list the user came from | OK | FE `app-shell.tsx:476-500` | – |

---

## 2. Inbox › Upload & capture

**Purpose:** get received bills, receipts and supplier notes into the system. **Links:** each upload → its document;
duplicates → the earlier copy. **Missing reference information:** expected supplier, and the upload batch with its
totals.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Choose files / folder / camera / paste / drag | Many files at once, PDF and photos | OK | FE `features/common/documents/inbox/inbox-page.tsx:151-213`; FE `inbox/collect-files.ts` | – |
| Sender (AP/AR) | Choose who sent them | OK | FE `inbox-page.tsx:151-152` | – |
| Type detected, or chosen for waiting files | Auto type, manual override | OK | FE `inbox-page.tsx:208-213` | – |
| Progress, retry, remove, open | Per file | OK | FE `inbox-page.tsx:235-273` | – |
| Duplicate refused with a link to the earlier copy | "Already uploaded" + link | OK *(note: H4 D3)* | FE `inbox-page.tsx:262` | – |
| Assign to a colleague at upload | Admin assigns work | BE-ONLY | `POST/DELETE /api/documents/{id}/workload`; `getWorkload` in FE `lib/api/workflow.service.ts:173` is never used | M |
| Discard a wrong upload | Delete before it is read; exclude a duplicate or own sales copy afterwards | MISSING (delete is BE-ONLY; exclude exists only as an admin script) | `services/routes/documents/routes.py:1580` (DELETE); `services/admin/planm_tidy.py:75` (excluded_at) | S–M |
| Email-in / forwarding address | Bukku/Xero inbox address | MISSING | – | L |

## 3. Inbox › All documents (type tabs)

**Purpose:** one register of every captured document by type. **Links:** row → document; supplier → contact;
record → journal. **Missing reference information:** supplier, document date, number, amount, due date and posted
state are all in the API response but not shown as columns.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Type tabs (Bills, Receipts, Supplier CN/DN, Sales invoice/CN/DN, Other) | Counts follow filters | OK | FE `features/common/documents/document-tabs.ts`, FE `documents.tsx:160-161` | – |
| Search, status, business, exact amount, upload date | Filters | OK | FE `documents.tsx:165-193` | – |
| Columns | Supplier/customer, doc no., doc date, amount, due date, posted | MISSING (only Document, Type, Uploaded, Status, Action) | FE `documents.tsx:223-258`; the API returns `supplier`, `totalAmount`, `dueDate` (`services/routes/documents/routes.py:262`) | S |
| Display name | Date_CONTACT_TYPE_NUMBER | BROKEN for bills read under `bill_number` (bug e) | §0 e | S |
| Bulk tag / assign / export | Bulk actions | BE-ONLY (`/api/documents/bulk-action`; `bulkDocumentAction` is never called; `WorkflowControls` is never rendered) | FE `lib/api/workflow.service.ts:185`; FE `features/common/workflow/workflow-controls.tsx` | M |
| Saved filters | Keep a filter | BE-ONLY (`/api/saved-filters`) | `services/routes/workspace/routes.py:292-294` | S |

## 4. Inbox › To review lanes

**Purpose:** work queue: To review → Ready to approve → Sent for approval (admin: Approval queue).

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Lanes and counts | Three lanes, the third is the approval queue for the admin | OK | FE `document-tabs.ts:104-116` | – |
| Bulk submit (accountant) | Submit selected for approval | OK | FE `documents.tsx:195-197` | – |
| Previous / Next document, Ctrl+Enter submit | Keyboard flow | OK | FE `document-review.tsx:524-601, 612-616` | – |
| Why a document is in "To review" | Reason column | Partial (only the Needs fixing lane shows reasons) | FE `documents.tsx:240-245` | S |
| Due date / amount in the lane | To prioritise | MISSING | as §3 | S |

## 5. Inbox › Exceptions

**Purpose:** fix problems found by the checks. **Links:** exception → document field (the "Fix" button);
supplier-name exception → contact.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Filters (status, priority, reason, type, assignee, date) | Filters | OK | FE `features/common/exceptions/exceptions.tsx:71-133` | – |
| Open document | Review link | OK | FE `exceptions.tsx:156-191` | – |
| Assign (admin) | Assign to an accountant | OK | FE `exceptions.tsx:193-232` | – |
| Resolve / dismiss with a reason | Close an exception that does not apply | BE-ONLY (`PATCH /api/exceptions/{id}/resolve`; no button) | `services/routes/validation/routes.py:73` | S |
| One-click fix ("did you mean Pauh Builders?") from the list | As in the document's Checks tab | MISSING here *(note: H4 defect 3)* | Checks tab only: FE `document-review.tsx:801-814` | S |
| Excluded documents not counted | Badge matches the list | OK (fixed in PR #30) *(note)* | – | – |

## 6. Document Review page (`/documents/{id}`)

**Purpose:** compare the original with what was read, correct it, choose the type, submit (accountant) or
approve (admin). **Links it should have:** supplier → contact; duplicate → earlier document; after approval →
saved record → journal → payments/bank lines; attachments; trace. **Missing reference information:** supplier's
open balance and terms, the last bill from this supplier, the posted journal, and who approved it.

### 6.1 Header and type

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Choose the type when unsure | Shown whenever the type is unknown, disputed or not editable | BROKEN for classified credit/debit notes (bug c) | §0 c | S |
| Type notice says who chose it | Audit of the type choice | OK | FE `features/common/documents/choose-type.tsx:27-40` | – |
| Change the type after a confident read | Accountant overrides an AI type that is wrong but "classified" | MISSING (the control shows only when `needsType` or a decision exists) | FE `choose-type.tsx:39`, FE `document-review.tsx:511` | S |
| Detail preview (supplier, no., dates, totals) | Summary card | OK | FE `features/common/documents/invoice-detail-preview.tsx:14-19` | – |

### 6.2 Review tab: header fields

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Side-by-side source with highlight | Click a field → source | OK | FE `features/common/documents/review/source-pages.tsx`; FE `document-review.tsx:651-657` | – |
| Edit a stored field (revisioned, audited) | Save field | OK | FE `document-review.tsx:218-223, 480-485`; `PATCH /api/documents/{id}/extraction/{key}` | – |
| **Payment / credit terms** | Field on every payable or receivable type, with catalogue choices (COD, NET30…) | MISSING (bug a) | §0 a | M |
| **Due date** | Always visible; defaults from printed terms or the supplier's terms; editable | BROKEN: hidden while "Only fields to check" is on when it was read; absent if not stored; never defaults from the contact's terms | FE `document-review.tsx:326, 432`; §0 a | M |
| Add a field that was not read (e.g. PO number, terms) | "+ Add field" | MISSING (the API also requires the field to exist) | `services/routes/documents/routes.py:886-887` | M |
| Supplier is a known contact | Link to the contact, or "add as supplier" | Partial: add only for the admin, from the Checks tab; no link to an existing contact | FE `document-review.tsx:827-830` | S |
| Currency review | Confirm "$" currency | OK | FE `document-review.tsx:102-125` | – |
| "Nothing to check" | Only when nothing blocks | BROKEN (bug d) | §0 d | S |
| Plausibility: tax ≤ total, subtotal + tax = total | Flag swapped amounts | Partial: EX-005 needs a subtotal; there is no tax ≤ total comparison | `validation/documents/document_validation.py:162-169` | S |
| Expense account / tax code on a scanned bill | Pick a GL account before posting | Partial: the supplier's default expense account is used when posting; nothing to choose on the review page | FE `features/common/contacts/contact-detail.tsx:211`; FE `features/common/records/journal-preview.tsx` | M |

### 6.3 Review tab: line items

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Edit description, qty, unit price, amount | Per line | OK | FE `document-review.tsx:716-746`; `PATCH /api/documents/{id}/line-items/{line}/{key}` | – |
| Add a printed line that was missed | Add line | OK | FE `features/common/documents/missing-line-form.tsx`; FE `document-review.tsx:750-751` | – |
| Delete a wrongly read line | Remove line | MISSING (no endpoint, no button) | – | M |
| Line account / tax code / product | Per-line coding as in Bukku | MISSING for scanned documents (typed forms have it) | compare FE `features/common/erp/document-form.tsx:663-691` | L |
| Line corrections when type is UNKNOWN | Editable after the type is chosen | BROKEN with bug c (read-only) | FE `document-review.tsx:420, 726` | S |

### 6.4 Review actions, submit and approve

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Save draft, Save & revalidate | Buttons | OK | FE `document-review.tsx:770-771` | – |
| Save, check and submit (accountant) | One action, then the next document | OK | FE `document-review.tsx:535-592, 772-773` | – |
| Blocking notice with "View issues" | Says why it cannot be submitted | OK | FE `document-review.tsx:776` | – |
| Admin: Approve | Visible with the reason when it is unavailable | BROKEN/unclear: hidden unless validation is PASS; no reason shown | FE `document-review.tsx:779-783` | S |
| Admin: Return / Reject with a note | On the document page | MISSING (queue only) | FE `approval-queue.tsx:206-207` | S |
| Admin edits then approves | Separation of duties (another accountant must resubmit) | OK by design, explained in a note | FE `document-review.tsx:777` | – |

### 6.5 Checks tab

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| All checks, results, priority, "Fix" jump | Table | OK | FE `document-review.tsx:815-840` | – |
| Suggested fixes (apply) | One click | OK | FE `document-review.tsx:136-166, 801-814` | – |
| Record a reasoned review (EX-010, TAX-STATUS) | Note ≥ 10 characters | OK | FE `document-review.tsx:77-87, 828` | – |
| Choose type from EX-002 | Inline control | OK (only when EX-002 fails, see bug c) | FE `document-review.tsx:825` | – |
| Save to records | After approval | OK | FE `document-review.tsx:851-857` | – |

### 6.6 Saved record tab

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Show the saved values and lines | Read-only record | OK | FE `features/common/records/records.tsx:41-91` | – |
| Links: journal, payments/allocations, bank line, supplier contact, statement | Bill ↔ payments ↔ journal ↔ supplier | MISSING (`TracePanel` exists but is not used here; it is used on typed documents, journals and bank lines) | FE `features/common/trace/trace-panel.tsx`; used at FE `erp/document-form.tsx:601`, FE `ledger/ledger-page.tsx:179`, FE `money/money-flow.tsx:758` | S |
| Outstanding, paid amount, payment status | Open item state | MISSING on the record (shown in Purchases list for typed only) | FE `features/common/erp/document-list.tsx:116-119` | S |
| Post to ledger from here (admin) | Button with journal preview | Partial: in Purchases list or Records "to post" only | FE `erp/document-list.tsx:127-128`; FE `records.tsx:424-513` | S |
| Request an amendment to an approved record | Amend with approval | BE-ONLY (`POST /api/records/{id}/amendments`; `decideRecordAmendment` defined, no UI) | `services/routes/documents/routes.py:1598`; FE `lib/api/workflow.service.ts:179` | M |
| Attachments (payment slip, DO) | Attach files | MISSING for scanned documents (only typed documents have `AttachmentsPanel`) | FE `features/common/erp/documents-page.tsx:23-25` | S |

### 6.7 Activity tab

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Comments | Add/list | OK | FE `document-review.tsx:876-913` | – |
| Audit trail | Who did what | OK | FE `document-review.tsx:914-925` | – |
| Status history / versions | Old revisions, replaced file | BE-ONLY (`status-history`, `versions`) | `services/routes/documents/routes.py:1613, 1618-1620` | S |

## 7. Approvals (admin) and approved records

**Purpose:** independent approval, then saving to records and posting. **Links:** card → document; after approval →
record → journal.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Reach the queue | Sidebar entry with a count | MISSING (bug b, §1) | §1 | S |
| Approve / Return / Reject with a note | Per card | OK in the API *(obs)*; Approve is disabled silently when not eligible | FE `approval-queue.tsx:91, 205-207` | S |
| Approve selected (bulk), then save to records | Bulk | OK (frontend PR #21) | FE `approval-queue.tsx:98-122`; `services/routes/review/lanes.py:242` (`/api/review/bulk-approve` with `saveToRecords` exists, BE-ONLY) | – |
| "Changes made" list | Only real changes | BROKEN: a value changed and changed back still shows "X → X" *(note: H4 defect 2)* | FE `approval-queue.tsx:232-246` (any `correctedBy` counts) | S |
| Show checks/blocking reason on each card | Reason the Approve button is off | MISSING | FE `approval-queue.tsx:161-210` | S |
| Load time | Fast with many documents | Risk: 3 calls per document plus a 15 s poll | FE `approval-queue.tsx:45-59` | M |
| Approved records list with "to post" filter and bulk post | Records page | OK (via tile link `?posting=to-post`) | FE `records.tsx:100-125, 424-513` | – |
| Export approved records (Excel) | Export | OK | FE `records.tsx:225, 253` | – |

## 8. Sales: quotations, invoices, credit/debit notes, payments received

**Purpose:** type what KNT issues. **Links:** quotation → invoice; invoice ↔ payments, CN/DN, journal, customer,
PDF copies, attachments, trace. **Missing reference information:** customer's credit limit, overdue balance
on the form, and a link to the exact journal (it opens the journal list).

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Lists with search, status, date range, source (typed/scanned), paging | Filters | OK | FE `features/common/erp/document-list.tsx:44-61` | – |
| List columns: number, date, contact, amount, outstanding, payment, status, ledger | Columns | OK; **due date column MISSING** | FE `document-list.tsx:106-123` | S |
| New invoice: customer, date, payment terms, due date, reference, currency, lines, typed tax, account | Full form | OK | FE `erp/document-form.tsx:474-559` | – |
| Payment terms → due date | Catalogue terms set the due date | OK | FE `document-form.tsx:508-517` | – |
| Quotation flow: send, accept, expire, convert to invoice | Status actions | OK; converted invoice has no due date *(note: H4 defect 7)* | FE `erp/document-logic.ts:91-101` | S |
| Issue / void (admin), submit for approval (accountant) | Separation | OK | FE `document-logic.ts:110-119` | – |
| Receive payment, credit note, debit note from an issued invoice | Transfers | OK; hidden when nothing is outstanding | FE `document-logic.ts:103-107, 127-131` | – |
| Allocations (payment to several invoices) | Allocate | OK | FE `document-form.tsx:746-772` | – |
| PDF preview/print/download, stored copies | Printed copies | OK *(note: sales sweep)* | FE `document-form.tsx:836-848` | – |
| Share link, MyInvois prep (admin) | Sharing | OK (behind a flag) | FE `features/common/erp/share-links.tsx`; FE `erp/myinvois-page.tsx` | – |
| History, trace, attachments | Panels | OK | FE `document-form.tsx:599-601, 864-912`; FE `erp/attachments-panel.tsx` | – |
| "Posted to the ledger" link | Opens that journal | Partial: opens the whole journal list | FE `document-form.tsx:792-801` | S |
| Draft/pending shows Outstanding/UNPAID before it is posted | Show only once issued | BROKEN (cosmetic) *(note: H4 defect 9)* | FE `document-form.tsx:802-805` | S |
| Sales orders, delivery orders, refunds | In the menu | MISSING from the sidebar (URL `?type=` only, by decision) *(note)* | FE `navigation.ts:30-36` | S |
| Recurring invoices, reminders | Bukku/Xero standard | MISSING | – | L |

## 9. Purchases: bills, expense receipts, supplier CN/DN, payments made (payment vouchers)

**Purpose:** what KNT owes and pays. **Links:** bill ↔ supplier, PV(s), journal, bank line, attachments, original
scan. **Missing reference information:** supplier's open bills when paying, and which bank line cleared the PV.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Bills list shows typed and scanned bills | One list | OK | FE `document-list.tsx:113, 123-129` | – |
| Scanned bill: open the scan, post to ledger (admin) | Row actions | OK | FE `document-list.tsx:125-128` | – |
| Scanned bill: "Pay" / "Make payment" | From the bill | MISSING (go to Payments made → New and allocate) | FE `document-list.tsx:123-129` | S |
| PO → goods received → bill | Chain | OK (URL types) *(note)* | FE `lib/api/erp.service.ts:113-116` | – |
| PO conversion takes the supplier's terms | Due date set | BROKEN for CSV-imported terms *(note: Codex P1 review P2)* | `services/routes/erp/documents.py:82` | S |
| Payment voucher: allocate to typed and scanned bills, bank account, post | PV | OK *(obs: PV-2026-00001 settled REC-10012)* | `services/routes/erp/documents.py:560-620` | – |
| Expense receipts | Captured receipts list | OK (Inbox list with `tab=receipts`) | FE `navigation.ts:39` | – |
| Supplier credit note applied to a bill | Allocation | OK for typed; scanned notes optional | FE `document-form.tsx:813-831` | – |
| Due date from supplier terms on scanned bills | Default | MISSING (§0 a) | – | M |
| Bills due/overdue tiles | Dashboard | OK (scanned bills without a due date are not counted, see §0 a) | FE `lib/today.ts:168-181` | – |

## 10. Contacts and statement of account

**Purpose:** customers and suppliers with their terms and balances. **Links:** contact → every invoice/bill
(typed **and** scanned), payments, statement. **Missing reference information:** outstanding balance, overdue
amount and last payment, on the list and the detail page.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| List with tabs, search, new contact | List | OK | FE `features/common/contacts/contacts.tsx:50-125` | – |
| Balance / overdue column | Bukku "Receivable/Payable" column | MISSING | FE `contacts.tsx:73-79` | M |
| Detail: legal name, code, SSM, TIN, SST, email, phone, address, remarks | Fields | OK | FE `contact-detail.tsx:137-178` | – |
| Payment terms, default expense account | Defaults | OK | FE `contact-detail.tsx:191-221` | – |
| Currency, credit limit, bank details for payments | Fields | MISSING (currency only in seed `fields`) | – | S |
| Related documents | All typed and scanned documents for the contact | Partial: only **captured Inbox** documents, matched **by name**, not typed invoices/PVs | FE `contact-detail.tsx:25, 38, 114-125`; FE `contact-logic.ts:155-164` | M |
| Statement of account (range, ageing, PDF) | SOA | OK; Codex P1 review flagged unposted allocations and name matching *(note, check whether PR #40 fixed them)* | FE `contact-statement.tsx:122-173`; `services/routes/reporting/statement.py` | – |
| History of changes; deactivate/reactivate | Audit | OK | FE `contact-detail.tsx:87-110` | – |
| Merge duplicate contacts; supplier/customer name matches | Matching | BE-ONLY (`/api/supplier-matches`, `/api/customer-matches`) | `services/routes/workspace/routes.py:295-296`; `services/routes/documents/routes.py:1616-1617` | M |

## 11. Products / catalog

**Purpose:** products, tax codes, payment terms, bank accounts, posting accounts, currencies.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Catalog kinds with add/edit/deactivate | CRUD | OK | FE `features/common/erp/catalog-page.tsx:46-122, 207-212` | – |
| Products: SKU, unit, sale/purchase price, default tax code, accounts | Product card | OK | FE `erp/products-panel.tsx` | – |
| Tax codes are labels only (never calculated) | Owner rule | OK | FE `catalog-page.tsx:29` | – |
| Stock quantity movements | Inventory | MISSING (types exist in the enum, no pages) | FE `lib/api/erp.service.ts:90` | L |
| Where-used (product on invoices) | Link | MISSING | – | M |

## 12. Bank: accounts, money in/out, transfers, reconciliation, import, matching

**Purpose:** get every bank line explained and the bank balance agreed. **Links:** bank line ↔ matched
bill/invoice, **↔ PV/OR that already recorded it**, ↔ journal; account → its ledger. **Missing reference
information:** the statement closing balance next to the ledger balance on the account list, and the
unreconciled payments (PV/OR not yet seen on a statement).

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Bank accounts with ledger balance, imported lines, last date | List | OK | FE `features/common/money/bank-accounts.tsx:140-170` | – |
| Add bank account (admin) | Via catalog | OK | FE `bank-accounts.tsx:140-141` | – |
| Statement import: CSV/PDF, column mapping, preview, confirm/discard | Import | OK; account label must be typed every time *(note: H4 defect 4)* | FE `money-flow.tsx:49-55, 612-622`; FE `reconciliation-page.tsx:263, 457-467` | S |
| Suggested matches: confirm / reject | Review modal | OK | FE `reconciliation-page.tsx:326-399` | – |
| Admin: confirm and post the payment in one step | Post | OK (PR #22) | FE `bank-line-actions.tsx:150`; `GET /api/bank/transactions/{id}/payment-preview` | – |
| Manual match to one or several records | Search + split | OK for scanned records | FE `bank-line-actions.tsx:299-341` | – |
| **Match to an existing PV / payment received / money out** | Reconcile against the document that already posted the cash | MISSING (bug f) | §0 f | M–L |
| Match to typed invoices/bills | Typed open items | MISSING (search covers `standardised_records` only) | `services/routes/bank/routes.py:676-685` | M |
| Code a line to an account (fees, interest) | Coding | OK | FE `money-flow.tsx:696-697`; `POST /api/bank/transactions/{id}/coding` | – |
| Record expense / money in from a line | Create document | OK | `services/routes/bank/routes.py:730+`; FE `bank-line-actions.tsx:208` | – |
| Ignore a line (duplicate, opening) | Ignore | OK ("Ignore row") | FE `money-flow.tsx:256, 755`; `services/routes/bank/routes.py:1131` | – |
| Transfers between accounts | Bank transfer type | OK | FE `navigation.ts:47`; FE `erp/document-logic.ts:7` | – |
| Period reconciliation: statement balances, sign-off, reopen, lock | Month-end | OK | FE `reconciliation-page.tsx:271-290`; migration 019 | – |
| Reconciliation report (unpresented payments, uncredited receipts) | Printable rec | MISSING | – | M |
| Bank rules (auto-code recurring lines) | Rules | MISSING (suggestions endpoint is BE-ONLY) | `GET /api/bank/reconciliation/suggestions` | M |

## 13. Accounting (ledger): chart of accounts, journals, general ledger, periods

**Purpose:** see and adjust the books. **Links:** journal → source document/record/bank line; account → GL.
**Missing reference information:** journal lines (debit/credit by account) and the source document number.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Chart of accounts | The ledger accounts journals use (1010, 2000, 5000…) with balances | BROKEN/confusing: the section shows the bank-coding category table ("Categories used when coding bank activity. Journals use ledger accounts."), not the ledger accounts | FE `features/common/ledger/chart-accounts.tsx:42`; FE `ledger-page.tsx:205`; ledger accounts at `GET /api/ledger/accounts` | M |
| Edit / deactivate an account | Maintain the chart | BE-ONLY (`PATCH`/`DELETE /api/chart-of-accounts/{id}`) | `services/routes/accounting/routes.py:1144-1145` | S |
| Journals list: filters (status, source, amount, month), trace, reverse (admin) | List | OK | FE `ledger-page.tsx:126-197` | – |
| Journal detail: lines, source document link | Open a journal | MISSING (totals only; "Record" column shows an id with no link) | FE `ledger-page.tsx:163-170, 200` | M |
| Manual journal (admin) | Two-line form | Partial: one debit and one credit line only | FE `ledger/journal-posting-form.tsx:89-104` | M |
| Typed "Journal entry" document | Multi-line journal with approval | UI-ONLY in the type list (no menu entry, no page) | FE `lib/api/erp.service.ts:90-93, 125` | M |
| General ledger by account and date range, CSV/Excel | GL | OK | FE `ledger-page.tsx:55-57`; FE `reports/financial-reports.tsx:58-95` | – |
| Trial balance | TB | OK (under Ledger, not Reports) | FE `ledger-page.tsx:152-157` | – |
| Periods: add, lock, reopen with a reason | Month-end | OK; can be locked out of order *(note: H4 defect 8)* | FE `ledger/periods.tsx:71-125` | S |
| Month-end checklist | Steps | OK (dashboard month-end steps) | FE `lib/today.ts:236+` | – |
| Opening balances | Enter or import | Partial: company-pack import only (`/api/imports/*`), no page | `services/routes/imports/routes.py:124-126` | M |
| Cost centres | Maintain | BE-ONLY (`/api/cost-centres` PATCH/DELETE) | `services/routes/accounting/routes.py:1146-1149` | S |

## 14. Reports: business summary, financial reports, month pack, performance evaluation

**Purpose:** figures from posted books. **Links:** report line → GL → journal → document. **Missing reference
information:** comparative periods and drill-down.

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| P&L, balance sheet, GL, AR/AP ageing, tax summary, SOA | Tabs with date filters | OK | FE `features/common/reports/accounting-reports.tsx:13-21` | – |
| Download CSV / Excel | Export | OK | FE `financial-reports.tsx:58-62` | – |
| PDF export of P&L / BS | Printable | MISSING (SOA has PDF) | FE `financial-reports.tsx:58-62` | M |
| Drill-down from a figure | Click → GL lines → journal | MISSING (no links in the report tables) | FE `reports/financial-report-tables.tsx` | M |
| Comparative period / budget | Columns | MISSING | – | M |
| Cash-flow statement | Report | MISSING | – | L |
| Business summary (owner view) | Sales, profit, owed by/to us, bank, trend | OK; Codex review flagged AR/AP using unposted allocations and missing bank accounts *(note, check whether PR #40 fixed them)* | FE `reports/business-summary.tsx`; `services/routes/reporting/business_summary.py:88, 146` | – |
| Month pack (Excel per period) | Download | OK | FE `financial-reports.tsx:101-125` | – |
| Performance evaluation, effort study | Reading accuracy, time saved | OK | FE `reports/evaluation-page.tsx`, FE `reports/effort-study.tsx` | – |

## 15. Settings, users and roles

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Company profile (admin) | Name, SSM, address, logo for PDFs | OK | FE `features/common/settings/company-profile.tsx` | – |
| Workspace preferences, change password | Settings | OK | FE `settings.tsx:161-258` | – |
| Check rules (admin) | Validation rules | OK | FE `settings.tsx:34-95` | – |
| Users: invite, edit, disable, cancel invitation, view | User management | OK | FE `features/common/users/user-management.tsx:102-427` | – |
| Change a user's role (accountant ↔ local admin) | Role change | MISSING in the UI (edit says "Save accountant") | FE `user-management.tsx:377-398` | S |
| Per-user permissions | Fine-grained | Partial (`/api/users/{id}/permissions` used) | FE `user-management.tsx` | – |
| Audit trail (admin) | Filterable log | OK (reached from Settings only) | FE `features/common/monitoring/monitoring.tsx` | – |
| Custom fields, document tags | Configure | BE-ONLY or unused (`/api/custom-fields` used by catalog; tags UI never rendered) | FE `workflow-controls.tsx` | S |
| Backups | Request/download | Partial (`GET /api/backups/{id}` unused) | `services/routes/reporting/routes.py:155-157` | S |

## 16. Dashboard, global search, notifications

| Function | Expected behaviour | Status | Evidence | Fix |
|---|---|---|---|---|
| Work tiles (to review, needs fixing, ready, waiting approval, bank to match, bills due/overdue, customers overdue, to post, payments to post) | Tile → filtered list | OK | FE `lib/today.ts:102-200`; `services/routes/home/routes.py:60-80` | – |
| KPI figures and monthly finance, drag layout | Owner view | OK | FE `features/common/today/dashboard-page.tsx`, FE `today/monthly-finance.tsx` | – |
| Global search by number, contact, reference, exact amount | Grouped results | OK; exact numbers can be pushed out by newer partial matches *(note: Codex review P2)* | FE `components/common/layout/global-search.tsx:22-55`; `services/routes/search/routes.py:88` | S |
| Notifications bell: unread count, open the resource, mark read | Bell | OK | FE `app-shell.tsx:312-335, 560` | – |
| Mark all read; notifications page | Housekeeping | MISSING | – | S |
| Review reminders | Remind an accountant | BE-ONLY (`POST /api/review-reminders`) | `services/routes/review/routes.py:90` | S |

## 17. Backend endpoints with no UI (checked by hand from the automatic comparison)

From 242 backend routes, these have no caller in the frontend: `/api/documents/{id}/workload` (assign/unassign),
`/api/documents/{id}/supplier-matches`, `/customer-matches`, `PATCH /api/supplier-matches/{id}` and
`/customer-matches/{id}`, `/api/documents/{id}/processing-history`, `DELETE /api/documents/{id}`,
`/api/documents/bulk-action`, `/api/saved-filters`, `/api/document-tags*`, `/api/documents/{id}/tags/{tag}`,
`/api/documents/{id}/status-history`, `/api/documents/{id}/versions*`, `POST /api/records/{id}/amendments`,
`/api/record-amendments/{id}/decision`, `PATCH /api/exceptions/{id}/resolve`, `/api/review/bulk-approve`,
`/api/review/bulk-submit`, `GET /api/bank/reconciliation/suggestions`, `POST /api/bank/reconciliation/decisions`,
`PATCH/DELETE /api/chart-of-accounts/{id}`, `PATCH/DELETE /api/cost-centres/{id}`, `GET /api/backups/{id}`,
`POST /api/review-reminders`, `GET /api/accountant/inbox`. (Some service functions exist in
`src/lib/api/workflow.service.ts` but no page calls them.)

---

## 18. Prioritised fix list

### P0: demo-blocking and approval-blocking

| # | Fix | Bug | Where | Size |
|---|---|---|---|---|
| 1 | **Approvals reachable and explained for the admin:** Approvals (with count), Users and Audit in the admin sidebar; on each card and on the document page show *why* Approve is unavailable (validation not PASS, coverage incomplete, open exception, own submission); add Return/Reject with a note to the document page; show the 422 reason when the server refuses. | b | FE `navigation.ts`, `app-shell.tsx:282`, `approval-queue.tsx`, `document-review.tsx:779-783` | M |
| 2 | **Credit/debit notes from the Inbox:** `sourceKind` accepts CREDIT_NOTE/DEBIT_NOTE (the backend already standardises them), and the type control shows whenever the kind is not editable, or on request ("Change type"). | c | FE `document-contract.ts:242-251`, `document-review.tsx:511`, `choose-type.tsx` | S |
| 3 | **Never say "Nothing to check" while something blocks:** list blocking checks without a field (or with WARNING/NOT_CHECKED) at the top of the Review tab, with a link to the Checks tab; add a printed-value plausibility check (tax must not exceed total; flag when subtotal + tax ≠ total even without line items). This compares printed values, it does not calculate tax. | d | FE `document-review.tsx:683-698`, `review-logic.ts`; `validation/documents/financial_documents.py`, `document_validation.py:162` | S–M |
| 4 | **Payment terms and due date on the review page:** always show Due date and Payment terms for bills, invoices and notes (create the empty field like the type choice does for `bill_number`); allow "+ Add field" for correctable keys; default the due date from printed terms, else from the supplier's terms (decision Q1). | a | `field_catalog.py:235`, `services/routes/documents/routes.py:830-890`, FE `document-review.tsx` | M |
| 5 | **Reconcile a bank line against an existing PV / payment received / money-out document** (link the line, no new allocation or journal), and include typed open items in the match search. | f | `services/routes/bank/routes.py:668-824`, FE `bank-line-actions.tsx`, `reconciliation-page.tsx` | M–L |
| 6 | **Stored names use the bill number:** add `bill_number` (and `bill_reference`) to the bill keys in `_usable_document_identifier`; rename after a correction; add a one-off repair for existing `NONUMBER` names. | e | `services/extraction/persistence/persistence.py:19-38` | S |

### P1: accountant daily-use gaps

| # | Fix | Where | Size |
|---|---|---|---|
| 7 | Saved record tab: Trace panel (journal, payments, bank line), outstanding and payment status, supplier link, attachments, "Post to ledger" for the admin, "Request amendment". | FE `records.tsx:41-91`, `document-review.tsx:862-874` | M |
| 8 | Inbox lists: supplier, document no., document date, amount, due date and posted columns; "Pay" from a scanned bill. | FE `documents.tsx:223-258`, `erp/document-list.tsx:123-129` | S |
| 9 | Exceptions: Resolve/dismiss with a reason and the one-click supplier fix on the list. | FE `exceptions.tsx`; `PATCH /api/exceptions/{id}/resolve` | S |
| 10 | Contacts: balance and overdue columns; related documents include typed invoices, bills and payments, matched by contact id rather than name. | FE `contacts.tsx`, `contact-detail.tsx:114-125`, `contact-logic.ts:155` | M |
| 11 | Journals: open a journal to see its lines and its source document; the "Posted to the ledger" link opens that journal. | FE `ledger-page.tsx`, `erp/document-form.tsx:792-801` | M |
| 12 | Chart of accounts shows the ledger accounts (with balances) and allows edit/deactivate; keep the bank-coding categories separate. | FE `chart-accounts.tsx`, `ledger-page.tsx:205` | M |
| 13 | Discard or exclude a wrong Inbox upload (delete before reading, exclude with a reason after). | `DELETE /api/documents/{id}`, `planm_tidy` logic | S–M |
| 14 | Records in the sidebar for both roles. | FE `navigation.ts` | S |
| 15 | Approval queue "Changes made" compares the original and current value; load reviews in one call. | FE `approval-queue.tsx:45-59, 232-246` | S–M |
| 16 | Statement import remembers or prefills the bank account. | FE `money-flow.tsx`, `reconciliation-page.tsx` | S |
| 17 | Due date on invoices converted from quotations and on POs with imported terms. | `services/routes/erp/documents.py:82` | S |
| 18 | Delete a wrongly read line item. | new endpoint + FE `document-review.tsx:716-746` | M |

### P2: nice to have

Report drill-down and PDF export for P&L/BS; comparative periods; reconciliation report; bank rules; multi-line manual
journal page; recurring invoices and reminders; role change in Users; mark-all-read; saved filters, tags, assignment
UI; due-date column in Sales/Purchases lists; periods must be locked in order; cash-flow statement; stock movements.

### Top 10 (for the report back)

1. Approvals for the admin: sidebar entry, reason when Approve is unavailable, Return/Reject on the document page (bug b, M).
2. Credit/debit notes read by the AI stuck as "Unknown", read-only, with no type control (bug c, S).
3. "Nothing to check" while blocking checks are open, plus a tax ≤ total plausibility check (bug d, S–M).
4. Payment terms and due date editable on every scanned bill, invoice and note, defaulting from supplier terms (bug a, M).
5. Match a bank line to the PV / payment that already recorded it (bug f, M–L).
6. Stored name uses `bill_number` instead of NONUMBER (bug e, S).
7. Saved record links: journal, payments, bank line, supplier, attachments (M).
8. Inbox lists show supplier, number, dates, amount and due date (S).
9. Exceptions: resolve/dismiss and the one-click supplier fix (S).
10. Contacts: balance column and related typed documents (M).

---

## 19. Questions for Jonathan (product decisions)

1. **Due date when it is not printed:** should a scanned bill take its due date from the supplier's payment terms
   (date arithmetic, not tax)? Or should it stay empty until the accountant types it?
2. **Payment terms on receipts:** receipts are paid on the spot. Should they show terms at all, or "Paid" with the
   payment method?
3. **Changing the type after a confident read:** may the accountant always override the AI's type before submitting,
   or only when the type is uncertain?
4. **Bank line vs payment voucher:** when a PV already posted Dr AP / Cr Bank, should matching the bank line only mark
   both as reconciled? And should a PV count as "unreconciled" until a statement line confirms it?
5. **One-person companies:** should a local admin be able to approve their own submission when the company has
   only one user, with an audit note? (Today another person must always approve.)
6. **"Only fields to check" on by default:** keep it, or always show the key fields (supplier, number, dates, terms,
   totals)?
7. **Admin sidebar:** a separate admin menu (Approvals, Users, Audit, Rules, System), or the accountant menu plus an
   "Admin" group?
8. **Who may discard an Inbox upload:** the uploading accountant before it is read, an admin at any time, or both?
9. **Scanned supplier credit notes:** should they be applied to the bill they name automatically when approved, or
   always allocated by hand?
10. **Tax plausibility:** is flagging "printed tax is greater than the printed total" acceptable under the
    no-tax-calculation rule? It only compares two printed values.
