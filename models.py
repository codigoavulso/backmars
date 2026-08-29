from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

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
        "emailVerified": bool(row["email_verified_at"]),
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
        "SELECT id, username, email, created_at, email_verified_at FROM users WHERE id = ?",
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


def record_registration_attempt(client_address: str) -> None:
    get_db().execute(
        "INSERT INTO registration_attempts (client_address, created_at) VALUES (?, ?)",
        (client_address, utc_now()),
    )
    get_db().commit()


def count_recent_registration_attempts(client_address: str, window_seconds: int) -> int:
    since = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=window_seconds)
    ).isoformat()
    row = get_db().execute(
        """
        SELECT COUNT(*) AS total
        FROM registration_attempts
        WHERE client_address = ? AND created_at >= ?
        """,
        (client_address, since),
    ).fetchone()
    return int(row["total"]) if row else 0


def purge_expired_registration_attempts(window_seconds: int) -> None:
    since = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=window_seconds)
    ).isoformat()
    get_db().execute("DELETE FROM registration_attempts WHERE created_at < ?", (since,))
    get_db().commit()


def get_user_session_version(user_id: int) -> int | None:
    row = get_db().execute(
        "SELECT session_version FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return int(row["session_version"]) if row is not None else None


def replace_user_password_and_revoke_sessions(user_id: int, password_hash: str) -> int:
    get_db().execute(
        """
        UPDATE users
        SET password_hash = ?, session_version = session_version + 1
        WHERE id = ?
        """,
        (password_hash, user_id),
    )
    get_db().commit()
    return int(get_user_session_version(user_id) or 0)


def mark_email_verified(user_id: int) -> dict[str, Any] | None:
    get_db().execute(
        "UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?",
        (utc_now(), user_id),
    )
    get_db().commit()
    return get_user_by_id(user_id)


def generate_account_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def account_code_digest(code: str, secret: str) -> str:
    return hmac.new(
        str(secret).encode("utf-8"),
        str(code).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def count_recent_account_codes(*, purpose: str, since_seconds: int, user_id: int | None = None, address: str | None = None) -> int:
    since = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=since_seconds)
    ).isoformat()
    clauses = ["purpose = ?", "created_at >= ?"]
    values: list[Any] = [purpose, since]
    if user_id is not None:
        clauses.append("user_id = ?")
        values.append(user_id)
    if address is not None:
        clauses.append("request_address = ?")
        values.append(address)
    row = get_db().execute(
        f"SELECT COUNT(*) AS total FROM account_codes WHERE {' AND '.join(clauses)}",
        values,
    ).fetchone()
    return int(row["total"]) if row else 0


def create_account_code(user_id: int, purpose: str, code_hash: str, ttl_seconds: int, address: str) -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    get_db().execute(
        """
        UPDATE account_codes SET consumed_at = ?
        WHERE user_id = ? AND purpose = ? AND consumed_at IS NULL
        """,
        (now.isoformat(), user_id, purpose),
    )
    cursor = get_db().execute(
        """
        INSERT INTO account_codes
            (user_id, purpose, code_hash, expires_at, attempts, created_at, consumed_at, request_address)
        VALUES (?, ?, ?, ?, 0, ?, NULL, ?)
        """,
        (
            user_id,
            purpose,
            code_hash,
            (now + timedelta(seconds=ttl_seconds)).isoformat(),
            now.isoformat(),
            address,
        ),
    )
    get_db().commit()
    return int(cursor.lastrowid)


def consume_account_code(code_id: int) -> None:
    get_db().execute(
        "UPDATE account_codes SET consumed_at = COALESCE(consumed_at, ?) WHERE id = ?",
        (utc_now(), code_id),
    )
    get_db().commit()


def verify_and_consume_account_code(
    user_id: int,
    purpose: str,
    code: str,
    secret: str,
    max_attempts: int,
) -> bool:
    connection = get_db()
    connection.execute("BEGIN IMMEDIATE")
    valid, code_id, now = _evaluate_account_code(
        connection, user_id, purpose, code, secret, max_attempts
    )
    if valid and code_id is not None:
        connection.execute(
            "UPDATE account_codes SET consumed_at = ? WHERE id = ?",
            (now, code_id),
        )
    connection.commit()
    return valid


