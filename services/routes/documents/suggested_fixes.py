"""Read-only suggestions drawn from the current extraction and validation findings."""
import os
import re
from datetime import date

from fastapi import HTTPException

from services.extraction.contracts.accounting_contract import FIELD_KEYS, effective_value, field_value
from services.extraction.contracts.field_catalog import CANONICAL_FIELD_TYPES
from services.extraction.classification.reconcile_selective import SOURCE_MATCH, SOURCE_ROLE_VERIFIED
from validation.documents.document_validation import exception_cause
from validation.matching.vendor_matching import FUZZY_MATCH_THRESHOLD, calculate_vendor_similarity

NUMERIC_DATE = r"(?:\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{4})"


def _alternative(field, prediction):
    current = _current_value(prediction)
    alternatives = set()
    for candidate in prediction.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("source") == "ai" and candidate.get("source_check") not in {SOURCE_MATCH, SOURCE_ROLE_VERIFIED}:
            continue
        value = candidate.get("value")
        if not isinstance(value, str) or not value.strip() or value.strip() == current:
            continue
        try:
            field_value(FIELD_KEYS.get(field, field), value.strip())
        except ValueError:
            continue
        alternatives.add(value.strip())
    return next(iter(alternatives)) if len(alternatives) == 1 else None


def _current_value(prediction):
    if prediction.get("field_key") in FIELD_KEYS:
        try:
            return effective_value(prediction)
        except ValueError:
            return prediction.get("current_value")
    return prediction.get("current_value")


def _format_date(value, order):
    match = re.fullmatch(r"(\d{4})([./-])(\d{1,2})\2(\d{1,2})", value or "")
    if match:
        year, month, day = (int(match[i]) for i in (1, 3, 4))
    else:
        match = re.fullmatch(r"(\d{1,2})([./-])(\d{1,2})\2(\d{4})", value or "")
        if not match:
            return None
        day, month, year = (int(match[i]) for i in (1, 3, 4))
        if order != "DMY" and day <= 12 and day != month:
            return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _format_billing_period(value, order):
    match = re.fullmatch(rf"({NUMERIC_DATE})\s*(?:to|-)\s*({NUMERIC_DATE})", value, re.IGNORECASE)
    if match:
        start, end = (_format_date(part, order) for part in match.groups())
        if start and end and start <= end:
            return f"{start} to {end}"
    return None


def _supplier_suggestion(db, tenant_id, current):
    # Saved supplier_matches have no extraction or source value. Match the current evidence directly.
    if not current:
        return None
    vendors = db.execute("SELECT vendor_name FROM vendors WHERE tenant_id=%s AND is_active",
                         (tenant_id,)).fetchall()
    matches = [row["vendor_name"] for row in vendors
               if calculate_vendor_similarity(current, row["vendor_name"]) >= FUZZY_MATCH_THRESHOLD]
    return matches[0] if len(matches) == 1 and matches[0] != current else None


