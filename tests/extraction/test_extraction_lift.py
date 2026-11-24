"""Lite extraction lift: reasoning effort, rate-limit back-off, heading columns, bill figures."""
import io
import json
from urllib.error import HTTPError

import pytest

from services.extraction.ai import text_llm
from services.extraction.lite import structure
from tests.extraction.test_lite_extraction import ENV, FakeOpener, FakeProvider, completion, document, row

pytestmark = pytest.mark.runtime_defaults
CUSTOM = {'AI_EXTRACTION': 'text', 'AI_PROVIDERS': 'custom', 'AI_BASE_URL': 'https://api.example.test/openai/v1',
          'AI_MODEL': 'openai/gpt-oss-120b', 'AI_API_KEY': 'c' * 32}


def sent_body(environ):
    opener = FakeOpener(completion('{"document_type": "bill"}'))
    provider = text_llm.TextModelProvider(environ['AI_PROVIDERS'], first=True, environ=environ, opener=opener)
    provider.complete_json('s', 'u')
    return json.loads(opener.requests[0].data)


# --- request: reasoning effort and back-off --------------------------------

def test_gpt_oss_defaults_to_low_reasoning_effort_and_others_send_none():
    assert sent_body(CUSTOM)['reasoning_effort'] == 'low'
    assert 'reasoning_effort' not in sent_body(ENV)  # gemini preset: not a gpt-oss model


def test_reasoning_effort_setting_and_extra_body_override():
    assert 'reasoning_effort' not in sent_body({**CUSTOM, 'AI_REASONING_EFFORT': 'off'})
    assert sent_body({**CUSTOM, 'AI_REASONING_EFFORT': 'medium'})['reasoning_effort'] == 'medium'
    assert sent_body({**CUSTOM, 'AI_EXTRA_BODY': '{"reasoning_effort": "high"}'})['reasoning_effort'] == 'high'
    with pytest.raises(text_llm.TextModelError) as caught:
        text_llm.TextModelProvider('custom', first=True, environ={**CUSTOM, 'AI_REASONING_EFFORT': 'max'})
    assert caught.value.code == 'AI_REASONING_EFFORT_INVALID'


def test_rate_limit_back_off_honours_retry_after_and_is_capped():
    assert text_llm.retry_delay(429, None, 1) == 5.0 and text_llm.retry_delay(429, None, 3) == 20.0
    assert text_llm.retry_delay(429, '12.5', 1) == 12.5
    assert text_llm.retry_delay(429, '600', 1) == 60.0
    assert text_llm.retry_delay(429, 'soon', 2) == 10.0
    assert text_llm.retry_delay(503, None, 1) == 1.5 and text_llm.retry_delay(503, '90', 1) == 20.0


def test_rate_limited_call_waits_and_succeeds_on_a_later_attempt():
    limited = lambda: HTTPError('https://x', 429, 'Too Many Requests', {'Retry-After': '7'}, io.BytesIO(b''))
    waits = []
    opener = FakeOpener(limited(), limited(), completion('{"document_type": "bill"}'))
    provider = text_llm.TextModelProvider('custom', first=True, environ=CUSTOM, opener=opener, sleep=waits.append)
    answer, record = provider.complete_json('s', 'u')
    assert answer == {'document_type': 'bill'} and record['attempts'] == 3 and waits == [7.0, 10.0]


def test_benchmark_continues_after_a_rate_limit():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        'lite_benchmark', Path(__file__).resolve().parents[2] / 'scripts' / 'lite_benchmark.py')
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    assert 'AI_RATE_LIMITED' not in benchmark.FATAL_AI_ERRORS and benchmark.RATE_LIMIT_COOLDOWN_SECONDS > 0


# --- heading columns beside names and addresses -----------------------------

@pytest.mark.parametrize('value,name,expected', [
    ('Dahlia Halia Air Komuniti Berhad | BILL WATER', True, 'Dahlia Halia Air Komuniti Berhad'),
    ('Bayu Cahaya Air Komuniti Berhad | BIL / BILL WATER', True, 'Bayu Cahaya Air Komuniti Berhad'),
    ('No. 54\nJalan Mawar 8/6 | BIL ELECTRICITY\nTaman Gemilang Baru', False, 'No. 54\nJalan Mawar 8/6\nTaman Gemilang Baru'),
    ('Puncak Cahaya Supplies PLT | No. Akaun:', True, 'Puncak Cahaya Supplies PLT'),
    ('Air Selangor Sdn Bhd', True, 'Air Selangor Sdn Bhd'),  # no column: unchanged, "Air" kept
    ('No. 3 | Jalan Air Itam', False, 'No. 3 Jalan Air Itam'),
])
def test_drop_heading_columns(value, name, expected):
    assert structure.drop_heading_columns(value, name=name) == expected


# --- bill subtotal and amount due from printed figures ----------------------

def bill_rows(*extra):
    texts = [('Bayu Cahaya Air Komuniti Berhad', 60, 40), ('BIL / BILL WATER', 700, 40),
             ('Account No.: 541950329943', 60, 120), ('Bill No.: B895256466', 60, 160),
             ('Caj Air / Water Charge', 60, 300), ('142.93', 900, 300),
             ('Caj Meter / Meter Rental', 60, 340), ('14.67', 900, 340),
             ('Jumlah Caj Semasa / Current Charges', 60, 380), ('157.60', 900, 380),
             ('SST', 60, 420), ('12.61', 900, 420), ('Jumlah / Total', 60, 460), ('170.21', 900, 460), *extra]
    return [row(index, text, left, top) for index, (text, left, top) in enumerate(texts)]


