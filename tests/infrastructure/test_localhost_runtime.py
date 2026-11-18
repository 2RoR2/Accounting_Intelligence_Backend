"""Safe-configuration rules of scripts/localhost_runtime.py. No database, Redis or env file is touched."""
import importlib.util
from pathlib import Path
import re

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'localhost_runtime.py'
_spec = importlib.util.spec_from_file_location('localhost_runtime_under_test', SCRIPT)
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)

LOCAL = {
    'DATABASE_URL': 'postgresql://user:pw@db.example.test/accounting',
    'REDIS_URL': 'redis://127.0.0.1:6379/11',
    'QUEUE_TENANT_ID': '00000000-0000-4000-8000-0000000000aa',
    'QUEUE_OWNER_ID': '00000000-0000-4000-8000-0000000000bb',
    'QUEUE_MIN_UPLOADED_AT': '2026-11-10T00:00:00+08:00',
}


def problems(local=None, **overrides):
    local = dict(LOCAL if local is None else local)
    environ = {**local, **overrides}
    return runtime.check_local_configuration(local, environ)


def test_complete_local_configuration_is_accepted():
    assert problems() == []


@pytest.mark.parametrize('key', runtime.REQUIRED_LOCAL_KEYS)
def test_scope_values_inherited_from_shared_env_are_refused(key):
    local = {k: v for k, v in LOCAL.items() if k != key}
    result = runtime.check_local_configuration(local, dict(LOCAL))
    assert result == ['.env.localhost must define ' + key]


def test_remote_or_wrong_redis_database_is_refused():
    assert problems(REDIS_URL='redis://cache.example.test:6379/11')
    assert problems(REDIS_URL='redis://127.0.0.1:6379/0')
    assert problems(REDIS_URL='redis://127.0.0.1:6379/5', LOCALHOST_REDIS_DB='5') == []


def test_optional_database_host_pin():
    assert problems(LOCALHOST_DATABASE_HOST='db.example.test') == []
    assert problems(LOCALHOST_DATABASE_HOST='other.example.test') == ['DATABASE_URL host differs from LOCALHOST_DATABASE_HOST']
    assert problems(DATABASE_URL='mysql://db.example.test/x') == ['DATABASE_URL must be a full PostgreSQL URL']


def test_queue_scope_must_be_well_formed():
    assert problems(QUEUE_TENANT_ID='tenant-1') == ['QUEUE_TENANT_ID must be a UUID']
    assert problems(QUEUE_OWNER_ID='') == ['QUEUE_OWNER_ID must be a UUID']
    assert problems(QUEUE_MIN_UPLOADED_AT='2026-11-10T00:00:00') == ['QUEUE_MIN_UPLOADED_AT must include a timezone']
    assert problems(QUEUE_MIN_UPLOADED_AT='yesterday') == ['QUEUE_MIN_UPLOADED_AT must be an ISO-8601 timestamp']


def test_messages_never_echo_secret_values():
    secret = 'postgresql://user:s3cret-value@db.example.test/accounting'
    for message in problems(DATABASE_URL=secret, LOCALHOST_DATABASE_HOST='elsewhere.test'):
        assert 's3cret' not in message


def test_missing_local_file_stops_before_loading_anything(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        runtime.load_local_environment(tmp_path)
    assert '.env.localhost' in str(stopped.value) and '.md' not in str(stopped.value)


def test_runtime_forces_model_inference_off_and_keeps_commands():
    assert runtime.RUNTIME_OVERRIDES == {'ENABLE_QWEN_RECOVERY': 'false', 'QWEN_RECOVERY_MODE': 'off', 'ALLOW_VLM_INFERENCE': 'false'}
    assert "choices=['api', 'worker', 'supervisor', 'check']" in SCRIPT.read_text(encoding='utf-8')


def test_script_holds_no_deployment_identifiers():
    source = SCRIPT.read_text(encoding='utf-8')
    assert 'neon.tech' not in source
    assert not re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', source)
    assert '.md' not in source
