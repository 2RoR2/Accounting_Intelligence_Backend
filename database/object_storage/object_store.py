"""Small S3-compatible storage adapter for Render's separate services."""
from __future__ import annotations

import os
from pathlib import Path


def enabled() -> bool:
    return bool(os.getenv("S3_BUCKET"))


def _client():
    import boto3
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
    )


def put_bytes(key: str, data: bytes, content_type: str) -> str:
    bucket = os.environ["S3_BUCKET"]
    _client().put_object(
        Bucket=bucket, Key=key, Body=data, ContentType=content_type
    )
    return f"s3://{bucket}/{key}"


def download(uri: str) -> Path:
    from tempfile import NamedTemporaryFile
    bucket, key = uri[5:].split("/", 1)
    with NamedTemporaryFile(prefix="accounting-document-", suffix=Path(key).suffix, delete=False) as target:
        _client().download_fileobj(bucket, key, target)
        return Path(target.name)

def delete(uri: str) -> None:
    bucket, key = uri[5:].split('/', 1)
    _client().delete_object(Bucket=bucket, Key=key)
