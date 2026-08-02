from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from db import get_db


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record_failed_login(client_address: str) -> None:
    get_db().execute(
        "INSERT INTO login_attempts (client_address, created_at) VALUES (?, ?)",
        (client_address, utc_now()),
    )
    get_db().commit()


def count_recent_failed_logins(client_address: str, window_seconds: int) -> int:
    since = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=window_seconds)
    ).isoformat()
    row = get_db().execute(
        """
        SELECT COUNT(*) AS total
        FROM login_attempts
        WHERE client_address = ? AND created_at >= ?
        """,
        (client_address, since),
    ).fetchone()
    return int(row["total"]) if row else 0


def clear_failed_logins(client_address: str) -> None:
    get_db().execute("DELETE FROM login_attempts WHERE client_address = ?", (client_address,))
    get_db().commit()


def purge_expired_login_attempts(window_seconds: int) -> None:
    # Housekeeping on the login path keeps the table from growing without bound
    # without needing a scheduled job.
    since = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=window_seconds)
    ).isoformat()
    get_db().execute("DELETE FROM login_attempts WHERE created_at < ?", (since,))
    get_db().commit()


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


def project_etag(name: str, project_json: str) -> str:
    # Derived from the stored content rather than from updated_at, which only has
    # second resolution: two saves inside the same second would otherwise share a
    # version token and one could overwrite the other unnoticed.
    digest = hashlib.sha256()
    digest.update(str(name).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(project_json).encode("utf-8"))
    return digest.hexdigest()[:32]


def _project_dict(row: Any, *, include_document: bool = False, include_etag: bool = False) -> dict[str, Any] | None:
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
    if include_document or include_etag:
        project["etag"] = project_etag(row["name"], row["project_json"])
    return project


def get_project_etag(user_id: int, project_id: int) -> str | None:
    row = get_db().execute(
        "SELECT name, project_json FROM projects WHERE id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return project_etag(row["name"], row["project_json"])


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


def list_projects_for_user(user_id: int, *, include_documents: bool = False) -> list[dict[str, Any]]:
    rows = get_db().execute(
        """
        SELECT id, user_id, name, project_json, created_at, updated_at
        FROM projects
        WHERE user_id = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (user_id,),
    ).fetchall()
    return [
        _project_dict(row, include_document=include_documents, include_etag=True)
        for row in rows
        if row is not None
    ]


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
    expected_etag: str,
) -> tuple[str, dict[str, Any] | None]:
    existing = get_db().execute(
        "SELECT name, project_json FROM projects WHERE id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchone()
    if existing is None:
        return "missing", None
    current_etag = project_etag(existing["name"], existing["project_json"])
    if expected_etag != "*" and expected_etag != current_etag:
        return "stale", get_project(user_id, project_id)
    next_name = name or existing["name"]
    next_payload = json.dumps(project, ensure_ascii=False) if project is not None else existing["project_json"]
    if expected_etag == "*":
        cursor = get_db().execute(
            """
            UPDATE projects SET name = ?, project_json = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (next_name, next_payload, utc_now(), project_id, user_id),
        )
    else:
        # Compare the exact row that produced the ETag in the UPDATE itself. A
        # second writer changing it after our SELECT makes rowcount zero instead
        # of slipping through a check-then-write race.
        cursor = get_db().execute(
            """
            UPDATE projects SET name = ?, project_json = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND name = ? AND project_json = ?
            """,
            (
                next_name,
                next_payload,
                utc_now(),
                project_id,
                user_id,
                existing["name"],
                existing["project_json"],
            ),
        )
    if cursor.rowcount != 1:
        get_db().rollback()
        return "stale", get_project(user_id, project_id)
    get_db().commit()
    return "updated", get_project(user_id, project_id)


def delete_project(user_id: int, project_id: int, *, expected_etag: str) -> tuple[str, dict[str, Any] | None]:
    existing = get_db().execute(
        "SELECT name, project_json FROM projects WHERE id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchone()
    if existing is None:
        return "missing", None
    current_etag = project_etag(existing["name"], existing["project_json"])
    if expected_etag != "*" and expected_etag != current_etag:
        return "stale", get_project(user_id, project_id)
    if expected_etag == "*":
        cursor = get_db().execute(
            "DELETE FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        )
    else:
        cursor = get_db().execute(
            """
            DELETE FROM projects
            WHERE id = ? AND user_id = ? AND name = ? AND project_json = ?
            """,
            (project_id, user_id, existing["name"], existing["project_json"]),
        )
    if cursor.rowcount != 1:
        get_db().rollback()
        return "stale", get_project(user_id, project_id)
    get_db().commit()
    return "deleted", None
