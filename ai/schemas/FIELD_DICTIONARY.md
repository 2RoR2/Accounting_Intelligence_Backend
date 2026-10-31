# Shared field dictionary v1.0.0

Generated from `validation/anomaly_evaluation/field_dictionary.py`. All fields are optional; omitted means UNLABELLED.

Money, quantities and percentage points use exact decimal strings. IDs stay text.

## invoice

| Scope | Tier | Key | Type | Definition |
|---|---|---|---|---|
| document | core | `invoice_number` | text | Printed identifier of this invoice; not an original invoice linked by a note. |
| document | core | `invoice_type` | text | Printed invoice or credit/debit-note type wording; not AP/AR or review status. |
| document | core | `invoice_reference` | text | Separately printed invoice reference other than its primary invoice number. |
| document | core | `supplier_name` | text | Invoice seller/issuer name; carries no AP/AR direction. |
| document | core | `supplier_address` | text | Printed address of the invoice seller, preserving all supplied address lines. |
| document | core | `customer_name` | text | Printed buyer/customer name; carries no AP/AR direction. |
| document | core | `customer_address` | text | Printed customer/buyer address; not automatically billing or service address. |
| document | core | `invoice_date` | date | Issue date printed for this invoice; not due, service, payment or upload date. |
| document | core | `due_date` | date | Explicit printed payment due date; do not calculate it from payment terms. |
| document | core | `currency` | currency | Unambiguously printed currency normalized to a three-letter code; otherwise null. |
| document | core | `subtotal` | decimal | Printed subtotal in its labelled scope; do not derive tax-exclusive total. |
| document | core | `tax` | decimal | Printed document-level tax amount, distinct from each category or item tax and tax rates. |
| document | core | `total` | decimal | Printed document grand total; never substitute amount due, sales, tender or payment. |
| document | core | `discount` | decimal | Printed document-level discount amount, not a rate or a sum inferred from item discounts. |
| document | extended | `supplier_registration_id` | text | Printed seller business-registration identifier, distinct from tax identifiers. |
| document | extended | `supplier_tax_id` | text | Printed seller tax identifier with no more specific supported tax scheme; do not duplicate TIN or SST labels. |
| document | extended | `customer_registration_id` | text | Printed customer business-registration identifier, distinct from tax identifiers. |
| document | extended | `customer_tax_id` | text | Printed customer tax identifier with no more specific supported tax scheme; do not duplicate TIN or SST labels. |
| document | extended | `purchase_order_number` | text | Printed purchase-order identifier; not a contract, invoice or delivery reference. |
| document | extended | `contract_reference` | text | Printed identifier of an associated contract. |
| document | extended | `delivery_reference` | text | Printed delivery-note/shipment reference; do not infer a goods-receipt record. |
| document | extended | `payment_terms` | text | Printed terms of payment as text; do not convert them to a due date without that date being printed. |
| document | extended | `payment_instructions` | text | Printed payment instruction text, distinct from the separately labelled account and method fields. |
| document | extended | `payment_reference` | text | Printed reference for a payment/transaction; not evidence of settlement by itself. |
| document | extended | `payment_method` | text | Printed payment method text; do not infer paid/approved status. |
| document | extended | `payment_bank_name` | text | Printed bank named in invoice payment instructions, not a customer or utility account provider. |
| document | extended | `payment_account_number` | text | Printed bank account in invoice payment instructions; preserve leading zeros. |
| document | extended | `billing_address` | text | Explicitly labelled billing address; do not copy a generic customer address into this field. |
| document | extended | `shipping_address` | text | Explicitly labelled ship-to address, distinct from billing and customer addresses. |
| document | extended | `allowance_total` | decimal | Explicit printed document allowance total; not an inferred discount sum. |
| document | extended | `charge_total` | decimal | Explicit printed invoice document charge total, excluding unrelated bill balances. |
| document | extended | `tax_exclusive_total` | decimal | Explicit total excluding tax; do not alias a generic subtotal. |
| document | extended | `tax_inclusive_total` | decimal | Explicit total including tax; keep distinct from grand total. |
| document | extended | `prepaid_amount` | decimal | Printed advance/prepayment amount, distinct from paid amount. |
| document | extended | `paid_amount` | decimal | Printed invoice paid amount; not an accounting settlement status. |
| document | extended | `amount_due` | decimal | Printed balance requested for payment; distinct from total and previous balance. |
| document | extended | `rounding` | decimal | Printed signed monetary rounding adjustment; no rounding is introduced by annotation. |
| document | extended | `service_start_date` | date | Printed start date of the service interval at document or item scope. |
| line | conditional | `service_start_date` | date | Printed start date of the service interval at document or item scope. |
| document | extended | `service_end_date` | date | Printed end date of the service interval at document or item scope. |
| line | conditional | `service_end_date` | date | Printed end date of the service interval at document or item scope. |
| document | extended | `delivery_date` | date | Printed actual delivery date at document or item scope; not a received/upload timestamp. |
| line | conditional | `delivery_date` | date | Printed actual delivery date at document or item scope; not a received/upload timestamp. |
| document | extended | `payment_date` | date | Explicit printed date of a payment; not inferred from issue date or payment method. |
| document | conditional | `myinvois_uuid` | text | Printed UUID assigned to this e-invoice; not the supplier invoice number or original invoice UUID. |
| document | conditional | `myinvois_long_id` | text | Printed MyInvois long identifier, kept distinct from the invoice UUID. |
| document | conditional | `myinvois_validation_reference` | text | Separately labelled validation reference; do not duplicate a UUID or long ID here. |
| document | conditional | `myinvois_qr_payload` | text | Exact payload independently decoded from a printed QR code; evidence must identify that code. |
| document | conditional | `supplier_tin` | text | Explicitly identified seller TIN; do not also count it as a generic supplier_tax_id. |
| document | conditional | `customer_tin` | text | Explicitly identified buyer TIN; do not also count it as a generic customer_tax_id. |
| document | conditional | `supplier_sst_id` | text | Explicitly identified seller SST registration identifier, distinct from TIN and company registration. |
| document | conditional | `customer_sst_id` | text | Explicitly identified buyer SST registration identifier, distinct from TIN and company registration. |
| document | conditional | `supplier_msic_code` | text | Printed seller business-classification code, kept as text including leading zeros. |
| document | conditional | `supplier_business_activity` | text | Printed seller business-activity description, not a model-inferred industry. |
| document | conditional | `credit_note_number` | text | Printed identifier of this credit note; invoice_number is reserved for invoice documents. |
| document | conditional | `debit_note_number` | text | Printed identifier of this debit note; invoice_number is reserved for invoice documents. |
| document | conditional | `original_invoice_number` | text | Printed number of an earlier invoice referenced by this document; not this document number. |
| document | conditional | `original_invoice_uuid` | text | Printed UUID of an earlier linked invoice, distinct from its invoice number. |
| document | conditional | `note_reason` | text | Printed reason for a credit/debit note or adjustment, without inferred accounting treatment. |
| document | conditional | `industry_reference` | text | Printed industry-specific reference with its original label retained in printed_text. |
| document | conditional | `myinvois_validation_datetime` | text | Printed timestamp preserved as text, including timezone if printed. |
| line | core | `description` | text | Printed item or charge-row description in its own row/group, preserving multiline text. |
| charge_breakdown | extended | `description` | text | Printed item or charge-row description in its own row/group, preserving multiline text. |
| line | core | `quantity` | decimal | Explicit printed row quantity as an exact decimal string; not inferred by dividing totals. |
| charge_breakdown | extended | `quantity` | decimal | Explicit printed row quantity as an exact decimal string; not inferred by dividing totals. |
| line | core | `unit_price` | decimal | Explicit printed price per stated row unit; not derived by dividing line amount by quantity. |
| charge_breakdown | extended | `unit_price` | decimal | Explicit printed price per stated row unit; not derived by dividing line amount by quantity. |
| line | core | `line_total` | decimal | Printed row amount; preserve whether tax-inclusive in annotation notes. |
| line | extended | `item_code` | text | Printed item/SKU code, preserved as text including leading zeros. |
| line | extended | `unit` | text | Printed row unit of measure; not an inferred product category or currency. |
| charge_breakdown | extended | `unit` | text | Printed row unit of measure; not an inferred product category or currency. |
| line | extended | `tax_category` | text | Printed tax-category code/name for this item or tax breakdown; not inferred from the rate. |
| tax_breakdown | extended | `tax_category` | text | Printed tax-category code/name for this item or tax breakdown; not inferred from the rate. |
| line | extended | `line_discount` | decimal | Printed item discount amount, distinct from document discount or item discount rate. |
| line | extended | `line_discount_rate` | decimal | Printed item discount percentage in percentage points. |
| line | extended | `line_tax` | decimal | Printed tax amount for this item, distinct from document/category tax. |
| line | extended | `line_tax_rate` | decimal | Printed item tax percentage in percentage points. |
| line | extended | `line_allowance` | decimal | Printed allowance amount for this item; not inferred from unit price and quantity. |
| line | extended | `line_charge` | decimal | Printed additional charge for this item, distinct from document charge total. |
| line | conditional | `myinvois_item_classification` | text | Printed classification code for this invoice item; not a predicted MSIC/business code. |
| tax_breakdown | extended | `tax_exemption_reason` | text | Printed reason/code for exemption in this tax category; never inferred from zero tax. |
| tax_breakdown | extended | `tax_rate` | decimal | Printed percentage expressed in percentage points (6% -> "6"). |
| tax_breakdown | extended | `taxable_amount` | decimal | Printed tax base for this category, distinct from document subtotal and tax amount. |
| tax_breakdown | extended | `tax_amount` | decimal | Printed tax amount within this repeated tax category, distinct from document tax and line_tax. |
| charge_breakdown | extended | `charge_category` | text | Printed category/reason for this charge or allowance row; preserve the label and sign. |
| charge_breakdown | extended | `charge_amount` | decimal | Printed signed amount for this charge/allowance row; category distinguishes an allowance from a charge. |
| additional_attributes | conditional | `attribute_name` | text | Original printed label of an additional attribute without an existing canonical key. |
| additional_attributes | conditional | `attribute_value` | text | Additional printed attribute text; do not use to bypass known typed keys. |

