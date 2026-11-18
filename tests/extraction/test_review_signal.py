"""reviewSignal (task P2.2): verified / check / suggestion / missing from stored field data only."""
import pytest

from services.extraction.contracts.accounting_contract import map_m2_result, prediction_dto
from services.extraction.contracts.reliability import REVIEW_SIGNALS, review_signal

TEXT_LAYER = {'page': 1, 'source_ref': '#/rows/3', 'text': 'RM 135.00', 'from_ocr': False, 'engine_score': 1.0}


def ocr(score, name='engine_score'):
    return {'page': 1, 'source_ref': '#/rows/4', 'text': 'RM 135.00', 'from_ocr': True, name: score}


def signal(evidence=(), candidates=(), resolution='parser_preserved', state='resolved', **kwargs):
    return review_signal(evidence=list(evidence), candidates=list(candidates), resolution=resolution,
                         extraction_state=state, **kwargs)


# --- verified ----------------------------------------------------------------

def test_text_layer_value_is_verified():
    assert signal([TEXT_LAYER]) == 'verified'


@pytest.mark.parametrize('name', ['engine_score', 'ocr_score'])
def test_ocr_value_at_or_above_the_threshold_is_verified(name):
    assert signal([ocr(0.95, name)]) == 'verified'
    assert signal([ocr(0.90, name)], threshold=0.90) == 'verified'


# --- check -------------------------------------------------------------------

def test_ocr_value_below_the_threshold_needs_a_check():
    assert signal([ocr(0.62)]) == 'check'
    assert signal([TEXT_LAYER, ocr(0.89)]) == 'check'  # Every evidence row must be good enough.
    assert signal([ocr(0.95)], threshold=0.97) == 'check'


@pytest.mark.parametrize('entry', [
    {'page': 1, 'source_ref': '#/rows/4', 'from_ocr': True},                      # OCR without a score
    {'page': 1, 'source_ref': '#/rows/4', 'engine_score': 0.99},                 # unknown origin
    {'page': 1, 'source_ref': '#/rows/4', 'from_ocr': True, 'engine_score': True},  # not a number
])
def test_unknown_reading_quality_needs_a_check(entry):
    assert signal([entry]) == 'check'


def test_filled_value_without_source_evidence_needs_a_check():
    # For example DEFAULT_CURRENCY: filled by configuration, not read from the page.
    assert signal([]) == 'check'


@pytest.mark.parametrize('issue', ['DATE_ORDER_ASSUMED', 'DEFAULT_CURRENCY_APPLIED'])
def test_assumed_values_need_a_check(issue):
    assert signal([TEXT_LAYER], field_issues=[issue]) == 'check'
    assert signal([TEXT_LAYER], field_issues=['SOMETHING_ELSE']) == 'verified'


def test_model_disagreement_needs_a_check():
    conflict = {'source': 'ai', 'value': '153.00', 'auto_accepted': False, 'outcome': 'PARSER_MODEL_CONFLICT'}
    agreeing = {'source': 'ai', 'value': '135.00', 'auto_accepted': False, 'source_check': 'printed_in_source'}
    assert signal([TEXT_LAYER], [conflict]) == 'check'
    assert signal([TEXT_LAYER], [agreeing]) == 'verified'


# --- suggestion --------------------------------------------------------------

def test_unfilled_ai_candidate_is_a_suggestion():
    candidate = {'source': 'ai', 'value': 'INV-2026-0042', 'auto_accepted': False}
    assert signal([], [candidate], resolution='unresolved_missing', state='missing') == 'suggestion'


def test_model_value_kept_as_a_candidate_is_a_suggestion():
    # AI_PREFILL=never stores the verified model value as a VLM candidate for review.
    assert signal([ocr(0.99)], [{'source': 'ai', 'value': '135.00', 'auto_accepted': False}],
                  resolution='candidate_for_review') == 'suggestion'


# --- missing -----------------------------------------------------------------

@pytest.mark.parametrize('state,candidates', [
    ('missing', []),
    ('missing', [{'source': 'ai', 'value': None}]),
    ('ambiguous', [{'value': None, 'raw_text': '$'}]),  # A symbol only: the reviewer must enter it.
    ('unreadable', []),
])
def test_nothing_to_show_is_missing(state, candidates):
    assert signal([], candidates, resolution='unresolved_missing', state=state) == 'missing'


def test_every_signal_is_one_of_the_four():
    assert REVIEW_SIGNALS == ('verified', 'check', 'suggestion', 'missing')


# --- DTO ---------------------------------------------------------------------

def public_result(fields):
    return {'schema_version': '0.1.0-proposed', 'mode': 'module2_public_result', 'document_id': 'doc-r',
            'document_type': {'classifier_version': 't', 'method': 'text_model', 'value': 'invoice', 'status': 'classified',
                              'reason': 'MODEL_CLASSIFIED', 'candidates': [], 'unsupported_headings': [],
                              'review_required': True},
            'status': 'candidate_for_review', 'review_required': True, 'accounting_approval_performed': False,
            'safety': {'ai_candidate_auto_accepted': False, 'automatic_accounting_approval': False},
            'fields': fields, 'line_items': [], 'issues': [], 'routing': {'decision': 'vlm_candidate'},
            'provenance': {'service_run_id': 'lite-test'}}


def cell(value, method, evidence, **extra):
    return {'value': value, 'state': 'resolved' if value is not None else 'missing', 'method': method,
            'review_required': True, 'raw_text': value, 'evidence': evidence, 'candidates': [], 'confidence': None,
            'model_name': None, 'model_version': 'test', **extra}


def test_field_dto_carries_the_review_signal():
    mapped = map_m2_result(public_result({
        'invoice_number': cell('INV-2026-0042', 'PARSER', [TEXT_LAYER]),
        'total_amount': cell('135.00', 'OCR', [ocr(0.55)]),
        'currency': cell('MYR', 'PARSER', []),  # e.g. DEFAULT_CURRENCY
        'due_date': cell(None, 'OCR', []),
        'subtotal': cell('125.00', 'VLM', [ocr(0.99)]),
    }), 'doc-r')
    signals = {row['field_key']: prediction_dto(row)['reviewSignal'] for row in mapped['fields']}
    assert signals['invoice'] == 'verified'
    assert signals['total'] == 'check'
    assert signals['currency'] == 'check'
    assert signals['due'] == 'missing'
    assert signals['subtotal'] == 'suggestion'
    assert all(dto['confidence'] is None for dto in map(prediction_dto, mapped['fields']))
