"""Lite engine: local reading, masking, text-model structuring and source verification.

No network, database or OCR model is used: the text model is a fake provider and
the page rows are synthetic (or come from a digital PDF's own text layer).
"""
import io
import json
import os
from pathlib import Path
from urllib.error import HTTPError

import pytest

from services.extraction.ai import text_llm
from services.extraction.contracts.accounting_contract import map_m2_result
from services.extraction.lite import pages, pipeline, structure
from services.extraction.lite.privacy import Masker

DATA = Path(__file__).parent / 'data'

pytestmark = pytest.mark.runtime_defaults


def row(order, text, left, top, *, width=None, page=1, score=0.97, from_ocr=True):
    width = width or 12 * len(text)
    return {'order': order, 'page': page, 'text': text, 'engine_score': score, 'from_ocr': from_ocr,
            'box_polygon_pixels': [[left, top], [left + width, top], [left + width, top + 24], [left, top + 24]]}


def document(rows, *, source='ocr'):
    return {'source_sha256': 'a' * 64, 'source_format': '.jpg', 'page_count': 1, 'truncated': False,
            'pages': [{'page': 1, 'source': source, 'size': {'width': 1200, 'height': 1600},
                       'coordinate_units': 'pixels', 'rows': rows, 'seconds': 0.1, 'transform': {}}],
            'rows': rows, 'versions': {'rapidocr': '3.10.0', 'onnxruntime': '1.31.1', 'pypdfium2': '5', 'pillow': '11'},
            'seconds': 0.1}


def invoice_rows():
    texts = [('KEDAI BESI MAJU SDN BHD', 60, 40), ('TAX INVOICE', 60, 90),
             ('Invoice No:', 60, 140), ('INV-2026-0042', 400, 140),
             ('Date:', 60, 180), ('13/11/2026', 400, 180),
             ('Bill To: Syarikat Contoh Enterprise', 60, 220),
             ('Tel: 03-1234 5678', 60, 260), ('Maybank A/C: 5123 4567 8901', 60, 300),
             ('Steel bracket 40mm', 60, 400), ('10', 500, 400, 24), ('12.50', 700, 400), ('125.00', 950, 400),
             ('Subtotal', 60, 500), ('RM 125.00', 950, 500),
             ('SST 8%', 60, 540), ('RM 10.00', 950, 540),
             ('Total', 60, 580), ('RM 135.00', 950, 580)]
    return [row(index, item[0], item[1], item[2], width=item[3] if len(item) > 3 else None)
            for index, item in enumerate(texts)]


class FakeProvider:
    name = 'fake'

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.prompts = answer, error, []

    def complete_json(self, system, user, *, max_tokens=4000):
        self.prompts.append(user)
        if self.error:
            raise text_llm.TextModelError(self.error)
        return self.answer, {'provider': 'fake', 'model': 'fake-model', 'local': False, 'status': 200,
                             'attempts': 1, 'seconds': 0.2, 'prompt_tokens': 10, 'completion_tokens': 10}


def good_answer(**overrides):
    fields = {
        'seller_name': {'value': 'Kedai Besi Maju Sdn Bhd', 'raw': 'KEDAI BESI MAJU SDN BHD'},
        'buyer_name': {'value': 'Syarikat Contoh Enterprise', 'raw': 'Syarikat Contoh Enterprise'},
        'document_number': {'value': 'INV-2026-0042', 'raw': 'INV-2026-0042'},
        'document_date': {'value': '2026-11-13', 'raw': '13/11/2026'},
        'currency': {'value': 'MYR', 'raw': 'RM'},
        'subtotal': {'value': '125.00', 'raw': 'RM 125.00'},
        'tax_amount': {'value': '10.00', 'raw': 'RM 10.00'},
        'total_amount': {'value': '135.00', 'raw': 'RM 135.00'},
        'due_date': {'value': None, 'raw': None},
    }
    fields.update(overrides)
    return {'document_type': 'invoice', 'fields': fields,
            'line_items': [{'description': 'Steel bracket 40mm', 'quantity': '10', 'unit_price': '12.50',
                            'line_total': '125.00'}]}


# --- masking ---------------------------------------------------------------

def test_masking_hides_identifiers_and_is_reversible():
    masker = Masker()
    text = ('Tel: 03-1234 5678  Email: akaun@contoh.com.my\n'
            'Maybank A/C: 5123 4567 8901\n'
            'IC 900101-14-5678\n'
            'Invoice No: INV-2026-0042  Date: 13-11-2026  Total RM 135.00')
    masked = masker.mask_text(text)
    for secret in ('03-1234 5678', 'akaun@contoh.com.my', '5123 4567 8901', '900101-14-5678'):
        assert secret not in masked
    # Accounting content the model needs stays readable.
    for kept in ('INV-2026-0042', '13-11-2026', '135.00', 'Maybank'):
        assert kept in masked
    assert masker.summary() == {'PHONE': 1, 'EMAIL': 1, 'ACCOUNT': 1, 'NRIC': 1}
    assert masker.unmask('[[ACCOUNT_1]]') == '5123 4567 8901'
    # The same value always maps to the same token.
    assert masker.mask_line('Call 03-1234 5678') == 'Call [[PHONE_1]]'


