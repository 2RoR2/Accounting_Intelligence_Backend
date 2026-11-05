"""Module 3 policy for D1, D3 and D5, tested with a fake database routed by SQL text. Touches no real database."""
from decimal import Decimal
import json
import unittest

from validation.documents.document_validation import (
    NOT_APPLICABLE_MARK, coverage_complete, evaluate_document, read_status, stored_status,
)
from validation.documents.financial_documents import bill_amount_due, derived_subtotal, tax_status
from validation.documents.references import read_or_unavailable, transaction_references


def field(key, value, state=None, candidates=()):
    return {'field_key': key, 'field_label': key, 'extraction_state': state or ('resolved' if value is not None else 'missing'),
            'resolution': 'parser_preserved', 'current_value': value, 'normalized_value': value, 'raw_value': value,
            'corrected_by': None, 'corrected_at': None, 'evidence': [], 'candidates': list(candidates)}


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDb:
    """Answers each query by a word in its SQL. A value of Exception means that table cannot be read."""

    def __init__(self, **tables):
        self.tables = {'validation_rules': [], 'vendors': [], 'customers': [], 'references': [], 'history': [], 'purchase_orders': [],
                       'purchase_order_lines': [], 'audit_logs': [], **tables}
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        for word, table in (('FROM validation_rules', 'validation_rules'), ('FROM vendors', 'vendors'), ('FROM customers', 'customers'),
                            ('FROM audit_logs', 'audit_logs'), ('FROM purchase_order_lines', 'purchase_order_lines'),
                            ('FROM purchase_orders', 'purchase_orders'), ('FROM standardised_records', 'history'),
                            ('FROM documents d JOIN raw_extractions', 'references')):
            if word in sql:
                rows = self.tables[table]
                if rows is Exception:
                    raise RuntimeError(table + ' unavailable')
                return Rows(rows)
        raise AssertionError('Unexpected query: ' + sql[:60])


DOCUMENT = {'id': 'doc-1', 'tenant_id': 'tenant-1', 'kind': 'invoice', 'category': 'accounts_payable', 'content_revision': 3}
ITEMS = [{'description': 'Item', 'quantity': '1', 'unit_price': '100.00', 'amount': '100.00', 'field_states': {}}]


def invoice_fields(**overrides):
    values = {'supplier': 'Acme Sdn Bhd', 'invoice': 'INV-1', 'date': '2026-01-01', 'currency': 'MYR', 'subtotal': '100.00',
              'tax': '6.00', 'total': '106.00', **overrides}
    return [field(key, value) for key, value in values.items()]


def run(fields=None, db=None, document=None, items=None, **raw):
    document = document or DOCUMENT
    return evaluate_document(document, {'raw_json': {'mode': 'module2_public_result',
                             'document_type': {'value': document['kind'], 'status': 'classified'}, **raw}},
                             fields if fields is not None else invoice_fields(), ITEMS if items is None else items, db)


def by_rule(result, rule):
    return [c for c in result['checks'] if c.get('rule') == rule]


def codes(result):
    return {e['code'] for e in result['errors']}


def vendor(name='Acme Sdn Bhd'):
    return {'id': 'v1', 'vendor_name': name}


def history(amounts, currency='MYR'):
    return [{'transaction_id': f't{i}', 'amount': Decimal(a), 'currency': currency, 'expense_category': None, 'counterparty': 'ACME SDN. BHD.'}
            for i, a in enumerate(amounts)]


