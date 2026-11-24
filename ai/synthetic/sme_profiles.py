"""Three fictional Malaysian SMEs for team testing: a Kuching café, a Miri hardware supplier and a Kota Kinabalu
advisory firm. Every name, number, address and email is invented; contact streets end in "Contoh" (example) and
email domains in ".test". Only the banks are real Malaysian banks, named on the bank-account label as KNT names
Public Bank; account numbers are invented and no bank's branding or statement design is reproduced.

Tax is typed as printed by each fictional supplier or customer document; the generator decides the printed amount
once and the application only ever receives it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class Item:
    """A product or service on the company's catalogue."""
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
    supplier: str | None = None  # who the company buys it from (typed stock bills); None: own service


@dataclass(frozen=True)
class Party:
    code: str
    kind: str  # customer | supplier
    name: str
    street: str
    city: str
    postcode: str
    terms: int
    category: str
    account: str  # default income (customers) or expense (suppliers) account


@dataclass(frozen=True)
class Overhead:
    """A typed supplier bill raised every month (rent, internet, software...)."""
    supplier: str
    account: str
    note: str
    amount: Decimal
    tax_code: str
    day: int
    paid: bool = True  # paid through the bank on its due date when that falls before the cut-off


@dataclass(frozen=True)
class InboxSource:
    """A supplier whose documents arrive in the Inbox (captured, then approved and posted)."""
    supplier: str
    account: str
    description: str
    nets: tuple[Decimal, Decimal, Decimal]  # one printed subtotal per month
    rate: Decimal  # the printed tax rate on the supplier's document


@dataclass(frozen=True)
class Profile:
    code: str
    name: str
    trade: str
    ssm: str
    tin: str
    sst: str
    street: str
    city: str
    state: str
    postcode: str
    phone_area: str
    bank_name: str
    bank_short: str
    bank_account_number: str
    bank_layout: str  # signed | debit_credit
    opening_bank: Decimal
    opening_cash: Decimal
    tax_codes: dict
    payment_terms: dict
    items: tuple[Item, ...]
    parties: tuple[Party, ...]
    overheads: tuple[Overhead, ...]
    stock_inbox: InboxSource  # monthly supplier invoice in the Inbox, plus the October credit and debit notes
    utility_inbox: InboxSource  # monthly electricity bill in the Inbox
    receipt_inbox: InboxSource  # monthly cash receipt in the Inbox
    flow: tuple[str, ...]  # what an accepted quotation passes through before the invoice
    invoices_per_month: int
    cash_customers: tuple[str, ...] = ()  # customers who pay at the counter (cash in hand)
    series: tuple[str, str, str] = ("99930", "2023021", "9993")  # TIN, SSM and SST number stems for contacts
    seed: int = 1
    fixture_emails: dict = field(default_factory=dict)

    @property
    def address(self):
        return f"{self.street}, {self.postcode} {self.city}, {self.state}"

    @property
    def bank_label(self):
        return f"{self.bank_short} Current Account ••{self.bank_account_number[-4:]}"


TERMS = {"COD": ("Cash on delivery", 0), "NET14": ("Net 14 days", 14), "NET30": ("Net 30 days", 30),
         "NET45": ("Net 45 days", 45)}


def _service(sku, name, unit, sale, group, tax, income="4010"):
    return Item(sku, name, unit, D(sale), D("0"), group, tax, kind="service", income=income, expense="5030")


