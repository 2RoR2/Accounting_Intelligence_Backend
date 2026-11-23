import asyncio
from io import BytesIO
from runpy import run_path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.testclient import TestClient
import pytest

import main as api
from database.object_storage import object_store
from services import routes as route_registry
from services.routes.admin import routes as admin
from services.routes.bank import routes as bank
from services.routes.documents import routes as documents


USER = {"id": "user", "tenant_id": "tenant", "role": "local_admin"}


@pytest.mark.parametrize(
    ("environment", "explicit_origin", "origin", "allowed"),
    [
        ("production", "https://frontend.example", "http://192.168.1.10:3000", False),
        ("production", "https://frontend.example", "https://frontend.example", True),
        ("production", None, "https://legacy.example", False),
        ("development", None, "http://192.168.1.10:3000", True),
    ],
)
def test_cors_origins(monkeypatch, environment, explicit_origin, origin, allowed):
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://legacy.example")
    if explicit_origin:
        monkeypatch.setenv("FRONTEND_ORIGINS", explicit_origin)
    else:
        monkeypatch.delenv("FRONTEND_ORIGINS", raising=False)
    monkeypatch.setattr(route_registry, "router", APIRouter())
    app = run_path(api.__file__)["app"]

    response = TestClient(app).options("/api/health", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
    })

    assert response.headers.get("access-control-allow-origin") == (origin if allowed else None)


def test_production_startup_requires_object_storage(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("QUEUE_BACKEND", "rq")
    monkeypatch.setattr(api, "check_schema_compatibility", lambda: None)

    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        with TestClient(api.app):
            pass


def test_production_upload_cannot_fall_back_to_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DOCUMENT_STORAGE_PATH", str(tmp_path))
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setattr(documents, "require_workspace_user", lambda *_: USER)
    database = MagicMock()
    monkeypatch.setattr(documents, "connect", database)
    upload = UploadFile(BytesIO(b"%PDF-1.7\n"), filename="scan.pdf")

    with pytest.raises(HTTPException, match="S3_BUCKET") as error:
        asyncio.run(documents.upload_document(None, upload))

    assert error.value.status_code == 503
    assert not list(tmp_path.iterdir())
    database.assert_not_called()


def test_super_admin_tenant_list_types_null_scope(monkeypatch):
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr(admin, "connect", lambda: db)
    db.__enter__.return_value = db
    monkeypatch.setattr(admin, "require_workspace_user", lambda *_: {"role": "super_admin"})

    assert admin.list_tenants(None) == []

    query, params = db.execute.call_args.args
    assert "%s::uuid IS NULL" in query
    assert params == (None, None)


@pytest.mark.parametrize(("content", "mime", "disposition", "remote"), [
    (b"%PDF-1.7\n", "application/pdf", "inline", False),
    (b"\x89PNG\r\n\x1a\n", "image/png", "inline", False),
    (b"\xff\xd8\xff", "image/jpeg", "inline", False),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp", "inline", True),
    (b"<html>fixture</html>", "application/octet-stream", "attachment", False),
])
def test_document_preview_uses_file_signature(monkeypatch, tmp_path, content, mime, disposition, remote):
    path = tmp_path / "upload.pdf"
    path.write_bytes(content)
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value.fetchone.return_value = {
        "storage_path": "s3://test-bucket/upload.pdf" if remote else str(path),
        "original_filename": path.name, "mime_type": "image/png",
    }
    if remote:
        monkeypatch.setattr(object_store, "download", lambda _: path)
    monkeypatch.setenv("DOCUMENT_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(documents, "require_workspace_user", lambda *_: USER)
    monkeypatch.setattr(documents, "connect", lambda: db)

    response = TestClient(api.app).get("/api/documents/doc/file")

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == mime
    assert response.headers["content-disposition"].startswith(f'{disposition};')
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers.get("content-security-policy") == (None if mime == "application/pdf" else "sandbox")
    assert path.exists() == (not remote)


def test_admin_password_reset_revokes_target_sessions(monkeypatch):
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value.fetchone.return_value = {"id": "target"}
    monkeypatch.setattr(admin, "require_workspace_user", lambda *_: USER)
    monkeypatch.setattr(admin, "connect", lambda: db)
    monkeypatch.setattr(admin, "api_user", lambda row: row)
    monkeypatch.setattr(admin, "hash_password", lambda _: "new-hash")

    admin.update_user_credentials("target", api.AdminCredentialsInput(name="Accountant", password="Example123!"), None)

    db.execute.assert_any_call("DELETE FROM web_sessions WHERE user_id=%s", ("target",))


def test_document_upload_bounds_file_read(monkeypatch):
    monkeypatch.setattr(documents, "require_workspace_user", lambda *_: USER)
    limit = 20 * 1024 * 1024
    upload = SimpleNamespace(read=AsyncMock(return_value=b"x" * (limit + 1)))

    with pytest.raises(HTTPException) as error:
        asyncio.run(documents.upload_document(None, upload))

    assert error.value.status_code == 413
    upload.read.assert_awaited_once_with(limit + 1)


def test_bank_statement_upload_bounds_file_read(monkeypatch):
    monkeypatch.setattr(bank, "require_workspace_user", lambda *_: USER)
    limit = bank.MAX_STATEMENT_BYTES
    upload = SimpleNamespace(filename="bank.csv", read=AsyncMock(return_value=b"x" * (limit + 1)))
    request = SimpleNamespace(headers={"content-type": "multipart/form-data"},
                              form=AsyncMock(return_value={"file": upload}))

    with pytest.raises(HTTPException) as error:
        asyncio.run(bank.create_bank_import(request))

    assert error.value.status_code == 413
    upload.read.assert_awaited_once_with(limit + 1)
