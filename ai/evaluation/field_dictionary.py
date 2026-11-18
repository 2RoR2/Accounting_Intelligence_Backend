"""Authoritative evaluation vocabulary. Never imported by production extraction."""

VERSION = '1.0.0'
FIELDS = {}


def _add(types, scope, tier, value_type, keys):
    for key in keys.split():
        entry = FIELDS.setdefault(key, {'value_type': value_type, 'uses': [],
                                       'definition': key.replace('_', ' ').capitalize()})
        assert entry['value_type'] == value_type
        entry['uses'].append({'document_types': types.split(), 'scope': scope, 'tier': tier})


_add('invoice', 'document', 'core', 'text', '''invoice_number invoice_type invoice_reference
supplier_name supplier_address customer_name customer_address''')
_add('invoice', 'document', 'core', 'date', 'invoice_date due_date')
_add('invoice bill receipt', 'document', 'core', 'currency', 'currency')
_add('invoice bill receipt', 'document', 'core', 'decimal', 'subtotal tax total')
_add('invoice', 'document', 'core', 'decimal', 'discount')
_add('invoice', 'document', 'extended', 'text', '''supplier_registration_id supplier_tax_id
customer_registration_id customer_tax_id purchase_order_number contract_reference
delivery_reference payment_terms payment_instructions payment_reference payment_method
payment_bank_name payment_account_number billing_address shipping_address''')
_add('invoice', 'document', 'extended', 'decimal', '''allowance_total charge_total
tax_exclusive_total tax_inclusive_total prepaid_amount paid_amount amount_due rounding''')
_add('invoice', 'document', 'extended', 'date', '''service_start_date service_end_date
delivery_date payment_date''')
_add('invoice', 'document', 'conditional', 'text', '''myinvois_uuid myinvois_long_id
myinvois_validation_reference myinvois_qr_payload supplier_tin customer_tin
supplier_sst_id customer_sst_id supplier_msic_code supplier_business_activity
credit_note_number debit_note_number original_invoice_number original_invoice_uuid
note_reason industry_reference''')
_add('invoice', 'document', 'conditional', 'text', 'myinvois_validation_datetime')

_add('bill', 'document', 'core', 'text', '''bill_number bill_reference provider_name
provider_address customer_name customer_address''')
_add('bill', 'document', 'core', 'date', 'bill_date due_date')
_add('bill', 'document', 'core', 'decimal', 'amount_due')
_add('bill', 'document', 'extended', 'text', '''customer_account_number service_address
billing_period meter_number consumption_unit tariff_name payment_method payment_reference''')
_add('bill', 'document', 'extended', 'date', '''billing_period_start billing_period_end
previous_reading_date current_reading_date payment_date''')
_add('bill', 'document', 'extended', 'decimal', '''previous_reading current_reading
consumption tariff_rate previous_balance arrears current_charges adjustment_total
credit_total late_fee payment_amount service_charge tips discount rounding''')
_add('bill', 'document', 'conditional', 'text', '''jompay_biller_code jompay_reference_1
jompay_reference_2 biller_reference table_number order_number''')

_add('receipt', 'document', 'core', 'text', 'merchant_name merchant_address receipt_number receipt_reference')
_add('receipt', 'document', 'core', 'date', 'transaction_date')
_add('receipt', 'document', 'core', 'time', 'transaction_time')
_add('receipt', 'document', 'extended', 'text', '''branch_name branch_id cashier_name
cashier_id terminal_id register_id payment_method payment_reference original_receipt_reference
refund_reference void_reference refund_reason customer_name customer_id loyalty_reference''')
_add('receipt', 'document', 'extended', 'decimal', '''discount service_charge rounding
total_sales amount_paid tender change tips refund_amount''')
_add('receipt', 'document', 'extended', 'boolean', 'refund_indicator void_indicator')
_add('receipt', 'document', 'extended', 'date', 'refund_date void_date')

