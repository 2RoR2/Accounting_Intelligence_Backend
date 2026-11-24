# Demo script: AccordAI on the hosted site (10-12 minutes)

Company: **Ketapang Nibong Trading Sdn. Bhd. (KNT)**, a fictional trading company. Books Sep-Nov 2026 are seeded, so
every number below starts from a real-looking position. Every figure here was produced by running this exact
sequence on a fresh local copy of the KNT books (backend and frontend `features` at 24 Nov 2026). Where the hosted
site differs, trust the screen, not this page, and say nothing about the difference.

Rules to keep in mind while speaking: tax is **never calculated** by the system (SST is typed as printed); nothing is
submitted to LHDN; every approval is a person's decision.

## Before you start (do this at 12:00-12:30, not at 12:55)

1. **Wake the backend 5 minutes ahead.** Open <https://accounting-intelligence-frontend.vercel.app> and sign in once;
   the Render service sleeps and the first request can take about a minute.
2. **Sign in with both accounts in two different browsers** (or one normal and one private window):
   accountant (emailed 6-digit code, valid 60 s) and local admin (password plus authenticator app).
3. **Put the files on the desktop** (all in `docs/demo/pack/` of the backend repository; copy the folder):
   `1_invoice_Seladah_SCC-2611-0412.pdf`, `2_scanned_bill_Kenyalang_KT-BL-2611-3358.jpg`,
   `3_receipt_photo_Rengas_RS-088412.jpg`, `4_credit_note_Pauh_PBS-CN-2611-017.pdf`,
   `6_bank_statement_PublicBank_9901_2026-11-24.csv`, `7_counter_receipt_Kenyalang_payment.jpg`
   (`5_transfer_slip_Seladah_payment.jpg` is an optional prop to show on screen when you explain the bank line).
   `expected_values.json` lists what each file should read as.
4. **Check the AI budget** (see "What can go wrong"). If the AI reading is not available, pre-upload now and use the
   fallbacks.
5. **Pre-upload at 12:30, one file at a time, 30 seconds apart.** Do not upload the whole pack at once: the AI
   provider allows only about 8,000 tokens a minute and a burst makes the later files fail. Leave documents 1 and 2
   un-submitted so you can still show upload live; submit nothing yet. (If you pre-upload everything, upload the
   *credit note* live instead, it is the file that fails most often under a rate limit.)
6. Hosted data must be at the "before" numbers below. If someone already ran the demo files, ask for a data reset
   before 13:00, because the same file cannot be uploaded twice ("already uploaded").
7. Have screenshots of each step in a folder (see "Network down").

### "Before" numbers (Dashboard, as at 23 Nov 2026)

| Item | Value |
|---|---|
| Receivables (AR) | RM 71,450.13 |
| Payables (AP) | RM 56,751.38 |
| Net profit, November | RM 12,269.30 |
| Cash in hand (1000) | RM 10,621.04 |
| Public Bank Current Account ••9901 (1010) | RM 25,368.76 |
| Bank statement balance 23 Nov | RM 25,538.76 (RM 170.00 of unidentified transfers carried) |

## The script

### 1. The Inbox: capture a received supplier invoice (accountant, about 3 minutes)

- **Inbox > Upload & capture**. Upload `1_invoice_Seladah_SCC-2611-0412.pdf`. Say: "Text is read from the PDF, one AI
  call structures it, nothing is approved automatically." Expect **about 5-10 seconds**.
- Open it (**All documents > Review**). Show the original on the right and the fields on the left; click a field to
  see where it was read. Expect: supplier Seladah Cement & Concrete Sdn. Bhd., invoice SCC-2611-0412, date 2026-11-19,
  due 2026-12-19, subtotal 5,976.00, tax 298.80, total 6,274.80, three lines (200 bags cement, 80 bags mortar, 20
  tonnes sand). Validation passes; the supplier is matched to the one in Contacts.
- **Correct one field** on purpose (for example Payment terms "30 days" to "Net 30 days") and say "every correction
  is kept with who and when". Then **Save, check and submit** (Ctrl+Enter).
