import json
from pathlib import Path
import tempfile
import unittest
from services.extraction.parsing.structured_note import extract_structured_note
from services.extraction.contracts.accounting_contract import map_m2_result


class StructuredNoteTests(unittest.TestCase):
    def source(self,code='02'):
        return {'Invoice':[{'InvoiceTypeCode':[{'_':code}],'ID':[{'_':'CN-42'}],
          'IssueDate':[{'_':'2024-07-23'}],'DocumentCurrencyCode':[{'_':'MYR'}],
          'AccountingSupplierParty':[{'Party':[{'PartyLegalEntity':[{'RegistrationName':[{ '_':"Supplier's Name"}]}]}]}],
          'BillingReference':[{'AdditionalDocumentReference':[{'ID':[{'_':'UNRELATED'}]}]},
                              {'InvoiceDocumentReference':[{'ID':[{'_':'INV-42'}]}]}],
          'LegalMonetaryTotal':[{'TaxInclusiveAmount':[{'_':'100.10'}], 'PayableAmount':[{'_':'100.10'}]}]}]}

    def run_source(self,payload):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name);source=root/'source.json';source.write_text(json.dumps(payload),encoding='utf-8')
        result=extract_structured_note('source-42',source,root)
        raw=json.loads(Path(result['public_result_path']).read_text(encoding='utf-8'))
        return result,raw,map_m2_result(raw,'source-42')

    def test_both_note_types_follow_strict_contract_and_never_approve(self):
        for code,kind in [('02','credit_note'),('03','debit_note')]:
            result,raw,mapped=self.run_source(self.source(code))
            self.assertEqual(raw['document_type']['value'],kind)
            self.assertFalse(result['accounting_approval_performed'])
            self.assertTrue(result['review_required'])
            self.assertEqual(next(f for f in mapped['fields'] if f['field_key']=='total')['current_value'],'100.10')

    def test_placeholder_is_retained_as_a_candidate_not_an_extracted_fact(self):
        _,raw,mapped=self.run_source(self.source())
        supplier=next(f for f in mapped['fields'] if f['field_key']=='supplier')
        self.assertIsNone(supplier['current_value'])
        self.assertIsNone(supplier['confidence'])
        self.assertTrue(supplier['candidates'][0]['placeholder'])
        self.assertFalse(supplier['candidates'][0]['auto_accepted'])

    def test_exact_role_pointer_selects_invoice_reference_without_guessing(self):
        _,raw,_=self.run_source(self.source())
        self.assertEqual(raw['fields']['invoice']['value'],'INV-42')
        self.assertEqual(raw['fields']['invoice']['evidence'][0]['source_ref'],
                         '#/Invoice/0/BillingReference/1/InvoiceDocumentReference/0/ID/0/_')

    def test_unknown_structured_type_does_not_become_a_credit_note(self):
        with self.assertRaisesRegex(ValueError,'UNSUPPORTED_STRUCTURED_DOCUMENT_TYPE'):
            self.run_source(self.source('01'))

    def test_bare_currency_symbol_cannot_become_a_currency(self):
        source=self.source();source['Invoice'][0]['DocumentCurrencyCode']=[{'_':'$'}]
        with self.assertRaisesRegex(ValueError,'Currency'):
            self.run_source(source)