def bill_answer(**overrides):
    fields = {'seller_name': {'value': 'Bayu Cahaya Air Komuniti Berhad | BIL / BILL WATER', 'raw': None},
              'tax_amount': {'value': '12.61', 'raw': '12.61'}, 'total_amount': {'value': '170.21', 'raw': '170.21'},
              'subtotal': {'value': None, 'raw': None}, 'amount_due': {'value': None, 'raw': None}}
    fields.update(overrides)
    return {'document_type': 'bill', 'fields': fields, 'line_items': []}


def test_bill_subtotal_and_amount_due_are_read_from_printed_figures(monkeypatch):
    monkeypatch.delenv('AI_PREFILL', raising=False)
    result = structure.structure_document(document(bill_rows()), providers=[FakeProvider(bill_answer())])
    fields = result['fields']
    assert fields['issuer']['value'] == 'Bayu Cahaya Air Komuniti Berhad'
    assert fields['subtotal']['value'] == '157.60' and fields['subtotal']['evidence'][0]['text'] == '157.60'
    assert fields['subtotal']['candidates'][0]['source_check'] == 'printed_on_labelled_line'
    # No arrears, payment or adjustment printed: the amount due is the printed total.
    assert fields['amount_due']['value'] == '170.21'
    assert fields['amount_due']['candidates'][0]['source_check'] == 'printed_total_no_adjustment_printed'
    flagged = {issue['field'] for issue in result['issues'] if issue['code'] == 'VALUE_FROM_PRINTED_FIGURES'}
    assert flagged == {'subtotal', 'amount_due'}


def test_amount_due_is_not_assumed_when_arrears_are_printed(monkeypatch):
    monkeypatch.delenv('AI_PREFILL', raising=False)
    rows = bill_rows(('Tunggakan / Arrears', 60, 500), ('288.76', 900, 500))
    fields = structure.structure_document(document(rows), providers=[FakeProvider(bill_answer())])['fields']
    assert fields['amount_due']['state'] == 'missing'


def test_amount_due_is_read_from_its_own_printed_line(monkeypatch):
    monkeypatch.delenv('AI_PREFILL', raising=False)
    rows = bill_rows(('Tunggakan / Arrears', 60, 500), ('288.76', 900, 500),
                     ('Jumlah Perlu Dibayar / Amount Due (RM)', 60, 540), ('458.97', 900, 540))
    fields = structure.structure_document(document(rows), providers=[FakeProvider(bill_answer())])['fields']
    assert fields['amount_due']['value'] == '458.97'


def test_subtotal_from_total_less_tax_only_when_that_amount_is_printed(monkeypatch):
    monkeypatch.delenv('AI_PREFILL', raising=False)
    unlabelled = [r for r in bill_rows() if 'Current Charges' not in r['text']]
    fields = structure.structure_document(document(unlabelled), providers=[FakeProvider(bill_answer())])['fields']
    assert fields['subtotal']['value'] == '157.60'
    assert fields['subtotal']['candidates'][0]['source_check'] == 'printed_total_less_printed_tax'
    not_printed = [r for r in unlabelled if r['text'] != '157.60']
    fields = structure.structure_document(document(not_printed), providers=[FakeProvider(bill_answer())])['fields']
    assert fields['subtotal']['state'] == 'missing'  # never a computed figure that the document does not print


def test_a_model_value_is_never_replaced(monkeypatch):
    monkeypatch.delenv('AI_PREFILL', raising=False)
    answer = bill_answer(subtotal={'value': '142.93', 'raw': '142.93'})
    fields = structure.structure_document(document(bill_rows()), providers=[FakeProvider(answer)])['fields']
    assert fields['subtotal']['value'] == '142.93'


@pytest.mark.parametrize('printed,expected', [('Bill No.: B293891844', 'B293891844'),
                                              ('No. Bil / Bill No.: B895256466', 'B895256466'),
                                              ('Debit Note No.: DN-2605-6676', 'DN-2605-6676'),
                                              ('DN-2605-6676', 'DN-2605-6676'), ('No. 12', 'No. 12')])
def test_a_label_copied_before_a_reference_number_is_removed(printed, expected):
    assert structure._REFERENCE_LABEL.sub('', printed) == expected


def test_bill_number_and_printed_period_separators(monkeypatch):
    monkeypatch.delenv('AI_PREFILL', raising=False)
    rows = bill_rows(('Tempoh Bil: 01/07/2026-31-07-2026', 60, 200))
    answer = bill_answer(document_number={'value': 'Bill No.: B895256466', 'raw': 'Bill No.: B895256466'},
                         billing_period={'value': '01/07/2026 - 31/07/2026', 'raw': '01/07/2026-31-07-2026'})
    fields = structure.structure_document(document(rows), providers=[FakeProvider(answer)])['fields']
    assert fields['bill_number']['value'] == 'B895256466'
    assert fields['billing_period']['value'] == '01/07/2026 - 31-07-2026'