def test_masking_handles_a_number_on_the_line_after_its_label():
    masker = Masker()
    masked = masker.mask_text('Bank account number\n512345678901\nInvoice total 512345678901')
    assert masked.splitlines()[1] == '[[ACCOUNT_1]]'
    assert masked.splitlines()[2].endswith('512345678901')  # No account cue on that line.


# --- local normalisation ---------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('RM 1,234.50', '1234.50'), ('135', '135.00'), ('(12.00)', '-12.00'), ('MYR12.5', '12.50'),
    ('12.345', None), ('twelve', None), ('', None), (None, None)])
def test_parse_amount_never_rounds(raw, expected):
    assert structure.parse_amount(raw) == expected


@pytest.mark.parametrize('raw,order,expected', [
    ('13/11/2026', 'DMY', ('2026-11-13', False)), ('03/04/2026', 'DMY', ('2026-04-03', True)),
    ('03/04/2026', 'MDY', ('2026-03-04', True)), ('12/25/2013', 'DMY', ('2013-12-25', False)),
    ('2026-11-13', 'DMY', ('2026-11-13', False)), ('13 Nov 2026', 'DMY', ('2026-11-13', False)),
    ('5 Ogos 2026', 'DMY', ('2026-08-05', False)), ('Nov 13, 2026', 'DMY', ('2026-11-13', False)),
    ('13-11-26', 'DMY', ('2026-11-13', False)), ('31/02/2026', 'DMY', (None, False)),
    ('no date here', 'DMY', (None, False))])
def test_parse_date_is_deterministic(raw, order, expected):
    assert structure.parse_date(raw, order) == expected


def test_parse_time():
    assert structure.parse_time('03:29 PM') == '15:29'
    assert structure.parse_time('15:29:10') == '15:29:10'
    assert structure.parse_time('25:00') is None


# --- structuring and verification -----------------------------------------

def test_verified_values_fill_fields_and_satisfy_the_contract(monkeypatch):
    monkeypatch.delenv('AI_PREFILL', raising=False)
    provider = FakeProvider(good_answer())
    result = structure.structure_document(document(invoice_rows()), providers=[provider])
    fields = result['fields']
    assert result['document_type']['status'] == 'classified' and result['document_type']['value'] == 'invoice'
    assert fields['supplier_name']['value'] == 'Kedai Besi Maju Sdn Bhd'
    assert fields['invoice_number']['value'] == 'INV-2026-0042'
    assert fields['invoice_date']['value'] == '2026-11-13'
    assert fields['currency']['value'] == 'MYR'
    assert fields['total_amount']['value'] == '135.00'
    assert fields['due_date']['state'] == 'missing'
    total = fields['total_amount']
    assert total['state'] == 'resolved' and total['method'] == 'OCR'
    assert total['evidence'][0]['text'] == 'RM 135.00' and total['evidence'][0]['page'] == 1
    assert total['candidates'][0]['auto_accepted'] is False
    line = result['line_items'][0]
    assert line['description']['value'] == 'Steel bracket 40mm' and line['line_total']['value'] == '125.00'
    # Private identifiers were masked in what the model saw.
    prompt = provider.prompts[0]
    assert '5123 4567 8901' not in prompt and '03-1234 5678' not in prompt and '[[ACCOUNT_1]]' in prompt
    assert result['ai']['image_sent'] is False and result['ai']['masked_identifiers'] == {'PHONE': 1, 'ACCOUNT': 1}
    public = pipeline._public_result('doc', 'lite-x', document(invoice_rows()), classification=result['document_type'],
                                     fields=fields, lines=result['line_items'], issues=[], status='candidate_for_review',
                                     routing='vlm_candidate', ai_record=result['ai'], prefill=True)
    mapped = map_m2_result(public, 'doc')
    stored = {field['field_key']: field for field in mapped['fields']}
    assert stored['total']['current_value'] == '135.00' and stored['supplier']['current_value'] == 'Kedai Besi Maju Sdn Bhd'
    assert mapped['line_items'][0]['amount'] == '125.00'


def test_values_not_printed_in_the_source_are_discarded():
    answer = good_answer(total_amount={'value': '999.99', 'raw': 'RM 999.99'},
                         document_number={'value': 'INV-9999', 'raw': 'INV-9999'},
                         currency={'value': 'USD', 'raw': 'USD'})
    result = structure.structure_document(document(invoice_rows()), providers=[FakeProvider(answer)])
    for key in ('total_amount', 'invoice_number', 'currency'):
        assert result['fields'][key]['state'] == 'missing' and result['fields'][key]['value'] is None
    discarded = [issue for issue in result['issues'] if issue['code'] == 'AI_VALUE_DISCARDED']
    assert {issue['field'] for issue in discarded} == {'total_amount', 'document_number', 'currency'}
    # Only a hash of a rejected value is kept, never the value itself.
    assert all('999.99' not in json.dumps(issue) for issue in discarded)
    assert result['ai']['outcomes']['discarded'] == 3


