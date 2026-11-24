# AccordAI vs Bukku and the best accounting tools: gaps and an AI roadmap (24 Nov 2026)

**Purpose.** An evidence-based comparison of AccordAI with Malaysian and global accounting tools, focused on where AI
can cut the accountant's *verification and daily workload* beyond extraction. It is for the team, Dr. Sim and SAIC.

**Companion document.** `docs/ACCOUNTANT_FUNCTION_AUDIT.md` (branch `claude/accountant-function-audit`) is the
page-by-page function audit with the six demo bugs and a prioritised fix list. This document refers to it as
**[Audit §n]** and does not repeat it.

**House rules respected throughout.** The system never calculates tax (a tax *code* may be proposed as a label for a
printed amount; the amount itself is always read or typed as printed). There is no LHDN submission: MyInvois appears
only as a comparison point and as future, read-only consistency checks. A person approves anything that posts.
Free or existing services only; anything paid is marked **needs Jonathan's approval**.

---

## Top 5 recommendations

1. **Fix the six demo bugs before adding anything (next 2 weeks).** Admin approval, credit-note type, the
   "Nothing to check" message, payment terms, matching a bank line to a payment voucher, and file names
   ([Audit §0, §18 P0]). Each of these is a daily flow that Bukku, AutoCount, SQL and Xero already cover. Until they
   work, no AI feature will save time. *Ties to SC4 (exceptions) and SC5 (all routed).*
2. **A risk-ranked review that explains itself: "look only at what is uncertain, and see why".** Sort the queue and
   the fields by risk, collapse verified fields, say in plain language why something needs a look, and keep a
   random spot-check as the guardrail. Most of it exists already (`services/review/readiness.py`,
   `services/routes/documents/suggested_fixes.py`). *Ties to SC2 (≥70% less manual entry).*
3. **Bank matching that explains its suggestions, covers vouchers and typed documents, and learns.** Xero's
   strongest AI feature is auto-reconciliation that runs only at high confidence and labels each match as
   Rule, Match, Memory or Prediction [X2]. AccordAI matches exact amounts and references only, and cannot match a
   line to a voucher ([Audit §0f, §12]). Daily bank work is the largest remaining time sink.
4. **Supplier memory 2.0: learn from approved corrections and propose the account and tax code.** Dext supplier rules
   apply category and tax-rate defaults [D1], and SQL AI Easy Scan "learns your preferences" [S1]. AccordAI's
   Phase 4 supplier memory (`docs/MASTER_PLAN.md` P4) was never built: there is no `supplier_profiles` table.
   *Ties to SC1 and SC2.*
5. **Measure workload, not only accuracy.** Extend the existing SC2 effort study (`services/routes/effort/routes.py`)
   with touches, corrections and fields looked at per document, taken from `audit_logs`. Then run a counterbalanced
   (AB/BA) timed study with SAIC accountants in UAT. Competitors publish only vendor claims (for example the 99.9%
   accuracy figure on Dext's Xero listing [D3], or Xero's "~96% accuracy" for auto-reconciliation [X2]). A measured
   number, honestly reported, is AccordAI's strongest argument.

---

## Method and evidence

- **AccordAI's status** comes from the code at `origin/features` **5ca59c3**, cited as `file:line` or by module, and
  from the function audit, cited as [Audit §n]. The audit read the frontend at `origin/features` **4b7ff59**.
- **Competitor claims** come from official product pages, help centres, pricing pages and changelogs first, then
  reputable trade press. Every competitor cell cites a source key (list at the end).
  - **Read**: the page was fetched and read on 24 Nov 2026.
  - **Search**: the claim comes from a search-result summary of that page. Re-open the page before quoting it to SAIC
    or in the report.
- **Cell values.**
  - **Has**: the source states the feature.
  - **Partial**: limited, add-on, or announced but not shipped.
  - **Missing**: the source or the code shows it is absent.
  - **n/v**: not verified, because no source covered it. n/v is *not* the same as Missing. The competitor may well
    have the feature.
- **Caveats.** Vendor figures are vendor claims and were not independently checked. Plans and prices change. Xero and
  QuickBooks features vary by country. Nothing here was tested hands-on in a competitor product.

## 1. Products compared

