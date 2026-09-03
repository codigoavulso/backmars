from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("WEBMARS_USERS_ENV", "development")

from app import create_app
from db import _rename_duplicate_project_names, get_db
from models import utc_now


class ProjectSyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "test.db"
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": self.database_path,
                "SECRET_KEY": "test-secret-key",
                "SESSION_COOKIE_SECURE": False,
            }
        )
        self.client = self.app.test_client()
        self.client.post(
            "/api/register",
            json={
                "username": "sync-user",
                "email": "sync@example.com",
                "password": "correct horse battery staple",
            },
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_project(self, name: str = "demo", source: str = ".text\nmain:\n"):
        return self.client.post(
            "/api/projects",
            json={"name": name, "project": {"files": {"main.asm": source}}},
        )


class OptimisticLockingTests(ProjectSyncTestCase):
    def test_project_mutations_reject_non_object_json(self) -> None:
        created = self.client.post("/api/projects", json=["project"])
        project_id = self.create_project().get_json()["project"]["id"]
        updated = self.client.put(
            f"/api/projects/{project_id}",
            json=["project"],
            headers={"If-Match": "*"},
        )

        self.assertEqual(created.status_code, 400)
        self.assertEqual(updated.status_code, 400)
        self.assertEqual(created.get_json()["error"], "A JSON object is required.")
        self.assertEqual(updated.get_json()["error"], "A JSON object is required.")

    def test_empty_json_scalars_and_missing_json_cannot_create_projects(self) -> None:
        for payload in (None, False, 0, "", []):
            with self.subTest(payload=payload):
                response = self.client.post("/api/projects", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "A JSON object is required.")

        missing = self.client.post("/api/projects")
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.client.get("/api/projects").get_json()["projects"], [])

    def test_project_names_are_bounded_text(self) -> None:
        for invalid_name in (["array"], {"object": True}, 7, "x" * 121):
            with self.subTest(name=invalid_name):
                response = self.client.post(
                    "/api/projects",
                    json={"name": invalid_name, "project": {"files": {}}},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["field"], "name")

    def test_oversized_sqlite_ids_are_clean_not_found_responses(self) -> None:
        huge_id = "9" * 200
        self.assertEqual(self.client.get(f"/api/projects/{huge_id}").status_code, 404)
        self.assertEqual(
            self.client.put(
                f"/api/projects/{huge_id}",
                json={"name": "safe"},
                headers={"If-Match": "*"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/projects/{huge_id}", headers={"If-Match": "*"}
            ).status_code,
            404,
        )

    def test_reading_a_project_returns_a_version_token(self) -> None:
        project_id = self.create_project().get_json()["project"]["id"]

        response = self.client.get(f"/api/projects/{project_id}")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["project"]["etag"])
        self.assertEqual(
            response.headers["ETag"].strip('"'),
            response.get_json()["project"]["etag"],
        )

    def test_the_token_changes_when_the_document_changes(self) -> None:
        project_id = self.create_project().get_json()["project"]["id"]
        first = self.client.get(f"/api/projects/{project_id}").get_json()["project"]["etag"]

        self.client.put(
            f"/api/projects/{project_id}",
            json={"project": {"files": {"main.asm": "different"}}},
            headers={"If-Match": first},
        )
        second = self.client.get(f"/api/projects/{project_id}").get_json()["project"]["etag"]

        self.assertNotEqual(first, second)

    def test_a_stale_writer_is_refused_instead_of_overwriting(self) -> None:
        project_id = self.create_project().get_json()["project"]["id"]
        stale_etag = self.client.get(f"/api/projects/{project_id}").get_json()["project"]["etag"]

        # Another tab saves first.
        winning = self.client.get(f"/api/projects/{project_id}").get_json()["project"]["etag"]
        self.client.put(
            f"/api/projects/{project_id}",
            json={"project": {"files": {"main.asm": "written by the other tab"}}},
            headers={"If-Match": winning},
        )

        response = self.client.put(
            f"/api/projects/{project_id}",
            json={"project": {"files": {"main.asm": "written by the stale tab"}}},
            headers={"If-Match": f'"{stale_etag}"'},
        )

        self.assertEqual(response.status_code, 412)
        self.assertEqual(response.get_json()["code"], "stale_version")
        # The winning content is intact and comes back for merging.
        self.assertEqual(
            response.get_json()["project"]["project"]["files"]["main.asm"],
            "written by the other tab",
        )

    def test_a_current_writer_is_accepted(self) -> None:
        project_id = self.create_project().get_json()["project"]["id"]
        etag = self.client.get(f"/api/projects/{project_id}").get_json()["project"]["etag"]

        response = self.client.put(
            f"/api/projects/{project_id}",
            json={"project": {"files": {"main.asm": "fresh"}}},
            headers={"If-Match": etag},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["project"]["project"]["files"]["main.asm"],
            "fresh",
        )

    def test_the_wildcard_matches_any_existing_project(self) -> None:
        project_id = self.create_project().get_json()["project"]["id"]

        response = self.client.put(
            f"/api/projects/{project_id}",
            json={"project": {"files": {"main.asm": "wildcard"}}},
            headers={"If-Match": "*"},
        )

        self.assertEqual(response.status_code, 200)

    def test_a_caller_without_the_header_is_refused(self) -> None:
        project_id = self.create_project().get_json()["project"]["id"]

        response = self.client.put(
            f"/api/projects/{project_id}",
            json={"project": {"files": {"main.asm": "no locking"}}},
        )

        self.assertEqual(response.status_code, 428)
        self.assertEqual(response.get_json()["code"], "precondition_required")

    def test_the_project_list_can_batch_documents_and_tokens(self) -> None:
        self.create_project("first", "one")
        self.create_project("second", "two")

        response = self.client.get("/api/projects?include=documents")

        self.assertEqual(response.status_code, 200)
        projects = response.get_json()["projects"]
        self.assertEqual(len(projects), 2)
        self.assertTrue(all(project.get("etag") for project in projects))
        self.assertEqual(
            {project["name"]: project["project"]["files"]["main.asm"] for project in projects},
            {"first": "one", "second": "two"},
        )

    def test_delete_requires_the_version_it_read(self) -> None:
        project_id = self.create_project().get_json()["project"]["id"]
        etag = self.client.get(f"/api/projects/{project_id}").get_json()["project"]["etag"]

        self.assertEqual(self.client.delete(f"/api/projects/{project_id}").status_code, 428)
        self.assertEqual(
            self.client.delete(
                f"/api/projects/{project_id}", headers={"If-Match": etag}
            ).status_code,
            200,
        )


