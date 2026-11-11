from types import SimpleNamespace as NS

import pytest

from ai.evaluation.field_accuracy import (
    evaluate_document_collection,
    evaluate_document_fields,
    prediction_view_from_public_result,
    summarize_collection,
)


HASH = 'a' * 64


def gold(field_key, value_type='text', state='PRESENT', value='expected',
         normalized_value=None, verification='INDEPENDENTLY_VERIFIED'):
    return NS(
        field_key=field_key,
        value_type=value_type,
        label_state=state,
        value=value if state == 'PRESENT' else None,
        normalized_value=(normalized_value if normalized_value is not None else value)
        if state == 'PRESENT' else None,
        annotation=NS(verification_state=verification),
    )


def cell(field_key, value, value_type='text', state='resolved', evidence=None):
    return NS(field_key=field_key, value_type=value_type, value=value, state=state,
              evidence=evidence or [])


def sample(*labels, sample_id='S-1', source_hash=HASH):
    return NS(sample_id=sample_id, source=NS(sha256=source_hash), fields=list(labels))


def prediction(*cells, sample_id='S-1', source_hash=HASH):
    return NS(sample_id=sample_id, source_sha256=source_hash, fields=list(cells))


def test_scores_present_missing_and_confirmed_absent_separately():
    reference = sample(
        gold('invoice_number', value=' INV-7 ', normalized_value='INV-7'),
        gold('supplier_name', value='Expected Supplier'),
        gold('invoice_date', value_type='date', value='2026-04-03'),
        gold('purchase_order_number', state='CONFIRMED_ABSENT'),
        gold('payment_terms', state='UNLABELLED'),
    )
    extracted = prediction(
        cell('invoice_number', 'INV-7'),
        cell('supplier_name', 'Wrong Supplier'),
        cell('purchase_order_number', 'PO-88'),
        cell('payment_terms', 'Net 30'),
    )

    result = evaluate_document_fields(reference, extracted)

    assert [item.outcome for item in result.field_outcomes] == [
        'CORRECT', 'INCORRECT', 'MISSING', 'FALSE_FILL', 'EXCLUDED',
    ]
    assert result.denominators.model_dump() == {
        'eligible_present': 3,
        'correct': 1,
        'incorrect': 1,
        'missing': 1,
        'eligible_absent': 1,
        'correct_absent': 0,
        'false_fills': 1,
        'excluded': 1,
        'evidence_available': 0,
    }
    assert result.accuracy == '0.3333'
    assert result.extraction_coverage == '0.6667'


def test_exact_decimal_values_are_compared_without_float_rounding():
    reference = sample(gold('total', value_type='decimal', value='10.00'))

    result = evaluate_document_fields(reference, prediction(cell('total', '10.0', 'decimal')))

    assert result.field_outcomes[0].outcome == 'CORRECT'


def test_text_comparison_does_not_invent_case_or_spacing_tolerance():
    reference = sample(gold('invoice_number', normalized_value='INV-7'))

    result = evaluate_document_fields(reference, prediction(cell('invoice_number', 'inv-7')))

    assert result.field_outcomes[0].outcome == 'INCORRECT'


@pytest.mark.parametrize(
    ('state', 'verification', 'reason'),
    [
        ('AMBIGUOUS', 'INDEPENDENTLY_VERIFIED', 'AMBIGUOUS'),
        ('UNREADABLE', 'INDEPENDENTLY_VERIFIED', 'UNREADABLE'),
        ('PRESENT', 'UNVERIFIED', 'UNVERIFIED'),
    ],
)
def test_ineligible_reference_labels_are_excluded(state, verification, reason):
    label = gold('invoice_number', state=state, verification=verification)
    result = evaluate_document_fields(sample(label), prediction(cell('invoice_number', 'anything')))

    assert result.field_outcomes[0].outcome == 'EXCLUDED'
    assert result.field_outcomes[0].exclusion_reason == reason
    assert result.denominators.eligible_present == 0
    assert result.accuracy is None


