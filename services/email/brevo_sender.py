"""Brevo transactional email transport.

Uses Brevo's HTTPS API directly so it works on Render/Vercel without SMTP
port requirements or the campaign API (OTP messages are transactional).
"""
import json
import os
from urllib import error, request


def send_email(*, recipient: str, subject: str, text: str, html: str):
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("BREVO_FROM_EMAIL", os.environ.get("EMAIL_FROM", "")).strip()
    sender_name = os.environ.get("BREVO_FROM_NAME", "Accounting Intelligence SAIC").strip()
    if not api_key or not sender_email:
        raise RuntimeError("Configure BREVO_API_KEY and BREVO_FROM_EMAIL in the backend environment.")
    payload = json.dumps({
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": recipient}],
        "subject": subject,
        "textContent": text,
        "htmlContent": html,
    }).encode("utf-8")
    req = request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=payload,
        headers={"accept": "application/json", "api-key": api_key, "content-type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            if response.status not in (200, 201, 202):
                raise RuntimeError(f"Brevo returned HTTP {response.status}.")
    except error.HTTPError as exc:
        raise RuntimeError(f"Brevo rejected the email (HTTP {exc.code}).") from exc
    except error.URLError as exc:
        raise RuntimeError("Brevo email service is unreachable.") from exc
