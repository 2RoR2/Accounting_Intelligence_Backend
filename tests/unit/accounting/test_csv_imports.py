"""CSV imports (plan v2 task A2): parsers, validators, duplicate rules and templates, without a database.

The company pack files (task A1) are the contract: the templates must have exactly their headers, and
a freshly generated pack must import with no errors.
"""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from ai.synthetic import company_pack
from services.imports import chart, contacts, opening
from services.imports.common import ImportFileError, preview_token, read_rows, template_csv

pytestmark = pytest.mark.runtime_defaults


@pytest.fixture(scope='module')
def pack(tmp_path_factory):
    return company_pack.generate_pack('trading', tmp_path_factory.mktemp('packs'))


def csv_bytes(*lines):
    return ('\n'.join(lines) + '\n').encode('utf-8')


CONTACT_HEADER = ','.join(company_pack.CONTACT_COLUMNS)


def contact(type_='supplier', name='Example Supplies Sdn. Bhd.', ssm='202001000001', tin='C1000000001',
            email='akaun@example.test', terms='30', account='5000', contact_id=''):
    return f'{contact_id},{type_},{name},trade,{ssm},{tin},SST-1,"No. 1, Jalan Contoh, 50000 Kuala Lumpur",{email},03-1234 5678,{terms},{account},MYR'


# --- templates and file reading ---------------------------------------------------------------------

@pytest.mark.parametrize('module,columns', [(contacts, company_pack.CONTACT_COLUMNS), (chart, company_pack.COA_COLUMNS),
                                            (opening, company_pack.OPENING_COLUMNS)])
def test_template_header_equals_what_the_company_pack_writes(module, columns, pack):
    assert module.COLUMNS == columns
    assert template_csv(module.COLUMNS) == ','.join(columns) + '\n'
    name = {contacts: 'contacts.csv', chart: 'coa.csv', opening: 'opening_balances.csv'}[module]
    assert (pack / name).read_text(encoding='utf-8').splitlines()[0] == ','.join(columns)


def test_file_level_problems_are_explained():
    with pytest.raises(ImportFileError, match='empty'):
        read_rows(b'', ['type'], 'contacts')
    with pytest.raises(ImportFileError, match='CSV UTF-8'):
        read_rows('type,name\nsupplier,Caf\xe9'.encode('latin-1'), ['type'], 'contacts')
    with pytest.raises(ImportFileError, match='name are missing'):
        read_rows(b'type\nsupplier\n', ['type', 'name'], 'contacts')
    with pytest.raises(ImportFileError, match='no rows'):
        read_rows(b'type,name\n\n', ['type', 'name'], 'contacts')
    with pytest.raises(ImportFileError, match='more cells'):
        read_rows(b'type,name\nsupplier,A,extra\n', ['type', 'name'], 'contacts')
    with pytest.raises(ImportFileError, match='larger than 2 MB'):
        read_rows(b'type,name\n' + b'x' * (2 * 1024 * 1024), ['type', 'name'], 'contacts')


def test_excel_bom_extra_columns_and_blank_rows_are_accepted():
    _, rows = read_rows('﻿type,name,notes\nsupplier, A Co ,x\n,,\n'.encode('utf-8'), ['type', 'name'], 'contacts')
    assert rows == [(1, {'type': 'supplier', 'name': 'A Co', 'notes': 'x'})]


def test_preview_token_follows_file_and_company():
    tenant = uuid4()
    assert preview_token('contacts', tenant, b'a') == preview_token('contacts', tenant, b'a')
    assert preview_token('contacts', tenant, b'a') != preview_token('contacts', tenant, b'b')
    assert preview_token('contacts', tenant, b'a') != preview_token('contacts', uuid4(), b'a')
    assert preview_token('contacts', tenant, b'a') != preview_token('chart_of_accounts', tenant, b'a')


# --- contacts ---------------------------------------------------------------------------------------

def test_pack_contacts_import_clean(pack):
    rows = contacts.plan_contacts(contacts.parse_contacts((pack / 'contacts.csv').read_bytes()), {})
    assert rows and all(row['status'] == 'new' for row in rows)
    assert {row['values']['type'] for row in rows} == {'supplier', 'customer'}


