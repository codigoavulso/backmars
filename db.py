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


def ensure_database() -> None:
    db_path = Path(current_app.config["DATABASE"])
    needs_schema = not db_path.exists()
    if not needs_schema:
        row = get_db().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        needs_schema = row is None
    if needs_schema:
        init_db()


def init_app(app: Flask) -> None:
    app.teardown_appcontext(close_db)
