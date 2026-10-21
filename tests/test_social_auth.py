import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("WEBMARS_USERS_ENV", "development")
from app import create_app
from db import get_db


ORIGIN = {"Origin": "http://localhost:8080"}


class SocialAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE": Path(self.tmp.name)/"db.sqlite3",
            "SECRET_KEY": "test-session-secret", "SESSION_COOKIE_SECURE": False,
            "GOOGLE_CLIENT_ID": "test-client", "GOOGLE_CLIENT_SECRET": "test-secret",
            "OAUTH_CALLBACK_BASE": "http://localhost:5000/api"})
        self.client = self.app.test_client()
        self.remote = self.app.extensions["social_login"]["oauth"].create_client("google")

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, **changes):
        # Metadata is public protocol configuration; no provider network calls.
        with patch.object(self.remote, "load_server_metadata", return_value={"authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth"}):
            response = self.client.post("/api/auth/google/start", json=changes, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def finish(self, state, subject="google-user", email="alice@example.com", verified=True):
        with patch.object(self.remote, "authorize_access_token", return_value={"userinfo": {"sub": subject, "email": email, "email_verified": verified}}):
            return self.client.get("/api/auth/google/callback", query_string={"state": state, "code": "test-code"})

    def test_disabled_provider_and_origin_are_rejected(self):
        self.assertEqual(self.client.get("/api/auth/providers").get_json()["providers"], ["google"])
        self.assertEqual(self.client.post("/api/auth/facebook/start", json={}, headers=ORIGIN).status_code, 404)
        self.assertEqual(self.client.post("/api/auth/google/start", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/auth/google/start", json={"intent": "link"}, headers=ORIGIN).status_code, 401)

    def prepare_merge(self):
        self.finish(self.start()["state"])
        source = self.client.get("/api/me").get_json()["user"]["id"]
        old_cookie = self.client.get_cookie("session").value
        self.client.post("/api/logout")
        self.client.post("/api/register", json={"username": "primary", "email": "primary@example.com", "password": "correct horse battery staple"})
        target = self.client.get("/api/me").get_json()["user"]["id"]
        with self.app.app_context():
            db = get_db()
            for owner, name, content in [(source, "Demo", '{"files":{"source":"saved"}}'),
                                         (target, "demo", '{"files":{"target":"saved"}}')]:
                db.execute("INSERT INTO projects(user_id,name,project_json,created_at,updated_at) VALUES (?,?,?, 'now','now')", (owner, name, content))
            db.execute("INSERT INTO publications(owner_id,slug,title,created_at,updated_at) VALUES (?, 'permanent-link', 'Published work','now','now')", (source,))
            db.commit()
        response = self.finish(self.start(intent="link")["state"])
        self.assertIn('"status": "merge_required"', response.get_data(as_text=True))
        return source, target, old_cookie

    def test_merge_preserves_both_projects_and_public_links_and_revokes_source_session(self):
        source, target, old_cookie = self.prepare_merge()
        preview = self.client.get("/api/auth/merge").get_json()
        self.assertEqual((preview["target"], preview["projects"], preview["publications"]), ("primary", 1, 1))
        # Discovery/cancellation alone cannot change ownership.
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
        data = {"confirm": True, "password": "correct horse battery staple"}
        self.assertEqual(self.client.post("/api/auth/merge", json=data).status_code, 403)
        response = self.client.post("/api/auth/merge", json=data, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["user"]["id"], target)
        self.assertEqual(self.client.post("/api/auth/merge", json=data, headers=ORIGIN).status_code, 409)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE id=?", (source,)).fetchone()[0], 0)
            projects = db.execute("SELECT name,project_json FROM projects WHERE user_id=? ORDER BY id", (target,)).fetchall()
            self.assertEqual({p["name"] for p in projects}, {"demo", "Demo (Google)"})
            self.assertTrue(any('"source"' in p["project_json"] for p in projects))
            self.assertTrue(any('"target"' in p["project_json"] for p in projects))
            self.assertEqual(db.execute("SELECT owner_id FROM publications WHERE slug='permanent-link'").fetchone()[0], target)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        old = self.app.test_client(); old.set_cookie("session", old_cookie)
        self.assertFalse(old.get("/api/me").get_json()["authenticated"])
        self.client.post("/api/logout")
        self.finish(self.start()["state"])
        self.assertEqual(self.client.get("/api/me").get_json()["user"]["id"], target)
        self.client.post("/api/logout")
        self.assertEqual(self.client.post("/api/login", json={"username":"primary", "password":data["password"]}).status_code, 200)

    def test_merge_requires_password_and_fresh_proof_for_both_accounts(self):
        source, target, _ = self.prepare_merge()
        self.assertEqual(self.client.post("/api/auth/merge", json={"confirm": True, "password":"incorrect"}, headers=ORIGIN).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/merge", json={"password":"correct horse battery staple"}, headers=ORIGIN).status_code, 400)
        with self.app.app_context():
            get_db().execute("UPDATE users SET session_version=session_version+1 WHERE id=?", (source,)); get_db().commit()
        self.assertEqual(self.client.post("/api/auth/merge", json={"confirm": True, "password":"correct horse battery staple"}, headers=ORIGIN).status_code, 409)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_merge_proof_expires_and_cannot_be_used_in_another_browser(self):
        self.prepare_merge()
        self.assertEqual(self.app.test_client().get("/api/auth/merge").status_code, 409)
        with self.app.app_context():
            get_db().execute("UPDATE account_merges SET expires_at=0"); get_db().commit()
        self.assertEqual(self.client.post("/api/auth/merge", json={"confirm": True, "password":"correct horse battery staple"}, headers=ORIGIN).status_code, 409)

    def test_merge_rolls_back_all_transfers_if_a_later_write_fails(self):
        self.prepare_merge()
        with self.app.app_context():
            db = get_db()
            before = [tuple(r) for r in db.execute("SELECT id,user_id,name FROM projects ORDER BY id")]
            db.execute("CREATE TRIGGER prevent_transfer BEFORE UPDATE ON publications BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
            db.commit()
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.post("/api/auth/merge", json={"confirm": True, "password":"correct horse battery staple"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 500)
        with self.app.app_context():
            self.assertEqual([tuple(r) for r in get_db().execute("SELECT id,user_id,name FROM projects ORDER BY id")], before)
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_merge_does_not_discard_another_accounts_password(self):
        source, _, _ = self.prepare_merge()
        with self.app.app_context():
            from utils import hash_password
            get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password("another account password"), source))
            get_db().commit()
        response = self.finish(self.start(intent="link")["state"])
        self.assertIn('"status": "identity_conflict"', response.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/auth/merge").status_code, 409)

    def test_google_creates_and_reuses_one_cloud_account_with_minimal_scope(self):
        start = self.start()
        self.assertIn("scope=openid+email", start["url"])
        self.assertIn("code_challenge=", start["url"])
        self.assertIn("nonce=", start["url"])
        response = self.finish(start["state"])
        self.assertIn('"status": "created"', response.get_data(as_text=True))
        user = self.client.get("/api/me").get_json()["user"]
        self.assertTrue(user["emailVerified"])
        self.assertEqual(self.client.get("/api/auth/providers").get_json()["connected"], ["google"])
        self.client.post("/api/logout")
        self.finish(self.start()["state"])
        self.assertEqual(self.client.get("/api/me").get_json()["user"]["id"], user["id"])
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            row = dict(get_db().execute("SELECT * FROM external_identities").fetchone())
            self.assertEqual(set(row), {"provider", "subject", "user_id", "created_at"})

    def test_existing_email_requires_explicit_authenticated_link_and_preserves_projects(self):
        self.client.post("/api/register", json={"username": "alice", "email": "alice@example.com", "password": "correct horse battery staple"})
        user = self.client.get("/api/me").get_json()["user"]
        self.client.post("/api/logout")
        response = self.finish(self.start()["state"])
        self.assertIn('"status": "existing_account"', response.get_data(as_text=True))
        self.assertFalse(self.client.get("/api/me").get_json()["authenticated"])
        self.client.post("/api/login", json={"username": "alice", "password": "correct horse battery staple"})
        response = self.finish(self.start(intent="link")["state"])
        self.assertIn('"status": "linked"', response.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/me").get_json()["user"]["id"], user["id"])

    def test_expired_tampered_cross_browser_and_replayed_states_do_not_login(self):
        state = self.start()["state"]
        other = self.app.test_client()
        response = other.get("/api/auth/google/callback", query_string={"state": state, "code": "test"})
        self.assertIn("invalid_state", response.get_data(as_text=True))
        self.finish(state)
        self.client.post("/api/logout")
        response = self.finish(state)
        self.assertIn("invalid_state", response.get_data(as_text=True))
        state = self.start()["state"]
        with self.app.app_context():
            get_db().execute("UPDATE oauth_flows SET expires_at=?", (int(time.time())-1,)); get_db().commit()
        self.assertIn("invalid_state", self.finish(state).get_data(as_text=True))
        self.assertFalse(self.client.get("/api/me").get_json()["authenticated"])

    def test_unverified_google_email_and_provider_errors_fail_closed_without_tokens(self):
        response = self.finish(self.start()["state"], verified=False)
        self.assertIn("verified_email_required", response.get_data(as_text=True))
        state = self.start()["state"]
        with patch.object(self.remote, "authorize_access_token", side_effect=RuntimeError("secret-access-token")):
            response = self.client.get("/api/auth/google/callback", query_string={"state": state})
        self.assertNotIn("secret-access-token", response.get_data(as_text=True))
        self.assertFalse(self.client.get("/api/me").get_json()["authenticated"])

    def test_server_statistics_exclude_internal_context_and_have_no_account_identity(self):
        self.finish(self.start(context="internal")["state"])
        public = self.client.get("/api/statistics?window=24h").get_json()
        internal = self.client.get("/api/statistics?window=24h&context=internal").get_json()
        self.assertEqual(public["events"], [])
        self.assertEqual(internal["active_accounts"], 1)
        self.assertEqual({row["event"] for row in internal["events"]}, {"account_created", "login_success"})
        self.assertNotIn("alice", str(internal))
        self.assertNotIn("user_id", str(internal))

    def facebook_app(self):
        self.app = create_app({"TESTING": True, "DATABASE": Path(self.tmp.name)/"facebook.sqlite3",
            "SECRET_KEY": "test-session-secret", "SESSION_COOKIE_SECURE": False,
            "FACEBOOK_CLIENT_ID": "test-facebook", "FACEBOOK_CLIENT_SECRET": "test-secret",
            "FACEBOOK_API_VERSION": "v25.0", "OAUTH_CALLBACK_BASE": "http://localhost:5000/api"})
        self.client = self.app.test_client()
        return self.app.extensions["social_login"]["oauth"].create_client("facebook")

    def test_facebook_uses_only_id_email_and_never_claims_email_was_verified(self):
        from unittest.mock import Mock
        remote = self.facebook_app()
        state = self.client.post("/api/auth/facebook/start", json={}, headers=ORIGIN).get_json()["state"]
        result = Mock()
        result.json.return_value = {"id":"facebook-subject", "email":"facebook@example.com"}
        with patch.object(remote, "authorize_access_token", return_value={"access_token":"private-token"}), patch.object(remote, "get", return_value=result) as get:
            response = self.client.get("/api/auth/facebook/callback", query_string={"state":state,"code":"test"})
        self.assertIn('"status": "created"', response.get_data(as_text=True))
        self.assertEqual(get.call_args.kwargs["params"]["fields"], "id,email")
        self.assertNotIn("private-token", response.get_data(as_text=True))
        self.assertFalse(self.client.get("/api/me").get_json()["user"]["emailVerified"])

    def test_facebook_missing_email_does_not_create_an_unusable_account(self):
        from unittest.mock import Mock
        remote = self.facebook_app()
        state = self.client.post("/api/auth/facebook/start", json={}, headers=ORIGIN).get_json()["state"]
        result = Mock(); result.json.return_value = {"id":"facebook-without-email"}
        with patch.object(remote, "authorize_access_token", return_value={"access_token":"private-token"}), patch.object(remote, "get", return_value=result):
            response = self.client.get("/api/auth/facebook/callback", query_string={"state":state,"code":"test"})
        self.assertIn("verified_email_required", response.get_data(as_text=True))
        self.assertFalse(self.client.get("/api/me").get_json()["authenticated"])

    def test_linking_rejects_a_revoked_session_and_social_only_password_login_is_invalid(self):
        self.finish(self.start()["state"])
        response = self.client.post("/api/login", json={"username":"alice@example.com","password":"not-a-password"})
        self.assertEqual(response.status_code, 401)
        state = self.start(intent="link")["state"]
        with self.app.app_context():
            get_db().execute("UPDATE users SET session_version=session_version+1"); get_db().commit()
        response = self.finish(state)
        self.assertIn("invalid_state", response.get_data(as_text=True))
