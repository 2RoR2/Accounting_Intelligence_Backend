"""Proposed header-only routing policy. Makes decisions, never inference calls."""
import argparse
import hashlib
import json
import os
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
    'invoice': ('supplier_name', 'supplier_address', 'invoice_number', 'invoice_date', 'due_date', 'tax_amount', 'total_amount'),
    'bill': ('issuer', 'provider_address', 'account_number', 'bill_reference', 'bill_date', 'due_date', 'amount_due', 'tax_amount', 'total_amount'),
    'receipt': ('merchant', 'merchant_address', 'receipt_number', 'transaction_date', 'transaction_time', 'tax_amount', 'total_amount'),
}
# Where a label is not expected or OCR lost it, these show the source has something to examine.
_STRUCTURAL_CUES = {
    'date': r'(?<!\d)\d{1,4}[/.-](?:\d{1,2}|[A-Za-z]{3,9})[/.-]\d{2,4}(?!\d)|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b',
    'due': r'\b(?:due|deadline|tarikh\s+akhir)\b',
    'time': r'\b\d{1,2}:\d{2}(?::\d{2})?\b|\b\d{1,2}\s*[AP]\.?M\b',  # OCR can drop the minutes: "03:29PM" read as "03PM".
    'total_amount': r'\b(?:total|jumlah|grand|amount)\b',
    'amount_due': r'\b(?:due|payable|balance|amount)\b',
    'tax_amount': r'\b(?:tax|vat|gst|sst)\b',
    'reference': r'\b(?:no\.?|number|ref(?:erence)?|inv|invoice|receipt|bill|doc)\b|#',
    'account_number': r'\b(?:account|acct|a/c)\b',
}
_PARTY_OF_ADDRESS = {'supplier_address': 'supplier_name', 'provider_address': 'issuer', 'merchant_address': 'merchant'}
RECOVERY_ISSUES = {
    'HUMAN_REVIEW_REQUIRED', 'UNMAPPED_FIELD', 'CONFLICTING_VALUES', 'INCOMPLETE_EVIDENCE',
    'AMBIGUOUS_LABEL_BOUNDARY', 'FORMAT_REQUIRES_REVIEW', 'AMOUNT_REQUIRES_REVIEW',
    'LAYOUT_LINE_RECOVERY', 'LINE_REPRESENTATION_DISAGREEMENT', 'LINE_FIELD_REQUIRES_REVIEW',
    'NO_MAPPED_LINE_ITEMS', 'UNSUPPORTED_TABLE', 'AMBIGUOUS_CURRENCY_SYMBOL',
}


# What validation needs before it can run on this profile. Routing only: Module 3
# owns the real requirements. No score here is a probability of correctness.
PROFILE_REQUIRED = {
    'invoice': ('supplier_name', 'invoice_date', 'currency', 'total_amount'),
    'bill': ('issuer', 'bill_date', 'currency', ('amount_due', 'total_amount')),
    'receipt': ('merchant', 'transaction_date', 'currency', 'total_amount'),
}
ASSESSMENT_POLICY_VERSION = 'extraction-assessment/2'
_EXPLANATIONS = {
    'PARSER_SUFFICIENT_FOR_VALIDATION': 'The parser found every value validation needs. A person still reviews it.',
    'DETERMINISTIC_RECOVERY_NEEDED': 'A value is printed in a format the parser does not support yet.',
    'QWEN_RECOVERY_ELIGIBLE': 'Fields are unresolved and the source shows something to examine, so the local model can suggest candidates.',
    'HUMAN_REVIEW_REQUIRED': 'A required value is unresolved and only a person can settle it.',
    'EXTRACTION_FAILED_OR_INFRA_UNAVAILABLE': 'Extraction or its local services failed; the document needs attention.',
}
ASSESSMENT_DECISIONS = ('PARSER_SUFFICIENT_FOR_VALIDATION', 'DETERMINISTIC_RECOVERY_NEEDED',
                        'QWEN_RECOVERY_ELIGIBLE', 'HUMAN_REVIEW_REQUIRED',
                        'EXTRACTION_FAILED_OR_INFRA_UNAVAILABLE')


