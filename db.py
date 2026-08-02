from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, current_app, g


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = Path(current_app.config["DATABASE"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
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
    if duplicates:
        get_db().commit()


def ensure_database() -> None:
    # The schema script is written entirely with IF NOT EXISTS, so replaying it
    # on every start is both safe and how additive migrations reach a database
    # that was created before the new table or index existed.
    init_db()
    _rename_duplicate_project_names()
    # v0.2 used a case-sensitive index while the web client keys project roots
    # case-insensitively. Rebuild it so `Demo` and `demo` cannot become two
    # ambiguous cloud projects for the same user.
    get_db().execute("DROP INDEX IF EXISTS idx_projects_user_name")
    get_db().execute(
        "CREATE UNIQUE INDEX idx_projects_user_name ON projects (user_id, name COLLATE NOCASE)"
    )
    get_db().commit()


def init_app(app: Flask) -> None:
    app.teardown_appcontext(close_db)
