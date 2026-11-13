"""Lite extraction: one trusted source file -> the existing M2 public result.

    PDF text layer / local CPU OCR  ->  positioned rows (never leave this server)
    AI_EXTRACTION=text              ->  masked text to a text model, values verified locally
    AI off or unavailable           ->  the existing rule mapper for single images, else review

Returns the same execution metadata as the other extraction paths, so claim
fencing, persistence, validation and review are unchanged. Nothing is approved.
"""
from __future__ import annotations

import json
import logging
import os
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from services.extraction.ai import text_llm
from services.extraction.contracts.accounting_contract import CURRENCY_MINOR_UNITS, map_m2_result
from services.extraction.lite.pages import PageError, load_pages

logger = logging.getLogger(__name__)
VERSION = 'lite-extraction/0.1'
STRUCTURAL_CODES = {'UNSUPPORTED_EXTRACTION_FORMAT', 'SOURCE_SIZE_LIMIT', 'SOURCE_FORMAT_MISMATCH',
                    'IMAGE_PIXEL_LIMIT', 'INVALID_OR_UNSAFE_IMAGE', 'INVALID_OR_UNREADABLE_PDF',
                    'OCR_NO_TEXT', 'OCR_RESULT_LIMIT'}


def _write(path, data):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)


def _empty_classification(reason):
    return {'classifier_version': VERSION, 'method': 'none', 'value': 'unknown', 'status': 'unknown',
            'reason': reason, 'candidates': [], 'unsupported_headings': [], 'review_required': True}


def default_currency(environ=None):
    """DEFAULT_CURRENCY (unset by default): the currency to assume when none is printed."""
    env = os.environ if environ is None else environ
    code = env.get('DEFAULT_CURRENCY', '').strip().upper()
    if not code:
        return None
    if code not in CURRENCY_MINOR_UNITS:
        raise PageError('INVALID_LITE_CONFIGURATION')
    return code


def _apply_default_currency(fields, issues, code):
    """Fill a currency that is not printed with the configured default, visibly.

    Only when the document was classified (it has a field profile) and neither the
    model nor the rules produced any currency value. A printed currency always wins.
    """
    if not code or not fields or (fields.get('currency') or {}).get('state', 'missing') != 'missing':
        return fields
    issues.append({'code': 'DEFAULT_CURRENCY_APPLIED', 'field': 'currency',
                   'detail': f'No currency is printed; the configured default {code} was used. Confirm it.'})
    return {**fields, 'currency': {
        'value': code, 'state': 'resolved', 'method': 'PARSER', 'review_required': True, 'raw_text': None,
        'evidence': [], 'candidates': [], 'confidence': None, 'model_name': None, 'model_version': VERSION}}


def _rule_mapping(document, document_id):
    """The established OCR rule mapper, for one OCR-read image page only."""
    if document['page_count'] != 1 or document['pages'][0]['source'] != 'ocr' or not document['rows']:
        return None
    if len(document['rows']) > 1000:
        return None
    from services.extraction.ocr.ocr_documents import map_rapidocr_document
    page = document['pages'][0]
    artifact = {'status': 'PASS', 'method': 'direct RapidOCR; not Docling pipeline', 'annotation_input': False,
                'device': 'cpu', 'image_sha256': document['source_sha256'], 'image_size': dict(page['size']),
                'rows': [{'order': row['order'], 'text': row['text'],
                          'box_polygon_pixels': [list(point) for point in row['box_polygon_pixels']],
                          'engine_score': row['engine_score']} for row in document['rows']]}
    try:
        mapped = map_rapidocr_document(artifact, document_id, document['versions'])
    except ValueError:
        return None
    mapped.pop('receipt_observations', None)
    mapped['provenance'].pop('native_ocr_result', None)
    return mapped


def _public_result(document_id, run_name, document, *, classification, fields, lines, issues, status,
                   routing, ai_record, prefill, rule_provenance=None, seconds=None):
    provenance = dict(rule_provenance or {})
    provenance.update({
        'adapter_version': VERSION, 'source_kind': 'lite_rows', 'service_run_id': run_name,
        'source_sha256': document['source_sha256'], 'source_format': document['source_format'],
        'model_versions': deepcopy(document['versions']),
        'pages': [{'page': page['page'], 'read_from': page['source'], 'size': page['size'],
                   'rows': len(page['rows']), 'seconds': page['seconds'], 'transform': page['transform']}
                  for page in document['pages']],
        'pages_truncated': document['truncated'], 'read_seconds': document['seconds'],
        'ai': ai_record, 'total_seconds': seconds})
    return {'schema_version': '0.1.0-proposed', 'mode': 'module2_public_result', 'document_id': document_id,
            'document_type': classification, 'status': status, 'review_required': True,
            'accounting_approval_performed': False,
            # A printed value placed in a field for the reviewer is not an approval:
            # the document still needs human review and an authorised decision.
            'safety': {'ai_candidate_auto_accepted': False, 'automatic_accounting_approval': False,
                       'source_verified_prefill': bool(prefill)},
            'fields': fields, 'line_items': lines, 'issues': issues,
            'routing': {'decision': routing}, 'provenance': provenance}


