"""Session-wide safety: automated tests may only use a disposable loopback PostgreSQL.

database.connection loads .env at import time, so a shared database URL there would
otherwise reach integration tests that create schemas. Stop before any test connects.
"""
from pathlib import Path
from urllib.parse import urlsplit

import pytest

LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def unsafe_test_database(test_url, shared_url=None):
    """Return a reason the URL is unsafe for automated tests, or None when it is safe or unset."""
    if not test_url:
        return None
    try:
        target = urlsplit(test_url)
        host = (target.hostname or '').lower()
    except ValueError:
        return 'TEST_DATABASE_URL is not a valid URL'
    if host not in LOOPBACK_HOSTS:
        return f'TEST_DATABASE_URL host {host or "<none>"} is not a local disposable database'
    if shared_url:
        try:
            shared = urlsplit(shared_url)
            if (shared.hostname or '').lower() == host and shared.port == target.port and shared.path == target.path:
                return 'TEST_DATABASE_URL is the same database as DATABASE_URL'
        except ValueError:
            pass
    return None


def pytest_configure(config):
    import os
    from dotenv import dotenv_values
    env_file = dotenv_values(Path(__file__).resolve().parents[1] / '.env')
    test_url = os.environ.get('TEST_DATABASE_URL', env_file.get('TEST_DATABASE_URL'))
    shared_url = os.environ.get('DATABASE_URL', env_file.get('DATABASE_URL'))
    reason = unsafe_test_database(test_url, shared_url)
    if reason:
        pytest.exit(f'Refusing to run tests: {reason}. Point it at a disposable local database '
                    'or set TEST_DATABASE_URL= (empty) to skip PostgreSQL tests.', returncode=4)
