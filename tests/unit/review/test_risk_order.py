"""Risk order for the review lanes (roadmap P1-lite): explainable, stable, and a random spot-check of ready documents."""
from uuid import uuid4

from services.review import risk


def reason(code, **details):
    return {'code': code, 'message': code, **details}


def test_wrong_ranks_above_unconfirmed_and_says_why():
    failed = risk.risk([reason('validation_failed'), reason('duplicate_candidate')], 0)
    unconfirmed = risk.risk([reason('field_needs_check', fields=['date'])], 2)
    clean = risk.risk([], 0)
    assert failed['score'] > unconfirmed['score'] > clean['score'] == 0
    assert failed['level'] == 'high' and unconfirmed['level'] == 'medium' and clean['level'] == 'low'
    assert failed['why'] == ['A check failed.', 'It may be a duplicate.']
    assert unconfirmed['why'][-1] == '2 fields not confirmed from the document.'
    assert len(risk.risk([reason(c) for c in risk.WHY], 9)['why']) == 3  # at most three sentences


def test_each_field_to_check_adds_up_and_unconfirmed_fields_are_capped():
    assert risk.risk([reason('field_needs_check', fields=['date', 'total', 'tax'])], 0)['score'] == 30
    assert risk.risk([], 100)['score'] == risk.FIELD_POINTS_CAP


def test_spot_check_is_stable_per_revision_and_about_one_in_twenty():
    ids = [uuid4() for _ in range(4000)]
    picked = [document for document in ids if risk.spot_check(document, 3)]
    assert 0.035 < len(picked) / len(ids) < 0.065
    assert all(risk.spot_check(document, 3) for document in picked)          # same answer every time
    assert {risk.spot_check(document, 4) for document in picked[:40]} == {True, False}  # a new revision draws again


def test_rank_puts_spot_checks_first_then_highest_score_and_keeps_order_on_ties():
    items = [{'id': 'a', 'spotCheck': False, 'risk': {'score': 10}}, {'id': 'b', 'spotCheck': False, 'risk': {'score': 40}},
             {'id': 'c', 'spotCheck': True, 'risk': {'score': 0}}, {'id': 'd', 'spotCheck': False, 'risk': {'score': 10}}]
    assert [item['id'] for item in risk.rank(items)] == ['c', 'b', 'a', 'd']
