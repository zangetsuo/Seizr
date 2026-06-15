"""Email sending. Uses SMTP when configured; otherwise a dev fallback that
prints the message (and any link) to the console so the flow is testable
without an email provider.

Configure for production via env:
    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS,
    SMTP_FROM (default no-reply@seizr.app), SMTP_STARTTLS (default "1")
"""

import asyncio
import os
import smtplib
from email.message import EmailMessage

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "no-reply@seizr.app")
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1") == "1"


def _send_sync(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        if SMTP_STARTTLS:
            s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


async def send_email(to: str, subject: str, body: str) -> None:
    if not SMTP_HOST:
        # dev fallback — surface the message (and link) in the server log
        print("\n--- DEV EMAIL (no SMTP configured) ---", flush=True)
        print(f"To: {to}\nSubject: {subject}\n\n{body}", flush=True)
        print("--- end email ---\n", flush=True)
        return
    await asyncio.get_running_loop().run_in_executor(None, _send_sync, to, subject, body)


async def send_verification(to: str, link: str) -> None:
    body = (
        "Welcome to Seizr.\n\n"
        "Confirm your email to activate your account:\n"
        f"{link}\n\n"
        "This link expires in 24 hours. If you didn't sign up, ignore this email."
    )
    await send_email(to, "Verify your Seizr account", body)
