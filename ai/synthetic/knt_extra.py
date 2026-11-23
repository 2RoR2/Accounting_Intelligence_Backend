"""The richer KNT demo data: more contacts, a product catalogue and Sep-Nov typed sales history that uses them.

Everything here is fictional (reserved .test addresses, invented SSM/TIN/SST numbers) and kept apart from the
original fixture in knt_pack.py so a hosted copy of the original books can be extended without touching it:
``extension()`` returns only what this module adds; knt_pack.books() returns the original plus the extension.

Tax amounts are typed per line (a label from the tax-code catalogue plus an amount); the rates below only fill in the
fixture, the application never calculates tax.
Payments received and made are posted to a "Cash in hand" account, not to the bank statement, so the three imported
bank statements and their reconciliations stay exactly as the original fixture had them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")

# Catalogue entries every KNT workspace gets (label: description, informational rate).
TAX_CODES = {
    "SST-10": ("Sales tax 10% (taxable goods)", Decimal("0.10")),
    "SST-5": ("Sales tax 5% (cement, sand, aggregates, bricks)", Decimal("0.05")),
    "SV-8": ("Service tax 8% (delivery, installation, cutting)", Decimal("0.08")),
    "SST-0": ("Not subject to SST", ZERO),
}
PAYMENT_TERMS = {  # code: (name, days)
    "COD": ("Cash on delivery", 0), "NET14": ("Net 14 days", 14), "NET30": ("Net 30 days", 30),
    "NET45": ("Net 45 days", 45), "NET60": ("Net 60 days", 60),
}
CASH_ACCOUNT = {"name": "Cash in hand (counter collections)", "code": "CA-1000", "ledger": "1000"}


# ------------------------------------------------------------------ contacts
@dataclass(frozen=True)
class ContactDetail:
    ssm: str
    tin: str
    sst: str
    phone: str
    terms: int
    postcode: str
    city: str
    category: str


# kind, name, default account, street: the same shape as knt_pack.CONTACTS.
EXTRA_CONTACTS = {
    "BEL": ("customer", "Belian Construction Sdn. Bhd.", "4000", "Lot 301, Jalan Belian Contoh"),
    "MER": ("customer", "Meranti Renovation Works", "4000", "Lot 118, Jalan Meranti Contoh"),
    "KEM": ("customer", "Kempas Engineering Sdn. Bhd.", "4000", "Lot 402, Jalan Kempas Contoh"),
    "RAM": ("customer", "Ramin Interior Design Sdn. Bhd.", "4000", "Lot 57, Jalan Ramin Contoh"),
    "NYA": ("customer", "Nyatoh Homes Sdn. Bhd.", "4000", "Lot 8, Jalan Nyatoh Contoh"),
    "JEL": ("customer", "Jelutong Hardware & Paints", "4000", "Lot 21, Jalan Jelutong Contoh"),
    "BAK": ("customer", "Bakau Marine Services Sdn. Bhd.", "4000", "Lot 90, Jalan Bakau Contoh"),
    "CMP": ("customer", "Cempaka Learning Centre Sdn. Bhd.", "4000", "Lot 14, Jalan Cempaka Contoh"),
    "DUR": ("customer", "Durian Burung Cafe Sdn. Bhd.", "4000", "Lot 3, Jalan Durian Contoh"),
    "KEL": ("customer", "Kelulut Clinic Sdn. Bhd.", "4000", "Lot 66, Jalan Kelulut Contoh"),
    "LAN": ("customer", "Langsat Property Management Sdn. Bhd.", "4000", "Lot 245, Jalan Langsat Contoh"),
    "MEN": ("customer", "Mengkuang Logistics Sdn. Bhd.", "4000", "Lot 77, Jalan Mengkuang Contoh"),
    "PIN": ("customer", "Pinang Sari Trading", "4000", "Lot 5, Jalan Pinang Contoh"),
    "RAS": ("customer", "Rasau Motors Sdn. Bhd.", "4000", "Lot 132, Jalan Rasau Contoh"),
    "TEM": ("customer", "Tempinis Furniture Sdn. Bhd.", "4000", "Lot 49, Jalan Tempinis Contoh"),
    "SEL": ("supplier", "Seladah Cement & Concrete Sdn. Bhd.", "5000", "Lot 210, Jalan Seladah Contoh"),
    "MAT": ("supplier", "Matang Timber & Plywood Sdn. Bhd.", "5000", "Lot 31, Jalan Matang Contoh"),
    "KAN": ("supplier", "Kandis Paint Distributors Sdn. Bhd.", "5000", "Lot 88, Jalan Kandis Contoh"),
    "SAM": ("supplier", "Samarahan Steel Trading Sdn. Bhd.", "5000", "Lot 19, Jalan Industri Contoh"),
    "BAT": ("supplier", "Batu Kawa Electrical Wholesale Sdn. Bhd.", "5000", "Lot 62, Jalan Batu Kawa Contoh"),
    "SAR": ("supplier", "Sarikei Pipe & Sanitary Supplies Sdn. Bhd.", "5000", "Lot 7, Jalan Sarikei Contoh"),
    "KUC": ("supplier", "Kuching Paper & Stationery Sdn. Bhd.", "5000", "Lot 40, Jalan Stesen Contoh"),
    "SAN": ("supplier", "Santubong Haulage Sdn. Bhd.", "5020", "Lot 15, Jalan Santubong Contoh"),
    "KEN": ("supplier", "Kenyalang Telecom Sdn. Bhd.", "6130", "Lot 2, Jalan Kenyalang Contoh"),
    "LUN": ("supplier", "Lundu Machinery Repairs", "6150", "Lot 28, Jalan Lundu Contoh"),
}
# Registered identifiers are invented: the SSM/TIN/SST ranges below belong to no real company.
_DETAIL_ROWS = {  # code: (terms days, city, postcode, category)
    "SEN": (14, "Kuching", "93000", "Fit-out contractor"), "KU": (14, "Kuching", "93100", "Office"),
    "PAU": (30, "Kuching", "93350", "Building materials"), "TUA": (14, "Kuching", "93450", "Utility"),
    "REN": (14, "Kuching", "93100", "Stationery"),
    "BEL": (30, "Kuching", "93350", "Main contractor"), "MER": (14, "Kuching", "93250", "Renovation"),
    "KEM": (30, "Kuching", "93450", "M&E contractor"), "RAM": (30, "Kuching", "93100", "Interior design"),
    "NYA": (30, "Kota Samarahan", "94300", "Developer"), "JEL": (14, "Serian", "94700", "Reseller"),
    "BAK": (30, "Kuching", "93000", "Marine services"), "CMP": (30, "Kuching", "93200", "Education"),
    "DUR": (14, "Kuching", "93100", "Food and beverage"), "KEL": (30, "Kuching", "93350", "Healthcare"),
    "LAN": (45, "Kuching", "93450", "Property management"), "MEN": (30, "Kuching", "93350", "Logistics"),
    "PIN": (14, "Sibu", "96000", "Retail"), "RAS": (14, "Kuching", "93250", "Automotive"),
    "TEM": (30, "Kuching", "93300", "Furniture"),
    "SEL": (30, "Kuching", "93350", "Cement"), "MAT": (30, "Kuching", "93250", "Timber"),
    "KAN": (45, "Kuching", "93000", "Paint"), "SAM": (30, "Kota Samarahan", "94300", "Steel"),
    "BAT": (30, "Kuching", "93250", "Electrical"), "SAR": (45, "Sarikei", "96100", "Plumbing"),
    "KUC": (30, "Kuching", "93100", "Stationery"), "SAN": (14, "Kuching", "93350", "Haulage"),
    "KEN": (30, "Kuching", "93000", "Telecom"), "LUN": (14, "Kuching", "93250", "Repairs"),
}
_AREA = {"Kuching": "082", "Kota Samarahan": "082", "Serian": "082", "Sibu": "084", "Sarikei": "084"}


def contact_details(code: str, kind: str, position: int) -> ContactDetail:
    """Header data for one contact. `position` is its 1-based place in its own list (customers or suppliers)."""
    terms, city, postcode, category = _DETAIL_ROWS[code]
    customer = kind == "customer"
    series = ("99910", "2025019", 9991) if customer else ("99920", "2024018", 9992)
    return ContactDetail(
        ssm=f"{series[1]}{position:05d}", tin=f"C{series[0]}{position:05d}",
        sst=f"W10-2611-{series[2]}{position:04d}",
        phone=f"{_AREA[city]}-9{position:05d}", terms=terms, postcode=postcode, city=city, category=category)


# ------------------------------------------------------------------ products
@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    unit: str
    sale: Decimal
    cost: Decimal
    group: str
    tax_code: str
    stock: int = 0
    kind: str = "inventory"
    income: str = "4000"
    expense: str = "5000"


def _goods(group, tax, rows):
    return [Product(sku, name, unit, Decimal(sale), Decimal(cost), group, tax, stock)
            for sku, name, unit, sale, cost, stock in rows]


PRODUCTS = tuple(
    _goods("Cement and aggregates", "SST-5", [
        ("CEM-50", "Portland cement 50 kg", "bag", "24.50", "19.80", 400),
        ("SND-FN", "Fine river sand", "tonne", "68.00", "52.00", 40),
        ("GRV-20", "Crushed granite 20 mm", "tonne", "74.00", "58.00", 35),
        ("BRK-RED", "Red clay bricks", "1,000 pcs", "420.00", "335.00", 12),
        ("MRT-25", "Ready-mix mortar 25 kg", "bag", "15.80", "12.20", 220)])
    + _goods("Steel and timber", "SST-10", [
        ("RBR-10", "Steel rebar 10 mm x 6 m", "length", "14.20", "11.00", 600),
        ("RBR-12", "Steel rebar 12 mm x 6 m", "length", "20.50", "16.30", 450),
        ("WMS-6", "Welded wire mesh A6", "sheet", "56.00", "44.00", 80),
        ("PLY-12", "Plywood 12 mm 4 x 8 ft", "sheet", "62.00", "48.50", 150),
        ("TMB-24", "Meranti timber 2 x 4 in x 12 ft", "length", "21.00", "16.50", 500),
        ("DRY-9", "Plasterboard 9 mm 4 x 8 ft", "sheet", "14.80", "11.30", 260)])
    + _goods("Paint and finishing", "SST-10", [
        ("PNT-18W", "Emulsion paint white 18 L", "pail", "178.00", "139.00", 60),
        ("PNT-5W", "Emulsion paint white 5 L", "pail", "54.00", "41.50", 120),
        ("PNT-PRM", "Alkali-resistant primer 18 L", "pail", "152.00", "118.00", 40),
        ("PNT-ENM", "Gloss enamel paint 1 L", "tin", "28.50", "21.00", 180),
        ("SLV-TH", "Paint thinner 4 L", "tin", "22.00", "16.20", 90)])
    + _goods("Plumbing and sanitary", "SST-10", [
        ("PVC-4", "PVC pipe 4 in x 6 m", "length", "38.00", "29.00", 140),
        ("PVC-2", "PVC pipe 2 in x 6 m", "length", "16.50", "12.40", 200),
        ("ELB-4", "PVC elbow 4 in", "pc", "6.80", "4.90", 400),
        ("TAP-BIB", "Brass bib tap 1/2 in", "pc", "24.00", "17.50", 120),
        ("WC-CST", "Close-coupled WC set", "set", "320.00", "248.00", 14),
        ("TNK-1000", "Water tank 1,000 L", "unit", "385.00", "305.00", 9)])
    + _goods("Electrical", "SST-10", [
        ("CBL-25", "Twin and earth cable 2.5 mm2 (100 m)", "roll", "245.00", "196.00", 30),
        ("SWT-2G", "Light switch 2-gang", "pc", "9.50", "6.80", 260),
        ("MCB-20", "Circuit breaker 20 A single pole", "pc", "18.50", "13.20", 180),
        ("LED-18", "LED tube 18 W", "pc", "12.50", "8.90", 500),
        ("TRK-25", "Cable trunking 25 x 16 mm (2.5 m)", "pc", "6.20", "4.30", 700)])
    + _goods("Hardware and fixings", "SST-10", [
        ("SCR-DRY", "Drywall screws 1 in (box of 1,000)", "box", "28.00", "20.50", 90),
        ("NL-3", "Common wire nails 3 in", "kg", "5.20", "3.80", 500),
        ("HNG-4", "Door hinge 4 in", "pair", "8.80", "6.10", 300),
        ("LCK-CYL", "Cylinder door lock set", "set", "46.00", "33.50", 70),
        ("SIL-GP", "Silicone sealant 300 ml", "tube", "12.00", "8.40", 300)])
    + _goods("Office supplies", "SST-10", [
        ("A4-80", "A4 copier paper 80 gsm (5 reams)", "box", "118.00", "92.00", 85),
        ("PEN-BP", "Ballpoint pens blue (box of 50)", "box", "21.00", "14.50", 60),
        ("FIL-LA", "Lever arch file A4", "pc", "5.80", "3.90", 300),
        ("STP-24", "Heavy-duty stapler", "pc", "22.50", "15.80", 40),
        ("TNR-HP", "Compatible toner cartridge, black", "pc", "135.00", "102.00", 25),
        ("NTB-A5", "Notebook A5 (pack of 10)", "pack", "18.00", "12.30", 80)])
    + [Product("SVC-DLV", "Delivery within Kuching (per trip)", "trip", Decimal("80.00"), Decimal("55.00"),
               "Services", "SV-8", kind="service", income="4010", expense="5020"),
       Product("SVC-INS", "Installation labour (per day)", "day", Decimal("180.00"), Decimal("130.00"),
               "Services", "SV-8", kind="service", income="4010", expense="5030"),
       Product("SVC-CUT", "Timber and board cutting", "job", Decimal("35.00"), Decimal("20.00"),
               "Services", "SV-8", kind="service", income="4010", expense="5030")]
)
PRODUCT_BY_SKU = {p.sku: p for p in PRODUCTS}


def cents(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Line:
    sku: str | None  # None: a line for something that is not in the catalogue (a utility or repair bill)
    quantity: Decimal
    price: Decimal
    discount: Decimal = ZERO
    account: str | None = None  # overrides the product's income account (returns go to 4090, bills to an expense)
    note: str | None = None
    code: str | None = None  # tax-code label when there is no product to take it from

    @property
    def product(self):
        return PRODUCT_BY_SKU[self.sku]

    @property
    def net(self):
        return cents(self.quantity * self.price) - self.discount

    @property
    def tax_code(self):
        return self.code or self.product.tax_code

    @property
    def tax(self):
        return cents(self.net * TAX_CODES[self.tax_code][1])

    @property
    def account_code(self):
        return self.account or self.product.income

    @property
    def description(self):
        return self.note or self.product.name


def L(sku, quantity, price=None, discount=0, account=None, note=None, code=None) -> Line:
    """A document line; the price defaults to the product's sale price, a discount is typed in ringgit."""
    return Line(sku, Decimal(str(quantity)), Decimal(str(price)) if price is not None else PRODUCT_BY_SKU[sku].sale,
                cents(discount), account, note, code)