_add('invoice bill receipt', 'line', 'core', 'text', 'description')
_add('invoice bill receipt', 'line', 'core', 'decimal', 'quantity unit_price line_total')
_add('invoice bill receipt', 'line', 'extended', 'text', 'item_code unit tax_category')
_add('invoice bill receipt', 'line', 'extended', 'decimal', '''line_discount line_discount_rate
line_tax line_tax_rate line_allowance line_charge''')
_add('invoice bill receipt', 'line', 'conditional', 'date', 'service_start_date service_end_date delivery_date')
_add('invoice', 'line', 'conditional', 'text', 'myinvois_item_classification')

_add('invoice bill receipt', 'tax_breakdown', 'extended', 'text', 'tax_category tax_exemption_reason')
_add('invoice bill receipt', 'tax_breakdown', 'extended', 'decimal', 'tax_rate taxable_amount tax_amount')
_add('invoice bill receipt', 'charge_breakdown', 'extended', 'text', 'description charge_category unit')
_add('invoice bill receipt', 'charge_breakdown', 'extended', 'decimal', 'quantity unit_price charge_amount')
_add('bill', 'meter_readings', 'conditional', 'text', 'meter_number consumption_unit tariff_name')
_add('bill', 'meter_readings', 'conditional', 'decimal',
     'previous_reading current_reading consumption tariff_rate')
_add('bill', 'meter_readings', 'conditional', 'date', 'previous_reading_date current_reading_date')
_add('invoice bill receipt', 'additional_attributes', 'conditional', 'text', 'attribute_name attribute_value')