class DuplicateRuleTests(unittest.TestCase):
    def test_an_empty_history_that_was_read_is_a_pass(self):
        check = by_rule(run(db=FakeDb(vendors=[vendor()])), 'EX-007')[0]
        self.assertEqual(check['checkStatus'], 'CHECKED')
        self.assertIn('no other invoices', check['message'])

    def test_unavailable_history_is_not_checked_and_blocks(self):
        result = run(db=FakeDb(vendors=Exception, references=Exception))
        check = by_rule(result, 'EX-007')[0]
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))
        self.assertFalse(result['coverage_complete'])

    def test_no_database_is_never_a_pass(self):
        self.assertEqual(by_rule(run(), 'EX-007')[0]['checkStatus'], 'NOT_CHECKED')

    def test_missing_identity_is_not_checked(self):
        check = by_rule(run(invoice_fields(invoice=None), db=FakeDb(vendors=[vendor()])), 'EX-007')[0]
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))
        self.assertIn('supplier and an invoice number', check['message'])

    def test_tenant_policy_can_turn_it_off(self):
        db = FakeDb(vendors=[vendor()], validation_rules=[{'check_duplicate': False, 'require_po_for_ap': False}])
        self.assertEqual(by_rule(run(db=db), 'EX-007')[0]['checkStatus'], NOT_APPLICABLE_MARK)

    def test_bills_are_outside_the_policy_today(self):
        bill = {**DOCUMENT, 'kind': 'bill'}
        fields = [field('supplier', 'Acme'), field('reference', 'B-1'), field('date', '2026-01-01'), field('currency', 'MYR'),
                  field('total', '10.00'), field('amount_due', '10.00')]
        self.assertEqual(by_rule(run(fields, db=FakeDb(), document=bill, items=[]), 'EX-007')[0]['checkStatus'], NOT_APPLICABLE_MARK)

    def test_every_query_is_scoped_to_the_tenant(self):
        db = FakeDb(vendors=[vendor()])
        run(db=db)
        for sql, params in db.queries:
            self.assertIn('tenant-1', params, sql[:60])


class CounterpartyRuleTests(unittest.TestCase):
    def test_exact_supplier_passes_without_approving_it(self):
        check = by_rule(run(db=FakeDb(vendors=[vendor()])), 'EX-008')[0]
        self.assertEqual(check['checkStatus'], 'CHECKED')
        self.assertIn('does not approve', check['message'])

    def test_a_fuzzy_supplier_needs_review(self):
        result = run(db=FakeDb(vendors=[vendor('Acme Sdn Bhd X')]))
        self.assertIn('EX-008', codes(result))
        self.assertTrue(next(e for e in result['errors'] if e['code'] == 'EX-008')['vendor_candidate']['vendor_name'])

    def test_no_supplier_list_is_not_checked_and_not_a_pass(self):
        check = by_rule(run(db=FakeDb()), 'EX-008')[0]
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', False))
        self.assertIn('no supplier list', check['message'])

    def test_a_list_that_cannot_be_read_blocks(self):
        check = by_rule(run(db=FakeDb(vendors=Exception)), 'EX-008')[0]
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))

    def test_receivable_matches_customers_not_suppliers(self):
        receivable = {**DOCUMENT, 'category': 'accounts_receivable'}
        db = FakeDb(vendors=[vendor('Someone Else')], customers=[{'id': 'c1', 'vendor_name': 'Beta Trading'}])
        result = run(invoice_fields(customer='Beta Trading'), db=db, document=receivable)
        self.assertEqual(by_rule(result, 'EX-008')[0]['checkStatus'], 'CHECKED')
        self.assertIn('customer list', by_rule(result, 'EX-008')[0]['message'])

    def test_receivable_with_an_unknown_customer_is_a_customer_finding(self):
        receivable = {**DOCUMENT, 'category': 'accounts_receivable'}
        result = run(invoice_fields(customer='Gamma Ltd'), db=FakeDb(customers=[{'id': 'c1', 'vendor_name': 'Beta Trading'}]), document=receivable)
        self.assertEqual(next(e for e in result['errors'] if e['code'] == 'EX-008')['field'], 'customer')

    def test_receivable_with_no_customer_list_is_not_checked(self):
        receivable = {**DOCUMENT, 'category': 'accounts_receivable'}
        check = by_rule(run(invoice_fields(customer='Beta'), db=FakeDb(), document=receivable), 'EX-008')[0]
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', False))

    def test_bill_with_a_fuzzy_supplier_needs_review(self):
        bill = {**DOCUMENT, 'kind': 'bill'}
        fields = [field('supplier', 'Acme'), field('reference', 'B-1'), field('date', '2026-01-01'), field('currency', 'MYR'),
                  field('total', '10.00'), field('amount_due', '10.00')]
        self.assertIn('EX-008', codes(run(fields, db=FakeDb(vendors=[vendor('Acme Trading')]), document=bill, items=[])))


