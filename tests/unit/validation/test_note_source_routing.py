import pytest
from validation.documents.document_validation import evaluate_document
from tests.unit.validation.test_bill_receipt_validation import prediction


@pytest.mark.parametrize('kind',['credit_note','debit_note'])
def test_classified_pdf_notes_reach_the_note_rules_instead_of_unknown_type(kind):
    fields=[prediction(key,value) for key,value in {
        'supplier':'Example Supplies','date':'2026-10-19','currency':'MYR','total':'10.00'}.items()]
    result=evaluate_document({'kind':kind},{'raw_json':{'mode':'module2_public_result',
        'document_type':{'value':kind,'status':'classified'}}},fields,[])
    assert not any(e['field']=='document_type' for e in result['errors'])
    assert result['review_required']
    assert not result['accounting_approval_performed']


def test_source_kind_disagreement_still_blocks_note_validation():
    result=evaluate_document({'kind':'debit_note'},{'raw_json':{'mode':'module2_public_result',
        'document_type':{'value':'credit_note','status':'classified'}}},[],[])
    assert any(e['field']=='document_type' for e in result['errors'])
