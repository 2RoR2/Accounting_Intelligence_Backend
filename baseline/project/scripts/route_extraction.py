"""Proposed header-only routing policy. Makes decisions, never inference calls."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from combine_extraction import ClassifiedExtractionResult

# Development policy, not statutory invoice requirements or team-approved rules.
REQUIRED_HEADERS = ('supplier_name', 'invoice_number', 'invoice_date', 'currency', 'total_amount')
POLICY_VERSION = '0.1.0-proposed'

def decide_route(payload):
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

    if blockers:
        decision = 'human_review'
    elif targets:
        decision = 'vlm_candidate'
    else:
        decision = 'parser_only'
    return {
        'policy_version': POLICY_VERSION,
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


