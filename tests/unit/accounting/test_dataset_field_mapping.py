import json
from copy import deepcopy
from pathlib import Path

import pytest

from ai.evaluation.field_dictionary import FIELDS
from services.extraction.contracts.accounting_contract import map_m2_result, record_values
from services.extraction.contracts.field_catalog import (
    CANONICAL_DOCUMENT_FIELDS,
    CANONICAL_FIELD_SCOPES,
    CANONICAL_FIELD_TYPES,
)

FIXTURE = Path(__file__).parents[2] / 'extraction/data/contracts/syn001_m2.json'
DOCUMENT_ID = '11111111-1111-4111-8111-111111111111'


def fixture():
    result = json.loads(FIXTURE.read_text())
    result['document_id'] = DOCUMENT_ID
    return result


def test_runtime_field_catalog_stays_aligned_with_dataset_dictionary():
    assert CANONICAL_FIELD_TYPES == {key: field['value_type'] for key, field in FIELDS.items()}
    assert CANONICAL_DOCUMENT_FIELDS == {
        key: sorted({kind for use in field['uses'] if use['scope'] == 'document'
                     for kind in use['document_types']})
        for key, field in FIELDS.items()
    }
    assert CANONICAL_FIELD_SCOPES == {
        key: sorted({use['scope'] for use in field['uses']})
        for key, field in FIELDS.items()
    }


def test_extended_invoice_fields_are_stored_under_their_canonical_keys():
    result = fixture()
    result['fields']['supplier_address'] = deepcopy(result['fields']['supplier_name'])
    result['fields']['supplier_address']['value'] = '1 Main Street'
    result['fields']['allowance_total'] = deepcopy(result['fields']['subtotal'])
    result['fields']['allowance_total']['value'] = '1.2500'

    mapped = map_m2_result(result, DOCUMENT_ID)
    values = record_values(mapped['fields'])

    assert values['supplier_address'] == '1 Main Street'
    assert values['allowances'] == '1.2500'


def test_canonical_fields_outside_the_document_scope_are_rejected():
    result = fixture()
    result['fields']['description'] = deepcopy(result['line_items'][0]['description'])

    with pytest.raises(ValueError, match='outside the canonical document scope'):
        map_m2_result(result, DOCUMENT_ID)


def test_boolean_dataset_field_is_persisted_as_text_and_read_back_as_boolean():
    result = fixture()
    result['document_type'] = {'value': 'receipt', 'status': 'classified'}
    result['fields'] = {
        'refund_indicator': {'value': 'true', 'state': 'resolved', 'method': 'PARSER',
            'raw_text': 'Refund', 'evidence': [], 'candidates': []},
    }
    mapped = map_m2_result(result, DOCUMENT_ID)

    assert mapped['fields'][0]['current_value'] == 'true'
    assert record_values(mapped['fields'])['refund_indicator'] is True


def test_bill_and_receipt_identifiers_keep_their_canonical_roles():
    bill = fixture()
    bill['document_type'] = {'value': 'bill', 'status': 'classified'}
    bill['fields'] = {
        'bill_number': {'value': 'B-14', 'state': 'resolved', 'method': 'PARSER',
            'raw_text': 'Bill no. B-14', 'evidence': [], 'candidates': []},
        'bill_reference': {'value': 'R-14', 'state': 'resolved', 'method': 'PARSER',
            'raw_text': 'Ref. R-14', 'evidence': [], 'candidates': []},
    }
    mapped = map_m2_result(bill, DOCUMENT_ID)
    values = record_values(mapped['fields'])

    assert values['bill_number'] == 'B-14'
    assert values['bill_reference'] == 'R-14'