| Product | Type | Market | Price (as published) | Source |
|---|---|---|---|---|
| **AccordAI** | Capture, review, books (final-year project) | Malaysian SMEs and their accountants | Runs on free tiers (Groq/Gemini free, Neon, Render, Vercel); not sold | this repo |
| **Bukku** | Cloud accounting | Malaysia | Launch RM0 (no extractions); Seed RM35; Grow RM65; Prime RM95; Elite RM135 per month, excl. 8% SST; unlimited users | [B2] read |
| **AutoCount Cloud Accounting** | Cloud accounting | Malaysia | Lite RM70, Basic RM100, Pro RM180 per month (UOB partner page, "subject to SST") | [A3] read |
| **SQL Account (E Stream)** | Desktop accounting with add-ons | Malaysia | Not found; AI Easy Scan "Free up to 28 February 2027" | [S1] read |
| **Xero** | Cloud accounting | Global, Malaysia site | USD: Lite 7, Starter 32, Standard 54, Premium 82 per month (list prices; 50% off for 36 months on MY subscriptions bought before 31 Dec 2026) | [X1] read |
| **QuickBooks Online** | Cloud accounting with AI agents | Global, Malaysia e-invoicing | AI features "at no additional cost" (Intuit) | [Q5] search |
| **Dext** | Capture and pre-accounting | Global | n/v | [D1] read |
| **Hubdoc (Xero)** | Capture | Global | Included with Xero plans | [X1] read |
| **Digits** | AI-native ledger and agents | US | From USD 65/month (Essentials) | [G1] search |
| **Vic.ai** | Autonomous accounts payable | Mid-market and enterprise | n/v | [V3] search |
| **Booke AI** | AI bookkeeping add-on for Xero/QBO | Global | Conflicting listings | [K1] search |

Watch list, not assessed (self-reported Malaysian AI-accounting entrants): Lejar.ai (AI-drafted journals, SST-02,
multi-company) [W1], QNE N3 AI Accounting (early access April 2026) [W2], Teera.AI [W3].

## 2. Feature matrix

### 2a. Ledgers: AccordAI vs Malaysian and global accounting software

