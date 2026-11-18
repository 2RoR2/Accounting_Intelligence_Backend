# Company packs (plan v2 task A1)

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
