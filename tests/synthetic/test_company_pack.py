"""Company packs (plan v2 task A1): deterministic, valid manifests, parseable bank files, gold matches
that point at real rows and documents, and books that reconcile."""
import csv
import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal

import pytest

from ai.evaluation.dataset_contract import validate_collection
from ai.synthetic import company_pack
from services.bank.csv_parser import parse_bank_csv

LEDGER_TYPES = {'asset', 'liability', 'equity', 'income', 'expense'}  # ledger_accounts.account_type
COA_CLASSES = {'asset', 'liability', 'equity', 'revenue', 'cogs', 'expense'}  # coa_accounts.account_class
REAL_BANK_NAMES = ['maybank', 'cimb', 'public bank', 'rhb', 'hong leong', 'ambank', 'bank islam', 'uob', 'ocbc',
                   'hsbc', 'standard chartered', 'affin', 'alliance bank', 'bsn', 'bank rakyat', 'agrobank', 'muamalat']


@pytest.fixture(scope='module')
def packs(tmp_path_factory):
    root = tmp_path_factory.mktemp('packs')
    return {key: company_pack.generate_pack(key, root) for key in company_pack.COMPANIES}


def rows(path):
    with open(path, encoding='utf-8', newline='') as handle:
        return list(csv.DictReader(handle))


def summary(pack):
    return json.loads((pack / 'pack.json').read_text(encoding='utf-8'))


def amount(value):
    return Decimal(value) if value else Decimal('0')


def digest(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob('*')) if path.is_file()}


def test_pack_has_every_file(packs):
    for key, pack in packs.items():
        info = summary(pack)
        for name in ['contacts.csv', 'coa.csv', 'opening_balances.csv', 'documents.csv', 'expected_matches.csv',
                     'invoices.json', 'bills.json', 'receipts.json', 'pack.json']:
            assert (pack / name).is_file(), (key, name)
        assert info['period']['months'] == ['2026-08', '2026-09', '2026-10']
        for month in info['period']['months']:
            assert (pack / f'bank_{month}.csv').is_file()
            assert any((pack / 'documents' / month).iterdir())
        assert info['seed'] == company_pack.DEFAULT_SEED
        assert info['generator_version'] == company_pack.GENERATOR_VERSION
    assert len({summary(pack)['bank']['layout'] for pack in packs.values()}) == 3


def test_generation_is_deterministic(packs, tmp_path):
    again = company_pack.generate_pack('services', tmp_path)
    assert digest(again) == digest(packs['services'])


def test_seed_changes_the_company():
    first, second = company_pack.CompanyPack('trading', seed=1), company_pack.CompanyPack('trading', seed=2)
    first.build_contacts()
    second.build_contacts()
    assert first.own['name'] != second.own['name']
    assert [c['ssm'] for c in first.contacts] != [c['ssm'] for c in second.contacts]


def test_manifests_validate_and_match_the_document_index(packs):
    for key, pack in packs.items():
        manifests = validate_collection([pack / 'invoices.json', pack / 'bills.json', pack / 'receipts.json'], pack)
        samples = {s.sample_id: (m.document_type, s) for m in manifests for s in m.samples}
        index = {row['document_id']: row for row in rows(pack / 'documents.csv')}
        assert set(samples) == set(index), key
        for doc_id, (manifest_type, sample) in samples.items():
            assert index[doc_id]['manifest'] == manifest_type
            assert sample.source.relative_path == index[doc_id]['file']
            assert sample.source.file_format == index[doc_id]['file_format']
        formats = {row['file_format'] for row in index.values()}
        assert formats == {'pdf', 'jpg'}, key  # clean PDFs and degraded scans
        kinds = {row['kind'] for row in index.values()}
        assert kinds == {'sales_invoice', 'supplier_invoice', 'utility_bill', 'receipt', 'credit_note', 'debit_note'}


def test_document_totals_are_lines_plus_printed_tax(packs):
    for pack in packs.values():
        index = {row['document_id']: row for row in rows(pack / 'documents.csv')}
        for name in ('invoices.json', 'bills.json', 'receipts.json'):
            manifest = json.loads((pack / name).read_text(encoding='utf-8'))
            for sample in manifest['samples']:
                fields = {f['field_key']: Decimal(f['value']) for f in sample['fields']
                          if f['value_type'] == 'decimal'}
                lines = sum(Decimal(cell['value']) for row in sample['line_items']['rows']
                            for cell in row['cells'] if cell['field_key'] == 'line_total')
                assert lines == fields['subtotal'], sample['sample_id']
                assert fields['subtotal'] + fields['tax'] + fields.get('rounding', 0) == fields['total']
                row = index[sample['sample_id']]
                assert amount(row['subtotal']) + amount(row['tax']) + amount(row['rounding']) == amount(row['total'])
                assert amount(row['total']) == fields['total']
                if 'amount_due' in fields:
                    assert fields['amount_due'] == fields['total']  # no arrears in packs


