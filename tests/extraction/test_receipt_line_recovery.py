"""Public retained OCR plus a different receipt layout; no shared database writes."""
from copy import deepcopy
import json
from pathlib import Path

from tests.extraction.test_multidocument import contract
from tests.extraction.test_financial_layout import layout
from validation.documents.document_validation import evaluate_document


def receipt():
    return json.loads((Path(__file__).parent / 'data/receipts/sroie-row27-ocr.json').read_text(encoding='utf-8'))


def test_public_merged_table_recovers_rows_and_preserves_printed_evidence():
    doc = receipt()
    original = deepcopy(doc)
    raw, mapped = contract(doc)
    rows = mapped['line_items']
    assert len(rows) == 4
    assert [row['amount'] for row in rows] == ['10.00', '6.50', '4.20', '1.90']
    assert [row['quantity'] for row in rows] == ['1'] * 4
    assert rows[1]['description'] == 'Apollo cake original 30s'
    assert [row['unit_price'] for row in rows] == ['10.00', None, '4.20', '1.90']
    assert {e['text'] for e in rows[1]['evidence']['unit_price']} == {'6.80', '(Diso: 0.3000)'}
    assert '1x' in {e['text'] for e in rows[0]['evidence']['quantity']}
    assert all(e['source_ref'] and e['page'] == 1 and e['bbox'] for row in rows for ev in row['evidence'].values() for e in ev)
    fields = {row['field_key']: row['current_value'] for row in mapped['fields']}
    assert fields['subtotal'] is None and fields['tax'] is None and fields['total'] == '22.60'
    result = evaluate_document({'kind': 'receipt'}, {'raw_json': raw}, mapped['fields'], rows)
    assert result['errors'] == []
    checks = {c['rule']: c for c in result['checks']}
    assert all(checks['RECEIPT-' + rule]['checkStatus'] == 'CHECKED' for rule in ('LINE_DETAILS', 'SUBTOTAL', 'TOTAL'))
    assert not any(i['code'] == 'UNSUPPORTED_TABLE' for i in raw['issues'])
    assert doc == original


def test_incomplete_or_changed_amounts_cannot_be_made_to_match_total():
    for change in ('missing', 'wrong'):
        doc = receipt()
        if change == 'missing':
            doc['m2_text_lines'] = [r for r in doc['m2_text_lines'] if r['self_ref'] != '#/m2_text_lines/27']
        else:
            next(r for r in doc['m2_text_lines'] if r['self_ref'] == '#/m2_text_lines/27')['text'] = '4.70'
        _, mapped = contract(doc)
        assert not mapped['line_items']


def test_an_unexplained_or_incorrect_line_discount_still_blocks_validation():
    for note in ('', '(Diso: 0.2000)'):
        doc = receipt()
        next(r for r in doc['m2_text_lines'] if r['self_ref'] == '#/m2_text_lines/23')['text'] = note
        raw, mapped = contract(doc)
        result = evaluate_document({'kind': 'receipt'}, {'raw_json': raw}, mapped['fields'], mapped['line_items'])
        assert any(e['code'] == 'RECEIPT-FORMAT' and e['field'].endswith('unit_price') for e in result['errors'])


def test_different_receipt_multiplier_layout_keeps_exact_values():
    doc = layout('RECEIPT', [('Merchant: Public test store',20,50,300,70), ('Date: 2026-10-19',20,80,250,100),
        ('Currency: MYR',20,110,250,130), ('Description',20,150,170,170), ('Amount',400,150,490,170),
        ('Tea',20,185,170,205), ('2 x 3.50',20,210,170,230), ('7.00',400,210,490,230),
        ('Bread',20,245,170,265), ('1 x 4.00',20,270,170,290), ('4.00',400,270,490,290),
        ('Total: 11.00',20,320,300,340)])
    raw, mapped = contract(doc)
    assert [(r['description'], r['quantity'], r['unit_price'], r['amount']) for r in mapped['line_items']] == [
        ('Tea', '2', '3.50', '7.00'), ('Bread', '1', '4.00', '4.00')]
    assert evaluate_document({'kind': 'receipt'}, {'raw_json': raw}, mapped['fields'], mapped['line_items'])['errors'] == []


def test_separate_public_tilted_headerless_receipt_retains_five_items():
    doc = json.loads((Path(__file__).parent / 'data/receipts/sroie-rec00006-ocr.json').read_text())
    raw, mapped = contract(doc)
    assert [r['amount'] for r in mapped['line_items']] == ['117.90', '34.90', '4.10', '5.89', '6.99']
    assert all(r['quantity'] is None and r['unit_price'] is None for r in mapped['line_items'])
    assert evaluate_document({'kind': 'receipt'}, {'raw_json': raw}, mapped['fields'], mapped['line_items'])['errors'] == []
    # A bad OCR amount must leave real visible rows blocking, not classify the
    # source as having no items and let its printed summary conceal the loss.
    next(x for x in doc['m2_text_lines'] if x['text'] == 'RM117.90 s')['text'] = 'RM119.90 s'
    from services.extraction.parsing.map_invoice import map_document
    failed = map_document(doc)
    assert not failed['line_items']
    assert failed['provenance']['item_structure']['status'] == 'unmapped_rows'