## bill

| Scope | Tier | Key | Type | Definition |
|---|---|---|---|---|
| document | core | `customer_name` | text | Printed buyer/customer name; carries no AP/AR direction. |
| document | core | `customer_address` | text | Printed customer/buyer address; not automatically billing or service address. |
| document | core | `due_date` | date | Explicit printed payment due date; do not calculate it from payment terms. |
| document | core | `currency` | currency | Unambiguously printed currency normalized to a three-letter code; otherwise null. |
| document | core | `subtotal` | decimal | Printed subtotal in its labelled scope; do not derive tax-exclusive total. |
| document | core | `tax` | decimal | Printed document-level tax amount, distinct from each category or item tax and tax rates. |
| document | core | `total` | decimal | Printed document grand total; never substitute amount due, sales, tender or payment. |
| document | extended | `discount` | decimal | Printed document-level discount amount, not a rate or a sum inferred from item discounts. |
| document | extended | `payment_reference` | text | Printed reference for a payment/transaction; not evidence of settlement by itself. |
| document | extended | `payment_method` | text | Printed payment method text; do not infer paid/approved status. |
| document | core | `amount_due` | decimal | Printed balance requested for payment; distinct from total and previous balance. |
| document | extended | `rounding` | decimal | Printed signed monetary rounding adjustment; no rounding is introduced by annotation. |
| line | conditional | `service_start_date` | date | Printed start date of the service interval at document or item scope. |
| line | conditional | `service_end_date` | date | Printed end date of the service interval at document or item scope. |
| line | conditional | `delivery_date` | date | Printed actual delivery date at document or item scope; not a received/upload timestamp. |
| document | extended | `payment_date` | date | Explicit printed date of a payment; not inferred from issue date or payment method. |
| document | core | `bill_number` | text | Printed primary bill identifier; do not duplicate a generic bill reference unless separately printed. |
| document | core | `bill_reference` | text | Printed generic bill reference; a bill-number alias needs printed-role confirmation. |
| document | core | `provider_name` | text | Printed issuer/provider of the bill; carries no AP/AR or settlement inference. |
| document | core | `provider_address` | text | Printed address of the bill issuer/provider, not the customer service location. |
| document | core | `bill_date` | date | Printed bill issue date; not due date or billing-period boundary. |
| document | extended | `customer_account_number` | text | Printed customer/service account with the bill provider; not a bank payment account. |
| document | extended | `service_address` | text | Explicit printed address where the billed service is supplied. |
| document | extended | `billing_period` | text | Complete printed billing-period text; start/end dates are separately normalized components when supported. |
| document | extended | `meter_number` | text | Printed meter identifier at document or repeated-meter scope; no value for documents without a meter. |
| meter_readings | conditional | `meter_number` | text | Printed meter identifier at document or repeated-meter scope; no value for documents without a meter. |
| document | extended | `consumption_unit` | text | Printed unit for measured usage, preserving the source notation. |
| meter_readings | conditional | `consumption_unit` | text | Printed unit for measured usage, preserving the source notation. |
| document | extended | `tariff_name` | text | Printed tariff/plan name; not a numeric rate or an inferred service category. |
| meter_readings | conditional | `tariff_name` | text | Printed tariff/plan name; not a numeric rate or an inferred service category. |
| document | extended | `billing_period_start` | date | Start date supported by an unambiguous printed billing interval; never derived from bill date. |
| document | extended | `billing_period_end` | date | End date supported by an unambiguous printed billing interval; never derived from due date. |
| document | extended | `previous_reading_date` | date | Printed date associated with the previous meter reading. |
| meter_readings | conditional | `previous_reading_date` | date | Printed date associated with the previous meter reading. |
| document | extended | `current_reading_date` | date | Printed date associated with the current meter reading. |
| meter_readings | conditional | `current_reading_date` | date | Printed date associated with the current meter reading. |
| document | extended | `previous_reading` | decimal | Printed previous cumulative meter reading; not previous-period consumption. |
| meter_readings | conditional | `previous_reading` | decimal | Printed previous cumulative meter reading; not previous-period consumption. |
| document | extended | `current_reading` | decimal | Printed current cumulative meter reading; not current charges or usage. |
| meter_readings | conditional | `current_reading` | decimal | Printed current cumulative meter reading; not current charges or usage. |
| document | extended | `consumption` | decimal | Explicit printed usage quantity; do not derive it by subtracting meter readings. |
| meter_readings | conditional | `consumption` | decimal | Explicit printed usage quantity; do not derive it by subtracting meter readings. |
| document | extended | `tariff_rate` | decimal | Printed monetary rate per stated consumption unit; preserve that unit and rate tier in evidence. |
| meter_readings | conditional | `tariff_rate` | decimal | Printed monetary rate per stated consumption unit; preserve that unit and rate tier in evidence. |
| document | extended | `previous_balance` | decimal | Printed balance brought forward before current-period charges. |
| document | extended | `arrears` | decimal | Printed overdue amount; not assumed equal to previous balance. |
| document | extended | `current_charges` | decimal | Printed charges for the current billing period only. |
| document | extended | `adjustment_total` | decimal | Printed signed bill adjustment amount; do not substitute credits or calculate a net balance. |
| document | extended | `credit_total` | decimal | Printed bill credit amount with its source sign; never infer it from previous payments. |
| document | extended | `late_fee` | decimal | Printed late-payment fee amount; not an inferred overdue balance. |
| document | extended | `payment_amount` | decimal | Printed bill payment amount; not current charges or amount due. |
| document | extended | `service_charge` | decimal | Printed document-level service charge; not a sum of all charge rows. |
| document | extended | `tips` | decimal | Explicit printed tip/gratuity amount; do not infer it from tender minus total. |
| document | conditional | `jompay_biller_code` | text | Printed JomPAY biller code, preserved as text. |
| document | conditional | `jompay_reference_1` | text | Printed JomPAY Ref-1, preserved as text including leading zeros. |
| document | conditional | `jompay_reference_2` | text | Printed JomPAY Ref-2; not a copy of Ref-1 when unprinted. |
| document | conditional | `biller_reference` | text | Separately printed biller/payment reference with its original label; not a duplicated bill number. |
| document | conditional | `table_number` | text | Printed restaurant table identifier; not a utility account or order identifier. |
| document | conditional | `order_number` | text | Printed restaurant/service order identifier; not automatically the bill number. |
| line | core | `description` | text | Printed item or charge-row description in its own row/group, preserving multiline text. |
| charge_breakdown | extended | `description` | text | Printed item or charge-row description in its own row/group, preserving multiline text. |
| line | core | `quantity` | decimal | Explicit printed row quantity as an exact decimal string; not inferred by dividing totals. |
| charge_breakdown | extended | `quantity` | decimal | Explicit printed row quantity as an exact decimal string; not inferred by dividing totals. |
| line | core | `unit_price` | decimal | Explicit printed price per stated row unit; not derived by dividing line amount by quantity. |
| charge_breakdown | extended | `unit_price` | decimal | Explicit printed price per stated row unit; not derived by dividing line amount by quantity. |
| line | core | `line_total` | decimal | Printed row amount; preserve whether tax-inclusive in annotation notes. |
| line | extended | `item_code` | text | Printed item/SKU code, preserved as text including leading zeros. |
| line | extended | `unit` | text | Printed row unit of measure; not an inferred product category or currency. |
| charge_breakdown | extended | `unit` | text | Printed row unit of measure; not an inferred product category or currency. |
| line | extended | `tax_category` | text | Printed tax-category code/name for this item or tax breakdown; not inferred from the rate. |
| tax_breakdown | extended | `tax_category` | text | Printed tax-category code/name for this item or tax breakdown; not inferred from the rate. |
| line | extended | `line_discount` | decimal | Printed item discount amount, distinct from document discount or item discount rate. |
| line | extended | `line_discount_rate` | decimal | Printed item discount percentage in percentage points. |
| line | extended | `line_tax` | decimal | Printed tax amount for this item, distinct from document/category tax. |
| line | extended | `line_tax_rate` | decimal | Printed item tax percentage in percentage points. |
| line | extended | `line_allowance` | decimal | Printed allowance amount for this item; not inferred from unit price and quantity. |
| line | extended | `line_charge` | decimal | Printed additional charge for this item, distinct from document charge total. |
| tax_breakdown | extended | `tax_exemption_reason` | text | Printed reason/code for exemption in this tax category; never inferred from zero tax. |
| tax_breakdown | extended | `tax_rate` | decimal | Printed percentage expressed in percentage points (6% -> "6"). |
| tax_breakdown | extended | `taxable_amount` | decimal | Printed tax base for this category, distinct from document subtotal and tax amount. |
| tax_breakdown | extended | `tax_amount` | decimal | Printed tax amount within this repeated tax category, distinct from document tax and line_tax. |
| charge_breakdown | extended | `charge_category` | text | Printed category/reason for this charge or allowance row; preserve the label and sign. |
| charge_breakdown | extended | `charge_amount` | decimal | Printed signed amount for this charge/allowance row; category distinguishes an allowance from a charge. |
| additional_attributes | conditional | `attribute_name` | text | Original printed label of an additional attribute without an existing canonical key. |
| additional_attributes | conditional | `attribute_value` | text | Additional printed attribute text; do not use to bypass known typed keys. |

