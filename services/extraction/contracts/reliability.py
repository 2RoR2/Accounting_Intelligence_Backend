"""Qualitative guidance from existing extraction states, separate from M3 rules.

Evidence status says what supports a value, never how likely it is to be right.
OCR engine scores describe text recognition only, not whether the text belongs to
this field. No calibrated confidence exists without a labelled calibration set, so
extraction confidence stays null.
"""
def evidence_quality(state,reviewed,evidence,candidates):
    entries=[e for e in evidence if isinstance(e,dict)]
    options=[c for c in candidates if isinstance(c,dict)]
    source=[c for c in options if c.get('source')!='ai']
    distinct={c.get('value') for c in source if c.get('value') is not None}
    complete=any(e.get('source_ref') and e.get('page') and e.get('bbox') for e in entries)
    if reviewed: status='human_corrected'
    elif state=='resolved': status='source_backed' if complete else 'unsupported'
    elif len(source)!=len(options):
        status='model_source_verified' if any(c.get('source')=='ai' and c.get('source_check')=='source_role_verified' for c in options) else 'model_suggested'
    elif state=='conflict' or len(distinct)>1: status='conflicting'
    elif source: status='ambiguous_candidate'
    elif state=='missing': status='missing'
    else: status='unsupported'
    scores=[e[name] for e in entries for name in ('ocr_score','engine_score')
            if isinstance(e.get(name),(int,float)) and not isinstance(e.get(name),bool)]
    return {'evidenceStatus':status,
            'ocrEngineScore':{'min':min(scores),'max':max(scores),'scope':'text_recognition_only'} if scores else None}


def field_guidance(row):
    key=row.get('field_key','')
    state=row.get('extraction_state') or 'unsupported'
    reviewed=row.get('corrected_by') is not None and row.get('corrected_at') is not None
    evidence=row.get('evidence') or []
    candidates=row.get('candidates') or []
    warnings=[]
    def warn(code,message): warnings.append({'code':code,'message':message,'reviewed':reviewed})
    provisional = any(isinstance(e,dict) and e.get('candidate_basis')=='provisional_document_type'
                      for e in evidence + [e for c in candidates if isinstance(c,dict) for e in c.get('evidence',[])])
    model=[c for c in candidates if isinstance(c,dict) and c.get('source')=='ai' and c.get('value') is not None]
    if provisional and not reviewed:
        warn('PROVISIONAL_DOCUMENT_FIELDS', 'Printed source candidate retained. Document type and field role are unresolved; no value has been selected.')
    elif state=='missing' and model:
        # The parser found nothing, but the model did: say what is actually holding it back.
        value=model[0]['value']; check=model[0].get('source_check')
        if check=='source_role_verified':
            warn('MODEL_CANDIDATE_ROLE_VERIFIED',f'The local model read "{value}", and it is printed beside a label for this field. It was not applied. Check it against the document, then use it.')
        elif check=='source_near_match':
            warn('MODEL_READING_DIFFERS_FROM_OCR',f'The local model read "{value}" from the page image. The OCR text is close but not the same, so one of the two misread it. It was not applied. Check the document.')
        elif check=='source_text_match':
            warn('MODEL_CANDIDATE_ROLE_NOT_VERIFIED',f'The local model read "{value}", and that text is printed on the document, but no label or position ties it to this field. It was not applied. Check what it is before using it.')
        else:
            warn('MODEL_CANDIDATE_NOT_CONFIRMED',f'The local model suggested "{value}", but it could not be confirmed against the document text. It was not applied.')
    elif state=='missing' and key=='tax':
        warn('TAX_NOT_FOUND','No tax amount was found on this document, and none was assumed. Confirm from the original document.')
    elif state=='missing':
        warn('MISSING_IDENTIFIER' if key in {'invoice','reference','account_number'} else 'MISSING_VALUE',
             'No supported source value was extracted. Confirm from the original document.')
    elif state in {'ambiguous','conflict'}:
        distinct={c.get('value') for c in candidates if isinstance(c,dict) and c.get('value') is not None}
        if len(distinct)>1:
            warn('CONFLICTING_TOTALS' if key in {'total','amount_due'} else 'CONFLICTING_VALUES','Source candidates disagree; no candidate was selected.')
        elif key=='currency' and not distinct:
            pass  # A bare symbol: AMBIGUOUS_CURRENCY_SYMBOL below says what is wrong.
        elif key in {'date','due'}:
            warn('AMBIGUOUS_DATE','Source date or date order needs accountant confirmation.')
        else:
            warn('UNRESOLVED_FORMAT','Source value has an unsupported or uncertain format.')
    elif state in {'unsupported','unreadable'}:
        warn('UNSUPPORTED_EVIDENCE','The source does not support a usable extracted value.')
    if state=='resolved' and not any(e.get('source_ref') and e.get('page') and e.get('bbox') for e in evidence if isinstance(e,dict)):
        warn('UNSUPPORTED_EVIDENCE','A value exists without complete page/coordinate evidence; inspect the original.')
    if any(isinstance(e,dict) and e.get('derivation') for e in evidence):
        warn('DERIVED_FROM_SOURCE_ROWS','Value is a deterministic sum of printed breakdown rows; confirm its scope.')
    if key=='currency' and state=='ambiguous' and not {c.get('value') for c in candidates if isinstance(c,dict)}-{None}:
        warn('AMBIGUOUS_CURRENCY_SYMBOL','The source prints only $, which does not identify a currency. Confirm the currency; none was assumed.')
    printed=[e for c in candidates if isinstance(c,dict) for e in c.get('evidence') or []
             if isinstance(e,dict) and e.get('candidate_basis')=='printed_tax_exclusive_total']
    if key=='subtotal' and state!='resolved' and printed:
        warn('SUBTOTAL_TAX_EXCLUSIVE_CANDIDATE',f"The document prints a tax-exclusive total ({printed[0].get('printed_label')}) but does not label a subtotal. "
             'Subtotal stays empty unless you confirm it. Validation can use the printed tax-exclusive total as its pre-tax basis when the layout supports that.')
    dated=next((e for e in evidence if isinstance(e,dict) and e.get('date_basis')=='day_first_two_digit_year'),None)
    if dated and not reviewed:
        warn('TWO_DIGIT_YEAR',f"Printed as {dated.get('printed_value')}. Read as day-month-year because the first number cannot be a month, with the year in this century. Confirm it.")
    if any(isinstance(e,dict) and e.get('sign_basis')=='printed_totals' for e in evidence) and not reviewed:
        warn('ROUNDING_SIGN_FROM_TOTALS','The sign comes from the two printed totals, because the rounding row itself shows no clear sign.')
    role=next((e for e in evidence if isinstance(e,dict) and e.get('field_role_basis')=='document_number_beside_heading'),None)
    if role and not reviewed:
        warn('DOCUMENT_REFERENCE_ROLE',f"Printed as \"{role.get('printed_label')}\" beside the heading \"{role.get('printed_heading')}\". It is kept as this document's reference, not as an invoice number. Confirm it.")
    if any(isinstance(e,dict) and e.get('candidate_basis')=='printed_tax_summary_base' for e in evidence) and not reviewed:
        warn('TAX_SUMMARY_BASE','Printed in the tax summary, not labelled as a subtotal. It stays separate from subtotal.')
    raw=' '.join(str(c.get('raw_text') or '') for c in candidates if isinstance(c,dict))
    if key=='tax' and state!='resolved' and '%' in raw:
        warn('TAX_RATE_NOT_AMOUNT','A tax percentage is not a document tax amount.')
    return {'warnings':warnings,'confirmationRequired':not reviewed,
            'reviewRequired':not reviewed,**evidence_quality(state,reviewed,evidence,candidates)}