def _evaluate_account_code(
    connection: Any,
    user_id: int,
    purpose: str,
    code: str,
    secret: str,
    max_attempts: int,
) -> tuple[bool, int | None, str]:
    row = connection.execute(
        """
        SELECT id, code_hash, expires_at, attempts
        FROM account_codes
        WHERE user_id = ? AND purpose = ? AND consumed_at IS NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (user_id, purpose),
    ).fetchone()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_text = now.isoformat()
    if row is None:
        return False, None, now_text
    attempts = int(row["attempts"] or 0)
    expires = datetime.fromisoformat(str(row["expires_at"]))
    if attempts >= max_attempts or expires < now:
        connection.execute(
            "UPDATE account_codes SET consumed_at = ? WHERE id = ?",
            (now_text, row["id"]),
        )
        return False, int(row["id"]), now_text
    valid = hmac.compare_digest(str(row["code_hash"]), account_code_digest(code, secret))
    if not valid:
        next_attempts = attempts + 1
        connection.execute(
            "UPDATE account_codes SET attempts = ?, consumed_at = ? WHERE id = ?",
            (next_attempts, now_text if next_attempts >= max_attempts else None, row["id"]),
        )
    return valid, int(row["id"]), now_text


def reset_password_with_account_code(
    user_id: int,
    code: str,
    secret: str,
    max_attempts: int,
    password_hash_factory: Callable[[], str],
) -> int | None:
    connection = get_db()
    connection.execute("BEGIN IMMEDIATE")
    try:
        valid, code_id, now = _evaluate_account_code(
            connection, user_id, "password_reset", code, secret, max_attempts
        )
        if not valid or code_id is None:
            connection.commit()
            return None
        # Password hashing is intentionally delayed until after the cheap,
        # attempt-limited code check. Keeping the write transaction open makes
        # code consumption and the password update atomic; valid resets are
        # rare, while invalid requests never receive expensive scrypt work.
        password_hash = password_hash_factory()
        connection.execute(
            "UPDATE account_codes SET consumed_at = ? WHERE id = ?",
            (now, code_id),
        )
        cursor = connection.execute(
            """
            UPDATE users
            SET password_hash = ?, session_version = session_version + 1
            WHERE id = ?
            """,
            (password_hash, user_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        row = connection.execute(
            "SELECT session_version FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        connection.commit()
        return int(row["session_version"]) if row is not None else None
    except Exception:
        connection.rollback()
        raise


def verify_email_with_account_code(
    user_id: int,
    code: str,
    secret: str,
    max_attempts: int,
) -> bool:
    connection = get_db()
    connection.execute("BEGIN IMMEDIATE")
    try:
        valid, code_id, now = _evaluate_account_code(
            connection, user_id, "email_verification", code, secret, max_attempts
        )
        if not valid or code_id is None:
            connection.commit()
            return False
        connection.execute(
            "UPDATE account_codes SET consumed_at = ? WHERE id = ?",
            (now, code_id),
        )
        cursor = connection.execute(
            "UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?",
            (now, user_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


def purge_old_account_codes(retention_seconds: int = 86400) -> None:
    cutoff = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=retention_seconds)
    ).isoformat()
    get_db().execute("DELETE FROM account_codes WHERE created_at < ?", (cutoff,))
    get_db().commit()


def _publication_metadata_snapshot(source: Any) -> dict[str, Any]:
    if hasattr(source, "keys"):
        record = {key: source[key] for key in source.keys()}
    else:
        record = dict(source or {})
    raw_tags = record.get("tags", record.get("tags_json", []))
    if isinstance(raw_tags, str):
        try:
            raw_tags = json.loads(raw_tags or "[]")
        except json.JSONDecodeError:
            raw_tags = []
    status = str(record.get("status", "unlisted"))
    return {
        "title": str(record.get("title", "")),
        "description": str(record.get("description", "")),
        "instructions": str(record.get("instructions", "")),
        "tags": raw_tags if isinstance(raw_tags, list) else [],
        "language": str(record.get("language", "en")),
        "difficulty": str(record.get("difficulty", "beginner")),
        "kind": str(record.get("kind", "example")),
        "license": str(record.get("license", "unspecified")),
        "status": status if status in {"published", "unlisted"} else "unlisted",
    }


def _publication_dict(
    row: Any,
    *,
    include_project: bool = False,
    immutable_revision: bool = False,
) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        tags = json.loads(row["tags_json"] or "[]")
    except json.JSONDecodeError:
        tags = []
    metadata = {
        "title": row["title"],
        "description": row["description"],
        "instructions": row["instructions"],
        "tags": tags if isinstance(tags, list) else [],
        "language": row["language"],
        "difficulty": row["difficulty"],
        "kind": row["kind"],
        "license": row["license"],
        "status": row["status"],
    }
    if immutable_revision and "metadata_json" in row.keys():
        try:
            snapshot = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            snapshot = {}
        if isinstance(snapshot, dict) and snapshot:
            metadata.update(_publication_metadata_snapshot(snapshot))
    revision_number = (
        int(row["revision"])
        if immutable_revision and "revision" in row.keys() and row["revision"] is not None
        else int(row["latest_revision"] or 0)
    )
    publication = {
        "slug": row["slug"],
        **metadata,
        "latestRevision": revision_number,
        "createdAt": row["created_at"],
        "updatedAt": (
            row["revision_created_at"]
            if immutable_revision and "revision_created_at" in row.keys()
            else row["updated_at"]
        ),
        "author": {"username": row["username"]},
    }
    if "revision" in row.keys() and row["revision"] is not None:
        publication["revision"] = int(row["revision"])
        publication["webmarsVersion"] = str(row["webmars_version"] or "")
        publication["revisionCreatedAt"] = row["revision_created_at"]
        if include_project:
            publication["project"] = _parse_project_json(row["project_json"])
    return publication


def list_publications(*, query: str = "", limit: int = 50, offset: int = 0, owner_id: int | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if owner_id is None:
        clauses.append("p.status = 'published'")
    else:
        clauses.append("p.owner_id = ?")
        values.append(owner_id)
    normalized_query = str(query or "").strip().lower()
    if normalized_query:
        clauses.append(
            "(LOWER(p.slug) LIKE ? OR LOWER(p.title) LIKE ? OR LOWER(p.description) LIKE ? OR LOWER(p.tags_json) LIKE ?)"
        )
        pattern = f"%{normalized_query}%"
        values.extend([pattern, pattern, pattern, pattern])
    values.extend([max(1, limit), max(0, offset)])
    rows = get_db().execute(
        f"""
        SELECT p.*, u.username
        FROM publications p
        JOIN users u ON u.id = p.owner_id
        WHERE {' AND '.join(clauses)}
        ORDER BY p.updated_at DESC, p.id DESC
        LIMIT ? OFFSET ?
        """,
        values,
    ).fetchall()
    return [publication for row in rows if (publication := _publication_dict(row))]


def get_publication(slug: str, revision: int | None = None, *, include_archived_for_owner: int | None = None) -> dict[str, Any] | None:
    if include_archived_for_owner is not None:
        status_clause = ""
    elif revision is not None:
        # Archiving removes the mutable latest URL and catalogue listing, but a
        # numbered revision remains a durable teaching reference. A future
        # administrator-only `removed` state is the explicit takedown escape.
        status_clause = "AND p.status != 'removed'"
    else:
        status_clause = "AND p.status IN ('published', 'unlisted')"
    owner_clause = "AND p.owner_id = ?" if include_archived_for_owner is not None else ""
    values: list[Any] = [slug]
    if include_archived_for_owner is not None:
        values.append(include_archived_for_owner)
    values.append(revision)
    row = get_db().execute(
        f"""
        SELECT p.*, u.username, r.revision, r.project_json, r.metadata_json, r.webmars_version,
               r.created_at AS revision_created_at
        FROM publications p
        JOIN users u ON u.id = p.owner_id
        JOIN publication_revisions r ON r.publication_id = p.id
        WHERE p.slug = ? COLLATE NOCASE {status_clause} {owner_clause}
          AND r.revision = COALESCE(?, p.latest_revision)
        LIMIT 1
        """,
        values,
    ).fetchone()
    return _publication_dict(
        row,
        include_project=True,
        immutable_revision=revision is not None,
    )


def create_publication(owner_id: int, metadata: dict[str, Any], project: dict[str, Any], webmars_version: str) -> dict[str, Any]:
    connection = get_db()
    now = utc_now()
    connection.execute("BEGIN IMMEDIATE")
    cursor = connection.execute(
        """
        INSERT INTO publications
            (owner_id, slug, title, description, instructions, tags_json, language,
             difficulty, kind, license, status, latest_revision, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            owner_id, metadata["slug"], metadata["title"], metadata["description"],
            metadata["instructions"], json.dumps(metadata["tags"], ensure_ascii=False),
            metadata["language"], metadata["difficulty"], metadata["kind"],
            metadata["license"], metadata["status"], now, now,
        ),
    )
    publication_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO publication_revisions
            (publication_id, revision, project_json, metadata_json, webmars_version, created_at)
        VALUES (?, 1, ?, ?, ?, ?)
        """,
        (
            publication_id,
            json.dumps(project, ensure_ascii=False),
            json.dumps(_publication_metadata_snapshot(metadata), ensure_ascii=False),
            webmars_version,
            now,
        ),
    )
    connection.commit()
    return get_publication(metadata["slug"], include_archived_for_owner=owner_id) or {}


def create_publication_revision(owner_id: int, slug: str, project: dict[str, Any], webmars_version: str) -> tuple[str, dict[str, Any] | None]:
    connection = get_db()
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        "SELECT * FROM publications WHERE slug = ? COLLATE NOCASE",
        (slug,),
    ).fetchone()
    if row is None:
        connection.rollback()
        return "missing", None
    if int(row["owner_id"]) != int(owner_id):
        connection.rollback()
        return "forbidden", None
    if str(row["status"]) not in {"published", "unlisted"}:
        connection.rollback()
        return "archived", None
    next_revision = int(row["latest_revision"] or 0) + 1
    now = utc_now()
    connection.execute(
        """
        INSERT INTO publication_revisions
            (publication_id, revision, project_json, metadata_json, webmars_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"],
            next_revision,
            json.dumps(project, ensure_ascii=False),
            json.dumps(_publication_metadata_snapshot(row), ensure_ascii=False),
            webmars_version,
            now,
        ),
    )
    connection.execute(
        "UPDATE publications SET latest_revision = ?, updated_at = ? WHERE id = ?",
        (next_revision, now, row["id"]),
    )
    connection.commit()
    return "created", get_publication(slug, next_revision, include_archived_for_owner=owner_id)


def update_publication_metadata(owner_id: int, slug: str, metadata: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    existing = get_db().execute(
        "SELECT id, owner_id FROM publications WHERE slug = ? COLLATE NOCASE",
        (slug,),
    ).fetchone()
    if existing is None:
        return "missing", None
    if int(existing["owner_id"]) != int(owner_id):
        return "forbidden", None
    get_db().execute(
        """
        UPDATE publications
        SET title = ?, description = ?, instructions = ?, tags_json = ?, language = ?,
            difficulty = ?, kind = ?, license = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            metadata["title"], metadata["description"], metadata["instructions"],
            json.dumps(metadata["tags"], ensure_ascii=False), metadata["language"],
            metadata["difficulty"], metadata["kind"], metadata["license"],
            metadata["status"], utc_now(), existing["id"],
        ),
    )
    get_db().commit()
    return "updated", get_publication(slug, include_archived_for_owner=owner_id)


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