def test_missing_prediction_counts_as_miss_for_present_and_correct_absence_for_absent():
    reference = sample(
        gold('invoice_number'),
        gold('purchase_order_number', state='CONFIRMED_ABSENT'),
    )

    result = evaluate_document_fields(reference, None)

    assert [item.outcome for item in result.field_outcomes] == ['MISSING', 'CORRECT_ABSENT']
    assert result.denominators.eligible_present == 1
    assert result.denominators.missing == 1
    assert result.denominators.eligible_absent == 1
    assert result.denominators.correct_absent == 1


def test_rejects_prediction_for_different_source_bytes():
    with pytest.raises(ValueError, match='source hash'):
        evaluate_document_fields(sample(gold('invoice_number')), prediction(source_hash='b' * 64))


def test_collection_includes_samples_without_a_prediction():
    references = [sample(gold('invoice_number'), sample_id='S-1'),
                  sample(gold('invoice_number'), sample_id='S-2')]

    results = evaluate_document_collection(references, [prediction(cell('invoice_number', 'expected'), sample_id='S-1')])

    assert len(results) == 2
    assert results[0].field_outcomes[0].outcome == 'CORRECT'
    assert results[1].field_outcomes[0].outcome == 'MISSING'
    summary = summarize_collection(results)
    assert summary.denominators.eligible_present == 2
    assert summary.denominators.correct == 1
    assert summary.denominators.missing == 1
    assert summary.accuracy == '0.5000'


def test_public_result_adapter_maps_legacy_supplier_key_by_document_kind():
    reference = NS(sample_id='B-1', document_type='bill', source=NS(sha256=HASH),
                   fields=[gold('provider_name', value='Water Board')])
    raw_result = {
        'fields': {
            'supplier': {'state': 'resolved', 'value': 'Water Board', 'evidence': [{'page': 1}]},
        },
        'provenance': {'service_run_id': 'run-1'},
    }

    adapted = prediction_view_from_public_result(reference, raw_result)
    result = evaluate_document_fields(reference, adapted)

    assert result.field_outcomes[0].outcome == 'CORRECT'
    assert result.denominators.evidence_available == 1
    assert adapted.extraction_run_id == 'run-1'


@pytest.mark.parametrize(
    ('kind', 'source_key', 'gold_key', 'value_type', 'value'),
    [
        ('invoice', 'invoice', 'invoice_number', 'text', 'INV-8'),
        ('invoice', 'date', 'invoice_date', 'date', '2026-03-04'),
        ('invoice', 'tax_amount', 'tax', 'decimal', '6.00'),
        ('invoice', 'total_amount', 'total', 'decimal', '106.00'),
        ('receipt', 'supplier_name', 'merchant_name', 'text', 'Cafe'),
        ('receipt', 'date', 'transaction_date', 'date', '2026-03-04'),
    ],
)
def test_public_result_adapter_uses_dictionary_aliases_by_kind(kind, source_key, gold_key, value_type, value):
    reference = NS(sample_id='S-1', document_type=kind, source=NS(sha256=HASH),
                   fields=[gold(gold_key, value_type=value_type, value=value)])
    raw_result = {'fields': {source_key: {'state': 'resolved', 'value': value, 'evidence': []}}}

    adapted = prediction_view_from_public_result(reference, raw_result)

    assert adapted.fields[0].field_key == gold_key
    assert evaluate_document_fields(reference, adapted).field_outcomes[0].outcome == 'CORRECT'


@pytest.mark.parametrize(('raw_text', 'expected_key'), [
    ('INVOICE NO.\nINV-2024-0042', 'bill_number'),
    ('Bill No: INV-2024-0042', 'bill_number'),
    ('No. Bil\nINV-2024-0042', 'bill_number'),
    ('Reference: INV-2024-0042', 'bill_reference'),
    ('Account No.\nINV-2024-0042', 'bill_reference'),
    ('INV-2024-0042', 'bill_reference'),
    (None, 'bill_reference'),
])
def test_bill_reference_scores_as_bill_number_only_with_a_printed_primary_number_label(raw_text, expected_key):
    from ai.evaluation.field_accuracy import ORIGINAL_ROLE_POLICY
    reference = NS(sample_id='B-2', document_type='bill', source=NS(sha256=HASH),
                   fields=[gold('bill_number', value='INV-2024-0042')])
    raw_result = {'fields': {'bill_reference': {'state': 'resolved', 'value': 'INV-2024-0042',
                                                'raw_text': raw_text, 'evidence': []}}}
    corrected = prediction_view_from_public_result(reference, raw_result)
    original = prediction_view_from_public_result(reference, raw_result, role_policy=ORIGINAL_ROLE_POLICY)

    assert corrected.fields[0].field_key == expected_key
    assert original.fields[0].field_key == 'bill_reference'  # The original scoring stays reproducible.
    assert evaluate_document_fields(reference, original).field_outcomes[0].outcome == 'MISSING'
    expected = 'CORRECT' if expected_key == 'bill_number' else 'MISSING'
    assert evaluate_document_fields(reference, corrected).field_outcomes[0].outcome == expected
    assert raw_result['fields']['bill_reference']['value'] == 'INV-2024-0042'  # The prediction is not rewritten.