class PurchaseOrderRuleTests(unittest.TestCase):
    def policy(self, required):
        return [{'check_duplicate': True, 'require_po_for_ap': required}]

    def test_optional_with_no_reference_is_not_applicable(self):
        self.assertEqual(by_rule(run(db=FakeDb(vendors=[vendor()], validation_rules=self.policy(False))), 'EX-009')[0]['checkStatus'], NOT_APPLICABLE_MARK)

    def test_required_with_no_reference_blocks(self):
        result = run(db=FakeDb(vendors=[vendor()], validation_rules=self.policy(True)))
        check = by_rule(result, 'EX-009')[0]
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))
        self.assertFalse(result['coverage_complete'])

    def test_requirement_covers_payables_only(self):
        receivable = {**DOCUMENT, 'category': 'accounts_receivable'}
        check = by_rule(run(invoice_fields(customer='B'), db=FakeDb(validation_rules=self.policy(True)), document=receivable), 'EX-009')[0]
        self.assertEqual(check['checkStatus'], NOT_APPLICABLE_MARK)

    def test_reference_with_no_such_order_fails(self):
        self.assertIn('EX-009', codes(run(invoice_fields(purchase_order_number='PO-1'), db=FakeDb(vendors=[vendor()]))))

    def test_reference_with_unreadable_order_data_is_not_checked_with_an_action(self):
        result = run(invoice_fields(purchase_order_number='PO-1'), db=FakeDb(vendors=[vendor()], purchase_orders=Exception))
        check = by_rule(result, 'EX-009')[0]
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))
        self.assertIn('revalidate', check['message'])
        self.assertNotIn('EX-009', codes(result))

    def test_matching_order_passes(self):
        order = {'id': 'p1', 'po_number': 'PO-1', 'currency': 'MYR', 'total_amount': Decimal('106.00'), 'vendor_name': 'Acme Sdn Bhd'}
        lines = [{'description': 'Item', 'quantity': Decimal('1'), 'unit_price': Decimal('100.00'), 'amount': Decimal('100.00')}]
        result = run(invoice_fields(purchase_order_number='PO-1'), db=FakeDb(vendors=[vendor()], purchase_orders=[order], purchase_order_lines=lines))
        self.assertEqual(by_rule(result, 'EX-009')[0]['checkStatus'], 'CHECKED')