def test_every_pdf_page_says_synthetic(packs):
    for pack in packs.values():
        for path in (pack / 'documents').rglob('*.pdf'):
            assert b'SYNTHETIC SAMPLE' in path.read_bytes(), path


def test_chart_of_accounts_matches_the_ledger_tables(packs):
    for pack in packs.values():
        accounts = rows(pack / 'coa.csv')
        codes = {row['code'] for row in accounts}
        assert 55 <= len(accounts) <= 70 and len(codes) == len(accounts)
        for row in accounts:
            assert row['type'] in LEDGER_TYPES and row['account_class'] in COA_CLASSES
            assert row['normal_balance'] in {'debit', 'credit'}
            assert row['statement'] in {'balance_sheet', 'profit_and_loss'} and row['report_section']
        for contact in rows(pack / 'contacts.csv'):
            assert contact['default_account_code'] in codes
            assert contact['type'] in {'supplier', 'customer'} and contact['currency'] == 'MYR'
            assert contact['ssm'] and contact['tin'] and contact['sst'] and contact['email'].endswith('.test')
            assert int(contact['payment_terms_days']) > 0
        assert {row['account_code'] for row in rows(pack / 'documents.csv')} <= codes


def test_opening_balances_balance_and_tie_to_the_open_items(packs):
    for pack in packs.values():
        info = summary(pack)
        lines = rows(pack / 'opening_balances.csv')
        accounts = [row for row in lines if row['row_type'] == 'account']
        debit = sum(amount(row['debit']) for row in accounts)
        credit = sum(amount(row['credit']) for row in accounts)
        assert debit == credit > 0
        balance = {row['account_code']: amount(row['debit']) - amount(row['credit']) for row in accounts}
        receivable = sum(amount(row['debit']) for row in lines if row['row_type'] == 'receivable')
        payable = sum(amount(row['credit']) for row in lines if row['row_type'] == 'payable')
        assert receivable == balance['1100'] > 0
        assert payable == -balance['2000'] > 0
        assert balance['1010'] == Decimal(info['bank']['opening_balance'])
        assert Decimal(info['receivables']['opening']) == receivable
        assert Decimal(info['payables']['opening']) == payable


def test_supported_bank_layouts_parse_with_the_existing_parser(packs):
    parsed_layouts = set()
    for pack in packs.values():
        info = summary(pack)
        parser = info['bank']['parser']
        if parser is None:
            continue
        expected = defaultdict(dict)
        for row in rows(pack / 'expected_matches.csv'):
            expected[row['bank_file']][int(row['source_row'])] = Decimal(row['amount'])
        for month in info['bank']['months']:
            content = (pack / month['file']).read_bytes()
            parsed, _ = parse_bank_csv(content, parser['mapping'], parser['date_format'], 'tenant-test', 'bank-test')
            assert [r['status'] for r in parsed] == ['imported'] * len(parsed)
            assert {r['source_row']: r['signed_amount_decimal'] for r in parsed} == expected[month['file']]
        parsed_layouts.add(info['bank']['layout'])
    assert len(parsed_layouts) >= 2


def test_bank_balances_reconcile(packs):
    for pack in packs.values():
        info = summary(pack)
        balance = Decimal(info['bank']['opening_balance'])
        for month in info['bank']['months']:
            assert Decimal(month['opening_balance']) == balance
            bank_rows = company_pack.read_bank_file(pack / month['file'], info['bank']['layout'])
            assert len(bank_rows) == month['rows']
            for row in bank_rows:
                balance += row['amount']
                assert row['balance'] == balance
                assert row['posted_date'].strftime('%Y-%m') == month['month']
                assert balance > 0
            money_in = sum(r['amount'] for r in bank_rows if r['amount'] > 0)
            money_out = -sum(r['amount'] for r in bank_rows if r['amount'] < 0)
            assert Decimal(month['opening_balance']) + money_in - money_out == Decimal(month['closing_balance']) == balance
            assert (money_in, money_out) == (Decimal(month['money_in']), Decimal(month['money_out']))
        assert balance == Decimal(info['bank']['closing_balance'])