def extraction_guidance(raw,items):
    warnings=[]
    if not items:
        warnings.append({'code':'NO_MAPPED_LINE_ITEMS','message':'No supported item rows were recovered. Inspect the original.'})
    kind=raw.get('document_type') or {}
    if kind.get('value')=='unknown':
        warnings.append({'code':'UNKNOWN_DOCUMENT_KIND','message':'Document kind remains unresolved from source evidence.'})
    signals=[s for s in kind.get('signals') or [] if isinstance(s,dict)]
    for marker in (s for s in signals if s.get('kind')=='copy_marker'):
        if kind.get('reason')=='EXPLICIT_DOCUMENT_HEADING' and len(kind.get('candidates') or [])==1 and kind['candidates'][0]['evidence'].get('text')!=marker.get('text'):
            warnings.append({'code':'MIXED_DOCUMENT_HEADINGS','message':f"The document also prints \"{marker.get('text')}\". It is treated as {kind.get('value')} from its main heading. That wording marks a copy and does not make it a second document type."})
    headings=[h.get('evidence',{}).get('text') for c in kind.get('candidates') or [] for h in (c.get('evidence') or {}).get('printed_headings',[])]
    if headings:
        warnings.append({'code':'HEADING_DIFFERS_FROM_TYPE','message':f"The heading reads \"{headings[0]}\", but the printed total, payment and change show a completed sale, so this was classified as {kind.get('value')}. Confirm the type."})
    cues=[s.get('text') for s in signals if s.get('kind')=='payment_cue']
    if cues:
        warnings.append({'code':'PAYMENT_WORDING_PRINTED','message':f"Payment wording is printed ({', '.join(dict.fromkeys(cues))}). It says nothing certain about settlement, and no payment was recorded from it."})
    return warnings