class AnomalyRuleTests(unittest.TestCase):
    NORMAL = ['100.00', '110.00', '90.00', '105.00', '95.00']

    def large(self):
        return invoice_fields(subtotal='100000.00', tax='6000.00', total='106000.00')

    def large_items(self):
        return [{'description': 'Item', 'quantity': '1', 'unit_price': '100000.00', 'amount': '100000.00', 'field_states': {}}]

    def test_too_little_history_is_not_checked_not_pass(self):
        check = by_rule(run(db=FakeDb(vendors=[vendor()], history=history(self.NORMAL[:4]))), 'EX-010')[0]
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', False))

    def test_unreadable_history_is_not_checked(self):
        check = by_rule(run(db=FakeDb(vendors=[vendor()], history=Exception)), 'EX-010')[0]
        self.assertEqual(check['checkStatus'], 'NOT_CHECKED')
        self.assertIn('could not be read', check['message'])

    def test_other_currencies_do_not_count_as_history(self):
        check = by_rule(run(db=FakeDb(vendors=[vendor()], history=history(self.NORMAL, 'USD'))), 'EX-010')[0]
        self.assertEqual(check['checkStatus'], 'NOT_CHECKED')

    def test_a_normal_amount_passes_with_its_numbers_kept(self):
        check = by_rule(run(db=FakeDb(vendors=[vendor()], history=history(self.NORMAL))), 'EX-010')[0]
        self.assertEqual(check['checkStatus'], 'CHECKED')
        self.assertEqual(check['actual_value'], '106.00')
        self.assertTrue(check['expected_value'])

    def test_an_outlier_fails_with_evidence(self):
        result = run(self.large(), db=FakeDb(vendors=[vendor()], history=history(self.NORMAL)), items=self.large_items())
        error = next(e for e in result['errors'] if e['code'] == 'EX-010')
        self.assertEqual(error['actual_value'], '106000.00')
        self.assertIn('5 approved records', error['message'])

    def test_comparable_history_is_filtered_by_accounting_category(self):
        rows = [{'transaction_id': 'a', 'amount': Decimal('1'), 'currency': 'MYR', 'expense_category': 'rent', 'counterparty': 'Acme'},
                {'transaction_id': 'b', 'amount': Decimal('2'), 'currency': 'MYR', 'expense_category': None, 'counterparty': 'Acme'}]
        self.assertEqual([r['transaction_id'] for r in transaction_references(FakeDb(history=rows), DOCUMENT, 'rent', 'Acme')], ['a'])
        self.assertEqual([r['transaction_id'] for r in transaction_references(FakeDb(history=rows), DOCUMENT, None, 'Acme')], ['b'])

    def test_unrelated_suppliers_are_not_history(self):
        rows = [{'transaction_id': 'a', 'amount': Decimal('1'), 'currency': 'MYR', 'expense_category': None, 'counterparty': 'Other Co'}]
        self.assertEqual(transaction_references(FakeDb(history=rows), DOCUMENT, None, 'Acme'), [])
        check = by_rule(run(db=FakeDb(vendors=[vendor()], history=history(['100.00'] * 5)[:0] + rows * 5)), 'EX-010')[0]
        self.assertEqual(check['checkStatus'], 'NOT_CHECKED')

    def test_no_counterparty_name_means_no_comparable_history(self):
        self.assertEqual(transaction_references(FakeDb(history=history(['1.00'])), DOCUMENT), [])

    def test_the_verdict_no_longer_depends_on_upload_order_across_unrelated_suppliers(self):
        from validation.transactions.transaction_anomaly import detect_transaction_anomalies
        amounts = [('A', '732.34'), ('B', '819.06'), ('C', '124.79'), ('D', '5138.35'), ('E', '12973.88'), ('F', '6860.45'), ('G', '44745.59')]
        def flagged(order):
            seen, found = [], []
            for party, amount in order:
                rows = [dict(row) for row in seen if row['counterparty'] == party]
                result = detect_transaction_anomalies([{'transaction_id': party, 'amount': amount, 'currency': 'USD'}], reference_transactions=rows)[0]
                found.extend([party] if result['is_anomaly'] else [])
                seen.append({'transaction_id': party + str(len(seen)), 'amount': amount, 'currency': 'USD', 'counterparty': party})
            return found
        self.assertEqual(flagged(amounts), [])
        self.assertEqual(flagged(amounts[::-1]), [])

    def reviewed(self, amount='106000.00', revision=3, currency='MYR'):
        return [{'details': json.dumps({'rule': 'EX-010', 'note': 'Agreed annual contract', 'content_revision': revision,
                                        'amount': amount, 'currency': currency}), 'user_name': 'Rachel', 'created_at': 'now'}]

    def test_a_recorded_review_settles_the_outlier_and_keeps_its_numbers(self):
        db = FakeDb(vendors=[vendor()], history=history(self.NORMAL), audit_logs=self.reviewed())
        result = run(self.large(), db=db, items=self.large_items())
        self.assertNotIn('EX-010', codes(result))
        check = by_rule(result, 'EX-010')[0]
        self.assertEqual((check['checkStatus'], check['actual_value']), ('CHECKED', '106000.00'))
        self.assertIn('Accepted on review by Rachel', check['message'])

    def test_a_review_of_an_earlier_revision_does_not_carry_over(self):
        db = FakeDb(vendors=[vendor()], history=history(self.NORMAL), audit_logs=self.reviewed(revision=2))
        self.assertIn('EX-010', codes(run(self.large(), db=db, items=self.large_items())))

    def test_a_review_of_a_different_amount_does_not_carry_over(self):
        db = FakeDb(vendors=[vendor()], history=history(self.NORMAL), audit_logs=self.reviewed(amount='5.00'))
        self.assertIn('EX-010', codes(run(self.large(), db=db, items=self.large_items())))


