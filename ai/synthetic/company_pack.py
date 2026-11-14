"""Company packs: three months of books for fictional Malaysian SMEs (plan v2, task A1).

usage:
  python ai/synthetic/company_pack.py [--company trading|services|fnb|all] [--seed N] [--start YYYY-MM]
                                      [--bank-layout signed|debit_credit|dr_cr] [--out ai/synthetic/output/packs]

Writes, per company, ai/synthetic/output/packs/<company>/ with
    contacts.csv            suppliers and customers (SSM, TIN, SST, address, terms, default account)
    coa.csv                 Malaysian SME chart of accounts (about 60 accounts)
    opening_balances.csv    balanced trial balance at the start of month 1 plus the open receivable and
                            payable items behind the two control accounts
    documents/<YYYY-MM>/    sales invoices, supplier invoices, rent, utility bills, receipts, credit and
                            debit notes: clean PDFs and degraded JPG scans
    invoices.json, bills.json, receipts.json   gold labels in the frozen ground-truth manifest schema
                            (checked with ai.evaluation.dataset_contract.validate_collection)
    documents.csv           one row per document: kind, contact, number, dates, amounts, account code, file
    bank_<YYYY-MM>.csv      the bank statement for each month in one of three generic column layouts
    expected_matches.csv    gold reconciliation: bank row -> document id(s) and allocated amounts, or
                            fee / interest / unmatched
    pack.json               summary: seed, generator version, counts, totals, bank and open-item balances

File formats are documented in docs/COMPANY_PACKS.md. Every name, number and address is invented (random
word combinations), there are no logos or bank names, and every page says it is a synthetic sample.
The same seed always gives the same files, byte for byte.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import io
import json
import random
import shutil
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.synthetic.generate import Generator, money, write_manifests  # noqa: E402

GENERATOR_VERSION = 'company-pack/1.0'
DEFAULT_SEED = 2026_11_14
DEFAULT_START = '2026-08'
MONTHS = 3
COMPANIES = ('trading', 'services', 'fnb')
DEFAULT_OUT = ROOT / 'ai' / 'synthetic' / 'output' / 'packs'
ZERO = Decimal('0.00')
NOTICE = 'Synthetic sample: fictional company, contacts and transactions. Not real records.'

# Word pool for invented names; disjoint from the tuning and holdout pools in generate.py.
PACK_NAMES = ['Anggerik', 'Bakawali', 'Cempedak', 'Dedalu', 'Gelugor', 'Jerai', 'Kantan', 'Ketapang', 'Langsat',
              'Nibong', 'Pulasan', 'Senduduk', 'Kesumba', 'Mengkudu', 'Rengas', 'Tualang', 'Pauh', 'Kuini']

# --- chart of accounts --------------------------------------------------------------------------------
# code, name, type (ledger_accounts.account_type), account_class (coa_accounts.account_class),
# statement, report section, normal balance
COA = [
    ('1000', 'Cash in Hand (Petty Cash)', 'asset', 'asset', 'balance_sheet', 'Current assets', 'debit'),
    ('1010', 'Bank - Current Account', 'asset', 'asset', 'balance_sheet', 'Current assets', 'debit'),
    ('1100', 'Trade Receivables', 'asset', 'asset', 'balance_sheet', 'Current assets', 'debit'),
    ('1110', 'Other Receivables', 'asset', 'asset', 'balance_sheet', 'Current assets', 'debit'),
    ('1120', 'Deposits and Prepayments', 'asset', 'asset', 'balance_sheet', 'Current assets', 'debit'),
    ('1140', 'Tax Paid on Purchases (Recorded)', 'asset', 'asset', 'balance_sheet', 'Current assets', 'debit'),
    ('1200', 'Inventory', 'asset', 'asset', 'balance_sheet', 'Current assets', 'debit'),
    ('1300', 'Plant and Machinery', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'debit'),
    ('1301', 'Accumulated Depreciation - Plant and Machinery', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'credit'),
    ('1310', 'Motor Vehicles', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'debit'),
    ('1311', 'Accumulated Depreciation - Motor Vehicles', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'credit'),
    ('1320', 'Office Equipment and Computers', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'debit'),
    ('1321', 'Accumulated Depreciation - Office Equipment', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'credit'),
    ('1330', 'Furniture and Fittings', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'debit'),
    ('1331', 'Accumulated Depreciation - Furniture and Fittings', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'credit'),
    ('1340', 'Renovation', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'debit'),
    ('1341', 'Accumulated Depreciation - Renovation', 'asset', 'asset', 'balance_sheet', 'Non-current assets', 'credit'),
    ('2000', 'Trade Payables', 'liability', 'liability', 'balance_sheet', 'Current liabilities', 'credit'),
    ('2010', 'Other Payables', 'liability', 'liability', 'balance_sheet', 'Current liabilities', 'credit'),
    ('2020', 'Accrued Expenses', 'liability', 'liability', 'balance_sheet', 'Current liabilities', 'credit'),
    ('2030', 'Tax Charged on Sales (Recorded)', 'liability', 'liability', 'balance_sheet', 'Current liabilities', 'credit'),
    ('2040', 'Customer Deposits', 'liability', 'liability', 'balance_sheet', 'Current liabilities', 'credit'),
    ('2050', 'Amount Due to Director', 'liability', 'liability', 'balance_sheet', 'Current liabilities', 'credit'),
    ('2070', 'EPF Payable', 'liability', 'liability', 'balance_sheet', 'Current liabilities', 'credit'),
    ('2080', 'SOCSO and EIS Payable', 'liability', 'liability', 'balance_sheet', 'Current liabilities', 'credit'),
    ('2100', 'Income Tax Payable', 'liability', 'liability', 'balance_sheet', 'Current liabilities', 'credit'),
    ('2200', 'Hire Purchase Payable', 'liability', 'liability', 'balance_sheet', 'Non-current liabilities', 'credit'),
    ('3000', 'Share Capital', 'equity', 'equity', 'balance_sheet', 'Equity', 'credit'),
    ('3100', 'Retained Earnings', 'equity', 'equity', 'balance_sheet', 'Equity', 'credit'),
    ('4000', 'Sales - Goods', 'income', 'revenue', 'profit_and_loss', 'Revenue', 'credit'),
    ('4010', 'Sales - Services', 'income', 'revenue', 'profit_and_loss', 'Revenue', 'credit'),
    ('4020', 'Sales - Food and Beverage', 'income', 'revenue', 'profit_and_loss', 'Revenue', 'credit'),
    ('4090', 'Sales Returns and Allowances', 'income', 'revenue', 'profit_and_loss', 'Revenue', 'debit'),
    ('4100', 'Interest Income', 'income', 'revenue', 'profit_and_loss', 'Other income', 'credit'),
    ('4110', 'Other Income', 'income', 'revenue', 'profit_and_loss', 'Other income', 'credit'),
    ('5000', 'Purchases', 'expense', 'cogs', 'profit_and_loss', 'Cost of sales', 'debit'),
    ('5010', 'Purchase Returns', 'expense', 'cogs', 'profit_and_loss', 'Cost of sales', 'credit'),
    ('5020', 'Carriage Inwards', 'expense', 'cogs', 'profit_and_loss', 'Cost of sales', 'debit'),
    ('5030', 'Subcontractor Costs', 'expense', 'cogs', 'profit_and_loss', 'Cost of sales', 'debit'),
    ('5040', 'Food and Beverage Ingredients', 'expense', 'cogs', 'profit_and_loss', 'Cost of sales', 'debit'),
    ('5050', 'Packaging Materials', 'expense', 'cogs', 'profit_and_loss', 'Cost of sales', 'debit'),
    ('6000', 'Salaries and Wages', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6010', 'Employer EPF Contribution', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6020', 'Employer SOCSO and EIS', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6030', 'Staff Welfare and Refreshments', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6100', 'Rent of Premises', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6110', 'Electricity', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6120', 'Water', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6130', 'Telephone and Internet', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6140', 'Printing and Stationery', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6150', 'Repairs and Maintenance', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6160', 'Cleaning and Pest Control', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6170', 'Travelling and Transport', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6190', 'Professional Fees', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6200', 'Advertising and Promotion', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6210', 'Entertainment', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6220', 'Insurance', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6230', 'Bank Charges', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6240', 'Licences and Permits', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6260', 'Software Subscriptions', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6270', 'Depreciation', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6280', 'Sundry Expenses', 'expense', 'expense', 'profit_and_loss', 'Operating expenses', 'debit'),
    ('6300', 'Hire Purchase Interest', 'expense', 'expense', 'profit_and_loss', 'Finance costs', 'debit'),
    ('6900', 'Income Tax Expense', 'expense', 'expense', 'profit_and_loss', 'Taxation', 'debit'),
]
COA_COLUMNS = ['code', 'name', 'type', 'account_class', 'statement', 'report_section', 'normal_balance']
ACCOUNT_NAMES = {row[0]: row[1] for row in COA}

# --- line-item pools: description, lowest unit price, highest unit price, largest quantity ---------------
BUILDING_GOODS = [
    ('Kertas A4 80gsm (5 rim) / A4 paper 80gsm (5 reams)', 18, 32, 40), ('Dakwat pencetak hitam / Black toner', 85, 260, 10),
    ('Kabel elektrik 2.5mm (90m) / Electrical cable 2.5mm (90m)', 120, 210, 12), ('Kerusi pejabat / Office chair', 180, 450, 8),
    ('Paip PVC 20mm / PVC pipe 20mm', 6, 15, 100), ('Lampu LED 18W / LED lamp 18W', 9, 25, 60),
    ('Cat dinding 18L / Wall paint 18L', 95, 230, 12), ('Simen 50kg / Cement 50kg', 18, 26, 80),
    ('Skru keluli (kotak) / Steel screws (box)', 12, 30, 40), ('Suis soket 13A / 13A switch socket', 8, 18, 50),
    ('Pemutus litar 32A / 32A circuit breaker', 25, 60, 20), ('Jubin lantai 60x60 (kotak) / Floor tiles 60x60 (box)', 35, 70, 40)]
TRANSPORT = [('Sewa lori 3 tan / 3-tonne lorry hire', 250, 600, 4), ('Caj penghantaran / Delivery charge', 40, 150, 6)]
SERVICE_SALES = [
    ('Khidmat perundingan IT / IT consulting (per hour)', 150, 280, 30), ('Penyelenggaraan sistem bulanan / Monthly system maintenance', 1200, 3500, 1),
    ('Pembangunan laman web / Website development milestone', 3000, 9000, 1), ('Latihan pengguna / User training (per day)', 900, 1800, 3),
    ('Lesen perisian / Software licence (per user)', 40, 90, 25), ('Pemasangan rangkaian / Network installation', 800, 2500, 2)]
SUBCONTRACT = [('Pembangun kontrak / Contract developer (per day)', 450, 800, 10), ('Ujian perisian / Software testing (per day)', 350, 600, 6),
               ('Reka bentuk UI / UI design package', 1500, 4000, 1)]
SOFTWARE = [('Langganan awan bulanan / Monthly cloud subscription', 600, 1800, 1),
            ('Langganan e-mel / E-mail subscription (per user)', 15, 30, 20)]
OFFICE = [('Kertas A4 80gsm (5 rim) / A4 paper (5 reams)', 18, 32, 10), ('Dakwat pencetak hitam / Black toner', 85, 260, 4),
          ('Fail lengkung / Lever arch file', 4, 9, 30), ('Pen biru (kotak) / Blue pens (box)', 12, 25, 5)]
CATERING_SALES = [
    ('Bufet makan tengah hari / Lunch buffet (per pax)', 18, 35, 120), ('Kotak nasi lemak / Nasi lemak box', 7, 12, 150),
    ('Kuih-muih / Assorted kuih (per set)', 3, 6, 100), ('Minuman panas / Hot beverages (per pax)', 3, 6, 120),
    ('Caj penghantaran dan susun atur / Delivery and setup', 80, 250, 1)]
INGREDIENTS = [
    ('Beras wangi 10kg / Fragrant rice 10kg', 32, 45, 20), ('Ayam segar (kg) / Fresh chicken (kg)', 9, 13, 80),
    ('Minyak masak 5kg / Cooking oil 5kg', 28, 40, 12), ('Sayur campuran (kg) / Mixed vegetables (kg)', 4, 9, 60),
    ('Telur gred A (papan 30) / Grade A eggs (tray of 30)', 12, 18, 20), ('Santan segar (kg) / Fresh coconut milk (kg)', 8, 14, 20),
    ('Ikan bilis (kg) / Anchovies (kg)', 25, 45, 10), ('Susu pekat (tin) / Condensed milk (can)', 3, 5, 48),
    ('Serbuk kopi 1kg / Ground coffee 1kg', 25, 45, 10), ('Gula pasir 1kg / Sugar 1kg', 2, 4, 40)]
PACKAGING = [('Bekas makanan kertas (100) / Paper food boxes (100)', 25, 45, 20), ('Cawan kertas 12oz (50) / Paper cups 12oz (50)', 10, 18, 30),
             ('Beg plastik (pek) / Carrier bags (pack)', 6, 12, 20)]
CLEANING = [('Khidmat pembersihan dapur bulanan / Monthly kitchen cleaning', 600, 1200, 1),
            ('Rawatan kawalan perosak / Pest control treatment', 180, 350, 1)]
PROFESSIONAL = [('Yuran setiausaha syarikat / Company secretarial fee', 600, 1500, 1),
                ('Yuran penyediaan akaun / Accounts preparation fee', 1500, 3500, 1)]

UTILITY_SPECS = {  # subtype -> (default account, printed tax rate, consumption range)
    'Electricity': ('6110', 8, None), 'Water': ('6120', 0, None), 'Internet': ('6130', 6, None)}

PROFILES = {
    'trading': {
        'prefix': 'TRD', 'trade': 'Trading', 'sector': 'Building and office supplies trading', 'layout': 'signed',
        'income': '4000', 'sales_rate': 10, 'sales_pool': BUILDING_GOODS, 'sales_scale': Decimal('1.35'), 'sales_lines': (2, 6),
        'sales_per_month': (6, 7), 'customers': 6, 'customer_terms': [30, 30, 45, 60],
        'customer_trades': ['Pembinaan', 'Kejuruteraan', 'Elektrikal', 'Renovation', 'Interior'],
        'suppliers': [
            {'category': 'trade', 'count': 3, 'trades': ['Supplies', 'Hardware', 'Elektrikal'], 'account': '5000', 'rate': 10,
             'pool': BUILDING_GOODS, 'lines': (2, 6), 'per_month': (1, 2), 'terms': [30, 30, 60]},
            {'category': 'trade', 'count': 1, 'trades': ['Logistik'], 'account': '5020', 'rate': 6,
             'pool': TRANSPORT, 'lines': (1, 2), 'per_month': (1, 1), 'terms': [30]}],
        'rent': (4200, 5600), 'usage': {'Electricity': (900, 2600), 'Water': (20, 90)},
        'salaries': (11000, 14000), 'receipts_per_month': 4, 'receipt_account': '6030', 'fee': Decimal('10.00'),
        'opening_bank': (40, 70),
        'opening': {'1000': (600, 900), '1200': (45000, 70000), '1310': (128000, 128000), '1311': (-51200, -51200),
                    '1320': (18500, 18500), '1321': (-7400, -7400), '2020': (-3600, -2800), '2030': (-5000, -2500),
                    '2050': (-30000, -15000), '2200': (-62000, -62000)},
    },
    'services': {
        'prefix': 'SVC', 'trade': 'Digital Services', 'sector': 'IT consulting and support services', 'layout': 'debit_credit',
        'income': '4010', 'sales_rate': 8, 'sales_pool': SERVICE_SALES, 'sales_scale': Decimal('1'), 'sales_lines': (1, 3),
        'sales_per_month': (5, 6), 'customers': 5, 'customer_terms': [14, 30, 30],
        'customer_trades': ['Logistik', 'Perabot', 'Printing', 'Catering', 'Trading'],
        'suppliers': [
            {'category': 'trade', 'count': 2, 'trades': ['Teknologi', 'Solutions'], 'account': '5030', 'rate': 8,
             'pool': SUBCONTRACT, 'lines': (1, 2), 'per_month': (1, 1), 'terms': [30]},
            {'category': 'trade', 'count': 1, 'trades': ['Cloud'], 'account': '6260', 'rate': 8,
             'pool': SOFTWARE, 'lines': (1, 2), 'per_month': (1, 1), 'terms': [14]},
            {'category': 'trade', 'count': 1, 'trades': ['Stationery'], 'account': '6140', 'rate': 10,
             'pool': OFFICE, 'lines': (2, 4), 'per_month': (1, 1), 'terms': [30]}],
        'rent': (5500, 7000), 'usage': {'Electricity': (400, 1200), 'Water': (10, 40)},
        'salaries': (14000, 17000), 'receipts_per_month': 4, 'receipt_account': '6030', 'fee': Decimal('12.00'),
        'opening_bank': (50, 80),
        'opening': {'1000': (400, 700), '1320': (52000, 52000), '1321': (-23400, -23400), '1330': (24000, 24000),
                    '1331': (-9600, -9600), '2020': (-5000, -4000), '2030': (-7000, -4000), '2050': (-20000, -10000)},
    },
    'fnb': {
        'prefix': 'FNB', 'trade': 'Kafe Katering', 'sector': 'Cafe and corporate catering', 'layout': 'dr_cr',
        'income': '4020', 'sales_rate': 6, 'sales_pool': CATERING_SALES, 'sales_scale': Decimal('1'), 'sales_lines': (2, 4),
        'sales_per_month': (3, 4), 'customers': 4, 'customer_terms': [14, 14, 30],
        'customer_trades': ['Kejuruteraan', 'Teknologi', 'Logistik', 'Printing'],
        'suppliers': [
            {'category': 'trade', 'count': 3, 'trades': ['Pemborong Makanan', 'Ayam Segar', 'Sayur Segar'], 'account': '5040',
             'rate': 0, 'pool': INGREDIENTS, 'lines': (3, 7), 'per_month': (2, 2), 'terms': [14, 30]},
            {'category': 'trade', 'count': 1, 'trades': ['Packaging'], 'account': '5050', 'rate': 10,
             'pool': PACKAGING, 'lines': (2, 3), 'per_month': (1, 1), 'terms': [30]},
            {'category': 'trade', 'count': 1, 'trades': ['Cleaning Services'], 'account': '6160', 'rate': 8,
             'pool': CLEANING, 'lines': (1, 2), 'per_month': (1, 1), 'terms': [14]}],
        'rent': (7000, 9000), 'usage': {'Electricity': (2500, 5000), 'Water': (60, 200)},
        'salaries': (16000, 19000), 'receipts_per_month': 4, 'receipt_account': '5040', 'fee': Decimal('8.00'),
        'opening_bank': (30, 50),
        'opening': {'1000': (1200, 1800), '1200': (6000, 9000), '1300': (88000, 88000), '1301': (-26400, -26400),
                    '1330': (30000, 30000), '1331': (-12000, -12000), '1340': (125000, 125000), '1341': (-37500, -37500),
                    '2020': (-3000, -2500), '2030': (-5000, -3000), '2050': (-40000, -20000)},
    },
}

# --- bank statement layouts -------------------------------------------------------------------------
BANK_LAYOUTS = {
    'signed': {
        'columns': ['Date', 'Description', 'Reference', 'Amount', 'Balance'],
        'description': 'ISO dates (YYYY-MM-DD), one signed Amount column (money out negative), running Balance.',
        'parser': {'mapping': {'posted_date': 'Date', 'description': 'Description', 'reference': 'Reference',
                               'debit': 'Amount', 'credit': 'Amount'}, 'date_format': 'iso'}},
    'debit_credit': {
        'columns': ['Transaction Date', 'Value Date', 'Transaction Description', 'Ref No', 'Debit', 'Credit', 'Balance'],
        'description': 'DD/MM/YYYY dates, separate Debit and Credit columns with thousands separators, value date.',
        'parser': {'mapping': {'posted_date': 'Transaction Date', 'value_date': 'Value Date',
                               'description': 'Transaction Description', 'reference': 'Ref No',
                               'debit': 'Debit', 'credit': 'Credit'}, 'date_format': 'dmy'}},
    'dr_cr': {
        'columns': ['Posting Date', 'Narrative 1', 'Narrative 2', 'Amount', 'DR/CR', 'Balance', 'Currency'],
        'description': 'DD-MM-YYYY dates, unsigned Amount with a DR/CR flag, two narrative columns and no reference '
                       'column. Not readable by services/bank/csv_parser.py yet (needs saved column mappings, task C5).',
        'parser': None},
}
CHANNEL_TEXT = {
    'in_transfer': ['IBG CREDIT', 'INSTANT TRANSFER FR', 'FUND TRANSFER FR'],
    'out_transfer': ['IBG DEBIT', 'INSTANT TRANSFER TO', 'FUND TRANSFER TO'],
    'bill': ['BILL PAYMENT', 'AUTODEBIT'], 'card': ['CARD PURCHASE', 'DEBIT CARD'],
    'payroll': ['PAYROLL BULK PAYMENT'], 'statutory': ['STATUTORY CONTRIBUTIONS'], 'cash_out': ['CASH WITHDRAWAL ATM'],
    'cash_in': ['CASH DEPOSIT'], 'settlement': ['CARD TERMINAL SETTLEMENT'], 'fee': ['SERVICE CHARGE'],
    'transfer_fee': ['TRANSFER FEES'], 'interest': ['INTEREST CREDIT'],
}
LAGS = [0, 0, 0, 1, 1, 2, 3, 4, 5]
INTEREST_RATE = Decimal('0.0025')  # per year, credited monthly


def add_months(day, count):
    years, month = divmod(day.month - 1 + count, 12)
    return date(day.year + years, month + 1, 1)


def month_end(day):
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def amount_text(value):
    return f'{value:.2f}'


def totals(items, rate):
    subtotal = sum((item['line_total'] for item in items), ZERO)
    tax = money(subtotal * Decimal(rate) / 100)
    return subtotal, tax, subtotal + tax


class CompanyPack(Generator):
    """One fictional company: contacts, chart of accounts, opening balances, three months of documents and
    bank statements, and the gold reconciliation between them. Rendering, degradation and gold labels are
    the Generator's; this class only decides what the documents say and how the money moves."""
    source_file = 'ai/synthetic/company_pack.py'
    dataset = 'ai-synthetic-my-company-pack'
    revision = GENERATOR_VERSION

    def __init__(self, key, seed=DEFAULT_SEED, start=DEFAULT_START, bank_layout=None):
        if key not in PROFILES:
            raise ValueError(f'Unknown company {key!r}; choose one of {", ".join(COMPANIES)}')
        self.key, self.profile, self.seed = key, PROFILES[key], seed
        self.set_name, self.split, self.prefix = key, 'development', PROFILES[key]['prefix']
        self.rng = random.Random(f'{GENERATOR_VERSION}:{seed}:{key}')
        self.names, self.used_names, self.style = PACK_NAMES, set(), 'mixed'
        year, month = (int(part) for part in start.split('-'))
        self.months = [add_months(date(year, month, 1), offset) for offset in range(MONTHS)]
        self.period_start, self.period_end = self.months[0], month_end(self.months[-1])
        self.layout = bank_layout or self.profile['layout']
        if self.layout not in BANK_LAYOUTS:
            raise ValueError(f'Unknown bank layout {self.layout!r}')
        self.events, self.items, self.documents, self.opening_items = [], {}, [], []

    # --- identities ------------------------------------------------------------------------------------
    def identity(self, name, style=None):
        first, second = name.split()[:2]
        return {'name': name, 'address_lines': self.address(), 'ssm': self.ssm(), 'tin': self.tin(), 'sst': self.sst(),
                'phone': self.phone(), 'email': f'akaun@{first.lower()}-{second.lower()}.test',
                'style': style or self.rng.choice(['ms', 'en', 'mixed', 'mixed'])}

    def add_contact(self, kind, category, name, account, terms, rate=None, pool=None, lines=None, per_month=None):
        number = sum(1 for contact in self.contacts if contact['type'] == kind) + 1
        contact = {'contact_id': f'{self.prefix}-{"C" if kind == "customer" else "S"}{number:02d}', 'type': kind,
                   'category': category, 'payment_terms_days': terms, 'default_account_code': account, 'rate': rate,
                   'pool': pool, 'lines': lines, 'per_month': per_month, **self.identity(name)}
        fmt = self.rng.choice(['SI-{yymm}-{n:04d}', 'INV/{yy}/{n:05d}', 'A{n:07d}', '{yy}{mm}{n:04d}', 'IV{n:06d}'])
        contact.update(number_format=fmt, counter=self.rng.randint(100, 4000), account_no=self.digits(12))
        self.contacts.append(contact)
        return contact

    def build_contacts(self):
        profile = self.profile
        self.own = self.identity(self.company_name(profile['trade'], 'Sdn. Bhd.'), style='mixed')
        self.own.update(sector=profile['sector'], bank_account=self.digits(12))
        self.contacts = []
        for _ in range(profile['customers']):
            name = self.company_name(self.rng.choice(profile['customer_trades']))
            self.add_contact('customer', 'customer', name, profile['income'], self.rng.choice(profile['customer_terms']))
        for spec in profile['suppliers']:
            for index in range(spec['count']):
                name = self.company_name(spec['trades'][index % len(spec['trades'])])
                self.add_contact('supplier', spec['category'], name, spec['account'], self.rng.choice(spec['terms']),
                                 spec['rate'], spec['pool'], spec['lines'], spec['per_month'])
        self.landlord = self.add_contact('supplier', 'rent', self.company_name('Properties', 'Sdn. Bhd.'), '6100', 7, 8)
        self.utilities = {}
        for subtype, (account, rate, _) in UTILITY_SPECS.items():
            word = {'Electricity': 'Elektrik', 'Water': 'Air', 'Internet': 'Jalur Lebar'}[subtype]
            name = self.company_name(f'{word} {self.rng.choice(["Wilayah", "Bersepadu", "Komuniti"])}',
                                     self.rng.choice(['Sdn. Bhd.', 'Berhad']))
            self.utilities[subtype] = self.add_contact('supplier', 'utility', name, account, 21, rate)
        self.professional = self.add_contact('supplier', 'professional', self.company_name('Corporate Services', 'PLT'),
                                             '6190', 30, 8, PROFESSIONAL, (1, 1))

    def company_name(self, trade, suffix=None):
        return self.company(trade=trade, suffix=suffix)

    # --- documents (data only; rendered later) ---------------------------------------------------------
    def pack_lines(self, pool, count, scale=Decimal('1')):
        items = []
        for description, low, high, most in self.rng.sample(pool, min(count, len(pool))):
            quantity = Decimal(self.rng.randint(1, most))
            unit = money(Decimal(self.rng.randint(low * 100, high * 100)) / 100 * scale)
            items.append({'description': description, 'quantity': quantity, 'unit_price': unit,
                          'line_total': money(quantity * unit)})
        return items

    def new_document(self, kind, code, issued, contact, items, rate, account, **extra):
        month = f'{issued:%Y-%m}'
        sequence = sum(1 for doc in self.documents if doc['month'] == month and doc['code'] == code) + 1
        if kind == 'receipt':
            subtotal, tax, total, rounding = self.receipt_totals(items, rate)
        else:
            (subtotal, tax, total), rounding = totals(items, rate), ZERO
        doc = {'document_id': f'{self.prefix}-{issued:%y%m}-{code}{sequence:02d}', 'kind': kind, 'code': code,
               'month': month, 'issued': issued, 'contact': contact, 'items': items, 'rate': Decimal(rate),
               'subtotal': subtotal, 'tax': tax, 'rounding': rounding, 'total': total, 'account': account, 'related': None, 'po': None,
               'due': None, 'number': None, **extra}
        self.documents.append(doc)
        self.items[doc['document_id']] = doc
        return doc

    def supplier_number(self, contact, issued):
        contact['counter'] += self.rng.randint(1, 40)
        return contact['number_format'].format(yymm=f'{issued:%y%m}', yy=f'{issued:%y}', mm=f'{issued:%m}', n=contact['counter'])

    def build_documents(self):
        profile = self.profile
        customers = [c for c in self.contacts if c['type'] == 'customer']
        trade_suppliers = [c for c in self.contacts if c['category'] == 'trade']
        self.sales_counter = self.rng.randint(120, 400)
        self.note_counter = self.rng.randint(10, 60)
        self.build_opening_items(customers, trade_suppliers)
        for index, month in enumerate(self.months):
            last = month_end(month).day
            sales = sorted(month.replace(day=self.rng.randint(1, last)) for _ in range(self.rng.randint(*profile['sales_per_month'])))
            for issued in sales:
                customer = self.rng.choice(customers)
                items = self.pack_lines(profile['sales_pool'], self.rng.randint(*profile['sales_lines']), profile['sales_scale'])
                self.sales_counter += 1
                po = f'PO-{self.digits(5)}' if self.rng.random() < 0.4 else None
                self.new_document('sales_invoice', 'SI', issued, customer, items, profile['sales_rate'], profile['income'],
                                  number=f'INV-{issued:%y%m}-{self.sales_counter:04d}', po=po,
                                  due=issued + timedelta(days=customer['payment_terms_days']))
            for supplier in trade_suppliers:
                for _ in range(self.rng.randint(*supplier['per_month'])):
                    self.supplier_invoice(supplier, month.replace(day=self.rng.randint(1, last)))
            self.supplier_invoice(self.landlord, month, items=[self.rent_line(month)])
            if index == MONTHS - 1:
                self.supplier_invoice(self.professional, month.replace(day=self.rng.randint(5, 20)))
            for subtype, contact in self.utilities.items():
                self.utility_bill(subtype, contact, month.replace(day=self.rng.randint(3, 8)))
            for _ in range(profile['receipts_per_month']):
                self.receipt_document(month.replace(day=self.rng.randint(1, last)))

    def rent_line(self, month):
        if not hasattr(self, 'rent'):
            self.rent = money(Decimal(self.rng.randint(*self.profile['rent']) // 100 * 100))
        return {'description': f'Sewa premis / Premises rent {month:%b %Y}', 'quantity': Decimal(1),
                'unit_price': self.rent, 'line_total': self.rent}

    def supplier_invoice(self, supplier, issued, items=None):
        items = items or self.pack_lines(supplier['pool'], self.rng.randint(*supplier['lines']))
        po = f'PO-{self.digits(5)}' if supplier['category'] == 'trade' and self.rng.random() < 0.3 else None
        return self.new_document('supplier_invoice', 'PI', issued, supplier, items, supplier['rate'],
                                 supplier['default_account_code'], number=self.supplier_number(supplier, issued), po=po,
                                 due=issued + timedelta(days=supplier['payment_terms_days']))

    def utility_bill(self, subtype, contact, issued):
        usage = self.profile['usage'].get(subtype, (80, 2400))
        items = self.utility_items(subtype, *usage)
        end = issued.replace(day=1) - timedelta(days=1)
        return self.new_document('utility_bill', 'UB', issued, contact, items, contact['rate'],
                                 contact['default_account_code'], number=f'B{self.digits(9)}', subtype=subtype,
                                 start=end.replace(day=1), end=end, due=issued + timedelta(days=21))

    def receipt_document(self, when):
        data = self.receipt_data()
        data.update(when=when, method=self.rng.choices(['card', 'cash', 'e_wallet'], weights=[5, 3, 2])[0])
        return self.new_document('receipt', 'RC', when, None, data['items'], data['rate'],
                                 self.profile['receipt_account'], number=data['number'], receipt=data,
                                 merchant=data['merchant'])

    def build_opening_items(self, customers, suppliers):
        """Unpaid invoices from before month 1: the detail behind the opening receivables and payables."""
        profile = self.profile
        for number, customer in enumerate(self.rng.sample(customers, self.rng.randint(2, 3)), 1):
            issued = self.period_start - timedelta(days=self.rng.randint(10, 45))
            items = self.pack_lines(profile['sales_pool'], self.rng.randint(*profile['sales_lines']), profile['sales_scale'])
            self.opening_items.append({'side': 'receivable', 'issued': issued, 'contact': customer, 'items': items,
                                       'rate': profile['sales_rate']})
        for supplier in self.rng.sample(suppliers, min(len(suppliers), self.rng.randint(2, 3))):
            issued = self.period_start - timedelta(days=self.rng.randint(5, 35))
            items = self.pack_lines(supplier['pool'], self.rng.randint(*supplier['lines']))
            self.opening_items.append({'side': 'payable', 'issued': issued, 'contact': supplier, 'items': items,
                                       'rate': supplier['rate']})
        self.opening_items.sort(key=lambda item: (item['side'], item['issued']))
        counts = {'receivable': 0, 'payable': 0}
        for item in self.opening_items:
            counts[item['side']] += 1
            side = 'AR' if item['side'] == 'receivable' else 'AP'
            _, _, total = totals(item['items'], item['rate'])
            if item['side'] == 'receivable':
                self.sales_counter += 1
                number = f'INV-{item["issued"]:%y%m}-{self.sales_counter:04d}'
                kind = 'opening_receivable'
            else:
                number = self.supplier_number(item['contact'], item['issued'])
                kind = 'opening_payable'
            item.update(document_id=f'{self.prefix}-OB-{side}{counts[item["side"]]:02d}', number=number, total=total,
                        kind=kind, po=None, due=item['issued'] + timedelta(days=item['contact']['payment_terms_days']))
            self.items[item['document_id']] = item

    def note(self, kind, target, issued, items, reason):
        """Credit or debit note against `target` (a sales or supplier invoice)."""
        ours = target['kind'] == 'sales_invoice'
        code = 'CN' if kind == 'credit_note' else 'DN'
        if ours:
            self.note_counter += 1
            number = f'{code}-{issued:%y%m}-{self.note_counter:03d}'
            account = '4090'
        else:
            target['contact']['counter'] += 1
            number = f'{code}-{issued:%y%m}-{target["contact"]["counter"] % 1000:03d}'
            account = '5020' if kind == 'debit_note' else ('5010' if target['account'] == '5000' else target['account'])
        return self.new_document(kind, code, issued, target['contact'], items, target['rate'], account, number=number,
                                 related=target['document_id'], reason=reason, direction='sales' if ours else 'purchase')

    def partial_line(self, target):
        line = max(target['items'], key=lambda item: item['line_total'])
        quantity = max(Decimal(1), (line['quantity'] / 2).to_integral_value())
        return [{'description': line['description'], 'quantity': quantity, 'unit_price': line['unit_price'],
                 'line_total': money(quantity * line['unit_price'])}]

    # --- money movements ------------------------------------------------------------------------------
    def event(self, when, amount, channel, targets=(), category='document', name='', reference='', match_type='',
              note='', lag=None, order=0):
        if lag is None:
            lag = self.rng.choice(LAGS) if channel in {'in_transfer', 'out_transfer', 'bill'} else 0
        event = {'initiated': when, 'posted': when + timedelta(days=lag), 'amount': money(amount), 'channel': channel,
                 'targets': list(targets), 'category': category, 'type_text': self.rng.choice(CHANNEL_TEXT[channel]),
                 'name_text': name, 'reference': reference, 'match_type': match_type, 'note': note, 'order': order}
        self.events.append(event)
        return event

    def bank_name(self, name):
        upper = name.upper()
        plain = upper.replace('SDN. BHD.', 'SDN BHD').replace('BERHAD', 'BHD')
        options = [upper, plain, plain.replace(' SDN BHD', '').replace(' PLT', ''), plain[:20].rstrip(),
                   plain.replace('SDN BHD', 'S/B'), plain.replace('TRADING', 'TRDG').replace('ENTERPRISE', 'ENT')]
        return self.rng.choice(options)

    def reference(self, targets, incoming, when):
        numbers = [self.items[target]['number'] for target in targets]
        if len(numbers) > 1:
            options = [' '.join(numbers), '/'.join(n.replace('/', '-').split('-')[-1] for n in numbers), f'{len(numbers)} INV', '']
        elif incoming:
            item = self.items[targets[0]]
            options = [numbers[0], numbers[0], numbers[0].replace('-', ''), numbers[0].split('-')[-1],
                       f'PAYMENT {when:%b %Y}'.upper(), ''] + ([item['po']] if item.get('po') else [])
        else:
            options = [numbers[0], f'PAY {numbers[0]}', numbers[0].replace('-', '').replace('/', ''),
                       f'{when:%b%y} PYMT'.upper(), '']
        return self.rng.choice(options)

    def installments(self, item, payable):
        """Payment plan of an open item: (date, amount, match type) tuples, some beyond the period."""
        total, due, issued = item['total'], item['due'], item['issued']
        roll = self.rng.random()
        opening = item['kind'].startswith('opening')
        if total < 1000 and not opening and roll < 0.9:
            roll = 0  # small invoices are paid in one go
        if item.get('plan') == 'full' or (opening and roll < 0.7) or (not opening and roll < (0.8 if payable else 0.7)):
            when = item.get('pay_on') or due + timedelta(days=self.rng.randint(-6, 4) if payable else self.rng.randint(-7, 12))
            plan = [(max(when, issued + timedelta(days=2)), total, 'exact')]
        elif opening or roll < (0.9 if payable else 0.85):
            first = max(due + timedelta(days=self.rng.randint(-3, 8)), issued + timedelta(days=3))
            part = money(Decimal(int(total * Decimal(self.rng.choice(['0.4', '0.5', '0.6'])))))
            plan = [(first, part, 'partial'), (first + timedelta(days=self.rng.randint(14, 30)), total - part, 'partial')]
        else:
            plan = [(due + timedelta(days=self.rng.randint(30, 60)), total, 'exact')]
        if opening:  # opening items are paid inside the pack period
            plan = [(max(when, self.period_start + timedelta(days=self.rng.randint(0, 6))), amount, kind)
                    for when, amount, kind in plan]
        return plan

    def build_payments(self):
        month1 = [doc for doc in self.documents if doc['month'] == f'{self.months[0]:%Y-%m}']
        sales1 = [doc for doc in month1 if doc['kind'] == 'sales_invoice']
        trade1 = [doc for doc in month1 if doc['kind'] == 'supplier_invoice' and doc['contact']['category'] == 'trade']
        used = set()

        # Customer credit note: the customer pays the invoice less the note in one transfer.
        target = sales1[0]
        cn_date = target['issued'] + timedelta(days=self.rng.randint(4, 12))
        customer_cn = self.note('credit_note', target, cn_date, self.partial_line(target),
                                self.rng.choice(['Barang rosak / Damaged goods', 'Diskaun tambahan / Additional discount']))
        target.update(plan='full', pay_on=max(target['due'], cn_date + timedelta(days=3)))
        used.add(target['document_id'])

        # Supplier debit note: paid together with its invoice.
        dn_target = trade1[0]
        dn_date = dn_target['issued'] + timedelta(days=self.rng.randint(2, 7))
        charge = money(Decimal(self.rng.randint(80, 250)))
        supplier_dn = self.note('debit_note', dn_target, dn_date,
                                [{'description': 'Caj penghantaran tambahan / Additional delivery charge',
                                  'quantity': Decimal(1), 'unit_price': charge, 'line_total': charge}],
                                'Caj penghantaran tambahan / Additional delivery charge')
        dn_target.update(plan='full', pay_on=dn_date + timedelta(days=self.rng.randint(2, 20)))
        used.add(dn_target['document_id'])

        # Supplier credit note after payment: the supplier refunds it.
        refund_target = trade1[1]
        refund_target.update(plan='full', pay_on=refund_target['issued'] + timedelta(days=self.rng.randint(7, 12)))
        used.add(refund_target['document_id'])
        paid = refund_target['pay_on'] + timedelta(days=5)
        supplier_cn = self.note('credit_note', refund_target, paid + timedelta(days=self.rng.randint(3, 8)),
                                self.partial_line(refund_target), 'Pulangan barang / Goods returned')

        open_items = [item for item in self.opening_items] + [
            doc for doc in self.documents if doc['kind'] in {'sales_invoice', 'supplier_invoice'}]
        plans = {}
        for item in sorted(open_items, key=lambda i: (i['issued'], i['document_id'])):
            payable = item['kind'] in {'supplier_invoice', 'opening_payable'}
            if item['contact'] is self.landlord:
                item.update(plan='full', pay_on=item['issued'] + timedelta(days=self.rng.randint(0, 4)))
            plans[item['document_id']] = self.installments(item, payable)

        # One transfer paying two invoices of the same supplier, and one receipt covering two invoices.
        for payable in (True, False):
            pair = self.combined_pair(open_items, plans, used, payable)
            if pair:
                first, second = pair
                when = max(plans[first['document_id']][0][0], plans[second['document_id']][0][0])
                for item in pair:
                    plans[item['document_id']] = []
                    used.add(item['document_id'])
                sign = -1 if payable else 1
                ids = [first['document_id'], second['document_id']]
                self.event(when, sign * (first['total'] + second['total']), 'out_transfer' if payable else 'in_transfer',
                           [(doc_id, sign * self.items[doc_id]['total']) for doc_id in ids],
                           name=self.bank_name(first['contact']['name']), reference=self.reference(ids, not payable, when),
                           match_type='combined', note='One transfer for two invoices of the same contact')

        for item in open_items:
            doc_id = item['document_id']
            payable = item['kind'] in {'supplier_invoice', 'opening_payable'}
            sign = -1 if payable else 1
            channel = 'out_transfer' if payable else 'in_transfer'
            for when, amount, kind in plans[doc_id]:
                targets, note = [(doc_id, sign * amount)], 'Pays the invoice in full'
                if doc_id == target['document_id']:
                    targets.append((customer_cn['document_id'], -customer_cn['total']))
                    kind, note = 'net_of_credit_note', 'Invoice less the credit note'
                elif doc_id == dn_target['document_id']:
                    targets.append((supplier_dn['document_id'], -supplier_dn['total']))
                    kind, note = 'with_debit_note', 'Invoice plus the debit note'
                elif kind == 'partial':
                    note = 'Part payment'
                self.event(when, sum(amount for _, amount in targets), channel, targets,
                           name=self.bank_name(item['contact']['name']),
                           reference=self.reference([t for t, _ in targets[:1]], not payable, when),
                           match_type=kind, note=note)
        refund_day = supplier_cn['issued'] + timedelta(days=self.rng.randint(5, 10))
        self.event(refund_day, supplier_cn['total'], 'in_transfer', [(supplier_cn['document_id'], supplier_cn['total'])],
                   name=self.bank_name(supplier_cn['contact']['name']), reference=f'REFUND {supplier_cn["number"]}',
                   match_type='refund', note='Supplier refunds the credit note')

        for doc in self.documents:
            if doc['kind'] == 'utility_bill':
                when = max(doc['issued'] + timedelta(days=1), doc['due'] - timedelta(days=self.rng.randint(0, 10)))
                self.event(when, -doc['total'], 'bill', [(doc['document_id'], -doc['total'])],
                           name=self.bank_name(doc['contact']['name']),
                           reference=self.rng.choice([doc['contact']['account_no'], doc['number']]),
                           match_type='exact', note='Utility bill payment')
            elif doc['kind'] == 'receipt' and doc['receipt']['method'] == 'card':
                self.event(doc['issued'], -doc['total'], 'card', [(doc['document_id'], -doc['total'])],
                           name=self.bank_name(doc['merchant'])[:22].rstrip(), reference=f'AUTH {self.digits(6)}',
                           match_type='card', note='Card payment of a receipt', lag=self.rng.randint(1, 3))

    def combined_pair(self, open_items, plans, used, payable):
        kinds = {'supplier_invoice'} if payable else {'sales_invoice'}
        by_contact = {}
        for item in open_items:
            if item['kind'] in kinds and item['document_id'] not in used and item['contact'] is not self.landlord:
                by_contact.setdefault(item['contact']['contact_id'], []).append(item)
        for contact_id in sorted(by_contact):
            group = sorted(by_contact[contact_id], key=lambda i: i['issued'])
            for first, second in zip(group, group[1:]):
                when = second['due'] + timedelta(days=self.rng.randint(-3, 3))
                if when + timedelta(days=5) <= self.period_end:
                    plans[first['document_id']] = plans[second['document_id']] = [(when, None, 'exact')]
                    return first, second
        return None

    def build_noise(self):
        profile = self.profile
        for index, month in enumerate(self.months):
            end = month_end(month)
            salary = money(Decimal(self.rng.randint(*profile['salaries'])))
            self.event(end - timedelta(days=self.rng.randint(2, 5)), -salary, 'payroll', category='unmatched',
                       reference=f'SALARY {month:%b%y}'.upper(), note='Payroll (no document in the pack)')
            self.event(month.replace(day=15), -money(salary * Decimal('0.15')), 'statutory', category='unmatched',
                       reference=f'{add_months(month, -1):%b%y}'.upper(), note='Statutory contributions (no document)')
            self.event(month.replace(day=self.rng.randint(2, 26)), -Decimal(self.rng.choice([300, 500, 800])), 'cash_out',
                       category='unmatched', note='Petty cash top-up')
            if self.key == 'trading' and index == 1:
                self.event(month.replace(day=self.rng.randint(3, 10)), Decimal(self.rng.choice([10000, 15000])), 'in_transfer',
                           category='unmatched', name='DIRECTOR', reference='ADVANCE', note='Director advance (no document)')
            if self.key == 'services' and index == 1:
                self.event(month.replace(day=self.rng.randint(5, 25)), money(Decimal(self.rng.randint(15000, 90000)) / 100),
                           'in_transfer', category='unmatched', name=self.bank_name(self.company_name('Resources')),
                           note='Unidentified deposit: ask the customer')
            if self.key == 'fnb':
                for day in (7, 14, 21, 28):
                    self.event(month.replace(day=day), money(Decimal(self.rng.randint(900000, 1600000)) / 100), 'settlement',
                               category='unmatched', reference=f'MID {self.digits(8)}',
                               note='Card terminal settlement of daily sales (no document)', lag=self.rng.randint(1, 2))
                for _ in range(2):
                    self.event(month.replace(day=self.rng.randint(2, 27)), money(Decimal(self.rng.randint(200000, 400000)) / 100),
                               'cash_in', category='unmatched', note='Cash takings banked (no document)')

    def build_bank(self):
        """Keep the rows posted inside the period, add fees and interest, set the opening balance and
        the running balance."""
        self.events = [e for e in self.events if self.period_start <= e['posted'] <= self.period_end]
        for month in self.months:
            end = month_end(month)
            self.event(end, -self.profile['fee'], 'fee', category='fee', note='Monthly account fee', order=1)
            transfers = sum(1 for e in self.events if e['channel'] == 'out_transfer' and e['posted'].month == month.month)
            if transfers:
                self.event(end, -money(Decimal('0.10') * transfers), 'transfer_fee', category='fee',
                           note=f'Fees for {transfers} outgoing transfers', order=1)
        for sequence, event in enumerate(self.events):
            event['sequence'] = sequence
        self.events.sort(key=lambda e: (e['posted'], e['order'], e['sequence']))
        running, lowest = ZERO, ZERO
        for event in self.events:
            running += event['amount']
            lowest = min(lowest, running)
        floor = Decimal(self.rng.randint(*self.profile['opening_bank']) * 1000)
        self.opening_bank = money(max(floor, ((-lowest + 10000) / 1000).to_integral_value() * 1000))
        rows, balance = [], self.opening_bank
        by_month = {f'{month:%Y-%m}': [] for month in self.months}
        for event in self.events:
            by_month[f'{event["posted"]:%Y-%m}'].append(event)
        for month in self.months:
            key = f'{month:%Y-%m}'
            month_rows = by_month[key]
            # Interest is credited last on the final day of the month, on the balance at that point.
            for event in month_rows:
                balance += event['amount']
            interest = money(balance * INTEREST_RATE / 12)
            interest_event = {'initiated': month_end(month), 'posted': month_end(month), 'amount': interest,
                              'channel': 'interest', 'targets': [], 'category': 'interest',
                              'type_text': CHANNEL_TEXT['interest'][0], 'name_text': '', 'reference': '',
                              'match_type': '', 'note': 'Monthly interest', 'order': 2}
            month_rows.append(interest_event)
            balance += interest
        self.bank_months = by_month
        self.closing_bank = balance

    # --- opening trial balance ---------------------------------------------------------------------------
    def build_opening_balances(self):
        balances = {}
        for code, (low, high) in self.profile['opening'].items():
            balances[code] = money(Decimal(self.rng.randint(low, high)) // 100 * 100)
        balances['1010'] = self.opening_bank
        balances['1120'] = self.rent * 3
        balances['1100'] = sum((i['total'] for i in self.opening_items if i['side'] == 'receivable'), ZERO)
        balances['2000'] = -sum((i['total'] for i in self.opening_items if i['side'] == 'payable'), ZERO)
        net = sum(balances.values(), ZERO)
        if net <= 0:
            raise RuntimeError('Opening net assets must be positive')
        share = max(Decimal(10000), (net / 2 / 10000).to_integral_value(rounding='ROUND_FLOOR') * 10000)
        balances['3000'] = -money(share)
        balances['3100'] = -(net - money(share))
        self.opening_balances = {code: balances[code] for code, *_ in COA if code in balances}

    # --- rendering and files --------------------------------------------------------------------------------
    def render(self, doc, out):
        kind = doc['kind']
        ours = kind == 'sales_invoice' or doc.get('direction') == 'sales'
        contact = doc['contact']
        self.style = self.own['style'] if ours else (contact['style'] if contact else self.rng.choice(['ms', 'en', 'mixed']))
        if kind == 'utility_bill':
            page, labels, items, subtype = self.render_bill({
                'subtype': doc['subtype'], 'provider': contact['name'], 'provider_address': contact['address_lines'],
                'customer': self.own['name'], 'service_address': self.own['address_lines'],
                'account': contact['account_no'], 'issued': doc['issued'], 'start': doc['start'], 'end': doc['end'],
                'due': doc['due'], 'number': doc['number'], 'items': doc['items'], 'tax': doc['tax'], 'arrears': ZERO,
                'provider_ssm': contact['ssm'], 'provider_sst': contact['sst'], 'phone': contact['phone'],
                'email': contact['email'], 'stamp': False,
                'payment_line': f'{self.labels("Rujukan bayaran", "Payment reference")}: {contact["account_no"]}'})
            manifest_kind, scan_share = 'bill', 0.35
        elif kind == 'receipt':
            page, labels, items = self.render_receipt(doc['receipt'])
            subtype, manifest_kind, scan_share = None, 'receipt', 0.8
        else:
            seller, buyer = (self.own, contact) if ours else (contact, self.own)
            render_kind = {'sales_invoice': 'invoice', 'supplier_invoice': 'invoice'}.get(kind, kind)
            spec = {'rate': doc['rate'], 'supplier': seller['name'], 'supplier_address': seller['address_lines'],
                    'customer': buyer['name'], 'customer_address': buyer['address_lines'], 'ssm': seller['ssm'],
                    'sst': seller['sst'], 'tin': seller['tin'], 'issued': doc['issued'], 'number': doc['number'],
                    'items': doc['items'], 'stamp': False, 'phone': seller['phone'], 'email': seller['email']}
            if render_kind == 'invoice':
                spec.update(terms=contact['payment_terms_days'], po=doc['po'])
            else:
                spec.update(original=self.items[doc['related']]['number'], reason=doc['reason'])
            page, labels, items = self.render_invoice_like(render_kind, spec)
            subtype, manifest_kind, scan_share = None, render_kind, 0.15 if ours else 0.5
        scan = self.rng.random() < scan_share
        folder = f'documents/{doc["month"]}'
        (out / folder).mkdir(parents=True, exist_ok=True)
        manifest_type, sample = self.package(doc['document_id'], manifest_kind, page, labels, items, subtype, out, scan,
                                             folder=folder)
        doc.update(file=sample['source']['relative_path'], file_format=sample['source']['file_format'],
                   manifest=manifest_type)
        return manifest_type, sample

    def build(self, out):
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        if (out / 'documents').exists():
            shutil.rmtree(out / 'documents')
        for stale in out.glob('bank_*.csv'):
            stale.unlink()
        self.build_contacts()
        self.build_documents()
        self.build_payments()
        self.build_noise()
        self.build_bank()
        self.build_opening_balances()
        self.documents.sort(key=lambda d: (d['issued'], d['document_id']))
        samples = {'invoice': [], 'bill': [], 'receipt': []}
        for doc in self.documents:
            manifest_type, sample = self.render(doc, out)
            samples[manifest_type].append(sample)
        _, self.manifests = write_manifests(out, f'company-pack-{self.key}', GENERATOR_VERSION, samples)
        self.write_files(out)
        return out

    def write_csv(self, path, header, rows):
        buffer = io.StringIO(newline='')
        writer = csv.writer(buffer, lineterminator='\n')
        writer.writerow(header)
        writer.writerows(rows)
        path.write_text(buffer.getvalue(), encoding='utf-8', newline='')

    def write_files(self, out):
        self.write_csv(out / 'coa.csv', COA_COLUMNS, COA)
        self.write_csv(out / 'contacts.csv', CONTACT_COLUMNS, [
            [c['contact_id'], c['type'], c['name'], c['category'], c['ssm'], c['tin'], c['sst'], ', '.join(c['address_lines']),
             c['email'], c['phone'], c['payment_terms_days'], c['default_account_code'], 'MYR'] for c in self.contacts])
        as_at = self.period_start.isoformat()
        rows = []
        for code, balance in self.opening_balances.items():
            rows.append(['account', as_at, code, ACCOUNT_NAMES[code], '', '', '', '', '',
                         amount_text(balance) if balance > 0 else '', amount_text(-balance) if balance < 0 else ''])
        for item in self.opening_items:
            receivable = item['side'] == 'receivable'
            rows.append([item['side'], as_at, '1100' if receivable else '2000', ACCOUNT_NAMES['1100' if receivable else '2000'],
                         item['contact']['contact_id'], item['document_id'], item['number'], item['issued'].isoformat(),
                         item['due'].isoformat(), amount_text(item['total']) if receivable else '',
                         '' if receivable else amount_text(item['total'])])
        self.write_csv(out / 'opening_balances.csv', OPENING_COLUMNS, rows)
        self.write_csv(out / 'documents.csv', DOCUMENT_COLUMNS, [[
            d['document_id'], d['month'], d['kind'], self.direction(d), d['contact']['contact_id'] if d['contact'] else '',
            d['contact']['name'] if d['contact'] else d['merchant'], d['number'], d['issued'].isoformat(),
            d['due'].isoformat() if d['due'] else '', d['related'] or '', 'MYR', amount_text(d['subtotal']),
            amount_text(d['tax']), amount_text(d['rounding']), amount_text(d['total']), d['account'],
            d['receipt']['method'] if d['kind'] == 'receipt' else '', d['file'], d['file_format'], d['manifest']]
            for d in self.documents])
        self.write_bank(out)
        self.write_summary(out)

    @staticmethod
    def direction(doc):
        if doc['kind'] in {'credit_note', 'debit_note'}:
            return doc['direction']
        return 'sales' if doc['kind'] == 'sales_invoice' else 'purchase'

    def bank_cells(self, event, balance):
        posted, amount = event['posted'], event['amount']
        name_text, reference = event['name_text'], event['reference']
        if self.layout == 'signed':
            return [posted.isoformat(), f'{event["type_text"]} {name_text}'.strip(), reference, amount_text(amount),
                    amount_text(balance)]
        if self.layout == 'debit_credit':
            description = f'{event["type_text"]} / {name_text}' if name_text else event['type_text']
            debit = f'{-amount:,.2f}' if amount < 0 else ''
            credit = f'{amount:,.2f}' if amount > 0 else ''
            return [f'{posted:%d/%m/%Y}', f'{event["initiated"]:%d/%m/%Y}', description, reference, debit, credit,
                    f'{balance:,.2f}']
        return [f'{posted:%d-%m-%Y}', event['type_text'], f'{name_text} {reference}'.strip(), amount_text(abs(amount)),
                'DR' if amount < 0 else 'CR', amount_text(balance), 'MYR']

    def write_bank(self, out):
        balance, matches, self.bank_summary = self.opening_bank, [], []
        for month in self.months:
            key = f'{month:%Y-%m}'
            opening, rows = balance, []
            for number, event in enumerate(self.bank_months[key], 1):
                balance += event['amount']
                rows.append(self.bank_cells(event, balance))
                event['row_id'] = f'{key}-{number:03d}'
                matches.append([f'bank_{key}.csv', number, event['row_id'], event['posted'].isoformat(),
                                event['initiated'].isoformat(), amount_text(event['amount']), event['category'],
                                event['match_type'], ';'.join(t for t, _ in event['targets']),
                                ';'.join(amount_text(a) for _, a in event['targets']), event['note']])
            self.write_csv(out / f'bank_{key}.csv', BANK_LAYOUTS[self.layout]['columns'], rows)
            month_events = self.bank_months[key]
            self.bank_summary.append({
                'month': key, 'file': f'bank_{key}.csv', 'rows': len(rows), 'opening_balance': amount_text(opening),
                'money_in': amount_text(sum((e['amount'] for e in month_events if e['amount'] > 0), ZERO)),
                'money_out': amount_text(-sum((e['amount'] for e in month_events if e['amount'] < 0), ZERO)),
                'closing_balance': amount_text(balance)})
        self.write_csv(out / 'expected_matches.csv', MATCH_COLUMNS, matches)

    def open_items(self):
        """Outstanding amount of every receivable and payable item after the last month."""
        settled = {}
        for month_events in self.bank_months.values():
            for event in month_events:
                for target, amount in event['targets']:
                    settled[target] = settled.get(target, ZERO) + abs(amount)
        result = []
        for item in self.opening_items + [d for d in self.documents if d['kind'] != 'receipt']:
            side = 'receivable' if item['kind'] in {'opening_receivable', 'sales_invoice'} or (
                item['kind'] in {'credit_note', 'debit_note'} and item['direction'] == 'sales') else 'payable'
            sign = -1 if item['kind'] == 'credit_note' else 1
            outstanding = item['total'] - settled.get(item['document_id'], ZERO)
            result.append({'item_id': item['document_id'], 'side': side, 'contact_id': item['contact']['contact_id'],
                           'document_number': item['number'], 'due_date': item['due'].isoformat() if item.get('due') else None,
                           'total': amount_text(sign * item['total']),
                           'settled': amount_text(sign * settled.get(item['document_id'], ZERO)),
                           'outstanding': amount_text(sign * outstanding)})
        return result

    def write_summary(self, out):
        items = self.open_items()
        documents, formats, categories, match_types = {}, {}, {}, {}
        for doc in self.documents:
            documents[doc['kind']] = documents.get(doc['kind'], 0) + 1
            formats[doc['file_format']] = formats.get(doc['file_format'], 0) + 1
        for month_events in self.bank_months.values():
            for event in month_events:
                categories[event['category']] = categories.get(event['category'], 0) + 1
                if event['match_type']:
                    match_types[event['match_type']] = match_types.get(event['match_type'], 0) + 1

        def side_total(side, field):
            return amount_text(sum((Decimal(i[field]) for i in items if i['side'] == side), ZERO))

        def kind_total(kind, field='total'):
            return amount_text(sum((d[field] for d in self.documents if d['kind'] == kind), ZERO))
        summary = {
            'generator': self.source_file, 'generator_version': GENERATOR_VERSION, 'seed': self.seed, 'company_key': self.key,
            'notice': NOTICE,
            'company': {key: self.own[key] for key in ('name', 'ssm', 'tin', 'sst', 'phone', 'email', 'sector')} | {
                'address': ', '.join(self.own['address_lines']), 'bank_account_number': self.own['bank_account']},
            'period': {'start': self.period_start.isoformat(), 'end': self.period_end.isoformat(),
                       'months': [f'{month:%Y-%m}' for month in self.months]},
            'bank': {'layout': self.layout, **{k: v for k, v in BANK_LAYOUTS[self.layout].items()},
                     'opening_balance': amount_text(self.opening_bank), 'closing_balance': amount_text(self.closing_bank),
                     'months': self.bank_summary},
            'counts': {'contacts': {'customer': sum(c['type'] == 'customer' for c in self.contacts),
                                    'supplier': sum(c['type'] == 'supplier' for c in self.contacts)},
                       'accounts': len(COA), 'documents': dict(sorted(documents.items())), 'formats': dict(sorted(formats.items())),
                       'manifests': {m.document_type: len(m.samples) for m in self.manifests},
                       'opening_items': len(self.opening_items),
                       'bank_rows': sum(len(rows) for rows in self.bank_months.values()),
                       'match_categories': dict(sorted(categories.items())), 'match_types': dict(sorted(match_types.items()))},
            'totals': {kind: kind_total(kind) for kind in sorted(documents)} | {
                'tax_printed': amount_text(sum((d['tax'] for d in self.documents), ZERO))},
            'opening_trial_balance': {'debit': amount_text(sum((b for b in self.opening_balances.values() if b > 0), ZERO)),
                                      'credit': amount_text(-sum((b for b in self.opening_balances.values() if b < 0), ZERO))},
            'receivables': {'opening': amount_text(self.opening_balances['1100']), 'closing': side_total('receivable', 'outstanding')},
            'payables': {'opening': amount_text(-self.opening_balances['2000']), 'closing': side_total('payable', 'outstanding')},
            'open_items_closing': [item for item in items if Decimal(item['outstanding']) != 0],
        }
        (out / 'pack.json').write_text(json.dumps(summary, indent=1, ensure_ascii=True) + '\n', encoding='utf-8')
        self.summary = summary


CONTACT_COLUMNS = ['contact_id', 'type', 'name', 'category', 'ssm', 'tin', 'sst', 'address', 'email', 'phone',
                   'payment_terms_days', 'default_account_code', 'currency']
OPENING_COLUMNS = ['row_type', 'as_at', 'account_code', 'account_name', 'contact_id', 'item_id', 'document_number',
                   'document_date', 'due_date', 'debit', 'credit']
DOCUMENT_COLUMNS = ['document_id', 'month', 'kind', 'direction', 'contact_id', 'counterparty_name', 'document_number',
                    'document_date', 'due_date', 'related_document_id', 'currency', 'subtotal', 'tax', 'rounding', 'total',
                    'account_code', 'payment_method', 'file', 'file_format', 'manifest']
MATCH_COLUMNS = ['bank_file', 'source_row', 'bank_row_id', 'posted_date', 'payment_date', 'amount', 'category',
                 'match_type', 'document_ids', 'allocated_amounts', 'note']


def read_bank_file(path, layout):
    """Read a pack bank CSV of any of the three layouts into dicts with posted_date (date), description,
    reference, amount (signed Decimal) and balance (Decimal). For tests and seeding; the product parser is
    services/bank/csv_parser.py."""
    with open(path, encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    result = []
    for row in rows:
        if layout == 'signed':
            posted, amount = date.fromisoformat(row['Date']), Decimal(row['Amount'])
            description, reference, balance = row['Description'], row['Reference'], row['Balance']
        elif layout == 'debit_credit':
            day, month, year = (int(part) for part in row['Transaction Date'].split('/'))
            posted = date(year, month, day)
            amount = Decimal((row['Credit'] or '0').replace(',', '')) - Decimal((row['Debit'] or '0').replace(',', ''))
            description, reference, balance = row['Transaction Description'], row['Ref No'], row['Balance']
        else:
            day, month, year = (int(part) for part in row['Posting Date'].split('-'))
            posted = date(year, month, day)
            amount = Decimal(row['Amount']) * (-1 if row['DR/CR'] == 'DR' else 1)
            description, reference, balance = f'{row["Narrative 1"]} {row["Narrative 2"]}'.strip(), '', row['Balance']
        result.append({'posted_date': posted, 'description': description, 'reference': reference, 'amount': amount,
                       'balance': Decimal(balance.replace(',', ''))})
    return result


def generate_pack(key, out_root=DEFAULT_OUT, seed=DEFAULT_SEED, start=DEFAULT_START, bank_layout=None):
    """Generate one company pack under out_root/<key>/ and return its folder."""
    pack = CompanyPack(key, seed=seed, start=start, bank_layout=bank_layout)
    out = pack.build(Path(out_root) / key)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--company', choices=[*COMPANIES, 'all'], default='all')
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED)
    parser.add_argument('--start', default=DEFAULT_START, help='first month, YYYY-MM')
    parser.add_argument('--bank-layout', choices=list(BANK_LAYOUTS), default=None,
                        help='override the company default bank CSV layout')
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    for key in (COMPANIES if args.company == 'all' else [args.company]):
        out = generate_pack(key, args.out, args.seed, args.start, args.bank_layout)
        summary = json.loads((out / 'pack.json').read_text(encoding='utf-8'))
        counts = summary['counts']
        print(f'{key}: {summary["company"]["name"]}; {sum(counts["documents"].values())} documents {counts["formats"]}, '
              f'{counts["bank_rows"]} bank rows ({summary["bank"]["layout"]}), bank {summary["bank"]["opening_balance"]} -> '
              f'{summary["bank"]["closing_balance"]}, AR {summary["receivables"]["closing"]}, AP {summary["payables"]["closing"]} '
              f'-> {out} (validate_collection passed)')


if __name__ == '__main__':
    main()
