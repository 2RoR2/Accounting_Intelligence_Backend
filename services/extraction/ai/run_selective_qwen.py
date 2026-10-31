"""Single-page selective field attempts; never overwrite parser predictions."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from uuid import uuid4

from services.extraction.classification.classify_document import classify_document
from services.extraction.ai.qwen_provider import (
    OllamaQwenProvider, prepare_request, validate_response, parse_json, validate_fields, REQUEST_OPTIONS,
)
from services.extraction.classification.route_extraction import decide_route, plan_field_recovery, reserve_recovery_attempt

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def checked_path(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Input is outside the SSD project: ' + str(path))
    return path

def verified_pdf(extraction, root, *, source_path=None, source_access_authorized=False):
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
    if 'source_transformation' in provenance:
        from services.extraction.workflow.execution import verify_source_transformation
        return verify_source_transformation(extraction, root, source_path=source_path,
                                             source_access_authorized=source_access_authorized)
    # A trusted caller may locate byte-identical content after staging expires.
    # Relocation never permits a different PDF or a reconstructed image wrapper.
    pdf_path = checked_path(source_path if source_path is not None else parser_run['source'], root)
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


def recovery_context(extraction, plan, fields, root):
    """Nearby OCR retains role qualifiers; it is context, never VLM evidence."""
    from services.extraction.classification.classify_document import context_pages
    doc = parse_json(checked_path(extraction['provenance']['source_document_json'], root).read_bytes())
    positioned = context_pages(doc).get(1, [])
    items = [x for x, _ in sorted(positioned, key=lambda pair: (pair[1][1], pair[1][0]))]
    anchors = {ref for field in fields for ref in plan['fields'][field]['source_refs']}
    indexes = {j for i, x in enumerate(items) if x.get('self_ref') in anchors
               for j in range(max(0, i - 2), min(len(items), i + 3))}
    context = {'page': 1, 'fields': {name: {k: extraction['fields'][name][k]
                for k in ('state', 'raw_text', 'value', 'candidates')} for name in fields},
               'nearby_text': [{'source_ref': items[i].get('self_ref'), 'text': items[i].get('text', '')}
                               for i in sorted(indexes)]}
    if len(json.dumps(context, ensure_ascii=False)) > 6000:
        raise ValueError('Selective source context exceeds the bounded request; human review required')
    return context

def run_attempt(source, root, provider_factory=OllamaQwenProvider, renderer=render_single_page,
                *, source_path=None, source_access_authorized=False, selected_fields=None,
                inference_permission=False, attempts=None, output_parent=None):
    source = checked_path(source, root)
    parent = checked_path(output_parent if output_parent is not None else Path(root) / 'runs', root)
    out = parent / ('selective-' + uuid4().hex)
    out.mkdir(parents=True, exist_ok=False)
    record = {'status': 'preparing', 'created_at': datetime.now(timezone.utc).isoformat(),
              'review_required': True, 'ai_request_attempted': False, 'ai_response_received': False,
              'source_result_path': str(source), 'endpoint': None, 'model': None,
              'scope': 'Single-page selected profile fields; unverified candidates, no automatic approval'}
    started = time.perf_counter()
    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    try:
        raw = source.read_bytes()
        record['source_result_sha256'] = sha256(raw)
        extraction = json.loads(raw)
        explicit_selection = selected_fields is not None
        decision = decide_route(extraction, allow_vlm_inference=inference_permission)
        scoped = explicit_selection or decision['decision'] == 'vlm_candidate'
        save('routing.json', decision)
        record['decision'] = decision['decision']
        record['document_id'] = extraction['document_id']
        record['extraction_run_id'] = extraction['extraction_run_id']
        profile = extraction['document_type']['value']
        fields = list(selected_fields) if explicit_selection else decision['requested_fields']
        if scoped:
            record.update(recovery_scope='profile_fields/1', profile=profile,
                          inference_permission=inference_permission, source_access_authorized=source_access_authorized)
            if inference_permission is not True or source_access_authorized is not True:
                raise ValueError('Explicit inference permission and independent source authorization are required')
            if not isinstance(attempts, dict):
                raise ValueError('A shared execution-local attempt context is required')
            validate_fields(fields, profile)
        if not scoped and decision['decision'] != 'vlm_candidate':
            record['status'] = 'skipped_' + decision['decision']
        else:
            pdf_path, pdf_raw = verified_pdf(extraction, root, source_path=source_path,
                                             source_access_authorized=source_access_authorized)
            image_path = out / 'page-1.png'
            dimensions = renderer(pdf_raw, image_path)
            image_raw = image_path.read_bytes()
            record.update(source_pdf=str(pdf_path), source_pdf_sha256=sha256(pdf_raw),
                          page=1, render_scale=1.5, image=str(image_path),
                          image_sha256=sha256(image_raw), dimensions=dimensions,
                          requested_fields=fields)
            provider = provider_factory()
            record.update(endpoint=provider.base, model=provider.model, timeout_seconds=provider.timeout)
            context = None
            if scoped:
                configuration = {'model': provider.model, 'options': dict(REQUEST_OPTIONS), 'render_scale': 1.5,
                                 'profile': profile, 'request_contract': 'profile_fields/1'}
                plan = plan_field_recovery(extraction, root, source_path=source_path,
                    source_access_authorized=source_access_authorized, model_configuration=configuration)
                if (not plan['source_verified'] or plan['blocking_reasons']
                        or any(not plan['fields'].get(name, {}).get('eligible') for name in fields)):
                    raise ValueError('Selected fields are not source-verified recovery eligible')
                if (len(attempts) + len(fields) > plan['max_attempts_per_execution']
                        or any(plan['fields'][name]['issue_id'] in attempts for name in fields)):
                    raise ValueError('Recovery attempt already reserved or execution budget exhausted')
                context = recovery_context(extraction, plan, fields, root)
                for name in fields:
                    if not reserve_recovery_attempt(plan, name, attempts):
                        raise ValueError('Recovery reservation failed; no automatic retry')
                plan.update(profile=profile, selected_fields=fields, source_result_sha256=record['source_result_sha256'])
                save('recovery-plan.json', plan)
                record.update(model_configuration=configuration, configuration_sha256=plan['configuration_sha256'],
                              recovery_plan_sha256=sha256((out / 'recovery-plan.json').read_bytes()))
            payload = prepare_request(image_raw, fields, model=provider.model, profile=profile, source_context=context)
            save('request-settings.json', {k: payload[k] for k in ('model', 'format', 'stream', 'keep_alive', 'options')})
            record['request_settings_sha256'] = sha256((out / 'request-settings.json').read_bytes())
            (out / 'prompt.txt').write_text(payload['messages'][0]['content'], encoding='utf-8')
            record['prompt_sha256'] = sha256((out / 'prompt.txt').read_bytes())
            record['server_version'] = provider.metadata('/api/version')
            record['model_inventory'] = provider.metadata('/api/tags')
            inventory = record['model_inventory']
            if (not isinstance(inventory, dict) or not isinstance(inventory.get('models'), list)
                    or not any(isinstance(m, dict) and provider.model in {m.get('name'), m.get('model')}
                               for m in inventory['models'])):
                raise ValueError('Configured model is not available in Ollama inventory; no download attempted')
            before = provider.metadata('/api/ps')
            record['models_before'] = before
            request_start = time.perf_counter()
            record['ai_request_attempted'] = True
            try:
                response_text = provider.chat(payload)
            finally:
                record['request_seconds'] = round(time.perf_counter() - request_start, 3)
            record['ai_response_received'] = True
            (out / 'raw_response.txt').write_bytes(response_text.encode('utf-8'))
            record['raw_response_sha256'] = sha256(response_text.encode('utf-8'))
            response = parse_json(response_text)
            save('raw_response.json', response)
            prediction = validate_response(response, fields, model=provider.model, profile=profile)
            save('prediction.json', prediction)
            record['prediction_sha256'] = sha256((out / 'prediction.json').read_bytes())
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
    root = Path(__file__).resolve().parents[3]
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


