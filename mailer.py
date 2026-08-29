from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from typing import Callable

from flask import current_app


def mail_is_configured() -> bool:
    config = current_app.config
    if callable(config.get("MAIL_SEND_FUNCTION")):
        return True
    return bool(
        config.get("MAIL_ENABLED")
        and config.get("MAIL_FROM")
        and config.get("SMTP_HOST")
    )


def send_mail(to_address: str, subject: str, text: str, html: str = "") -> bool:
    """Send one transactional message through the deployment's SMTP relay.

    Tests and embedded deployments may inject MAIL_SEND_FUNCTION.  Production
    uses only standard SMTP, keeping BackMars independent of any mail vendor.
    """
    override: Callable[..., object] | None = current_app.config.get("MAIL_SEND_FUNCTION")
    if callable(override):
        override(to_address=to_address, subject=subject, text=text, html=html)
        return True
    if not mail_is_configured():
        current_app.logger.warning("Transactional mail requested, but SMTP is not configured.")
        return False

    config = current_app.config
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((str(config["MAIL_FROM_NAME"]), str(config["MAIL_FROM"])))
    message["To"] = str(to_address)
    message.set_content(str(text))
    if html:
        message.add_alternative(str(html), subtype="html")

    host = str(config["SMTP_HOST"])
    port = int(config["SMTP_PORT"])
    timeout = max(1, int(config["SMTP_TIMEOUT_SECONDS"]))
    security = str(config.get("SMTP_SECURITY", "starttls")).lower()
    username = str(config.get("SMTP_USERNAME", ""))
    password = str(config.get("SMTP_PASSWORD", ""))
    context = ssl.create_default_context()

    if security not in {"starttls", "ssl", "tls", "none"}:
        raise ValueError("SMTP security must be starttls, ssl, tls or none.")
    if security == "none" and username:
        raise ValueError("SMTP credentials must never be sent without TLS.")

    if security in {"ssl", "tls"}:
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    with client:
        client.ehlo()
        if security == "starttls":
            client.starttls(context=context)
            client.ehlo()
        if username:
            client.login(username, password)
        client.send_message(message)
    return True


def send_account_code(email: str, username: str, code: str, purpose: str, ttl_seconds: int) -> bool:
    minutes = max(1, (int(ttl_seconds) + 59) // 60)
    if purpose == "email_verification":
        subject = "Confirm your webMARS email"
        action = "confirm your email address"
    else:
        subject = "Your webMARS password reset code"
        action = "reset your password"
    text = (
        f"Hello {username},\n\n"
        f"Use this code to {action}:\n\n{code}\n\n"
        f"The code expires in {minutes} minutes and can be used only once. "
        "If you did not request it, you can ignore this message.\n\n"
        "webMARS"
    )
    html = (
        f"<p>Hello {escape(str(username))},</p><p>Use this code to {escape(action)}:</p>"
        f"<p style=\"font:700 30px/1.2 monospace;letter-spacing:.18em\">{escape(str(code))}</p>"
        f"<p>The code expires in {minutes} minutes and can be used only once.</p>"
        "<p>If you did not request it, you can ignore this message.</p><p>webMARS</p>"
    )
    return send_mail(email, subject, text, html)


def send_password_changed_notice(email: str, username: str) -> bool:
    return send_mail(
        email,
        "Your webMARS password was changed",
        f"Hello {username},\n\nYour webMARS password was changed. "
        "If this was not you, contact the administrator of this webMARS installation.\n",
    )
