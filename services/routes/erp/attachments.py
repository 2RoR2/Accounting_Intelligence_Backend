"""Proof attached to typed records: a receipt photo, bank slip, PDF or an https link.

Targets are typed ERP documents: payments received/made, refunds, money in, money out, bank transfers and manual
journals, plus typed supplier documents (bills, supplier credit and debit notes), which carry the supplier's copy.
Files use the same private document storage as uploads (S3 in production, a local folder in development) and the same
hardened rules: at most 10 MB, at most limit+1 bytes read, PDF/PNG/JPEG/WebP only, decided from the
file's own bytes (never the name or the client's content type). Files are served with the safe preview headers of
the document viewer. Every read is tenant-checked (another company's id answers 404). Removing is a soft delete
that keeps the stored object and the row; uploads and removals write audit rows.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from main import HTTPException, PRODUCTION_STORAGE_ERROR, Request, connect, is_production

from .common import audit, erp_user, parse_uuid

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_RECORD = 20
MAX_DECODE_PIXELS = 40_000_000  # about 160 MB decoded as RGBA: the most one upload check may use
MAX_LINK_LENGTH = 2000
MAX_NAME_LENGTH = 200
TARGET_TYPE = "erp_document"
# Typed records that can carry proof. A typed bill or supplier note is keyed in from the supplier's paper, so it carries
# that paper here (the form shows it where sales documents show their printed preview). Documents the company prints
# itself (invoices, quotations ...) keep their own stored PDF instead.
ATTACHABLE_TYPES = frozenset({
    "payment", "purchase-payment", "refund", "purchase-refund",
    "money-in", "money-out", "bank-transfer", "journal-entry",
    "bill", "purchase-credit-note", "purchase-debit-note",
})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class LinkInput(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_LINK_LENGTH + 100)
    name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH + 100)


def sniff_media_type(head: bytes) -> str | None:
    """The media type shown by a file's leading bytes, or None when it is not an allowed type."""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP" and head[12:16] in (b"VP8 ", b"VP8L", b"VP8X"):
        return "image/webp"
    return None


def is_complete(data: bytes, media_type: str) -> bool:
    """Whether the whole file arrived: a PDF ends with its %%EOF marker (in the last 1 KB, where readers look for it)
    and an image decodes to its last pixel. A recognised header alone (a cut-off upload) is not enough.

    A JPEG is decoded at a reduced scale (the whole stream is still read); any other image larger than
    MAX_DECODE_PIXELS is refused rather than decoded, so a small file cannot claim a huge canvas and exhaust memory."""
    if media_type == "application/pdf":
        return b"%%EOF" in data[-1024:]
    from io import BytesIO

    from PIL import Image, ImageFile
    if ImageFile.LOAD_TRUNCATED_IMAGES:  # a process-wide switch that would make a cut-off image look whole
        return False
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format == "JPEG":
                image.draft("RGB", (1024, 1024))
            if image.width * image.height > MAX_DECODE_PIXELS:
                return False
            image.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return False
    return True


def clean_name(value: str | None, fallback: str) -> str:
    name = Path(_CONTROL.sub("", str(value or "")).replace("\\", "/")).name.strip()
    return name[:MAX_NAME_LENGTH] or fallback


def validate_link(raw: str) -> str:
    """Only plain https:// links: no other scheme, no credentials in the address, no spaces or control characters."""
    url = (raw or "").strip()
    if not url or len(url) > MAX_LINK_LENGTH:
        raise HTTPException(422, f"Enter a link of up to {MAX_LINK_LENGTH} characters.")
    if _CONTROL.search(url) or re.search(r"\s", url) or "\\" in url:
        raise HTTPException(422, "The link must not contain spaces or control characters.")
    if url[:8].lower() != "https://":
        raise HTTPException(422, "Only https:// links can be attached.")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018  (raises ValueError for an invalid port)
    except ValueError:
        raise HTTPException(422, "The link is not a valid web address.") from None
    if not host or parts.username is not None or parts.password is not None:
        raise HTTPException(422, "The link is not a valid web address.")
    # The scheme is case-insensitive; store it lowercase because the table requires a literal https://.
    return "https://" + url[8:]


def _document(db, user, document_type: str, document_id):
    """The attachable typed record of this company; any other company's id (or type) is simply not found."""
    if document_type not in ATTACHABLE_TYPES:
        raise HTTPException(404, "Record not found.")
    doc = db.execute(
        """SELECT id, document_number FROM erp_document_headers
           WHERE id=%s AND tenant_id=%s AND document_type=%s""",
        (parse_uuid(document_id, "record id"), user["tenant_id"], document_type)).fetchone()
    if not doc:
        raise HTTPException(404, "Record not found.")
    return doc


def _dto(row) -> dict:
    is_file = row["kind"] == "file"
    return {
        "id": str(row["id"]), "kind": row["kind"], "name": row["original_name"],
        "mediaType": row["media_type"], "sizeBytes": row["size_bytes"],
        "url": row["url"], "fileUrl": f"/api/erp/attachments/{row['id']}/file" if is_file else None,
        "uploadedBy": row.get("uploaded_by_name"), "createdAt": row["created_at"].isoformat(),
    }


