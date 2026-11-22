from datetime import date

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