class ServerClockTests(ProjectSyncTestCase):
    def test_the_project_list_reports_the_server_clock(self) -> None:
        response = self.client.get("/api/projects")

        self.assertEqual(response.status_code, 200)
        server_time = response.get_json()["serverTime"]
        self.assertTrue(server_time.endswith("+00:00"))


class UniqueProjectNameTests(ProjectSyncTestCase):
    def test_a_second_project_cannot_reuse_a_name(self) -> None:
        self.create_project("demo")

        response = self.create_project("demo")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["field"], "name")

    def test_project_names_are_unique_without_case_distinctions(self) -> None:
        self.create_project("Demo")

        response = self.create_project("demo")

        self.assertEqual(response.status_code, 409)

    def test_renaming_onto_an_existing_name_is_refused(self) -> None:
        self.create_project("first")
        second_id = self.create_project("second").get_json()["project"]["id"]
        etag = self.client.get(f"/api/projects/{second_id}").get_json()["project"]["etag"]

        response = self.client.put(
            f"/api/projects/{second_id}",
            json={"name": "first"},
            headers={"If-Match": etag},
        )

        self.assertEqual(response.status_code, 409)

    def test_saving_a_project_under_its_own_name_still_works(self) -> None:
        project_id = self.create_project("demo").get_json()["project"]["id"]

        etag = self.client.get(f"/api/projects/{project_id}").get_json()["project"]["etag"]
        response = self.client.put(
            f"/api/projects/{project_id}",
            json={"name": "demo", "project": {"files": {"main.asm": "still fine"}}},
            headers={"If-Match": etag},
        )

        self.assertEqual(response.status_code, 200)


class DuplicateNameMigrationTests(unittest.TestCase):
    """A database written before the constraint may already hold duplicates."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "legacy.db"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def seed_legacy_database(self) -> None:
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                project_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        now = utc_now()
        connection.execute(
            "INSERT INTO users (id, username, email, password_hash, created_at) VALUES (1,'a','a@x','h',?)",
            (now,),
        )
        for index, updated_at in enumerate(
            ["2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00"]
        ):
            legacy_name = "Clash" if index == 0 else "clash"
            connection.execute(
                """
                INSERT INTO projects (user_id, name, project_json, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?)
                """,
                (legacy_name, f'{{"files":{{"main.asm":"copy {index}"}}}}', now, updated_at),
            )
        connection.commit()
        connection.close()

    def test_startup_renames_duplicates_and_keeps_every_row(self) -> None:
        self.seed_legacy_database()

        app = create_app(
            {
                "TESTING": True,
                "DATABASE": self.database_path,
                "SECRET_KEY": "test-secret-key",
            }
        )

        with app.app_context():
            rows = get_db().execute(
                "SELECT name, updated_at FROM projects WHERE user_id = 1 ORDER BY updated_at DESC"
            ).fetchall()
            user = get_db().execute(
                "SELECT username, email, email_verified_at, session_version FROM users WHERE id = 1"
            ).fetchone()
            versions = {
                row["version"]
                for row in get_db().execute("SELECT version FROM schema_migrations").fetchall()
            }
            tables = {
                row["name"]
                for row in get_db().execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

        self.assertEqual(len(rows), 3, "no project may be dropped by the migration")
        names = [row["name"] for row in rows]
        self.assertEqual(len(set(names)), 3, "every name must be unique after the migration")
        # The most recently updated row keeps the original name.
        self.assertEqual(names[0], "clash")
        self.assertEqual((user["username"], user["email"]), ("a", "a@x"))
        self.assertIsNone(user["email_verified_at"])
        self.assertEqual(user["session_version"], 0)
        self.assertTrue({4, 5}.issubset(versions))
        self.assertTrue(
            {"account_codes", "registration_attempts", "publications", "publication_revisions"}.issubset(tables)
        )

    def test_the_migration_is_idempotent(self) -> None:
        self.seed_legacy_database()
        overrides = {
            "TESTING": True,
            "DATABASE": self.database_path,
            "SECRET_KEY": "test-secret-key",
        }

        create_app(overrides)
        app = create_app(overrides)

        with app.app_context():
            names = [
                row["name"]
                for row in get_db().execute("SELECT name FROM projects WHERE user_id = 1").fetchall()
            ]

        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3)

    def test_the_helper_leaves_an_already_clean_table_alone(self) -> None:
        self.seed_legacy_database()
        app = create_app(
            {
                "TESTING": True,
                "DATABASE": self.database_path,
                "SECRET_KEY": "test-secret-key",
            }
        )

        with app.app_context():
            before = [row["name"] for row in get_db().execute("SELECT name FROM projects").fetchall()]
            _rename_duplicate_project_names()
            after = [row["name"] for row in get_db().execute("SELECT name FROM projects").fetchall()]

        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