def test_bank_files_carry_no_bank_branding(packs):
    for pack in packs.values():
        text = ' '.join(path.read_text(encoding='utf-8').casefold() for path in pack.glob('*.csv'))
        text += (pack / 'pack.json').read_text(encoding='utf-8').casefold()
        for name in REAL_BANK_NAMES:
            assert f' {name} ' not in f' {text} '.replace(',', ' ').replace('"', ' '), name


def test_expected_matches_point_at_real_rows_and_documents(packs):
    for key, pack in packs.items():
        info = summary(pack)
        documents = {row['document_id']: row for row in rows(pack / 'documents.csv')}
        opening = {row['item_id'] for row in rows(pack / 'opening_balances.csv') if row['item_id']}
        bank_rows = {}
        for month in info['bank']['months']:
            for number, row in enumerate(company_pack.read_bank_file(pack / month['file'], info['bank']['layout']), 1):
                bank_rows[(month['file'], number)] = row
        matches = rows(pack / 'expected_matches.csv')
        assert sorted((m['bank_file'], int(m['source_row'])) for m in matches) == sorted(bank_rows)
        types = defaultdict(list)
        for match in matches:
            row = bank_rows[(match['bank_file'], int(match['source_row']))]
            assert row['amount'] == Decimal(match['amount'])
            assert row['posted_date'] == date.fromisoformat(match['posted_date'])
            lag = (row['posted_date'] - date.fromisoformat(match['payment_date'])).days
            assert 0 <= lag <= 5
            assert match['category'] in {'document', 'fee', 'interest', 'unmatched'}
            ids = match['document_ids'].split(';') if match['document_ids'] else []
            allocations = [Decimal(a) for a in match['allocated_amounts'].split(';')] if ids else []
            if match['category'] == 'document':
                assert ids and set(ids) <= set(documents) | opening, match
                assert sum(allocations) == Decimal(match['amount'])
                types[match['match_type']].append((ids, Decimal(match['amount'])))
            else:
                assert not ids
        assert {'exact', 'partial', 'combined', 'refund', 'net_of_credit_note', 'with_debit_note', 'card'} <= set(types), key
        assert any(len(ids) == 2 and value < 0 and all(documents[i]['kind'] == 'supplier_invoice' for i in ids)
                   for ids, value in types['combined']), 'one transfer paying two supplier invoices'
        assert all(documents[ids[0]]['kind'] == 'credit_note' and value > 0 for ids, value in types['refund'])
        categories = info['counts']['match_categories']
        assert categories['fee'] >= 3 and categories['interest'] == 3 and categories['unmatched'] >= 3
        references = {row['reference'] or row['description'] for row in bank_rows.values()}
        assert len(references) > len(bank_rows) // 2  # varied references, not one template


def test_open_items_after_month_three_follow_the_matches(packs):
    for pack in packs.values():
        info = summary(pack)
        totals, sides = {}, {}
        for row in rows(pack / 'opening_balances.csv'):
            if row['row_type'] in {'receivable', 'payable'}:
                totals[row['item_id']] = amount(row['debit']) + amount(row['credit'])
                sides[row['item_id']] = row['row_type']
        for row in rows(pack / 'documents.csv'):
            if row['kind'] == 'receipt':
                continue
            sign = -1 if row['kind'] == 'credit_note' else 1
            totals[row['document_id']] = sign * amount(row['total'])
            sides[row['document_id']] = 'receivable' if row['direction'] == 'sales' else 'payable'
        settled = defaultdict(Decimal)
        for match in rows(pack / 'expected_matches.csv'):
            if match['document_ids']:
                for doc_id, value in zip(match['document_ids'].split(';'), match['allocated_amounts'].split(';')):
                    settled[doc_id] += abs(Decimal(value))
        outstanding = {}
        for item_id, total in totals.items():
            sign = -1 if total < 0 else 1
            assert settled[item_id] <= abs(total), item_id  # never over-allocated
            outstanding[item_id] = sign * (abs(total) - settled[item_id])
        for side, key in (('receivable', 'receivables'), ('payable', 'payables')):
            closing = sum(v for item_id, v in outstanding.items() if sides[item_id] == side)
            assert closing == Decimal(info[key]['closing']), side
        listed = {item['item_id']: Decimal(item['outstanding']) for item in info['open_items_closing']}
        assert listed == {item_id: value for item_id, value in outstanding.items() if value}
        opening_ids = [item_id for item_id in totals if '-OB-' in item_id]
        assert all(settled[item_id] > 0 for item_id in opening_ids)  # opening items get paid