class LineItemRuleTests(unittest.TestCase):
    def receipt(self, items, structure, **extra):
        doc = {**DOCUMENT, 'kind': 'receipt'}
        values = {'supplier': 'Shop', 'date': '2026-01-01', 'currency': 'MYR', 'total': '10.00', **extra}
        return run([field(k, v) for k, v in values.items()], document=doc, items=items, item_structure={'status': structure} if structure else None)

    def test_a_document_with_no_item_rows_may_have_no_lines(self):
        result = self.receipt([], 'not_itemized')
        self.assertEqual(result['errors'], [])
        self.assertEqual(by_rule(result, 'RECEIPT-LINE_DETAILS')[0]['checkStatus'], NOT_APPLICABLE_MARK)
        self.assertEqual(by_rule(result, 'RECEIPT-TOTAL')[0]['checkStatus'], NOT_APPLICABLE_MARK)
        self.assertTrue(result['coverage_complete'])

    def test_visible_rows_that_were_not_read_need_review(self):
        result = self.receipt([], 'unmapped_rows')
        check = by_rule(result, 'RECEIPT-LINE_DETAILS')[0]
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))
        self.assertFalse(result['coverage_complete'])

    def test_an_older_extraction_with_no_structure_needs_review(self):
        self.assertFalse(self.receipt([], None)['coverage_complete'])

    def test_quantity_and_price_are_not_needed_and_stay_empty(self):
        items = [{'description': 'Tea', 'quantity': None, 'unit_price': None, 'amount': '10.00', 'field_states': {}}]
        result = self.receipt(items, 'itemized')
        self.assertEqual(result['errors'], [])
        self.assertEqual(by_rule(result, 'RECEIPT-LINE_MATH')[0]['checkStatus'], NOT_APPLICABLE_MARK)
        self.assertIsNone(items[0]['quantity'])

    def test_printed_quantity_and_price_are_checked(self):
        items = [{'description': 'Tea', 'quantity': '2', 'unit_price': '9.00', 'amount': '10.00', 'field_states': {}}]
        self.assertIn('RECEIPT-LINE_MATH', codes(self.receipt(items, 'itemized')))

    def test_a_line_with_no_description_needs_review(self):
        items = [{'description': None, 'quantity': None, 'unit_price': None, 'amount': '10.00', 'field_states': {}}]
        self.assertFalse(self.receipt(items, 'itemized')['coverage_complete'])

    def test_standardising_allows_zero_lines_only_when_none_are_printed(self):
        from services.extraction.contracts.accounting_contract import approved_snapshot
        doc = {'kind': 'receipt', 'category': 'accounts_payable', 'content_revision': 1}
        def extraction(status):
            return {'raw_json': {'item_structure': {'status': status}}}
        fields = [field('total', '10.00')]
        self.assertEqual(approved_snapshot(doc, extraction('not_itemized'), fields, [])['lineItems'], [])
        with self.assertRaises(ValueError):
            approved_snapshot(doc, extraction('unmapped_rows'), fields, [])
        with self.assertRaises(ValueError):
            approved_snapshot({**doc, 'kind': 'invoice'}, extraction('not_itemized'), fields, [])


class AmountDueTests(unittest.TestCase):
    def test_due_equal_to_the_bill_total(self):
        self.assertEqual(bill_amount_due({'total': '80.00', 'amount_due': '80.00'})[0], 'CHECKED')

    def test_no_due_is_not_assumed_to_be_the_total(self):
        self.assertEqual(bill_amount_due({'total': '80.00'})[0], 'NOT_CHECKED')

    def test_partial_payment(self):
        state, message = bill_amount_due({'charges': '100.00', 'amount_due': '60.00', 'payment_amount': '40.00'})
        self.assertEqual(state, 'CHECKED')
        self.assertIn('100.00 current charges - 40.00 payment = 60.00', message)

    def test_a_payment_printed_negative_also_reduces_it(self):
        self.assertEqual(bill_amount_due({'charges': '100.00', 'amount_due': '60.00', 'payment_amount': '-40.00'})[0], 'CHECKED')

    def test_previous_balance_credit_adjustment_and_fee(self):
        values = {'charges': '100.00', 'previous_balance': '50.00', 'credit_total': '-20.00', 'adjustment_total': '-5.00', 'late_fee': '10.00', 'amount_due': '135.00'}
        self.assertEqual(bill_amount_due(values)[0], 'CHECKED')

    def test_a_negative_due_is_kept(self):
        state, _ = bill_amount_due({'charges': '10.00', 'previous_balance': '-30.00', 'amount_due': '-20.00'})
        self.assertEqual(state, 'CHECKED')

    def test_printed_components_that_disagree_fail(self):
        self.assertEqual(bill_amount_due({'charges': '100.00', 'amount_due': '70.00'})[0], 'FAILED')

    def test_a_difference_from_the_bill_total_alone_proves_nothing(self):
        self.assertEqual(bill_amount_due({'total': '100.00', 'amount_due': '70.00'})[0], 'NOT_CHECKED')

    def test_no_base_amount_is_not_checked(self):
        self.assertEqual(bill_amount_due({'amount_due': '70.00'})[0], 'NOT_CHECKED')

    def test_it_reaches_the_validator(self):
        bill = {**DOCUMENT, 'kind': 'bill'}
        values = {'supplier': 'Power Co', 'reference': 'B-1', 'date': '2026-01-01', 'currency': 'MYR', 'charges': '100.00', 'amount_due': '70.00'}
        result = run([field(k, v) for k, v in values.items()], document=bill, items=[], item_structure={'status': 'not_itemized'})
        self.assertIn('BILL-AMOUNT_DUE', codes(result))