def _listing(db, tenant_id, document_id) -> list[dict]:
    rows = db.execute(
        """SELECT a.id, a.kind, a.original_name, a.media_type, a.size_bytes, a.url, a.created_at,
                  u.name AS uploaded_by_name
           FROM record_attachments a LEFT JOIN users u ON u.id=a.uploaded_by
           WHERE a.tenant_id=%s AND a.target_type=%s AND a.target_id=%s AND a.deleted_at IS NULL
           ORDER BY a.created_at, a.id""", (tenant_id, TARGET_TYPE, document_id)).fetchall()
    return [_dto(row) for row in rows]


def _check_capacity(db, tenant_id, document_id):
    # The record row lock serialises two uploads to the same record so the cap cannot be passed by a race.
    db.execute("SELECT 1 FROM erp_document_headers WHERE id=%s AND tenant_id=%s FOR UPDATE", (document_id, tenant_id))
    count = db.execute(
        "SELECT count(*) AS n FROM record_attachments WHERE tenant_id=%s AND target_type=%s AND target_id=%s AND deleted_at IS NULL",
        (tenant_id, TARGET_TYPE, document_id)).fetchone()["n"]
    if count >= MAX_ATTACHMENTS_PER_RECORD:
        raise HTTPException(422, f"A record can hold up to {MAX_ATTACHMENTS_PER_RECORD} attachments. Remove one first.")


def list_attachments(document_type: str, document_id: str, request: Request):
    user = erp_user(request)
    with connect() as db:
        doc = _document(db, user, document_type, document_id)
        return {"items": _listing(db, user["tenant_id"], doc["id"]),
                "maxBytes": MAX_ATTACHMENT_BYTES, "maxItems": MAX_ATTACHMENTS_PER_RECORD}


def _store(user, document_id, attachment_id, name: str, data: bytes, media_type: str):
    """Write the bytes to private storage; return (stored path or s3:// uri, s3 uri to clean up, local file to clean up)."""
    from database.object_storage.object_store import enabled, local_storage_root, put_bytes, safe_component
    use_object_storage = enabled()
    if is_production() and not use_object_storage:
        raise HTTPException(503, PRODUCTION_STORAGE_ERROR)
    key = (f"tenants/{safe_component(str(user['tenant_id']))}/attachments/{safe_component(str(document_id))}/"
           f"{safe_component(str(attachment_id))}/{safe_component(name, 'attachment')}")
    if use_object_storage:
        uri = put_bytes(key, data, media_type)
        return uri, uri, None
    root = local_storage_root()
    path = (root / Path(key)).resolve()
    if not path.is_relative_to(root):
        raise HTTPException(422, "Invalid storage destination.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path), None, path


def _discard(uri: str | None, local: Path | None):
    if local is not None:
        local.unlink(missing_ok=True)
    if uri:
        try:
            from database.object_storage.object_store import delete
            delete(uri)
        except Exception:  # best effort: the row was never created, so nothing refers to the object
            pass


async def add_file(document_type: str, document_id: str, request: Request, file: UploadFile = File(...)):
    user = erp_user(request)
    with connect() as db:  # check access before reading the upload
        doc = _document(db, user, document_type, document_id)
    data = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, "This file exceeds the 10 MB limit. Choose a smaller file.")
    if not data:
        raise HTTPException(422, "The uploaded file is empty.")
    media_type = sniff_media_type(data[:16])
    if media_type is None:
        raise HTTPException(422, "Unsupported file type. Attach a PDF, PNG, JPEG or WebP file, or paste a link.")
    if not is_complete(data, media_type):
        raise HTTPException(422, "This file is incomplete or damaged and cannot be opened. Attach the full file again.")
    # Attachments are another document ingress path. Reuse the optional
    # ClamAV boundary so malware scanning cannot be bypassed by uploading to a
    # typed ERP record instead of the main Inbox.
    try:
        from security.upload_guard import UploadSecurityError, scan_for_malware
        scan_for_malware(data)
    except UploadSecurityError as exc:
        message = "The attached file failed the malware scan." if exc.code == "UPLOAD_MALWARE_DETECTED" else "The attachment could not be security-scanned."
        raise HTTPException(422, message) from exc
    name = clean_name(file.filename, "attachment")
    attachment_id = uuid.uuid4()
    stored, s3_uri, local_file = _store(user, doc["id"], attachment_id, name, data, media_type)
    try:
        with connect() as db:
            _document(db, user, document_type, document_id)
            _check_capacity(db, user["tenant_id"], doc["id"])
            db.execute(
                """INSERT INTO record_attachments(id,tenant_id,target_type,target_id,kind,storage_path,original_name,
                       media_type,size_bytes,uploaded_by)
                   VALUES (%s,%s,%s,%s,'file',%s,%s,%s,%s,%s)""",
                (attachment_id, user["tenant_id"], TARGET_TYPE, doc["id"], stored, name, media_type, len(data), user["id"]))
            audit(db, user, "erp.attachment_added", str(doc["id"]),
                  {"attachmentId": str(attachment_id), "kind": "file", "name": name, "mediaType": media_type,
                   "sizeBytes": len(data), "type": document_type, "number": doc["document_number"]})
            return {"items": _listing(db, user["tenant_id"], doc["id"]), "id": str(attachment_id)}
    except Exception:
        _discard(s3_uri, local_file)
        raise


