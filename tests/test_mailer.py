from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WEBMARS_USERS_ENV", "development")

from app import create_app
from mailer import send_mail


class FakeSmtp:
    def __init__(self) -> None:
        self.events: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self) -> None:
        self.events.append("ehlo")

    def starttls(self, **_kwargs) -> None:
        self.events.append("starttls")

    def login(self, _username: str, _password: str) -> None:
        self.events.append("login")

    def send_message(self, _message) -> None:
        self.events.append("send")


class MailTransportTests(unittest.TestCase):
    def make_app(self, **overrides):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = {
            "TESTING": True,
            "DATABASE": Path(directory.name) / "test.db",
            "SECRET_KEY": "test-secret",
            "MAIL_ENABLED": True,
            "MAIL_FROM": "noreply@example.com",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": 587,
            "SMTP_SECURITY": "starttls",
            "SMTP_USERNAME": "noreply@example.com",
            "SMTP_PASSWORD": "app-password",
        }
        config.update(overrides)
        return create_app(config)

    def test_starttls_happens_before_authenticated_delivery(self) -> None:
        app = self.make_app()
        fake = FakeSmtp()
        with app.app_context(), mock.patch("mailer.smtplib.SMTP", return_value=fake):
            self.assertTrue(send_mail("student@example.com", "Subject", "Body"))
        self.assertEqual(fake.events, ["ehlo", "starttls", "ehlo", "login", "send"])

    def test_authenticated_plaintext_smtp_is_refused(self) -> None:
        app = self.make_app(SMTP_SECURITY="none")
        with app.app_context():
            with self.assertRaises(ValueError):
                send_mail("student@example.com", "Subject", "Body")

    def test_unknown_security_mode_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            self.make_app(SMTP_SECURITY="start-tls")


if __name__ == "__main__":
    unittest.main()