class SubtotalTests(unittest.TestCase):
    INV00001_LINES = ['720531.00', '75375.00', '76786.00', '140845.00', '46768.00', '720208.00']

    def items(self, amounts):
        return [{'description': 'x', 'quantity': '1', 'unit_price': a, 'amount': a, 'field_states': {}} for a in amounts]

    def test_inv00001_six_lines_equal_its_tax_exclusive_total(self):
        values = {'total_excluding_tax': '1780513.00', 'tax': '178051.30', 'total': '1958564.30'}
        self.assertEqual(sum(Decimal(a) for a in self.INV00001_LINES), Decimal('1780513.00'))
        self.assertEqual(derived_subtotal(values, self.items(self.INV00001_LINES)), '1780513.00')

    def test_a_wrong_tax_exclusive_total_stops_the_derivation(self):
        values = {'total_excluding_tax': '1780512.00', 'tax': '178051.30', 'total': '1958563.30'}
        self.assertIsNone(derived_subtotal(values, self.items(self.INV00001_LINES)))

    def test_discount_charge_and_rounding_are_applied(self):
        values = {'tax': '6.00', 'discount': '10.00', 'charges': '5.00', 'rounding': '-0.01', 'total': '100.99'}
        self.assertEqual(derived_subtotal(values, self.items(['100.00'])), '100.00')

    def test_a_missing_line_amount_stops_it(self):
        items = self.items(['50.00']) + [{'description': 'x', 'quantity': None, 'unit_price': None, 'amount': None}]
        self.assertIsNone(derived_subtotal({'tax': '0.00', 'total': '50.00'}, items))

    def test_tax_inclusive_lines_do_not_derive(self):
        self.assertIsNone(derived_subtotal({'tax': '6.00', 'total': '106.00'}, self.items(['106.00'])))

    def test_a_derived_subtotal_is_never_stored(self):
        result = run(invoice_fields(subtotal=None), db=FakeDb(vendors=[vendor()]))
        self.assertTrue(by_rule(result, 'SUBTOTAL-DERIVED'))
        self.assertNotIn('EX-001', codes(result))