SECOND_OPINION_POLICY = 'risk-based-second-opinion/1'
MAX_SECOND_OPINIONS = 2
_IDENTITY_FIELDS = {'supplier_name', 'supplier_address', 'issuer', 'provider_address', 'merchant', 'merchant_address'}
_DATE_AMOUNT_FIELDS = {'invoice_date', 'bill_date', 'transaction_date', 'total_amount', 'amount_due'}
# OCR glyph slips ("&" read as "G", words run together) pass every format rule. Identity
# fields drive supplier matching and duplicate checks, so they are re-read below a near-
# perfect line score; dates and amounts only when the line score is clearly lower.
# Thresholds were chosen on the development split and need held-out confirmation.
IDENTITY_OCR_SCORE, DATE_AMOUNT_OCR_SCORE = 0.99, 0.95


def second_opinion_mode():
    """off (default) or risk. Selection only; eligibility in the plan is always computed."""
    return 'risk' if os.environ.get('QWEN_SECOND_OPINION_MODE', '').strip().lower() == 'risk' else 'off'


def second_opinion_risks(name, field):
    """Why a value the parser already resolved deserves a second reading; empty if none.

    Uses only the extraction itself, never labels or the model.
    """
    if field.state != 'resolved' or name not in _IDENTITY_FIELDS | _DATE_AMOUNT_FIELDS:
        return []
    risks = []
    ocr = [e for e in field.evidence if isinstance(e, dict) and e.get('from_ocr') is True]
    scores = [e['ocr_score'] for e in ocr if isinstance(e.get('ocr_score'), (int, float)) and not isinstance(e.get('ocr_score'), bool)]
    floor = IDENTITY_OCR_SCORE if name in _IDENTITY_FIELDS else DATE_AMOUNT_OCR_SCORE
    if scores and min(scores) < floor:  # No score means no evidence of risk, not risk.
        risks.append('OCR_LINE_SCORE_BELOW_' + str(floor))
    if len({c.value for c in field.candidates if c.value is not None}) > 1:
        risks.append('PRINTED_CANDIDATES_DISAGREE')
    return risks


def profile_document(document):
    """How the source reached us, for routing reasons. Counts only, never source text."""
    lines = document.get('m2_text_lines') or []
    ocr = [line for line in lines if line.get('from_ocr') is True]
    scores = [line['ocr_score'] for line in ocr if isinstance(line.get('ocr_score'), (int, float))]
    kind = 'no_text' if not lines else 'digital_text' if not ocr else 'ocr_image' if len(ocr) == len(lines) else 'mixed'
    mean = sum(scores) / len(scores) if scores else None
    low = kind == 'no_text' or len(lines) < 5 or (mean is not None and mean < .75)
    return {'text_source': kind, 'page_count': len(document.get('pages') or {}), 'text_lines': len(lines),
            'ocr_engine_score_mean': round(mean, 3) if mean is not None else None,
            'low_quality_text': low, 'score_scope': 'text_recognition_only'}


