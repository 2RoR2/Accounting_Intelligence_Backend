"""BUYER-NOT-COMPANY: a purchase document addressed to another company (25 Nov review findings). No database."""
import unittest

from validation.documents.addressee import RULE, addressee_check, apply_addressee, names_match, readable_name
from validation.documents.document_validation import evaluate_document

KNT = {'name': 'Ketapang Nibong Trading Sdn. Bhd.', 'registrationNumber': '202601099901 (1590001-K)', 'tin': 'C9990001901'}
PAYABLE = 'accounts_payable'


def run(values, identity=KNT, category=PAYABLE):
    return addressee_check(identity, values, category)


class NameVariantTests(unittest.TestCase):
    def test_knt_own_name_in_its_usual_spellings_passes(self):
        for buyer in ('Ketapang Nibong Trading Sdn. Bhd.', 'KETAPANG NIBONG TRADING SDN BHD', 'ketapang nibong trading',
                      'Ketapang Nibong Trading Sendirian Berhad', 'Ketapang-Nibong Trading Sdn.Bhd.', 'Ketapang Nibong Trading (M) Sdn Bhd',
                      'Ketapang Nibong Trading Sdn. Bhd. (Kuching)', 'KNT', 'KNT Ketapang Nibong Trading', 'Ketapang Nibong Tradng Sdn Bhd'):
            with self.subTest(buyer=buyer):
                error, check = run({'customer': buyer})
                self.assertIsNone(error)
                self.assertEqual((check['rule'], check['result']), (RULE, 'PASS'))

    def test_another_company_fails_with_the_message(self):
        error, check = run({'customer': 'Puncak Tanjung Catering Sdn. Bhd.'})
        self.assertIsNone(check)
        self.assertEqual((error['code'], error['severity'], error['field']), (RULE, 'high', 'customer'))
        self.assertEqual(error['message'], 'This document is addressed to Puncak Tanjung Catering Sdn. Bhd., not '
                                           "Ketapang Nibong Trading Sdn. Bhd. If it isn't for this company, set it aside.")
        self.assertIn('not Acme Trading. If', run({'customer': 'Puncak Tanjung Catering'}, identity={'name': 'Acme Trading'})[0]['message'])

    def test_a_similar_but_different_company_still_fails(self):
        for buyer in ('Ketapang Trading Enterprise', 'Nibong Hardware Sdn Bhd', 'Sungai Nibong Trading Sdn Bhd'):
            with self.subTest(buyer=buyer):
                self.assertIsNotNone(run({'customer': buyer})[0])

    def test_form_words_alone_do_not_make_two_companies_match(self):
        self.assertFalse(names_match('Sdn Bhd', 'Ketapang Nibong Trading Sdn. Bhd.'))


class IdentifierTests(unittest.TestCase):
    def test_own_tin_passes_even_under_a_different_name(self):
        error, check = run({'customer': 'KN Trading Kuching Branch', 'customer_tax_id': 'c9990001901'})
        self.assertIsNone(error)
        self.assertIn('TIN', check['message'])

    def test_own_ssm_number_passes_in_another_format(self):
        error, check = run({'customer': 'Head Office Purchasing', 'customer_registration_id': '202601099901(1590001K)'})
        self.assertIsNone(error)
        self.assertIn('registration', check['message'])

    def test_another_companys_tin_with_another_name_fails(self):
        self.assertIsNotNone(run({'customer': 'Puncak Tanjung Catering', 'customer_tax_id': 'C1234567890'})[0])

    def test_a_tin_without_a_name_does_not_run(self):
        self.assertEqual(run({'customer_tax_id': 'C1234567890'}), (None, None))


class DoesNotRunTests(unittest.TestCase):
    def test_missing_buyer_does_not_run(self):
        self.assertEqual(run({}), (None, None))
        self.assertEqual(run({'customer': None}), (None, None))
        self.assertEqual(run({'customer': '  '}), (None, None))

    def test_unreadable_buyer_does_not_run(self):
        for buyer in ('#&', '1', 'N/A', 'Sdn Bhd', '-', 'Cash'):
            with self.subTest(buyer=buyer):
                self.assertFalse(readable_name(buyer))
                self.assertEqual(run({'customer': buyer}), (None, None))

    def test_sales_documents_and_unknown_direction_do_not_run(self):
        self.assertEqual(run({'customer': 'Puncak Tanjung Catering'}, category='accounts_receivable'), (None, None))
        self.assertEqual(run({'customer': 'Puncak Tanjung Catering'}, category=None), (None, None))

    def test_company_without_identity_does_not_run(self):
        self.assertEqual(run({'customer': 'Puncak Tanjung Catering'}, identity=None), (None, None))
        self.assertEqual(run({'customer': 'Puncak Tanjung Catering'}, identity={'name': '', 'tin': '', 'registrationNumber': ''}), (None, None))