class TaxTreatmentTests(unittest.TestCase):
    def sale(self, tax=None, tax_candidates=(), state=None, **raw):
        fields = invoice_fields(subtotal='106.00', tax=None, total='106.00')
        fields = [f for f in fields if f['field_key'] != 'tax'] + [{**field('tax', tax, state), 'candidates': list(tax_candidates)}]
        items = [{'description': 'x', 'quantity': '1', 'unit_price': '106.00', 'amount': '106.00', 'field_states': {}}]
        return fields, items, raw

    def check(self, result):
        return by_rule(result, 'TAX-STATUS')[0]

    def test_printed_zero_is_a_checked_value(self):
        fields, items, _ = self.sale(tax='0.00')
        check = self.check(run(fields, db=FakeDb(vendors=[vendor()]), items=items))
        self.assertEqual(check['checkStatus'], 'CHECKED')
        self.assertIn('zero', check['message'])

    def test_not_printed_needs_a_person_and_stores_nothing(self):
        fields, items, _ = self.sale()
        result = run(fields, db=FakeDb(vendors=[vendor()]), items=items)
        check = self.check(result)
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))
        self.assertFalse(result['coverage_complete'])
        self.assertTrue(result['validation_passed'])
        self.assertIsNone(next(f for f in fields if f['field_key'] == 'tax')['current_value'])

    def test_a_stated_exemption_with_matching_amounts_is_supported(self):
        fields, items, _ = self.sale()
        check = self.check(run(fields, db=FakeDb(vendors=[vendor()]), items=items, tax_statement={'status': 'exempt', 'text': 'Non-taxable supply'}))
        self.assertEqual(check['checkStatus'], 'CHECKED')
        self.assertIn('Non-taxable supply', check['message'])

    def test_tax_inside_the_prices_needs_a_person(self):
        fields, items, _ = self.sale()
        check = self.check(run(fields, db=FakeDb(vendors=[vendor()]), items=items, tax_statement={'status': 'inclusive', 'text': 'Prices include SST'}))
        self.assertEqual((check['checkStatus'], check['required']), ('NOT_CHECKED', True))
        self.assertIn('cannot be separated', check['message'])

    def test_a_tax_candidate_means_unknown_and_tax_stays_required(self):
        fields, items, _ = self.sale(tax_candidates=[{'source': 'ai', 'value': '6.00'}])
        result = run(fields, db=FakeDb(vendors=[vendor()]), items=items)
        self.assertIn('EX-001', codes(result))
        self.assertEqual(tax_status(fields, {'tax': None}, {}), 'unknown')

    def test_amounts_that_do_not_add_up_keep_tax_required(self):
        fields = invoice_fields(subtotal='100.00', tax=None, total='106.00')
        self.assertIn('EX-001', codes(run(fields, db=FakeDb(vendors=[vendor()]))))

    def test_a_recorded_review_settles_it_for_this_revision_only(self):
        fields, items, _ = self.sale()
        def audit(revision):
            return [{'details': json.dumps({'rule': 'TAX-STATUS', 'note': 'Supplier is not SST registered', 'content_revision': revision}), 'user_name': 'Rachel', 'created_at': 'now'}]
        check = self.check(run(fields, db=FakeDb(vendors=[vendor()], audit_logs=audit(3)), items=items))
        self.assertEqual(check['checkStatus'], 'CHECKED')
        self.assertIn('Confirmed on review by Rachel', check['message'])
        self.assertEqual(self.check(run(fields, db=FakeDb(vendors=[vendor()], audit_logs=audit(2)), items=items))['checkStatus'], 'NOT_CHECKED')


class FourStateTests(unittest.TestCase):
    def test_not_applicable_is_stored_as_a_marked_non_blocking_warning_and_read_back(self):
        status, expected, blocks, review = stored_status({'checkStatus': 'NOT_APPLICABLE', 'result': 'NOT_APPLICABLE'})
        self.assertEqual((status, expected, blocks, review), ('warning', 'NOT_APPLICABLE', False, False))
        self.assertEqual(read_status({'status': status, 'expected_value': expected}), 'NOT_APPLICABLE')

    def test_the_other_states_are_unchanged(self):
        self.assertEqual(stored_status({'checkStatus': 'CHECKED', 'result': 'PASS'})[0], 'passed')
        self.assertEqual(stored_status({'checkStatus': 'NOT_CHECKED', 'required': True})[0::2], ('not_checked', True))
        self.assertEqual(stored_status({'checkStatus': 'NOT_CHECKED', 'required': False})[0::2], ('not_checked', False))
        self.assertEqual(read_status({'status': 'failed', 'expected_value': '5.00'}), 'FAILED')
        self.assertEqual(read_status({'status': 'warning', 'expected_value': None}), 'WARNING')

    def test_a_check_with_no_stated_requirement_counts_as_mandatory(self):
        self.assertFalse(coverage_complete([{'rule': 'X', 'checkStatus': 'NOT_CHECKED'}]))

    def test_optional_and_not_applicable_do_not_stop_coverage(self):
        self.assertTrue(coverage_complete([{'checkStatus': 'NOT_CHECKED', 'required': False}, {'checkStatus': 'NOT_APPLICABLE'}, {'checkStatus': 'CHECKED'}]))

    def test_coverage_and_passing_are_separate(self):
        result = run(invoice_fields(total='999.00'), db=FakeDb(vendors=[vendor()]))
        self.assertFalse(result['validation_passed'])
        complete = run(db=FakeDb(vendors=[vendor()]))
        self.assertTrue(complete['coverage_complete'] and complete['validation_passed'])

    def test_stored_blocking_agrees_with_coverage(self):
        for result in (run(db=FakeDb(vendors=[vendor()])), run(db=FakeDb(vendors=Exception, references=Exception)), run(invoice_fields(subtotal=None, tax=None), db=FakeDb())):
            stored = any(stored_status(c)[2] for c in result['checks'])
            self.assertEqual(stored, not result['coverage_complete'])

    def test_one_user_facing_finding_for_a_missing_currency(self):
        result = run(invoice_fields(currency=None), db=FakeDb(vendors=[vendor()]))
        found = [e for e in result['errors'] if e.get('field') == 'currency']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['relatedRules'], ['PROFILE-REQUIRED'])

    def test_a_failed_read_is_kept_apart_from_an_empty_result(self):
        self.assertEqual(read_or_unavailable(FakeDb(vendors=[]), lambda: []), [])
        self.assertIsNone(read_or_unavailable(None, lambda: []))

        def broken():
            raise RuntimeError('down')
        self.assertIsNone(read_or_unavailable(FakeDb(), broken))


