"""Small synthetic regressions for observed public receipt shapes; no accuracy claims."""
from copy import deepcopy
from decimal import Decimal
import pytest
from services.extraction.classification.classify_document import classify_document
from services.extraction.layout.layout_headers import normalized_date, explicit_currencies
from tests.extraction.test_financial_layout import layout
from validation.documents.models import finite_decimal


def receipt_shape():
    return layout('TAXINVOICE', [
        ('07 Mar 2018 18:22',194,169,247,178),
        ('Item',143,224,158,233),('Qty',191,224,205,234),
        ('Nett Total: $8.20',250,299,297,307),
        ('CASH',142,320,163,330),('$8.20',278,320,297,330),
        ('Change',142,330,167,339),('$0.00',278,330,297,339)])


def test_named_month_and_wide_tender_columns_identify_retail_tax_invoice():
    result=classify_document(receipt_shape())
    assert (result['value'],result['status'])==('receipt','classified')
    assert result['candidates'][0]['evidence']['printed_headings'][0]['matched_title']=='taxinvoice'


@pytest.mark.parametrize('missing',['CASH','Change','item_header','07 Mar 2018 18:22'])
def test_incomplete_retail_evidence_does_not_override_fiscal_invoice(missing):
    doc=receipt_shape()
    doc['m2_text_lines']=[x for x in doc['m2_text_lines'] if x['text'] not in ({'Qty','Item'} if missing=='item_header' else {missing})]
    assert classify_document(doc)['value']!='receipt'


def test_bare_invoice_and_unsupported_titles_are_not_overridden_by_payment_cues():
    for title in ('INVOICE','QUOTATION','CREDIT NOTE'):
        doc=receipt_shape();doc['m2_text_lines'][0]['text']=title
        assert classify_document(doc)['value']!='receipt'


def test_competing_tender_values_remain_reviewable():
    doc=receipt_shape();cell=deepcopy(doc['m2_text_lines'][-3])
    cell.update(text='$9.00',self_ref='#/m2_text_lines/extra')
    cell['prov'][0]['bbox'].update(l=200,r=220)
    doc['m2_text_lines'].append(cell)
    assert classify_document(doc)['value']!='receipt'


def test_named_month_dates_validate_calendar_and_keep_numeric_ambiguity():
    assert normalized_date('07 Mar 2018')=='2018-03-07'
    assert normalized_date('29 February 2024')=='2024-02-29'
    for value in ('29 February 2023','07 Ma 2018','07/03/2018'):
        with pytest.raises(ValueError):normalized_date(value)


def test_currency_symbol_does_not_become_myr_from_dataset_location():
    assert explicit_currencies('$8.20')==set()
    assert explicit_currencies('RM 8.20')=={'MYR'}


def test_missing_numeric_is_reported_separately_from_inexact_numeric():
    with pytest.raises(ValueError,match='Missing numeric'):finite_decimal(None)
    for value in (8.2, True, float('nan')):
        with pytest.raises(ValueError,match='exact decimal'):finite_decimal(value)
    assert finite_decimal('8.20')==Decimal('8.20')
    assert finite_decimal(Decimal('8.20'))==Decimal('8.20')