# ------------------------------------------------------------------ quotations and sales history
@dataclass(frozen=True)
class Quote:
    """A quotation that is not (yet) turned into an invoice: still open, expired, or accepted into a sales order."""
    number: str
    contact: str
    issued: date
    valid_until: date
    description: str
    lines: tuple[Line, ...]
    outcome: str  # open | expired | accepted
    via: tuple[str, ...] = ()  # sales-order / delivery-order made from an accepted quotation


@dataclass(frozen=True)
class CashPayment:
    posted: date
    reference: str
    contact: str
    side: str  # in: payment received from a customer; out: payment made to a supplier
    targets: tuple[tuple[str, Decimal | None], ...]  # (document number, amount); None pays what is still owed
    note: str


def _sale(number, kind, contact, issued, description, lines, *, related=None, quote=None, quoted=None, via=(),
          valid_days=14):
    from ai.synthetic.knt_pack import Document

    terms = _DETAIL_ROWS[contact][0]
    subtotal = sum((line.net for line in lines), ZERO)
    tax = sum((line.tax for line in lines), ZERO)
    due = None if kind == "credit-note" else issued + timedelta(days=terms)
    # The single account on a Document is only a label here; the per-account split comes from the lines.
    return Document(number, kind, contact, issued, due, description, subtotal, tax, lines[0].account_code, related,
                    quote=quote, lines=tuple(lines), flow=tuple(via), quoted=quoted,
                    valid_until=(quoted + timedelta(days=valid_days)) if quoted else None)


