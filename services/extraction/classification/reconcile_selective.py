#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path

from services.extraction.parsing.combine_extraction import ClassifiedExtractionResult
from services.extraction.ai.qwen_provider import (
    parse_json, validate_response, validate_response_fields, prepare_request, validate_fields,
    validate_prediction as validate_provider_prediction, TIME_TEXT, clock_time,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs"

RECONCILIATION_SCHEMA_VERSION = "0.1.0-proposed"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return parse_json(f.read())


def validate_requested_fields(fields, profile=None):
    """Validate stored routing scope independently of the provider."""
    require(isinstance(fields, list) and bool(fields), "Expected a nonempty requested-field list.")
    validate_fields(fields, profile)
    require(len(set(fields)) == len(fields), "Requested fields must be unique.")


def validate_prediction(prediction, requested_fields, profile=None):
    validate_requested_fields(requested_fields, profile)
    require(isinstance(prediction, dict) and set(prediction) == set(requested_fields),
            "Prediction must contain exactly the requested fields.")
    require(all(value is None or (isinstance(value, str) and bool(value.strip()))
                for value in prediction.values()),
            "Prediction values must be nonempty strings or null.")
    validate_provider_prediction(prediction, requested_fields, profile)


SOURCE_ROLE_VERIFIED, SOURCE_MATCH, SOURCE_NOT_FOUND, DATE_ORDER_UNVERIFIABLE, SOURCE_NOT_CHECKED = (
    'source_role_verified', 'source_text_match', 'not_found_in_source', 'date_order_not_verifiable', 'not_checked')
SOURCE_NEAR_MATCH, NEAR_MATCH_RATIO = 'source_near_match', 0.9
# Field arbitration. These map onto the stored resolution values; see arbitrate().
PARSER_PRESERVED, SOURCE_VERIFIED_CANDIDATE, UNVERIFIED_MODEL_SUGGESTION = (
    'PARSER_PRESERVED', 'SOURCE_VERIFIED_CANDIDATE', 'UNVERIFIED_MODEL_SUGGESTION')
PARSER_MODEL_CONFLICT, NO_RECOVERY, MODEL_FAILURE = ('PARSER_MODEL_CONFLICT', 'NO_RECOVERY', 'MODEL_FAILURE')
# Words a label must carry for a value beside it to play this role, and words that rule it out.
ROLE_LABELS = {
    'total_amount': (r'\b(?:grand\s+|net\s+|final\s+)?total\b|\bjumlah\b|\bamount\b', r'\b(?:sub\s*-?\s*total|tax|gst|vat|sst|cash|change|tender|discount|rounding|item|due|payable|balance|current\s+charges)\b'),
    'amount_due': (r'\b(?:amount|total|balance)\s+(?:due|payable)\b|\bpayable\b', r'\bprevious\b'),
    'tax_amount': (r'\b(?:tax|vat|gst|sst)\b', r'\b(?:excl|before|id|no|reg|registration|summary)\b'),
    'invoice_number': (r'\binvoice\s*(?:no|number|#|id)\b|\bno\b\.?\s*:|\bdoc(?:ument)?\s*no\b', r'\b(?:order|account|tax|tel|fax)\b'),
    'bill_reference': (r'\b(?:bill|invoice|reference|ref)\s*(?:no|number|#|id)?\b', r'\b(?:account|tax)\b'),
    # "Tax Invoice" is a heading that can sit beside the number; only a tax or GST identifier rules a line out.
    'receipt_number': (r'\b(?:receipt|bill|invoice|doc(?:ument)?|trans(?:action)?|cb)\s*(?:no|number|#|id|ref)?\b|#|\bno\b\.?\s*:', r'\b(?:account|tax\s*(?:id|no|reg)|gst|tel|fax)\b'),
    'account_number': (r'\b(?:account|acct|a/c)\b', r'\b(?:name|bank)\b'),
    'invoice_date': (r'\b(?:invoice\s+date|date\s*of\s*issue|issue\s+date|date|tarikh)\b', r'\bdue\b'),
    'bill_date': (r'\b(?:bill\s+date|date|tarikh)\b', r'\b(?:due|deadline)\b'),
    'transaction_date': (r'\b(?:date|tarikh|tanggal)\b', r'\b(?:due|expiry)\b'),
    'due_date': (r'\bdue\b|\bdeadline\b|tarikh\s+akhir', r'$^'),
    # A POS timestamp prints the time beside the date, with no word for it.
    'transaction_time': (r'\b(?:time|masa|date|tarikh)\b|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}', r'$^'),
}
_PARTY_ROLES = {'supplier_name': r'\b(?:seller|supplier|vendor|from)\b', 'issuer': r'\b(?:supplier|issuer|provider|from)\b',
                'merchant': r'\b(?:merchant|seller|store|shop)\b'}
_OTHER_PARTY = r'\b(?:client|customer|buyer|bill\s+to|ship\s+to|sold\s+to|deliver(?:y)?\s+to|attn|member)\b'
_NON_CURRENT_PARTY = r'\b(?:(?:formerly|previously)(?:\s+known\s+as)?|licensee\s+of)\b'
_ADDRESS_PARTY = {'supplier_address': 'supplier_name', 'provider_address': 'issuer', 'merchant_address': 'merchant'}
# A rejected value stays in the private artifact for audit but is never published.
PLACEHOLDER_VALUE = 'placeholder_not_a_value'
REJECTED_SOURCE_CHECKS = {SOURCE_NOT_FOUND, DATE_ORDER_UNVERIFIABLE, PLACEHOLDER_VALUE}
# A printed "N/A" says the document has no value; it is not a value to offer for this field.
_PLACEHOLDER = re.compile(r'(?:n\s*/?\s*a|n\.a\.?|not\s+applicable|none|nil|null|unknown|tbc|tba|-+)', re.I)
_AMOUNT_FIELDS = {'subtotal', 'tax_amount', 'total_amount', 'amount_due'}
_IDENTIFIER_FIELDS = {'invoice_number', 'bill_reference', 'receipt_number', 'account_number'}
_DATE_FORMATS = ('%d %b %Y', '%d %B %Y', '%b %d, %Y', '%B %d, %Y', '%d-%b-%Y', '%d/%b/%Y')


def _times_in(text):
    """Yield (24-hour value, clock digits as printed) for each time on a line."""
    # OCR can run a date into the time: "18/01/201817:09:21".
    text = re.sub(r'(\b\d{1,2}[/-]\d{1,2}[/-]\d{4})(?=\d{1,2}:)', r'\1 ', text)
    for hour, minute, second, meridiem in re.findall(r'(?<![\d:])' + TIME_TEXT + r'(?![\d:])', text):
        value = clock_time(hour, minute, second or None, meridiem or None)
        if value:
            yield value, f'{int(hour):02d}:{minute}' + (f':{second}' if second else '')


def _dates_in(text):
    """Yield (date, order_is_certain) for each complete date printed in a line."""
    for token in re.findall(r'\d{4}-\d{2}-\d{2}', text):
        try:
            yield date.fromisoformat(token), True
        except ValueError:
            pass
    for first, second, year in re.findall(r'(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)', text):
        readings = set()
        for day, month in ((first, second), (second, first)):
            try:
                readings.add(date(int(year), int(month), int(day)))
            except ValueError:
                pass
        for reading in readings:
            yield reading, len(readings) == 1
    for fmt in _DATE_FORMATS:
        for token in re.findall(r'\d{1,2}[ /-][A-Za-z]{3,9}[ /-]\d{4}|[A-Za-z]{3,9} \d{1,2}, \d{4}', text):
            try:
                yield datetime.strptime(token, fmt).date(), True
            except ValueError:
                pass


def _positioned_lines(document, lines):
    from services.extraction.parsing.map_invoice import location
    placed = []
    for item in lines:
        prov = item.get('prov') or []
        if len(prov) != 1:
            continue
        entry = location(document, prov[0].get('page_no'), prov[0].get('bbox'), item.get('self_ref'), item.get('text', ''))
        if entry.get('bbox'):
            placed.append((item, entry))
    return placed


def _role_supported(field_name, matched, document, lines, parser_fields):
    """Lines whose position and label tie this value to the requested field."""
    placed = _positioned_lines(document, lines)
    at = {item.get('self_ref'): entry for item, entry in placed}

    def beside_or_above(entry):
        box = entry['bbox']; height = box['b'] - box['t']
        for other, there in placed:
            if there['page'] != entry['page'] or there is entry:
                continue
            b = there['bbox']
            left = abs((b['t'] + b['b'] - box['t'] - box['b']) / 2) <= height * .7 and b['r'] <= box['l'] + height
            above = 0 <= box['t'] - b['b'] <= height * 1.8 and min(b['r'], box['r']) > max(b['l'], box['l'])
            if left or above:
                yield other

    def top_of_first_page(entry):
        size = entry.get('page_size') or {}
        return entry['page'] == 1 and size.get('height') and entry['bbox']['t'] <= size['height'] * .3

    def below_other_party(entry):
        box = entry['bbox']
        return any(there['page'] == entry['page'] and re.search(_OTHER_PARTY, other.get('text', ''), re.I)
                   and there['bbox']['b'] <= box['t'] + 1 and box['t'] - there['bbox']['b'] <= (box['b'] - box['t']) * 8
                   and min(there['bbox']['r'], box['r']) > max(there['bbox']['l'], box['l']) for other, there in placed)

    if field_name in ROLE_LABELS:
        wanted, excluded = ROLE_LABELS[field_name]
        def is_label(text):
            return bool(re.search(wanted, text, re.I)) and not re.search(excluded, text, re.I)
        return [item for item in matched if item.get('self_ref') in at and (
            not re.search(excluded, item.get('text', ''), re.I)) and (
            is_label(item.get('text', '')) or any(is_label(other.get('text', '')) for other in beside_or_above(at[item['self_ref']])))]
    if field_name in _PARTY_ROLES:
        supported = []
        for item in matched:
            entry = at.get(item.get('self_ref'))
            if not entry or re.search(_OTHER_PARTY, item.get('text', ''), re.I) or below_other_party(entry):
                continue
            # A header position cannot turn a former name or franchise reference
            # into the current issuer. Keep its printed text available for review.
            if re.search(_NON_CURRENT_PARTY, item.get('text', ''), re.I) or any(
                    re.fullmatch(_NON_CURRENT_PARTY + r'\s*:?', other.get('text', '').strip(), re.I)
                    for other in beside_or_above(entry)):
                continue
            labelled = re.search(_PARTY_ROLES[field_name], item.get('text', ''), re.I) or any(
                re.search(_PARTY_ROLES[field_name], other.get('text', ''), re.I) for other in beside_or_above(entry))
            if labelled or top_of_first_page(entry):
                supported.append(item)
        return supported
    if field_name in _ADDRESS_PARTY:
        party = (parser_fields or {}).get(_ADDRESS_PARTY[field_name]) or {}
        refs = {e.get('source_ref') for e in party.get('evidence') or [] if isinstance(e, dict)}
        anchors = [at[ref] for ref in refs if ref in at]
        supported = []
        for item in matched:
            entry = at.get(item.get('self_ref'))
            if not entry or below_other_party(entry):
                continue
            box = entry['bbox']
            if any(a['page'] == entry['page'] and 0 <= box['t'] - a['bbox']['t'] <= (a['bbox']['b'] - a['bbox']['t']) * 10
                   and min(a['bbox']['r'], box['r']) > max(a['bbox']['l'], box['l']) for a in anchors):
                supported.append(item)
        return supported
    return []


def check_candidate_against_source(field_name, candidate, document, parser_fields=None):
    """Check a model value against OCR/parser text; the model's own claims are not used.

    source_text_match proves the text is printed. source_role_verified also ties it
    to the field by label or position, which is stronger but still needs review.
    Parser hints in the prompt can be repeated back, so neither is proof of truth.
    """
    from services.extraction.classification.classify_document import classification_records
    from services.extraction.layout.layout_headers import normalized_amount
    from services.extraction.parsing.map_invoice import location
    if _PLACEHOLDER.fullmatch(candidate.strip()):
        return {'status': PLACEHOLDER_VALUE, 'source_refs': [], 'evidence': []}
    lines = [item for item in classification_records(document) if item.get('text', '').strip()]

    def amount_matches(text):
        wanted = Decimal(candidate)
        for token in re.findall(r'-?\d[\d., \u00a0]*\d|\d', text):
            try:
                if Decimal(normalized_amount(token, None)) == wanted:
                    return True
            except (ValueError, ArithmeticError):
                pass
        return False

    def squeezed(text):
        return re.sub(r'[\W_]+', '', text).casefold()

    status, matched, printed_value = SOURCE_NOT_FOUND, [], None
    parts = []
    if field_name == 'transaction_time':
        # Same clock digits as printed: AM/PM comes from the document, never the model.
        readings = {value: item for item in lines for value, clock in _times_in(item['text']) if clock == candidate}
        if len(readings) <= 1:
            matched = [item for item in lines if any(value == candidate for value, _ in _times_in(item['text']))]
            if not matched and readings:
                printed_value, item = next(iter(readings.items()))
                matched = [item]
        if not matched:
            # OCR kept the hour and AM/PM but lost the minutes ("03PM"). The image shows them, so a person can confirm.
            wanted = re.fullmatch(r'(\d{2}):\d{2}(?::\d{2})?', candidate)
            fragments = [item for item in lines for part in [re.fullmatch(r'(\d{1,2})\s*([AaPp])\.?\s*[Mm]\.?', item['text'].strip())]
                         if part and wanted and clock_time(part[1], '00', None, part[2]) == f'{wanted[1]}:00']
            # The same printed line can come back from two OCR streams, so count distinct text.
            if len({item['text'].strip() for item in fragments}) == 1:
                status, matched = SOURCE_NEAR_MATCH, fragments[:1]
    elif field_name in _AMOUNT_FIELDS:
        matched = [item for item in lines if amount_matches(item['text'])]
    elif field_name.endswith('_date'):
        wanted = date.fromisoformat(candidate)
        certain = [item for item in lines if (wanted, True) in set(_dates_in(item['text']))]
        uncertain = [item for item in lines if (wanted, False) in set(_dates_in(item['text']))]
        matched = certain
        if not certain and uncertain:
            status, matched = DATE_ORDER_UNVERIFIABLE, uncertain
    elif field_name in _IDENTIFIER_FIELDS:
        wanted = candidate.strip().casefold()
        matched = [item for item in lines if wanted in {
            token.rstrip('./-').casefold() for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9./_-]*', item['text'])}]
    else:
        parts = [squeezed(part) for part in candidate.splitlines() if squeezed(part)]
        matched = [item for item in lines if any(part in squeezed(item['text']) for part in parts)]
        found = {part for part in parts if any(part in squeezed(item['text']) for item in lines)}
        if len(found) != len(parts) or not parts:
            matched = []
        # The model reads the image, so where OCR misread a name ("&" as "G") its answer will not match the OCR.
        # That is shown for a person to settle against the page. It is never a verified role.
        if not matched and field_name in _PARTY_ROLES and parts:
            near = [item for item in lines if SequenceMatcher(None, parts[0], squeezed(item['text'])).ratio() >= NEAR_MATCH_RATIO]
            # The same printed line can come back from two OCR streams, so count distinct text.
            if len({squeezed(item['text']) for item in near}) == 1:
                status, matched = SOURCE_NEAR_MATCH, near[:1]
    if matched and status not in {DATE_ORDER_UNVERIFIABLE, SOURCE_NEAR_MATCH}:
        supported = _role_supported(field_name, matched, document, lines, parser_fields)
        # Every line of a multiline candidate needs role evidence. A supplier
        # street plus a customer's city must not inherit the street's role.
        if parts and not all(any(part in squeezed(item['text']) for item in supported) for part in parts):
            supported = []
        status = SOURCE_ROLE_VERIFIED if supported else SOURCE_MATCH
        matched = supported or matched
    evidence = []
    for item in matched[:5]:
        for prov in item.get('prov', []):
            entry = location(document, prov.get('page_no'), prov.get('bbox'), item.get('self_ref'), item.get('text', ''))
            entry.update(evidence_origin='parser_text_matched_to_model_candidate',
                         from_ocr=item.get('from_ocr'), ocr_score=item.get('ocr_score'))
            evidence.append(entry)
    return {'status': status, 'source_refs': sorted({e['source_ref'] for e in evidence if e.get('source_ref')}),
            'evidence': evidence, **({'printed_value': printed_value} if printed_value else {})}


def same_candidate_value(parser_value, candidate):
    try:
        return Decimal(str(parser_value)) == Decimal(str(candidate))
    except ArithmeticError:
        fold = lambda value: ' '.join(re.sub(r'[,\s]+', ' ', str(value)).casefold().split())
        return fold(parser_value) == fold(candidate)


def arbitrate(parser_value, candidate, check_status, rejection=None):
    """One explicit outcome per field. A parser value is never replaced here."""
    if rejection:
        return MODEL_FAILURE
    if parser_value not in (None, ''):
        if candidate is None or same_candidate_value(parser_value, candidate):
            return PARSER_PRESERVED
        return PARSER_MODEL_CONFLICT
    if candidate is None or check_status in REJECTED_SOURCE_CHECKS:
        return NO_RECOVERY
    return SOURCE_VERIFIED_CANDIDATE if check_status == SOURCE_ROLE_VERIFIED else UNVERIFIED_MODEL_SUGGESTION


def verify_profile_recovery(extraction, run, routing, directory, root, source_path, authorized):
    """Recheck the source-bound plan; a successful JSON response proves no field."""
    from services.extraction.ai.run_selective_qwen import verified_pdf, recovery_context
    from services.extraction.classification.route_extraction import plan_field_recovery, decide_route
    require(authorized is True and root is not None, 'Current source authorization and project root are required.')
    profile = extraction['document_type']['value']
    require(run.get('profile') == profile and run.get('inference_permission') is True
            and run.get('source_access_authorized') is True, 'Recovery profile or explicit permissions differ.')
    fields = run.get('requested_fields')
    validate_requested_fields(fields, profile)
    expected_route = decide_route(extraction, allow_vlm_inference=True)
    require(all(routing.get(k) == v for k, v in expected_route.items()), 'Original document review route changed.')
    plan_path = directory / 'recovery-plan.json'
    require(plan_path.is_file() and sha256_file(plan_path) == run.get('recovery_plan_sha256'), 'Recovery plan hash mismatch.')
    saved = load_json(plan_path)
    configuration = run.get('model_configuration')
    require(isinstance(configuration, dict) and configuration.get('model') == run.get('model')
            and configuration.get('profile') == profile and configuration.get('render_policy') == run.get('render_policy')
            and configuration.get('request_contract') == 'profile_fields/1', 'Model/configuration identity mismatch.')
    plan = plan_field_recovery(extraction, root, source_path=source_path,
        source_access_authorized=authorized, model_configuration=configuration)
    require(plan['source_verified'] and not plan['blocking_reasons']
            and all(plan['fields'].get(name, {}).get('eligible') for name in fields), 'Recovery eligibility/source no longer valid.')
    plan.update(profile=profile, selected_fields=fields, source_result_sha256=run['source_result_sha256'])
    require(saved == plan and run.get('configuration_sha256') == plan['configuration_sha256'], 'Recovery plan/configuration changed.')
    _, pdf_bytes = verified_pdf(extraction, root, source_path=source_path, source_access_authorized=authorized)
    require(hashlib.sha256(pdf_bytes).hexdigest() == run.get('source_pdf_sha256') and run.get('page') == 1,
            'Recovery PDF/page identity mismatch.')
    image_path = Path(run['image']).resolve()
    require(image_path.parent == directory and sha256_file(image_path) == run.get('image_sha256'), 'Recovery image hash/path mismatch.')
    request = prepare_request(image_path.read_bytes(), fields, model=run['model'], profile=profile,
                              source_context=recovery_context(extraction, plan, fields, root, run.get('previous_problems')))
    settings = directory / 'request-settings.json'; prompt = directory / 'prompt.txt'
    require(sha256_file(settings) == run.get('request_settings_sha256')
            and load_json(settings) == {k:request[k] for k in ('model','format','stream','keep_alive','options')}
            and configuration.get('options') == request['options'], 'Requested schema/configuration mismatch.')
    require(sha256_file(prompt) == run.get('prompt_sha256')
            and prompt.read_text(encoding='utf-8') == request['messages'][0]['content'], 'Source context/prompt changed.')
    return profile, fields, plan


def require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


def reconcile(
    extraction_result_path: Path,
    selective_run_dir: Path,
    output_parent: Path = DEFAULT_RUNS_DIR,
    *, project_root=None, source_path=None, source_access_authorized=False,
):
    extraction_result_path = extraction_result_path.resolve()
    selective_run_dir = selective_run_dir.resolve()
    output_parent = output_parent.resolve()

    require(
        extraction_result_path.is_file(),
        f"Extraction result not found: {extraction_result_path}",
    )

    run_path = selective_run_dir / "run.json"
    routing_path = selective_run_dir / "routing.json"
    prediction_path = selective_run_dir / "prediction.json"

    for p in (run_path, routing_path, prediction_path):
        require(p.is_file(), f"Required selective-run artifact missing: {p}")

    extraction_raw = extraction_result_path.read_bytes()
    extraction = ClassifiedExtractionResult.model_validate_json(extraction_raw).model_dump()
    run = load_json(run_path)
    routing = load_json(routing_path)
    prediction = load_json(prediction_path)

    require(isinstance(run, dict) and isinstance(routing, dict),
            "Selective run and routing must be JSON objects.")
    require(run.get("status") == "response_structure_valid_needs_review",
            "Selective run must have response_structure_valid_needs_review status.")

    require(
        extraction.get("schema_version") == "1.1.0-proposed",
        "Reconciliation requires a classified 1.1.0-proposed extraction result.",
    )

    document_id = extraction.get("document_id")
    extraction_run_id = extraction.get("extraction_run_id")
    require(bool(document_id.strip()) and bool(extraction_run_id.strip()),
            "Document and extraction-run IDs must not be blank.")

    require(
        document_id == routing.get("document_id"),
        "Document ID mismatch between extraction and routing result.",
    )

    require(
        document_id == run.get("document_id"),
        "Document ID mismatch between extraction and selective run.",
    )

    require(
        extraction_run_id == routing.get("extraction_run_id"),
        "Extraction run ID mismatch between extraction and routing result.",
    )

    extraction_sha256 = hashlib.sha256(extraction_raw).hexdigest()

    require(
        extraction_sha256 == run.get("source_result_sha256"),
        "Extraction-result SHA-256 does not match the selective run.",
    )

    source_result_path = run.get("source_result_path")
    if source_result_path:
        require(
            Path(source_result_path).resolve() == extraction_result_path,
            "Selective run refers to a different extraction-result path.",
        )

    scoped = run.get('recovery_scope') == 'profile_fields/1'
    profile, plan = None, None
    if scoped:
        profile, routing_requested, plan = verify_profile_recovery(extraction, run, routing, selective_run_dir,
            project_root, source_path, source_access_authorized)
        require(run.get('decision') == routing.get('decision'), 'Document review decision changed.')
    else:
        require('recovery_scope' not in run and routing.get('decision') == run.get('decision') == 'vlm_candidate',
                'Reconciliation expects an authorized supported selective scope.')
        routing_requested = routing.get('requested_fields')

    require(
        run.get("ai_request_attempted") is True,
        "Selective AI request was not attempted.",
    )

    require(
        run.get("ai_response_received") is True,
        "Selective AI response was not received.",
    )

    run_requested = run.get("requested_fields")
    validate_requested_fields(routing_requested, profile)
    validate_requested_fields(run_requested, profile)

    require(
        routing_requested == run_requested,
        "Requested fields differ between routing and selective run.",
    )

    validate_prediction(prediction, routing_requested, profile)
    if 'prediction_sha256' in run:
        require(run['prediction_sha256'] == sha256_file(prediction_path),
                'Selective prediction SHA-256 mismatch.')
    if 'raw_response_sha256' in run:
        raw_response = selective_run_dir / 'raw_response.txt'
        require(raw_response.is_file() and sha256_file(raw_response) == run['raw_response_sha256'],
                'Selective raw-response SHA-256 mismatch.')
        raw_answer = parse_json(raw_response.read_text(encoding='utf-8'))
        rechecked = (validate_response_fields(raw_answer, routing_requested, model=run.get('model'), profile=profile)[0]
                     if scoped else validate_response(raw_answer, routing_requested, model=run.get('model'), profile=profile))
        require(rechecked == prediction, 'Prediction differs from the validated raw response.')
    if "extraction_run_id" in run:
        require(run["extraction_run_id"] == extraction_run_id,
                "Extraction run ID mismatch in selective run.")

    extraction_fields = extraction.get("fields") or {}

    reconciled_fields = {}
    source_document = None
    if scoped:
        source_document = json.loads(Path(extraction['provenance']['source_document_json']).read_bytes())

    for field_name in routing_requested:
        require(
            field_name in extraction_fields,
            f"Requested field does not exist in extraction result: {field_name}",
        )

        parser_field = extraction_fields[field_name]
        parser_value = parser_field.get("value")
        parser_state = parser_field.get("state")
        parser_method = parser_field.get("method")

        vlm_candidate = prediction.get(field_name)
        source_check = {'status': SOURCE_NOT_CHECKED, 'source_refs': [], 'evidence': []}
        if scoped and vlm_candidate is not None:
            source_check = check_candidate_against_source(field_name, vlm_candidate, source_document, extraction_fields)
        field_rejection = (run.get('field_rejections') or {}).get(field_name)

        if parser_value not in (None, ""):
            resolution = "parser_preserved"
            final_value = parser_value
            review_required = True

        elif vlm_candidate is None or source_check['status'] in REJECTED_SOURCE_CHECKS:
            resolution = "unresolved_missing"
            final_value = None
            review_required = True

        else:
            resolution = "candidate_for_review"
            final_value = None
            review_required = True

        reconciled_fields[field_name] = {
            "parser": {
                "value": parser_value,
                "state": parser_state,
                "method": parser_method,
                "raw_text": parser_field.get("raw_text"),
                "evidence": parser_field.get("evidence") or [],
                "candidates": parser_field.get("candidates") or [],
            },
            "vlm": {
                "candidate": vlm_candidate,
                "model": run.get("model"),
                "selective_run_id": selective_run_dir.name,
                "requested": True,
                "profile": profile or extraction['document_type']['value'],
                "field": field_name,
                "verification": {
                    "status": "unverified",
                    "reasons": ['MODEL_VALUE_NOT_VERIFIED_AGAINST_SOURCE']
                        + (['MODEL_RETURNED_NULL'] if vlm_candidate is None else [])
                        + (['MODEL_VALUE_NOT_FOUND_IN_SOURCE'] if source_check['status'] == SOURCE_NOT_FOUND else [])
                        + (['DATE_ORDER_NOT_VERIFIABLE'] if source_check['status'] == DATE_ORDER_UNVERIFIABLE else [])
                        + (['MODEL_RETURNED_PLACEHOLDER'] if source_check['status'] == PLACEHOLDER_VALUE else [])
                        + (['PARSER_CANDIDATES_REQUIRE_REVIEW'] if parser_state in {'ambiguous','conflict'} else [])
                        + (['PARSER_VALUE_PRESERVED'] if parser_value is not None else [])
                        + (['MODEL_DISAGREES_WITH_PARSER'] if vlm_candidate is not None and parser_value is not None
                           and vlm_candidate != parser_value else [])
                        + (['PARTY_ROLE_NOT_VERIFIED'] if field_name in {'merchant','merchant_address','supplier_name',
                            'supplier_address','issuer','provider_address'} else []),
                },
                # A page image is provenance, not verified field localization.
                "confidence": None,
                "evidence": [],
                "source_check": source_check,
                "provenance": {
                    "model_digest": run.get("model_digest"),
                    "source_extraction_sha256": extraction_sha256,
                    "document_id": document_id,
                    "extraction_run_id": extraction_run_id,
                    "configuration_sha256": run.get('configuration_sha256'),
                    "source_transformation": extraction['provenance'].get('source_transformation'),
                    "source_json_sha256": extraction['provenance'].get('source_json_sha256'),
                    "attempt_id": plan['fields'][field_name]['attempt_id'] if plan else None,
                    "source_context_refs": plan['fields'][field_name]['source_refs'] if plan else [],
                    "source_pdf_sha256": run.get("source_pdf_sha256"),
                    "image": run.get("image"),
                    "image_sha256": run.get("image_sha256"),
                    "page": run.get("page"),
                    "prediction_sha256": sha256_file(prediction_path),
                    "raw_response_sha256": run.get("raw_response_sha256"),
                    "grounding": "unverified_page_image",
                },
            },
            "resolution": resolution,
            "outcome": arbitrate(parser_value, source_check.get('printed_value') or vlm_candidate,
                                 source_check['status'], field_rejection),
            "field_rejection": field_rejection,
            "final_value": final_value,
            "review_required": review_required,
        }

    overall_review_required = any(
        field["review_required"] for field in reconciled_fields.values()
    )

    if any(
        field["resolution"] == "candidate_for_review"
        for field in reconciled_fields.values()
    ):
        status = "candidate_for_review"

    elif any(
        field["resolution"] == "unresolved_missing"
        for field in reconciled_fields.values()
    ):
        status = "unresolved_needs_review"

    else:
        status = "parser_preserved"

    result = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "mode": "selective_vlm_reconciliation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "document_id": document_id,
        "source_extraction_run_id": extraction_run_id,
        "source_extraction_result": str(extraction_result_path),
        "source_extraction_sha256": extraction_sha256,
        "selective_run_id": selective_run_dir.name,
        "status": status,
        "review_required": overall_review_required,
        "fields": reconciled_fields,
        "provenance": {
            "routing_policy_version": routing.get("policy_version"),
            "document_review_decision": routing.get('decision'),
            "document_review_blockers": routing.get('blocking_reasons', []),
            "recovery_scope": run.get('recovery_scope'),
            "model": run.get("model"),
            "source_pdf": run.get("source_pdf"),
            "source_pdf_sha256": run.get("source_pdf_sha256"),
            "image": run.get("image"),
            "image_sha256": run.get("image_sha256"),
            "page": run.get("page"),
            "requested_fields": routing_requested,
            "prediction_sha256": sha256_file(prediction_path),
            "selective_run_status": run.get("status"),
        },
        "safety": {
            "parser_result_overwritten": False,
            "ai_candidate_auto_accepted": False,
            "automatic_accounting_approval": False,
        },
    }

    output_parent.mkdir(parents=True, exist_ok=True)

    output_dir = output_parent / f"reconciliation-{uuid.uuid4().hex}"
    output_dir.mkdir(parents=False)

    result_path = output_dir / "result.json"
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return result_path, result


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile a classified parser result with a selective VLM "
            "prediction without overwriting parser evidence."
        )
    )

    parser.add_argument(
        "extraction_result",
        help="Path to a 1.1.0-proposed classified extraction result.",
    )

    parser.add_argument(
        "selective_run",
        help="Path to a selective-* run directory.",
    )

    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_RUNS_DIR),
        help="Parent folder for reconciliation-* run directories.",
    )

    args = parser.parse_args()

    try:
        result_path, result = reconcile(
            Path(args.extraction_result),
            Path(args.selective_run),
            Path(args.output_dir),
        )
    except Exception as exc:
        print(f"Status: failed")
        print(f"Error: {exc}")
        raise SystemExit(1)

    print(f"Document: {result['document_id']}")
    print(f"Status: {result['status']}")
    print(f"Review required: {result['review_required']}")

    for field_name, field in result["fields"].items():
        print()
        print(f"Field: {field_name}")
        print(f"Parser value: {field['parser']['value']!r}")
        print(f"Parser state: {field['parser']['state']}")
        print(f"VLM candidate: {field['vlm']['candidate']!r}")
        print(f"Resolution: {field['resolution']}")
        print(f"Final value: {field['final_value']!r}")

    print()
    print("Parser result overwritten: False")
    print("AI candidate auto-accepted: False")
    print(f"Saved: {result_path.parent}")


if __name__ == "__main__":
    main()
