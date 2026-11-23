"""Plan P1: the pure rules for proof attached to typed records (file sniffing, links, names)."""
import pytest
import main  # noqa: F401  (the route modules import from main, so load it first)
from fastapi import HTTPException

from services.routes.erp.attachments import clean_name, sniff_media_type, validate_link

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12
PDF = b"%PDF-1.7\n" + b"\x00" * 8
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 4


@pytest.mark.parametrize("head,expected", [
    (PDF, "application/pdf"), (PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"),
    (b"RIFF\x24\x00\x00\x00WAVEfmt ", None),             # RIFF but not WebP
    (b"GIF89a" + b"\x00" * 10, None), (b"<html><script>", None), (b"MZ\x90\x00", None),
    (b"II*\x00" + b"\x00" * 12, None),                   # TIFF is accepted for scanned documents, not as proof
    (b"", None),
])
def test_media_type_comes_from_the_bytes(head, expected):
    assert sniff_media_type(head) == expected


@pytest.mark.parametrize("url", [
    "https://bank.example.com/slip/123", "HTTPS://Bank.Example.com/a?b=c#d", "https://example.com:8443/x",
    "https://[2001:db8::1]/receipt",
])
def test_https_links_are_accepted(url):
    assert validate_link(url) == url


@pytest.mark.parametrize("url", [
    "http://example.com/a", "javascript:alert(1)", "data:text/html,<b>x</b>", "ftp://example.com/a", "//example.com/a",
    "example.com/a", "https://", "https:///path", "https://user:pw@example.com/a", "https://user@example.com/a",
    "https://exa mple.com/a", "https://example.com/a\nb", "https://example.com/a" + chr(92) + "b","https://example.com:99999/a",
    "https://example.com:abc/a", "", "   ", "https://example.com/" + "a" * 2000,
])
def test_other_links_are_refused(url):
    with pytest.raises(HTTPException) as caught:
        validate_link(url)
    assert caught.value.status_code == 422


def test_names_cannot_carry_paths_or_control_characters():
    assert clean_name("../../etc/passwd", "x") == "passwd"
    assert clean_name("C:" + chr(92) + "Users" + chr(92) + "slip.png", "x") == "slip.png"
    assert clean_name("sl\x00ip\n.png", "x") == "slip.png"
    assert clean_name("", "attachment") == "attachment"
    assert clean_name(None, "attachment") == "attachment"
    assert len(clean_name("a" * 500 + ".png", "x")) == 200