def _sales():
    d = date

    def inv(number, contact, issued, description, lines, **kw):
        return _sale(number, "invoice", contact, issued, description, lines, **kw)

    return [
        # September
        inv("SI-2609-03", "MER", d(2026, 9, 12), "Paint and sealant for the Jalan Meranti renovation",
            [L("PNT-18W", 6), L("PNT-PRM", 3), L("PNT-ENM", 4), L("SIL-GP", 12)]),
        _sale("CN-2609-02", "credit-note", "MER", d(2026, 9, 20), "One pail returned unopened",
              [L("PNT-18W", 1, account="4090")], related="SI-2609-03"),
        inv("SI-2609-04", "BEL", d(2026, 9, 16), "Rebar and mesh for the Belian shophouse slab",
            [L("RBR-12", 60), L("RBR-10", 80), L("WMS-6", 10), L("SVC-DLV", 1)],
            quote="QT-2609-03", quoted=d(2026, 9, 9), via=("sales-order", "delivery-order")),
        inv("SI-2609-05", "KEM", d(2026, 9, 19), "Switchgear and lighting for the Kempas workshop",
            [L("CBL-25", 4), L("MCB-20", 12), L("SWT-2G", 20), L("LED-18", 30), L("TRK-25", 25)],
            quote="QT-2609-04", quoted=d(2026, 9, 12)),
        inv("SI-2609-06", "RAM", d(2026, 9, 22), "Boards and fixings for a show-unit fit-out",
            [L("PLY-12", 14), L("DRY-9", 20), L("SCR-DRY", 4), L("HNG-4", 10), L("SVC-CUT", 3)]),
        inv("SI-2609-07", "JEL", d(2026, 9, 24), "Paint stock for resale (trade discount)",
            [L("PNT-5W", 24, discount="64.80"), L("PNT-ENM", 36, discount="102.60"), L("SLV-TH", 12),
             L("SIL-GP", 24)]),
        inv("SI-2609-08", "CMP", d(2026, 9, 26), "Term-start office and classroom supplies",
            [L("A4-80", 10), L("PEN-BP", 4), L("FIL-LA", 30), L("STP-24", 6), L("NTB-A5", 5)]),
        inv("SI-2609-09", "DUR", d(2026, 9, 29), "Plumbing for the cafe kitchen",
            [L("PVC-2", 12), L("ELB-4", 10), L("TAP-BIB", 6), L("WC-CST", 2), L("SVC-INS", 2)],
            quote="QT-2609-05", quoted=d(2026, 9, 22), via=("sales-order",)),
        # October
        inv("SI-2610-03", "NYA", d(2026, 10, 3), "Masonry materials for Phase 2, Kota Samarahan",
            [L("CEM-50", 120), L("SND-FN", 6), L("GRV-20", 6), L("BRK-RED", 3), L("MRT-25", 20), L("SVC-DLV", 3)],
            quote="QT-2609-07", quoted=d(2026, 9, 25), via=("delivery-order",)),
        _sale("DN-2610-02", "debit-note", "NYA", d(2026, 10, 8), "Two extra delivery trips to site",
              [L("SVC-DLV", 2)], related="SI-2610-03"),
        inv("SI-2610-04", "BAK", d(2026, 10, 7), "Timber and boards for the jetty office",
            [L("TMB-24", 60), L("PLY-12", 30), L("NL-3", 40), L("SCR-DRY", 6), L("SVC-CUT", 4)]),
        inv("SI-2610-05", "KEL", d(2026, 10, 9), "Clinic extension: boards, paint, lighting",
            [L("DRY-9", 40), L("PNT-18W", 8), L("LED-18", 40), L("SWT-2G", 14), L("SVC-INS", 3)]),
        inv("SI-2610-06", "LAN", d(2026, 10, 12), "Common-area lighting and door hardware",
            [L("LED-18", 120), L("MCB-20", 20), L("TRK-25", 60), L("LCK-CYL", 10), L("HNG-4", 30)]),
        inv("SI-2610-07", "MEN", d(2026, 10, 14), "Warehouse water supply and sanitary",
            [L("PVC-4", 30), L("ELB-4", 30), L("TNK-1000", 2), L("TAP-BIB", 8)]),
        inv("SI-2610-08", "PIN", d(2026, 10, 17), "Shop supplies",
            [L("A4-80", 4), L("TNR-HP", 3), L("PEN-BP", 2), L("NTB-A5", 3)]),
        inv("SI-2610-09", "SEN", d(2026, 10, 20), "Bathroom fit-out sundries",
            [L("TAP-BIB", 4), L("PVC-2", 6), L("SIL-GP", 6)]),
        inv("SI-2610-10", "RAS", d(2026, 10, 21), "Workshop lighting and sealants",
            [L("LED-18", 24), L("SIL-GP", 20), L("PNT-ENM", 12)]),
        inv("SI-2610-11", "BEL", d(2026, 10, 23), "Structure materials for the Belian shophouse, second pour",
            [L("CEM-50", 300), L("SND-FN", 14), L("GRV-20", 14), L("BRK-RED", 5), L("RBR-12", 120),
             L("WMS-6", 20), L("SVC-DLV", 5)],
            quote="QT-2610-04", quoted=d(2026, 10, 14), via=("sales-order",)),
        _sale("CN-2610-01", "credit-note", "LAN", d(2026, 10, 26), "Ten tubes returned, wrong colour temperature",
              [L("LED-18", 10, account="4090")], related="SI-2610-06"),
        inv("SI-2610-12", "TEM", d(2026, 10, 28), "Boards and timber for the Tempinis showroom",
            [L("PLY-12", 40), L("TMB-24", 80), L("DRY-9", 15), L("SCR-DRY", 5), L("SVC-CUT", 5)]),
        # November
        inv("SI-2611-03", "KEM", d(2026, 11, 3), "Phase 2 wiring for the Kempas workshop",
            [L("MCB-20", 30), L("CBL-25", 6), L("SWT-2G", 30), L("LED-18", 60), L("TRK-25", 40), L("SVC-INS", 4)],
            quote="QT-2610-05", quoted=d(2026, 10, 27)),
        inv("SI-2611-04", "RAM", d(2026, 11, 5), "Finishing materials for two show units",
            [L("PNT-18W", 10), L("PNT-PRM", 4), L("DRY-9", 30), L("SIL-GP", 10), L("SVC-INS", 2)]),
        inv("SI-2611-05", "JEL", d(2026, 11, 6), "Paint stock for resale (trade discount)",
            [L("PNT-5W", 30, discount="81.00"), L("PNT-ENM", 48, discount="136.80"), L("SLV-TH", 12)]),
        inv("SI-2611-06", "CMP", d(2026, 11, 9), "Office and classroom supplies",
            [L("A4-80", 12), L("TNR-HP", 2), L("FIL-LA", 40), L("PEN-BP", 3), L("STP-24", 4)]),
        inv("SI-2611-07", "DUR", d(2026, 11, 10), "Cafe bar and wash-area plumbing",
            [L("PVC-2", 20), L("ELB-4", 16), L("TAP-BIB", 8), L("SIL-GP", 10)]),
        inv("SI-2611-08", "MEN", d(2026, 11, 12), "Second warehouse: water tanks and piping",
            [L("PVC-4", 60), L("ELB-4", 60), L("TAP-BIB", 10), L("TNK-1000", 3), L("SVC-DLV", 2)],
            quote="QT-2611-03", quoted=d(2026, 11, 3), via=("delivery-order",)),
        inv("SI-2611-09", "BAK", d(2026, 11, 13), "Boards and fixings for the pontoon store",
            [L("TMB-24", 50), L("PLY-12", 24), L("NL-3", 30), L("HNG-4", 20), L("SVC-CUT", 3)]),
        inv("SI-2611-10", "KU", d(2026, 11, 14), "Printer and paper supplies",
            [L("A4-80", 6), L("TNR-HP", 2), L("STP-24", 3)]),
        inv("SI-2611-11", "KEL", d(2026, 11, 16), "Treatment-room finishing",
            [L("PNT-5W", 12), L("LED-18", 30), L("DRY-9", 20), L("SVC-INS", 2)]),
        inv("SI-2611-12", "LAN", d(2026, 11, 17), "Stairwell lighting and door locks",
            [L("LED-18", 80), L("TRK-25", 40), L("LCK-CYL", 8), L("MCB-20", 10)]),
        inv("SI-2611-13", "RAS", d(2026, 11, 18), "Spray-booth sealants and enamel",
            [L("PNT-ENM", 10), L("SLV-TH", 6), L("SIL-GP", 12), L("LED-18", 12)]),
        inv("SI-2611-14", "TEM", d(2026, 11, 19), "Boards and timber for a furniture order",
            [L("PLY-12", 30), L("TMB-24", 60), L("SCR-DRY", 4), L("HNG-4", 16), L("SVC-CUT", 4)]),
        _sale("DN-2611-01", "debit-note", "KEM", d(2026, 11, 19), "Extra site visit for the Phase 2 wiring",
              [L("SVC-INS", 1)], related="SI-2611-03"),
        inv("SI-2611-15", "NYA", d(2026, 11, 20), "Masonry materials for Phase 3, Kota Samarahan",
            [L("CEM-50", 200), L("SND-FN", 10), L("GRV-20", 10), L("MRT-25", 30), L("BRK-RED", 4), L("SVC-DLV", 4)],
            quote="QT-2611-04", quoted=d(2026, 11, 11), via=("sales-order", "delivery-order")),
        inv("SI-2611-16", "PIN", d(2026, 11, 21), "Shop supplies",
            [L("A4-80", 3), L("PEN-BP", 2), L("FIL-LA", 20), L("NTB-A5", 4)]),
        _sale("CN-2611-01", "credit-note", "MEN", d(2026, 11, 21), "One tank returned, cracked on delivery",
              [L("TNK-1000", 1, account="4090")], related="SI-2611-08"),
    ]


