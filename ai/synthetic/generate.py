"""Synthetic Malaysian accounting documents with gold labels (task P1.1).

usage:
  python ai/synthetic/generate.py [--set tuning|holdout|all] [--out ai/synthetic/output]

Writes, per set, a folder ai/synthetic/output/<set>/ with
    files/<ID>.pdf|jpg                 the documents
    invoices.json, bills.json, receipts.json   gold labels in the frozen ground-truth
                                       manifest schema (ai/evaluation/dataset_contract.py)
and validates the three manifests with validate_collection.

Kinds: tax invoice, credit note and debit note (all in the invoice manifest, because the
contract's document types are invoice/bill/receipt), cash-sale receipt (thermal width) and
utility bill (electricity, water, broadband). Content is Malaysian in style: SSM, SST and
TIN numbers, MYR, DD/MM/YYYY dates, Malay/English labels, 1-12 line items, SST 6% or 8%,
cash rounding to 5 sen on receipts and stamps such as "PAID / DIBAYAR".

Everything is invented: company names are random word combinations, numbers are random,
there are no logos, and every page says it is a synthetic sample.

Rendering: PDF pages are written directly as content streams with the standard Helvetica
font (the same zero-dependency approach as tests/extraction/synthetic_documents.py; no
HTML-to-PDF tool is installed). Image variants are the same PDF rendered with pypdfium2
and degraded with Pillow: rotation up to 3 degrees, blur, a shadow gradient, a cropped
margin and JPEG quality 40-70.

Provenance: labels are known by construction, which the contract records as
SYNTHETIC_FIXTURE / SCHEMA_TEST_ONLY, so the manifests carry purpose SCHEMA_TEST_ONLY.
The field evaluator still scores them. The holdout set must not be used while tuning.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
GENERATOR = 'ai-synthetic-my/1.0'
SETS = {
    # Different seeds and disjoint name pools, so no supplier appears in both sets.
    'tuning': {'seed': 2026_11_01, 'split': 'development', 'prefix': 'TUN'},
    'holdout': {'seed': 2026_11_02, 'split': 'held_out', 'prefix': 'HLD'},
}
MIX = {'invoice': 14, 'credit_note': 5, 'debit_note': 5, 'bill': 18, 'receipt': 18}  # 60 per set
A4 = (595.0, 842.0)
THERMAL_WIDTH = 226.0  # 80 mm roll

NAME_POOLS = {
    'tuning': ['Cahaya', 'Murni', 'Teratai', 'Delima', 'Bayu', 'Kenanga', 'Puncak', 'Rimba', 'Seroja', 'Kemboja',
               'Tanjung', 'Pelangi', 'Embun', 'Merak', 'Suria', 'Kasturi'],
    'holdout': ['Angsana', 'Selasih', 'Gemilang', 'Lestari', 'Nirmala', 'Saujana', 'Indera', 'Bistari',
                'Cendana', 'Melur', 'Halia', 'Serunai', 'Rembulan', 'Dahlia', 'Jentayu', 'Zamrud'],
}
TRADES = ['Trading', 'Hardware', 'Logistik', 'Printing', 'Kejuruteraan', 'Supplies', 'Teknologi', 'Perabot',
          'Elektrikal', 'Pembinaan', 'Stationery', 'Catering']
SHOPS = ['Kedai Runcit', 'Mini Market', 'Restoran', 'Kedai Kopi', 'Farmasi', 'Kedai Buku', 'Bakeri', 'Kedai Alat Tulis']
UTILITIES = {
    'Electricity': ('Elektrik', 'kWh', ['Caj Tenaga / Energy Charge', 'Caj Perkhidmatan / Service Charge',
                                        'Kumpulan Wang Tenaga Boleh Baharu / Renewable Energy Fund']),
    'Water': ('Air', 'm3', ['Caj Air / Water Charge', 'Caj Pembentungan / Sewerage Charge', 'Caj Meter / Meter Rental']),
    'Internet': ('Jalur Lebar', None, ['Pelan Fiber 300Mbps / Fibre 300Mbps Plan', 'Sewa Router / Router Rental',
                                      'Caj Talian Tetap / Fixed Line Charge']),
}
STREETS = ['Cempaka', 'Kenari', 'Merbah', 'Belatuk', 'Mawar', 'Tembusu', 'Meranti', 'Pinang', 'Rambai', 'Sena']
PLACES = [('47301', 'Petaling Jaya', 'Selangor'), ('40150', 'Shah Alam', 'Selangor'), ('81100', 'Johor Bahru', 'Johor'),
          ('10450', 'George Town', 'Pulau Pinang'), ('30250', 'Ipoh', 'Perak'), ('75200', 'Melaka', 'Melaka'),
          ('88300', 'Kota Kinabalu', 'Sabah'), ('93350', 'Kuching', 'Sarawak'), ('56000', 'Kuala Lumpur', 'W.P. Kuala Lumpur'),
          ('70200', 'Seremban', 'Negeri Sembilan')]
GOODS = [('Kertas A4 80gsm (5 rim)', 18, 32), ('Dakwat pencetak hitam / Black toner', 85, 260), ('Kabel elektrik 2.5mm (90m)', 120, 210),
         ('Kerusi pejabat / Office chair', 180, 450), ('Khidmat penyelenggaraan / Maintenance service', 150, 900),
         ('Paip PVC 20mm', 6, 15), ('Lampu LED 18W', 9, 25), ('Cat dinding 18L / Wall paint 18L', 95, 230),
         ('Sewa lori 3 tan / 3-tonne lorry hire', 250, 600), ('Bekalan makanan mesyuarat / Meeting catering', 120, 800),
         ('Pemasangan rangkaian / Network installation', 300, 1500), ('Fail lengkung / Lever arch file', 4, 9),
         ('Simen 50kg / Cement 50kg', 18, 26), ('Skru keluli (kotak) / Steel screws (box)', 12, 30)]
SHOP_ITEMS = [('Roti Gardenia', 3, 5), ('Susu segar 1L', 6, 9), ('Teh tarik', 2, 4), ('Nasi lemak ayam', 7, 13),
              ('Air mineral 1.5L', 1, 3), ('Pen biru', 1, 3), ('Buku nota A5', 3, 8), ('Panadol 10s', 7, 12),
              ('Kopi O ais', 2, 4), ('Mee goreng', 6, 10), ('Biskut kering', 4, 9), ('Sabun basuh 1kg', 8, 16)]
REASONS = ['Barang rosak / Damaged goods', 'Lebihan caj / Overcharged', 'Pulangan barang / Goods returned',
           'Diskaun tambahan / Additional discount']
DEBIT_REASONS = ['Caj penghantaran tambahan / Additional delivery charge', 'Kekurangan caj / Undercharged',
                 'Caj pemasangan / Installation charge']
CENT = Decimal('0.01')
# Helvetica advance widths (1/1000 em) for the characters right-aligned columns use.
WIDTHS = {**{d: 556 for d in '0123456789'}, ',': 278, '.': 278, ' ': 278, 'R': 722, 'M': 833, '-': 333, '%': 889}


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def printed(amount):
    return f'{amount:,.2f}'


def text_width(text, size):
    return sum(WIDTHS.get(char, 556) for char in text) * size / 1000


class Page:
    """Text and simple graphics in PDF points, y measured from the top."""

    def __init__(self, width, height):
        self.width, self.height, self.ops = width, height, []

    def text(self, x, y, value, size=9, bold=False, right=False):
        if right:
            x -= text_width(value, size)
        self.ops.append(('text', x, y, value, size, bold))

    def line(self, x1, y, x2):
        self.ops.append(('line', x1, y, x2))

    def stamp(self, x, y, value):
        self.ops.append(('stamp', x, y, value))

    def pdf(self):
        def escape(value):
            return value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        stream = ''
        for op in self.ops:
            if op[0] == 'text':
                _, x, y, value, size, bold = op
                stream += f'BT /{"F2" if bold else "F1"} {size} Tf {x:.2f} {self.height - y - size:.2f} Td ({escape(value)}) Tj ET\n'
            elif op[0] == 'line':
                _, x1, y, x2 = op
                stream += f'0.6 w {x1:.2f} {self.height - y:.2f} m {x2:.2f} {self.height - y:.2f} l S\n'
            else:
                _, x, y, value = op
                # Red, rotated 15 degrees, like a rubber stamp.
                stream += (f'q 0.75 0.1 0.1 rg 0.75 0.1 0.1 RG 1.5 w BT /F2 22 Tf 0.966 0.259 -0.259 0.966 '
                           f'{x:.2f} {self.height - y:.2f} Tm ({escape(value)}) Tj ET Q\n')
        data = stream.encode('ascii')
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   (f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.width:g} {self.height:g}] '
                    '/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>').encode(),
                   b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>',
                   b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>',
                   f'<< /Length {len(data)} >>\nstream\n'.encode() + data + b'\nendstream']
        pdf, offsets = b'%PDF-1.4\n', []
        for number, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n'
        start = len(pdf)
        pdf += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
        pdf += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets)
        return pdf + f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()


class Generator:
    def __init__(self, set_name):
        config = SETS[set_name]
        self.set_name, self.split, self.prefix = set_name, config['split'], config['prefix']
        self.rng = random.Random(config['seed'])
        self.names = NAME_POOLS[set_name]
        self.used_names = set()

    # --- invented identities -------------------------------------------------
    def company(self, trade=None, suffix=None):
        while True:
            name = (f'{self.rng.choice(self.names)} {self.rng.choice(self.names)} {trade or self.rng.choice(TRADES)} '
                    f'{suffix or self.rng.choice(["Sdn. Bhd.", "Sdn. Bhd.", "Enterprise", "PLT"])}')
            if name.split()[0] != name.split()[1] and name not in self.used_names:
                self.used_names.add(name)
                return name

    def address(self):
        postcode, city, state = self.rng.choice(PLACES)
        return [f'No. {self.rng.randint(1, 88)}, Jalan {self.rng.choice(STREETS)} {self.rng.randint(1, 12)}/{self.rng.randint(1, 9)}',
                f'Taman {self.rng.choice(self.names)} {self.rng.choice(["Jaya", "Indah", "Utama", "Baru"])}',
                f'{postcode} {city}, {state}']

    def digits(self, count):
        return ''.join(str(self.rng.randint(0, 9)) for _ in range(count))

    def ssm(self):
        return f'20{self.rng.randint(10, 25)}0{self.rng.randint(1, 3)}{self.digits(7)}'

    def sst(self):
        return f'{self.rng.choice("BWJ")}{self.digits(2)}-{self.digits(4)}-{self.digits(8)}'

    def tin(self):
        return f'C{self.digits(10)}'

    def phone(self):
        return f'0{self.rng.choice(["3", "4", "5", "6", "7", "9"])}-{self.digits(4)} {self.digits(4)}'

    def day(self):
        return date(2026, 1, 1) + timedelta(days=self.rng.randint(0, 300))

    def dmy(self, value):
        return value.strftime('%d/%m/%Y' if self.rng.random() < 0.8 else '%d-%m-%Y')

    def labels(self, malay, english):
        style = self.style
        return malay if style == 'ms' else english if style == 'en' else f'{malay} / {english}'

    # --- documents ---------------------------------------------------------------
    def lines(self, pool, count, scale=1):
        items = []
        for description, low, high in self.rng.sample(pool, count):
            quantity = Decimal(self.rng.choice([1, 1, 2, 3, 4, 5, 10, 12, 20]))
            unit = money(Decimal(self.rng.randint(low * 100, high * 100)) / 100 * scale)
            items.append({'description': description, 'quantity': quantity, 'unit_price': unit,
                          'line_total': money(quantity * unit)})
        return items

    def header(self, page, supplier, address, ids, title, y=40):
        page.text(40, y, supplier, 14, bold=True)
        y += 20
        for row in address + [ids]:
            page.text(40, y, row, 8.5)
            y += 12
        page.text(40, y + 4, f'Tel: {self.phone()}   E-mel: akaun@contoh-{self.rng.randint(100, 999)}.test', 8.5)
        page.text(page.width - 40, 44, title, 15, bold=True, right=True)
        return y + 26

    def footer(self, page, y):
        page.text(40, y, 'CONTOH SINTETIK / SYNTHETIC SAMPLE - fictional business, not a real transaction.', 7)

    def invoice_like(self, kind):
        """Tax invoice, credit note or debit note: one A4 page, labels for the invoice manifest."""
        rate = Decimal(self.rng.choice([6, 8]))
        supplier, supplier_address = self.company(), self.address()
        customer, customer_address = self.company(), self.address()
        ssm, sst, tin = self.ssm(), self.sst(), self.tin()
        issued = self.day()
        number = {'invoice': 'INV', 'credit_note': 'CN', 'debit_note': 'DN'}[kind] + f'-{issued:%y%m}-{self.digits(4)}'
        items = self.lines(GOODS, self.rng.randint(1, 12) if kind == 'invoice' else self.rng.randint(1, 3))
        subtotal = sum((item['line_total'] for item in items), Decimal('0.00'))
        tax = money(subtotal * rate / 100)
        total = subtotal + tax
        title = {'invoice': self.labels('INVOIS CUKAI', 'TAX INVOICE'),
                 'credit_note': self.labels('NOTA KREDIT', 'CREDIT NOTE'),
                 'debit_note': self.labels('NOTA DEBIT', 'DEBIT NOTE')}[kind]
        page = Page(*A4)
        ids = f'{self.labels("No. Syarikat", "Co. Reg. No.")}: {ssm}   SST: {sst}   TIN: {tin}'
        y = self.header(page, supplier, supplier_address, ids, title)
        page.text(40, y, self.labels('Kepada', 'Bill To') + ':', 9, bold=True)
        page.text(40, y + 13, customer, 9)
        for index, row in enumerate(customer_address):
            page.text(40, y + 25 + index * 12, row, 8.5)
        number_label = {'invoice': self.labels('No. Invois', 'Invoice No.'),
                        'credit_note': self.labels('No. Nota Kredit', 'Credit Note No.'),
                        'debit_note': self.labels('No. Nota Debit', 'Debit Note No.')}[kind]
        meta = [(number_label, number), (self.labels('Tarikh', 'Date'), self.dmy(issued))]
        labels = {'supplier_name': supplier, 'supplier_address': '\n'.join(supplier_address),
                  'supplier_registration_id': ssm, 'supplier_sst_id': sst, 'supplier_tin': tin,
                  'customer_name': customer, 'customer_address': '\n'.join(customer_address),
                  'invoice_date': (issued.isoformat(), meta[1][1]), 'currency': ('MYR', 'RM'),
                  'subtotal': (format(subtotal, 'f'), printed(subtotal)), 'tax': (format(tax, 'f'), printed(tax)),
                  'total': (format(total, 'f'), printed(total))}
        if kind == 'invoice':
            terms = self.rng.choice([14, 30, 30, 60])
            due = issued + timedelta(days=terms)
            meta += [(self.labels('Terma', 'Terms'), f'Net {terms}'), (self.labels('Tarikh Akhir', 'Due Date'), self.dmy(due))]
            labels.update(invoice_number=number, payment_terms=f'Net {terms}', due_date=(due.isoformat(), meta[3][1]))
            if self.rng.random() < 0.4:
                po = f'PO-{self.digits(5)}'
                meta.append((self.labels('No. Pesanan', 'PO No.'), po))
                labels['purchase_order_number'] = po
        else:
            original = f'INV-{issued:%y%m}-{self.digits(4)}'
            reason = self.rng.choice(REASONS if kind == 'credit_note' else DEBIT_REASONS)
            meta.append((self.labels('Rujukan Invois', 'Original Invoice'), original))
            labels.update({kind + '_number': number, 'original_invoice_number': original, 'note_reason': reason})
        for index, (label, value) in enumerate(meta):
            page.text(340, y + index * 13, label + ':', 9, bold=True)
            page.text(450, y + index * 13, value, 9)
        y += max(len(meta) * 13, 64) + 18
        if kind != 'invoice':
            page.text(40, y, self.labels('Sebab', 'Reason') + ': ' + labels['note_reason'], 9)
            y += 18
        y = self.table(page, y, items)
        y = self.totals(page, y, [(self.labels('Jumlah Kecil', 'Subtotal'), subtotal),
                                  (f'SST {rate}%', tax),
                                  (self.labels('Jumlah Besar', 'Grand Total') + ' (RM)', total)])
        if kind == 'invoice' and self.rng.random() < 0.3:
            page.stamp(380, y + 40, 'PAID / DIBAYAR')
        self.footer(page, 800)
        return page, labels, items

    def table(self, page, y, items, columns=True):
        headings = [(48, self.labels('Perihal', 'Description')), (290, self.labels('Kuantiti', 'Qty')),
                    (352, self.labels('Harga Seunit', 'Unit Price')), (555, self.labels('Jumlah', 'Amount'))]
        page.line(40, y, 555)
        for x, heading in headings if columns else headings[:1] + headings[3:]:
            page.text(x, y + 5, heading, 7.5, bold=True, right=x == 555)
        page.line(40, y + 17, 555)
        y += 22
        for item in items:
            page.text(48, y, item['description'], 8.5)
            if columns:
                page.text(290, y, format(item['quantity'], 'f'), 8.5)
                page.text(352, y, printed(item['unit_price']), 8.5)
            page.text(555, y, printed(item['line_total']), 8.5, right=True)
            y += 14
        page.line(40, y, 555)
        return y + 8

    def totals(self, page, y, rows):
        for label, amount in rows:
            page.text(470, y, label, 9, bold=True, right=True)
            page.text(555, y, printed(amount), 9, bold=True, right=True)
            y += 14
        return y

    def bill(self):
        subtype = self.rng.choice(list(UTILITIES))
        word, unit, charges = UTILITIES[subtype]
        provider = self.company(trade=word + ' ' + self.rng.choice(['Wilayah', 'Bersepadu', 'Komuniti']),
                                suffix=self.rng.choice(['Sdn. Bhd.', 'Berhad']))
        provider_address, customer, service_address = self.address(), self.company(), self.address()
        account = self.digits(12)
        issued = self.day()
        start = (issued.replace(day=1) - timedelta(days=1)).replace(day=1)
        end = issued.replace(day=1) - timedelta(days=1)
        due = issued + timedelta(days=21)
        number = f'B{self.digits(9)}'
        items = []
        if unit:
            consumption = Decimal(self.rng.randint(80, 2400))
            rate = Decimal(self.rng.choice(['0.218', '0.334', '0.516', '0.57', '1.20', '1.53']))
            items.append({'description': f'{charges[0]} ({consumption} {unit} x RM {rate})', 'line_total': money(consumption * rate)})
        else:
            items.append({'description': charges[0], 'line_total': money(self.rng.choice([129, 149, 189, 249]))})
        for charge in charges[1:self.rng.randint(2, 3)]:
            items.append({'description': charge, 'line_total': money(Decimal(self.rng.randint(300, 2500)) / 100)})
        subtotal = sum((item['line_total'] for item in items), Decimal('0.00'))
        tax = money(subtotal * Decimal(self.rng.choice([6, 8])) / 100)
        total = subtotal + tax
        arrears = money(Decimal(self.rng.randint(1000, 30000)) / 100) if self.rng.random() < 0.3 else Decimal('0.00')
        due_amount = total + arrears
        page = Page(*A4)
        ids = f'{self.labels("No. Syarikat", "Co. Reg. No.")}: {self.ssm()}   SST: {self.sst()}'
        y = self.header(page, provider, provider_address, ids, self.labels('BIL', 'BILL') + ' ' + subtype.upper())
        period = f'{self.dmy(start)} - {self.dmy(end)}'
        meta = [(self.labels('No. Akaun', 'Account No.'), account), (self.labels('No. Bil', 'Bill No.'), number),
                (self.labels('Tarikh Bil', 'Bill Date'), self.dmy(issued)), (self.labels('Tempoh Bil', 'Billing Period'), period),
                (self.labels('Tarikh Akhir Bayaran', 'Pay Before'), self.dmy(due))]
        page.text(40, y, customer, 9, bold=True)
        for index, row in enumerate(service_address):
            page.text(40, y + 12 + index * 12, row, 8.5)
        for index, (label, value) in enumerate(meta):
            page.text(320, y + index * 13, label + ':', 9, bold=True)
            page.text(455, y + index * 13, value, 9)
        y = self.table(page, y + 86, items, columns=False)
        rows = [(self.labels('Jumlah Caj Semasa', 'Current Charges'), subtotal), ('SST', tax),
                (self.labels('Jumlah', 'Total'), total)]
        if arrears:
            rows.append((self.labels('Tunggakan', 'Arrears'), arrears))
        rows.append((self.labels('Jumlah Perlu Dibayar', 'Amount Due') + ' (RM)', due_amount))
        y = self.totals(page, y, rows)
        if self.rng.random() < 0.25:
            page.stamp(380, y + 40, 'PAID / DIBAYAR')
        page.text(40, y + 30, f'JomPAY Biller Code: {self.digits(5)}   Ref-1: {account}', 8.5)
        self.footer(page, 800)
        labels = {'provider_name': provider, 'provider_address': '\n'.join(provider_address),
                  'customer_name': customer, 'customer_account_number': account, 'bill_number': number,
                  'bill_date': (issued.isoformat(), meta[2][1]), 'due_date': (due.isoformat(), meta[4][1]),
                  'billing_period': period, 'currency': ('MYR', 'RM'),
                  'subtotal': (format(subtotal, 'f'), printed(subtotal)), 'tax': (format(tax, 'f'), printed(tax)),
                  'total': (format(total, 'f'), printed(total)), 'amount_due': (format(due_amount, 'f'), printed(due_amount))}
        return page, labels, [{'description': i['description'], 'line_total': i['line_total']} for i in items], subtype

    def receipt(self):
        merchant = self.company(trade=self.rng.choice(SHOPS), suffix=self.rng.choice(['Enterprise', 'Sdn. Bhd.']))
        address = self.address()
        items = self.lines(SHOP_ITEMS, self.rng.randint(1, 8))
        subtotal = sum((item['line_total'] for item in items), Decimal('0.00'))
        rate = Decimal(self.rng.choice([6, 8]))
        tax = money(subtotal * rate / 100)
        exact = subtotal + tax
        total = (exact * 20).quantize(Decimal('1'), rounding=ROUND_HALF_UP) / 20  # Cash rounding to 5 sen.
        total = money(total)
        rounding = total - exact
        when = self.day()
        clock = f'{self.rng.randint(8, 22):02d}:{self.rng.randint(0, 59):02d}'
        number = f'{self.rng.choice(["CS", "R", "INV"])}{self.digits(6)}'
        method, method_text = self.rng.choice([('cash', self.labels('TUNAI', 'CASH')), ('card', self.labels('KAD', 'CARD')),
                                               ('e_wallet', 'E-WALLET')])
        height = 330 + 24 * len(items)
        page = Page(THERMAL_WIDTH, height)
        center = THERMAL_WIDTH / 2

        def centred(y, value, size=7.5, bold=False):
            page.text(center - text_width(value, size) / 2, y, value, size, bold)
        y = 20
        centred(y, merchant, 8.5, True)
        for row in address:
            y += 11
            centred(y, row)
        y += 11
        centred(y, f'SST: {self.sst()}')
        y += 18
        centred(y, self.labels('RESIT JUALAN TUNAI', 'CASH SALE RECEIPT'), 8, True)
        y += 16
        page.text(10, y, f'{self.labels("No. Resit", "Receipt No")}: {number}', 7.5)
        y += 11
        printed_date = self.dmy(when)
        page.text(10, y, f'{self.labels("Tarikh", "Date")}: {printed_date}  {clock}', 7.5)
        y += 8
        page.line(10, y + 4, THERMAL_WIDTH - 10)
        y += 10
        for item in items:
            page.text(10, y, item['description'], 7.5)
            y += 10
            page.text(16, y, f'{item["quantity"]} x {printed(item["unit_price"])}', 7.5)
            page.text(THERMAL_WIDTH - 10, y, printed(item['line_total']), 7.5, right=True)
            y += 12
        page.line(10, y, THERMAL_WIDTH - 10)
        y += 6
        for label, amount in [(self.labels('Jumlah Kecil', 'Subtotal'), subtotal), (f'SST {rate}%', tax),
                              (self.labels('Pelarasan', 'Rounding'), rounding), (self.labels('JUMLAH', 'TOTAL') + ' RM', total)]:
            page.text(10, y, label, 7.5, bold=label.startswith(('JUMLAH', 'TOTAL')))
            page.text(THERMAL_WIDTH - 10, y, printed(amount), 7.5, right=True)
            y += 11
        tender = total if method != 'cash' else money(Decimal(self.rng.choice([10, 20, 50, 100])) + (total // 10) * 10)
        page.text(10, y, method_text, 7.5)
        page.text(THERMAL_WIDTH - 10, y, printed(tender), 7.5, right=True)
        y += 11
        if method == 'cash':
            page.text(10, y, self.labels('BAKI', 'CHANGE'), 7.5)
            page.text(THERMAL_WIDTH - 10, y, printed(tender - total), 7.5, right=True)
            y += 11
        y += 8
        centred(y, self.labels('Terima kasih', 'Thank you') + '!')
        centred(height - 16, 'SYNTHETIC SAMPLE - fictional shop', 6)
        labels = {'merchant_name': merchant, 'merchant_address': '\n'.join(address), 'receipt_number': number,
                  'transaction_date': (when.isoformat(), printed_date), 'transaction_time': (clock, clock),
                  'payment_method': (method, method_text), 'currency': ('MYR', 'RM'),
                  'subtotal': (format(subtotal, 'f'), printed(subtotal)), 'tax': (format(tax, 'f'), printed(tax)),
                  'rounding': (format(rounding, 'f'), printed(rounding)), 'total': (format(total, 'f'), printed(total))}
        return page, labels, items

    # --- images and labels ------------------------------------------------------------
    def degrade(self, pdf_bytes, thermal):
        import pypdfium2 as pdfium
        from PIL import Image, ImageChops, ImageFilter
        document = pdfium.PdfDocument(pdf_bytes)
        try:
            image = document[0].render(scale=3.0 if thermal else 2.0).to_pil().convert('RGB')
        finally:
            document.close()
        angle = round(self.rng.uniform(-3, 3), 2)
        image = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(255, 255, 255))
        blur = round(self.rng.uniform(0.3, 1.2), 2)
        image = image.filter(ImageFilter.GaussianBlur(blur))
        width, height = image.size
        strength = self.rng.uniform(0.15, 0.4)
        horizontal = self.rng.random() < 0.5
        steps = width if horizontal else height
        ramp = Image.linear_gradient('L').resize((256, 256)).rotate(90 if horizontal else 0)
        ramp = ramp.resize((width, height)).point(lambda v: int(255 - strength * v))
        image = ImageChops.multiply(image, Image.merge('RGB', [ramp] * 3))
        crop = [round(self.rng.uniform(0, 0.025) * side) for side in (width, height, width, height)]
        image = image.crop((crop[0], crop[1], width - crop[2], height - crop[3]))
        quality = self.rng.randint(40, 70)
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=quality)
        variant = f'rotate={angle};blur={blur};shadow={strength:.2f};crop={",".join(map(str, crop))};jpeg_q={quality};steps={steps}'
        return buffer.getvalue(), image.size, variant

    def annotation(self, reference):
        return {'source_kind': 'SYNTHETIC_FIXTURE', 'source_reference': reference, 'revision': GENERATOR,
                'verification_state': 'SCHEMA_TEST_ONLY', 'annotator_id': None, 'verifier_id': None, 'verified_at': None,
                'notes': 'Label is the generator input printed on the page.'}

    def label(self, key, value, reference):
        from ai.evaluation.field_dictionary import FIELDS
        normalized, shown = value if isinstance(value, tuple) else (value, value.replace('\n', ', '))
        return {'field_key': key, 'value_type': FIELDS[key]['value_type'], 'label_state': 'PRESENT',
                'value': normalized, 'printed_text': shown, 'normalized_value': normalized,
                'evidence': [{'page': 1, 'source_reference': reference, 'printed_text': shown, 'bbox': None,
                              'coordinate_convention': None, 'coordinate_units': None, 'page_width': None, 'page_height': None}],
                'candidates': [], 'annotation': self.annotation(reference)}

    def sample(self, number, kind, out):
        self.style = self.rng.choice(['ms', 'en', 'mixed', 'mixed'])
        subtype = None
        if kind == 'bill':
            page, labels, items, subtype = self.bill()
        elif kind == 'receipt':
            page, labels, items = self.receipt()
        else:
            page, labels, items = self.invoice_like(kind)
        manifest_kind = 'invoice' if kind in {'credit_note', 'debit_note'} else kind
        sample_id = f'{self.prefix}-{kind.upper().replace("_", "")}-{number:03d}'
        pdf_bytes = page.pdf()
        image_share = 0.8 if kind == 'receipt' else 0.6
        if self.rng.random() < image_share:
            data, (width, height), variant = self.degrade(pdf_bytes, kind == 'receipt')
            file_format = 'jpg'
            pages = [{'page': 1, 'width': float(width), 'height': float(height), 'units': 'pixel',
                      'image_width_px': width, 'image_height_px': height}]
        else:
            data, file_format, variant = pdf_bytes, 'pdf', 'digital'
            pages = [{'page': 1, 'width': page.width, 'height': page.height, 'units': 'pdf_point',
                      'image_width_px': None, 'image_height_px': None}]
        relative = f'files/{sample_id}.{file_format}'
        (out / relative).write_bytes(data)
        reference = f'ai/synthetic/generate.py#{self.set_name}/{sample_id}'
        rows = []
        for order, item in enumerate(items, 1):
            cells = [self.label('description', item['description'], f'{reference}/line/{order}')]
            for key in ('quantity', 'unit_price', 'line_total'):
                if key in item:
                    amount = item[key]
                    shown = format(amount, 'f') if key == 'quantity' else printed(amount)
                    cells.append(self.label(key, (format(amount, 'f'), shown), f'{reference}/line/{order}'))
            rows.append({'row_id': f'{sample_id}-L{order}', 'printed_order': order, 'page': 1, 'row_evidence': None,
                         'cells': cells})
        return manifest_kind, {
            'sample_id': sample_id, 'document_type': manifest_kind,
            'classification_annotation': self.annotation(reference + f'#kind={kind}'),
            'bill_subtype': subtype, 'benchmark_split': self.split,
            'source': {'dataset': 'ai-synthetic-my', 'dataset_revision': GENERATOR, 'original_split': self.set_name,
                       'original_sample_id': f'{sample_id}:{kind}', 'origin': 'SYNTHETIC', 'languages': ['ms', 'en'],
                       'country': 'MY', 'file_format': file_format, 'page_count': 1, 'pages': pages,
                       'relative_path': relative, 'private_object_key': None,
                       'sha256': hashlib.sha256(data).hexdigest(), 'licence': None, 'licence_reference': None,
                       'annotation_provenance': 'Generated with the document; labels are the printed generator inputs.',
                       'related_source_group': f'{self.set_name}:{sample_id}',
                       'base_document_group_id': f'{self.set_name}:{sample_id}',
                       'template_group_id': f'{self.set_name}:{kind}', 'quality_variant': variant},
            'fields': [self.label(key, value, f'{reference}/{key}') for key, value in labels.items()],
            'line_items': {'status': 'LABELLED_ROWS', 'row_coverage': 'COMPLETE',
                           'annotation': self.annotation(reference + '/line_items'), 'rows': rows},
            'repeated_groups': []}


def generate(set_name, out_root):
    out = Path(out_root) / set_name
    (out / 'files').mkdir(parents=True, exist_ok=True)
    for old in (out / 'files').iterdir():
        old.unlink()
    generator = Generator(set_name)
    kinds = [kind for kind, count in MIX.items() for _ in range(count)]
    generator.rng.shuffle(kinds)
    samples = {'invoice': [], 'bill': [], 'receipt': []}
    for number, kind in enumerate(kinds, 1):
        manifest_kind, sample = generator.sample(number, kind, out)
        samples[manifest_kind].append(sample)
    paths = []
    for kind, name in (('invoice', 'invoices.json'), ('bill', 'bills.json'), ('receipt', 'receipts.json')):
        manifest = {'schema_version': '1.0.0', 'artifact_type': 'frozen_ground_truth', 'field_dictionary_version': '1.0.0',
                    'benchmark_id': f'ai-synthetic-my-{set_name}', 'benchmark_revision': GENERATOR,
                    'purpose': 'SCHEMA_TEST_ONLY', 'document_type': kind, 'samples': samples[kind]}
        path = out / name
        path.write_text(json.dumps(manifest, indent=1, ensure_ascii=True), encoding='utf-8')
        paths.append(path)
    from ai.evaluation.dataset_contract import validate_collection
    manifests = validate_collection(paths, out)
    counts = {m.document_type: len(m.samples) for m in manifests}
    formats = {}
    for m in manifests:
        for s in m.samples:
            formats[s.source.file_format] = formats.get(s.source.file_format, 0) + 1
    print(f'{set_name}: {sum(counts.values())} documents {counts} {formats} -> {out} (validate_collection passed)')
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--set', choices=[*SETS, 'all'], default='all')
    parser.add_argument('--out', default=str(ROOT / 'ai' / 'synthetic' / 'output'))
    args = parser.parse_args(argv)
    for name in (SETS if args.set == 'all' else [args.set]):
        generate(name, args.out)


if __name__ == '__main__':
    main()