DEFINITIONS = {
    'invoice_number': 'Printed identifier of this invoice; not an original invoice linked by a note.',
    'invoice_type': 'Printed invoice or credit/debit-note type wording; not AP/AR or review status.',
    'invoice_reference': 'Separately printed invoice reference other than its primary invoice number.',
    'supplier_address': 'Printed address of the invoice seller, preserving all supplied address lines.',
    'customer_address': 'Printed customer/buyer address; not automatically billing or service address.',
    'invoice_date': 'Issue date printed for this invoice; not due, service, payment or upload date.',
    'due_date': 'Explicit printed payment due date; do not calculate it from payment terms.',
    'currency': 'Unambiguously printed currency normalized to a three-letter code; otherwise null.',
    'tax': 'Printed document-level tax amount, distinct from each category or item tax and tax rates.',
    'discount': 'Printed document-level discount amount, not a rate or a sum inferred from item discounts.',
    'supplier_registration_id': 'Printed seller business-registration identifier, distinct from tax identifiers.',
    'supplier_tax_id': 'Printed seller tax identifier with no more specific supported tax scheme; do not duplicate TIN or SST labels.',
    'customer_registration_id': 'Printed customer business-registration identifier, distinct from tax identifiers.',
    'customer_tax_id': 'Printed customer tax identifier with no more specific supported tax scheme; do not duplicate TIN or SST labels.',
    'purchase_order_number': 'Printed purchase-order identifier; not a contract, invoice or delivery reference.',
    'contract_reference': 'Printed identifier of an associated contract.',
    'delivery_reference': 'Printed delivery-note/shipment reference; do not infer a goods-receipt record.',
    'payment_terms': 'Printed terms of payment as text; do not convert them to a due date without that date being printed.',
    'payment_instructions': 'Printed payment instruction text, distinct from the separately labelled account and method fields.',
    'payment_reference': 'Printed reference for a payment/transaction; not evidence of settlement by itself.',
    'payment_method': 'Printed payment method text; do not infer paid/approved status.',
    'payment_bank_name': 'Printed bank named in invoice payment instructions, not a customer or utility account provider.',
    'payment_account_number': 'Printed bank account in invoice payment instructions; preserve leading zeros.',
    'billing_address': 'Explicitly labelled billing address; do not copy a generic customer address into this field.',
    'shipping_address': 'Explicitly labelled ship-to address, distinct from billing and customer addresses.',
    'allowance_total': 'Explicit printed document allowance total; not an inferred discount sum.',
    'charge_total': 'Explicit printed invoice document charge total, excluding unrelated bill balances.',
    'rounding': 'Printed signed monetary rounding adjustment; no rounding is introduced by annotation.',
    'service_start_date': 'Printed start date of the service interval at document or item scope.',
    'service_end_date': 'Printed end date of the service interval at document or item scope.',
    'delivery_date': 'Printed actual delivery date at document or item scope; not a received/upload timestamp.',
    'payment_date': 'Explicit printed date of a payment; not inferred from issue date or payment method.',
    'myinvois_uuid': 'Printed UUID assigned to this e-invoice; not the supplier invoice number or original invoice UUID.',
    'myinvois_long_id': 'Printed MyInvois long identifier, kept distinct from the invoice UUID.',
    'myinvois_validation_reference': 'Separately labelled validation reference; do not duplicate a UUID or long ID here.',
    'myinvois_qr_payload': 'Exact payload independently decoded from a printed QR code; evidence must identify that code.',
    'supplier_tin': 'Explicitly identified seller TIN; do not also count it as a generic supplier_tax_id.',
    'customer_tin': 'Explicitly identified buyer TIN; do not also count it as a generic customer_tax_id.',
    'supplier_sst_id': 'Explicitly identified seller SST registration identifier, distinct from TIN and company registration.',
    'customer_sst_id': 'Explicitly identified buyer SST registration identifier, distinct from TIN and company registration.',
    'supplier_msic_code': 'Printed seller business-classification code, kept as text including leading zeros.',
    'supplier_business_activity': 'Printed seller business-activity description, not a model-inferred industry.',
    'credit_note_number': 'Printed identifier of this credit note; invoice_number is reserved for invoice documents.',
    'debit_note_number': 'Printed identifier of this debit note; invoice_number is reserved for invoice documents.',
    'original_invoice_number': 'Printed number of an earlier invoice referenced by this document; not this document number.',
    'original_invoice_uuid': 'Printed UUID of an earlier linked invoice, distinct from its invoice number.',
    'note_reason': 'Printed reason for a credit/debit note or adjustment, without inferred accounting treatment.',
    'industry_reference': 'Printed industry-specific reference with its original label retained in printed_text.',
    'bill_number': 'Printed primary bill identifier; do not duplicate a generic bill reference unless separately printed.',
    'bill_reference': 'Printed generic bill reference; a bill-number alias needs printed-role confirmation.',
    'provider_name': 'Printed issuer/provider of the bill; carries no AP/AR or settlement inference.',
    'provider_address': 'Printed address of the bill issuer/provider, not the customer service location.',
    'bill_date': 'Printed bill issue date; not due date or billing-period boundary.',
    'customer_account_number': 'Printed customer/service account with the bill provider; not a bank payment account.',
    'service_address': 'Explicit printed address where the billed service is supplied.',
    'billing_period': 'Complete printed billing-period text; start/end dates are separately normalized components when supported.',
    'meter_number': 'Printed meter identifier at document or repeated-meter scope; no value for documents without a meter.',
    'consumption_unit': 'Printed unit for measured usage, preserving the source notation.',
    'tariff_name': 'Printed tariff/plan name; not a numeric rate or an inferred service category.',
    'billing_period_start': 'Start date supported by an unambiguous printed billing interval; never derived from bill date.',
    'billing_period_end': 'End date supported by an unambiguous printed billing interval; never derived from due date.',
    'previous_reading_date': 'Printed date associated with the previous meter reading.',
    'current_reading_date': 'Printed date associated with the current meter reading.',
    'previous_reading': 'Printed previous cumulative meter reading; not previous-period consumption.',
    'current_reading': 'Printed current cumulative meter reading; not current charges or usage.',
    'consumption': 'Explicit printed usage quantity; do not derive it by subtracting meter readings.',
    'tariff_rate': 'Printed monetary rate per stated consumption unit; preserve that unit and rate tier in evidence.',
    'adjustment_total': 'Printed signed bill adjustment amount; do not substitute credits or calculate a net balance.',
    'credit_total': 'Printed bill credit amount with its source sign; never infer it from previous payments.',
    'late_fee': 'Printed late-payment fee amount; not an inferred overdue balance.',
    'service_charge': 'Printed document-level service charge; not a sum of all charge rows.',
    'tips': 'Explicit printed tip/gratuity amount; do not infer it from tender minus total.',
    'jompay_biller_code': 'Printed JomPAY biller code, preserved as text.',
    'jompay_reference_1': 'Printed JomPAY Ref-1, preserved as text including leading zeros.',
    'jompay_reference_2': 'Printed JomPAY Ref-2; not a copy of Ref-1 when unprinted.',
    'biller_reference': 'Separately printed biller/payment reference with its original label; not a duplicated bill number.',
    'table_number': 'Printed restaurant table identifier; not a utility account or order identifier.',
    'order_number': 'Printed restaurant/service order identifier; not automatically the bill number.',
    'merchant_name': 'Printed merchant issuing the receipt; carries no AP/AR inference.',
    'merchant_address': 'Printed merchant address on the receipt, distinct from customer information.',
    'receipt_number': 'Printed primary receipt identifier; not an original/refunded receipt reference.',
    'receipt_reference': 'Separately printed generic receipt reference, not a duplicate primary receipt number.',
    'transaction_date': 'Printed date of the receipt transaction, not refund, void, issue/upload or payment processing date.',
    'transaction_time': 'Printed local transaction time in HH:MM[:SS]; do not invent timezone or missing seconds.',
    'branch_name': 'Printed merchant branch/outlet name, distinct from merchant legal name.',
    'branch_id': 'Printed branch/outlet code; preserve leading zeros.',
    'cashier_name': 'Printed cashier/operator name; do not infer it from an operator code.',
    'cashier_id': 'Printed cashier/operator identifier, distinct from cashier name.',
    'terminal_id': 'Printed payment/POS terminal identifier, distinct from register identifier.',
    'register_id': 'Printed cash register identifier, distinct from terminal identifier.',
    'original_receipt_reference': 'Printed reference to an earlier receipt linked by a refund/void; not this receipt identifier.',
    'refund_reference': 'Printed identifier of this refund transaction, distinct from the original receipt reference.',
    'void_reference': 'Printed identifier of this void transaction, distinct from the original receipt reference.',
    'refund_reason': 'Printed explanation for a refund; no invented accounting categorization.',
    'customer_id': 'Printed customer identifier; not a loyalty reference unless explicitly identified as such.',
    'loyalty_reference': 'Legitimately available printed loyalty/member reference; do not infer customer identity.',
    'refund_amount': 'Printed monetary refund amount with original sign retained; not automatically negative total.',
    'refund_indicator': 'Boolean meaning of explicit printed refund wording; an unprinted indicator is not false.',
    'void_indicator': 'Boolean meaning of explicit printed void wording; an unprinted indicator is not false.',
    'refund_date': 'Explicit printed refund transaction date, distinct from original transaction date.',
    'void_date': 'Explicit printed void transaction date, distinct from original transaction date.',
    'description': 'Printed item or charge-row description in its own row/group, preserving multiline text.',
    'quantity': 'Explicit printed row quantity as an exact decimal string; not inferred by dividing totals.',
    'unit_price': 'Explicit printed price per stated row unit; not derived by dividing line amount by quantity.',
    'item_code': 'Printed item/SKU code, preserved as text including leading zeros.',
    'unit': 'Printed row unit of measure; not an inferred product category or currency.',
    'tax_category': 'Printed tax-category code/name for this item or tax breakdown; not inferred from the rate.',
    'line_discount': 'Printed item discount amount, distinct from document discount or item discount rate.',
    'line_tax': 'Printed tax amount for this item, distinct from document/category tax.',
    'line_allowance': 'Printed allowance amount for this item; not inferred from unit price and quantity.',
    'line_charge': 'Printed additional charge for this item, distinct from document charge total.',
    'myinvois_item_classification': 'Printed classification code for this invoice item; not a predicted MSIC/business code.',
    'tax_exemption_reason': 'Printed reason/code for exemption in this tax category; never inferred from zero tax.',
    'taxable_amount': 'Printed tax base for this category, distinct from document subtotal and tax amount.',
    'tax_amount': 'Printed tax amount within this repeated tax category, distinct from document tax and line_tax.',
    'charge_category': 'Printed category/reason for this charge or allowance row; preserve the label and sign.',
    'charge_amount': 'Printed signed amount for this charge/allowance row; category distinguishes an allowance from a charge.',
    'attribute_name': 'Original printed label of an additional attribute without an existing canonical key.',
    'total': 'Printed document grand total; never substitute amount due, sales, tender or payment.',
    'amount_due': 'Printed balance requested for payment; distinct from total and previous balance.',
    'total_sales': 'Printed sales aggregate; distinct from receipt total and amount paid.',
    'amount_paid': 'Printed receipt payment amount; not inferred from payment method or total.',
    'paid_amount': 'Printed invoice paid amount; not an accounting settlement status.',
    'payment_amount': 'Printed bill payment amount; not current charges or amount due.',
    'prepaid_amount': 'Printed advance/prepayment amount, distinct from paid amount.',
    'tender': 'Printed amount tendered; may exceed total or amount paid.',
    'change': 'Printed change returned.',
    'current_charges': 'Printed charges for the current billing period only.',
    'previous_balance': 'Printed balance brought forward before current-period charges.',
    'arrears': 'Printed overdue amount; not assumed equal to previous balance.',
    'subtotal': 'Printed subtotal in its labelled scope; do not derive tax-exclusive total.',
    'tax_exclusive_total': 'Explicit total excluding tax; do not alias a generic subtotal.',
    'tax_inclusive_total': 'Explicit total including tax; keep distinct from grand total.',
    'supplier_name': 'Invoice seller/issuer name; carries no AP/AR direction.',
    'customer_name': 'Printed buyer/customer name; carries no AP/AR direction.',
    'tax_rate': 'Printed percentage expressed in percentage points (6% -> "6").',
    'line_tax_rate': 'Printed item tax percentage in percentage points.',
    'line_discount_rate': 'Printed item discount percentage in percentage points.',
    'line_total': 'Printed row amount; preserve whether tax-inclusive in annotation notes.',
    'myinvois_validation_datetime': 'Printed timestamp preserved as text, including timezone if printed.',
    'attribute_value': 'Additional printed attribute text; do not use to bypass known typed keys.',
}
if set(DEFINITIONS) != set(FIELDS):
    raise ValueError('Every canonical field requires an explicit, evaluable definition')
