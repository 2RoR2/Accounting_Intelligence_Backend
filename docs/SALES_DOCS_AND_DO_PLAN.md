# Sales documents, live preview, AI-assisted entry and delivery orders: plan

Date: 25 Nov 2026 · Status: **plan only, no code** · Base: backend `features` a302ade, frontend `origin/features` ecd317b
Prompted by Jonathan's questions on the New quotation form (hosted UAT, KNT). Builds on
`docs/COMPETITOR_AND_AI_ROADMAP.md` and `docs/ACCOUNTANT_FUNCTION_AUDIT.md` §8; it does not repeat them.

House rules this plan keeps: **tax is never calculated** (amounts and SST are typed as printed); **no LHDN/MyInvois
submission** (future scope); free or existing services only (any paid API needs Jonathan's approval); competitor
facts are cited, and a claim we could not verify is marked *n/v*.

---

## 0. Short answers to Jonathan's four questions

| Question | Answer |
|---|---|
| Are the quotation's fields enough? | **Nearly, but not yet.** Identity, number, date, reference, currency, lines, discount, tax-code label, bank details and company terms already print. Missing for a proper Malaysian quotation: **a validity date** (the form never shows one, and the PDF never prints it), **an attention person**, **a delivery/site address**, **per-document terms**, and the **customer's SST number on the PDF** (the form shows it, but the PDF drops it). Salesperson and project are optional. See §1. |
| Can we preview what we typed before saving? | Yes: a **live preview panel** that draws the same layout as the PDF while you type (§2). Today "Preview PDF" works only after a save, because the server renders a saved draft. |
| What does Save do? | **Save keeps an editable draft. Nothing is sent, printed or posted.** But it **takes the next number** (e.g. QT-2026-00012) at the first save (`documents.py:886`), so deleting a draft leaves a gap. **Send** (quotation) or **Issue** (the others) freezes the document, stores a copy of the PDF that cannot be changed, and, for invoices, notes and receipts, posts to the ledger. The plan renames the button "Save draft" and adds one line of help text (§2.3). |
| Same for invoices and the other sales documents? | Yes. One form drives all of them (`document-form.tsx`), so the preview, the field additions and the assists apply to every type in one change. Per-type differences are in §1. |
| Delivery orders: needed? Created or extracted? | **Both, in this order:** (a) show the **typed outgoing DO**, which already exists in the API (create it from a quotation or invoice; print it with a "Received by" signature block); then (b) **capture incoming supplier DOs/GRNs** in the Inbox and link them to bills (three-way match: PO, DO/GRN, bill). See §4. |

---

## 1. Field adequacy

**Sources of "normal":** what Malaysian SME documents carry, the LHDN e-Invoice field list (used here only as a checklist
of identity fields, **not** for submission) [L1, L2], and the competitors' document flows [K1–K3, A1–A2, S1–S2, X1–X2].

**Code evidence (what AccordAI has today):**
- Form fields: frontend `src/features/common/erp/document-form.tsx:478-540` (header) and `:650-700` (lines)
- API model: `services/routes/erp/documents.py:121-143` (`DocumentInput`; free `fields` dict)
- What prints: `services/routes/erp/documents.py:411-475` (company, contact and print data) and `services/routes/erp/pdf.py:241-610` (layout)

Legend: **Has** = typed in the form and printed · **Partial** = stored or printed but not both, or only some types ·
**Missing** = not anywhere · **Optional** = only some SMEs need it. ✔ normally carried · ○ sometimes · – not used.
Columns: QT quotation, IV invoice, CN/DN credit and debit note, OR official receipt (payment received), DO delivery order.

### 1a. Header and parties

| Field | QT | IV | CN/DN | OR | DO | AccordAI today (evidence) | Verdict | Proposal |
|---|---|---|---|---|---|---|---|---|
| Seller name, SSM, TIN, SST no., address, phone, email | ✔ | ✔ | ✔ | ✔ | ✔ | Printed from the company profile (`documents.py:411-416`, `pdf.py:391-411`) | **Has** | – |
| Company logo | ✔ | ✔ | ✔ | ✔ | ✔ | No logo; the PDF has Helvetica text only (`pdf.py:1-14`) | **Optional** | Later: one PNG logo in the profile |
| Buyer name, code, SSM, TIN | ✔ | ✔ | ✔ | ✔ | ✔ | Contact card on the form, and printed (`documents.py:419-432`) | **Has** | – |
| Buyer **SST no.** | ✔ | ✔ | ✔ | – | – | Shown on the form card (`document-form.tsx:633`) but **not printed**: `_contact_for_print` leaves out `fields.sstNumber` (`documents.py:431-432`) | **Partial (bug)** | Print it: a one-line fix, the first item in the next 2 weeks |
| **Billing address** | ✔ | ✔ | ✔ | ✔ | – | One contact address, printed (`documents.py:428`) | **Has** | – |
| **Delivery / site address** (≠ billing) | ✔ | ✔ | – | – | **✔ required** | None: a DO prints "Deliver to" with the *billing* address (`documents.py:435-436`) | **Missing** | Optional `fields.deliveryAddress` per document, filled from a contact default; a DO prints it as "Deliver to" and the billing address as "Bill to". LHDN also names a shipping recipient (name and address) [L1] |
| **Attention** (contact person) | ✔ | ✔ | ○ | – | ✔ | None | **Missing** | `fields.attention`, picked from the contact's people (the contact page already holds contacts) or typed |
| Document no. (auto) | ✔ | ✔ | ✔ | ✔ | ✔ | QT/IV/CN/DN/OR/DO + year + sequence (`documents.py:48-53, 199`) | **Has** | Taken at the first Save; see §0 and the decision in §6 |
| Document date | ✔ | ✔ | ✔ | ✔ | ✔ | Yes | **Has** | – |
| **Validity / expiry date** | **✔** | – | – | – | – | Stored as `due_date` by the API (comment at `documents.py:73-75`), but the form shows a due date only for open items (`hasOutstanding`, `document-logic.ts:49`), and the PDF prints one only for open items (`documents.py:459-460`). "Expired" is set by hand | **Missing (QT)** | Show "Valid until" on quotations (default: date + a company default, e.g. 30 days); print it; list badge "expires in 3 days". Xero users report a quote stays *Sent* after expiry [X2], so a reminder works better than auto-expiry |
| **Payment terms + due date** | ○ | ✔ | DN ✔ | – | – | Combobox from the catalog; the due date follows the terms (`document-form.tsx:508-517`); printed as "Terms" and "Due date" | **Has** (IV, DN) | On a QT, show terms (text only, no due date) so the invoice inherits them |
| Reference no. (customer PO / your ref.) | ✔ | ✔ | ✔ | ✔ | ✔ | One free-text "Reference no." (`document-form.tsx:518`) | **Has** | Hint: "Customer PO / reference" |
| **Salesperson** | ○ | ○ | – | – | – | None | **Optional (missing)** | `fields.salesperson` (from the company's users or a short list); printed; later filters the sales reports |
| **Project / job** | ○ | ○ | ○ | – | ○ | Tags exist in the API (`DocumentInput.tags`), but there is no field in the form | **Optional (partial)** | Show tags as "Project" when the company turns it on |
| Related document (original invoice, quotation) | – | ○ | **✔** | ✔ | ✔ | "Corrects" for CN/DN; transfer link printed as "Original …" (`documents.py:467-474`) | **Has** | – |
| Currency | ✔ | ✔ | ✔ | ✔ | – | Yes; only MYR posts | **Has** | – |
| Payment method, bank account (OR) | – | – | – | ✔ | – | Bank account and amount for OR (`document-form.tsx:525-537`); "Payment method" prints from `fields.paymentMethod`, but there is no form input | **Partial** | A "Method" select (cash / transfer / cheque + cheque no.) on OR |

### 1b. Lines

| Field | QT | IV | CN/DN | OR | DO | AccordAI today | Verdict | Proposal |
|---|---|---|---|---|---|---|---|---|
| Product / item code, description | ✔ | ✔ | ✔ | ○ | ✔ | Product combobox and description (`document-form.tsx:663-677`) | **Has** | – |
| Quantity, **UOM** | ✔ | ✔ | ✔ | – | ✔ | Qty only; no unit of measure | **Partial** | `uom` on the product, printed next to Qty ("10 pcs") |
| Unit price | ✔ | ✔ | ✔ | – | ✗ | Yes (up to 4 dp) | **Has** | A DO prints no prices (already true: `pdf.py:282-286`) |
| **Line discount** | ✔ | ✔ | ✔ | – | – | An **amount** per line ("Disc." column) | **Has** | Optional "%" entry that the form turns into an amount *before* save, shown for checking (price arithmetic, not tax) |
| **Tax code label** | ✔ | ✔ | ✔ | – | – | A label from the catalog ("A label only; it does not calculate tax", `document-form.tsx:656`) | **Has** | – |
| Tax amount (typed) | ✔ | ✔ | ✔ | – | – | Typed; printed as "Tax (as recorded)" (`pdf.py:538`) | **Has** | – |
| Line amount, subtotal, discount, total | ✔ | ✔ | ✔ | ✔ | ✗ | Live sums on the form; printed | **Has** | – |
| Delivered vs ordered qty | – | – | – | – | ○ | None | **Optional** | Only with partial deliveries (Later) |

### 1c. Footer

| Field | QT | IV | CN/DN | OR | DO | AccordAI today | Verdict | Proposal |
|---|---|---|---|---|---|---|---|---|
| Notes | ✔ | ✔ | ✔ | ✔ | ✔ | "Description" prints as "Notes" (`pdf.py:607`) | **Has** | Label the form field "Notes (printed)" |
| **Terms and conditions** | ✔ | ✔ | ○ | – | ○ | The company default prints (`documents.py:497`); `fields.terms` is read, but the **form has no input** for it | **Partial** | A "Terms" box, pre-filled from the company default for this type, editable per document |
| **Bank details for payment** | ✔ | ✔ | DN ✔ | – | – | Printed on QT/IV/DN from the profile (`pdf.py:262, 543-553`) | **Has** | – |
| **Received by** (signature, name, date, chop) | – | – | – | – | **✔ required** | Every page prints "computer-generated… No signature is required" (`pdf.py:25, 586-596`), **including the DO** | **Missing (DO)** | A DO-only block: "Goods received in good order and condition" + signature, name, date and company chop boxes; no "no signature" line on DOs |
| "Authorised by" (issuer signature) | ○ | ○ | ○ | ○ | ○ | No-signature line | **Optional** | Keep: computer-generated is normal |
| Revision / issued time | – | – | – | – | – | Printed (`pdf.py:387-388`) | **Has** | – |

**Gap summary.** Must add: quotation validity, delivery address, attention, per-document terms, the DO Received-by
block, and the SST-on-PDF bug. Optional, for the client to choose: logo, salesperson, project, UOM, % discount and OR payment method.
Everything else a Malaysian SME document normally carries is already typed and printed.

---

## 2. Preview before saving

### 2.1 Today
- "Preview PDF" appears only on a **saved** draft; the server renders it with a "DRAFT - NOT ISSUED" watermark (`documents.py:1323-1338`; action list in frontend `document-logic.ts`; button `document-form.tsx:424`, tooltip "The last saved version, marked DRAFT").
- So Jonathan's case, "let me see it before I press Save", is not covered. Xero users ask for the same and print to PDF as a workaround [X1].

### 2.2 Design: a live paper preview next to the form
- **Layout:** on wide screens (≥ 1280 px), a split view: the form on the left (~55%), an **A4 "paper" preview** on the right (~45%), sticky and scaled to fit. On narrow screens, a **Preview** button opens the same sheet full-screen (a modal with *Back to editing*).
- **What it draws:** the same blocks, in the same order and with the same labels, as `pdf.py` (header → contact → lines → totals & bank → notes → terms → footer), filled from the **unsaved** form state, with a diagonal "PREVIEW – NOT SAVED" mark. Empty required fields show as amber placeholders on the paper ("Customer?", "Valid until?"), so the eye goes straight to the gap.
- **Same-layout guarantee:** two options:
  1. **(Recommended) Server-rendered unsaved preview.** A new `POST /api/erp/documents/{type}/preview.pdf` takes the unsaved body, runs the *same* `_print_data` and `render_document_pdf` (nothing stored, no number taken; it prints "QT-2026-(next)"), and returns a PDF shown in an `<iframe>`, about 600 ms after typing stops. It is the same code path as the real PDF, so there is **no drift**. Cost: one small request per pause; `pdf.py` has no dependencies and its output is deterministic.
  2. An HTML clone of the layout in React: instant, but two layouts must be kept in step. Fall back to it only if option 1 feels slow on Render's free tier.
- **Click to edit:** clicking a block on the paper focuses the matching form field, or explains where the value comes from ("Company profile › Bank details").
- **Covers every printed type:** QT, IV, CN, DN, OR, DO (and the payment voucher on the purchase side), with the same panel. A DO preview shows no prices and shows the Received-by block.

### 2.3 Make Save vs Issue unambiguous

| Button (new label) | What it does | Who | Reversible? |
|---|---|---|---|
| **Save draft** | Keeps an editable draft. Takes the next number now (today's behaviour; see decision 3 in §6). Nothing is printed, sent or posted. | Accountant, admin | Yes: edit or delete |
| **Preview** | Shows the PDF as it will print (the live panel, or the modal on a phone). Stores nothing. | Anyone who can view | – |
| **Submit for approval** (IV/CN/DN/OR/DO) | Locks the draft for the admin. | Accountant | Admin can send it back |
| **Issue** (IV/CN/DN/OR/DO) · **Send** (QT) | Freezes it and stores PDF revision 1, which cannot be changed. **Posts to the ledger** for IV/CN/DN/OR; a DO and a QT never post. | Admin (QT: accountant or admin) | Only by **Void**, which keeps both copies and adds a reversal |

- A one-line strip under the title: *"Draft – visible to your team only. Issue to print it and post it."*
- Before Issue/Send, a **confirm sheet** shows the preview plus a plain-words posting summary ("Dr Trade debtors RM 1,806.40 / Cr Sales …", reusing roadmap item P10) and any duplicate or outlier warnings from §3.

---

## 3. AI-assisted entry

Everything below is **suggest → human confirms**. No assist computes tax or chooses a tax amount, and none issues or posts.
Most of it is **rules over the company's own data** (free); only A4 calls an LLM.

| # | Assist | Pain today | How it works | Guardrail | Data needed | Effort |
|---|---|---|---|---|---|---|
| A1 | **Repeat last lines and prices for this customer** | Retyping the same 5–10 lines for a regular customer; price slips | When a customer is chosen, a chip offers "Copy lines from QT-2026-00009 (12 Nov)", and each line shows the **last price this customer paid** for that product, with the date. The recent-line typeahead from PR #31 becomes customer-aware | Lines go in as editable; each suggested price carries a small "from IV-2026-00031" tag until edited. Tax amounts are never filled in by the assist | Past QT/IV lines by customer and product (`erp_document_headers.payload.lines`) | **S** |
| A2 | **Terms, due date, validity, attention and delivery address from the contact** | No validity on quotations; the due date was missing on converted invoices (H4 defect 7, fixed by PR #36) | Already done for IV/DN (contact `fields.paymentTermDays` → due date). Extend: QT validity = date + company default; attention and delivery address default from the contact | Hint "Follows the customer's terms until you change it" (exists) | Contact fields; a company default for validity | **S** |
| A3 | **Convert with nothing retyped** (QT→IV, QT→DO, IV→DO, DO→IV) | The UI offers QT→IV and DO→IV only, although the API already allows QT→SO/DO/IV and IV→DO (`documents.py:56-61` vs frontend `document-logic.ts` `quotationActions` / `ISSUED_TRANSFERS`) | Show every allowed transfer as a button. The copy keeps lines, reference, attention, delivery address and terms, and opens as a **draft with the live preview**; a banner lists what changed ("Due date set from 30-day terms") | The target is a draft; Issue is still a human step. "Already converted" is hidden (exists: `form-actions.ts`) | Existing transfer API and links | **S** (UI only) |
| A4 | **Draft a quotation from a pasted email, WhatsApp message or PO** (optional) | Customers write "pls quote 20 ctn of X, 5 of Y, deliver to site at …"; staff retype it | Paste the text, or drop a PO PDF/photo → **one LLM call** (the existing text-LLM path used by extraction, `services/extraction/ai/text_llm.py`) returns customer, attention, delivery address, PO/reference no. and lines (description, qty, *price only if stated*). Customer and products are matched to the catalog. The result opens as a draft with **every AI value highlighted** and a side list "From the message: … → field" | Nothing saves until the user clicks Save draft; unmatched items are marked "new?"; a price that is not in the text stays **empty** (never guessed); tax is never filled | Catalog, contacts, one LLM call per paste on the existing key/free tier (**no new paid API without approval**) | **M** |
| A5 | **Duplicate warning before saving** | The duplicate check exists, but shows only *after* save on the detail page (`documents.py:368-383`) | Run the same rule live as the form changes (same type and customer, plus the same reference, or the same total on the same date); show an amber line under the totals with a link to the other document | A warning, not a block | Existing query | **S** |
| A6 | **Price outlier warning** | A 10× price or a missing zero gets through | For each line with a product, compare the unit price with the last 10 prices for that product (this customer first, then all customers); flag one outside ±30% of the median ("Usual RM 42.00–48.50; this is RM 4.50") | A warning only; "Price is correct" is one click and is logged | Past lines; no LLM | **S–M** |
| A7 | **Missing-field nudges** | Quotations sent without validity or attention; DOs without a delivery address | The preview's amber placeholders, plus a short checklist on the Send/Issue confirm sheet | Soft: the user can proceed | Rules | **S** |

Not proposed: AI-written tax amounts, auto-issue, auto-send to the customer, or e-invoice submission.

---

## 4. Delivery orders

### 4.1 What a DO is for
A **delivery order (DO)** travels with the goods. The customer signs and chops it on receipt, and the signed copy is the
**proof the goods were delivered** (useful in disputes and as backing for the invoice). It lists items and quantities, with **no
prices**, and in AccordAI (no inventory) it **never posts** to the ledger. In stock-keeping systems the DO is where
stock leaves: AutoCount says an invoice transferred from a DO does not move stock again, because the DO already reduced
it [A1], and SQL says stock is deducted when the DO is saved [S1].

### 4.2 Usual flows

```
SALES       Quotation ──(optional) Sales order──► Delivery order ──► Invoice ──► Official receipt
                 └──────────────────────────────► Invoice ──► Delivery order   (invoice first, then deliver)
PURCHASES   Purchase order ──► Supplier DO, recorded as a Goods received note (GRN) ──► Bill ──► Payment voucher
```
- **Bukku:** transfer a quotation to a sales order, delivery order or invoice; steps can be skipped (QT → IV); transferred documents are linked and the source is locked [K1]; a DO can also be created directly under Sales › Delivery Orders [K2].
- **AutoCount:** full or partial transfer of a DO to an invoice; once a DO is issued, the invoice must be created by "Transfer from DO" [A1]; quotation → SO/DO/IV [A2].
- **SQL Account:** SO → DO → invoice by "Transfer from" [S1]; purchases PO → GRN → purchase invoice, with the GRN as "proof of receipt" [S2].
- **Xero:** we found no delivery-order document in the pages we checked (*n/v*).

### 4.3 What AccordAI already has (code)
- The types `sales-order`, `delivery-order`, `purchase-order` and `goods-received` exist end-to-end in the API and the frontend types (`common.py:9-15`; frontend `erp.service.ts:91-129`), with numbering SO/DO/PO/GR (`documents.py:48-49`), transfers QT→SO/DO/IV, SO→DO/IV, IV→DO, DO→IV, PO→GR/BL and GR→BL (`documents.py:56-61`), and a **DO PDF**: title "DELIVERY ORDER", "Deliver to", columns No/Description/Qty, no totals (`pdf.py:248, 282-286, 523-525`).
- **Hidden from the sidebar by decision** (Accountant audit §8; frontend `navigation.ts:33-46`): reachable only by URL `?type=delivery-order`.
- Missing for real use: a delivery address, the Received-by block, a "Create DO" button on a quotation or issued invoice, a "Delivered" state with the signed copy, and capture of *incoming* DOs/GRNs.

### 4.4 Recommendation: **both, (a) first, then (b)**

| Option | What | Why / why not | Effort |
|---|---|---|---|
| **(a) Typed outgoing DO** | Sidebar **Sales › Delivery orders** (between Quotations and Invoices). Create it from a **quotation** (deliver first, invoice later), from an **issued invoice** (invoice first), or with New. The PDF gets the delivery address and the **Received by** block. Status: Draft → **Issued** (printed) → **Delivered** (signed copy uploaded with the existing attachments panel) → Invoiced. **Never posts.** | Most of it exists, so it is cheap; KNT's seeded invoices already reference a "D.O. No."; it answers Jonathan's question with a visible UAT feature | **M** (sidebar, transfer buttons, PDF block, delivery address, Delivered status) |
| **(b) Captured incoming supplier DO / GRN** | Inbox: a new extraction type **Supplier DO**, reviewed like a bill but with **no amounts required**, becomes a **Goods received** record (the `goods-received` type exists). On a bill, a "Match" panel shows the PO, the DO/GRN and the bill side by side (**three-way match**): quantity per item (received vs billed), price (ordered vs billed), and flags such as "Billed 12, received 10" | Real control value (no paying for goods not received), built on AccordAI's strength (capture). It needs a new extraction type and a matching UI, and is less useful until POs are used | **M–L** |
| **Sales orders** (decided 25 Nov: **in scope, shown**) | Sidebar **Sales › Sales orders**. Created from an accepted quotation; **Confirm** (admin) freezes it; it is delivered with one or more DOs and then invoiced. **Never posts.** Optional: a quotation can still go straight to a DO or an invoice | Jonathan chose to show them. They record the customer's confirmed order (and PO number) before goods leave. The type, numbering (SO), transfers and API already exist | **S** (sidebar, Confirm label, PDF title "SALES ORDER") |

**Sidebar after (a):** Sales: Quotations · **Sales orders** · **Delivery orders** · Invoices · Credit notes · Debit notes · Payments received.

**Clickable prototype:** the visual page (`https://claude.ai/artifact/177xUjCaWQQZRsWaAfC65D`, private until shared) runs this flow on fictional KNT data: quotation → sales order → delivery order (signed copy) → invoice with posting, the live A4 preview, Save draft vs Send/Issue, accountant submit vs admin issue, and the assists A1, A3, A4 (simulated without an LLM), A5, A6 and A7. It is a prototype in one HTML page and does not change the app.
**After (b):** Purchases: (Purchase orders, if turned on) · **Goods received** · Bills · Expense receipts · Supplier credit notes · Supplier debit notes · Payments made; and Inbox › Upload & capture accepts "Supplier DO".

**Trace:** every DO shows chips "From QT-2026-00012 · Invoiced in IV-2026-00031" (links already returned by the history endpoint, `documents.py:1126`); an invoice shows "Delivered: DO-2026-00007 (signed copy ✓)"; trace search (PR #24) finds a DO number from either side. On purchases: Bill ⇄ GRN ⇄ PO chips plus the match status.

---

## 5. Roadmap

Effort: **S** ≤ 1 day, **M** 2–4 days, **L** ≥ 1 week (one developer with Claude Code). Success criteria are the
project's own (`docs/COMPETITOR_AND_AI_ROADMAP.md`): SC1 typing and time per document, SC4 first-pass exception rate,
SC5 admin approval time, plus a new **SC-D "document correct on first print"** (no void-and-reissue during UAT).

### Next 2 weeks (UAT)
| # | Item | Effort | Supports | Done when |
|---|---|---|---|---|
| 1 | Print the customer's **SST no.** on the PDF (bug) | S | SC-D | The PDF shows SST for a contact that has one |
| 2 | **Rename Save → "Save draft"**, add the status strip and the Issue/Send confirm sheet with plain-words posting | S | SC5, SC-D | UAT testers can say what Save does |
| 3 | **Quotation "Valid until"** (form, PDF, default days, expiry badge) | S | SC-D | A QT PDF prints Valid until; the list shows "expires soon" |
| 4 | **Live preview** (server-rendered unsaved preview, panel and modal) for all printed types | M | SC1, SC-D | Typing updates the paper within ~1 s; no number is taken |
| 5 | **Attention, delivery address, per-document Terms** (fields + PDF) | S–M | SC-D | Printed on QT/IV/DO |
| 6 | **Sales orders and DOs in the sidebar** + QT→SO, QT→DO, SO→DO, IV→DO, DO→IV buttons + **Received-by block** + "Delivered" with the signed copy | M | SC1 | KNT walk-through: QT → SO → DO (signed copy) → IV, with trace chips both ways |
| 7 | Live **duplicate warning** (A5) and **last-price chips** (A1) | S | SC1 | The warning shows before save; copying a repeat order's lines takes ≤ 3 clicks |

### Next semester
| # | Item | Effort | Supports |
|---|---|---|---|
| 8 | **Price outlier warning** (A6) and missing-field nudges (A7) | S–M | SC-D |
| 9 | **Quote from a pasted email or PO** (A4): one LLM call, every value confirmed | M | SC1 |
| 10 | **Capture supplier DO / GRN** in the Inbox → Goods received record | M | SC4 |
| 11 | **Three-way match panel** on bills (PO / GRN / bill; quantity and price flags) | M–L | SC4, SC5 |
| 12 | Salesperson, project (tags), UOM, % discount entry, OR payment method | S each | SC-D |

### Later
| # | Item | Effort |
|---|---|---|
| 13 | Partial deliveries (delivered vs ordered quantity, backorders); many DOs → one invoice | L |
| 14 | Show the PO module (sales orders move to the next 2 weeks) | M |
| 15 | Company logo and a form-design picker (Bukku has custom form designs [K3]) | M |
| 16 | Map e-invoice fields (shipping recipient, buyer SST) for the future MyInvois scope [L1, L2]; **no submission** | M |

---

## 6. Decisions for Jonathan / SAIC

1. ~~Sales orders in scope?~~ **Decided 25 Nov (Jonathan): yes, show them** in Sales, between Quotations and Delivery orders (§4.4).
2. **For the KNT UAT script, DO before or after the invoice?** Both will work; the recommendation is QT → DO → IV.
3. **Numbering on Save:** keep "the number is taken at the first save" (simple; deleted drafts leave gaps), or give drafts a temporary label and assign the number at Send/Issue (no gaps, which auditors prefer; it touches numbering, so M effort)?
4. **The DO's "No signature required" line:** replace it with the Received-by block? (The recommendation is yes.)
5. **Quote-from-email (A4):** OK to use the existing LLM key/free tier for it next semester? No new paid API is proposed.
6. **Incoming DO/GRN capture (b):** worth the next-semester slot, given that KNT does not raise POs today?

---

## Sources

Verification: **read** = the page was opened; **search** = taken from search-result snippets only (re-check before quoting in the report).

| Key | Source | Used for | Verified |
|---|---|---|---|
| K1 | Bukku, Transferring sales documents (quotation, sales order, delivery order, invoice): https://intercom.help/bukku/en/articles/14764582-transferring-sales-documents-quotation-sales-order-delivery-order-invoice | Transfer chain; steps can be skipped; linked, locked source | search |
| K2 | Bukku, Creating a delivery order: https://intercom.help/bukku/en/articles/6459783-creating-a-delivery-order | DO created under Sales › Delivery Orders | search |
| K3 | Bukku, Customise your form designs: https://intercom.help/bukku/en/articles/8865375-customise-your-form-designs | Form-design customisation (logo, colours, shown columns) | search |
| A1 | AutoCount wiki, How can I transfer Delivery Order to Invoice: https://wiki.autocountsoft.com/wiki/Accounting_2.0_-_Sales:_How_can_I_transfer_Delivery_Order_to_Invoice | DO→IV, full or partial; stock moves at the DO | search |
| A2 | AutoCount wiki, How to transfer Advanced Quotation to Sales Order: https://wiki.autocountsoft.com/wiki/Accounting_2.0_-_Sales:_How_to_transfer_Advanced_Quotation_to_Sales_Order | QT → SO/DO/IV | search |
| S1 | SQL Account, Sales guide: https://docs.sql.com.my/sqlacc/usage/sales/guide | SO→DO→IV; stock deducted at the DO | search |
| S2 | SQL Account, Purchase guide: https://docs.sql.com.my/sqlacc/usage/purchase/guide | PO→GRN→purchase invoice; GRN = proof of receipt | search |
| X1 | Xero Product Ideas, Quotes – Preview option: https://productideas.xero.com/forums/967115-invoices-quotes/suggestions/44961295-quotes-preview-option | Users ask for a quote preview; they print to PDF instead | search (user forum, not official docs) |
| X2 | Xero Product Ideas, Move quotes to an expired status after expiry date: https://productideas.xero.com/forums/939198-for-small-businesses/suggestions/49813757-quotes-move-quotes-to-an-expired-status-after-ex | A quote stays *Sent* after its expiry date | search (user forum) |
| L1 | LHDN (IRBM), e-Invoice Specific Guideline: https://www.hasil.gov.my/wp-content/uploads/irbm-e-invoice-specific-guideline.pdf | Shipping recipient name/address/TIN fields; checklist only | search |
| L2 | MyInvois SDK, Debit note v1.1: https://sdk.myinvois.hasil.gov.my/documents/debit-v1-1/ | Buyer SST no. "mandatory if SST registered"; checklist only | search |

Internal: `docs/COMPETITOR_AND_AI_ROADMAP.md` (P10 posting preview, success criteria), `docs/ACCOUNTANT_FUNCTION_AUDIT.md` §8 (SO/DO hidden by decision; defect 7), and the handovers for PR #31 (assisted entry; keystrokes 57→38) and PR #36 (line defaults, contact terms).
