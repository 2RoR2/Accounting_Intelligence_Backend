"""AI Inbox pieces that need no database: number binding, the supplier draft, bank-match reasons, ids."""
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

import main  # noqa: F401  (route modules import main first, as the app does)
from services.insights import inbox
from services.insights.fraud import Context, Doc
from services.routes.insights.routes import ai_assist_enabled


def test_bind_numbers_fills_placeholders_and_rejects_model_digits():
    assert inbox.bind_numbers('Pay {{amount}} for {{number}}', {'amount': 'MYR 462.00', 'number': 'BL-0098'}) == \
        'Pay MYR 462.00 for BL-0098'
    with pytest.raises(ValueError, match='DIGIT'):
        inbox.bind_numbers('Pay RM 500 for {{number}}', {'number': 'BL-0098'})
    with pytest.raises(ValueError, match='UNKNOWN'):
        inbox.bind_numbers('Send to {{iban}}', {'number': 'x'})


def test_draft_templates_hold_no_digits_of_their_own():
    inbox.bind_numbers(inbox.DRAFT_SUBJECT + inbox.DRAFT_BODY, {name: '' for name in inbox.DRAFT_PLACEHOLDERS})


def _bank_change_case():
    old = Doc(id='h', code='BILL_1', kind='bill', category='accounts_payable', status='validated',
              uploaded_at=datetime(2026, 10, 1, tzinfo=UTC), counterparty='Seladah Utilities',
              number='BL-0090', total=Decimal('462.00'), doc_date=date(2026, 10, 18), payment_account='514099002093')
    new = Doc(id='t', code='BILL_2', kind='bill', category='accounts_payable', status='extracted',
              uploaded_at=datetime(2026, 11, 20, tzinfo=UTC), counterparty='Seladah Utilities',
              number='BL-0098', total=Decimal('462.00'), doc_date=date(2026, 11, 18), payment_account='860412334417')
    return new, Context([old, new])


def test_supplier_draft_template_uses_only_server_values_and_last_four_digits():
    new, ctx = _bank_change_case()
    draft = inbox.supplier_draft(new, {}, ctx, 'KNT Trading Sdn Bhd', 'ap@seladah.example')
    assert draft['wording'] == 'template' and draft['to'] == 'ap@seladah.example'
    assert 'BL-0098' in draft['subject'] and 'MYR 462.00' in draft['body']
    assert 'ends in 4417' in draft['body'] and 'ending 2093' in draft['body']
    assert '860412334417' not in draft['body'] and '514099002093' not in draft['body']


def test_model_wording_is_used_only_when_it_keeps_placeholders_and_writes_no_digits():
    new, ctx = _bank_change_case()
    seen = {}

    def good(system, user, max_tokens):
        seen['user'] = user
        return {'subject': 'Bank details for {{number}}',
                'body': 'Hello {{supplier}}, {{number}} shows {{new_account}}, not {{old_account}}. {{company}}'}, {}
    inbox._wording_cache.clear()
    draft = inbox.supplier_draft(new, {}, ctx, 'KNT', None, use_model=True, complete=good)
    assert draft['wording'] == 'model' and draft['subject'] == 'Bank details for BL-0098'
    assert '4417' not in seen['user'] and 'Seladah' not in seen['user'] and '462' not in seen['user']

    def sneaky(system, user, max_tokens):
        return {'subject': 'Pay RM 9999 now', 'body': '{{number}} {{new_account}} {{old_account}}'}, {}
    inbox._wording_cache.clear()
    assert inbox.supplier_draft(new, {}, ctx, 'KNT', None, use_model=True, complete=sneaky)['wording'] == 'template'

    def broken(system, user, max_tokens):
        raise RuntimeError('provider down')
    inbox._wording_cache.clear()
    assert inbox.supplier_draft(new, {}, ctx, 'KNT', None, use_model=True, complete=broken)['wording'] == 'template'


def test_bank_match_basis_labels():
    line = {'reference': 'IBG 0098', 'description': 'SELADAH UTIL 0098', 'posted_date': date(2026, 11, 20)}
    record = {'invoice_number': 'BL-0098', 'reference_number': None, 'party': 'Seladah Utilities Sdn Bhd',
              'due_date': date(2026, 11, 18), 'invoice_date': date(2026, 11, 1)}
    assert inbox.score_candidate(line, record, rule=True, memory={'seladah utilities sdn bhd'}) == \
        ['Rule', 'Match', 'Memory', 'Similar']
    unrelated = {'invoice_number': 'INV-7', 'reference_number': None, 'party': 'Kedai Runcit Ah Seng',
                 'due_date': date(2026, 1, 1), 'invoice_date': None}
    assert inbox.score_candidate(line, unrelated, rule=False, memory=set()) == []


def test_ids_are_stable_hex_and_the_flag_defaults_off():
    first = inbox.fingerprint('BANK_MATCH', 'a|b')
    assert first == inbox.fingerprint('BANK_MATCH', 'a|b') != inbox.fingerprint('BANK_MATCH', 'a|c')
    assert inbox.ID_PATTERN.match(first)
    assert not ai_assist_enabled({}) and not ai_assist_enabled({'AI_ASSIST': 'off'})
    assert ai_assist_enabled({'AI_ASSIST': 'on'}) and ai_assist_enabled({'AI_ASSIST': 'TRUE'})
    assert inbox.short('a' + chr(0x200B) + 'b' + chr(0x202E) + ' c' + chr(10) + 'x' * 200, 10) == 'a b c xxx…'


@pytest.mark.parametrize('template', ['Pay thousands for {{number}}', 'See https://x.test for {{number}}',
                                      'Pay <b>{{amount}}</b>', 'Pay {{Amount}}'])
def test_bind_numbers_applies_the_assistant_guard(template):
    with pytest.raises(ValueError):
        inbox.bind_numbers(template, {'amount': 'MYR 462.00', 'number': 'BL-0098'})


def test_bind_numbers_keeps_values_unquoted_and_line_breaks():
    assert inbox.bind_numbers('Dear {{supplier}},\n\nRe {{number}}', {'supplier': 'Seladah', 'number': 'BL-0098'}) == \
        'Dear Seladah,\n\nRe BL-0098'