def test_separate_bill_number_cell_is_never_shadowed_by_a_promoted_reference():
    reference = NS(sample_id='B-3', document_type='bill', source=NS(sha256=HASH),
                   fields=[gold('bill_number', value='B-1')])
    raw_result = {'fields': {
        'bill_number': {'state': 'resolved', 'value': 'B-1', 'evidence': []},
        'bill_reference': {'state': 'resolved', 'value': 'R-9', 'raw_text': 'Bill No: R-9', 'evidence': []}}}
    adapted = prediction_view_from_public_result(reference, raw_result)
    assert sorted(c.field_key for c in adapted.fields) == ['bill_number', 'bill_reference']


def test_unknown_role_policy_is_rejected():
    reference = NS(sample_id='B-4', document_type='bill', source=NS(sha256=HASH), fields=[])
    with pytest.raises(ValueError):
        prediction_view_from_public_result(reference, {'fields': {}}, role_policy='guess')


def test_address_separators_are_reported_separately_from_strict_exact_match():
    reference = sample(
        gold('supplier_address', value='Plot 14, MIDC Industrial Area, Andheri East, Mumbai - 400093'),
        gold('customer_address', value='15 Connaught Place, New Delhi'),
        gold('supplier_name', value='TechVision Distributors Pvt Ltd'),
    )
    extracted = prediction(
        cell('supplier_address', 'Plot 14, MIDC Industrial Area, Andheri East\nMumbai - 400093'),
        cell('customer_address', '16 Connaught Place\nNew Delhi'),
        cell('supplier_name', 'TechVision Distributors Pvt Ltd'),
    )

    result = evaluate_document_fields(reference, extracted)

    # Strict exact match is unchanged; the stored prediction keeps its printed line break.
    assert [o.outcome for o in result.field_outcomes] == ['INCORRECT', 'INCORRECT', 'CORRECT']
    assert extracted.fields[0].value.count('\n') == 1
    assert result.accuracy == '0.3333'
    # Separator-only equivalence; different content (16 vs 15) stays incorrect.
    assert result.address_normalized_fields == ('supplier_address',)
    assert result.address_normalized_accuracy == '0.6667'
    assert result.address_normalized_denominators.eligible_present == result.denominators.eligible_present
    summary = summarize_collection([result, result])
    assert (summary.accuracy, summary.address_normalized_accuracy) == ('0.3333', '0.6667')


@pytest.mark.parametrize('expected, actual, same', [
    ('1,200 Jalan Ampang, KL', '1,200 Jalan Ampang\nKL', True),
    ('1,200 Jalan Ampang', '1 200 Jalan Ampang', False),       # numeric comma is content
    ('Lot 5, Jalan Besar', 'Lot 5 Jalan-Besar', False),         # other punctuation still matters
    ('Lot 5, Jalan Besar', 'Jalan Besar, Lot 5', False),        # component order still matters
    ('Lot 5, Jalan Besar', 'LOT 5,\n  JALAN   BESAR', True),     # original case/whitespace rules
])
def test_shared_address_policy_is_separator_only(expected, actual, same):
    from ai.evaluation.address_policy import same_address
    assert same_address(expected, actual) is same


def test_non_address_fields_never_use_address_normalization():
    reference = sample(gold('supplier_name', value='Acme, Ltd'))
    result = evaluate_document_fields(reference, prediction(cell('supplier_name', 'Acme\nLtd')))
    assert result.address_normalized_fields == ()
    assert result.address_normalized_accuracy == result.accuracy == '0.0000'