## receipt

| Scope | Tier | Key | Type | Definition |
|---|---|---|---|---|
| document | extended | `customer_name` | text | Printed buyer/customer name; carries no AP/AR direction. |
| document | core | `currency` | currency | Unambiguously printed currency normalized to a three-letter code; otherwise null. |
| document | core | `subtotal` | decimal | Printed subtotal in its labelled scope; do not derive tax-exclusive total. |
| document | core | `tax` | decimal | Printed document-level tax amount, distinct from each category or item tax and tax rates. |
| document | core | `total` | decimal | Printed document grand total; never substitute amount due, sales, tender or payment. |
| document | extended | `discount` | decimal | Printed document-level discount amount, not a rate or a sum inferred from item discounts. |
| document | extended | `payment_reference` | text | Printed reference for a payment/transaction; not evidence of settlement by itself. |
| document | extended | `payment_method` | text | Printed payment method text; do not infer paid/approved status. |
| document | extended | `rounding` | decimal | Printed signed monetary rounding adjustment; no rounding is introduced by annotation. |
| line | conditional | `service_start_date` | date | Printed start date of the service interval at document or item scope. |
| line | conditional | `service_end_date` | date | Printed end date of the service interval at document or item scope. |
| line | conditional | `delivery_date` | date | Printed actual delivery date at document or item scope; not a received/upload timestamp. |
| document | extended | `service_charge` | decimal | Printed document-level service charge; not a sum of all charge rows. |
| document | extended | `tips` | decimal | Explicit printed tip/gratuity amount; do not infer it from tender minus total. |
| document | core | `merchant_name` | text | Printed merchant issuing the receipt; carries no AP/AR inference. |
| document | core | `merchant_address` | text | Printed merchant address on the receipt, distinct from customer information. |
| document | core | `receipt_number` | text | Printed primary receipt identifier; not an original/refunded receipt reference. |
| document | core | `receipt_reference` | text | Separately printed generic receipt reference, not a duplicate primary receipt number. |
| document | core | `transaction_date` | date | Printed date of the receipt transaction, not refund, void, issue/upload or payment processing date. |
| document | core | `transaction_time` | time | Printed local transaction time in HH:MM[:SS]; do not invent timezone or missing seconds. |
| document | extended | `branch_name` | text | Printed merchant branch/outlet name, distinct from merchant legal name. |
| document | extended | `branch_id` | text | Printed branch/outlet code; preserve leading zeros. |
| document | extended | `cashier_name` | text | Printed cashier/operator name; do not infer it from an operator code. |
| document | extended | `cashier_id` | text | Printed cashier/operator identifier, distinct from cashier name. |
| document | extended | `terminal_id` | text | Printed payment/POS terminal identifier, distinct from register identifier. |
| document | extended | `register_id` | text | Printed cash register identifier, distinct from terminal identifier. |
| document | extended | `original_receipt_reference` | text | Printed reference to an earlier receipt linked by a refund/void; not this receipt identifier. |
| document | extended | `refund_reference` | text | Printed identifier of this refund transaction, distinct from the original receipt reference. |
| document | extended | `void_reference` | text | Printed identifier of this void transaction, distinct from the original receipt reference. |
| document | extended | `refund_reason` | text | Printed explanation for a refund; no invented accounting categorization. |
| document | extended | `customer_id` | text | Printed customer identifier; not a loyalty reference unless explicitly identified as such. |
| document | extended | `loyalty_reference` | text | Legitimately available printed loyalty/member reference; do not infer customer identity. |
| document | extended | `total_sales` | decimal | Printed sales aggregate; distinct from receipt total and amount paid. |
| document | extended | `amount_paid` | decimal | Printed receipt payment amount; not inferred from payment method or total. |
| document | extended | `tender` | decimal | Printed amount tendered; may exceed total or amount paid. |
| document | extended | `change` | decimal | Printed change returned. |
| document | extended | `refund_amount` | decimal | Printed monetary refund amount with original sign retained; not automatically negative total. |
| document | extended | `refund_indicator` | boolean | Boolean meaning of explicit printed refund wording; an unprinted indicator is not false. |
| document | extended | `void_indicator` | boolean | Boolean meaning of explicit printed void wording; an unprinted indicator is not false. |
| document | extended | `refund_date` | date | Explicit printed refund transaction date, distinct from original transaction date. |
| document | extended | `void_date` | date | Explicit printed void transaction date, distinct from original transaction date. |
| line | core | `description` | text | Printed item or charge-row description in its own row/group, preserving multiline text. |
| charge_breakdown | extended | `description` | text | Printed item or charge-row description in its own row/group, preserving multiline text. |
| line | core | `quantity` | decimal | Explicit printed row quantity as an exact decimal string; not inferred by dividing totals. |
| charge_breakdown | extended | `quantity` | decimal | Explicit printed row quantity as an exact decimal string; not inferred by dividing totals. |
| line | core | `unit_price` | decimal | Explicit printed price per stated row unit; not derived by dividing line amount by quantity. |
| charge_breakdown | extended | `unit_price` | decimal | Explicit printed price per stated row unit; not derived by dividing line amount by quantity. |
| line | core | `line_total` | decimal | Printed row amount; preserve whether tax-inclusive in annotation notes. |
| line | extended | `item_code` | text | Printed item/SKU code, preserved as text including leading zeros. |
| line | extended | `unit` | text | Printed row unit of measure; not an inferred product category or currency. |
| charge_breakdown | extended | `unit` | text | Printed row unit of measure; not an inferred product category or currency. |
| line | extended | `tax_category` | text | Printed tax-category code/name for this item or tax breakdown; not inferred from the rate. |
| tax_breakdown | extended | `tax_category` | text | Printed tax-category code/name for this item or tax breakdown; not inferred from the rate. |
| line | extended | `line_discount` | decimal | Printed item discount amount, distinct from document discount or item discount rate. |
| line | extended | `line_discount_rate` | decimal | Printed item discount percentage in percentage points. |
| line | extended | `line_tax` | decimal | Printed tax amount for this item, distinct from document/category tax. |
| line | extended | `line_tax_rate` | decimal | Printed item tax percentage in percentage points. |
| line | extended | `line_allowance` | decimal | Printed allowance amount for this item; not inferred from unit price and quantity. |
| line | extended | `line_charge` | decimal | Printed additional charge for this item, distinct from document charge total. |
| tax_breakdown | extended | `tax_exemption_reason` | text | Printed reason/code for exemption in this tax category; never inferred from zero tax. |
| tax_breakdown | extended | `tax_rate` | decimal | Printed percentage expressed in percentage points (6% -> "6"). |
| tax_breakdown | extended | `taxable_amount` | decimal | Printed tax base for this category, distinct from document subtotal and tax amount. |
| tax_breakdown | extended | `tax_amount` | decimal | Printed tax amount within this repeated tax category, distinct from document tax and line_tax. |
| charge_breakdown | extended | `charge_category` | text | Printed category/reason for this charge or allowance row; preserve the label and sign. |
| charge_breakdown | extended | `charge_amount` | decimal | Printed signed amount for this charge/allowance row; category distinguishes an allowance from a charge. |
| additional_attributes | conditional | `attribute_name` | text | Original printed label of an additional attribute without an existing canonical key. |
| additional_attributes | conditional | `attribute_value` | text | Additional printed attribute text; do not use to bypass known typed keys. |