| Dimension | AccordAI (code evidence) | Bukku | AutoCount Cloud | SQL Account | Xero | QuickBooks Online |
|---|---|---|---|---|---|---|
| **Capture** | **Has**: bulk PDF/photo upload, lite OCR on the server, one masked-text AI call, each value checked against the OCR text (`services/routes/documents/routes.py` `upload_document`; `services/extraction/`); SHA-256 duplicate-file refusal (`documents/routes.py:581`). **No email-in.** | **Has**: phone receipt snap; 50–300 extractions/month by plan; method not stated [B1, B2] | **Has**: AI SmartScan fills transactions from photos [A1] | **Has**: AI Easy Scan via WhatsApp or upload; reads supplier, date, amount, tax, description; repeat-document warning [S1]. Imports supplier e-invoices from MyInvois [S2] | **Has**: Smart Document Capture (Starter+), Hubdoc included [X1] | n/v |
| **Review / verification** | **Has**: 10 rule checks EX-001…EX-010 (`validation/documents/engine.py`); readiness reasons per document (`services/review/readiness.py`); suggested fixes (`documents/suggested_fixes.py`). **Bug:** "Nothing to check" while approval is blocked [Audit §0d] | n/v | n/v (review step not stated) [A1] | **Partial**: "instant preview", then confirm [S1] | **Partial**: Review Reconciled page for auto-reconciled bank lines [X2]; document review n/v | **Partial**: Accounting AI asks the user about unclear bank transactions [Q1] |
| **Approval workflow** | **Has**: accountant submits, another admin approves (`own_submission` in `readiness.py`); bulk approve (`services/routes/review/lanes.py`). **Bug:** admin approval hard to reach, no reasons given [Audit §0b] | **Partial**: "Advanced permissions" on Prime/Elite; approval flow not stated [B1, B2] | n/v (plan user limits only) [A3] | n/v | **Partial**: native bill approval reported as a single step; multi-level needs an add-on such as ApprovalMax [X8] search | **Partial**: agents act "with your permission" [Q1] |
| **Bank feeds and reconciliation** | **Partial**: statement CSV/PDF import (`services/bank/`); exact-match suggestions, one-to-many match, confirm-and-post, reconciliation periods (`services/routes/bank/routes.py`). **No live feed, no bank rules, cannot match a voucher** [Audit §0f, §12] | **Has**: live Maybank feed; SmartRecon matching; other banks by statement upload [B1, B4] | **Has**: feeds for Maybank, UOB, Affin (beta); bank rules for automatic matching [A1, A2] | **Has**: auto bank reconciliation for "more than 20 Malaysian banks"; Maybank feed [S3, S4, S5] search | **Has, strongest**: JAX auto-reconciles only at high confidence; Rule/Match/Memory/Prediction labels; "over 60%" auto-reconciled (vendor figure) [X2] | **Has**: Accounting AI categorises and assists reconciliation [Q1] |
| **AR / AP** | **Has**: quotation → invoice → payment, CN/DN, payment vouchers and receipts (`services/routes/erp/`); statement of account (`reporting/statement.py`); ageing (`accounting/routes.py:865`). **Missing:** recurring invoices, reminders, online payment [Audit §18 P2] | **Has**: repeating invoices, automatic reminders, automated statements, online payment [B1, B2] | **Has**: invoices, quotes, credit notes, POs; overdue tracking [A3] | n/v | **Has** (invoice/bill limits on Lite/Starter) [X1] | n/v |
| **Period close** | **Has**: periods close/reopen with a reason (`accounting/routes.py`); month-end checklist (`services/routes/home/routes.py:206`); month pack (`reporting/financial.py:465`). Periods can be locked out of order [Audit §13] | n/v | n/v | n/v | **Has**: period lock and adviser lock dates [X6] search; month-end agent announced in XeroForce [X3] | **Has**: close books with a password; "Exceptions to Closing Date" report [Q3] search |
| **Reporting** | **Has**: P&L, balance sheet, TB, GL, AR/AP ageing, recorded-tax summary, statement of account, business summary, month pack (Excel). **Missing:** cash-flow statement, drill-down, comparatives, P&L/BS PDF [Audit §14] | **Has**: "50+ financial reports" incl. cash flow; SST [B1] | **Has**: customisable reports, dashboard [A3] | n/v | **Has**: cash-flow forecast 30/60/180 days by plan; budgets [X1] | **Partial**: Finance AI performance summary [Q1] |
| **E-invoicing (MyInvois)** | **Partial by design**: a consolidation *prep* record, off by default, nothing sent to LHDN (`services/routes/erp/sharing.py`). Submission is out of scope | **Has**: MyInvois intermediary; single/bulk, consolidated, self-billed; every plan; Peppol "coming soon" [B3, B2] | **Has**: direct MyInvois integration, all plans [A1] | **Has**: SQL Einvoice add-on [S6]; import of supplier e-invoices [S2] | **Has**: via the Invoici access point [X4] search | **Has**: native MY e-invoicing; consolidated first on Plus/Advanced [Q2] search |
| **Multi-company / practice** | **Partial**: multi-tenant with super-admin company approval (`services/routes/admin/routes.py`), but each user belongs to one company (`user["tenant_id"]` on every route); no cross-client view for a firm such as SAIC. Statement of account is MYR only (`reporting/statement.py:3`) | **Partial**: discount for 5+ companies; multi-currency on Prime/Elite or as an add-on [B2] | **Partial**: "+1 accountant" per plan; multi-currency on Pro [A3] | **Has**: "Unlimited companies" (Accountant Set) [S6] | **Has**: Xero Practice Manager and HQ [X7] search; multi-currency on Premium [X1] | **Has**: QuickBooks Online Accountant with Workpapers [Q3] search |
| **Audit trail** | **Has**: `audit_logs` on every decision, audit list (`services/routes/workspace/routes.py`), append-only journals with reversal, status history, record amendments | **Has**: "Every change tracked, audit-ready" [B1] | n/v | **Partial**: documents "stored securely for audit" [S1] | n/v (users ask for a lock-date change history [X6]) | **Has**: audit log and exceptions report [Q3] search |
| **Collaboration** | **Partial**: document comments, notifications, share links (off by default); assignment and review reminders have no UI [Audit §16–17]. No client portal, no mobile app | **Partial**: accountant joins the books; attachments [B1] | n/v (mobile only for payroll) [A3] | **Has**: WhatsApp intake for accountants; X-Mobile [S1, S6] | **Partial**: Document Requests "coming soon" [X3] | **Partial**: Customer AI (leads, outreach) [Q1] |
| **AI beyond extraction** | **Partial**: supplier amount-outlier check EX-010, modified z-score against approved records (`validation/transactions/transaction_anomaly.py`, `engine.py:52-110`); duplicate check EX-007; suggested fixes; bank coding suggestions table; "read again with AI" | **Missing** on the pages read (no AI feature named) [B1, B2, B3] | **Partial**: SmartScan only [A1] | **Partial**: Easy Scan assigns category and account, learns preferences [S1] | **Has**: JAX auto-reconciliation [X2]; JAX chat over the company's own data [X5] search; cash-flow actions; book-health flags announced [X3] | **Has**: Accounting, Finance, Customer, Project and VAT agents [Q1] |

### 2b. Capture and AI-first tools (dimensions where they compete)