def _quotes():
    d = date
    return [
        Quote("QT-2609-06", "NYA", d(2026, 9, 5), d(2026, 9, 19), "Timber package, not taken up",
              (L("TMB-24", 100), L("PLY-12", 20), L("NL-3", 50)), "expired"),
        Quote("QT-2610-03", "MEN", d(2026, 10, 1), d(2026, 10, 15), "Pipe quotation, customer went elsewhere",
              (L("PVC-4", 40), L("ELB-4", 40), L("TNK-1000", 1)), "expired"),
        Quote("QT-2610-06", "RAM", d(2026, 10, 27), d(2026, 11, 10), "Joinery boards, validity lapsed",
              (L("PLY-12", 50), L("TMB-24", 40), L("HNG-4", 40)), "expired"),
        Quote("QT-2611-05", "BEL", d(2026, 11, 18), d(2026, 12, 2), "Third pour: accepted, sales order issued",
              (L("CEM-50", 250), L("SND-FN", 12), L("GRV-20", 12), L("RBR-12", 100), L("SVC-DLV", 4)),
              "accepted", ("sales-order",)),
        Quote("QT-2611-06", "DUR", d(2026, 11, 20), d(2026, 12, 4), "Second outlet plumbing",
              (L("PVC-2", 24), L("TAP-BIB", 10), L("WC-CST", 2), L("SVC-INS", 3)), "open"),
        Quote("QT-2611-07", "MER", d(2026, 11, 22), d(2026, 12, 6), "Exterior repaint, three units",
              (L("PNT-18W", 14), L("PNT-PRM", 6), L("SIL-GP", 18), L("SVC-INS", 4)), "open"),
        Quote("QT-2611-08", "CMP", d(2026, 11, 16), d(2026, 11, 30), "Examination-week stationery",
              (L("A4-80", 20), L("PEN-BP", 6), L("NTB-A5", 12), L("TNR-HP", 4)), "open"),
        Quote("QT-2611-09", "RAS", d(2026, 11, 19), d(2026, 12, 3), "New bay lighting and wiring",
              (L("LED-18", 40), L("CBL-25", 3), L("MCB-20", 10), L("SWT-2G", 8), L("SVC-INS", 2)), "open"),
    ]


