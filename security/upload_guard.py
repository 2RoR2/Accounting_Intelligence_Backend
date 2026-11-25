"""Pre-processing checks for uploaded accounting files."""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path


class UploadSecurityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


MAX_ZIP_ENTRIES = 2_000
MAX_ZIP_UNCOMPRESSED = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_DIMENSION = 20_000


def _zip_safety(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise UploadSecurityError("UPLOAD_ARCHIVE_TOO_MANY_ENTRIES")
            total = 0
            for entry in entries:
                name = entry.filename.replace("\\", "/")
                if name.startswith("/") or "../" in name or ":" in name.split("/")[0]:
                    raise UploadSecurityError("UPLOAD_ARCHIVE_PATH_INVALID")
                total += max(0, entry.file_size)
                if total > MAX_ZIP_UNCOMPRESSED:
                    raise UploadSecurityError("UPLOAD_ARCHIVE_EXPANDS_TOO_LARGE")
    except zipfile.BadZipFile as exc:
        raise UploadSecurityError("UPLOAD_ARCHIVE_INVALID") from exc


def _image_safety(data: bytes) -> None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.width > MAX_IMAGE_DIMENSION or image.height > MAX_IMAGE_DIMENSION:
                raise UploadSecurityError("UPLOAD_IMAGE_DIMENSION_LIMIT")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise UploadSecurityError("UPLOAD_IMAGE_PIXEL_LIMIT")
            # Verify headers without decoding the full image into memory.
            image.verify()
    except UploadSecurityError:
        raise
    except Exception as exc:
        raise UploadSecurityError("UPLOAD_IMAGE_INVALID") from exc


def scan_for_malware(data: bytes) -> str:
    """Optional ClamAV integration; production can fail closed with `required`."""
    mode = os.getenv("UPLOAD_MALWARE_SCAN", "off").strip().lower() or "off"
    if mode not in {"off", "clamd", "required"}:
        raise UploadSecurityError("UPLOAD_MALWARE_SCAN_MODE_INVALID")
    if mode == "off":
        return "disabled"
    try:
        import clamd  # optional dependency, enabled only when configured
        client = clamd.ClamdUnixSocket(os.getenv("CLAMAV_SOCKET", "/var/run/clamav/clamd.ctl"))
        result = client.instream(io.BytesIO(data))
        status = (result.get("stream") or ("", ""))[0]
        if status == "FOUND":
            raise UploadSecurityError("UPLOAD_MALWARE_DETECTED")
        if status != "OK":
            raise UploadSecurityError("UPLOAD_MALWARE_SCAN_FAILED")
        return "clamd"
    except UploadSecurityError:
        raise
    except Exception as exc:
        if mode == "required":
            raise UploadSecurityError("UPLOAD_MALWARE_SCANNER_UNAVAILABLE") from exc
        return "unavailable_optional"


def validate_upload_payload(filename: str, data: bytes, content_type: str | None = None) -> dict[str, str]:
    suffix = Path(filename or "").suffix.lower()
    signatures = {
        ".pdf": data.startswith(b"%PDF-"),
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": data.startswith(b"\xff\xd8\xff"),
        ".jpeg": data.startswith(b"\xff\xd8\xff"),
        ".tif": data.startswith((b"II*\x00", b"MM\x00*")),
        ".tiff": data.startswith((b"II*\x00", b"MM\x00*")),
        ".xlsx": data.startswith(b"PK\x03\x04"),
        ".csv": True,
    }
    if suffix not in signatures:
        raise UploadSecurityError("UPLOAD_FILE_TYPE_UNSUPPORTED")
    if not data or not signatures[suffix]:
        raise UploadSecurityError("UPLOAD_FILE_SIGNATURE_INVALID")
    if suffix == ".xlsx":
        _zip_safety(data)
    elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        _image_safety(data)
    scanner = scan_for_malware(data)
    return {"scanner": scanner, "suffix": suffix, "content_type": content_type or "application/octet-stream"}
