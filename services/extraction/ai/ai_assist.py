"""Accountant-requested second pass by the local model. Never changes a stored value.

Shares the automatic path's response validation, source verification and arbitration.
The result is advice: applying a value goes through the normal authorised correction.
"""
import hashlib
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import uuid4

from services.extraction.ai.qwen_provider import (
    OllamaQwenProvider, model_digest, parse_json, prepare_request, validate_response_fields,
)
from services.extraction.ai.run_selective_qwen import checked_path, render_single_page, verified_pdf
from services.extraction.classification.reconcile_selective import (
    MODEL_FAILURE, arbitrate, check_candidate_against_source, REJECTED_SOURCE_CHECKS,
)
from services.extraction.classification.route_extraction import RECOVERY_FIELDS
from services.extraction.contracts.accounting_contract import FIELD_KEYS

VERSION = 'ai-assist/1'
MAX_FIELDS = 5
_gpu_slot = threading.BoundedSemaphore(1)  # One inference at a time from this process.


class AssistUnavailable(Exception):
    """A plain explanation for the accountant; the document is unaffected."""
    def __init__(self, code, message):
        super().__init__(code)
        self.code, self.message = code, message


def _load_extraction(service_run_id, root):
    summary_path = Path(root) / 'runs' / str(service_run_id) / 'service-result.json'
    if not re.fullmatch(r'[A-Za-z0-9_-]+', str(service_run_id)) or not summary_path.is_file():
        raise AssistUnavailable('EXTRACTION_ARTIFACTS_NOT_ON_THIS_MACHINE',
                                'The extraction files for this document are not on this machine, so AI Assist cannot examine it here.')
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    path = checked_path(summary['artifacts']['classified_extraction'], root)
    return json.loads(path.read_bytes())


def _explain(outcome, status, rejection):
    return {
        'PARSER_PRESERVED': 'The model agrees with, or did not contradict, the current value. Nothing changes.',
        'SOURCE_VERIFIED_CANDIDATE': 'The value is printed and sits beside a matching label or in the expected position. Confirm it against the document before saving.',
        'UNVERIFIED_MODEL_SUGGESTION': 'The text is printed, but nothing ties it to this field. Treat it as a hint only.',
        'PARSER_MODEL_CONFLICT': 'The model read something different from the current value. The current value is kept; check the document.',
        'NO_RECOVERY': ('The model suggested a value that is not printed on the document, so it was rejected.' if status == 'not_found_in_source'
                        else 'The date is printed but its day and month order cannot be confirmed, so it was rejected.' if status == 'date_order_not_verifiable'
                        else 'The model returned a placeholder such as "N/A", not a value for this field, so it was rejected.' if status == 'placeholder_not_a_value'
                        else 'The model found no value it could support for this field.'),
        'MODEL_FAILURE': 'The model answered in an unusable format: ' + str(rejection),
    }[outcome]