def _cash():
    d, D = date, Decimal

    def pay(*args):
        return CashPayment(*args)

    return [
        pay(d(2026, 9, 27), "OR-2609-11", "MER", "in", (("SI-2609-03", None),), "Meranti paid after the credit note"),
        pay(d(2026, 10, 2), "OR-2610-11", "KEM", "in", (("SI-2609-05", None),), "Kempas paid on the due date"),
        pay(d(2026, 10, 9), "OR-2610-12", "BEL", "in", (("SI-2609-04", None),), "Belian paid in full"),
        pay(d(2026, 10, 12), "OR-2610-13", "JEL", "in", (("SI-2609-07", None),), "Jelutong paid early"),
        pay(d(2026, 10, 16), "OR-2610-14", "RAM", "in", (("SI-2609-06", None),), "Ramin paid in full"),
        pay(d(2026, 10, 24), "OR-2610-15", "CMP", "in", (("SI-2609-08", None),), "Cempaka paid in full"),
        pay(d(2026, 10, 28), "OR-2610-16", "DUR", "in", (("SI-2609-09", D("1000.00")),), "Durian Burung part payment"),
        pay(d(2026, 10, 30), "PV-2610-11", "PAU", "out", (("PI-2610-01", D("300.00")),), "Cash part payment to Pauh"),
        pay(d(2026, 11, 2), "OR-2611-11", "NYA", "in", (("SI-2610-03", None), ("DN-2610-02", None)),
            "Nyatoh paid the invoice and the extra delivery"),
        pay(d(2026, 11, 6), "OR-2611-12", "BAK", "in", (("SI-2610-04", None),), "Bakau paid on the due date"),
        pay(d(2026, 11, 9), "OR-2611-13", "KEL", "in", (("SI-2610-05", None),), "Kelulut paid in full"),
        pay(d(2026, 11, 12), "PV-2611-11", "PAU", "out", (("PI-2611-01", D("800.00")),), "Cash part payment to Pauh"),
        pay(d(2026, 11, 13), "OR-2611-14", "PIN", "in", (("SI-2610-08", None),), "Pinang Sari paid late"),
        pay(d(2026, 11, 16), "OR-2611-15", "SEN", "in", (("SI-2610-09", None),), "Senduduk paid in full"),
        pay(d(2026, 11, 20), "OR-2611-16", "BEL", "in", (("SI-2610-11", D("3000.00")),), "Belian part payment"),
        # Supplier bills settled in cash (oldest first); the rest stay open, some of them overdue.
        pay(d(2026, 9, 27), "PV-2609-11", "SAN", "out", (("SAN-2609-01", None),), "Haulage paid on the due date"),
        pay(d(2026, 9, 28), "PV-2609-12", "KEN", "out", (("KEN-2609-01", None),), "Telephone and internet"),
        pay(d(2026, 10, 14), "PV-2610-12", "MAT", "out", (("MAT-2609-01", None),), "Timber paid in full"),
        pay(d(2026, 10, 20), "PV-2610-13", "BAT", "out", (("BAT-2609-01", None),), "Electrical stock paid in full"),
        pay(d(2026, 10, 20), "PV-2610-14", "KUC", "out", (("KUC-2609-01", None),), "Stationery paid in full"),
        pay(d(2026, 10, 24), "PV-2610-15", "SAM", "out", (("SAM-2609-01", None),), "Steel and hardware paid in full"),
        pay(d(2026, 10, 29), "PV-2610-16", "SAN", "out", (("SAN-2610-01", None),), "Haulage paid late"),
        pay(d(2026, 10, 30), "PV-2610-17", "LUN", "out", (("LUN-2610-01", None),), "Forklift repair paid"),
        pay(d(2026, 11, 4), "PV-2611-12", "KEN", "out", (("KEN-2610-01", None),), "Telephone and internet"),
        pay(d(2026, 11, 6), "PV-2611-13", "KAN", "out", (("KAN-2609-01", None),), "Paint paid, three weeks overdue"),
        pay(d(2026, 11, 9), "PV-2611-14", "SAM", "out", (("SAM-2610-01", None),), "Steel and hardware paid in full"),
        pay(d(2026, 11, 13), "PV-2611-15", "SAR", "out", (("SAR-2609-01", None),), "Pipes paid, overdue"),
        pay(d(2026, 11, 16), "PV-2611-16", "KUC", "out", (("KUC-2610-01", None),), "Stationery paid in full"),
        pay(d(2026, 11, 17), "PV-2611-17", "SAN", "out", (("SAN-2611-01", None),), "Haulage paid on the due date"),
        pay(d(2026, 11, 19), "PV-2611-18", "BAT", "out", (("BAT-2610-01", None),), "Electrical stock paid in full"),
    ]