def test_contact_row_validation():
    content = csv_bytes(CONTACT_HEADER, contact(type_='vendor'), contact(name=''), contact(email='not-an-email'),
                        contact(terms='400'), contact(ssm='20200#1'),
                        'X1,customer,Good Customer Sdn. Bhd.,customer,,,,,,,,,myr2')
    rows = contacts.plan_contacts(contacts.parse_contacts(content), {})
    errors = [' '.join(row['errors']) for row in rows]
    assert 'supplier or customer' in errors[0]
    assert 'Name is empty' in errors[1]
    assert 'E-mail' in errors[2]
    assert 'Payment terms' in errors[3]
    assert 'SSM number' in errors[4]
    assert 'Currency' in errors[5]
    assert all(row['status'] == 'error' for row in rows)


def test_existing_contacts_are_reported_as_duplicates_not_merged():
    existing = {'supplier': [{'name': 'EXAMPLE SUPPLIES SDN BHD', 'ssm': None, 'tin': None},
                             {'name': 'Other Name', 'ssm': '2020-0100-0002', 'tin': None},
                             {'name': 'Third Name', 'ssm': None, 'tin': 'c 2000000002'}],
                'customer': []}
    content = csv_bytes(CONTACT_HEADER,
                        contact(name='Example Supplies Sdn. Bhd.', ssm='', tin=''),  # same name, punctuation differs
                        contact(name='New Name A', ssm='202001000002', tin=''),       # same SSM
                        contact(name='New Name B', ssm='', tin='C2000000002'),        # same TIN
                        contact(type_='customer', name='Example Supplies Sdn. Bhd.', ssm='', tin=''))  # customer list is separate
    rows = contacts.plan_contacts(contacts.parse_contacts(content), existing)
    assert [row['status'] for row in rows] == ['duplicate', 'duplicate', 'duplicate', 'new']
    assert rows[0]['duplicateOf'] == 'EXAMPLE SUPPLIES SDN BHD'
    assert 'same SSM number' in rows[1]['warnings'][0] and 'same TIN' in rows[2]['warnings'][0]


def test_clashing_rows_in_one_file_are_errors():
    content = csv_bytes(CONTACT_HEADER, contact(contact_id='S1'), contact(contact_id='S2', name='Example Supplies Sdn Bhd', ssm='', tin=''),
                        contact(contact_id='S1', name='Another', ssm='', tin=''))
    rows = contacts.plan_contacts(contacts.parse_contacts(content), {})
    assert [row['status'] for row in rows] == ['new', 'error', 'error']
    assert 'row 1' in rows[1]['errors'][0] and 'Contact id S1' in rows[2]['errors'][0]


def test_unknown_default_account_and_unstored_customer_columns_are_warnings():
    content = csv_bytes(CONTACT_HEADER, contact(account='9999'),
                        'C1,customer,Some Customer Bhd,customer,,C3000000003,,,,,,4000,MYR')
    rows = contacts.plan_contacts(contacts.parse_contacts(content), {}, frozenset({'4000'}))
    assert rows[0]['status'] == 'new' and 'Default account 9999' in rows[0]['warnings'][0]
    assert rows[1]['status'] == 'new' and 'Not stored for customers yet: contact_id, category, tin, default_account_code.' in rows[1]['warnings']


# --- chart of accounts ------------------------------------------------------------------------------

COA_HEADER = ','.join(company_pack.COA_COLUMNS)


def test_pack_chart_imports_into_both_lists(pack):
    rows = chart.plan_chart(chart.parse_chart((pack / 'coa.csv').read_bytes()), {}, {})
    assert len(rows) == len(company_pack.COA) and all(row['status'] == 'new' for row in rows)
    assert all(row['values']['addTo'] == ['chart', 'ledger'] for row in rows)


def test_chart_row_validation():
    content = csv_bytes(COA_HEADER, '1000,Cash,asset,revenue,balance_sheet,Current assets,debit',
                        '4000,Sales,income,revenue,balance_sheet,Revenue,credit', '5000,Purchases,expense,cogs,profit_and_loss,Cost of sales,both',
                        'bad code!,Name,asset,asset,,,', '6000,,expense,expense,,,', '1000,Cash again,asset,asset,,,')
    rows = chart.plan_chart(chart.parse_chart(content), {}, {})
    messages = [' '.join(row['errors']) for row in rows]
    assert 'does not fit type' in messages[0]
    assert 'belongs on the profit and loss' in messages[1]
    assert 'Normal balance' in messages[2]
    assert 'Account code must be' in messages[3]
    assert 'Account name is empty' in messages[4]
    assert 'also on row 1' in messages[5]


