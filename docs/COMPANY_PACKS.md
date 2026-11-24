# Company packs (plan v2 task A1)

## KNT demo books

Ketapang Nibong Trading Sdn. Bhd. (KNT) is a fictional Malaysian trading company. Its books cover September
and October 2026 in full and November through 23 November. The pack uses the Malaysian SME chart below.
Each document's tax amount is entered as a fixed value.

```bash
python -m ai.synthetic.knt_pack
python services/admin/manage.py seed-knt
python services/admin/manage.py tally
python services/admin/manage.py reset-knt
```

Run the database commands after the [local schema setup](LOCALHOST.md). They require development mode,
a loopback PostgreSQL URL and local storage. `seed-knt` creates the books in an empty KNT workspace.
`reset-knt` clears the existing KNT Inbox and accounting data, including any legacy benchmark imports,
and rebuilds the books. It keeps KNT's existing users and files under `ai/`. The seed commands show the
passwords for newly created demo logins once, or accept `--prompt-password`.

The generator writes to `ai/synthetic/output/packs/knt/` by default. It includes the fictional company
identity, chart and contacts, source purchases named `{YYYY-MM-DD}_{CONTACT}_{TYPE}_{NUMBER}.{ext}`,
gold manifests, and one bank CSV per cutoff. Import the purchase files into Inbox to test extraction.
The local seed uses the labels to establish reproducible Inbox states and runs the approval and posting
steps through the application. Sales is entered through the typed Sales endpoints. The sales debit note
uses the invoice form and carries the original invoice reference and a debit-note subtype, since the
Sales form has no native debit-note type.

The tally checks the trial balance, P&L by account, AR and AP open items, and each month-to-date statement
against the ledger. Each statement keeps one transfer unmatched. The reconciliation shows the resulting
cumulative adjustment separately from the bank ledger balance.

### Richer demo data (`ai/synthetic/knt_extra.py`)

On top of the original fixture the seed adds, so that dropdowns, auto-fill and "recent items" have real data:

- 30 contacts (17 customers, 13 suppliers) with legal name, short code, address, SSM, TIN, SST number, phone, e-mail,
  payment terms (days) and default account. The five original contacts keep their codes. TIN, SST number, terms,
  default account, currency and category live in the contact's `fields`; SSM is its registration number.
- 41 products and services (SKU, unit, sale and purchase price, default tax-code label, income and expense account),
  4 tax codes (`SST-10`, `SST-5`, `SV-8`, `SST-0`), 5 payment terms and a "Cash in hand" account.
- September to November typed history using all of them: 16 more quotations (accepted ones become sales orders,
  delivery orders and invoices; some are expired or still open), 31 invoices, 3 credit notes, 2 debit notes, 27 typed
  supplier bills for the stock sold, and 30 payments (13 received, 17 made). Tax amounts are typed per line.
- The payments go through cash in hand, so the three bank statements and their reconciliations are exactly the
  original fixture's. `tally` also checks the contacts, products, quotation chains, bills and the cash balance.

`extend-knt` adds this data to a KNT company that already holds the original books (the hosted UAT database) without
editing anything that is there; it prints the plan by default and writes only with `--apply`. A database that is
not on this machine also needs `--allow-remote` and `KNT_EXTEND_REMOTE_HOST`. Run `tally` afterwards.

```bash
python services/admin/manage.py extend-knt --as-user <company admin email>
python services/admin/manage.py extend-knt --as-user <company admin email> --apply [--fill-blank-contacts]
```

## Evaluation and test packs

`ai/synthetic/company_pack.py` generates the demo and test books for three fictional Malaysian SMEs.
Tasks A2 (CSV imports), A3 (demo company seed), C4 (payables/receivables) and C5 (bank reconciliation)
read these files, so the formats below are a contract: add columns at the end, never rename or reorder.
Format version: `company-pack/1.0` (`generator_version` in `pack.json`).

```bash
python ai/synthetic/company_pack.py                       # all three companies, default seed and months
python ai/synthetic/company_pack.py --company trading --seed 7 --start 2026-01 --out /tmp/packs
```

| Company key | Business | Bank layout | Sales tax printed |
|---|---|---|---|
| `trading` | building and office supplies trading | `signed` | 10% |
| `services` | IT consulting and support | `debit_credit` | 8% |
| `fnb` | cafe and corporate catering | `dr_cr` | 6% |

`--bank-layout` gives every company one layout instead. All names are random word combinations, numbers are
random, e-mail domains end in `.test`, there are no logos or bank names, and every page prints
"SYNTHETIC SAMPLE". Amounts are MYR with two decimals; dates in the pack's own files are `YYYY-MM-DD`.

## Files

**`contacts.csv`**: `contact_id, type, name, category, ssm, tin, sst, address, email, phone, payment_terms_days,
default_account_code, currency`. `type` is `supplier` or `customer`; `category` is `customer`, `trade`, `rent`,
`utility` or `professional`. `address` is one line joined with ", ".

