"""Plan M4 naming rule and M2 direction, without a database."""
from datetime import date

from services import naming
from services.company_identity import direction


def test_display_name_rule_sorts_by_date_and_is_path_safe():
    assert naming.display_name(date(2026, 11, 3), "krm", "INV", "IV-2026-00012", ".PDF") == "2026-11-03_KRM_INV_IV-2026-00012.pdf"
    assert naming.display_name("2026-08-07", "SBJ", naming.type_code(kind="invoice", category="accounts_payable"),
                               "INV/26 03494", ".jpg") == "2026-08-07_SBJ_BILL_INV-26-03494.jpg"
    assert naming.display_name(None, None, "CN", "n/a") == "0000-00-00_NOCONTACT_CN_NONUMBER.pdf"
    assert naming.display_name("07/08/2026", "A B", "DN", "../../etc", ".exe..") == "0000-00-00_A-B_DN_etc.bin"
    names = [naming.display_name(d, "X", "INV", "1") for d in ("2026-10-01", "2026-08-30", "2026-09-15")]
    assert sorted(names) == [names[1], names[2], names[0]]


def test_type_codes():
    assert [naming.type_code(t) for t in ("quotation", "invoice", "credit-note", "debit-note", "payment", "purchase-payment",
                                          "delivery-order")] == ["QT", "INV", "CN", "DN", "OR", "PV", "DO"]
    assert naming.type_code(kind="receipt", category="accounts_payable") == "RCPT"
    assert naming.type_code(kind="credit_note", category="accounts_payable") == "SCN"


def test_contact_code_suggestions_are_short_and_unique():
    assert naming.suggest_contact_code("Kedai Runcit Maju Sdn. Bhd.") == "KRM"
    assert naming.suggest_contact_code("Pulasan Ketapang Hardware Enterprise") == "PKH"
    assert naming.suggest_contact_code("Tenaga Nasional Berhad") == "TN"
    assert naming.suggest_contact_code("Syarikat Bekalan Jaya", taken={"sbj", "SBJ2"}) == "SBJ3"
    assert naming.suggest_contact_code("") == "CONT"


def test_direction_by_name_tin_or_registration():
    me = {"name": "Ketapang Nibong Trading Sdn. Bhd.", "registrationNumber": "202401012345", "tin": "C99999999990"}
    assert direction(me, seller_name="KETAPANG NIBONG TRADING SDN BHD", buyer_name="Kedai") == "sales"
    assert direction(me, seller_name="Other", seller_tin="c9999 9999 990") == "sales"
    assert direction(me, seller_name="Supplier", buyer_name="Ketapang Nibong Trading Sdn. Bhd.") == "purchase"
    assert direction(me, seller_name="Supplier", buyer_registration="2024-0101-2345") == "purchase"
    assert direction(me, seller_name="A", buyer_name="B") == "unknown"
    assert direction({"name": "", "registrationNumber": "", "tin": ""}, seller_name="", buyer_name="") == "unknown"
