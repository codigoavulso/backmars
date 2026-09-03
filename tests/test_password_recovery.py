from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("WEBMARS_USERS_ENV", "development")

from app import create_app
from db import get_db


class PasswordRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.messages: list[dict[str, str]] = []

        def capture_mail(**message: str) -> None:
            self.messages.append(message)

        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": Path(self.temporary_directory.name) / "test.db",
                "SECRET_KEY": "test-secret-key",
                "SESSION_COOKIE_SECURE": False,
                "MAIL_SEND_FUNCTION": capture_mail,
                "ACCOUNT_CODE_TTL_SECONDS": 600,
                "ACCOUNT_CODE_MAX_ATTEMPTS": 5,
            }
        )
        self.client = self.app.test_client()
        response = self.client.post(
            "/api/register",
            json={
                "username": "recover-user",
                "email": "recover@example.com",
                "password": "old password",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.messages.clear()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def request_code(self) -> str:
        response = self.client.post(
            "/api/password-reset/request", json={"email": "recover@example.com"}
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(self.messages), 1)
        match = re.search(r"\b(\d{6})\b", self.messages[-1]["text"])
        self.assertIsNotNone(match)
        return match.group(1) if match else ""

    def test_request_does_not_reveal_whether_an_account_exists(self) -> None:
        existing = self.client.post(
            "/api/password-reset/request", json={"email": "recover@example.com"}
        )
        unknown = self.client.post(
            "/api/password-reset/request", json={"email": "unknown@example.com"}
        )

        self.assertEqual(existing.status_code, 202)
        self.assertEqual(unknown.status_code, 202)
        self.assertEqual(existing.get_json(), unknown.get_json())

    def test_code_requests_are_limited_without_invalidating_the_latest_code(self) -> None:
        codes = []
        for _ in range(6):
            response = self.client.post(
                "/api/password-reset/request", json={"email": "recover@example.com"}
            )
            self.assertEqual(response.status_code, 202)
            if len(self.messages) > len(codes):
                match = re.search(r"\b(\d{6})\b", self.messages[-1]["text"])
                self.assertIsNotNone(match)
                codes.append(match.group(1))

        self.assertEqual(len(self.messages), 5)
        self.assertEqual(len(codes), 5)
        confirmed = self.client.post(
            "/api/password-reset/confirm",
            json={
                "email": "recover@example.com",
                "code": codes[-1],
                "password": "new secure password",
            },
        )
        self.assertEqual(confirmed.status_code, 200)

    def test_only_a_digest_is_stored_and_the_code_is_one_use(self) -> None:
        code = self.request_code()
        with self.app.app_context():
            row = get_db().execute(
                "SELECT code_hash FROM account_codes WHERE purpose = 'password_reset'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertNotEqual(row["code_hash"], code)
            self.assertNotIn(code, row["code_hash"])

        first = self.client.post(
            "/api/password-reset/confirm",
            json={
                "email": "recover@example.com",
                "code": code,
                "password": "new secure password",
            },
        )
        second = self.client.post(
            "/api/password-reset/confirm",
            json={
                "email": "recover@example.com",
                "code": code,
                "password": "another secure password",
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_reset_revokes_other_sessions_and_changes_the_password(self) -> None:
        other_browser = self.app.test_client()
        login = other_browser.post(
            "/api/login",
            json={"username": "recover-user", "password": "old password"},
        )
        self.assertEqual(login.status_code, 200)
        code = self.request_code()

        reset = self.client.post(
            "/api/password-reset/confirm",
            json={
                "email": "recover@example.com",
                "code": code,
                "password": "new secure password",
            },
        )

        self.assertEqual(reset.status_code, 200)
        self.assertFalse(other_browser.get("/api/me").get_json()["authenticated"])
        old_login = other_browser.post(
            "/api/login",
            json={"username": "recover-user", "password": "old password"},
        )
        new_login = other_browser.post(
            "/api/login",
            json={"username": "recover@example.com", "password": "new secure password"},
        )
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200)

    def test_five_wrong_attempts_invalidate_the_code(self) -> None:
        code = self.request_code()
        for attempt in range(5):
            wrong = f"{(int(code) + attempt + 1) % 1_000_000:06d}"
            response = self.client.post(
                "/api/password-reset/confirm",
                json={
                    "email": "recover@example.com",
                    "code": wrong,
                    "password": "new secure password",
                },
            )
            self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/password-reset/confirm",
            json={
                "email": "recover@example.com",
                "code": code,
                "password": "new secure password",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_codes_do_not_trigger_expensive_password_hashing(self) -> None:
        code = self.request_code()
        wrong = f"{(int(code) + 1) % 1_000_000:06d}"
        with patch("auth.hash_password") as password_hasher:
            response = self.client.post(
                "/api/password-reset/confirm",
                json={
                    "email": "recover@example.com",
                    "code": wrong,
                    "password": "new secure password",
                },
            )

        self.assertEqual(response.status_code, 400)
        password_hasher.assert_not_called()

    def test_recovery_reports_an_unconfigured_mail_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                {
                    "TESTING": True,
                    "DATABASE": Path(directory) / "test.db",
                    "SECRET_KEY": "test-secret-key",
                    "MAIL_SEND_FUNCTION": None,
                    "MAIL_ENABLED": False,
                }
            )
            response = app.test_client().post(
                "/api/password-reset/request", json={"email": "someone@example.com"}
            )
        self.assertEqual(response.status_code, 503)

    def test_recovery_fields_must_be_text(self) -> None:
        requested = self.client.post(
            "/api/password-reset/request", json={"email": ["recover@example.com"]}
        )
        confirmed = self.client.post(
            "/api/password-reset/confirm",
            json={
                "email": "recover@example.com",
                "code": ["123456"],
                "password": "new secure password",
            },
        )
        self.assertEqual(requested.status_code, 400)
        self.assertEqual(confirmed.status_code, 400)


if __name__ == "__main__":
    unittest.main()