def coa_account(name, account_class='asset'):
    return {'name': name, 'account_class': account_class}


def ledger_account(name, account_type='asset'):
    return {'name': name, 'account_type': account_type}


def test_chart_existing_codes():
    content = csv_bytes(COA_HEADER, '1000,Cash,asset,asset,,,', '1010,Bank,asset,asset,,,', '1020,Petty Cash,asset,asset,,,',
                        '1030,Deposits,asset,asset,,,')
    rows = chart.plan_chart(chart.parse_chart(content),
                            {'1000': coa_account('Cash'), '1010': coa_account('BANK'), '1020': coa_account('Something else')},
                            {'1000': ledger_account('Cash')})
    assert [row['status'] for row in rows] == ['duplicate', 'new', 'duplicate', 'new']
    assert rows[1]['values']['addTo'] == ['ledger']
    assert rows[2]['duplicateOf'] == 'Something else'
    assert rows[3]['values']['addTo'] == ['chart', 'ledger']


def test_chart_counterpart_must_keep_the_existing_classification():
    content = csv_bytes(COA_HEADER, '2000,Trade Payables,asset,asset,,,', '2010,Other Payables,asset,asset,,,',
                        '2020,Accrued Expenses,liability,liability,,,')
    rows = chart.plan_chart(chart.parse_chart(content),
                            {'2010': coa_account('Other Payables', 'liability'), '2020': coa_account('Accrued Expenses', 'liability')},
                            {'2000': ledger_account('Trade Payables', 'liability')})
    assert [row['status'] for row in rows] == ['error', 'error', 'new']
    assert 'already in the ledger as liability' in rows[0]['errors'][0]
    assert 'already in the chart as liability' in rows[1]['errors'][0]
    assert rows[2]['values']['addTo'] == ['ledger']


def test_chart_section_backfill_requires_a_matching_account():
    content = csv_bytes(COA_HEADER, '5000,Purchases,expense,cogs,profit_and_loss,Cost of sales,debit',
                        '6000,Rent,expense,expense,profit_and_loss,Operating expenses,debit')
    rows = chart.plan_chart(chart.parse_chart(content),
                            {'5000': coa_account('Purchases', 'cogs'), '6000': coa_account('Rent', 'expense')},
                            {'5000': ledger_account('Purchases', 'expense'), '6000': ledger_account('Other', 'expense')})
    assert rows[0]['status'] == 'new'
    assert rows[0]['values']['fillReportSection'] is True
    assert rows[0]['values']['addTo'] == []
    assert rows[1]['status'] == 'duplicate'


def test_unclosed_quote_is_an_error_not_a_swallowed_row():
    content = b'type,name\nsupplier,"Open Quote Sdn Bhd\ncustomer,Later Customer\ncustomer,Last Customer\n'
    with pytest.raises(ImportFileError, match='not valid CSV'):
        read_rows(content, ['type', 'name'], 'contacts')


def test_csv_parser_failures_become_import_errors():
    too_long = b'type,name\nsupplier,' + b'x' * 200_000 + b'\n'
    with pytest.raises(ImportFileError, match='Row 1 is not valid CSV'):
        read_rows(too_long, ['type', 'name'], 'contacts')
    with pytest.raises(ImportFileError, match='column-name row is not valid CSV'):
        read_rows(b'type,' + b'n' * 200_000 + b'\nsupplier,A\n', ['type'], 'contacts')


# --- opening balances -------------------------------------------------------------------------------

OPENING_HEADER = ','.join(company_pack.OPENING_COLUMNS)
PERIOD = {'id': uuid4(), 'period_code': '2026-08', 'starts_on': date(2026, 8, 1), 'ends_on': date(2026, 8, 31)}


def ledger(*codes):
    return {code: {'id': uuid4(), 'name': code} for code in codes}


def plan(*lines, accounts=('1010', '1100', '3000'), periods=(PERIOD,), posted=frozenset(), used=frozenset()):
    return opening.plan_opening(opening.parse_opening(csv_bytes(OPENING_HEADER, *lines)), ledger(*accounts), list(periods),
                                posted, used)