**`coa.csv`**: `code, name, type, account_class, statement, report_section, normal_balance`. `type` uses the
`ledger_accounts.account_type` values (asset, liability, equity, income, expense); `account_class` uses the
`coa_accounts.account_class` values (asset, liability, equity, revenue, cogs, expense). `statement` is
`balance_sheet` or `profit_and_loss`; `report_section` is the report line (Current assets, Revenue, Cost of
sales, Operating expenses, ...). Contra accounts (accumulated depreciation, returns) have the opposite
`normal_balance`.

**`opening_balances.csv`**: `row_type, as_at, account_code, account_name, contact_id, item_id, document_number,
document_date, due_date, debit, credit`. Rows with `row_type = account` are the trial balance at the start of
month 1 (debits equal credits; profit and loss already closed to retained earnings). Rows with `receivable` or
`payable` are the unpaid invoices behind accounts 1100 and 2000 (they sum to those balances); their `item_id`
(`<PFX>-OB-AR01`, `<PFX>-OB-AP01`) is used in `expected_matches.csv`. They have no document file.

**`documents/<YYYY-MM>/<document_id>.pdf|jpg`** and **`documents.csv`**: `document_id, month, kind, direction,
contact_id, counterparty_name, document_number, document_date, due_date, related_document_id, currency, subtotal,
tax, rounding, total, account_code, payment_method, file, file_format, manifest`. `kind` is `sales_invoice`,
`supplier_invoice` (including rent and professional fees), `utility_bill`, `receipt`, `credit_note` or
`debit_note`; `direction` is `sales` or `purchase`; `related_document_id` links a note to its invoice;
`account_code` is the expected coding; `payment_method` (receipts only) is `card`, `cash` or `e_wallet` (only
card receipts appear in the bank). `subtotal + tax + rounding = total` (rounding is non-zero only on cash-rounded
receipts).

**`invoices.json`, `bills.json`, `receipts.json`**: gold labels in the frozen ground-truth manifest schema
(`ai/evaluation/dataset_contract.py`), purpose `SCHEMA_TEST_ONLY`; `sample_id` equals `document_id`.
Credit and debit notes are in `invoices.json`, as in `generate.py`.

**`bank_<YYYY-MM>.csv`**: rows posted in that month, oldest first, with a running balance. There is no
opening-balance row: opening balance = first `Balance` minus first amount (also in `pack.json`).

| Layout | Columns | Existing parser (`services/bank/csv_parser.py`) |
|---|---|---|
| `signed` | `Date` (YYYY-MM-DD), `Description`, `Reference`, `Amount` (signed), `Balance` | yes: map debit and credit both to `Amount`, `date_format=iso` |
| `debit_credit` | `Transaction Date` (DD/MM/YYYY), `Value Date`, `Transaction Description`, `Ref No`, `Debit`, `Credit`, `Balance` (thousands separators) | yes: `date_format=dmy` |
| `dr_cr` | `Posting Date` (DD-MM-YYYY), `Narrative 1`, `Narrative 2` (name and reference), `Amount` (unsigned), `DR/CR`, `Balance`, `Currency` | no: needs saved column mappings (task C5) |

The exact mapping is in `pack.json` under `bank.parser`. `company_pack.read_bank_file(path, layout)` reads all
three layouts (for tests and seeding).

**`expected_matches.csv`**: one row per bank row. `bank_file, source_row, bank_row_id, posted_date,
payment_date, amount, category, match_type, document_ids, allocated_amounts, note`.
- `source_row` is the 1-based data row, the same number the bank parser reports.
- `payment_date` is when the payment was made; `posted_date - payment_date` is 0-5 days.
- `category`: `document`, `fee`, `interest` or `unmatched` (payroll, statutory contributions, cash
  withdrawals and deposits, card terminal settlements, a director advance, an unidentified deposit).
- `match_type` (documents only): `exact`, `partial`, `combined` (one transfer for two invoices of one
  contact), `net_of_credit_note`, `with_debit_note`, `refund` (supplier refunds a credit note) or `card`.
- `document_ids` and `allocated_amounts` are `;`-separated and aligned. Allocations are signed like the bank
  amount and sum to it (a credit note inside a customer receipt is negative). An id is a `document_id` or an
  opening `item_id`.

**`pack.json`**: seed, generator version, the company identity, period, bank layout and parser mapping,
opening and closing balance per month, counts, totals per document kind, opening trial-balance totals,
receivables and payables (opening and closing) and `open_items_closing`, the items still outstanding after
month 3 (credit notes negative).

## Invariants (checked by `tests/synthetic/test_company_pack.py`)

- Same seed, same bytes.
- The three manifests pass `validate_collection`; every document total equals its lines plus the printed tax.
- The opening trial balance balances and ties to the open items and the bank's opening balance.
- Bank: opening + movements = closing, month by month; the balance never goes below zero.
- Every bank row has one gold row; every referenced id exists; allocations sum to the bank amount.
- Closing receivables and payables equal the documents and opening items less the allocations in
  `expected_matches.csv`; no item is over-allocated; every opening item receives a payment.

