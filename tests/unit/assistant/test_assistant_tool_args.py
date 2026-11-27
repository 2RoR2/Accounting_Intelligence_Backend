"""G2: every tool's argument model is strict (extra keys, tenant ids, SQL text, out-of-range dates are refused)."""
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from services.assistant.runner import ToolCallInvalid, parse_call
from services.assistant.tools import TOOLS

TODAY = date.today()
VALID = {
    'contact_balance': {'contact': 'Senduduk Fit-Out', 'side': 'owed_to_us'},
    'due_soon': {'kind': 'bills', 'days': 7},
    'top_contacts': {'kind': 'suppliers', 'from_date': '2026-11-01', 'to_date': '2026-11-30', 'limit': 5},
    'profit_and_loss': {'from_date': '2026-11-01', 'to_date': '2026-11-30'},
    'balance_sheet': {'as_of': '2026-11-27'},
    'compare_periods': {'period_a': '2026-10', 'period_b': '2026-11'},
    'document_queue': {},
    'month_end_status': {},
    'bank_unmatched': {'limit': 5},
    'search_records': {'query': 'Pauh Builders', 'kind': 'purchases'},
    'search_document_text': {'query': 'electricity'},
    'explain_document': {'document_id': '00000000-0000-4000-8000-000000000001'},
}
# One field per tool that holds a date, an enum or a bounded number, and a value it must refuse.
BAD_VALUES = {
    'contact_balance': [('side', 'DROP TABLE users'), ('contact', 'x'), ('contact', 'y' * 81),
                        ('as_of', (TODAY + timedelta(days=6 * 366)).isoformat())],
    'due_soon': [('kind', "bills'; DELETE FROM documents; --"), ('days', 0), ('days', 61)],
    'top_contacts': [('kind', 'everyone'), ('limit', 11), ('from_date', '1999-01-01'), ('to_date', '2026-10-01')],
    'profit_and_loss': [('from_date', 'last month'), ('to_date', '2026-10-31'), ('from_date', '2040-01-01')],
    'balance_sheet': [('as_of', '2026-13-01'), ('as_of', '2010-01-01')],
    'compare_periods': [('period_a', '2026-13'), ('period_b', 'November'), ('period_a', '1990-01')],
    'bank_unmatched': [('limit', 0), ('limit', 100)],
    'search_records': [('kind', 'all tenants'), ('query', 'a'), ('query', 'q' * 81)],
    'search_document_text': [('query', ''), ('query', 'z' * 81)],
    'explain_document': [('document_id', "1 OR 1=1"), ('document_id', 'not-a-uuid')],
}


def test_every_tool_is_covered():
    assert set(VALID) == set(TOOLS)
    assert len(TOOLS) >= 8


@pytest.mark.parametrize('name', sorted(VALID))
def test_valid_arguments_are_accepted(name):
    tool, args = parse_call(name, VALID[name])
    assert tool.name == name
    assert args.model_dump()  is not None


@pytest.mark.parametrize('name', sorted(VALID))
@pytest.mark.parametrize('smuggled', [{'tenant_id': 'other-tenant'}, {'tenantId': 'tenant-zzz'}, {'user_id': 'user-zzz'},
                                      {'sql': 'SELECT * FROM users'}, {'role': 'super_admin'}])
def test_extra_keys_such_as_a_tenant_id_are_refused(name, smuggled):
    with pytest.raises(ToolCallInvalid) as error:
        parse_call(name, {**VALID[name], **smuggled})
    assert error.value.code == 'TOOL_ARGUMENTS_INVALID'
    assert 'extra_forbidden' in error.value.detail
    # The refusal names the field, never echoes the smuggled value.
    assert str(next(iter(smuggled.values()))) not in error.value.detail


@pytest.mark.parametrize('name,field,value', [(n, f, v) for n, items in BAD_VALUES.items() for f, v in items])
def test_out_of_range_or_injected_values_are_refused(name, field, value):
    with pytest.raises(ToolCallInvalid):
        parse_call(name, {**VALID[name], field: value})


def test_no_tool_schema_offers_a_tenant_user_or_sql_parameter():
    for tool in TOOLS.values():
        schema = tool.schema()['function']['parameters']
        names = {key.lower() for key in schema.get('properties', {})}
        assert not names & {'tenant', 'tenant_id', 'tenantid', 'user', 'user_id', 'sql', 'query_sql', 'role'}
        assert schema['additionalProperties'] is False


def test_unknown_tool_and_bad_json_are_refused():
    with pytest.raises(ToolCallInvalid) as unknown:
        parse_call('run_sql', {'statement': 'DELETE FROM documents'})
    assert unknown.value.code == 'TOOL_UNKNOWN'
    with pytest.raises(ToolCallInvalid) as broken:
        parse_call('due_soon', '{"kind": "bills"')
    assert broken.value.code == 'TOOL_ARGUMENTS_NOT_JSON'
    with pytest.raises(ToolCallInvalid):
        parse_call('due_soon', '["bills"]')


def test_date_range_must_be_ordered():
    with pytest.raises(ValidationError):
        TOOLS['profit_and_loss'].args.model_validate({'from_date': '2026-11-30', 'to_date': '2026-11-01'})
