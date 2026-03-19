from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from db import get_db


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_project_json(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _user_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "createdAt": row["created_at"],
    }


def _project_dict(row: Any, *, include_document: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None
    project = {
        "id": row["id"],
        "userId": row["user_id"],
        "name": row["name"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_document:
        project["project"] = _parse_project_json(row["project_json"])
    return project


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    row = get_db().execute(
        "SELECT id, username, email, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return _user_dict(row)


def get_user_by_username(username: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def create_user(username: str, email: str, password_hash: str) -> dict[str, Any]:
    now = utc_now()
    cursor = get_db().execute(
        """
        INSERT INTO users (username, email, password_hash, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (username, email, password_hash, now),
    )
    get_db().commit()
    return get_user_by_id(int(cursor.lastrowid)) or {}


def update_user_password_hash(user_id: int, password_hash: str) -> None:
    get_db().execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id),
    )
    get_db().commit()


def list_projects_for_user(user_id: int) -> list[dict[str, Any]]:
    rows = get_db().execute(
        """
        SELECT id, user_id, name, created_at, updated_at
        FROM projects
        WHERE user_id = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (user_id,),
    ).fetchall()
    return [_project_dict(row) for row in rows if row is not None]


def get_project(user_id: int, project_id: int) -> dict[str, Any] | None:
    row = get_db().execute(
        "SELECT * FROM projects WHERE id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchone()
    return _project_dict(row, include_document=True)


def create_project(user_id: int, name: str, project: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    cursor = get_db().execute(
        """
        INSERT INTO projects (user_id, name, project_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, name, json.dumps(project, ensure_ascii=False), now, now),
    )
    get_db().commit()
    return get_project(user_id, int(cursor.lastrowid)) or {}


def update_project(
    user_id: int,
    project_id: int,
    *,
    name: str | None = None,
    project: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    existing = get_db().execute(
        "SELECT name, project_json FROM projects WHERE id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchone()
    if existing is None:
        return None
    next_name = name or existing["name"]
    next_payload = json.dumps(project, ensure_ascii=False) if project is not None else existing["project_json"]
    get_db().execute(
        """
        UPDATE projects
        SET name = ?, project_json = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (next_name, next_payload, utc_now(), project_id, user_id),
    )
    get_db().commit()
    return get_project(user_id, project_id)


def delete_project(user_id: int, project_id: int) -> bool:
    cursor = get_db().execute(
        "DELETE FROM projects WHERE id = ? AND user_id = ?",
        (project_id, user_id),
    )
    get_db().commit()
    return cursor.rowcount > 0