# --------------------------------------------------------------------------- TKK: Kuching café and catering
TKK = Profile(
    code="TKK", name="Teratai Kopi & Katering Sdn. Bhd.", trade="café and corporate catering",
    ssm="202301045118", tin="C9990003302", sst="Q10-2611-99900033",
    street="No. 8, Lorong Teratai Contoh 3", city="Kuching", state="Sarawak", postcode="93100", phone_area="082",
    bank_name="Malayan Banking Berhad", bank_short="Maybank", bank_account_number="514099004402",
    bank_layout="signed", opening_bank=D("15000.00"), opening_cash=D("800.00"),
    tax_codes={"SV-6": ("Service tax 6% (food and beverage)", D("0.06")),
               "SV-8": ("Service tax 8% (delivery, staffing, other services)", D("0.08")),
               "SST-0": ("Not subject to SST", D("0"))},
    payment_terms=TERMS,
    items=(
        _service("CAT-BUF", "Buffet lunch package", "pax", "28.00", "Catering packages", "SV-6", "4020"),
        _service("CAT-HT", "Hi-tea package", "pax", "18.00", "Catering packages", "SV-6", "4020"),
        _service("CAT-BOX", "Packed nasi lemak meal box", "box", "9.50", "Catering packages", "SV-6", "4020"),
        _service("CAT-COF", "Coffee-break set (kuih and drinks)", "pax", "12.00", "Catering packages", "SV-6", "4020"),
        _service("CAT-LAK", "Sarawak laksa live station", "pax", "15.00", "Catering packages", "SV-6", "4020"),
        _service("BEV-KOP", "Kopi-O urn (40 cups)", "urn", "85.00", "Beverages", "SV-6", "4020"),
        _service("BEV-TEH", "Teh tarik urn (40 cups)", "urn", "90.00", "Beverages", "SV-6", "4020"),
        Item("CAK-KEK", "Kek lapis, whole (1 kg)", "pc", D("65.00"), D("38.00"), "Bakery", "SV-6", stock=24,
             income="4020", expense="5040", supplier="LAP"),
        Item("CAK-PUF", "Curry puffs (tray of 30)", "tray", D("36.00"), D("21.00"), "Bakery", "SV-6", stock=30,
             income="4020", expense="5040", supplier="LAP"),
        _service("SVC-DLV", "Delivery and set-up", "trip", "60.00", "Services", "SV-8"),
        _service("SVC-STF", "Service staff (per hour)", "hour", "25.00", "Services", "SV-8"),
    ),
    parties=(
        Party("MEL", "customer", "Melati Events & Weddings Sdn. Bhd.", "Lot 14, Jalan Melati Contoh", "Kuching", "93150", 30, "events", "4020"),
        Party("KEN", "customer", "Kenanga Training Centre Sdn. Bhd.", "No. 22, Jalan Kenanga Contoh", "Kuching", "93200", 30, "corporate", "4020"),
        Party("ORK", "customer", "Orkid Family Clinic", "No. 5, Lorong Orkid Contoh 1", "Kuching", "93250", 14, "corporate", "4020"),
        Party("RAF", "customer", "Raflesia Property Management Sdn. Bhd.", "Lot 30, Jalan Raflesia Contoh", "Kuching", "93350", 45, "corporate", "4020"),
        Party("SEM", "customer", "Semarak Learning Centre", "No. 9, Jalan Semarak Contoh", "Kota Samarahan", "94300", 14, "education", "4020"),
        Party("BUN", "customer", "Bunga Raya Insurance Agency", "No. 41, Jalan Bunga Raya Contoh", "Kuching", "93100", 30, "corporate", "4020"),
        Party("MAW", "customer", "Mawar Corporate Services Sdn. Bhd.", "Lot 7, Jalan Mawar Contoh", "Kuching", "93300", 30, "corporate", "4020"),
        Party("CEP", "customer", "Cempaka Walk-in Orders", "Counter sales, No. 8, Lorong Teratai Contoh 3", "Kuching", "93100", 0, "walk-in", "4020"),
        Party("SEG", "supplier", "Segar Pagi Fresh Produce Trading", "Lot 3, Jalan Pasar Contoh", "Kuching", "93050", 14, "trade", "5040"),
        Party("BIJ", "supplier", "Biji Emas Coffee Roastery Sdn. Bhd.", "No. 16, Jalan Kopi Contoh", "Kuching", "93450", 30, "trade", "5040"),
        Party("LAP", "supplier", "Lapis Manis Home Bakery", "No. 2, Lorong Lapis Contoh", "Kuching", "93150", 14, "trade", "5040"),
        Party("HIJ", "supplier", "Hijau Packaging Supplies", "Lot 21, Jalan Kotak Contoh", "Kuching", "93350", 30, "trade", "5050"),
        Party("ARA", "supplier", "Ara Power Services Sdn. Bhd.", "Level 2, Wisma Ara Contoh", "Kuching", "93050", 14, "utility", "6110"),
        Party("JAT", "supplier", "Jati Realty Sdn. Bhd.", "Lot 50, Jalan Jati Contoh", "Kuching", "93100", 7, "rent", "6100"),
        Party("ROT", "supplier", "Rotan Telecom Services", "No. 11, Jalan Rotan Contoh", "Kuching", "93200", 14, "utility", "6130"),
        Party("PAS", "supplier", "Pasar Contoh Market Stall 12", "Pasar Contoh, Jalan Pasar Contoh", "Kuching", "93050", 0, "trade", "5040"),
    ),
    overheads=(
        Overhead("JAT", "6100", "Cafe premises rent", D("2600.00"), "SST-0", 1),
        Overhead("ROT", "6130", "Cafe internet and telephone", D("168.00"), "SV-8", 12),
        Overhead("BIJ", "5040", "Coffee beans and tea leaves", D("640.00"), "SST-0", 4),
        Overhead("HIJ", "5050", "Meal boxes, cups and cutlery", D("420.00"), "SST-0", 6, paid=False),
    ),
    stock_inbox=InboxSource("SEG", "5040", "Fresh vegetables, chicken and eggs",
                            (D("1260.00"), D("1385.00"), D("1190.00")), D("0")),
    utility_inbox=InboxSource("ARA", "6110", "Cafe electricity", (D("612.00"), D("648.00"), D("596.00")), D("0.08")),
    receipt_inbox=InboxSource("PAS", "5040", "Market herbs and limes paid in cash", (D("46.50"), D("52.00"), D("38.70")), D("0")),
    flow=("sales-order",), invoices_per_month=10, cash_customers=("CEP",),
    series=("99930", "2023021", "9993"), seed=31,
)


