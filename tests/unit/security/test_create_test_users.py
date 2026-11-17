"""E1: manage.py create-test-users without a database (parsing, masking, guards, planning, output)."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.admin import test_users as tu

TENANT = uuid4()
OTHER_TENANT = uuid4()
LOCAL_ENV = {"APP_ENV": "development", "DATABASE_URL": "postgresql://postgres:x@127.0.0.1:5433/accounting_db"}


def row(email, role="accountant", tenant_id=TENANT, status="active", must_change=False):
    return {"id": uuid4(), "email": email, "tenant_id": tenant_id, "role": role,
            "status": status, "must_change_password": must_change}


# --- masking ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("email, masked", [
    ("first.last@example.com", "f***@e***.com"),
    ("a@b.co", "a***@b***.co"),
    ("someone@students.uni.example.my", "s***@s***.my"),
])
def test_mask_email_keeps_only_first_letters_and_last_label(email, masked):
    assert tu.mask_email(email) == masked


# --- parsing ---------------------------------------------------------------------------------------

def test_parse_defaults_to_accountant_and_reads_roles():
    users = tu.parse_test_emails(" Boss@Example.com:local_admin , staff@example.com,root@example.com:super_admin ,")
    assert users == [tu.TestUser("boss@example.com", "local_admin"),
                     tu.TestUser("staff@example.com", "accountant"),
                     tu.TestUser("root@example.com", "super_admin")]


@pytest.mark.parametrize("raw, message", [
    ("", "empty"),
    ("staff@example.com", "at least one local admin"),
    ("boss@example.com:local_admin,not-an-email", "entry 2 is not a valid email"),
    ("boss@example.com:owner", "has role 'owner'"),
    ("boss@example.com:local_admin,BOSS@example.com", "more than once"),
])
def test_parse_errors_never_show_the_full_address(raw, message):
    with pytest.raises(tu.TestUsersError) as error:
        tu.parse_test_emails(raw)
    assert message in str(error.value)
    assert "boss@example.com" not in str(error.value).lower()
    assert "not-an-email" not in str(error.value)


# --- guard -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("environ, reason", [
    ({}, "APP_ENV"),
    ({"APP_ENV": "production", "DATABASE_URL": LOCAL_ENV["DATABASE_URL"]}, "APP_ENV"),
    ({"APP_ENV": "development"}, "not set"),
    ({"APP_ENV": "development", "DATABASE_URL": "postgresql://u:p@ep-x.neon.tech/db"}, "ep-x.neon.tech"),
    # libpq lets query parameters and extra hosts replace the URL host: all of them must be local
    ({"APP_ENV": "development", "DATABASE_URL": "postgresql://u:p@127.0.0.1:5433/db?host=ep-x.neon.tech"}, "ep-x.neon.tech"),
    ({"APP_ENV": "development", "DATABASE_URL": "postgresql://u:p@localhost/db?sslmode=require&hostaddr=10.1.2.3"}, "10.1.2.3"),
    ({"APP_ENV": "development", "DATABASE_URL": "postgresql://u:p@127.0.0.1:5433,ep-x.neon.tech:5432/db"}, "ep-x.neon.tech"),
    ({"APP_ENV": "development", "DATABASE_URL": "postgresql:///db?host=ep-x.neon.tech"}, "ep-x.neon.tech"),
    ({"APP_ENV": "development", "DATABASE_URL": "postgresql:///db"}, "no host"),
    ({"APP_ENV": "development", "DATABASE_URL": "postgresql:///db", "PGHOST": "ep-x.neon.tech"}, "ep-x.neon.tech"),
    ({"APP_ENV": "development", "DATABASE_URL": "postgresql://127.0.0.1/db?service=team"}, "service"),
    ({"APP_ENV": "development", "DATABASE_URL": "mysql://127.0.0.1/db"}, "postgresql://"),
    ({**LOCAL_ENV, "S3_BUCKET": "team-bucket"}, "S3_BUCKET"),
])
def test_refuses_anything_but_a_local_development_database(environ, reason):
    assert reason in tu.refusal_reason(environ)


def test_accepts_loopback_database():
    assert tu.refusal_reason(LOCAL_ENV) is None
    assert tu.refusal_reason({**LOCAL_ENV, "DATABASE_URL": "postgresql://p@localhost/db"}) is None
    assert tu.refusal_reason({**LOCAL_ENV, "DATABASE_URL": "postgresql://p@[::1]:5433/db?host=127.0.0.1"}) is None


def test_run_refuses_before_connecting(capsys):
    def connect():
        raise AssertionError("must not connect")
    args = SimpleNamespace(pack=None, company=None, reset_passwords=False)
    assert tu.run(args, environ={"APP_ENV": "development", "DATABASE_URL": "postgresql://h.neon.tech/db"},
                  connect=connect) == 2
    assert "Refusing" in capsys.readouterr().err


# --- planning: roles, tenant, idempotency ----------------------------------------------------------

def test_plan_creates_new_users_and_leaves_matching_ones_alone():
    wanted = tu.parse_test_emails("a@x.com:local_admin,b@x.com")
    changes = tu.plan_changes({"a@x.com": row("a@x.com", "local_admin")}, TENANT, wanted)
    assert [(c.action, c.user.email) for c in changes] == [("unchanged", "a@x.com"), ("create", "b@x.com")]


def test_plan_is_idempotent_once_applied():
    wanted = tu.parse_test_emails("a@x.com:local_admin,b@x.com,c@x.com:super_admin")
    existing = {"a@x.com": row("a@x.com", "local_admin"), "b@x.com": row("b@x.com"),
                "c@x.com": row("c@x.com", "super_admin", tenant_id=None)}
    assert {c.action for c in tu.plan_changes(existing, TENANT, wanted)} == {"unchanged"}


def test_plan_updates_role_status_and_forced_password_change():
    wanted = tu.parse_test_emails("a@x.com:local_admin,b@x.com,c@x.com")
    existing = {"a@x.com": row("a@x.com", "accountant"), "b@x.com": row("b@x.com", status="disabled"),
                "c@x.com": row("c@x.com", must_change=True)}
    changes = tu.plan_changes(existing, TENANT, wanted)
    assert [c.action for c in changes] == ["update", "update", "update"]
    assert [c.role_changed for c in changes] == [True, False, False]


def test_reset_passwords_updates_everyone_existing():
    wanted = tu.parse_test_emails("a@x.com:local_admin")
    changes = tu.plan_changes({"a@x.com": row("a@x.com", "local_admin")}, TENANT, wanted, reset_passwords=True)
    assert changes[0].action == "update"


@pytest.mark.parametrize("existing", [
    row("a@x.com", "accountant", tenant_id=OTHER_TENANT),        # another company's user
    row("a@x.com", "super_admin", tenant_id=None),               # super admin asked to be a company user
])
def test_plan_never_moves_an_account_between_companies(existing):
    with pytest.raises(tu.TestUsersError) as error:
        tu.plan_changes({"a@x.com": existing}, TENANT, tu.parse_test_emails("a@x.com:local_admin"))
    assert "a***@x***.com" in str(error.value) and "a@x.com" not in str(error.value)


def test_super_admin_requested_for_a_company_user_is_refused():
    wanted = tu.parse_test_emails("boss@x.com:local_admin,a@x.com:super_admin")
    with pytest.raises(tu.TestUsersError):
        tu.plan_changes({"a@x.com": row("a@x.com", "local_admin")}, TENANT, wanted)


# --- password and company lookup -------------------------------------------------------------------

def test_password_from_environment_must_be_strong():
    assert tu.read_password({"UAT_TEST_PASSWORD": "Str0ng!pass"}) == "Str0ng!pass"
    with pytest.raises(tu.TestUsersError) as error:
        tu.read_password({"UAT_TEST_PASSWORD": "weak"})
    assert "weak" not in str(error.value)


def test_password_prompt_must_match():
    answers = iter(["Str0ng!pass", "Different1!"])
    with pytest.raises(tu.TestUsersError):
        tu.read_password({}, prompt=lambda _: next(answers))


def test_pack_company_reads_name_and_ssm(tmp_path):
    (tmp_path / "trading").mkdir()
    (tmp_path / "trading" / "pack.json").write_text(json.dumps({"company": {"name": "Demo Sdn Bhd", "ssm": "2026", "tin": "x"}}))
    assert tu.pack_company("trading", tmp_path) == {"name": "Demo Sdn Bhd", "ssm": "2026"}
    with pytest.raises(tu.TestUsersError) as error:
        tu.pack_company("fnb", tmp_path)
    assert "seed-demo-company --pack fnb" in str(error.value)


def test_display_name_from_address():
    assert tu.display_name("first.last+uat@x.com") == "First Last"
    assert tu.display_name("___@x.com") == "Test User"


# --- output ----------------------------------------------------------------------------------------

def test_report_masks_every_email():
    wanted = tu.parse_test_emails("first.person@example.com:local_admin,second@example.com")
    changes = tu.plan_changes({}, TENANT, wanted)
    text = "\n".join(tu.report_lines({"name": "Demo Sdn Bhd"}, changes, 12))
    assert "example.com" not in text
    assert "first.person" not in text and "second@" not in text
    assert "f***@e***.com" in text and "raised to 12" in text and "2 test users ready" in text