def add_link(document_type: str, document_id: str, data: LinkInput, request: Request):
    user = erp_user(request)
    url = validate_link(data.url)
    name = clean_name(data.name, "") or (urlsplit(url).hostname or "link")
    attachment_id = uuid.uuid4()
    with connect() as db:
        doc = _document(db, user, document_type, document_id)
        _check_capacity(db, user["tenant_id"], doc["id"])
        db.execute(
            """INSERT INTO record_attachments(id,tenant_id,target_type,target_id,kind,url,original_name,uploaded_by)
               VALUES (%s,%s,%s,%s,'link',%s,%s,%s)""",
            (attachment_id, user["tenant_id"], TARGET_TYPE, doc["id"], url, name, user["id"]))
        audit(db, user, "erp.attachment_added", str(doc["id"]),
              {"attachmentId": str(attachment_id), "kind": "link", "name": name, "url": url,
               "type": document_type, "number": doc["document_number"]})
        return {"items": _listing(db, user["tenant_id"], doc["id"]), "id": str(attachment_id)}


def _owned(db, user, attachment_id, *, lock: bool = False):
    row = db.execute(
        f"""SELECT a.*, h.document_number, h.document_type FROM record_attachments a
            JOIN erp_document_headers h ON h.id=a.target_id AND h.tenant_id=a.tenant_id
            WHERE a.id=%s AND a.tenant_id=%s AND a.target_type=%s AND a.deleted_at IS NULL{' FOR UPDATE OF a' if lock else ''}""",
        (parse_uuid(attachment_id, "attachment id"), user["tenant_id"], TARGET_TYPE)).fetchone()
    if not row:
        raise HTTPException(404, "Attachment not found.")
    return row


def download_attachment(attachment_id: str, request: Request):
    user = erp_user(request)
    with connect() as db:
        row = _owned(db, user, attachment_id)
    if row["kind"] != "file":
        raise HTTPException(404, "Attachment not found.")
    temporary = None
    if str(row["storage_path"]).startswith("s3://"):
        from database.object_storage.object_store import download
        temporary = download(str(row["storage_path"]))
        path = temporary
    else:
        from database.object_storage.object_store import local_storage_root
        path = Path(row["storage_path"]).resolve()
        if local_storage_root() not in path.parents:
            raise HTTPException(404, "The attachment file is unavailable.")
    if not path.is_file():
        if temporary:
            temporary.unlink(missing_ok=True)
        raise HTTPException(404, "The attachment file is unavailable.")
    with path.open("rb") as stored_file:
        media_type = sniff_media_type(stored_file.read(16)) or "application/octet-stream"
    headers = {"X-Content-Type-Options": "nosniff"}
    if media_type != "application/pdf":
        # Browsers' built-in PDF viewers refuse sandboxed documents; PDFs run no page script on this origin.
        headers["Content-Security-Policy"] = "sandbox"
    return FileResponse(
        path, media_type=media_type, filename=row["original_name"],
        content_disposition_type="attachment" if media_type == "application/octet-stream" else "inline",
        headers=headers, background=BackgroundTask(temporary.unlink, missing_ok=True) if temporary else None)


def remove_attachment(attachment_id: str, request: Request):
    """Soft delete: the row and the stored object stay for audit; the attachment leaves every list."""
    user = erp_user(request)
    with connect() as db:
        row = _owned(db, user, attachment_id, lock=True)
        db.execute("UPDATE record_attachments SET deleted_at=now(), deleted_by=%s WHERE id=%s AND tenant_id=%s",
                   (user["id"], row["id"], user["tenant_id"]))
        audit(db, user, "erp.attachment_removed", str(row["target_id"]),
              {"attachmentId": str(row["id"]), "kind": row["kind"], "name": row["original_name"],
               "type": row["document_type"], "number": row["document_number"]})
        return {"items": _listing(db, user["tenant_id"], row["target_id"]), "id": str(row["id"])}


def register(router):
    base = "/api/erp/documents/{document_type}/{document_id}/attachments"
    router.add_api_route(base, list_attachments, methods=["GET"], tags=["ERP Attachments"])
    router.add_api_route(base, add_file, methods=["POST"], status_code=201, tags=["ERP Attachments"])
    router.add_api_route(base + "/link", add_link, methods=["POST"], status_code=201, tags=["ERP Attachments"])
    router.add_api_route("/api/erp/attachments/{attachment_id}/file", download_attachment, methods=["GET"], tags=["ERP Attachments"])
    router.add_api_route("/api/erp/attachments/{attachment_id}", remove_attachment, methods=["DELETE"], tags=["ERP Attachments"])
