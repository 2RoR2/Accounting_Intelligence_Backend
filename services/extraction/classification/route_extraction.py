"""Proposed header-only routing policy. Makes decisions, never inference calls."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4
from services.extraction.parsing.combine_extraction import ClassifiedExtractionResult

# Development policy, not statutory invoice requirements or team-approved rules.
REQUIRED_HEADERS = ('supplier_name', 'invoice_number', 'invoice_date', 'currency', 'total_amount')
POLICY_VERSION = '0.1.0-proposed'

# Planning scope only: these names already belong to the existing profiles.
# Currency, tax, settlement and line-item operations are deliberately not proposed.
RECOVERY_FIELDS = {
    'invoice': ('supplier_name', 'supplier_address', 'invoice_number', 'invoice_date', 'total_amount'),
    'bill': ('issuer', 'provider_address', 'account_number', 'bill_reference', 'bill_date', 'due_date', 'amount_due', 'total_amount'),
    'receipt': ('merchant', 'merchant_address', 'receipt_number', 'transaction_date', 'total_amount'),
}
RECOVERY_ISSUES = {
    'HUMAN_REVIEW_REQUIRED', 'UNMAPPED_FIELD', 'CONFLICTING_VALUES', 'INCOMPLETE_EVIDENCE',
    'AMBIGUOUS_LABEL_BOUNDARY', 'FORMAT_REQUIRES_REVIEW', 'AMOUNT_REQUIRES_REVIEW',
    'LAYOUT_LINE_RECOVERY', 'LINE_REPRESENTATION_DISAGREEMENT', 'LINE_FIELD_REQUIRES_REVIEW',
    'NO_MAPPED_LINE_ITEMS', 'UNSUPPORTED_TABLE',
}


def plan_field_recovery(payload, project_root, *, source_access_authorized=False,
                        source_path=None, model_configuration, prior_attempts=None):
    """Read-only proposals, independent of the execution route and permission.

    The caller must establish access before granting source examination. A plan
    neither grants inference permission nor reserves an attempt or changes fields.
    """
    from services.extraction.classification.classify_document import context_pages
    from services.extraction.layout.layout_headers import ALIASES
    from services.extraction.parsing.map_invoice import INVOICE_LABELS, BILL_LABELS, RECEIPT_LABELS
    # Reuse the selective runner's source/hash/classification checks, not a
    # parallel verifier. Lazy import avoids its import of decide_route at startup.
    from services.extraction.ai.run_selective_qwen import verified_pdf, checked_path

    result = ClassifiedExtractionResult.model_validate(payload)
    data = result.model_dump()
    if not isinstance(model_configuration, dict) or not isinstance(model_configuration.get('model'), str) or not model_configuration['model'].strip():
        raise ValueError('An explicit model/configuration identity is required')
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    configuration_hash = digest(model_configuration)
    kind = result.document_type.value
    blockers = []
    if source_access_authorized is not True:
        blockers.append('SOURCE_ACCESS_NOT_AUTHORIZED')
    if kind not in RECOVERY_FIELDS or result.document_type.status != 'classified':
        blockers.append('DOCUMENT_TYPE_REQUIRES_REVIEW')
    if any(i.get('code') not in RECOVERY_ISSUES for i in result.issues):
        blockers.append('UNHANDLED_OR_NONEXTRACTION_ISSUE')
    source_hash = None
    positioned = []
    if not blockers:
        try:
            _, source_bytes = verified_pdf(data, project_root, source_path=source_path,
                                          source_access_authorized=source_access_authorized)
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            doc = json.loads(checked_path(data['provenance']['source_document_json'], project_root).read_bytes())
            positioned = [(item, box) for items in context_pages(doc).values() for item, box in items]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            blockers.append('SOURCE_IDENTITY_UNVERIFIED_OR_UNAVAILABLE')
    refs = {}
    for item, box in positioned:
        refs.setdefault(item.get('self_ref'), []).append((item, box))
    labels = {**{'invoice': INVOICE_LABELS, 'bill': BILL_LABELS, 'receipt': RECEIPT_LABELS}.get(kind, {}),
              **ALIASES.get(kind, {})}
    absent = re.compile(r'(?:n/?a|none|not\s+(?:applicable|provided|stated)|[-—])', re.I)

    def readable(value):
        return (isinstance(value, str) and bool(value.strip()) and not absent.fullmatch(value.strip())
                and not (':' in value and absent.fullmatch(value.rsplit(':', 1)[1].strip())))

    def witnessed(evidence):
        matches = refs.get(evidence.get('source_ref'), [])
        if len(matches) != 1:
            return False
        item, box = matches[0]; prov = item['prov'][0]
        if evidence.get('page') != prov['page_no']:
            return False
        if 'text' in evidence and evidence['text'] != item.get('text'):
            return False
        expected = dict(zip(('l', 't', 'r', 'b'), box), coord_origin='TOPLEFT')
        return (evidence.get('bbox_original') == prov['bbox'] or evidence.get('bbox') == expected)

    def printed_cues(name, field):
        evidence = [e for candidate in field.candidates if readable(candidate.raw_text)
                    for e in candidate.evidence if witnessed(e)]
        evidence += [e for e in field.evidence if witnessed(e) and readable(field.raw_text)]
        if evidence:
            return sorted({e['source_ref'] for e in evidence})
        # A missing wrapper alone is not a trigger. Require a recognizable
        # field label and nonblank printed value in its immediate source region.
        cues = []
        for item, box in positioned:
            text = item.get('text', '').strip(); label, colon, inline = text.partition(':')
            if labels.get(label.lower().strip()) != name:
                continue
            if colon and readable(inline):
                cues.append(item['self_ref']); continue
            if colon and inline.strip():  # Explicit N/A/none: no recovery proposal.
                continue
            height = box[3] - box[1]; page = item['prov'][0]['page_no']
            for other, b in positioned:
                if other is item or other['prov'][0]['page_no'] != page:
                    continue
                raw = other.get('text', '').strip()
                if not readable(raw) or raw.partition(':')[0].lower().strip() in labels:
                    continue
                aligned = abs((box[1] + box[3] - b[1] - b[3]) / 2) <= min(height, b[3] - b[1]) * .65
                if (aligned and 0 <= b[0] - box[2] <= height * 8
                        or 0 <= b[1] - box[3] <= height * 1.5 and abs(b[0] - box[0]) <= height):
                    cues.extend((item['self_ref'], other['self_ref']))
        return sorted(set(cues))

    fields = {}
    for name, field in result.fields.items():
        weak = (field.state == 'resolved' and any(i.get('code') == 'INCOMPLETE_EVIDENCE'
                and i.get('detail', '').split(':', 1)[0] == name for i in result.issues))
        reason = None
        if field.state == 'resolved' and not weak:
            reason = 'RESOLVED_PARSER_VALUE_PROTECTED'
        elif name not in RECOVERY_FIELDS.get(kind, ()):
            reason = 'OUTSIDE_RECOVERY_SCOPE'
        elif field.state not in {'missing', 'ambiguous', 'conflict'} and not weak:
            reason = 'UNSUPPORTED_OR_UNREADABLE_STATE'
        elif blockers:
            reason = blockers[0]
        cues = printed_cues(name, field) if reason is None else []
        if reason is None and not cues:
            reason = 'NO_VERIFIABLE_PRINTED_FIELD_CUE'
        issue = {'source_sha256': source_hash, 'source_json_sha256': result.provenance.get('source_json_sha256'),
                 'profile': kind, 'field': name, 'state': field.state, 'raw_text': field.raw_text,
                 'value': field.value, 'evidence': field.evidence,
                 'candidates': [c.model_dump() for c in field.candidates],
                 'issues': [i for i in result.issues if i.get('detail', '').split(':', 1)[0] == name]}
        issue_id = digest(issue)
        attempt_id = digest({'issue_id': issue_id, 'configuration_sha256': configuration_hash,
                             'purpose': 'source-field-disambiguation/1'})
        if reason is None and issue_id in (prior_attempts or {}):
            reason = 'ATTEMPT_ALREADY_RESERVED'
        fields[name] = {'eligible': reason is None, 'reason': reason or ('INCOMPLETE_EVIDENCE' if weak else field.state.upper()),
                        'source_refs': cues, 'issue_id': issue_id, 'attempt_id': attempt_id,
                        'parser_value_protected': True, 'comparison_only': field.state == 'resolved'}
    return {'policy_version': 'field-recovery-plan/1', 'document_id': result.document_id,
            'document_review_required': True, 'inference_authorized': False, 'ai_call_performed': False,
            'source_verified': source_hash is not None, 'source_sha256': source_hash,
            'blocking_reasons': blockers, 'configuration_sha256': configuration_hash,
            'max_attempts_per_execution': 3, 'max_attempts_per_field_issue': 1, 'fields': fields}


def reserve_recovery_attempt(plan, field_name, attempts):
    """Execution-local reservation only; never calls AI or grants permission."""
    field = plan['fields'].get(field_name)
    if (not field or not field['eligible'] or field['issue_id'] in attempts
            or len(attempts) >= plan['max_attempts_per_execution']):
        return False
    attempts[field['issue_id']] = field['attempt_id']
    return True

def decide_route(payload, allow_vlm_inference: bool = True):
    if not isinstance(allow_vlm_inference, bool):
        raise ValueError('Inference permission must be an explicit boolean')
    result = ClassifiedExtractionResult.model_validate(payload)
    reasons, targets, blockers = [], [], []

    def block(code, field=None):
        reason = {'code': code, 'field': field}
        if reason not in blockers:
            blockers.append(reason)

    if result.document_type.value != 'invoice' or result.document_type.status != 'classified':
        block('DOCUMENT_TYPE_REQUIRES_REVIEW')

    all_fields = list(result.fields.items())
    for index, row in enumerate(result.line_items):
        all_fields.extend((f'line_items[{index}].{k}', v) for k, v in row.items())
    for name, field in all_fields:
        if field.state == 'resolved':
            # Presence check only: geometry correctness still needs separate QA.
            if not field.evidence or any(
                not e.get('source_ref') or not e.get('page') or e.get('bbox') is None
                for e in field.evidence
            ):
                block('INCOMPLETE_EVIDENCE', name)
        elif name in REQUIRED_HEADERS and field.state in {'missing', 'ambiguous'}:
            targets.append(name)
            reasons.append({'code': 'MISSING_REQUIRED_HEADER' if field.state == 'missing'
                            else 'AMBIGUOUS_REQUIRED_HEADER', 'field': name})
        else:
            block('UNRESOLVED_FIELD_REQUIRES_REVIEW', name)

    # Keep issue handling conservative. Arithmetic/format/evidence/table problems
    # are not silently treated as questions that a header-only VLM can repair.
    for issue in result.issues:
        code = issue.get('code')
        name = issue.get('detail', '').split(':', 1)[0]
        if code == 'HUMAN_REVIEW_REQUIRED':
            continue
        if code == 'UNMAPPED_FIELD' and name in targets and result.fields[name].state == 'missing':
            continue
        if code == 'CONFLICTING_VALUES' and name in targets and result.fields[name].state == 'ambiguous':
            continue
        block('SOURCE_ISSUE_REQUIRES_REVIEW:' + str(code), name or None)
    if not result.line_items:
        block('NO_SUPPORTED_LINE_ITEMS')

    if targets and not allow_vlm_inference:
        block('VLM_INFERENCE_DISABLED')

    if blockers:
        decision = 'human_review'
    elif targets:
        decision = 'vlm_candidate'
    else:
        decision = 'parser_only'
    return {
        'policy_version': POLICY_VERSION,
        'allow_vlm_inference': allow_vlm_inference,
        'document_id': result.document_id,
        'extraction_run_id': result.extraction_run_id,
        'decision': decision,
        'required_headers': list(REQUIRED_HEADERS),
        'requested_fields': sorted(targets) if decision == 'vlm_candidate' else [],
        'candidate_reasons': reasons,
        'blocking_reasons': blockers,
        'review_required': True,
        'ai_call_performed': False,
        'scope': 'Header routing recommendation only; no provider, availability or image checks performed',
        'missing_value_rule': 'If the source does not contain the value, preserve null. Never invent it.',
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('extraction_result', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    source = args.extraction_result.resolve()
    if not source.is_relative_to(root):
        parser.error('Input must be within the SSD project')
    try:
        raw = source.read_bytes()
        decision = decide_route(json.loads(raw))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        parser.error(str(error))
    decision['source_result_path'] = str(source)
    decision['source_result_sha256'] = hashlib.sha256(raw).hexdigest()
    out = root / 'runs' / ('routing-' + uuid4().hex)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'decision.json').write_text(json.dumps(decision, indent=2) + '\n', encoding='utf-8')
    print('Document:', decision['document_id'])
    print('Decision:', decision['decision'])
    print('Requested fields:', decision['requested_fields'])
    print('Candidate reasons:', decision['candidate_reasons'])
    print('Blocking reasons:', decision['blocking_reasons'])
    print('AI call performed: False | Review required: True')
    print('Saved:', out)

if __name__ == '__main__':
    main()