def test_dates_are_normalised_locally_not_trusted_from_the_model(monkeypatch):
    rows = invoice_rows()
    rows[5]['text'] = '03/04/2026'
    # The model read month/day; the configured order is day/month.
    answer = good_answer(document_date={'value': '2026-03-04', 'raw': '03/04/2026'})
    result = structure.structure_document(document(rows), providers=[FakeProvider(answer)])
    assert result['fields']['invoice_date']['value'] == '2026-04-03'
    assert any(issue['code'] == 'DATE_ORDER_ASSUMED' for issue in result['issues'])
    monkeypatch.setenv('DATE_ORDER', 'MDY')
    result = structure.structure_document(document(rows), providers=[FakeProvider(answer)])
    assert result['fields']['invoice_date']['value'] == '2026-03-04'


def test_masked_account_number_is_restored_locally():
    rows = [row(0, 'TENAGA NASIONAL BERHAD', 60, 40), row(1, 'Bil Elektrik', 60, 90),
            row(2, 'Account No: 220012345678', 60, 140), row(3, 'Bill Date: 01/11/2026', 60, 180),
            row(4, 'Amount Due RM 88.40', 60, 220)]
    provider = FakeProvider({'document_type': 'bill', 'fields': {
        'seller_name': {'value': 'TENAGA NASIONAL BERHAD', 'raw': 'TENAGA NASIONAL BERHAD'},
        'account_number': {'value': '[[ACCOUNT_1]]', 'raw': '[[ACCOUNT_1]]'},
        'document_date': {'value': '2026-11-01', 'raw': '01/11/2026'},
        'amount_due': {'value': '88.40', 'raw': 'RM 88.40'},
        'currency': {'value': 'MYR', 'raw': 'RM'}}, 'line_items': []})
    result = structure.structure_document(document(rows), providers=[provider])
    assert '220012345678' not in provider.prompts[0]
    assert result['fields']['account_number']['value'] == '220012345678'
    assert result['fields']['issuer']['value'] == 'TENAGA NASIONAL BERHAD'
    assert result['fields']['amount_due']['value'] == '88.40'


def test_prefill_never_keeps_every_value_as_a_candidate(monkeypatch):
    monkeypatch.setenv('AI_PREFILL', 'never')
    result = structure.structure_document(document(invoice_rows()), providers=[FakeProvider(good_answer())])
    total = result['fields']['total_amount']
    assert total['method'] == 'VLM' and total['value'] == '135.00'
    public = pipeline._public_result('doc', 'lite-x', document(invoice_rows()), classification=result['document_type'],
                                     fields=result['fields'], lines=result['line_items'], issues=[],
                                     status='candidate_for_review', routing='vlm_candidate', ai_record=result['ai'],
                                     prefill=False)
    stored = {field['field_key']: field for field in map_m2_result(public, 'doc')['fields']}
    assert stored['total']['current_value'] is None and stored['total']['resolution'] == 'candidate_for_review'
    assert stored['total']['candidates'][-1] == {'source': 'ai', 'value': '135.00', 'auto_accepted': False}


def test_unknown_document_type_extracts_nothing():
    answer = good_answer()
    answer['document_type'] = 'delivery order'
    result = structure.structure_document(document(invoice_rows()), providers=[FakeProvider(answer)])
    assert result['document_type']['status'] == 'unknown' and result['fields'] == {} and result['line_items'] == []


# --- reading pages ---------------------------------------------------------

def test_digital_pdf_is_read_from_its_text_layer_without_ocr(monkeypatch):
    def no_ocr(image):
        raise AssertionError('A digital PDF must not be sent to OCR')
    monkeypatch.setattr(pages, 'ocr_image', no_ocr)
    loaded = pages.load_pages(DATA / 'development' / 'synthetic_invoice_001.pdf')
    assert loaded['page_count'] == 1 and loaded['pages'][0]['source'] == 'text_layer'
    assert loaded['rows'] and all(r['from_ocr'] is False and r['engine_score'] == 1.0 for r in loaded['rows'])
    assert [r['order'] for r in loaded['rows']] == list(range(len(loaded['rows'])))
    size = loaded['pages'][0]['size']
    for r in loaded['rows']:
        assert all(0 <= x <= size['width'] and 0 <= y <= size['height'] for x, y in r['box_polygon_pixels'])
    text, line_rows = pages.layout_text(loaded)
    assert len(text.splitlines()) == len(line_rows) and text.strip()


def test_unsupported_and_mislabelled_files_are_rejected(tmp_path):
    fake = tmp_path / 'not-a-pdf.pdf'
    fake.write_bytes(b'hello')
    with pytest.raises(pages.PageError) as caught:
        pages.load_pages(fake)
    assert caught.value.code == 'SOURCE_FORMAT_MISMATCH'
    with pytest.raises(pages.PageError) as caught:
        pages.load_pages(tmp_path / 'sheet.xlsx')
    assert caught.value.code == 'UNSUPPORTED_EXTRACTION_FORMAT'


# --- pipeline --------------------------------------------------------------

def source_file(tmp_path):
    path = tmp_path / 'scan.jpg'
    path.write_bytes(b'\xff\xd8\xff placeholder')
    return path