def execute_lite_extraction(document_id, file_path, project_root=None, *, providers=None):
    """Extract one resolver-authorised document. Never raises; returns execution metadata."""
    base = {'document_id': document_id, 'public_result_path': None, 'service_run_id': None,
            'review_required': True, 'accounting_approval_performed': False}
    started = time.perf_counter()
    run = None
    try:
        root = (Path(project_root) if project_root else Path(__file__).resolve().parents[3]).resolve()
        path = Path(file_path).resolve()
        if not path.is_file():
            raise PageError('SOURCE_NOT_FOUND')
        fallback_currency = default_currency()  # Checked before any work so a bad setting fails fast.
        run = root / 'runs' / ('lite-' + uuid4().hex)
        run.mkdir(parents=True, mode=0o700, exist_ok=False)
        document = load_pages(path)
        if not document['rows']:
            raise PageError('OCR_NO_TEXT')
        # Full OCR stays in this private run folder; the public artifact carries
        # only field evidence.
        _write(run / 'ocr.private.json', {'version': VERSION, 'document_id': document_id, **document})

        issues = [{'code': 'OCR_REQUIRES_REVIEW',
                   'detail': 'Values are read from the document and require source review; no approval is inferred.'}]
        if document['truncated']:
            issues.append({'code': 'PAGE_LIMIT', 'detail': 'Only the first pages were read; review the rest manually.'})
        ai_record = {'mode': 'off', 'image_sent': False}
        structured = None
        mode = text_llm.ai_mode()
        if mode == 'text':
            from services.extraction.lite.structure import structure_document
            try:
                structured = structure_document(document, providers=providers)
                ai_record = structured['ai']
            except text_llm.TextModelError as error:
                # The document is unaffected: it falls back to rules and human review.
                logger.warning('Text model unavailable for %s: %s', document_id, error.code)
                ai_record = {'mode': 'text', 'image_sent': False, 'error': error.code,
                             'failed': list(getattr(error, 'failures', []))}
                issues.append({'code': 'AI_UNAVAILABLE',
                               'detail': 'The text model could not be used (' + error.code + '). Review this document manually.'})

        prefill = False
        rule_provenance = None
        if structured is not None:
            classification, fields, lines = structured['document_type'], structured['fields'], structured['line_items']
            issues.extend(structured['issues'])
            status, routing = 'candidate_for_review', 'vlm_candidate'
            prefill = ai_record.get('prefill_policy') == 'verified'
        else:
            mapped = _rule_mapping(document, document_id)
            status = 'ai_attempt_failed_needs_review' if ai_record.get('error') else 'human_review_required'
            routing = 'human_review'
            if mapped is not None:
                classification, fields, lines = mapped['document_type'], mapped['fields'], mapped['line_items']
                issues.extend(issue for issue in mapped['issues'] if issue.get('code') != 'OCR_REQUIRES_REVIEW')
                rule_provenance = mapped['provenance']
            else:
                classification, fields, lines = _empty_classification('NO_AUTOMATIC_MAPPING'), {}, []
        fields = _apply_default_currency(fields, issues, fallback_currency)

        def build(fields, lines, issues, status):
            return _public_result(document_id, run.name, document, classification=classification, fields=fields,
                                  lines=lines, issues=issues, status=status, routing=routing, ai_record=ai_record,
                                  prefill=prefill, rule_provenance=rule_provenance,
                                  seconds=round(time.perf_counter() - started, 3))

        public = build(fields, lines, issues, status)
        try:
            map_m2_result(public, document_id)
        except ValueError as error:
            if structured is None:
                raise
            # A model answer must never fail the job: keep the read, drop the answer.
            logger.warning('Model answer rejected by the field contract for %s: %s', document_id, error)
            ai_record = {**ai_record, 'error': 'AI_RESULT_REJECTED_BY_CONTRACT'}
            issues = issues + [{'code': 'AI_RESULT_REJECTED',
                                'detail': 'The model answer did not fit the accounting field contract and was not used.'}]
            prefill = False
            public = build({}, [], issues, 'ai_attempt_failed_needs_review')
            map_m2_result(public, document_id)
        public_path = run / 'public-result.json'
        _write(public_path, public)
        return {**base, 'execution_status': 'EXTRACTED', 'extraction_status': public['status'],
                'public_result_path': str(public_path), 'service_run_id': run.name, 'error': None}
    except PageError as error:
        code = error.code
    except Exception:
        logger.exception('Lite extraction failed for %s', document_id)
        code = 'LITE_EXTRACTION_FAILED'
    if run is not None and run.is_dir():
        try:
            _write(run / 'failure.json', {'error_code': code, 'review_required': True})
        except OSError:
            pass
    return {**base, 'execution_status': 'STRUCTURAL_FAILURE' if code in STRUCTURAL_CODES else 'INFRASTRUCTURE_ERROR',
            'error': code, 'error_code': code}