| Dimension | Dext | Hubdoc | Digits | Vic.ai | Booke AI |
|---|---|---|---|---|---|
| Capture | **Has**: date, due date, supplier, currency, PO, total, tax, number, category [D1]; email, mobile app, upload [D2] | **Has**: email forwarding [H3]; PDF statement → CSV (digital PDFs only) [H2]; supplier-portal fetch retired in 2022 [H1] | n/v | **Has**: template-free invoice processing, "Autopilot" [V3] | n/v |
| Review / learning | **Has**: supplier matched from past documents; supplier rules set category, tax rate, payment method [D1] | n/v | **Has**: "auto-books 93%" of transactions (vendor claim) [G1] | **Has**: models trained on the customer's history; "85% no-touch by month 6" (vendor claim) [V3] | **Has**: reviews bank feeds, matches bills and invoices [K1] |
| Duplicates / anomalies | **Has**: three duplicate-detection modes, per supplier, Costs only; same-file rejection across channels [D2] | n/v | **Has**: quality-control checks, exceptions surfaced [G2] | **Has**: unusual-invoice alerts, duplicate payments, unauthorised vendors [V1, V2] | n/v |
| Close / practice | n/v | n/v | **Has**: "Agentic Close" with checklist, QC and firm dashboard (June 2026) [G2] | **Partial**: approval-routing analytics [V1] | **Has**: client portal and one-click client queries [K1] |

**Where competitors are clearly stronger (honest list).**
- **Live bank feeds.** Bukku, AutoCount and SQL connect to Maybank (and others); AccordAI imports statements only.
- **MyInvois.** Every Malaysian competitor, plus Xero and QuickBooks, submits e-invoices; AccordAI deliberately does
  not.
- **Auto-reconciliation at scale.** Xero reports over 100 million auto-reconciled transactions [X3].
- **AR breadth.** Reminders, repeating invoices, online payment and 50+ reports in Bukku.
- **Practice tools.** Xero Practice Manager.
- **Maturity.** Years in production; AccordAI is a final-year system in UAT.

**Where AccordAI is different (and can argue it with evidence).**
- **Every extracted value is checked against the OCR text before it is offered.** No competitor page read here
  describes value-level source checking.
- **Segregation of duties is built into the flow.** The approver cannot be the submitter.
- **Readiness reasons are stated per document.** The system says why a document is not ready.
- **Accuracy is measured on a published hold-out set.** 97.1% field accuracy when the AI answered, 91.3% strict,
  8.0 s per document (`docs/ACCURACY_LOG.md`); competitors publish vendor claims.
- **Tax is never computed.**
- **It costs nothing to run.**

## 3. Gap list: what a Malaysian SME accountant expects daily

Ranked by **impact on the accountant's time** (H = daily, many minutes; M = weekly or per document; L = occasional),
then by effort (S < ½ day, M 1–2 days, L 3+ days). Bugs a–f come from [Audit §0].