## Team demo companies (TKK, EHB, CDA) and team work

Plan and decisions: [TEAM_DEMO_DATA_PLAN.md](TEAM_DEMO_DATA_PLAN.md). Three more fictional companies, built
with the same steps as KNT (`services/admin/company_seed.py`). The scenario comes from
`ai/synthetic/sme_profiles.py` and `ai/synthetic/sme_books.py`, with a fixed seed, so the same code always builds
the same books. Every company has:

- Sep–Nov 2026 books, with a cut-off of 23 Nov;
- an opening-funds journal;
- contacts with full headers, products, tax-code labels and payment terms;
- quotation chains into invoices;
- credit and debit notes, one of them paid together with its invoice in one transfer;
- typed supplier bills, some paid and some open;
- 14 Inbox documents stored as already-read records (no AI is called);
- three bank statements that reconcile, each with one unidentified line;
- September closed (locked).

| Code | Company | Trade, place | Bank account label | Statement layout |
|---|---|---|---|---|
| TKK | Teratai Kopi & Katering Sdn. Bhd. | café and catering, Kuching | Maybank Current Account ••4402 | signed amount, ISO dates |
| EHB | Enggang Hardware & Building Supply Sdn. Bhd. | hardware and rental, Miri | RHB Current Account ••7715 | Debit/Credit columns, dd/mm/yyyy |
| CDA | Cendana Advisory Sdn. Bhd. | accounting and tax advisory, Kota Kinabalu | CIMB Current Account ••3360 | signed amount, ISO dates |

The banks are real Malaysian banks named on the account label (as KNT names Public Bank). Account numbers are
invented, and no bank's design is copied. Tax is typed as printed on every document.

### Commands

```bash
python services/admin/manage.py build-pack --company TKK           # files only
python services/admin/manage.py seed-company --company TKK         # local, from empty (then tallies)
python services/admin/manage.py reset-company --company TKK        # local: wipe TKK (users kept) and rebuild
python services/admin/manage.py apply-company --company TKK        # dry run; add --apply (and --allow-remote)
python services/admin/manage.py tally --company TKK                # or --all; read-only
python services/admin/manage.py team-accounts --roster PRIVATE.json             # dry run; then --apply
python services/admin/manage.py team-inbox --company TKK --roster PRIVATE.json  # dry run; then --apply
python services/admin/manage.py team-upload-pack --roster PRIVATE.json          # live-upload test files
```

**The roster** is a private JSON file kept outside the repository. The format is in
`docs/demo/team_roster.example.json` (synthetic addresses).
- Each slot (`TKK.accountant.2`) gives one person one role in one company. A person who works in several companies
  uses a `+company` address for each company after their first; Gmail delivers it to the same inbox.
- Emails are always printed masked.

**`team-accounts`** creates missing users as active with no password.
- Each person sets their own password with Forgot password (email code).
- Local admins enrol an authenticator at first sign-in.
- It never changes an existing user. An address that already belongs to another company or role, or a full seat
  limit, stops the run before anything is written.

**`team-inbox`** gives each roster accountant their own documents.
- A full set is 12: 3 to review, 3 ready, 2 waiting for approval, 3 needing a fix (unknown supplier, possible
  duplicate, missing number) and 1 approved and posted, including a supplier credit note and debit note. A starter
  set is 6.
- The documents are made by the company's fixture logins through the app (submit, approve, post) and assigned with
  the app's own assignment (workload row, audit entry, notification). They are temporarily set active for the run.
- Amounts follow each supplier's usual amount, so the app's unusual-amount check stays quiet except where a finding
  is meant to appear.
- A second run adds nothing.

**`tally`** knows about the team documents. Each one must still be in the lane it was made for, and the posted ones
count in P&L and AP. Run it right after an apply: once people work in a company, its books move on.

### Applying to hosted UAT (only on Jonathan's explicit go-ahead)

1. In a shell with the uat `DATABASE_URL` (read from the private env file, never printed) and
   `COMPANY_PACK_REMOTE_HOST` set to its host, run every command without `--apply`, and check the plan.
2. Create a Neon snapshot branch of `uat` (`uat-pre-team-demo-<yyyymmdd-hhmm>`) and record its id.
3. Run `apply-company --company TKK --apply --allow-remote`, then EHB, then CDA. On a remote database the fixture
   logins get no password and are disabled at the end.
4. Run `team-accounts --apply --allow-remote`, then `team-inbox --company … --apply --allow-remote` for each company,
   KNT included. Without the S3 bucket variables in the shell, Inbox files stay on this machine and the hosted
   preview cannot show them (the command warns).
5. Run `tally --all --allow-remote`, then sign in as one admin and one accountant per company and check the lanes.
6. Rollback: restore `uat` from the snapshot branch. This also discards anything done after the snapshot.
