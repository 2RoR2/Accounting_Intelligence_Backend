"""Risk order for the review lanes: look first at what is most likely wrong (AI roadmap P1-lite).

Each document gets a score built only from what readiness already found (services/review/readiness.py) and from
how many of its fields still need a person, plus the sentences behind the score, so the order explains itself.
Nothing here approves, hides or blocks anything: the lanes, the checks and every approval rule are unchanged.

Guardrail against trusting "verified" too much (1.0% of fields are filled wrong on the hold-out set): about one
ready document in twenty is picked at random for a full check. The pick is deterministic per document revision
(a hash, not a random call), so it does not change between page loads and cannot be steered by re-opening.
"""
from __future__ import annotations

import hashlib

# Points per readiness reason: things that are wrong rank above things that are only unconfirmed.
REASON_POINTS = {
    'extraction_not_succeeded': 50, 'validation_failed': 40, 'duplicate_candidate': 40, 'open_exception': 30,
    'validation_not_run': 25, 'field_missing': 20, 'supplier_not_known': 15, 'field_needs_check': 10,
    'own_submission': 0,
}
FIELD_POINTS = 3          # each field that still says "Please check", "Suggested" or "Not found"
FIELD_POINTS_CAP = 30
SPOT_CHECK_ONE_IN = 20    # 5%
HIGH, MEDIUM = 40, 15

WHY = {
    'extraction_not_succeeded': 'The document was not read.',
    'validation_failed': 'A check failed.',
    'duplicate_candidate': 'It may be a duplicate.',
    'open_exception': 'An exception is still open.',
    'validation_not_run': 'The checks have not run on this version.',
    'field_missing': 'A required field is empty.',
    'supplier_not_known': 'The supplier or customer is not on your list.',
    'field_needs_check': 'A required field needs checking.',
}


def spot_check(document_id, revision) -> bool:
    """True for about one document revision in twenty, the same answer every time it is asked."""
    digest = hashlib.sha256(f'{document_id}:{revision}'.encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big') % SPOT_CHECK_ONE_IN == 0


def risk(reasons, unverified_fields: int) -> dict:
    """Score, level and the plain sentences behind it. Higher means look at it first."""
    score, why = 0, []
    for item in reasons:
        code = item.get('code')
        points = REASON_POINTS.get(code, 10)
        if code == 'field_needs_check':
            points *= max(1, len(item.get('fields') or []))
        score += points
        sentence = WHY.get(code)
        if sentence and sentence not in why:
            why.append(sentence)
    if unverified_fields:
        score += min(FIELD_POINTS * unverified_fields, FIELD_POINTS_CAP)
        why.append(f'{unverified_fields} field{"s" if unverified_fields != 1 else ""} not confirmed from the document.')
    level = 'high' if score >= HIGH else 'medium' if score >= MEDIUM else 'low'
    return {'score': score, 'level': level, 'why': why[:3]}


def rank(items):
    """Spot-checks first, then highest score; the original (newest-first) order breaks ties."""
    order = {id(item): index for index, item in enumerate(items)}
    return sorted(items, key=lambda item: (not item.get('spotCheck'), -item['risk']['score'], order[id(item)]))
