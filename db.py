from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, current_app, g


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = Path(current_app.config["DATABASE"])
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix" and not current_app.debug:
            os.chmod(db_path.parent, 0o700)
        connection = sqlite3.connect(db_path)
        if os.name == "posix" and not current_app.debug:
            os.chmod(db_path, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(current_app.config['DATABASE_BUSY_TIMEOUT_MS'])}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        g.db = connection
    return g.db


def close_db(_: Any = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db() -> None:
    schema_path = Path(current_app.config["SCHEMA"])
    get_db().executescript(schema_path.read_text(encoding="utf-8"))
    get_db().commit()


def _add_column_if_missing(table: str, column: str, declaration: str) -> None:
    columns = {str(row["name"]) for row in get_db().execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        get_db().execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _rename_duplicate_project_names() -> None:
    """Give every (user, name) pair a single owner before the unique index lands.

    Databases created before the constraint can hold duplicates, and a plain
    CREATE UNIQUE INDEX would fail against them. The newest row of each clash
    keeps the name and the older ones are renamed - never deleted, because the
    duplicates are somebody's work.
    """
    duplicates = get_db().execute(
        """
        SELECT user_id, name
        FROM projects
        GROUP BY user_id, name COLLATE NOCASE
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    for duplicate in duplicates:
        rows = get_db().execute(
            """
            SELECT id FROM projects
            WHERE user_id = ? AND name = ? COLLATE NOCASE
            ORDER BY updated_at DESC, id DESC
            """,
            (duplicate["user_id"], duplicate["name"]),
        ).fetchall()
        # rows[0] keeps the original name.
        for position, row in enumerate(rows[1:], start=2):
            candidate = f"{duplicate['name']} ({position})"
            attempt = 0
            while get_db().execute(
                "SELECT 1 FROM projects WHERE user_id = ? AND name = ? COLLATE NOCASE",
                (duplicate["user_id"], candidate),
            ).fetchone() is not None:
                attempt += 1
                candidate = f"{duplicate['name']} ({position}-{attempt})"
            get_db().execute(
                "UPDATE projects SET name = ? WHERE id = ?",
                (candidate, row["id"]),
            )


def _backfill_publication_revision_metadata() -> None:
    rows = get_db().execute(
        """
        SELECT r.id, r.metadata_json, p.title, p.description, p.instructions,
               p.tags_json, p.language, p.difficulty, p.kind, p.license, p.status
        FROM publication_revisions r
        JOIN publications p ON p.id = r.publication_id
        """
    ).fetchall()
    for row in rows:
        if str(row["metadata_json"] or "").strip() not in {"", "{}"}:
            continue
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except json.JSONDecodeError:
            tags = []
        snapshot = {
            "title": row["title"],
            "description": row["description"],
            "instructions": row["instructions"],
            "tags": tags if isinstance(tags, list) else [],
            "language": row["language"],
            "difficulty": row["difficulty"],
            "kind": row["kind"],
            "license": row["license"],
            "status": row["status"] if row["status"] in {"published", "unlisted"} else "unlisted",
        }
        get_db().execute(
            "UPDATE publication_revisions SET metadata_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), row["id"]),
        )


def ensure_database() -> None:
    # The schema script is written entirely with IF NOT EXISTS, so replaying it
    # on every start is both safe and how additive migrations reach a database
    # that was created before the new table or index existed.
    init_db()
    connection = get_db()
    # Gunicorn workers can import the application concurrently.  Taking one
    # SQLite write lock makes the additive migration a single-writer operation;
    # later workers wait, re-check the schema, and then perform no destructive work.
    connection.execute("BEGIN IMMEDIATE")
    try:
        # CREATE TABLE IF NOT EXISTS cannot add columns to an existing users table.
        # Keep these upgrades explicit and additive so v0.3 databases migrate in
        # place without rebuilding or dropping an account.
        _add_column_if_missing("users", "email_verified_at", "TEXT")
        _add_column_if_missing("users", "session_version", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing("projects", "client_project_id", "TEXT")
        _add_column_if_missing("projects", "deleted_at", "TEXT")
        _add_column_if_missing("oauth_flows", "context", "TEXT NOT NULL DEFAULT 'public'")
        _add_column_if_missing("oauth_flows", "attempt_id", "INTEGER")
        _add_column_if_missing(
            "publication_revisions", "metadata_json", "TEXT NOT NULL DEFAULT '{}'"
        )
        _backfill_publication_revision_metadata()
        _rename_duplicate_project_names()
        # v0.2 used a case-sensitive index while the web client keys project roots
        # case-insensitively. Rebuild it so `Demo` and `demo` cannot become two
        # ambiguous cloud projects for the same user.
        connection.execute("DROP INDEX IF EXISTS idx_projects_user_name")
        connection.execute(
            "CREATE UNIQUE INDEX idx_projects_user_name ON projects (user_id, name COLLATE NOCASE)"
        )
        connection.execute("DROP INDEX IF EXISTS idx_projects_user_updated")
        connection.execute(
            "CREATE INDEX idx_projects_user_updated ON projects (user_id, deleted_at, updated_at DESC)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_user_client_id "
            "ON projects (user_id, client_project_id) WHERE client_project_id IS NOT NULL"
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
            (4,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
            (5,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
            (6,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
            (7,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def init_app(app: Flask) -> None:
    app.teardown_appcontext(close_db)