def _supplier_of(sku):
    """Who KNT buys each product from. Own labour (installation, cutting) is not bought in."""
    product = PRODUCT_BY_SKU[sku]
    if sku == "SVC-DLV":
        return "SAN"
    if product.kind == "service":
        return None
    if sku in {"PLY-12", "TMB-24", "DRY-9"}:
        return "MAT"
    return {"Cement and aggregates": "SEL", "Steel and timber": "SAM", "Hardware and fixings": "SAM",
            "Paint and finishing": "KAN", "Plumbing and sanitary": "SAR", "Electrical": "BAT",
            "Office supplies": "KUC"}[product.group]


def bill_number(supplier, month_tag):
    return f"{supplier}-{month_tag}-01"


def _bills(sales):
    """Typed supplier bills: each month the stock and haulage sold that month, bought at the product's purchase
    price from the usual supplier, plus a telephone bill every month and one machinery repair."""
    bills = []
    for month, billed_on in ((9, date(2026, 9, 3)), (10, date(2026, 10, 2)), (11, date(2026, 11, 2))):
        tag = f"26{month:02d}"
        wanted = {}
        for doc in sales:
            if doc.kind in {"invoice", "debit-note"} and doc.issued.month == month:
                for line in doc.lines:
                    supplier = _supplier_of(line.sku)
                    if supplier:
                        wanted.setdefault(supplier, {})
                        wanted[supplier][line.sku] = wanted[supplier].get(line.sku, ZERO) + line.quantity
        for supplier in sorted(wanted):
            lines = [L(sku, qty, PRODUCT_BY_SKU[sku].cost, account=PRODUCT_BY_SKU[sku].expense)
                     for sku, qty in sorted(wanted[supplier].items())]
            bills.append(_bill(supplier, tag, billed_on, f"Stock bought for {billed_on:%B} sales", lines))
    for month, day in ((9, 15), (10, 15), (11, 16)):
        issued = date(2026, month, day)
        bills.append(_bill("KEN", f"26{month:02d}", issued, "Office internet and telephone",
                           [L(None, 1, "188.00", account="6130", note="Office internet and telephone", code="SV-8")]))
    bills.append(_bill("LUN", "2610", date(2026, 10, 14), "Forklift service and repair",
                       [L(None, 1, "650.00", account="6150", note="Forklift service and hydraulic repair",
                          code="SV-8")]))
    return sorted(bills, key=lambda doc: (doc.issued, doc.number))