- Upload the **scanned bill** `2_scanned_bill_...jpg` (a skewed photo, about 8 s): Kenyalang Telecom, bill
  KT-BL-2611-3358, subtotal 427.60, service tax 34.21, total 461.81. Submit it.
- Upload the **receipt photo** `3_receipt_photo_...jpg` (about 10-12 s): Rengas Stationers, RS-088412, 21 Nov,
  subtotal 92.50, SST 9.25, total 101.75, cash. If a field says "Please check" (the date has done so), accept or
  correct it, then submit.
- Upload the **credit note** `4_credit_note_...pdf`: Pauh Builders Supply, PBS-CN-2611-017, 20 Nov, 150.00 + 15.00 =
  165.00, refers to PI-2611-01. **Check the two number fields**: the AI can put the credit note's own number and the
  original invoice number the wrong way round. Correct if so, then submit.

### 2. Admin approves; records and journals (about 2 minutes)

- Switch to the admin window. **Approvals**: select the four submitted documents, **Approve** (this also saves them
  to records). Say: "the accountant prepares, a different person approves".
- **Approved records** (or the Dashboard tile "journals to post"), preview then **Post selected** for the
  Seladah invoice, the Kenyalang bill and the credit note:

| Document | Journal |
|---|---|
| Seladah SCC-2611-0412 | Dr 5000 Purchases 5,976.00; Dr 1140 SST input tax 298.80; Cr 2000 Trade payables 6,274.80 |
| Kenyalang KT-BL-2611-3358 | Dr 6130 Telephone and Internet 427.60; Dr 1140 34.21; Cr 2000 461.81 |
| Pauh credit note | Dr 2000 165.00; Cr 5000 150.00; Cr 1140 15.00 |

  The Rengas receipt is approved and kept as a record; it has no journal (cash expenses are booked through Money
  out or a journal). It is not in the "to post" list.

### 3. Pay the telephone bill: Payment Voucher (about 1.5 minutes)

- Accountant: **Purchases > Payments made > New**: supplier Kenyalang Telecom, date 24 Nov, pay from **Cash in
  hand**, allocate RM 461.81 to bill KT-BL-2611-3358. Save, send for approval. Admin: open it and **Issue**.
  Result **PV-2026-00001**; open the **PDF** ("PAYMENT VOUCHER", Pay to Kenyalang Telecom, 461.81).
- **Attach** `7_counter_receipt_Kenyalang_payment.jpg` as payment proof. The bill now shows paid.

### 4. Bank statement, matching and reconciliation (about 3 minutes, admin)

- **Bank > Reconciliation > Upload bank statement**: file `6_bank_statement_...csv`, bank account `000026119901`
  (type it, the dialog does not prefill it), date format ISO, columns auto-mapped. Confirm: **2 lines**, one match
  proposed.
- Open the line **IBG TRANSFER SELADAH CEMENT & CONC -6,274.80**: it suggests the Seladah invoice (exact amount,
  reference SCC-2611-0412, difference 0.00). Click **Confirm and post payment**: Dr 2000 6,274.80 / Cr 1010 6,274.80,
  line shows Matched, "Payment posted". Show `5_transfer_slip` here if you want a prop.
- Line **SERVICE CHARGE -8.00**: **Record as expense**, account **6230 Bank Charges**.
- **Period reconciliation > New period**: 24 Nov - 30 Nov 2026, account 000026119901, opening 25,538.76, closing
  19,255.96, book balance 19,085.96, statement adjustment -170.00 with the evidence "Unidentified statement transfers
  awaiting remittance details". Expect **unexplained difference 0.00**, 0 unaccounted lines. **Complete** it.
  Say: "the RM 170.00 are the unidentified September-November transfers we are still chasing, shown, not hidden".
- Never pay a bill by Payment Voucher **and** then match the same bill to a bank line (see "Known limits"): in this
  demo the telephone bill is paid in cash, the cement invoice by bank.

### 5. Sales: quotation to invoice to part payment to receipt (about 2 minutes, admin)

