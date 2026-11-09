from io import BytesIO
import hashlib
from unittest.mock import patch

from db import get_db
from test_security import ServiceTestCase


class FeedbackAndAttributionTests(ServiceTestCase):
    config_overrides = {
        "INTERNAL_USERNAMES": "Nenepbl,teste",
        "METRICS_INTERNAL_RELAY": True,
        "FEEDBACK_TO": "operator@example.test",
    }
    origin = {"Origin": "http://localhost:4173"}

    def test_account_overrides_login_context_and_associates_existing_token(self):
        token = "a" * 32
        route = "/api/statistics/session"
        self.assertEqual(self.client.post(route, json={"session": token}).status_code, 401)
        self.assertEqual(self.client.post("/api/register", json={
            "username": "nenepbl", "email": "test@example.test", "password": "test passphrase 123"
        }).status_code, 201)
        with patch("account_statistics.urlopen") as relay:
            relay.return_value.__enter__.return_value.status = 204
            result = self.client.post(route, json={"session": token}, headers={"X-WebMARS-Context": "public"})
            self.assertEqual(result.status_code, 204)
            self.assertEqual(relay.call_args.args[0].full_url, "http://127.0.0.1:8766/associate")
        with self.app.app_context():
            row = get_db().execute("SELECT session_hash FROM account_metric_sessions").fetchone()
            self.assertEqual(row["session_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(self.client.get("/api/statistics?context=public").json["active_accounts"], 0)
        self.assertEqual(self.client.get("/api/statistics?context=internal").json["active_accounts"], 1)
        self.assertTrue(self.client.get("/api/statistics?context=internal").json["events"])
        self.client.post("/api/logout")
        self.register()
        with patch("account_statistics.urlopen") as relay:
            self.assertEqual(self.client.post(route, json={"session": token}).status_code, 409)
            self.assertEqual(self.client.post(route, json={"session": "b" * 32}).status_code, 204)
            relay.assert_not_called()

    def test_feedback_validates_attachments_and_sends_only_selected_content(self):
        mail = []
        self.app.config["MAIL_SEND_FUNCTION"] = lambda **data: mail.append(data)
        route = "/api/feedback"
        data = {"message": "An issue", "build": "202611082358"}
        self.assertTrue(self.client.get(route).json["enabled"])
        result = self.client.post(route, headers=self.origin, data={
            **data, "screenshots": (BytesIO(b"<script>"), "not-a-screenshot.png")
        })
        self.assertEqual(result.status_code, 400)
        self.assertFalse(mail)
        self.assertEqual(self.client.post(route, headers=self.origin, data=data,
                                         content_type="multipart/form-data").status_code, 204)
        self.assertNotIn("attachments", mail[0])
        self.assertEqual(self.client.post(route, headers=self.origin, data={
            **data,
            "screenshots": (BytesIO(b"\x89PNG\r\n\x1a\nfixture"), "arbitrary.png"),
            "project": (BytesIO(b'{"name":"demo","files":[]}'), "project.json"),
        }).status_code, 204)
        self.assertEqual([item[0] for item in mail[1]["attachments"]], ["screenshot-1.png", "webmars-project.json"])
        self.assertIn(data["build"], mail[1]["text"])
