# Invoice JSON Schema Documentation — Module 3 (Validation Engine)

This defines the exact field names, types, and rules that Module 2 (Extraction Engine) must output and that Module 3 (Validation Engine) enforces before data reaches PostgreSQL. Field names are case-sensitive and must match exactly.

## Invoice object

| Field | Type | Required | Description | Validation rule |
|---|---|---|---|---|
| `invoice_number` | string | Yes | Unique invoice/document identifier (matches extraction output and `standardised_records.invoice_number` in the DB) | Non-empty string |
| `vendor_name` | string | Yes | Name of the vendor/supplier on the invoice | Non-empty string |
| `invoice_date` | string (`YYYY-MM-DD`) | Yes | Date the invoice was issued | Must be a valid ISO date |
| `due_date` | string (`YYYY-MM-DD`) | No | Payment due date | If present, must not be earlier than `invoice_date` |
| `line_items` | array of `LineItem` | Yes | Itemised charges on the invoice | At least 1 item |
| `subtotal` | decimal string | Yes | Sum of all line item amounts, before tax | Must equal `sum(line_items[].amount)` (±0.01 tolerance) |
| `tax_amount` | decimal string | Yes | Total tax applied | Must be ≥ 0 |
| `total_amount` | decimal string | Yes | Final invoice total | Must equal `subtotal + tax_amount` (±0.01 tolerance) |
| `currency` | string (3 letters) | No | ISO currency code, defaults to `MYR` | Exactly 3 letters |

## LineItem object

| Field | Type | Required | Description | Validation rule |
|---|---|---|---|---|
| `description` | string | Yes | What was purchased | Non-empty string |
| `quantity` | number | Yes | Quantity purchased | Must be > 0 |
| `unit_price` | decimal string | Yes | Price per unit | Must be > 0 |
| `amount` | decimal string | Yes | Line total | Must equal `quantity * unit_price` (±0.01 tolerance) |

## Notes for the team

- **Monetary fields are sent as strings, not floats.** Floats cause rounding errors in financial calculations; the Validation Engine converts them to Python `Decimal` internally. Module 2 should output amounts like `"125.00"`, not `125.0`.
- **Dates must be ISO format (`YYYY-MM-DD`)**, not `DD/MM/YYYY` or any locale-specific format, to avoid ambiguity.
- **±0.01 tolerance** is applied to all cross-field math checks to account for legitimate rounding differences in the source document — not a bug.
- **Monetary fields may contain thousand-separator commas** (e.g. `"2,193,713.00"`) as output by the extraction engine — the Validation Engine strips these before parsing, so commas are accepted, but plain digit strings are still preferred where possible.
- **Open items still pending decision** (flagged in the FYP overview, not yet resolved): whether Module 2's OCR confidence scores will be passed into this schema and used to route low-confidence fields into the exception queue, and the exact rules for exception vs. hard rejection.
- **Open items needing team alignment (as of latest extraction sample):** `invoice_date` and `line_items` are not yet present in the extraction engine's output — without these, subtotal/line-item math validation cannot run. Also, the extraction engine outputs `tax_amount`/`total_amount` while the DB schema uses `tax`/`total` column names — needs a decision on where the rename happens (extraction, validation, or DB insert layer).

## Example — valid payload

```json
{
  "invoice_number": "INV-2026-0091",
  "vendor_name": "Sinar Trading Sdn Bhd",
  "invoice_date": "2026-09-01",
  "due_date": "2026-09-30",
  "line_items": [
    {"description": "A4 Paper (Ream)", "quantity": 10, "unit_price": "12.50", "amount": "125.00"},
    {"description": "Printer Toner", "quantity": 2, "unit_price": "180.00", "amount": "360.00"}
  ],
  "subtotal": "485.00",
  "tax_amount": "29.10",
  "total_amount": "514.10",
  "currency": "MYR"
}
```
