from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("WEBMARS_USERS_ENV", "development")

from app import create_app


class PublicationTests(unittest.TestCase):
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
                "REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH": True,
            }
        )
        self.client = self.app.test_client()
        self.register_and_verify(self.client, "alice", "alice@example.com")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def register_and_verify(self, client, username: str, email: str) -> None:
        self.messages.clear()
        response = client.post(
            "/api/register",
            json={"username": username, "email": email, "password": "secure password"},
        )
        self.assertEqual(response.status_code, 201)
        match = re.search(r"\b(\d{6})\b", self.messages[-1]["text"])
        self.assertIsNotNone(match)
        verified = client.post(
            "/api/email-verification/confirm", json={"code": match.group(1)}
        )
        self.assertEqual(verified.status_code, 200)

    def payload(self, slug: str = "first-exercise", source: str = "li $v0, 1") -> dict:
        return {
            "slug": slug,
            "title": "First exercise",
            "description": "Registers and syscalls",
            "instructions": "Run it and explain the result.",
            "tags": ["mips", "registers"],
            "language": "en",
            "difficulty": "beginner",
            "kind": "exercise",
            "license": "CC BY 4.0",
            "status": "published",
            "webmarsVersion": "0.6.0",
            "project": {"files": {"main.asm": source}, "activeFile": "main.asm"},
        }

    def publish(self, **changes):
        payload = self.payload()
        payload.update(changes)
        return self.client.post("/api/publications", json=payload)

    def test_anonymous_users_can_browse_and_open_a_publication(self) -> None:
        created = self.publish()
        anonymous = self.app.test_client()
        listing = anonymous.get("/api/publications")
        opened = anonymous.get("/api/publications/first-exercise")

        self.assertEqual(created.status_code, 201)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.get_json()["publications"]), 1)
        publication = opened.get_json()["publication"]
        self.assertEqual(publication["project"]["files"][0]["source"], "li $v0, 1")
        self.assertEqual(publication["author"], {"username": "alice"})
        self.assertNotIn("email", str(publication).lower())

    def test_public_listing_reports_an_exact_next_page(self) -> None:
        self.assertEqual(self.publish().status_code, 201)
        self.assertEqual(self.publish(slug="second-exercise").status_code, 201)

        first = self.client.get("/api/publications?limit=1&offset=0").get_json()
        second = self.client.get("/api/publications?limit=1&offset=1").get_json()

        self.assertEqual(len(first["publications"]), 1)
        self.assertEqual(len(second["publications"]), 1)
        self.assertTrue(first["hasMore"])
        self.assertFalse(second["hasMore"])

    def test_publication_fields_refuse_implicit_object_to_string_coercion(self) -> None:
        invalid_title = self.payload(slug="invalid-title")
        invalid_title["title"] = {"unexpected": "object"}
        invalid_source = self.payload(slug="invalid-source")
        invalid_source["project"] = {"files": {"main.asm": ["not", "text"]}}

        self.assertEqual(
            self.client.post("/api/publications", json=invalid_title).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/publications", json=invalid_source).status_code,
            400,
        )

    def test_oversized_revision_ids_are_clean_not_found_responses(self) -> None:
        self.assertEqual(self.publish().status_code, 201)
        huge_revision = "9" * 200
        response = self.client.get(
            f"/api/publications/first-exercise/revisions/{huge_revision}"
        )
        self.assertEqual(response.status_code, 404)

    def test_webmars_public_interchange_array_is_accepted_for_create_and_revision(self) -> None:
        payload = self.payload(slug="browser-format")
        payload["project"] = {
            "kind": "webmars-public-project",
            "version": 1,
            "name": "Browser format",
            "description": "The exact shape emitted by webMARS.",
            "files": [
                {"path": "src/main.s", "source": "li $v0, 1"},
                {"path": "src/helper.s", "source": "jr $ra"},
            ],
            "activeFilePath": "src/main.s",
        }

        created = self.client.post("/api/publications", json=payload)
        revision = self.client.post(
            "/api/publications/browser-format/revisions",
            json={
                "webmarsVersion": "0.6.0",
                "project": {
                    **payload["project"],
                    "files": [{"path": "src/main.s", "source": "li $v0, 10"}],
                    "activeFilePath": "src/main.s",
                },
            },
        )

        self.assertEqual(created.status_code, 201, created.get_json())
        self.assertEqual(revision.status_code, 201, revision.get_json())
        opened = self.client.get("/api/publications/browser-format/revisions/2").get_json()["publication"]
        self.assertEqual(opened["project"]["files"][0]["source"], "li $v0, 10")
        self.assertEqual(opened["project"]["activeFilePath"], "src/main.s")

    def test_the_first_claim_wins_even_without_case_distinction(self) -> None:
        self.assertEqual(self.publish().status_code, 201)
        bob = self.app.test_client()
        self.register_and_verify(bob, "bob", "bob@example.com")
        conflict = bob.post(
            "/api/publications", json=self.payload(slug="FIRST-EXERCISE")
        )

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["code"], "slug_taken")

    def test_old_revisions_are_immutable_and_can_be_pinned(self) -> None:
        self.assertEqual(self.publish().status_code, 201)
        revision = self.client.post(
            "/api/publications/first-exercise/revisions",
            json={
                "project": {"files": {"main.asm": "li $v0, 2"}},
                "webmarsVersion": "0.6.1",
            },
        )
        changed = self.client.put(
            "/api/publications/first-exercise",
            json={"title": "Updated title", "description": "Updated description"},
        )
        first = self.client.get("/api/publications/first-exercise/revisions/1")
        latest = self.client.get("/api/publications/first-exercise")

        self.assertEqual(revision.status_code, 201)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(first.get_json()["publication"]["project"]["files"][0]["source"], "li $v0, 1")
        self.assertEqual(first.get_json()["publication"]["title"], "First exercise")
        self.assertEqual(first.get_json()["publication"]["latestRevision"], 1)
        self.assertEqual(latest.get_json()["publication"]["project"]["files"][0]["source"], "li $v0, 2")
        self.assertEqual(latest.get_json()["publication"]["title"], "Updated title")
        self.assertIn("immutable", first.headers["Cache-Control"])

    def test_archiving_hides_the_item_but_never_releases_the_name(self) -> None:
        self.assertEqual(self.publish().status_code, 201)
        archived = self.client.delete("/api/publications/first-exercise")
        anonymous = self.app.test_client()

        self.assertEqual(archived.status_code, 200)
        self.assertEqual(anonymous.get("/api/publications/first-exercise").status_code, 404)
        pinned = anonymous.get("/api/publications/first-exercise/revisions/1")
        self.assertEqual(pinned.status_code, 200)
        self.assertEqual(pinned.get_json()["publication"]["status"], "published")
        self.assertIn("immutable", pinned.headers["Cache-Control"])
        self.assertEqual(self.publish().status_code, 409)

    def test_owner_can_restore_an_archive_without_changing_its_public_name(self) -> None:
        self.assertEqual(self.publish().status_code, 201)
        self.assertEqual(self.client.delete("/api/publications/first-exercise").status_code, 200)

        restored = self.client.put(
            "/api/publications/first-exercise",
            json={"status": "published", "title": "Restored exercise"},
        )
        latest = self.client.get("/api/publications/first-exercise")

        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.get_json()["publication"]["slug"], "first-exercise")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.get_json()["publication"]["title"], "Restored exercise")
        self.assertEqual(latest.get_json()["publication"]["latestRevision"], 1)

    def test_owner_listing_is_paginated_and_edit_rejects_non_object_json(self) -> None:
        self.assertEqual(self.publish().status_code, 201)
        self.assertEqual(self.publish(slug="second-exercise").status_code, 201)

        first_page = self.client.get("/api/publications/mine?limit=1&offset=0")
        second_page = self.client.get("/api/publications/mine?limit=1&offset=1")
        invalid_edit = self.client.put(
            "/api/publications/first-exercise",
            json=[{"status": "archived"}],
        )
        invalid_revision = self.client.post(
            "/api/publications/first-exercise/revisions",
            json=["project"],
        )

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(len(first_page.get_json()["publications"]), 1)
        self.assertEqual(len(second_page.get_json()["publications"]), 1)
        self.assertTrue(first_page.get_json()["hasMore"])
        self.assertFalse(second_page.get_json()["hasMore"])
        self.assertNotEqual(
            first_page.get_json()["publications"][0]["slug"],
            second_page.get_json()["publications"][0]["slug"],
        )
        self.assertEqual(invalid_edit.status_code, 400)
        self.assertEqual(invalid_edit.get_json()["error"], "A JSON object is required.")
        self.assertEqual(invalid_revision.status_code, 400)
        self.assertEqual(invalid_revision.get_json()["error"], "A JSON object is required.")

    def test_unverified_accounts_cannot_publish(self) -> None:
        unverified = self.app.test_client()
        response = unverified.post(
            "/api/register",
            json={
                "username": "unverified",
                "email": "unverified@example.com",
                "password": "secure password",
            },
        )
        self.assertEqual(response.status_code, 201)
        rejected = unverified.post("/api/publications", json=self.payload("new-name"))
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.get_json()["code"], "email_not_verified")

    def test_public_snapshots_strip_cloud_and_interface_state(self) -> None:
        payload = self.payload()
        payload["project"].update(
            {
                "settings": {"cloudApiBase": "https://private.invalid/api"},
                "layout": {"windows": [{"id": "private"}]},
                "states": [{"secret": True}],
                "updatedAt": 123,
            }
        )
        response = self.client.post("/api/publications", json=payload)
        stored = response.get_json()["publication"]["project"]
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            set(stored), {"kind", "version", "name", "description", "files", "activeFilePath"}
        )
        self.assertNotIn("private.invalid", str(stored))

    def test_unsafe_paths_and_unsupported_files_are_refused(self) -> None:
        traversal = self.payload(slug="unsafe-one")
        traversal["project"] = {"files": {"../secret.s": "nop"}}
        binary = self.payload(slug="unsafe-two")
        binary["project"] = {"files": {"payload.exe": "MZ"}}
        self.assertEqual(self.client.post("/api/publications", json=traversal).status_code, 400)
        self.assertEqual(self.client.post("/api/publications", json=binary).status_code, 400)


if __name__ == "__main__":
    unittest.main()