def assess_extraction(payload, project_root=None, *, allow_vlm_inference=False,
                      source_access_authorized=False, source_path=None, document=None):
    """Explain what should happen next, field by field. Never calls a model.

    Human review stays mandatory whatever this returns. decide_route remains the
    stored document route; this adds the reasons that route cannot express.
    """
    result = ClassifiedExtractionResult.model_validate(payload)
    kind = result.document_type.value
    fields, unresolved = {}, []
    classified = result.document_type.status == 'classified' and kind in PROFILE_REQUIRED
    for entry in PROFILE_REQUIRED.get(kind, ()):
        names = (entry,) if isinstance(entry, str) else entry
        cells = [(name, result.fields[name]) for name in names if name in result.fields]
        if any(cell.state == 'resolved' for _, cell in cells):
            continue
        for name, cell in cells or [(names[0], None)]:
            state = cell.state if cell else 'missing'
            printed = [c for c in (cell.candidates if cell else []) if c.value is not None]
            if name == 'currency':
                reason = 'CURRENCY_SYMBOL_DOES_NOT_IDENTIFY_CURRENCY' if cell and cell.candidates else 'NO_PRINTED_CURRENCY'
                status = 'human_review'
            elif state == 'conflict' or len({c.value for c in printed}) > 1:
                reason, status = 'CONFLICTING_SOURCE_VALUES', 'human_review'
            elif name.endswith('_date') and printed:
                # The digits are printed; only a person can settle their order.
                reason, status = 'DATE_ORDER_NOT_RECOVERABLE_BY_MODEL', 'human_review'
            elif printed:
                reason, status = 'PRINTED_VALUE_FORMAT_UNSUPPORTED', 'deterministic_rule_gap'
            else:
                reason, status = 'REQUIRED_VALUE_NOT_MAPPED', 'human_review'
            fields[name] = {'status': status, 'reason': reason, 'state': state}
            unresolved.append(name)
    recovery = []
    if allow_vlm_inference is True and source_access_authorized is True and classified:
        plan = plan_field_recovery(payload, project_root, source_access_authorized=True,
                                   source_path=source_path, model_configuration={'model': 'eligibility-check'})
        for name, cell in plan['fields'].items():
            current = result.fields[name]
            # A printed numeric date needs a person to settle its order; a model
            # guess could not be verified. Protected parser values are never asked for.
            if (not cell['eligible'] or current.state == 'resolved'
                    or (name.endswith('_date') and any(c.value for c in current.candidates))):
                continue
            recovery.append(name)
            fields.setdefault(name, {'state': current.state})
            fields[name].update(status='qwen_candidate_eligible', reason=cell['reason'])
        # Fields validation needs first, within the execution's attempt budget.
        recovery.sort(key=lambda name: name not in unresolved)
        recovery = recovery[:plan['max_attempts_per_execution']]
        if second_opinion_mode() == 'risk':
            # Parser values with a stated risk, read again in the same request. Advice only:
            # a disagreement keeps the parser value and needs a person's decision.
            second = [name for name, cell in plan['fields'].items() if cell['eligible'] and cell.get('second_opinion')]
            second.sort(key=lambda name: (name not in _IDENTITY_FIELDS, name))
            for name in second[:plan['max_second_opinions_per_execution']]:
                recovery.append(name)
                fields.setdefault(name, {'state': result.fields[name].state})
                fields[name].update(status='qwen_second_opinion', reason='SECOND_OPINION:' + ','.join(plan['fields'][name]['second_opinion']))
    if not classified:
        decision, reasons = 'HUMAN_REVIEW_REQUIRED', ['DOCUMENT_TYPE_REQUIRES_REVIEW']
    elif recovery:
        decision, reasons = 'QWEN_RECOVERY_ELIGIBLE', ['SOURCE_CUED_FIELD_UNRESOLVED']
    elif any(cell['status'] == 'deterministic_rule_gap' for cell in fields.values()):
        decision, reasons = 'DETERMINISTIC_RECOVERY_NEEDED', ['PRINTED_VALUE_FORMAT_UNSUPPORTED']
    elif unresolved:
        decision, reasons = 'HUMAN_REVIEW_REQUIRED', ['REQUIRED_VALUE_UNRESOLVED']
    else:
        decision, reasons = 'PARSER_SUFFICIENT_FOR_VALIDATION', []
    profile = profile_document(document) if document is not None else None
    if profile and profile['page_count'] > 1 and recovery:
        decision, reasons, recovery = 'HUMAN_REVIEW_REQUIRED', ['MULTI_PAGE_RECOVERY_NOT_SUPPORTED'], []
    if profile and profile['low_quality_text']:
        reasons = reasons + ['LOW_QUALITY_SOURCE_TEXT']
    return {'policy_version': ASSESSMENT_POLICY_VERSION, 'decision': decision, 'reasons': reasons,
            'explanation': _EXPLANATIONS[decision], 'document_profile': profile,
            'fields': fields, 'recovery_fields': recovery, 'review_required': True, 'ai_call_performed': False}


