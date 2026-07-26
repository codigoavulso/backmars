from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path

from app import create_app
from config import ProductionConfig


class PersistentAuthSessionTests(unittest.TestCase):
    def test_production_cookies_are_secure_by_default(self) -> None:
        self.assertTrue(ProductionConfig.SESSION_COOKIE_SECURE)

    def test_health_endpoint_reports_the_service_version(self) -> None:
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["version"], "0.1.1")

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "test.db"
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": database_path,
                "SECRET_KEY": "test-secret-key",
                "SESSION_COOKIE_SECURE": False,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def register(self):
        return self.client.post(
            "/api/register",
            json={
                "username": "session-user",
                "email": "session@example.com",
                "password": "correct horse battery staple",
            },
        )

    def session_cookie_from(self, response):
        cookie = SimpleCookie()
        cookie.load(response.headers["Set-Cookie"])
        return cookie[self.app.config["SESSION_COOKIE_NAME"]]

    def test_registration_creates_a_one_year_permanent_session(self) -> None:
        before = datetime.now(timezone.utc)
        response = self.register()
        after = datetime.now(timezone.utc)

        self.assertEqual(response.status_code, 201)
        cookie = self.session_cookie_from(response)
        expiry = parsedate_to_datetime(cookie["expires"])
        self.assertGreaterEqual(expiry, before + timedelta(days=365) - timedelta(seconds=2))
        self.assertLessEqual(expiry, after + timedelta(days=365) + timedelta(seconds=2))
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"].lower(), "lax")

        serializer = self.app.session_interface.get_signing_serializer(self.app)
        payload = serializer.loads(cookie.value)
        self.assertTrue(payload["_permanent"])
        self.assertGreater(payload["user_id"], 0)

    def test_session_survives_a_follow_up_page_load(self) -> None:
        self.register()

        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["authenticated"])
        self.assertEqual(response.get_json()["user"]["username"], "session-user")
        self.assertNotIn("Set-Cookie", response.headers)

    def test_stored_cookie_restores_the_session_in_a_new_browser_client(self) -> None:
        registration_response = self.register()
        cookie = self.session_cookie_from(registration_response)
        fresh_client = self.app.test_client(use_cookies=False)

        response = fresh_client.get(
            "/api/me",
            headers={
                "Cookie": (
                    f"{self.app.config['SESSION_COOKIE_NAME']}={cookie.value}"
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["authenticated"])
        self.assertEqual(response.get_json()["user"]["username"], "session-user")

    def test_logout_revokes_the_session_immediately(self) -> None:
        self.register()

        logout_response = self.client.post("/api/logout", json={})
        me_response = self.client.get("/api/me")

        self.assertEqual(logout_response.status_code, 200)
        expired_cookie = self.session_cookie_from(logout_response)
        self.assertEqual(expired_cookie["max-age"], "0")
        self.assertFalse(me_response.get_json()["authenticated"])

    def test_login_also_creates_a_permanent_session(self) -> None:
        self.register()
        self.client.post("/api/logout", json={})

        response = self.client.post(
            "/api/login",
            json={"username": "session-user", "password": "correct horse battery staple"},
        )

        self.assertEqual(response.status_code, 200)
        cookie = self.session_cookie_from(response)
        serializer = self.app.session_interface.get_signing_serializer(self.app)
        self.assertTrue(serializer.loads(cookie.value)["_permanent"])


if __name__ == "__main__":
    unittest.main()