for _key, _definition in DEFINITIONS.items():
    FIELDS[_key]['definition'] = _definition

# Documentation only: no runtime adapter is installed at this checkpoint.
# Per-kind targets prevent supplier/date/reference aliases losing their roles.
ALIASES = [
    {'production': source, 'evaluation': targets, 'status': status, 'note': note}
    for source, targets, status, note in [
        ('supplier / supplier_name / vendor_name / seller', {'invoice': 'supplier_name', 'bill': 'provider_name', 'receipt': 'merchant_name'}, 'context_required', 'Use confirmed kind and printed party role.'),
        ('issuer / merchant', {'bill': 'provider_name', 'receipt': 'merchant_name'}, 'context_required', 'Preserve issuer versus merchant role.'),
        ('invoice / invoice_number', {'invoice': 'invoice_number'}, 'supported', 'Never use original_invoice_number for the current invoice number.'),
        ('date / invoice_date / bill_date / transaction_date', {'invoice': 'invoice_date', 'bill': 'bill_date', 'receipt': 'transaction_date'}, 'context_required', 'No filename date inference.'),
        ('due / due_date', {'invoice': 'due_date', 'bill': 'due_date'}, 'supported', 'ISO date after unambiguous normalization.'),
        ('customer / buyer / buyer_name', {'invoice': 'customer_name', 'bill': 'customer_name', 'receipt': 'customer_name'}, 'context_required', 'Printed buyer only; no AP/AR inference.'),
        ('reference / reference_number', {'invoice': 'invoice_reference', 'bill': 'bill_reference', 'receipt': 'receipt_reference'}, 'context_required', 'Generic reference only. A stored short key loses primary-number versus reference role; require original printed label/key.'),
        ('bill_reference / receipt_number', {'bill': 'bill_reference', 'receipt': 'receipt_number'}, 'lossy_context', 'Receipt number retains its role. Bill mapper also maps printed bill number to bill_reference: use bill_number only when original evidence confirms that label.'),
        ('type', {'invoice': 'invoice_type'}, 'context_required', 'Printed invoice type is not classification, business category or approval state.'),
        ('tax / tax_amount', dict.fromkeys(('invoice', 'bill', 'receipt'), 'tax'), 'supported', 'Document tax amount, never a percentage.'),
        ('total / total_amount', dict.fromkeys(('invoice', 'bill', 'receipt'), 'total'), 'supported', 'Never amount_due; historical mixed values require original raw key/evidence.'),
        ('amount_due', {'invoice': 'amount_due', 'bill': 'amount_due'}, 'supported', 'Live accounting_contract final mapping is amount_due. No fallback into total or current_charges.'),
        ('allowances / document_allowances', {'invoice': 'allowance_total'}, 'supported', 'Document allowance sum only.'),
        ('charges / document_charges', {'invoice': 'charge_total', 'bill': 'current_charges'}, 'lossy_context', 'Bill mapper uses document_charges for current charges; raw label required.'),
        ('prepaid / prepaid_amount', {'invoice': 'prepaid_amount'}, 'supported', 'Not paid_amount.'),
        ('rounding / rounding_amount', dict.fromkeys(('invoice', 'bill', 'receipt'), 'rounding'), 'supported', 'Printed rounding adjustment.'),
        ('total_excluding_tax', {'invoice': 'tax_exclusive_total'}, 'supported', 'Preserve scope.'),
        ('total_including_tax', {'invoice': 'tax_inclusive_total'}, 'supported', 'Preserve scope.'),
        ('account_number', {'bill': 'customer_account_number'}, 'context_required', 'Not an invoice payment bank account.'),
        ('amount_paid', {'invoice': 'paid_amount', 'bill': 'payment_amount', 'receipt': 'amount_paid'}, 'context_required', 'Boundary accepts key; current parsers have uneven coverage.'),
        ('tendered_amount / change_given', {'receipt': ['tender', 'change']}, 'adapter_required', 'Two distinct source keys, respectively.'),
        ('line_items.amount / line_total', {'invoice': 'line_total', 'bill': 'line_total', 'receipt': 'line_total'}, 'supported', 'Use each row; never aggregate as document total.'),
        ('currency / subtotal / discount / billing_period / transaction_time / payment_method / payment_terms / payment_reference / purchase_order_number / refund_indicator', {}, 'same_key_if_applicable', 'Allowed only in dictionary scope; boundary acceptance is not parser coverage.'),
        ('received_date / expense_category / outstanding_amount / goods_receipt_reference', {}, 'adapter_required', 'Do not silently map to delivery date, amount_due or delivery_reference; semantics need approval.'),
        ('credit_note_number / debit_note_number / original_invoice_number / related_invoice_number', {'invoice': ['credit_note_number', 'debit_note_number', 'original_invoice_number']}, 'lossy_context', 'Boundary collapses note reference to reference and related invoice to invoice. Retain raw identity; related invoice needs role confirmation.'),
    ]
]


def allowed_keys(kind, scope):
    return {key for key, field in FIELDS.items()
            if any(kind in use['document_types'] and use['scope'] == scope for use in field['uses'])}