def structural_cues(name, positioned, result):
    """Source refs showing a value could be present though no label was mapped.

    This only allows the question to be asked. A model answer must still pass
    source verification, and an absent value stays null.
    """
    def top_lines(limit=4):
        first = [(item, box) for item, box in positioned if item['prov'][0]['page_no'] == 1]
        ordered = sorted(first, key=lambda pair: (pair[1][1], pair[1][0]))
        return [item['self_ref'] for item, _ in ordered if re.search(r'[A-Za-z]{2}', item.get('text', ''))][:limit]
    if name in {'issuer', 'merchant'}:  # Printed as a letterhead. Invoice parties carry role labels.
        return top_lines()
    if name in _PARTY_OF_ADDRESS:
        party = result.fields.get(_PARTY_OF_ADDRESS[name])
        if party is None or party.state != 'resolved':
            return []
        # Only when address-like text sits under the party, not merely because it is empty.
        refs = {e.get('source_ref') for e in party.evidence if e.get('source_ref')}
        anchors = [box for item, box in positioned if item.get('self_ref') in refs]
        found = []
        for item, box in positioned:
            text = item.get('text', '').strip()
            if (item.get('self_ref') in refs or ':' in text or not re.search(r'\d', text) or not re.search(r'[A-Za-z]{2}', text)):
                continue
            if any(0 <= box[1] - a[3] <= (a[3] - a[1]) * 8 and min(box[2], a[2]) > max(box[0], a[0]) for a in anchors):
                found.append(item['self_ref'])
        return sorted(refs | set(found)) if found else []
    key = ('due' if name == 'due_date' else 'date' if name.endswith('_date') else 'time' if name.endswith('_time') else
           'reference' if name in {'invoice_number', 'bill_reference', 'receipt_number'} else name)
    pattern = _STRUCTURAL_CUES.get(key)
    if pattern is None:
        return []
    return sorted({item['self_ref'] for item, _ in positioned if re.search(pattern, item.get('text', ''), re.I)})[:6]


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
        risks = second_opinion_risks(name, field) if name in RECOVERY_FIELDS.get(kind, ()) else []
        if field.state == 'resolved' and not weak and not risks:
            reason = 'RESOLVED_PARSER_VALUE_PROTECTED'
        elif name not in RECOVERY_FIELDS.get(kind, ()):
            reason = 'OUTSIDE_RECOVERY_SCOPE'
        elif field.state not in {'missing', 'ambiguous', 'conflict'} and not weak and not risks:
            reason = 'UNSUPPORTED_OR_UNREADABLE_STATE'
        elif blockers:
            reason = blockers[0]
        cues = (printed_cues(name, field) or structural_cues(name, positioned, result)) if reason is None else []
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
                        'parser_value_protected': True, 'comparison_only': field.state == 'resolved',
                        **({'second_opinion': risks} if risks and not weak and reason is None else {})}
    # An address needs a cue showing where it is printed. When its party is itself being
    # recovered, that party's letterhead is the cue, so the address is read in the same
    # request. Verification still anchors an address to parser party evidence, so without
    # a resolved party the answer can only be an unverified hint for the reviewer.
    for address, party in _PARTY_OF_ADDRESS.items():
        cell, owner = fields.get(address), fields.get(party)
        if (cell and owner and owner['eligible'] and not owner.get('second_opinion') and not cell['eligible']
                and cell['reason'] == 'NO_VERIFIABLE_PRINTED_FIELD_CUE'):
            cell.update(eligible=True, reason=result.fields[address].state.upper(),
                        source_refs=list(owner['source_refs']), cue='party_recovered_in_same_request')
    return {'policy_version': 'field-recovery-plan/1', 'document_id': result.document_id,
            'document_review_required': True, 'inference_authorized': False, 'ai_call_performed': False,
            'source_verified': source_hash is not None, 'source_sha256': source_hash,
            'blocking_reasons': blockers, 'configuration_sha256': configuration_hash,
            'max_attempts_per_execution': 3, 'max_second_opinions_per_execution': MAX_SECOND_OPINIONS,
            'max_attempts_per_field_issue': 1, 'second_opinion_policy': SECOND_OPINION_POLICY, 'fields': fields}


def reserve_recovery_attempt(plan, field_name, attempts):
    """Execution-local reservation only; never calls AI or grants permission."""
    field = plan['fields'].get(field_name)
    if not field or not field['eligible'] or field['issue_id'] in attempts or not budget_allows(plan, [field_name], attempts):
        return False
    attempts[field['issue_id']] = field['attempt_id']
    return True


def budget_allows(plan, field_names, attempts):
    """Recovery fields and second opinions each have their own per-execution allowance."""
    second = {f['issue_id'] for f in plan['fields'].values() if f.get('second_opinion')}
    used_second = sum(1 for issue in attempts if issue in second)
    used_main = len(attempts) - used_second
    asked_second = sum(1 for name in field_names if plan['fields'].get(name, {}).get('second_opinion'))
    asked_main = len(field_names) - asked_second
    return (used_main + asked_main <= plan['max_attempts_per_execution']
            and used_second + asked_second <= plan.get('max_second_opinions_per_execution', 0))

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


