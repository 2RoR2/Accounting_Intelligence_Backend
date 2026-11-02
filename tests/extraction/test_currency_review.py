"""Currency proposals stay advice: they rank, explain and never fill or verify the field."""
import json
import unittest

from services.extraction.contracts.currency_review import build_currency_review, decision_details, location_of, DOLLAR_FAMILY


def row(key, value=None, state='resolved', candidates=(), corrected=False):
    return {'field_key': key, 'field_label': key, 'normalized_value': value, 'current_value': value, 'extraction_state': state, 'resolution': 'parser_preserved',
            'candidates': list(candidates), 'evidence': [{'page': 1, 'bbox': {'l': 1, 't': 2, 'r': 3, 'b': 4}, 'source_ref': '#/x'}],
            'corrected_by': 'u' if corrected else None, 'corrected_at': 'now' if corrected else None}


DOLLAR = row('currency', None, 'ambiguous', [{'source': 'parser', 'value': None, 'raw_text': '$ 124,79', 'evidence': [{'page': 1, 'bbox': {'l': 5}}]}])


class CurrencyReviewTests(unittest.TestCase):
    def test_bare_dollar_alone_proposes_nothing(self):
        review = build_currency_review([DOLLAR])
        self.assertTrue(review['needed'])
        self.assertEqual(review['proposals'], [])
        self.assertEqual(review['alternatives'], list(DOLLAR_FAMILY))
        self.assertEqual(review['printedSymbol']['text'], '$ 124,79')

    def test_address_gives_a_proposal_that_stays_unverified(self):
        fields = [DOLLAR, row('supplier_address', '53017 Lopez Locks\nAlecborough, OH 49620')]
        review = build_currency_review(fields)
        top = review['proposals'][0]
        self.assertEqual((top['currency'], top['status'], top['support']), ('USD', 'proposed_not_verified', 'document_context'))
        self.assertEqual(top['clues'][0]['text'], 'OH 49620')
        self.assertFalse(review['sourceVerified'])
        self.assertIsNone(DOLLAR['current_value'])

    def test_no_numeric_confidence_anywhere(self):
        text = json.dumps(build_currency_review([DOLLAR, row('supplier_address', 'Alecborough, OH 49620'), row('customer_address', 'Boston, MA 02134')]))
        self.assertNotIn('confidence', text)
        self.assertNotIn('probability', text)
        self.assertNotIn('%', text)

    def test_a_place_without_dollars_cannot_explain_a_dollar_sign(self):
        review = build_currency_review([DOLLAR, row('supplier_address', '15 Connaught Place\nNew Delhi, Delhi - 110001')])
        self.assertEqual(review['proposals'], [])
        self.assertEqual(review['conflicts'][0]['currency'], 'INR')

    def test_no_symbol_uses_context_without_a_family_limit(self):
        fields = [row('currency', None, 'missing'), row('merchant_address', 'NO 2 & 4, JALAN BAYU 4,\n81750 MASAI, JOHOR'), row('supplier', 'OJC MARKETING SDN BHD')]
        review = build_currency_review(fields)
        self.assertEqual(review['proposals'][0]['currency'], 'MYR')
        self.assertEqual({c['kind'] for c in review['proposals'][0]['clues']}, {'supplier_address', 'supplier_company_form'})
        self.assertEqual(review['alternatives'], [])

    def test_printed_codes_outrank_every_inference(self):
        cands = [{'source': 'parser', 'value': 'USD', 'raw_text': 'USD 10.00'}, {'source': 'parser', 'value': 'SGD', 'raw_text': 'SGD 10.00'}]
        review = build_currency_review([row('currency', None, 'conflict', cands), row('supplier_address', 'Alecborough, OH 49620'), row('customer_address', 'Singapore 018989')])
        self.assertEqual([p['support'] for p in review['proposals'][:2]], ['printed_on_document', 'printed_on_document'])
        self.assertTrue(review['disagreement'])

    def test_verified_supplier_records_outrank_an_address(self):
        supplier = {'name': 'Example', 'address': None, 'records': [{'currency': 'SGD', 'count': 4}]}
        review = build_currency_review([DOLLAR, row('supplier_address', 'Alecborough, OH 49620')], supplier_record=supplier)
        self.assertEqual([p['currency'] for p in review['proposals']], ['SGD', 'USD'])
        self.assertEqual(review['proposals'][0]['support'], 'supplier_records')
        self.assertTrue(review['disagreement'])

    def test_more_agreeing_clues_rank_first_within_a_tier(self):
        supplier = {'name': 'Example', 'address': 'Toronto, ON M5V 2T6', 'records': []}
        review = build_currency_review([DOLLAR, row('supplier_address', 'Alecborough, OH 49620'), row('customer_address', 'Boston, MA 02134')], supplier_record=supplier)
        self.assertEqual(review['proposals'][0]['currency'], 'USD')

    def test_model_candidates_are_never_clues(self):
        review = build_currency_review([row('currency', None, 'missing', [{'source': 'ai', 'value': 'USD', 'raw_text': 'USD'}])])
        self.assertEqual(review['proposals'], [])

    def test_resolved_currency_needs_no_review(self):
        self.assertFalse(build_currency_review([row('currency', 'MYR', 'resolved')])['needed'])

    def test_confirmed_currency_keeps_its_proposals_for_display(self):
        confirmed = row('currency', 'USD', 'ambiguous', [{'source': 'parser', 'value': None, 'raw_text': '$ 1'}], corrected=True)
        review = build_currency_review([confirmed, row('supplier_address', 'Alecborough, OH 49620')])
        self.assertFalse(review['needed'])
        self.assertFalse(review['sourceVerified'])

    def test_decision_details_record_what_was_shown(self):
        review = build_currency_review([DOLLAR, row('supplier_address', 'Alecborough, OH 49620')])
        details = decision_details(review, 'USD', None)
        self.assertEqual((details['selectedRank'], details['proposalsShown'], details['sourceVerified']), (1, ['USD'], False))
        self.assertEqual(details['selectedBasis'][0]['text'], 'OH 49620')
        other = decision_details(review, 'SGD', None)
        self.assertIsNone(other['selectedRank'])
        self.assertEqual(other['selectedBasis'], [])

    def test_bare_symbol_warning_says_what_is_wrong(self):
        from services.extraction.contracts.reliability import field_guidance
        codes = [w['code'] for w in field_guidance(DOLLAR)['warnings']]
        self.assertEqual(codes, ['AMBIGUOUS_CURRENCY_SYMBOL'])

    def test_location_patterns_stay_narrow(self):
        self.assertEqual(location_of('Lot 5, Jalan Baru'), [])
        self.assertEqual(location_of(None), [])
        self.assertEqual(location_of('PSC 2324, Box 6632\nAPO AP 93827')[0][0], 'USD')
        self.assertEqual(location_of('10 Grid Street London SW1A 2AA')[0][0], 'GBP')
        self.assertEqual(location_of('Maharashtra - 400093')[0][0], 'INR')


if __name__ == '__main__':
    unittest.main()