# --------------------------------------------------------------------------- EHB: Miri hardware and building supply
EHB = Profile(
    code="EHB", name="Enggang Hardware & Building Supply Sdn. Bhd.", trade="hardware, construction materials and equipment rental",
    ssm="201901023774", tin="C9990004403", sst="B16-2611-99900044",
    street="Lot 1207, Jalan Enggang Contoh", city="Miri", state="Sarawak", postcode="98000", phone_area="085",
    bank_name="RHB Bank Berhad", bank_short="RHB", bank_account_number="21208400007715",
    bank_layout="debit_credit", opening_bank=D("32000.00"), opening_cash=D("600.00"),
    tax_codes={"SST-10": ("Sales tax 10% (taxable goods)", D("0.10")),
               "SST-5": ("Sales tax 5% (cement, sand, bricks)", D("0.05")),
               "SV-8": ("Service tax 8% (rental, delivery)", D("0.08")),
               "SST-0": ("Not subject to SST", D("0"))},
    payment_terms=TERMS,
    items=(
        Item("CEM-50", "Portland cement 50 kg", "bag", D("25.00"), D("20.10"), "Cement and aggregates", "SST-5", 500, supplier="BEL"),
        Item("SND-RV", "River sand", "tonne", D("70.00"), D("54.00"), "Cement and aggregates", "SST-5", 30, supplier="BEL"),
        Item("BRK-CM", "Cement bricks", "1,000 pcs", D("480.00"), D("380.00"), "Cement and aggregates", "SST-5", 10, supplier="BEL"),
        Item("STL-Y10", "Steel bar Y10 (12 m)", "pc", D("27.50"), D("21.40"), "Steel and roofing", "SST-10", 300, supplier="MER"),
        Item("STL-Y12", "Steel bar Y12 (12 m)", "pc", D("39.00"), D("30.80"), "Steel and roofing", "SST-10", 250, supplier="MER"),
        Item("ROF-ZN", "Zinc-aluminium roofing sheet (3 m)", "sheet", D("46.00"), D("35.50"), "Steel and roofing", "SST-10", 120, supplier="MER"),
        Item("PLY-18", "Plywood 18 mm (4 x 8 ft)", "sheet", D("78.00"), D("61.00"), "Timber", "SST-10", 80, supplier="KEM"),
        Item("PVC-50", "PVC pipe 50 mm (6 m)", "length", D("22.00"), D("16.20"), "Plumbing", "SST-10", 150, supplier="KEM"),
        Item("PNT-EX", "Exterior emulsion paint 18 L", "pail", D("185.00"), D("142.00"), "Paint", "SST-10", 40, supplier="KEM"),
        Item("NAL-3", "Wire nails 3 in (5 kg)", "box", D("32.00"), D("24.00"), "Hardware", "SST-10", 60, supplier="MER"),
        _service("RNT-MIX", "Concrete mixer hire", "day", "95.00", "Equipment rental", "SV-8"),
        _service("RNT-SCF", "Scaffolding set hire", "week", "160.00", "Equipment rental", "SV-8"),
        _service("SVC-LRY", "Lorry delivery within Miri", "trip", "120.00", "Services", "SV-8"),
    ),
    parties=(
        Party("KNY", "customer", "Kenyalang Builders Sdn. Bhd.", "Lot 88, Jalan Kenyalang Contoh", "Miri", "98000", 45, "contractor", "4000"),
        Party("MUR", "customer", "Murai Construction & Engineering", "Lot 302, Jalan Murai Contoh", "Miri", "98100", 30, "contractor", "4000"),
        Party("PUC", "customer", "Pucung Renovation Works", "No. 17, Lorong Pucung Contoh 2", "Miri", "98000", 30, "contractor", "4000"),
        Party("BAY", "customer", "Bayan Home Contractor Enterprise", "No. 6, Jalan Bayan Contoh", "Lutong", "98100", 14, "contractor", "4000"),
        Party("HEL", "customer", "Helang Interior Design Sdn. Bhd.", "Lot 55, Jalan Helang Contoh", "Miri", "98009", 30, "contractor", "4000"),
        Party("RAJ", "customer", "Raja Udang Plumbing Services", "No. 23, Jalan Raja Udang Contoh", "Miri", "98000", 14, "contractor", "4000"),
        Party("SER", "customer", "Serindit M&E Engineering Sdn. Bhd.", "Lot 9, Jalan Serindit Contoh", "Bintulu", "97000", 45, "contractor", "4000"),
        Party("BEL", "supplier", "Belatuk Cement Distributors Sdn. Bhd.", "Lot 400, Jalan Belatuk Contoh", "Miri", "98000", 30, "trade", "5000"),
        Party("MER", "supplier", "Merbau Steel Trading Sdn. Bhd.", "Lot 77, Jalan Merbau Contoh", "Bintulu", "97000", 30, "trade", "5000"),
        Party("KEM", "supplier", "Kempas Building Materials Wholesale", "No. 12, Jalan Kempas Contoh", "Miri", "98100", 45, "trade", "5000"),
        Party("NUR", "supplier", "Nuri Power Services Sdn. Bhd.", "Level 3, Wisma Nuri Contoh", "Miri", "98000", 14, "utility", "6110"),
        Party("PEL", "supplier", "Pelangi Yard Properties", "Lot 15, Jalan Pelangi Contoh", "Miri", "98000", 7, "rent", "6100"),
        Party("GAR", "supplier", "Garuda Lorry Workshop", "No. 4, Jalan Garuda Contoh", "Miri", "98100", 14, "trade", "6150"),
        Party("SIA", "supplier", "Sial Hardware Wholesale", "Lot 61, Jalan Sial Contoh", "Miri", "98000", 30, "trade", "5000"),
        Party("STE", "supplier", "Stesen Contoh Fuel Station", "Jalan Stesen Contoh", "Miri", "98000", 0, "trade", "6170"),
    ),
    overheads=(
        Overhead("PEL", "6100", "Yard and showroom rent", D("2200.00"), "SST-0", 1),
        Overhead("GAR", "6150", "Lorry service and tyre change", D("780.00"), "SV-8", 18, paid=False),
    ),
    stock_inbox=InboxSource("SIA", "5000", "Hinges, locks and fasteners for resale",
                            (D("480.00"), D("625.00"), D("410.00")), D("0.10")),
    utility_inbox=InboxSource("NUR", "6110", "Yard and showroom electricity", (D("488.00"), D("512.00"), D("470.00")), D("0.08")),
    receipt_inbox=InboxSource("STE", "6170", "Lorry diesel paid in cash", (D("180.00"), D("205.00"), D("164.00")), D("0")),
    flow=("sales-order", "delivery-order"), invoices_per_month=10,
    series=("99940", "2019014", "9994"), seed=44,
)


