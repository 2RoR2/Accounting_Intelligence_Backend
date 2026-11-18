import unittest
from services.extraction.contracts.accounting_contract import prediction_dto, line_dto
from services.extraction.contracts.reliability import field_guidance, extraction_guidance


class ReliabilityTests(unittest.TestCase):
    def test_line_cells_keep_individual_missing_and_conflicting_guidance(self):
        result=line_dto({'id':'synthetic-line','description':'Synthetic item','quantity':None,
                         'unit_price':None,'amount':None,
                         'field_states':{'description':'resolved','quantity':'missing','amount':'ambiguous'},
                         'candidates':{'amount':[{'value':'10'},{'value':'20'}]}})
        self.assertIsNone(result['quantity'])
        self.assertEqual(result['guidance']['quantity']['warnings'][0]['code'],'MISSING_VALUE')
        self.assertEqual(result['guidance']['amount']['warnings'][0]['code'],'CONFLICTING_VALUES')
        self.assertTrue(result['guidance']['quantity']['confirmationRequired'])
        reviewed=line_dto({'id':'synthetic-line','quantity':'1','field_states':{'quantity':'missing'}},
                          corrections=[{'line_item_id':'synthetic-line','field_key':'quantity',
                                        'corrected_by':'actor','corrected_at':'timestamp'}])
        self.assertFalse(reviewed['guidance']['quantity']['confirmationRequired'])
        self.assertTrue(reviewed['guidance']['quantity']['warnings'][0]['reviewed'])

    def test_unknown_confidence_stays_null_with_qualitative_guidance(self):
        row={'field_key':'date','field_label':'Date','extraction_state':'ambiguous','current_value':None,
             'raw_value':'03/04/2026','candidates':[{'value':'03/04/2026','raw_text':'03/04/2026'}]}
        dto=prediction_dto(row)
        self.assertIsNone(dto['confidence']);self.assertIsNone(dto['value'])
        self.assertEqual(dto['warnings'][0]['code'],'AMBIGUOUS_DATE')
        self.assertTrue(dto['confirmationRequired'])

    def test_conflicting_totals_do_not_pick_one_candidate(self):
        result=field_guidance({'field_key':'total','extraction_state':'ambiguous','candidates':[{'value':'10'},{'value':'20'}]})
        self.assertEqual(result['warnings'][0]['code'],'CONFLICTING_TOTALS')

    def test_reviewed_correction_retains_original_warning_and_no_longer_requests_confirmation(self):
        result=field_guidance({'field_key':'invoice','extraction_state':'missing','corrected_by':'actor','corrected_at':'timestamp'})
        self.assertFalse(result['confirmationRequired'])
        self.assertTrue(result['warnings'][0]['reviewed'])

    def test_resolved_without_coordinates_is_explicitly_unverified(self):
        result=field_guidance({'field_key':'supplier','extraction_state':'resolved','evidence':[]})
        self.assertEqual(result['warnings'][0]['code'],'UNSUPPORTED_EVIDENCE')

    def test_tax_percentage_never_claims_tax_amount(self):
        result=field_guidance({'field_key':'tax','extraction_state':'ambiguous','candidates':[{'value':'6%','raw_text':'Tax: 6%'}]})
        self.assertIn('TAX_RATE_NOT_AMOUNT',[w['code'] for w in result['warnings']])

    def status(self,**row):
        return field_guidance({'field_key':'total',**row})['evidenceStatus']

    def test_evidence_status_describes_support_not_probability(self):
        good=[{'source_ref':'#/a','page':1,'bbox':{'l':0}}]
        self.assertEqual(self.status(extraction_state='resolved',evidence=good),'source_backed')
        self.assertEqual(self.status(extraction_state='resolved',evidence=[{'page':1}]),'unsupported')
        self.assertEqual(self.status(extraction_state='missing'),'missing')
        self.assertEqual(self.status(extraction_state='unreadable'),'unsupported')
        self.assertEqual(self.status(extraction_state='ambiguous',candidates=[{'value':'1'},{'value':'2'}]),'conflicting')
        self.assertEqual(self.status(extraction_state='ambiguous',candidates=[{'value':None,'raw_text':'$ 1'}]),'ambiguous_candidate')
        self.assertEqual(self.status(extraction_state='missing',candidates=[{'value':'1'}]),'ambiguous_candidate')
        self.assertEqual(self.status(extraction_state='missing',candidates=[{'source':'ai','value':'1'}]),'model_suggested')
        self.assertEqual(self.status(extraction_state='missing',corrected_by='actor',corrected_at='now'),'human_corrected')
        result=field_guidance({'field_key':'total','extraction_state':'resolved','evidence':good})
        self.assertNotIn('confidence',result)

    def test_high_ocr_score_is_reported_as_recognition_only_and_changes_nothing(self):
        result=field_guidance({'field_key':'total','extraction_state':'ambiguous','candidates':[{'value':'1'},{'value':'2'}],
                               'evidence':[{'ocr_score':.99},{'ocr_score':.91},{'ocr_score':None}]})
        self.assertEqual(result['ocrEngineScore'],{'min':.91,'max':.99,'scope':'text_recognition_only'})
        self.assertEqual(result['evidenceStatus'],'conflicting')
        self.assertIsNone(field_guidance({'field_key':'total','extraction_state':'missing'})['ocrEngineScore'])

    def test_empty_rows_and_unknown_kind_are_extraction_warnings(self):
        result=extraction_guidance({'document_type':{'value':'unknown'}},[])
        self.assertEqual({w['code'] for w in result},{'NO_MAPPED_LINE_ITEMS','UNKNOWN_DOCUMENT_KIND'})
