"""Small S3-compatible storage adapter for Render's separate services."""
from __future__ import annotations

import os
import importlib.util
import re
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def enabled() -> bool:
    # A configured bucket alone must not make local uploads fail when the
    # optional S3 client is not installed. In that case callers use the same
    # local storage path used by the worker and document preview routes.
    return bool(os.getenv("S3_BUCKET")) and importlib.util.find_spec("boto3") is not None


def local_storage_root() -> Path:
    """The one local document root for upload, worker and preview; relative paths anchor at the backend."""
    root = Path(os.getenv("DOCUMENT_STORAGE_PATH", "storage")).expanduser()
    return (root if root.is_absolute() else BACKEND_ROOT / root).resolve()


def _client():
    import boto3
    from botocore.config import Config
    try:
        import certifi
        ca_bundle = os.getenv("AWS_CA_BUNDLE") or certifi.where()
    except ImportError:
        ca_bundle = os.getenv("AWS_CA_BUNDLE")
    verify_setting = os.getenv("S3_TLS_VERIFY", "true").strip().lower()
    verify = False if verify_setting in {"0", "false", "no"} else (ca_bundle or True)
    return boto3.client(
        "s3",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        endpoint_url=(os.getenv("S3_ENDPOINT_URL") or os.getenv("AWS_ENDPOINT_URL")
                      or os.getenv("AWS_ENDPOINT_URL_S3") or None),
        verify=verify,
        config=Config(
            connect_timeout=int(os.getenv("S3_CONNECT_TIMEOUT_SECONDS", "15")),
            read_timeout=int(os.getenv("S3_READ_TIMEOUT_SECONDS", "60")),
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def put_bytes(key: str, data: bytes, content_type: str) -> str:
    bucket = os.environ["S3_BUCKET"]
    _client().put_object(
        Bucket=bucket, Key=key, Body=data, ContentType=content_type
    )
    return f"s3://{bucket}/{key}"


def safe_component(value: str | None, fallback: str = "unknown") -> str:
    """Return a readable, path-safe component without trusting user input."""
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-_")
    return value[:80] or fallback


def document_key(tenant_id, accountant_id, accountant_name, document_id,
                 filename: str) -> str:
    """Build a stable, tenant-isolated key with a readable accountant segment."""
    return (
        f"tenants/{safe_component(str(tenant_id))}/"
        f"accountants/{safe_component(accountant_name, 'accountant')}-"
        f"{safe_component(str(accountant_id))}/documents/"
        f"{safe_component(str(document_id))}/{safe_component(filename, 'document')}"
    )


def relocate(uri: str, new_key: str, content_type: str | None = None, *, delete_original: bool = True) -> str:
    """Move an object, preserving the old object if the destination fails."""
    if uri.startswith("s3://"):
        bucket, old_key = uri[5:].split("/", 1)
        if old_key == new_key:
            return uri
        client = _client()
        client.copy_object(
            Bucket=bucket, Key=new_key,
            CopySource={"Bucket": bucket, "Key": old_key},
            **({"ContentType": content_type, "MetadataDirective": "REPLACE"}
               if content_type else {}),
        )
        if delete_original:
            client.delete_object(Bucket=bucket, Key=old_key)
        return f"s3://{bucket}/{new_key}"
    old_path = Path(uri).resolve()
    root = local_storage_root()
    if not old_path.is_relative_to(root) or not old_path.is_file():
        raise FileNotFoundError("Stored document is unavailable")
    destination = (root / Path(new_key)).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("Invalid storage destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if old_path == destination:
        return str(old_path)
    if delete_original:
        old_path.replace(destination)
    else:
        import shutil
        shutil.copy2(old_path, destination)
    return str(destination)


def download(uri: str) -> Path:
    from tempfile import NamedTemporaryFile
    bucket, key = uri[5:].split("/", 1)
    with NamedTemporaryFile(prefix="accounting-document-", suffix=Path(key).suffix, delete=False) as target:
        _client().download_fileobj(bucket, key, target)
        return Path(target.name)

def delete(uri: str) -> None:
    bucket, key = uri[5:].split('/', 1)
    _client().delete_object(Bucket=bucket, Key=key)
