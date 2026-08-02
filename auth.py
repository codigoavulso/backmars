from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, session

from models import (
    clear_failed_logins,
    count_recent_failed_logins,
    create_user,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    purge_expired_login_attempts,
    record_failed_login,
    update_user_password_hash,
)
from utils import client_address, current_user_id, hash_password, json_error, password_hash_needs_upgrade, verify_password

bp = Blueprint("auth", __name__)


def establish_user_session(user_id: int) -> None:
    session.clear()
    session.permanent = True
    session["user_id"] = user_id


@bp.post("/api/register")
def register() -> tuple[object, int]:
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    if not username or not email or not password:
        return json_error("username, email and password are required.")
    if get_user_by_username(username):
        return json_error("Username already exists.", 409, field="username")
    if get_user_by_email(email):
        return json_error("Email already exists.", 409, field="email")

    user = create_user(username, email, hash_password(password))
    establish_user_session(user["id"])
    return jsonify({"user": user}), 201


@bp.post("/api/login")
def login() -> tuple[object, int]:
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if not username or not password:
        return json_error("username and password are required.")

    address = client_address()
    window_seconds = int(current_app.config["LOGIN_ATTEMPT_WINDOW_SECONDS"])
    max_attempts = int(current_app.config["LOGIN_MAX_ATTEMPTS"])
    purge_expired_login_attempts(window_seconds)
    if count_recent_failed_logins(address, window_seconds) >= max_attempts:
        response, status = json_error("Too many failed sign-in attempts. Try again later.", 429)
        response.headers["Retry-After"] = str(window_seconds)
        return response, status

    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        record_failed_login(address)
        return json_error("Invalid credentials.", 401)
    if password_hash_needs_upgrade(user["password_hash"]):
        update_user_password_hash(user["id"], hash_password(password))

    clear_failed_logins(address)
    establish_user_session(user["id"])
    return jsonify({"user": get_user_by_id(user["id"])}), 200


@bp.post("/api/logout")
def logout() -> tuple[object, int]:
    session.clear()
    return jsonify({"ok": True}), 200


@bp.get("/api/me")
def me() -> tuple[object, int]:
    user_id = current_user_id()
    if user_id is None:
        return jsonify({"authenticated": False, "user": None}), 200

    user = get_user_by_id(user_id)
    if user is None:
        session.clear()
        return jsonify({"authenticated": False, "user": None}), 200

    return jsonify({"authenticated": True, "user": user}), 200
