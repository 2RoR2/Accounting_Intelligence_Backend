"""Synthetic single-page selective attempt; never overwrites parser predictions."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from uuid import uuid4

from classify_document import classify_document
from qwen_provider import BASE, MODEL, OllamaQwenProvider, prepare_request, validate_response
from route_extraction import decide_route

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def checked_path(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Input is outside the SSD project: ' + str(path))
    return path

def verified_pdf(extraction, root):
    """Bind the rendered bytes to the recorded parser input and classification."""
    provenance = extraction['provenance']
    doc_path = checked_path(provenance['source_document_json'], root)
    doc_raw = doc_path.read_bytes()
    classification = extraction['document_type']
    if doc_path != checked_path(classification['source_document_json'], root):
        raise ValueError('Classification source differs from extraction source')
    if sha256(doc_raw) != provenance['source_json_sha256'] or sha256(doc_raw) != classification['source_json_sha256']:
        raise ValueError('Docling source hash mismatch')
    actual_rules = {k: v for k, v in classification.items()
                    if k not in {'source_document_json', 'source_json_sha256'}}
    if classify_document(json.loads(doc_raw)) != actual_rules:
        raise ValueError('Classification no longer matches source and rules')
    parser_run = provenance['parser_run']
    if parser_run['status'] != 'success':
        raise ValueError('Expected a successful parser run')
    pdf_path = checked_path(parser_run['source'], root)
    pdf_raw = pdf_path.read_bytes()
    if sha256(pdf_raw) != parser_run['source_sha256']:
        raise ValueError('PDF changed since parser extraction')
    return pdf_path, pdf_raw

def render_single_page(pdf_bytes, output):
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(pdf_bytes) as pdf:
        if len(pdf) != 1:
            raise ValueError('This selective experiment supports exactly one PDF page')
        page = pdf[0]
        try:
            bitmap = page.render(scale=1.5)
            try:
                image = bitmap.to_pil()
                try:
                    dimensions = list(image.size)
                    image.save(output)
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()
    return dimensions

def run_attempt(source, root, provider_factory=OllamaQwenProvider, renderer=render_single_page):
    source = checked_path(source, root)
    out = Path(root).resolve() / 'runs' / ('selective-' + uuid4().hex)
    out.mkdir(parents=True, exist_ok=False)
    record = {'status': 'preparing', 'created_at': datetime.now(timezone.utc).isoformat(),
              'review_required': True, 'ai_request_attempted': False, 'ai_response_received': False,
              'source_result_path': str(source), 'endpoint': BASE, 'model': MODEL,
              'scope': 'Synthetic single-page header attempt; no reconciliation or automatic approval'}
    started = time.perf_counter()
    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    try:
        raw = source.read_bytes()
        record['source_result_sha256'] = sha256(raw)
        extraction = json.loads(raw)
        decision = decide_route(extraction)
        save('routing.json', decision)
        record['decision'] = decision['decision']
        record['document_id'] = extraction['document_id']
        if decision['decision'] != 'vlm_candidate':
            record['status'] = 'skipped_' + decision['decision']
        else:
            pdf_path, pdf_raw = verified_pdf(extraction, root)
            image_path = out / 'page-1.png'
            dimensions = renderer(pdf_raw, image_path)
            image_raw = image_path.read_bytes()
            fields = decision['requested_fields']
            record.update(source_pdf=str(pdf_path), source_pdf_sha256=sha256(pdf_raw),
                          page=1, render_scale=1.5, image=str(image_path),
                          image_sha256=sha256(image_raw), dimensions=dimensions,
                          requested_fields=fields, timeout_seconds=120)
            payload = prepare_request(image_raw, fields)
            save('request-settings.json', {k: payload[k] for k in ('model', 'format', 'stream', 'keep_alive', 'options')})
            (out / 'prompt.txt').write_text(payload['messages'][0]['content'], encoding='utf-8')
            provider = provider_factory()
            record['server_version'] = provider.metadata('/api/version')
            record['model_inventory'] = provider.metadata('/api/tags')
            before = provider.metadata('/api/ps')
            record['models_before'] = before
            request_start = time.perf_counter()
            record['ai_request_attempted'] = True
            try:
                response_text = provider.chat(payload)
            finally:
                record['request_seconds'] = round(time.perf_counter() - request_start, 3)
            record['ai_response_received'] = True
            (out / 'raw_response.txt').write_text(response_text, encoding='utf-8')
            response = json.loads(response_text)
            save('raw_response.json', response)
            prediction = validate_response(response, fields)
            save('prediction.json', prediction)
            record['status'] = 'response_structure_valid_needs_review'
            record['server_timing_ns'] = {k: response.get(k) for k in
                ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration')}
            try:
                record['models_after'] = provider.metadata('/api/ps')
            except Exception as error:
                record['post_request_metadata_error'] = str(error)
    except Exception as error:
        record['status'] = 'failed'
        record['error_type'] = type(error).__name__
        record['error'] = str(error)
        if isinstance(error, HTTPError):
            try:
                (out / 'http-error.txt').write_bytes(error.read())
            except Exception:
                pass
    finally:
        record['attempt_seconds'] = round(time.perf_counter() - started, 3)
        save('run.json', record)
    return out, record

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('extraction_result', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    out, record = run_attempt(args.extraction_result, root)
    print('Status:', record['status'])
    print('Decision:', record.get('decision'))
    print('AI request attempted:', record['ai_request_attempted'])
    print('Saved:', out)
    if record['status'] == 'failed':
        print('Error:', record['error'])
        raise SystemExit(1)

if __name__ == '__main__':
    main()