def run_ai_assist(service_run_id, source_path, requested, *, root, current_values=None, provider_factory=OllamaQwenProvider,
                  renderer=render_single_page):
    """Examine fields of one already authorised document and return advice per field.

    current_values holds the saved values, including human corrections. They are
    compared and reported, never altered.
    """
    extraction = _load_extraction(service_run_id, root)
    classification = extraction['document_type']
    profile = classification['value']
    if classification['status'] != 'classified' or profile not in RECOVERY_FIELDS:
        raise AssistUnavailable('DOCUMENT_TYPE_UNRESOLVED', 'AI Assist needs a classified invoice, bill or receipt. Confirm the document type first.')
    allowed = RECOVERY_FIELDS[profile]
    fields = extraction['fields']
    unknown = [name for name in (requested or []) if name not in allowed]
    if unknown:
        raise AssistUnavailable('FIELD_NOT_SUPPORTED', 'AI Assist does not cover: ' + ', '.join(unknown) + '. Currency is never requested from the model.')
    if requested:
        selected = list(dict.fromkeys(requested))
    else:
        selected = [name for name in allowed if fields.get(name, {}).get('state') != 'resolved'
                    and not (name.endswith('_date') and any(c.get('value') for c in fields.get(name, {}).get('candidates', [])))]
    selected = selected[:MAX_FIELDS]
    if not selected:
        return {'version': VERSION, 'status': 'NOTHING_TO_EXAMINE', 'profile': profile, 'fields': [],
                'message': 'Every field AI Assist covers is already resolved. Choose a field to get a second opinion.'}
    try:
        _, pdf_bytes = verified_pdf(extraction, root, source_path=source_path, source_access_authorized=True)
        document = parse_json(checked_path(extraction['provenance']['source_document_json'], root).read_bytes())
    except (OSError, ValueError, KeyError) as error:
        raise AssistUnavailable('SOURCE_UNAVAILABLE', 'The original file could not be verified against the extraction, so it was not sent to the model.') from error
    if not _gpu_slot.acquire(blocking=False):
        raise AssistUnavailable('MODEL_BUSY', 'The local model is busy with another request. Try again shortly.')
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix='ai-assist-', dir=Path(root) / 'runs') as folder:
            image = Path(folder) / 'page-1.png'
            try:
                renderer(pdf_bytes, image)
            except ValueError as error:
                raise AssistUnavailable('MULTI_PAGE_NOT_SUPPORTED', 'AI Assist currently examines single-page documents only.') from error
            try:
                provider = provider_factory()
                inventory = provider.metadata('/api/tags')
                digest = model_digest(inventory, provider.model)
                if digest is None and not any(isinstance(m, dict) and provider.model in {m.get('name'), m.get('model')} for m in inventory.get('models', [])):
                    raise AssistUnavailable('MODEL_NOT_INSTALLED', 'The local model is not installed on this machine. Nothing was downloaded.')
                # No parser hints: the model must read the page itself.
                payload = prepare_request(image.read_bytes(), selected, model=provider.model, profile=profile, source_context={})
                response = parse_json(provider.chat(payload))
                usable, rejected = validate_response_fields(response, selected, model=provider.model, profile=profile)
            except AssistUnavailable:
                raise
            except ValueError as error:
                return {'version': VERSION, 'status': 'MODEL_FAILURE', 'profile': profile, 'fields': [
                    {'field': name, 'stored_key': FIELD_KEYS.get(name, name), 'outcome': MODEL_FAILURE, 'candidate': None, 'parser_value': fields.get(name, {}).get('value'),
                     'explanation': 'The model answered in an unusable format. Nothing changed.'} for name in selected],
                    'seconds': round(time.perf_counter() - started, 2), 'message': 'The model answer could not be used. Nothing changed.'}
            except Exception as error:
                raise AssistUnavailable('MODEL_UNAVAILABLE', 'The local model is not responding. The document is unaffected; continue reviewing it manually.') from error
    finally:
        _gpu_slot.release()
    results = []
    for name in selected:
        cell = fields.get(name, {})
        saved = (current_values or {}).get(name)
        baseline = saved if saved is not None else cell.get('value')
        candidate = usable.get(name)
        check = {'status': 'not_checked', 'source_refs': [], 'evidence': []}
        if candidate is not None:
            check = check_candidate_against_source(name, candidate, document, fields)
            candidate = check.get('printed_value') or candidate
        outcome = arbitrate(baseline, candidate, check['status'], rejected.get(name))
        visible = None if check['status'] in REJECTED_SOURCE_CHECKS else candidate
        results.append({'field': name, 'stored_key': FIELD_KEYS.get(name, name), 'outcome': outcome, 'parser_value': cell.get('value'), 'parser_state': cell.get('state'),
                        'current_value': saved, 'candidate': visible, 'source_check': check['status'],
                        'evidence': [{key: e.get(key) for key in ('page', 'text', 'bbox', 'source_ref', 'page_size', 'coordinate_units')} for e in check['evidence']],
                        'request_reason': 'second opinion on a resolved value' if cell.get('state') == 'resolved' else ('field state: ' + str(cell.get('state'))),
                        'rejected_value_hash': (hashlib.sha256(candidate.encode()).hexdigest()[:12] if candidate and visible is None else None),
                        'explanation': _explain(outcome, check['status'], rejected.get(name))})
    return {'version': VERSION, 'status': 'COMPLETED', 'attempt_id': uuid4().hex, 'profile': profile, 'model': provider.model,
            'model_digest': digest, 'page': 1, 'seconds': round(time.perf_counter() - started, 2), 'fields': results,
            'review_required': True, 'accounting_approval_performed': False}
