"""Qualitative guidance from existing extraction states, separate from M3 rules."""
def field_guidance(row):
    key=row.get('field_key','')
    state=row.get('extraction_state') or 'unsupported'
    reviewed=row.get('corrected_by') is not None and row.get('corrected_at') is not None
    evidence=row.get('evidence') or []
    candidates=row.get('candidates') or []
    warnings=[]
    def warn(code,message): warnings.append({'code':code,'message':message,'reviewed':reviewed})
    if state=='missing':
        warn('MISSING_IDENTIFIER' if key in {'invoice','reference','account_number'} else 'MISSING_VALUE',
             'No supported source value was extracted. Confirm from the original document.')
    elif state in {'ambiguous','conflict'}:
        distinct={c.get('value') for c in candidates if isinstance(c,dict) and c.get('value') is not None}
        if len(distinct)>1:
            warn('CONFLICTING_TOTALS' if key in {'total','amount_due'} else 'CONFLICTING_VALUES','Source candidates disagree; no candidate was selected.')
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
    raw=' '.join(str(c.get('raw_text') or '') for c in candidates if isinstance(c,dict))
    if key=='tax' and state!='resolved' and '%' in raw:
        warn('TAX_RATE_NOT_AMOUNT','A tax percentage is not a document tax amount.')
    return {'warnings':warnings,'confirmationRequired':not reviewed,
            'reviewRequired':not reviewed}


def extraction_guidance(raw,items):
    warnings=[]
    if not items:
        warnings.append({'code':'NO_MAPPED_LINE_ITEMS','message':'No supported item rows were recovered. Inspect the original.'})
    if (raw.get('document_type') or {}).get('value')=='unknown':
        warnings.append({'code':'UNKNOWN_DOCUMENT_KIND','message':'Document kind remains unresolved from source evidence.'})
    return warnings