def _bill(supplier, tag, issued, description, lines):
    from ai.synthetic.knt_pack import Document

    return Document(bill_number(supplier, tag), "bill", supplier, issued,
                    issued + timedelta(days=_DETAIL_ROWS[supplier][0]), description,
                    sum((line.net for line in lines), ZERO), sum((line.tax for line in lines), ZERO),
                    lines[0].account_code, lines=tuple(lines))


@dataclass(frozen=True)
class Extension:
    sales: list
    quotes: list
    cash: list  # BankLine records of kind cash_in / cash_out with every amount resolved
    bills: list  # typed supplier bills


def extension() -> Extension:
    """Only what this module adds to the original KNT books."""
    from ai.synthetic.knt_pack import BankLine, books

    base_sales, purchases, base_bank = books(include_extension=False)
    sales = _sales()
    bills = _bills(sales)
    left = {doc.number: doc.total for doc in (*base_sales, *sales) if doc.kind in {"invoice", "debit-note"}}
    left.update({doc.number: doc.total for doc in (*purchases, *bills) if doc.kind in {"invoice", "bill"}})
    for doc in (*base_sales, *sales):
        if doc.kind == "credit-note":
            left[doc.related] -= doc.total
    for line in base_bank:  # bank payments already allocated to the original documents
        for number, amount in line.targets:
            if number in left:
                left[number] -= amount
    cash = []
    for payment in _cash():
        targets = []
        for number, amount in payment.targets:
            amount = left[number] if amount is None else amount
            if amount <= 0 or amount > left[number]:
                raise ValueError(f"{payment.reference} pays {amount} against {number}, which has {left[number]} left")
            left[number] -= amount
            targets.append((number, amount))
        total = sum((amount for _, amount in targets), ZERO)
        cash.append(BankLine(payment.posted, payment.reference, payment.note,
                             total if payment.side == "in" else -total,
                             "cash_in" if payment.side == "in" else "cash_out", tuple(targets)))
    return Extension(sales, _quotes(), cash, bills)