class TaxInclusiveTotalTests(unittest.TestCase):
    """The trap in the saved REC00006: 160.17 failed the line check, and 169.78 failed the total check."""

    def receipt(self, *, total_raw='Total Sales (Inclusive GST) RM\n169.78', **values):
        base = {'supplier': 'Shop', 'date': '2017-11-20', 'currency': 'MYR', 'tax': '9.61', 'total': '169.78', **values}
        fields = [field(key, value) for key, value in base.items() if value is not None]
        next(f for f in fields if f['field_key'] == 'total')['raw_value'] = total_raw
        items = [{'description': 'Goods', 'quantity': None, 'unit_price': None, 'amount': '169.78', 'field_states': {}}]
        return run(fields, document={**DOCUMENT, 'kind': 'receipt'}, items=items, item_structure={'status': 'itemized'})

    def test_true_base_passes_both_checks(self):
        result = self.receipt(subtotal='160.17')
        self.assertEqual(codes(result) & {'RECEIPT-TOTAL', 'RECEIPT-SUBTOTAL'}, set())
        self.assertIn('include tax', by_rule(result, 'RECEIPT-SUBTOTAL')[0]['message'])

    def test_inclusive_amount_entered_as_subtotal_does_not_add_tax_again(self):
        result = self.receipt(subtotal='169.78')
        self.assertNotIn('RECEIPT-TOTAL', codes(result))
        message = by_rule(result, 'RECEIPT-TOTAL')[0]['message']
        self.assertIn('not added again', message)
        self.assertIn('160.17', message)

    def test_a_total_not_labelled_inclusive_still_fails(self):
        self.assertIn('RECEIPT-TOTAL', codes(self.receipt(subtotal='169.78', total_raw='Total\n169.78')))

    def test_a_wrong_base_still_fails(self):
        self.assertIn('RECEIPT-TOTAL', codes(self.receipt(subtotal='150.00')))

    def test_inclusive_total_rounding_and_payable_must_agree(self):
        self.assertIn('RECEIPT-TOTAL', codes(self.receipt(total='170.00', total_including_tax='169.78', rounding='0.02')))

    def test_inclusive_total_with_no_base_reconciles_without_adding_tax(self):
        result = self.receipt(total='169.80', total_including_tax='169.78', rounding='0.02')
        self.assertIn('Tax is not added again', by_rule(result, 'RECEIPT-TOTAL')[0]['message'])

    def test_tender_and_change_never_enter_the_total_equation(self):
        result = self.receipt(subtotal='160.17', total='169.80', total_including_tax='169.78', rounding='0.02', tendered_amount='200.00', change_given='30.20')
        self.assertIn('160.17 + 9.61 tax + 0.02 rounding = 169.80', by_rule(result, 'RECEIPT-TOTAL')[0]['message'])
        self.assertEqual(by_rule(result, 'RCP-005')[0]['checkStatus'], 'CHECKED') if by_rule(result, 'RCP-005') else None

    def test_change_that_does_not_fit_the_payment_fails(self):
        self.assertIn('RCP-005', codes(self.receipt(subtotal='160.17', total='169.80', total_including_tax='169.78', rounding='0.02', tendered_amount='200.00', change_given='20.00')))
