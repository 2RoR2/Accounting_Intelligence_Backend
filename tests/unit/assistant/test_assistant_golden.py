"""The 30-question golden set: the fallback router picks the expected tool, must-refuse questions are refused, and
every page suggestion is answerable without the model."""
import json
from pathlib import Path

import pytest
from assistant_fakes import TODAY

from services.assistant.fallback import compare_months, period, route_question
from services.assistant.refusals import refusal_for
from services.assistant.suggestions import PAGES, suggestions_for

GOLDEN = json.loads((Path(__file__).parent / 'golden_questions.json').read_text(encoding='utf-8'))['questions']


def test_golden_set_has_thirty_questions_and_five_refusals():
    assert len(GOLDEN) == 30
    assert sum(1 for item in GOLDEN if item.get('refuse')) == 5
    assert len({item['id'] for item in GOLDEN}) == 30


@pytest.mark.parametrize('item', [q for q in GOLDEN if not q.get('refuse')], ids=lambda q: q['id'])
def test_router_picks_the_expected_tool(item):
    assert refusal_for(item['question']) is None
    routed = route_question(item['question'], item.get('context', {}), TODAY)
    assert routed is not None, item['question']
    tool, args = routed
    assert tool.name == item['tool']
    dumped = args.model_dump(mode='json')
    for key, value in item['args'].items():
        assert dumped[key] == value, (item['id'], key)


@pytest.mark.parametrize('item', [q for q in GOLDEN if q.get('refuse')], ids=lambda q: q['id'])
def test_must_refuse_questions(item):
    result = refusal_for(item['question'])
    assert result is not None and result[0] == item['refuse']


@pytest.mark.parametrize('page', sorted(PAGES) + ['unknown-page'])
def test_every_suggestion_routes_to_a_tool(page):
    context = {'documentId': '00000000-0000-4000-8000-000000000001'}
    for question in suggestions_for(page):
        assert refusal_for(question) is None, question
        assert route_question(question, context, TODAY) is not None, question


def test_periods():
    assert period('profit in October', TODAY)[0].isoformat() == '2026-10-01'
    assert period('profit in December', TODAY)[0].isoformat() == '2025-12-01'   # a later month means last year
    assert period('profit last month', TODAY)[1].isoformat() == '2026-10-31'
    assert period('profit this year', TODAY)[0].isoformat() == '2026-01-01'
    assert compare_months('expenses higher in November than October', TODAY) == ('2026-10', '2026-11')
    assert compare_months('compare January and February', TODAY) == ('2026-01', '2026-02')
    assert compare_months('why did expenses rise in March', TODAY) == ('2026-02', '2026-03')


@pytest.mark.parametrize('context,tool,args', [
    ({'page': 'insights', 'period': '2026-11'}, 'compare_periods', {'period_a': '2026-10', 'period_b': '2026-11'}),
    ({'page': 'reports', 'reportType': 'profit-and-loss', 'period': '2026-01'}, 'compare_periods',
     {'period_a': '2025-12', 'period_b': '2026-01'}),
    ({'page': 'reports', 'reportType': 'balance-sheet', 'period': '2026-10'}, 'balance_sheet', {'as_of': '2026-10-31'}),
    ({'page': 'reports', 'reportType': 'ageing'}, 'due_soon', {'kind': 'bills', 'include_overdue': True}),
])
def test_explain_this_report_reads_the_report_on_screen(context, tool, args):
    """The Insights and report "Explain this" buttons send "Explain this report" with the page context."""
    routed = route_question('Explain this report', context, TODAY)
    assert routed is not None and routed[0].name == tool
    dumped = routed[1].model_dump(mode='json')
    assert {key: dumped[key] for key in args} == args


def test_explain_without_a_report_context_is_not_taken_as_a_report():
    assert route_question('Explain this report', {'page': 'contacts'}, TODAY) is None
    routed = route_question('Why is this document flagged?', {'page': 'insights',
                                                               'documentId': '00000000-0000-4000-8000-000000000001'},
                            TODAY)
    assert routed[0].name == 'explain_document'