class NewSupplierIsWithdrawnTests(unittest.TestCase):
    def test_new_supplier_is_not_raised_beside_it(self):
        result = {'errors': [{'code': 'EX-008', 'field': 'supplier', 'message': 'Supplier matching requires review.'}],
                  'checks': [], 'vendor_match': {'status': 'REVIEW'}}
        apply_addressee(result, KNT, {'customer': 'Puncak Tanjung Catering', 'supplier': 'Bayu Seroja Jalur Lebar Bersepadu Berhad'}, PAYABLE)
        self.assertEqual([e['code'] for e in result['errors']], [RULE])
        self.assertEqual([(c['rule'], c['checkStatus']) for c in result['checks']], [('EX-008', 'NOT_APPLICABLE')])
        self.assertEqual(result['vendor_match']['status'], 'NOT_CHECKED')

    def test_new_supplier_stays_for_a_document_addressed_to_us(self):
        result = {'errors': [{'code': 'EX-008', 'field': 'supplier'}], 'checks': []}
        apply_addressee(result, KNT, {'customer': 'Ketapang Nibong Trading'}, PAYABLE)
        self.assertEqual([e['code'] for e in result['errors']], ['EX-008'])


def field(key, value):
    return {'field_key': key, 'field_label': key, 'extraction_state': 'resolved' if value is not None else 'missing',
            'resolution': 'parser_preserved', 'current_value': value, 'normalized_value': value, 'raw_value': value,
            'corrected_by': None, 'corrected_at': None, 'evidence': [], 'candidates': []}


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Db:
    """Answers by table name: the company's identity, a supplier list without the sender, nothing else."""

    def execute(self, sql, params=()):
        if 'FROM tenants' in sql:
            return Rows([{'name': KNT['name'], 'registration_number': KNT['registrationNumber'], 'profile': {'tin': KNT['tin']}}])
        if 'FROM vendors' in sql:
            return Rows([{'id': 'v1', 'vendor_name': 'Kayu Jati Supplies Sdn Bhd'}])
        return Rows([])


class EndToEndTests(unittest.TestCase):
    """Through evaluate_document, as the bill BILL_100004 on uat (another company's bill in KNT's Inbox)."""

    def bill(self, customer):
        document = {'id': 'doc-4', 'tenant_id': 'tenant-1', 'kind': 'bill', 'category': PAYABLE, 'content_revision': 1}
        fields = [field('supplier', 'Bayu Seroja Jalur Lebar Bersepadu Berhad'), field('customer', customer),
                  field('reference', 'BSJ-7781'), field('date', '2026-11-03'), field('currency', 'MYR'),
                  field('total', '212.00'), field('amount_due', '212.00')]
        raw = {'mode': 'module2_public_result', 'document_type': {'value': 'bill', 'status': 'classified'}}
        return evaluate_document(document, {'raw_json': raw}, fields, [], Db())

    def test_another_companys_bill_fails_and_offers_no_new_supplier(self):
        result = self.bill('Puncak Tanjung Catering')
        codes = [e['code'] for e in result['errors']]
        self.assertIn(RULE, codes)
        self.assertNotIn('EX-008', codes)
        error = next(e for e in result['errors'] if e['code'] == RULE)
        self.assertEqual(error['cause'], 'NOT_ADDRESSED_TO_COMPANY')
        self.assertFalse(result['validation_passed'])

    def test_knt_bill_passes_this_check_and_keeps_supplier_matching(self):
        result = self.bill('KETAPANG NIBONG TRADING SDN BHD')
        self.assertNotIn(RULE, [e['code'] for e in result['errors']])
        self.assertIn('EX-008', [e['code'] for e in result['errors']])
        self.assertEqual([c['result'] for c in result['checks'] if c['rule'] == RULE], ['PASS'])

    def test_bill_without_a_buyer_has_no_result_for_it(self):
        result = self.bill(None)
        self.assertFalse([c for c in result['checks'] if c['rule'] == RULE])
        self.assertNotIn(RULE, [e['code'] for e in result['errors']])


if __name__ == '__main__':
    unittest.main()
