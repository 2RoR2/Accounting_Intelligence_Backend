from datetime import date

import pytest

from services.admin.erp_demo import Seeder


def test_current_month_period_is_created_once_and_skips_overlap():
    class PeriodAPI:
        def __init__(self):
            self.periods = []

        def call(self, role, method, path, **kwargs):
            assert role == "local_admin"
            assert path == "/api/ledger/periods"
            if method == "POST":
                self.periods.append(kwargs["json"])
            return self

        def json(self):
            return self.periods

    api = PeriodAPI()
    seeder = Seeder(api, "trading", [], None)
    seeder.ensure_current_period(date(2028, 2, 9))
    seeder.ensure_current_period(date(2028, 2, 26))
    assert api.periods == [{"periodCode": "2028-02", "startsOn": "2028-02-01", "endsOn": "2028-02-29"}]

    api.periods.append({"periodCode": "custom", "startsOn": "2028-03-12", "endsOn": "2028-04-14"})
    seeder.ensure_current_period(date(2028, 3, 1))
    assert len(api.periods) == 2


@pytest.mark.parametrize("legacy", [False, True])
def test_demo_catalog_bank_account_matches_import_label_on_seed_and_reseed(legacy):
    path = "/api/erp/catalog/bank-account"
    ledger = {"1010": {"id": "bank-ledger"}}

    class BankAPI:
        entry = {"id": "bank", "name": "Current account", "code": "CA-1010", "ledgerAccountId": "bank-ledger",
                 "isActive": True, "data": {"bankName": "Demo Bank", "accountNumber": "000-111-222"}} if legacy else None

        def __init__(self):
            self.writes = []

        def call(self, role, method, url, **kwargs):
            assert role == "local_admin"
            if method == "GET":
                assert url == path
                response = [self.entry] if self.entry else []
            else:
                assert url == (f"{path}/bank" if legacy else path)
                self.writes.append(method)
                self.entry = {"id": "bank", "isActive": True, **kwargs["json"]}
                response = self.entry
            return type("Response", (), {"json": lambda _: response})()

    api = BankAPI()
    seeder = Seeder(api, "trading", [], None)
    assert seeder.ensure_bank_account(ledger, "123456789") == "bank"
    assert seeder.ensure_bank_account(ledger, "123456789") == "bank"
    assert api.entry["name"] == "Current account 6789"
    assert api.entry["data"] == {"bankName": "Demo Bank", "accountNumber": "123456789"}
    assert api.writes == ["PUT" if legacy else "POST"]