| # | Gap | Who has it | Time impact | Effort | Horizon |
|---|---|---|---|---|---|
| 1 | Admin can approve with reasons shown; Return/Reject (bug b) | All | H | M | UAT (2 wk) |
| 2 | Bank line ↔ existing PV/OR and typed open items (bug f) | Bukku, AutoCount, SQL, Xero | H | M–L | UAT (2 wk) |
| 3 | Never "Nothing to check" while blocked; printed tax ≤ printed total plausibility check, a comparison and not a calculation (bug d) | – (AccordAI-specific) | H | S–M | UAT (2 wk) |
| 4 | Payment terms and due date editable on every scanned document (bug a) | All | H | M | UAT (2 wk) |
| 5 | AI-read credit/debit notes usable, with a type control (bug c) | All | M | S | UAT (2 wk) |
| 6 | Bank rules and memory: recurring lines (fees, rent, salaries) coded automatically, confirmed by a person | AutoCount [A2], Xero [X2] | H | M | Next semester |
| 7 | Supplier defaults learned from history: account, tax code (label), terms | Dext [D1], SQL [S1] | H | M | Next semester |
| 8 | Email-in capture (one address per company) | Dext [D2], Hubdoc [H3] | H | M | Next semester |
| 9 | One accountant, many client companies: a firm queue across clients (SAIC is a firm) | Xero [X7], QBO [Q3], SQL [S6] | H for SAIC | L | Next semester |
| 10 | Recurring invoices and payment reminders | Bukku [B1, B2] | M | M | Next semester |
| 11 | Report drill-down, comparatives, P&L/BS PDF, cash-flow statement | Bukku [B1], Xero [X1] | M | M–L | Next semester |
| 12 | Reconciliation report (unpresented payments) | Common | M | M | Next semester |
| 13 | Live bank feed (Maybank) | Bukku, AutoCount, SQL | H | L, needs a bank partnership | Later |
| 14 | WhatsApp/mobile capture | SQL [S1] | M | M; WhatsApp Business API is usually paid (**needs Jonathan's approval**) | Later |
| 15 | MyInvois submission | All MY competitors | – | Out of scope by owner rule | Not planned |

## 4. AI-assist proposals beyond extraction

Each proposal keeps the rules: **no tax computation** and **a person approves anything that posts**. "Exists" names the
code it builds on.

| # | Proposal | Accountant's pain | How AI helps | Human still decides | Data needed | Risk → guardrail | Workload metric | Effort |
|---|---|---|---|---|---|---|---|---|
| **P1** | **Risk-ranked review queue** ("look only at what is uncertain") | Opens every document and scans every field, though 92.4% are filled automatically | Scores each field and document from source-check result, rule outcome, supplier history and correction history; sorts the queue; collapses verified fields; one line saying why each flagged field needs a look. Exists: `readiness.py`, `review_signal`, review lanes | Every flagged field; submit and approve | Review signals, rule results, `audit_logs` corrections | A wrong value marked "verified" (1.0% are filled wrong) gets skipped → **random 5% of ready documents forced into full review**; show "why verified"; never auto-approve | Fields looked at per document; active seconds per document; wrong-but-verified rate in spot checks | S–M |
| **P2** | **Explain why a check failed, with a suggested fix** | Rule codes and blocked buttons with no reason (bugs b, d) | A plain-language sentence for every blocking rule (with or without a field), showing the evidence (OCR text, printed values) and a one-click fix the user confirms. Exists: `suggested_fixes.py` | Accepting the fix | Rule results, OCR text | Confident-sounding wrong advice → fixes only re-use values present in the OCR text or contact records; the fix is shown before it is applied; audited | Seconds to resolve an exception; reopen rate | S |
| **P3** | **Bank-match suggestions with explanations** | Exact matching only; vouchers and typed documents cannot be matched | Scored candidates (amount, date gap, reference token in the description, counterparty fuzzy match, past matches) across scanned records, PVs, ORs and typed invoices/bills, each labelled *Rule / Match / Memory / Similar* with a "why" line (as Xero labels its matches [X2]) | Confirm each match; posting stays on the existing confirm-and-post path | Bank lines, open items, past match decisions | A wrong one-click match → no auto-post; a top suggestion that is not exact shows a warning; undo exists (reverse allocation) | Correct top-1 suggestion rate; clicks per bank line; lines explained per minute | M |
| **P4** | **Supplier memory 2.0** (learning from corrections) | The same supplier's quirks are corrected again and again | Stores (field, read value, corrected value) per supplier from **approved** documents; adds up to two past approved value sets of that supplier to the masked prompt as examples; applies learned hints as "check" values, never as verified. Plan P4 (`docs/MASTER_PLAN.md:188-205`), not built | Every learned value stays a suggestion until a person accepts it | Approved corrections, vendor match (`validation/matching/vendor_matching.py`) | Learning a mistake → learn only from approved documents; expiry and a "forget" button; per-company isolation; audit row per change | Corrections per document on the 2nd+ document of a supplier vs the 1st (the P4.3 done-when) | M |
| **P5** | **Auto-coding: account and tax code from supplier history** | Choosing an account and a tax code on every bill line | Proposes the account and the tax *code* used most on this supplier's approved documents ("used 7 of the last 8 times"). The tax amount is always the printed one; no printed tax → propose the no-tax code, never compute. Exists: contact `default_expense_account_id` (`services/routes/erp/contacts.py:90`), line defaults (PR #25), `bank_coding_suggestions` | Accept or change the coding; approval | Approved records, chart of accounts | Silent mis-coding → coding is a suggestion with its reason; changes logged; the admin approval shows the proposed journal (P10) | Share of lines whose suggestion was accepted; coding edits per document | M |
| **P6** | **Anomaly and duplicate detection 2.0** | Double billing and odd amounts are caught late or never | Adds checks on top of EX-007 (supplier + number) and EX-010 (amount outlier): near-duplicates (same supplier and amount within N days, different number), the same image under a different file (perceptual hash), a document number out of sequence, a new supplier with a large amount, and a bank payment matching a bill that is already settled. Similar to Dext [D2], Vic.ai [V2], Sage Intacct [V4] | Whether a flag is a real problem (flag, not block, except exact duplicates) | Approved records, hashes, bank lines | Alert fatigue → per-rule false-positive tracking; tunable per company; "accepted with a note" is already audited (`documents/routes.py:1147`) | Duplicates caught before posting on a seeded set; false-positive rate | M |
| **P7** | **Month-end close assistant** | Hunting for what is still open before the lock | Turns the existing checklist counts (`home/routes.py:206`) into a list of blockers with deep links and reasons. It also spots missing documents: a supplier billed in each of the last 3 months with no bill this month, or a bank payment with no supporting document. | Whether to accrue, chase or ignore; the lock itself | Periods, documents, bank lines, supplier cadence | False "all clear" → lock still needs a person; items found after a lock are tracked | Days from period end to lock; reopen count | S–M |
| **P8** | **Payment-due and cash-flow nudges** | Missed due dates; owner asks "can we pay?" | Bills due in 7 days vs bank balance; overdue customers with a drafted reminder (date arithmetic only) | Sending any reminder (no automatic customer email) | Due dates, ageing, bank balances | Wrong due date → shows its source (printed or supplier terms); drafts only | Overdue days; reminders sent per week | S–M |
| **P9** | **Exception batch fix by root cause** (new) | The same cause repeated across many documents (e.g. supplier not on the list) | Groups open exceptions by cause across documents: "12 documents: supplier 'Seladah' not on the list → add once". One confirmed action clears them all | The batch action | Exceptions, contacts | Over-broad batch → preview lists every affected document; per-document undo | Exceptions cleared per action; SC4 first-pass exception rate | S–M |
| **P10** | **Posting preview in plain words for the approver** (new) | The admin cannot see what approval will do to the books | Before approval, shows the proposed journal in words ("Dr Utilities 5100 RM 462.00; Dr SST input RM 0.00 as printed; Cr AP Seladah"), plus what changed since the AI read it (Audit P1 #15) | Approve, return or reject | Proposed journal, field history | None beyond today: it only displays | Admin seconds per approval | S |
| **P11** | **Email-in capture** | Downloading attachments and uploading them by hand | One inbound address per company; PDF/image attachments land in the Inbox | Which documents to process | Inbound mail | Malware and spam → allow-listed senders, PDF/JPG/PNG only, size limits, quarantine unknown senders. Service: a free inbound route (e.g. Cloudflare Email Routing to a webhook) — **confirm terms; any paid tier needs Jonathan's approval** | Share of documents arriving without manual upload | M |
| **P12** | **Natural-language questions over the books** ("why did expenses rise in November?") | Building ad-hoc reports to answer the owner | The model picks one of a fixed set of parameterised report queries and phrases the answer; **every number comes from the database**, with a "show the lines" link. Similar in spirit to Xero JAX chat [X5] and Ask Digits [G1] | Reading and acting on the answer (read-only) | Ledger aggregates (masked; no documents sent) | Invented numbers → no free-form SQL, numbers rendered from query results only; refuses when no template fits; test set of 30 questions | Answer correctness on the test set; minutes to answer a variance question | M–L |
| **P13** | **Supplier statement reconciliation** | Ticking a supplier's statement against AP by hand | Reads the supplier's statement (same masked extraction), matches its lines to that supplier's ledger, lists differences (missing bill, payment not credited) | How to resolve each difference | Supplier statement, AP ledger, statement of account (`reporting/statement.py`) | Extraction errors on statements → each difference shows both sources; nothing posts | Minutes per supplier statement; differences found | M–L |
| **P14** | **E-invoice consistency checks (later)** | Scanned bill and validated e-invoice may disagree | When validated supplier e-invoices are available read-only (as SQL imports them [S2]), compares UUID, supplier TIN, number and printed totals with the scanned bill and flags mismatches. **No submission.** | Which source is right | MyInvois data (needs the taxpayer's API access; legal check first) | Credentials and scope → read-only, later scope, only after SAIC and Jonathan agree | Mismatches found per month | L |

**Evaluated and not recommended now.**
- **Fully automatic posting at high confidence (as Xero auto-reconciles [X2]).** This conflicts with the rule that a
  person approves anything that posts. Keep it to one-click confirm.
- **Agent-style "do it for me" bookkeeping (QuickBooks [Q1], Digits [G3]).** Too large for the team's horizon, and it
  needs much more data to be safe.

## 5. Roadmap

Success criteria (SC): SC1 ≥95% field accuracy · SC2 ≥70% less manual entry · SC3 ≤20 s per document · SC4 <10%
exceptions · SC5 all documents routed.

### Horizon 1: next 2 weeks (UAT, to about 8 Dec)

Freeze is Thu 26 Nov 12:00; anything after the freeze lands only with Jonathan's approval.

| Item | Ties to | Metric |
|---|---|---|
| Bugs a–f [Audit §18 P0 #1–6] | SC4, SC5 | UAT walkthrough passes without workarounds |
| P2 explanations for every blocking rule (fixes bug d's message) | SC4 | Seconds to resolve an exception |
| P10 posting preview for the approver | SC5 | Admin seconds per approval |
| P1-lite: sort "To review" by risk using existing signals, plus the 5% spot-check | SC2 | Fields looked at per document |
| P3-lite: include PVs, ORs and typed documents in bank-match search, with a "why" line | SC5 | Bank lines explained per minute |
| Measurement instrumentation and the AB/BA study (section 6) | SC2 | Reduction in active seconds and touches |

### Horizon 2: next semester

| Item | Ties to | Metric |
|---|---|---|
| P4 supplier memory 2.0 and P5 auto-coding | SC1, SC2 | Corrections per document, 2nd vs 1st supplier document |
| P3 full: scored matches, match memory, bank rules (gap 6) | SC2 | Correct top-1 rate; clicks per bank line |
| P6 anomaly and duplicate 2.0 | SC4 | Duplicates caught before posting; false-positive rate |
| P7 close assistant, P8 nudges | SC2 | Days to lock; overdue days |
| P9 exception batch fix | SC4 | Exceptions cleared per action |
| P11 email-in (free route only) | SC2 | Share of documents without manual upload |
| Firm view for SAIC: one accountant, many companies (gap 9) | SC2 | Minutes per client per month |
| Recurring invoices, reminders, report drill-down and comparatives (gaps 10–12) | SC2 | Accountant time per month-end |

### Horizon 3: later

- P12 natural-language questions.
- P13 supplier statement reconciliation.
- P14 e-invoice consistency checks.
- Live bank feeds, which need a bank partnership.
- WhatsApp/mobile capture, which is paid and needs approval.

## 6. Measurement plan: showing workload reduction in UAT

**What already exists.**
- `services/routes/effort/routes.py` runs the SC2 study. The same public documents are typed manually or reviewed
  with AccordAI, active, idle and paused seconds are recorded, and edited keys are logged.
- The result counts as valid only with at least 2 participants who did both modes in both orders.
- Each session is stored as an audited `effort_study.session` event.
- `audit_logs` already records every correction, decision and posting.

**Metrics** (defined once, reported per document and per participant):

| Metric | Definition | Source |
|---|---|---|
| Active seconds per document | Timer while the review page is focused and input is active | Effort study (exists) |
| Touches per document | Field edits + type choices + decision clicks (submit, approve, fix) | `audit_logs` + `editedKeys` |
| Corrections per document | Fields with `corrected_by` set, by field | Extraction fields |
| Fields looked at | Fields shown under "only fields to check" | Review page event (add) |
| Wrong-but-verified rate | Spot-check documents where a "verified" field was wrong | 5% spot-check (P1) |
| First-pass exception rate | Documents with an open exception after reading | Validation (SC4) |
| Bank lines explained per minute | Matched, coded or ignored lines ÷ active minutes | Bank decisions in `audit_logs` |
| Admin seconds per approval | Open to decision on the approval page | Review page event (add) |

**Protocol.**
1. **Participants.** At least 2 SAIC accountants (more if available); the team does not count as participants.
2. **Material.** Two matched sets of 20 synthetic documents (same mix of invoices, bills, receipts, notes, photos)
   and one 30-line bank statement, from the KNT pack.
3. **Design.** Counterbalanced AB/BA. Participant 1 does set A manually, then set B with AccordAI; participant 2 does
   the reverse. Manual means typing into AccordAI's typed forms with AI off (same screens, so the comparison is fair).
4. **Recording.** In-app timers and counters only; no screen recording without consent; participant ids, not names.
5. **Reporting.** Median and range per metric, the n, and every failed or excluded session with the reason. No
   extrapolation beyond the tested set. If a criterion is not met, say so.
6. **Competitor baseline (optional).** Only if a free trial allows it (Bukku's free Launch plan has no extractions
   [B2]). Otherwise make no claim that AccordAI is faster than a named competitor.

**Pass lines.**
- SC2: median active seconds or touches per document ≥70% lower with AccordAI.
- SC1 and SC3: re-run the hold-out set at the release commit.
- SC4 and SC5: taken from the same UAT batch.

---

## Sources

The access date is 24 Nov 2026 for all sources. **read** = page fetched and read; **search** = from a search-result
summary, so re-open the page before quoting it.

| Key | Source | Level |
|---|---|---|
| B1 | Bukku features — https://bukku.my/features | read |
| B2 | Bukku pricing — https://bukku.my/pricing | read |
| B3 | Bukku MyInvois/Peppol — https://bukku.my/malaysia-e-invoice-compliance-lhdn-myinvois-peppol-network | read |
| B4 | Maybank × Bukku FAQ — https://www.maybank2u.com.my/iwov-resources/pdf/business/sme/faq-bukku.pdf | search |
| A1 | AutoCount Cloud features FAQ — https://autocount.freshdesk.com/support/solutions/articles/69000876765-features-questions | read |
| A2 | AutoCount bank integration listing — https://autocount.freshdesk.com/support/solutions/articles/69000859432-bank-integration-listing | read |
| A3 | UOB × AutoCount plans — https://www.uob.com.my/business/digital/accounting/autocount.page | read |
| S1 | SQL AI Easy Scan — https://sql.com.my/ai-easy-scan | read |
| S2 | SQL purchase invoice import (MyInvois) — https://cdn.sql.com.my/purchase-invoice-import | read |
| S3 | SQL Account product page (auto bank recon, 20+ banks) — https://www.sql.com.my/sqlaccount | search |
| S4 | SQL × Maybank — https://sql.com.my/maybank/ | search |
| S5 | SQL docs, auto recon — https://docs.sql.com.my/sqlacc/faq/general-ledger/auto-recon | search |
| S6 | SQL site menu (Accountant Set, SQL Einvoice, X-Mobile) — https://www.sql.com.my/sql-account | read |
| X1 | Xero Malaysia pricing — https://www.xero.com/my/pricing-plans/ | read |
| X2 | Xero automatic bank reconciliation with JAX — https://www.xero.com/my/campaign/auto-bank-reconciliation/ | read |
| X3 | Xerocon Denver 2026 — https://blog.xero.com/nz/product-updates/xerocon-denver-2026-agentic-workflows-ai-era/ | read |
| X4 | Xero MY e-invoicing — https://www.xero.com/my/accounting-software/e-invoicing/ ; Invoici setup (third party) — https://intercom.help/caltrix/en/articles/12378323-how-to-set-up-integration-of-xero-and-lhdn-through-invoici | search |
| X5 | Xero JAX — https://www.xero.com/my/jax | search |
| X6 | Xero lock dates as described by Dext — https://help.dext.com/en/s/article/lock-dates ; lock-date history request — https://productideas.xero.com/forums/939198-for-small-businesses/suggestions/45631876-history-record-changes-to-lock-dates | search |
| X7 | Xero Practice Manager — https://www.xero.com/my/xero-practice-manager | search |
| X8 | Xero bill approval via ApprovalMax — https://support.approvalmax.com/en/articles/413755-how-does-the-approval-of-xero-bills-work ; multi-level approval request — https://productideas.xero.com/forums/967139-purchase-orders-bills-inventory/suggestions/44960764-bills-ability-to-allow-multiple-levels-of-approval-for-bills?page=1 | search |
| Q1 | QuickBooks Online agents overview — https://quickbooks.intuit.com/learn-support/en-uk/help-article/accounting-bookkeeping/overview-agents-quickbooks-online/L9irCAtK4_GB_en_GB | read |
| Q2 | QuickBooks e-invoicing help hub, Malaysia — https://quickbooks.intuit.com/learn-support/en-global/help-article/invoicing/e-invoicing-help-hub-malaysia/L9fk6fZ3I_ROW_en | search |
| Q3 | QuickBooks close books — https://quickbooks.intuit.com/learn-support/en-sg/help-article/close-books/close-books-quickbooks-online/L59LelyPM_SG_en_SG | search |
| Q5 | Intuit Intelligence (no additional cost) — https://quickbooks.intuit.com/ph/resources/accounting-and-bookkeeping/intuit-intelligence-advisor-ph/ | search |
| D1 | Dext extracted data, supplier rules — https://help.dext.com/en/s/article/what-data-is-extracted-by-dext-prepare | read |
| D2 | Dext duplicate documents — https://help.dext.com/en/s/article/how-does-dext-handle-duplicate-documents | search |
| D3 | Dext Prepare on the Xero App Store (99.9% claim) — https://apps.xero.com/app/dext-prepare | search |
| H1 | Hubdoc fetch changes — https://blog.xero.com/product-updates/hubdoc-importing-docs-changing/ | search |
| H2 | Hubdoc PDF statement to CSV — https://blog.xero.com/2022/07/import-transaction-data-into-xero/ | search |
| H3 | Hubdoc forwarding (partner guide) — https://www.xero.com/us/partner-programs/partners/toolkit/hubdoc-partner-guide/email-templates | search |
| G1 | Digits — https://digits.com/ ; https://digits.com/product/month-end-close | search |
| G2 | Digits Agentic Close (CPA Practice Advisor) — https://www.cpapracticeadvisor.com/?p=184769 | search |
| G3 | Digits accounting agents — https://digits.com/blog/introducing-digits-accounting-agents/ | search |
| V1 | Vic.ai analytics and insights — https://www.vic.ai/products/processing-insights | search |
| V2 | Vic.ai fraud prevention — https://www.vic.ai/solutions/fraud-prevention | search |
| V3 | Vic.ai autonomous invoice processing — https://vic.ai/products/autonomous-invoice-processing | search |
| V4 | Sage Intacct AP anomaly detection — https://www.intacct.com/ia/docs/en_US/help_action/Accounts_Payable/Automation/about-anomaly-detection-ap-automation.htm | search |
| K1 | Booke AI on the Xero App Store — https://apps.xero.com/us/app/booke-ai ; https://apps.xero.com/my/industry/accounting/app/booke-ai/reviews | search |
| W1 | Lejar.ai listing — https://index.dodopayments.com/lejar-ai | search |
| W2 | QNE N3 AI Accounting — https://qne.cloud/my/?p=32878 | search |
| W3 | Teera.AI — https://my.linkedin.com/in/gurminderjeetkaur (brand-ambassador post; self-reported) | search |
