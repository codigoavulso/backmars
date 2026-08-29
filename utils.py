from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable

import bcrypt
from flask import jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

PREFERRED_PASSWORD_METHOD = "scrypt"
LEGACY_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def hash_password(password: str) -> str:
    return generate_password_hash(password, method=PREFERRED_PASSWORD_METHOD)


def verify_password(password: str, password_hash: str) -> bool:
    normalized_hash = str(password_hash or "")
    if not normalized_hash:
        return False
    if is_legacy_bcrypt_hash(normalized_hash):
        try:
            return bcrypt.checkpw(
                str(password).encode("utf-8"),
                normalized_hash.encode("utf-8"),
            )
        except ValueError:
            return False
    try:
        return check_password_hash(normalized_hash, password)
    except ValueError:
        return False


def is_legacy_bcrypt_hash(password_hash: str) -> bool:
    normalized_hash = str(password_hash or "")
    return normalized_hash.startswith(LEGACY_BCRYPT_PREFIXES)


def password_hash_needs_upgrade(password_hash: str) -> bool:
    normalized_hash = str(password_hash or "")
    if not normalized_hash:
        return True
    if is_legacy_bcrypt_hash(normalized_hash):
        return True
    return not normalized_hash.startswith(f"{PREFERRED_PASSWORD_METHOD}:")


def current_user_id() -> int | None:
    value = session.get("user_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def session_is_current() -> bool:
    user_id = current_user_id()
    if user_id is None:
        return False
    # Lazy import avoids making models -> utils -> models a module cycle.
    from models import get_user_session_version

    stored_version = get_user_session_version(user_id)
    if stored_version is None:
        return False
    try:
        cookie_version = int(session.get("session_version", 0) or 0)
    except (TypeError, ValueError):
        return False
    return cookie_version == stored_version


def client_address() -> str:
    # ProxyFix has already rewritten remote_addr when a trusted proxy count is
    # configured, so reading X-Forwarded-For here would only add a spoofable path.
    return str(request.remote_addr or "unknown")


def json_error(message: str, status: int = 400, **extra: Any) -> tuple[Any, int]:
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def coerce_project_document(payload: Any, *, allow_file_list: bool = False) -> dict[str, Any]:
    if payload is None:
        return {"files": {}, "settings": {}}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Project must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Project must be a JSON object.")
    if "files" in payload and not isinstance(payload["files"], dict):
        if not (allow_file_list and isinstance(payload["files"], list)):
            expected = "an object or array" if allow_file_list else "an object"
            raise ValueError(f"Project field 'files' must be {expected}.")
    return payload


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not session_is_current():
            session.clear()
            return json_error("Authentication required.", 401)
        return view(*args, **kwargs)

    return wrapped