def suggested_fixes(db, user, document_id):
    tenant_id = user["tenant_id"]
    visibility = " AND (owner_id=%s OR assigned_to=%s)" if user["role"] == "accountant" else ""
    params = [document_id, tenant_id, *([user["id"], user["id"]] if visibility else [])]
    document = db.execute("SELECT content_revision FROM documents WHERE id=%s AND tenant_id=%s" + visibility,
                          params).fetchone()
    if not document:
        raise HTTPException(404, "Document not found.")

    revision = document["content_revision"]
    findings = db.execute("""SELECT rule_id,field_key,actual_value,message
        FROM validation_results WHERE document_id=%s AND tenant_id=%s AND content_revision=%s
          AND (status='failed' OR prevents_approval OR (status='not_checked' AND requires_review))
        ORDER BY created_at,rule_id,id""", (document_id, tenant_id, revision)).fetchall()
    exceptions = db.execute("""SELECT e.type,e.message FROM exceptions e
        WHERE e.document_id=%s AND e.tenant_id=%s AND e.status<>'resolved'
          AND NOT EXISTS (SELECT 1 FROM validation_results v WHERE v.document_id=e.document_id
            AND v.tenant_id=e.tenant_id AND v.content_revision<%s AND v.status='failed'
            AND e.type='VALIDATION:' || v.rule_id AND split_part(e.message, ' (also required by ', 1)
                = COALESCE(v.field_key || ': ', '') || v.message)
        ORDER BY e.created_at,e.id""", (document_id, tenant_id, revision)).fetchall()
    if not findings and not exceptions:
        return []
    fields = db.execute("""SELECT fp.* FROM field_predictions fp JOIN raw_extractions re
        ON re.id=fp.extraction_id AND re.document_id=fp.document_id AND re.is_current
        WHERE fp.document_id=%s ORDER BY fp.field_key""", (document_id,)).fetchall()
    exact = {row["field_key"]: row for row in fields}
    aliases = {FIELD_KEYS.get(key, key): row for key, row in exact.items()}
    mirrored = {(f"VALIDATION:{f['rule_id']}", f"{f['field_key']}: {f['message']}") for f in findings}
    for exception in exceptions:
        code, separator, rule = exception["type"].partition(":")
        message = exception["message"]
        if code == "VALIDATION" and separator:
            if (exception["type"], message.split(" (also required by ", 1)[0]) in mirrored:
                continue
            field, sep, detail = message.partition(": ")
            if sep:
                findings.append({"rule_id": rule, "field_key": field, "actual_value": None, "message": detail})
                continue
        findings.append({"rule_id": exception["type"], "field_key": None, "actual_value": None, "message": message})

    lines = []
    if any((finding["field_key"] or "").startswith("line_items.") for finding in findings):
        lines = db.execute("""SELECT li.description,li.quantity,li.unit_price,li.amount,li.candidates FROM line_items li
            JOIN raw_extractions re ON re.id=li.extraction_id AND re.document_id=li.document_id AND re.is_current
            WHERE li.document_id=%s ORDER BY li.line_number""", (document_id,)).fetchall()
    suggestions = []
    for finding in findings:
        key = finding["field_key"] or "document"
        canonical = FIELD_KEYS.get(key, key)
        prediction = exact.get(key) or aliases.get(canonical) or {}
        line_field = re.fullmatch(r"line_items\.(\d+)\.(description|quantity|unit_price|amount)", key)
        if line_field and int(line_field[1]) < len(lines):
            index, line_key = int(line_field[1]), line_field[2]
            line = lines[index]
            prediction = {"field_label": f"Line {index + 1} {line_key.replace('_', ' ')}",
                          "current_value": str(line[line_key]) if line[line_key] is not None else None,
                          "candidates": (line["candidates"] or {}).get(line_key, [])}
        field = prediction.get("field_key", key)
        label = prediction.get("field_label") or key.replace("_", " ").capitalize()
        rule, reason = finding["rule_id"], finding["message"]
        cause = exception_cause(rule, canonical, reason)
        value = _alternative(canonical, prediction)
        source = "rule"
        explanation = f"Check {label.lower()} against the original document and review this finding."
        if canonical == "supplier" and (cause == "UNKNOWN_VENDOR" or value is None):
            current = _current_value(prediction) or finding["actual_value"] or prediction.get("raw_value")
            match = _supplier_suggestion(db, tenant_id, current)
            if match:
                suggestions.append({"field": field, "label": label, "reason": reason, "suggestedValue": match,
                                    "source": "supplier-match", "explanation": "Compare the matched catalog supplier with the original document."})
                continue
            explanation = "The current name did not identify a unique supplier replacement. Check it against the supplier catalog."
            value = None
        if value is not None:
            source = "candidate"
            explanation = "The extraction found this alternative. Verify it against the original document."
        elif cause == "INVALID_FORMAT" and (canonical in {"date", "due", "billing_period"} or
                                            CANONICAL_FIELD_TYPES.get(canonical) == "date"):
            original = (_current_value(prediction) or finding["actual_value"] or prediction.get("raw_value") or "").strip()
            order = os.getenv("DATE_ORDER", "DMY").strip().upper() or "DMY"
            is_period = canonical == "billing_period"
            value = _format_billing_period(original, order) if is_period else _format_date(original, order)
            format_hint = "YYYY-MM-DD to YYYY-MM-DD" if is_period else "YYYY-MM-DD"
            if value and value != original:
                explanation = f"The printed {label.lower()} can be written as {format_hint}."
            else:
                value = None
                explanation = f"Check {label.lower()} on the original document and enter it as {format_hint}."
        suggestions.append({"field": field, "label": label, "reason": reason, "suggestedValue": value,
                            "source": source, "explanation": explanation})
    return suggestions