# --------------------------------------------------------------------------- CDA: Kota Kinabalu advisory firm
CDA = Profile(
    code="CDA", name="Cendana Advisory Sdn. Bhd.", trade="accounting, tax and business advisory",
    ssm="202001017336", tin="C9990005504", sst="S10-2611-99900055",
    street="Suite 6-2, Menara Cendana Contoh, Jalan Cendana Contoh", city="Kota Kinabalu", state="Sabah",
    postcode="88000", phone_area="088",
    bank_name="CIMB Bank Berhad", bank_short="CIMB", bank_account_number="8604123360",
    bank_layout="signed", opening_bank=D("26000.00"), opening_cash=D("300.00"),
    tax_codes={"SV-8": ("Service tax 8% (professional services)", D("0.08")),
               "SST-0": ("Not subject to SST", D("0"))},
    payment_terms=TERMS,
    items=(
        _service("ACC-RET", "Monthly bookkeeping retainer", "month", "1800.00", "Accounting", "SV-8"),
        _service("TAX-CP", "Corporate tax computation and Form C filing", "job", "2500.00", "Tax", "SV-8"),
        _service("TAX-PIT", "Personal tax filing (Form BE)", "person", "450.00", "Tax", "SV-8"),
        _service("SEC-RET", "Company secretarial retainer", "month", "250.00", "Secretarial", "SV-8"),
        _service("AUD-SUP", "Audit preparation support", "day", "1200.00", "Accounting", "SV-8"),
        _service("PAY-RUN", "Monthly payroll processing", "run", "300.00", "Payroll", "SV-8"),
        _service("ADV-HR", "Business advisory", "hour", "350.00", "Advisory", "SV-8"),
        _service("DIS-SSM", "Disbursement: registry filing fees", "item", "120.00", "Disbursements", "SST-0", "4110"),
    ),
    parties=(
        Party("GAH", "customer", "Gaharu Resort Management Sdn. Bhd.", "Lot 12, Jalan Gaharu Contoh", "Kota Kinabalu", "88300", 30, "hospitality", "4010"),
        Party("PUL", "customer", "Pulai Logistics Sdn. Bhd.", "Lot 5, Jalan Pulai Contoh", "Kota Kinabalu", "88450", 30, "logistics", "4010"),
        Party("KAP", "customer", "Kapur Heritage Crafts", "No. 3, Lorong Kapur Contoh", "Penampang", "89500", 14, "retail", "4010"),
        Party("SRW", "customer", "Serai Wangi Spa & Wellness", "No. 19, Jalan Serai Contoh", "Kota Kinabalu", "88100", 14, "services", "4010"),
        Party("BAM", "customer", "Bambangan Agro Sdn. Bhd.", "Lot 230, Jalan Bambangan Contoh", "Tuaran", "89200", 45, "agriculture", "4010"),
        Party("TUH", "customer", "Tuhau Foods Enterprise", "No. 8, Jalan Tuhau Contoh", "Kota Kinabalu", "88200", 30, "food", "4010"),
        Party("JAR", "customer", "Jarum Electrical Works Sdn. Bhd.", "Lot 44, Jalan Jarum Contoh", "Putatan", "88200", 30, "contractor", "4010"),
        Party("DAM", "customer", "Damar Travel & Tours Sdn. Bhd.", "No. 27, Jalan Damar Contoh", "Kota Kinabalu", "88000", 30, "travel", "4010"),
        Party("SRY", "supplier", "Seraya Properties Sdn. Bhd.", "Level 1, Wisma Seraya Contoh", "Kota Kinabalu", "88000", 7, "rent", "6100"),
        Party("AWN", "supplier", "Awan Cloud Software Sdn. Bhd.", "Suite 3-1, Jalan Awan Contoh", "Kota Kinabalu", "88300", 14, "professional", "6260"),
        Party("LIN", "supplier", "Lindung Insurance Agency", "No. 15, Jalan Lindung Contoh", "Kota Kinabalu", "88100", 30, "professional", "6220"),
        Party("BKT", "supplier", "Bukit Padang Power Services", "Level 2, Wisma Bukit Contoh", "Kota Kinabalu", "88000", 14, "utility", "6110"),
        Party("CTK", "supplier", "Cetak Pantas Printing Services", "No. 6, Lorong Cetak Contoh", "Kota Kinabalu", "88200", 30, "trade", "6140"),
        Party("TAN", "supplier", "Tanjung Aru Associates PLT", "Suite 9-3, Jalan Tanjung Contoh", "Kota Kinabalu", "88100", 30, "professional", "6190"),
        Party("KED", "supplier", "Kedai Contoh Stationery Corner", "No. 1, Jalan Kedai Contoh", "Kota Kinabalu", "88000", 0, "trade", "6140"),
    ),
    overheads=(
        Overhead("SRY", "6100", "Office rent, Suite 6-2", D("2800.00"), "SST-0", 1),
        Overhead("AWN", "6260", "Accounting and payroll software subscription", D("390.00"), "SV-8", 5),
        Overhead("LIN", "6220", "Professional indemnity insurance (monthly instalment)", D("260.00"), "SST-0", 10),
        Overhead("TAN", "6190", "Sub-contracted audit review", D("1500.00"), "SV-8", 20, paid=False),
    ),
    stock_inbox=InboxSource("CTK", "6140", "Printed engagement letters and binding",
                            (D("380.00"), D("455.00"), D("310.00")), D("0.08")),
    utility_inbox=InboxSource("BKT", "6110", "Office electricity", (D("342.00"), D("366.00"), D("329.00")), D("0.08")),
    receipt_inbox=InboxSource("KED", "6140", "Stationery paid in cash", (D("28.90"), D("41.20"), D("33.50")), D("0")),
    flow=(), invoices_per_month=7,
    series=("99950", "2020017", "9995"), seed=55,
)

PROFILES = {profile.code: profile for profile in (TKK, EHB, CDA)}
