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
