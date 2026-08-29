from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WEBMARS_USERS_ENV", "development")

from app import create_app
from config import DEV_SECRET_KEY
from db import get_db


class SecretKeyGuardTests(unittest.TestCase):
    """A publicly known signing key lets anyone mint a session for any user id."""

    def test_production_refuses_to_start_with_the_placeholder_secret(self) -> None:
        with mock.patch.dict(os.environ, {"WEBMARS_USERS_ENV": "production"}):
            with self.assertRaises(RuntimeError) as raised:
                create_app({"SECRET_KEY": DEV_SECRET_KEY})

        self.assertIn("SECRET_KEY", str(raised.exception))

    def test_an_unspecified_environment_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as raised:
                create_app({"SECRET_KEY": DEV_SECRET_KEY})

        self.assertIn("WEBMARS_USERS_ENV", str(raised.exception))

    def test_production_starts_once_a_real_secret_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"WEBMARS_USERS_ENV": "production"}):
                app = create_app(
                    {
                        "SECRET_KEY": "a-real-production-secret",
                        "DATABASE": Path(directory) / "test.db",
                        "REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH": False,
                    }
                )

        self.assertFalse(app.config["DEBUG"])

    def test_production_refuses_verified_publishing_without_mail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"WEBMARS_USERS_ENV": "production"}):
                with self.assertRaises(RuntimeError) as raised:
                    create_app(
                        {
                            "SECRET_KEY": "a-real-production-secret",
                            "DATABASE": Path(directory) / "test.db",
                            "MAIL_ENABLED": False,
                            "REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH": True,
                        }
                    )

        self.assertIn("verification", str(raised.exception).lower())

    def test_development_still_starts_but_warns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"WEBMARS_USERS_ENV": "development"}):
                with self.assertLogs("app", level="WARNING") as logs:
                    create_app(
                        {
                            "SECRET_KEY": DEV_SECRET_KEY,
                            "DATABASE": Path(directory) / "test.db",
                        }
                    )

        self.assertTrue(any("forgeable" in message for message in logs.output))


class ServiceTestCase(unittest.TestCase):
    config_overrides: dict[str, object] = {}

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        overrides = {
            "TESTING": True,
            "DATABASE": Path(self.temporary_directory.name) / "test.db",
            "SECRET_KEY": "test-secret-key",
            "SESSION_COOKIE_SECURE": False,
        }
        overrides.update(self.config_overrides)
        self.app = create_app(overrides)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def register(self, password: str = "correct horse battery staple"):
        return self.client.post(
            "/api/register",
            json={
                "username": "rate-limit-user",
                "email": "rate@example.com",
                "password": password,
            },
        )


class LoginRateLimitTests(ServiceTestCase):
    config_overrides = {"LOGIN_MAX_ATTEMPTS": 3, "LOGIN_ATTEMPT_WINDOW_SECONDS": 900}

    def fail_login(self):
        return self.client.post(
            "/api/login",
            json={"username": "rate-limit-user", "password": "wrong password"},
        )

    def test_repeated_failures_from_one_address_are_turned_away(self) -> None:
        self.register()
        self.client.post("/api/logout", json={})

        for _ in range(3):
            self.assertEqual(self.fail_login().status_code, 401)

        blocked = self.fail_login()
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["Retry-After"], "900")

    def test_the_correct_password_is_refused_too_while_blocked(self) -> None:
        self.register()
        self.client.post("/api/logout", json={})
        for _ in range(3):
            self.fail_login()

        response = self.client.post(
            "/api/login",
            json={"username": "rate-limit-user", "password": "correct horse battery staple"},
        )

        self.assertEqual(response.status_code, 429)

    def test_a_successful_login_does_not_reset_the_address_counter(self) -> None:
        self.register()
        self.client.post("/api/logout", json={})
        self.fail_login()
        self.fail_login()

        good = self.client.post(
            "/api/login",
            json={"username": "rate-limit-user", "password": "correct horse battery staple"},
        )
        self.assertEqual(good.status_code, 200)

        self.client.post("/api/logout", json={})
        self.assertEqual(self.fail_login().status_code, 401)
        self.assertEqual(self.fail_login().status_code, 429)

    def test_the_forwarded_header_cannot_shed_the_limit_without_a_trusted_proxy(self) -> None:
        self.register()
        self.client.post("/api/logout", json={})
        for _ in range(3):
            self.fail_login()

        response = self.client.post(
            "/api/login",
            json={"username": "rate-limit-user", "password": "wrong password"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )

        self.assertEqual(response.status_code, 429)


class RegistrationGuardTests(ServiceTestCase):
    config_overrides = {
        "REGISTER_MAX_ATTEMPTS": 1,
        "REGISTER_ATTEMPT_WINDOW_SECONDS": 3600,
        "PASSWORD_MAX_LENGTH": 32,
    }

    def test_registration_is_limited_before_another_password_hash(self) -> None:
        self.assertEqual(self.register(password="short enough").status_code, 201)
        limited = self.client.post(
            "/api/register",
            json={
                "username": "another-user",
                "email": "another@example.com",
                "password": "short enough",
            },
        )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)

    def test_oversized_password_and_non_object_json_are_rejected(self) -> None:
        oversized = self.client.post(
            "/api/register",
            json={
                "username": "long-password",
                "email": "long@example.com",
                "password": "x" * 33,
            },
        )
        malformed = self.client.post("/api/register", json=["not", "an", "object"])
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(malformed.status_code, 400)


class RequestSizeLimitTests(ServiceTestCase):
    config_overrides = {"MAX_CONTENT_LENGTH": 2048}

    def test_an_oversized_project_document_is_rejected(self) -> None:
        self.register()

        response = self.client.post(
            "/api/projects",
            json={"name": "huge", "project": {"files": {"main.asm": "x" * 8192}}},
        )

        self.assertEqual(response.status_code, 413)

    def test_a_normal_project_still_saves(self) -> None:
        self.register()

        response = self.client.post(
            "/api/projects",
            json={"name": "small", "project": {"files": {"main.asm": ".text\nmain:\n"}}},
        )

        self.assertEqual(response.status_code, 201)


class CorsHeaderTests(ServiceTestCase):
    def test_the_locking_header_is_allowed_and_the_etag_is_exposed(self) -> None:
        response = self.client.get(
            "/api/health",
            headers={"Origin": "http://localhost:4173"},
        )

        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://localhost:4173")
        self.assertIn("If-Match", response.headers["Access-Control-Allow-Headers"])
        self.assertIn("ETag", response.headers["Access-Control-Expose-Headers"])

    def test_an_unknown_origin_gets_no_cors_grant(self) -> None:
        response = self.client.get(
            "/api/health",
            headers={"Origin": "https://not-webmars.example"},
        )

        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_an_unknown_browser_origin_cannot_send_a_state_changing_request(self) -> None:
        response = self.client.post(
            "/api/register",
            json={"username": "intruder", "email": "intruder@example.com", "password": "secret"},
            headers={"Origin": "https://evil.nfiles.top"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)


class SqliteConcurrencyTests(ServiceTestCase):
    def test_connections_use_wal_and_a_busy_timeout(self) -> None:
        with self.app.app_context():
            journal_mode = get_db().execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = get_db().execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertEqual(busy_timeout, 5000)


if __name__ == "__main__":
    unittest.main()