def test_pipeline_with_text_model_writes_a_valid_public_result(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_EXTRACTION', 'text')
    monkeypatch.setattr(pipeline, 'load_pages', lambda path: document(invoice_rows()))
    result = pipeline.execute_lite_extraction('doc-1', source_file(tmp_path), tmp_path,
                                              providers=[FakeProvider(good_answer())])
    assert result['execution_status'] == 'EXTRACTED' and result['extraction_status'] == 'candidate_for_review'
    assert result['review_required'] is True and result['accounting_approval_performed'] is False
    public = json.loads(Path(result['public_result_path']).read_text(encoding='utf-8'))
    assert public['provenance']['service_run_id'] == result['service_run_id']
    assert public['safety'] == {'ai_candidate_auto_accepted': False, 'automatic_accounting_approval': False,
                                'source_verified_prefill': True}
    assert public['routing'] == {'decision': 'vlm_candidate'}
    assert public['provenance']['ai']['image_sent'] is False
    assert map_m2_result(public, 'doc-1')['document_kind']['value'] == 'invoice'
    # The full OCR text stays in a private file next to the public artifact.
    assert (Path(result['public_result_path']).parent / 'ocr.private.json').is_file()


def test_pipeline_survives_a_model_outage(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_EXTRACTION', 'text')
    monkeypatch.setattr(pipeline, 'load_pages', lambda path: document(invoice_rows()))
    result = pipeline.execute_lite_extraction('doc-2', source_file(tmp_path), tmp_path,
                                              providers=[FakeProvider(error='AI_RATE_LIMITED')])
    assert result['execution_status'] == 'EXTRACTED'
    assert result['extraction_status'] == 'ai_attempt_failed_needs_review'
    public = json.loads(Path(result['public_result_path']).read_text(encoding='utf-8'))
    assert public['provenance']['ai']['error'] == 'AI_RATE_LIMITED'
    assert any(issue['code'] == 'AI_UNAVAILABLE' for issue in public['issues'])
    map_m2_result(public, 'doc-2')


def test_pipeline_with_ai_off_contacts_no_model(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_EXTRACTION', 'off')
    monkeypatch.setattr(pipeline, 'load_pages', lambda path: document(invoice_rows()))
    monkeypatch.setattr(text_llm, 'complete_json', lambda *a, **k: pytest.fail('AI is off'))
    result = pipeline.execute_lite_extraction('doc-3', source_file(tmp_path), tmp_path)
    assert result['execution_status'] == 'EXTRACTED' and result['extraction_status'] == 'human_review_required'
    public = json.loads(Path(result['public_result_path']).read_text(encoding='utf-8'))
    assert public['provenance']['ai'] == {'mode': 'off', 'image_sent': False}


def test_pipeline_reports_an_unreadable_source(tmp_path, monkeypatch):
    def unreadable(path):
        raise pages.PageError('INVALID_OR_UNSAFE_IMAGE')
    monkeypatch.setattr(pipeline, 'load_pages', unreadable)
    result = pipeline.execute_lite_extraction('doc-4', source_file(tmp_path), tmp_path)
    assert result['execution_status'] == 'STRUCTURAL_FAILURE' and result['error'] == 'INVALID_OR_UNSAFE_IMAGE'
    assert result['public_result_path'] is None


# --- text model client -----------------------------------------------------

class FakeResponse:
    status = 200

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self, limit=-1):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def open(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


def completion(content):
    return {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 7}}


ENV = {'AI_EXTRACTION': 'text', 'AI_PROVIDERS': 'gemini', 'GEMINI_API_KEY': 'k' * 32}


def test_provider_sends_text_only_and_never_exposes_the_key():
    opener = FakeOpener(completion('```json\n{"document_type": "invoice"}\n```'))
    provider = text_llm.TextModelProvider('gemini', first=True, environ=ENV, opener=opener)
    answer, record = provider.complete_json('system', 'user text')
    assert answer == {'document_type': 'invoice'}
    request = opener.requests[0]
    assert request.full_url == 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    body = json.loads(request.data)
    assert body['model'] == 'gemini-3.5-flash-lite' and body['temperature'] == 0
    assert [message['content'] for message in body['messages']] == ['system', 'user text']
    assert 'k' * 32 not in repr(provider) and 'k' * 32 not in json.dumps(record)
    assert record['prompt_tokens'] == 5 and record['attempts'] == 1


def test_provider_retries_rate_limits_then_falls_back():
    limited = lambda: HTTPError('https://x', 429, 'Too Many Requests', {}, io.BytesIO(b''))
    first = text_llm.TextModelProvider('gemini', first=True, environ={**ENV, 'AI_MAX_RETRIES': '1'},
                                       opener=FakeOpener(limited(), limited()), sleep=lambda seconds: None)
    second = text_llm.TextModelProvider(
        'openrouter', environ={**ENV, 'OPENROUTER_API_KEY': 'o' * 32, 'AI_OPENROUTER_MODEL': 'vendor/model:free'},
        opener=FakeOpener(completion('{"document_type": "receipt"}')))
    answer, record = text_llm.complete_json('s', 'u', providers=[first, second])
    assert answer == {'document_type': 'receipt'} and record['provider'] == 'openrouter'
    assert record['failed_before'] == [{'provider': 'gemini', 'error': 'AI_RATE_LIMITED'}]


@pytest.mark.parametrize('status,code', [(400, 'AI_BAD_REQUEST'), (401, 'AI_AUTH_REJECTED'), (402, 'AI_BILLING_REQUIRED'),
                                         (403, 'AI_AUTH_REJECTED'), (404, 'AI_MODEL_OR_ENDPOINT_NOT_FOUND'),
                                         (413, 'AI_REQUEST_TOO_LARGE'), (418, 'AI_HTTP_ERROR')])
def test_provider_http_errors_map_to_specific_codes_without_retrying(status, code):
    # Google answers an invalid key with 400; a model that rejects a request option also gives 400.
    opener = FakeOpener(HTTPError('https://x', status, 'error', {}, io.BytesIO(b'{"error": {}}')))
    provider = text_llm.TextModelProvider('gemini', first=True, environ=ENV, opener=opener, sleep=lambda s: None)
    with pytest.raises(text_llm.TextModelError) as caught:
        provider.complete_json('s', 'u')
    assert caught.value.code == code and len(opener.requests) == 1


def test_benchmark_loads_only_provider_variables_and_lets_the_command_line_win(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('lite_benchmark', Path(__file__).resolve().parents[2] / 'scripts' / 'lite_benchmark.py')
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    env_file = tmp_path / '.env'
    env_file.write_text('AI_MODEL=from-file\nAI_BASE_URL=https://file.example/v1\nGEMINI_API_KEY=file-key\n'
                        'DATABASE_URL=postgresql://should-not-load\nJWT_SECRET=should-not-load\n', encoding='utf-8')
    for name in ('AI_MODEL', 'GEMINI_API_KEY', 'DATABASE_URL', 'JWT_SECRET'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('AI_BASE_URL', '')  # Blanked on the command line: must stay blank.
    assert benchmark.load_provider_env(env_file) == 2
    assert os.environ['AI_MODEL'] == 'from-file' and os.environ['GEMINI_API_KEY'] == 'file-key'
    assert os.environ['AI_BASE_URL'] == ''
    assert 'DATABASE_URL' not in os.environ and 'JWT_SECRET' not in os.environ


@pytest.mark.parametrize('environ,code', [
    ({**ENV, 'GEMINI_API_KEY': ''}, 'AI_API_KEY_MISSING'),
    ({**ENV, 'AI_BASE_URL': 'https://example.com/v1'}, 'AI_BASE_URL_INVALID'),
    ({**ENV, 'AI_BASE_URL': 'http://generativelanguage.googleapis.com/v1beta/openai'}, 'AI_BASE_URL_INVALID'),
    ({**ENV, 'AI_EXTRA_BODY': '{"messages": []}'}, 'AI_EXTRA_BODY_INVALID')])
def test_provider_rejects_unsafe_configuration(environ, code):
    with pytest.raises(text_llm.TextModelError) as caught:
        text_llm.TextModelProvider('gemini', first=True, environ=environ)
    assert caught.value.code == code


def test_local_model_needs_no_key_but_only_on_a_private_address():
    local = text_llm.TextModelProvider('ollama', first=True, environ={'AI_MODEL': 'qwen2.5:7b'})
    assert local.local is True and local.base == 'http://127.0.0.1:11434/v1'
    with pytest.raises(text_llm.TextModelError):
        text_llm.TextModelProvider('custom', first=True,
                                   environ={'AI_MODEL': 'm', 'AI_BASE_URL': 'http://example.com/v1'})


def test_ai_is_off_by_default():
    assert text_llm.ai_mode({}) == 'off'


# --- field profiles and addresses (MG1) ------------------------------------------

def test_every_profile_field_is_asked_for_and_accepted_by_the_contract():
    from services.extraction.contracts.accounting_contract import FIELD_KEYS
    for kind, profile in structure.PROFILE_FIELDS.items():
        assert set(profile) <= set(structure.MODEL_FIELDS), kind
        assert all(key in FIELD_KEYS for key in profile.values()), kind
    assert {'seller_registration_id', 'seller_sst_id', 'seller_tin', 'buyer_address'} <= set(structure.PROFILE_FIELDS['invoice'])
    assert {'note_reason', 'seller_address'} <= set(structure.PROFILE_FIELDS['credit_note'])
    assert 'rounding' in structure.PROFILE_FIELDS['receipt'] and 'rounding' in structure.MONEY_FIELDS
    assert 'buyer_name' in structure.PROFILE_FIELDS['bill']
    # The printed bill number is stored under the key standardisation and the evaluator read first.
    assert structure.PROFILE_FIELDS['bill']['document_number'] == 'bill_number'
    assert FIELD_KEYS['bill_number'] == 'bill_number'


def address_rows():
    texts = [('KEDAI BESI MAJU SDN BHD', 60, 40), ('No. 75, Jalan Cempaka 12/2', 60, 64),
             ('Taman Bayu Indah', 60, 88), ('93350 Kuching, Sarawak', 60, 112),
             ('No. Syarikat: 202301045678   SST: W10-1808-32000123   TIN: C2584563200', 60, 136),
             ('TAX INVOICE', 700, 40), ('Invoice No:', 700, 64), ('INV-2026-0042', 900, 64),
             ('Total', 700, 300), ('RM 135.00', 900, 300)]
    return [row(index, text, left, top) for index, (text, left, top) in enumerate(texts)]


def test_address_keeps_the_printed_line_breaks():
    answer = {'document_type': 'invoice', 'fields': {
        'seller_name': {'value': 'KEDAI BESI MAJU SDN BHD', 'raw': 'KEDAI BESI MAJU SDN BHD'},
        'seller_address': {'value': 'No. 75, Jalan Cempaka 12/2, Taman Bayu Indah, 93350 Kuching, Sarawak', 'raw': None},
        'seller_registration_id': {'value': '202301045678', 'raw': '202301045678'},
        'seller_sst_id': {'value': 'W10-1808-32000123', 'raw': 'W10-1808-32000123'},
        'seller_tin': {'value': 'C2584563200', 'raw': 'C2584563200'},
        'document_number': {'value': 'INV-2026-0042', 'raw': 'INV-2026-0042'},
        'total_amount': {'value': '135.00', 'raw': 'RM 135.00'}}}
    result = structure.structure_document(document(address_rows()), providers=[FakeProvider(answer)])
    fields = result['fields']
    assert fields['supplier_address']['value'] == 'No. 75, Jalan Cempaka 12/2\nTaman Bayu Indah\n93350 Kuching, Sarawak'
    assert [e['region_order'] for e in fields['supplier_address']['evidence']] == [1, 2, 3]
    assert (fields['supplier_registration_id']['value'], fields['supplier_sst_id']['value'], fields['supplier_tin']['value']) \
        == ('202301045678', 'W10-1808-32000123', 'C2584563200')
    public = {'schema_version': '0.1.0-proposed', 'mode': 'module2_public_result', 'document_id': 'doc-a',
              'document_type': result['document_type'], 'status': 'candidate_for_review', 'review_required': True,
              'accounting_approval_performed': False,
              'safety': {'ai_candidate_auto_accepted': False, 'automatic_accounting_approval': False},
              'fields': fields, 'line_items': [], 'issues': [], 'routing': {'decision': 'vlm_candidate'},
              'provenance': {'service_run_id': 'lite-x'}}
    stored = {row['field_key']: row for row in map_m2_result(public, 'doc-a')['fields']}
    assert stored['supplier_address']['normalized_value'].count('\n') == 2


def test_address_parts_printed_together_stay_on_one_line():
    # "No. 2" alone also occurs inside another row ("No. 23 ..."); it must stay with its street.
    texts = [('ACME SDN BHD', 60, 40), ('No. 23', 400, 52),
             ('No. 2, Jalan Meranti 11/9', 60, 64), ('Taman Merak Baru', 60, 88), ('47301 Petaling Jaya, Selangor', 60, 112)]
    rows = [row(index, text, left, top) for index, (text, left, top) in enumerate(texts)]
    answer = {'document_type': 'invoice', 'fields': {'seller_address': {
        'value': 'No. 2, Jalan Meranti 11/9, Taman Merak Baru, 47301 Petaling Jaya, Selangor', 'raw': None}}}
    result = structure.structure_document(document(rows), providers=[FakeProvider(answer)])
    assert result['fields']['supplier_address']['value'] == \
        'No. 2, Jalan Meranti 11/9\nTaman Merak Baru\n47301 Petaling Jaya, Selangor'


def test_an_address_with_an_unprinted_part_is_not_reshaped_or_invented():
    answer = {'document_type': 'invoice', 'fields': {
        'seller_address': {'value': 'No. 75, Jalan Cempaka 12/2, Taman Tidak Wujud, 93350 Kuching', 'raw': None}}}
    result = structure.structure_document(document(address_rows()), providers=[FakeProvider(answer)])
    assert result['fields']['supplier_address']['state'] == 'missing'
    assert any(issue['field'] == 'seller_address' for issue in result['issues'] if issue['code'] == 'AI_VALUE_DISCARDED')


@pytest.mark.parametrize('printed,expected', [
    ('6 204,19', '6204.19'), ('$ 6 204,19', '6204.19'), ('1.234,56', '1234.56'), ('564,02', '564.02'),
    ('6 204.19', '6204.19'), ('1,234.56', '1234.56'), ('1,234', '1234.00'), ('RM 1,031.50', '1031.50'),
    ('-0.02', '-0.02'), ('(12.50)', '-12.50'), ('1.234', None)])
def test_amounts_with_a_decimal_comma_or_spaced_thousands_are_read_correctly(printed, expected):
    # Before, "6 204,19" was read as 620419.00.
    assert structure.parse_amount(printed) == expected


def test_amount_printed_with_a_decimal_comma_is_found_in_the_source():
    rows = [row(0, 'TAX INVOICE', 60, 40), row(1, 'Invoice no: 51109338', 60, 80),
            row(2, 'VAT [%] | Net worth | VAT | Gross worth', 60, 300), row(3, 'Total $ 5 640,17 $ 564,02 $ 6 204,19', 60, 340)]
    answer = {'document_type': 'invoice', 'fields': {
        'document_number': {'value': '51109338', 'raw': '51109338'},
        'tax_amount': {'value': '564.02', 'raw': '$ 564,02'},
        'total_amount': {'value': '6204.19', 'raw': '$ 6 204,19'}}}
    result = structure.structure_document(document(rows), providers=[FakeProvider(answer)])
    assert result['fields']['tax_amount']['value'] == '564.02'
    assert result['fields']['total_amount']['value'] == '6204.19'
    assert result['fields']['total_amount']['evidence'][0]['region_order'] == 3


@pytest.mark.parametrize('printed,expected', [
    ('01/07/2026-31-07-2026', '01/07/2026 - 31-07-2026'),
    ('01/02/2026 -28-02-2026', '01/02/2026 - 28-02-2026'),
    ('01/03/2026 hingga 31/03/2026', '01/03/2026 - 31/03/2026'),
])
def test_billing_period_keeps_both_printed_dates_with_a_normal_joint(printed, expected):
    rows = [row(0, 'ELEKTRIK SERI PELANGI SDN BHD', 60, 40), row(1, 'Tempoh Bil:', 320, 120), row(2, printed, 455, 120)]
    answer = {'document_type': 'bill', 'fields': {'billing_period': {'value': printed, 'raw': printed}}}
    result = structure.structure_document(document(rows), providers=[FakeProvider(answer)])
    assert result['fields']['billing_period']['value'] == expected
    assert result['fields']['billing_period']['evidence'][0]['text'] == printed


def test_billing_period_that_is_not_printed_is_discarded():
    rows = [row(0, 'Tempoh Bil:', 320, 120), row(1, '01/07/2026 - 31/07/2026', 455, 120)]
    answer = {'document_type': 'bill', 'fields': {'billing_period': {'value': '01/08/2026 - 31/08/2026', 'raw': None}}}
    result = structure.structure_document(document(rows), providers=[FakeProvider(answer)])
    assert result['fields']['billing_period']['state'] == 'missing'


# --- DEFAULT_CURRENCY --------------------------------------------------------

def run_without_printed_currency(tmp_path, monkeypatch, answer=None):
    monkeypatch.setenv('AI_EXTRACTION', 'text')
    monkeypatch.setattr(pipeline, 'load_pages', lambda path: document(invoice_rows()))
    answer = answer or good_answer(currency={'value': None, 'raw': None})
    result = pipeline.execute_lite_extraction('doc-c', source_file(tmp_path), tmp_path, providers=[FakeProvider(answer)])
    public = json.loads(Path(result['public_result_path']).read_text(encoding='utf-8')) if result['public_result_path'] else None
    return result, public


def test_currency_stays_missing_when_no_default_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv('DEFAULT_CURRENCY', raising=False)
    _, public = run_without_printed_currency(tmp_path, monkeypatch)
    assert public['fields']['currency']['state'] == 'missing' and public['fields']['currency']['value'] is None
    assert not any(issue['code'] == 'DEFAULT_CURRENCY_APPLIED' for issue in public['issues'])


def test_default_currency_fills_an_unprinted_currency_visibly(tmp_path, monkeypatch):
    monkeypatch.setenv('DEFAULT_CURRENCY', 'myr')
    _, public = run_without_printed_currency(tmp_path, monkeypatch)
    cell = public['fields']['currency']
    assert (cell['state'], cell['value'], cell['method'], cell['model_name']) == ('resolved', 'MYR', 'PARSER', None)
    assert cell['review_required'] is True and cell['evidence'] == []
    assert [issue['field'] for issue in public['issues'] if issue['code'] == 'DEFAULT_CURRENCY_APPLIED'] == ['currency']
    assert public['review_required'] is True and public['safety']['automatic_accounting_approval'] is False
    mapped = map_m2_result(public, 'doc-c')
    currency = next(row for row in mapped['fields'] if row['field_key'] == 'currency')
    assert currency['normalized_value'] == 'MYR' and currency['extraction_method'] == 'PARSER'


def test_a_printed_currency_wins_over_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv('DEFAULT_CURRENCY', 'USD')
    _, public = run_without_printed_currency(tmp_path, monkeypatch, answer=good_answer())
    assert public['fields']['currency']['value'] == 'MYR'
    assert not any(issue['code'] == 'DEFAULT_CURRENCY_APPLIED' for issue in public['issues'])


def test_default_currency_is_not_added_to_an_unclassified_document(tmp_path, monkeypatch):
    monkeypatch.setenv('DEFAULT_CURRENCY', 'MYR')
    _, public = run_without_printed_currency(tmp_path, monkeypatch, answer={'document_type': 'unknown'})
    assert public['fields'] == {}


def test_an_invalid_default_currency_is_a_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setenv('DEFAULT_CURRENCY', 'RINGGIT')
    result, _ = run_without_printed_currency(tmp_path, monkeypatch)
    assert result['execution_status'] == 'INFRASTRUCTURE_ERROR' and result['error'] == 'INVALID_LITE_CONFIGURATION'


# --- spreadsheets ------------------------------------------------------------

@pytest.mark.parametrize('name,data', [('sheet.csv', b'a,b\n1,2\n'), ('sheet.xlsx', b'PK\x03\x04not-a-real-workbook')])
def test_spreadsheets_fail_as_a_controlled_unsupported_format(tmp_path, monkeypatch, name, data):
    from services.extraction.workflow import execution
    monkeypatch.delenv('EXTRACTION_ENGINE', raising=False)
    monkeypatch.setattr(execution, 'DocumentIntelligenceService',
                        lambda *a, **k: pytest.fail('The lite engine must not build the Docling service'))
    path = tmp_path / name
    path.write_bytes(data)
    result = execution.execute_document_extraction('doc-s', path, tmp_path)
    assert result['execution_status'] == 'STRUCTURAL_FAILURE'
    assert result['error'] == result['error_code'] == 'UNSUPPORTED_EXTRACTION_FORMAT'
    assert result['public_result_path'] is None and result['review_required'] is True
    assert not (tmp_path / '.extraction-tmp').exists()


# --- AI Assist on a lite run ----------------------------------------------------

def test_ai_assist_on_a_lite_run_returns_the_controlled_message(tmp_path, monkeypatch):
    import main
    from services.queue import jobs
    from services.routes.documents import routes
    run_id = 'lite-' + 'a' * 32
    (tmp_path / 'runs' / run_id).mkdir(parents=True)
    (tmp_path / 'runs' / run_id / 'public-result.json').write_text('{}', encoding='utf-8')
    source = tmp_path / 'scan.jpg'
    source.write_bytes(b'\xff\xd8\xff')
    inserts = []

    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=()):
            if sql.lstrip().startswith('INSERT INTO audit_logs'):
                inserts.append(params)
                return Rows([])
            if 'FROM documents d' in sql:
                return Rows([{'id': 'd1', 'status': 'exception', 'kind': 'invoice', 'content_revision': 1, 'tenant_id': 't1'}])
            if 'count(*)' in sql:
                return Rows([{'n': 0}])
            if 'FROM raw_extractions' in sql:
                return Rows([{'service_run_id': run_id}])
            if 'FROM field_predictions' in sql:
                return Rows([])
            raise AssertionError('Unexpected SQL: ' + sql)

    monkeypatch.setenv('QWEN_RECOVERY_MODE', 'manual')
    monkeypatch.setattr(routes, 'connect', lambda: Db())
    monkeypatch.setattr(routes, 'require_workspace_user',
                        lambda *a, **k: {'id': 'u1', 'tenant_id': 't1', 'role': 'accountant', 'name': 'Reviewer'})
    monkeypatch.setattr(jobs, 'BACKEND_ROOT', tmp_path)
    monkeypatch.setattr(jobs, 'resolve_document_from_database', lambda document_id: source)
    result = routes.request_ai_assist('d1', main.AIAssistInput(fields=None), object())
    assert result['status'] == 'EXTRACTION_ARTIFACTS_NOT_ON_THIS_MACHINE'
    assert result['fields'] == [] and 'AI Assist' in result['message']
    assert len(inserts) == 1


# --- OCR_WARM_ON_START ----------------------------------------------------------

def test_ocr_warm_up_is_off_by_default_and_runs_when_enabled(monkeypatch):
    import main
    calls = []
    monkeypatch.setattr(pages, 'warm_engine', lambda: calls.append(1) or 0.5)
    monkeypatch.delenv('OCR_WARM_ON_START', raising=False)
    assert main.warm_ocr_on_start() is None and calls == []
    monkeypatch.setenv('OCR_WARM_ON_START', 'true')
    assert main.warm_ocr_on_start() == 0.5 and calls == [1]


def test_ocr_warm_up_failure_never_stops_startup(monkeypatch):
    import main

    def broken():
        raise RuntimeError('weights missing')
    monkeypatch.setattr(pages, 'warm_engine', broken)
    monkeypatch.setenv('OCR_WARM_ON_START', 'true')
    assert main.warm_ocr_on_start() is None


def test_lifespan_warms_ocr_after_starting_the_supervisor(monkeypatch):
    import asyncio
    import main
    from services.queue import inline
    order = []
    monkeypatch.setattr(main, 'check_schema_compatibility', lambda: None)
    monkeypatch.setattr(inline, 'start_supervisor', lambda: order.append('supervisor'))
    monkeypatch.setattr(inline, 'stop_supervisor', lambda: order.append('stopped'))
    monkeypatch.setattr(pages, 'warm_engine', lambda: order.append('warm') or 0.1)
    monkeypatch.setenv('OCR_WARM_ON_START', 'true')
    monkeypatch.delenv('QUEUE_BACKEND', raising=False)

    async def start_and_stop():
        async with main.lifespan(main.app):
            order.append('serving')
    asyncio.run(start_and_stop())
    assert order == ['supervisor', 'warm', 'serving', 'stopped']