## Production aliases

| Production vocabulary | Evaluation targets | Status | Constraint |
|---|---|---|---|
| supplier / supplier_name / vendor_name / seller | {"invoice": "supplier_name", "bill": "provider_name", "receipt": "merchant_name"} | context_required | Use confirmed kind and printed party role. |
| issuer / merchant | {"bill": "provider_name", "receipt": "merchant_name"} | context_required | Preserve issuer versus merchant role. |
| invoice / invoice_number | {"invoice": "invoice_number"} | supported | Never use original_invoice_number for the current invoice number. |
| date / invoice_date / bill_date / transaction_date | {"invoice": "invoice_date", "bill": "bill_date", "receipt": "transaction_date"} | context_required | No filename date inference. |
| due / due_date | {"invoice": "due_date", "bill": "due_date"} | supported | ISO date after unambiguous normalization. |
| customer / buyer / buyer_name | {"invoice": "customer_name", "bill": "customer_name", "receipt": "customer_name"} | context_required | Printed buyer only; no AP/AR inference. |
| reference / reference_number | {"invoice": "invoice_reference", "bill": "bill_reference", "receipt": "receipt_reference"} | context_required | Generic reference only. A stored short key loses primary-number versus reference role; require original printed label/key. |
| bill_reference / receipt_number | {"bill": "bill_reference", "receipt": "receipt_number"} | lossy_context | Receipt number retains its role. Bill mapper also maps printed bill number to bill_reference: use bill_number only when original evidence confirms that label. |
| type | {"invoice": "invoice_type"} | context_required | Printed invoice type is not classification, business category or approval state. |
| tax / tax_amount | {"invoice": "tax", "bill": "tax", "receipt": "tax"} | supported | Document tax amount, never a percentage. |
| total / total_amount | {"invoice": "total", "bill": "total", "receipt": "total"} | supported | Never amount_due; historical mixed values require original raw key/evidence. |
| amount_due | {"invoice": "amount_due", "bill": "amount_due"} | supported | Live accounting_contract final mapping is amount_due. No fallback into total or current_charges. |
| allowances / document_allowances | {"invoice": "allowance_total"} | supported | Document allowance sum only. |
| charges / document_charges | {"invoice": "charge_total", "bill": "current_charges"} | lossy_context | Bill mapper uses document_charges for current charges; raw label required. |
| prepaid / prepaid_amount | {"invoice": "prepaid_amount"} | supported | Not paid_amount. |
| rounding / rounding_amount | {"invoice": "rounding", "bill": "rounding", "receipt": "rounding"} | supported | Printed rounding adjustment. |
| total_excluding_tax | {"invoice": "tax_exclusive_total"} | supported | Preserve scope. |
| total_including_tax | {"invoice": "tax_inclusive_total"} | supported | Preserve scope. |
| account_number | {"bill": "customer_account_number"} | context_required | Not an invoice payment bank account. |
| amount_paid | {"invoice": "paid_amount", "bill": "payment_amount", "receipt": "amount_paid"} | context_required | Boundary accepts key; current parsers have uneven coverage. |
| tendered_amount / change_given | {"receipt": ["tender", "change"]} | adapter_required | Two distinct source keys, respectively. |
| line_items.amount / line_total | {"invoice": "line_total", "bill": "line_total", "receipt": "line_total"} | supported | Use each row; never aggregate as document total. |
| currency / subtotal / discount / billing_period / transaction_time / payment_method / payment_terms / payment_reference / purchase_order_number / refund_indicator | {} | same_key_if_applicable | Allowed only in dictionary scope; boundary acceptance is not parser coverage. |
| received_date / expense_category / outstanding_amount / goods_receipt_reference | {} | adapter_required | Do not silently map to delivery date, amount_due or delivery_reference; semantics need approval. |
| credit_note_number / debit_note_number / original_invoice_number / related_invoice_number | {"invoice": ["credit_note_number", "debit_note_number", "original_invoice_number"]} | lossy_context | Boundary collapses note reference to reference and related invoice to invoice. Retain raw identity; related invoice needs role confirmation. |
