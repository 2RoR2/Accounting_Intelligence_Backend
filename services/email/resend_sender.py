import os


def send_email(*, recipient: str, subject: str, text: str, html: str):
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("EMAIL_FROM", "").strip()
    if not api_key or not sender:
        raise RuntimeError("Configure RESEND_API_KEY and EMAIL_FROM in the backend environment.")
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        # Linux deployments use the system CA bundle; Windows development
        # uses truststore to include the Windows root/enterprise CAs.
        pass
    try:
        import resend
    except ImportError as exc:
        raise RuntimeError("The resend package is not installed.") from exc
    resend.api_key = api_key
    resend.Emails.send({"from": sender, "to": [recipient], "subject": subject, "text": text, "html": html})