BALANCED = ['account,2026-08-01,1010,Bank,,,,,,900.00,', 'account,2026-08-01,1100,Receivables,,,,,,100.00,',
            'account,2026-08-01,3000,Capital,,,,,,,"1,000.00"',
            'receivable,2026-08-01,1100,Receivables,C1,OB-AR01,INV-1,2026-07-01,2026-07-31,60.00,',
            'receivable,2026-08-01,1100,Receivables,C2,OB-AR02,INV-2,2026-07-05,2026-08-04,40.00,']


def test_pack_opening_balances_make_one_balanced_journal(pack):
    accounts = {code: {'id': uuid4(), 'name': name} for code, name, *_ in company_pack.COA}
    rows, file_errors, journal = opening.plan_opening(
        opening.parse_opening((pack / 'opening_balances.csv').read_bytes()), accounts, [PERIOD])
    assert file_errors == [] and all(row['status'] == 'new' for row in rows)
    debit = sum(line['debit'] for line in journal['lines'])
    assert debit == sum(line['credit'] for line in journal['lines']) > 0
    assert journal['idempotencyKey'] == 'opening-balances:2026-08-01' and journal['sourceType'] == 'manual'
    items = [line for line in journal['lines'] if line['sourceField']]
    assert items and all(line['sourceField'].startswith('opening_item:') for line in items)


def test_open_items_replace_the_control_account_line():
    rows, file_errors, journal = plan(*BALANCED)
    assert file_errors == []
    assert [(line['description'][:22], line['debit'], line['credit']) for line in journal['lines']] == [
        ('Opening balance', Decimal('900.00'), Decimal('0.00')),
        ('Opening receivable INV', Decimal('60.00'), Decimal('0.00')),
        ('Opening receivable INV', Decimal('40.00'), Decimal('0.00')),
        ('Opening balance', Decimal('0.00'), Decimal('1000.00'))]
    assert 'contact C1, dated 2026-07-01, due 2026-07-31' in journal['lines'][1]['description']


def test_unbalanced_trial_balance_and_open_items_that_do_not_tie():
    _, file_errors, journal = plan(BALANCED[0], BALANCED[1], 'account,2026-08-01,3000,Capital,,,,,,,999.00', BALANCED[3])
    assert journal is None
    assert any('do not balance' in error and '1.00' in error for error in file_errors)
    assert any('open items for account 1100 add up to 60.00' in error for error in file_errors)


def test_opening_rows_need_known_accounts_and_one_sided_amounts():
    rows, _, journal = plan(*BALANCED[:2], 'account,2026-08-01,9999,Unknown,,,,,,,1000.00',
                            'account,2026-08-01,1010,Bank,,,,,,5.00,5.00', 'account,2026-08-01,3000,x,,,,,,12.345,')
    assert journal is None
    assert 'Import the chart first' in rows[2]['errors'][0]
    assert 'either a debit or a credit' in ' '.join(rows[3]['errors'])
    assert 'at most two decimals' in ' '.join(rows[4]['errors'])


def test_replacement_for_a_reversed_opening_journal_gets_a_new_key():
    assert opening.idempotency_key(date(2026, 8, 1)) == 'opening-balances:2026-08-01'
    used = frozenset({'opening-balances:2026-08-01', 'opening-balances:2026-08-01:2'})
    assert opening.idempotency_key(date(2026, 8, 1), used) == 'opening-balances:2026-08-01:3'
    # The reversed journal's date is not in already_posted, but its key is in used_keys.
    _, errors, journal = plan(*BALANCED, used=frozenset({'opening-balances:2026-08-01'}))
    assert errors == [] and journal['idempotencyKey'] == 'opening-balances:2026-08-01:2'


def test_opening_needs_an_open_period_one_date_and_posts_once():
    _, errors, _ = plan(*BALANCED, periods=())
    assert any('No open accounting period covers 2026-08-01' in error for error in errors)
    _, errors, _ = plan(*BALANCED, posted=frozenset({date(2026, 8, 1)}))
    assert any('already posted' in error for error in errors)
    _, errors, _ = plan(BALANCED[0].replace('2026-08-01', '2026-08-02'), *BALANCED[1:])
    assert any('same opening date' in error for error in errors)