- **Sales > Quotations > New**: customer Ramin Interior Design Sdn. Bhd.; line 1 Portland cement 50 kg, 100 x 24.50,
  tax 122.50 (SST-5); line 2 "Delivery to site (Kuching)" 1 x 150.00, tax 0.00. Total **2,722.50**. Issue
  (QT-2026-00001).
- **Convert to invoice**. **Set the due date yourself (24 Dec 2026)**: the converted invoice does not copy the
  customer's terms. Issue: **IV-2026-00012**.
- **Payments received > New**: 1,500.00 into **Cash in hand**, reference "Cheque 441207", allocate to IV-2026-00012.
  Result **OR-2026-00001**; open the **Official Receipt PDF**. The invoice now shows **outstanding 1,222.50,
  partly paid**. On the customer's page, **Statement of account** lists the invoice, the receipt and the 1,222.50
  balance.

### 6. Reports reflect it (about 1 minute)

**Reports > Business summary**, November to date, should now read:

| Item | Before | After |
|---|---|---|
| Sales | 42,506.30 | 45,106.30 |
| Expenses | 30,237.00 | 36,498.60 |
| Net profit | 12,269.30 | 8,607.70 |
| Receivables | 71,450.13 | 72,672.63 |
| Payables | 56,751.38 | 56,586.38 |
| Cash in hand | 10,621.04 | 11,659.23 |
| Public Bank ••9901 | 25,368.76 | 19,085.96 |
| Recorded input tax / output tax | 2,665.55 / 3,780.03 | 2,983.56 / 3,902.53 |

(Why: sales +2,600.00; expenses +5,976.00 +427.60 -150.00 +8.00; payables +6,274.80 -6,274.80 +461.81 -461.81 -165.00
(credit note); net profit = 12,269.30 + 2,600.00 - 6,261.60.) Open **Financial reports** (P&L: Revenue 45,106.30 for
November) and **Month pack** with period `2026-11`. The AP ageing (**Reports > Financial reports > Ageing >
Payables**) keeps the older Seladah, Matang, Kandis and Sarikei items; the new Seladah invoice and the Kenyalang
bill are not on it because they are paid.

Close with the accountant's side: "everything above was typed once, matched once, approved by a second person."

## What can go wrong, and what to do

| Symptom | Fallback |
|---|---|
| **AI rate-limited: a document says "Needs fixing" / "AI unavailable" or takes about 40 seconds** (primary fallback) | **Open an already-extracted document** (one from the 12:30 pre-upload, or any seeded Inbox document marked "Read, to check") and carry on the script from the review screen. The AI provider's daily or per-minute token limit is used up (Groq on_demand: 200,000 tokens a day, 8,000 a minute; about 2,500 per document); do not retry in a loop. |
| No already-extracted document is available | Open the stuck document, **Choose the type**, type the fields from `expected_values.json`, add the lines, Save and revalidate, then submit. This is the manual path and it works; say so. |
| Extraction slow but running | Show an already-extracted document (step 5 of "Before you start") and keep talking about the review screen. |
| Same file "already uploaded" | Open the existing document from **All documents**. |
| Receipt will not submit ("Check against the document: date") | Retype the date as 2026-11-21 and revalidate; resubmit. |
| Render service asleep (first click hangs) | Wait 60 seconds, refresh; this is why you wake it up at 12:50. |
| Network or hosted site down | Show the screenshots, then the PDFs in `docs/demo/pack/` and `expected_values.json`. The local rehearsal showed all steps pass. |

## Known limits (do not click into these during the demo)

- Pay one bill **either** by Payment Voucher **or** by bank match, not both: matching a bank line to a bill already
  settled by a voucher is refused with "already settled" (a clear message since this branch; before it, a 503).
- Expense receipts are recorded and approved but have no one-click journal.
- The converted invoice has no due date until you set it; the dashboard "Outstanding" is shown before posting.
- Statement lines dated after an existing reconciliation period need a new period (step 4).
- The AI sometimes swaps a credit note's number and the original invoice number; check both.
- The detail page title of a scanned bill can read "NONUMBER" while the list shows the right name.
