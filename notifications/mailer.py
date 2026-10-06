import os
import smtplib
import ssl
from email.message import EmailMessage


def _send_code(recipient: str, code: str, subject: str, label: str, expiry: str):
    username = os.environ.get("SMTP_USER", "").strip()
    # Gmail displays App Passwords in groups separated by spaces; normalize them
    # so either the grouped or ungrouped form works in .env.
    password = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()
    sender = os.environ.get("SMTP_FROM", username).strip()
    if not username or not password or "@" not in sender:
        raise RuntimeError("Configure SMTP_USER, SMTP_PASSWORD and SMTP_FROM in backend .env.")
    message = EmailMessage()
    message["From"] = f"Accounting Intelligence <{sender}>"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(f"Your {label} is {code}.\n\n"
                        f"It expires in {expiry}. Do not share this code.\n"
                        "If you did not request a reset, ignore this email.")
    message.add_alternative(
        f"<html><body style='font-family:Arial,sans-serif;color:#172033'>"
        f"<h2 style='margin-bottom:8px'>Accounting Intelligence</h2>"
        f"<p>Your {label} is:</p>"
        f"<p style='font-size:32px;font-weight:700;letter-spacing:8px;margin:20px 0;user-select:all'>{code}</p>"
        f"<p>This code expires in <strong>{expiry}</strong>. Select the bold code above to copy it.</p>"
        f"<p style='color:#667085;font-size:13px'>Never share this code. If you did not request it, you can ignore this email.</p>"
        f"</body></html>", subtype="html"
    )
    port = int(os.getenv("SMTP_PORT", "587"))
    smtp_class = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with smtp_class(os.getenv("SMTP_HOST", "smtp.gmail.com"), port, timeout=15) as smtp:
        if port != 465:
            smtp.starttls(context=ssl.create_default_context())
        smtp.login(username, password)
        smtp.send_message(message)


def send_reset_code(recipient: str, code: str):
    _send_code(recipient, code, "Accounting Intelligence password reset code", "password reset code", "10 minutes")


def send_login_code(recipient: str, code: str):
    _send_code(recipient, code, "Your Accounting Intelligence sign-in code", "sign-in code", "60 seconds")
