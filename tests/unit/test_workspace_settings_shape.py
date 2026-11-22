"""UAT A2: the settings GET returns the same field names the PUT accepts, so a saved value shows after reload."""
from contextlib import contextmanager
from unittest.mock import patch

import main  # noqa: F401  (the route modules import from main)
from main import SettingsInput
from services.routes.workspace import routes as workspace

STORED = {"timezone": "Asia/Kuala_Lumpur", "notifications": "enabled", "session_timeout_minutes": 120,
          "default_document_view": "exceptions", "email_assignment_notifications": False,
          "processing_status_notifications": True, "upload_max_size_mb": 10, "allow_camera_uploads": True}


class _Db:
    def __init__(self, row):
        self.row = row

    def execute(self, *_args):
        return self

    def fetchone(self):
        return self.row


def _get(row):
    @contextmanager
    def connect():
        yield _Db(row)
    user = {"role": "local_admin", "tenant_id": "t1", "id": "u1", "name": "Admin"}
    with patch.multiple(workspace, connect=connect, require_workspace_user=lambda *_a, **_k: user):
        return workspace.get_workspace_settings(None)


def test_saved_settings_come_back_with_the_names_the_form_sends():
    result = _get(STORED)
    assert set(result) == set(SettingsInput.model_fields)
    assert result["defaultDocumentView"] == "exceptions" and result["sessionTimeoutMinutes"] == 120
    SettingsInput(**result)  # what the form reads back can be saved again unchanged


def test_defaults_use_the_same_names():
    assert set(_get(None)) == set(SettingsInput.model_fields)
