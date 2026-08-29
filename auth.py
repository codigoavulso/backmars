from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, current_app, jsonify, request, session

from mailer import mail_is_configured, send_account_code, send_password_changed_notice

from models import (
    consume_account_code,
    count_recent_registration_attempts,
    count_recent_account_codes,
    count_recent_failed_logins,
    create_account_code,
    create_user,
    account_code_digest,
    generate_account_code,
    get_user_by_email,
    get_user_by_id,
    get_user_session_version,
    get_user_by_username,
    purge_expired_registration_attempts,
    purge_old_account_codes,
    purge_expired_login_attempts,
    record_registration_attempt,
    record_failed_login,
    reset_password_with_account_code,
    update_user_password_hash,
    verify_email_with_account_code,
)
from utils import (
    client_address,
    current_user_id,
    hash_password,
    json_error,
    login_required,
    password_hash_needs_upgrade,
    session_is_current,
    verify_password,
)

bp = Blueprint("auth", __name__)


PASSWORD_RESET_PURPOSE = "password_reset"
EMAIL_VERIFICATION_PURPOSE = "email_verification"
_MAIL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="backmars-mail")


def establish_user_session(user_id: int, session_version: int | None = None) -> None:
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    session["session_version"] = (
        int(session_version)
        if session_version is not None
        else int(get_user_session_version(user_id) or 0)
    )


def _valid_email(email: str) -> bool:
    local, separator, domain = email.partition("@")
    return bool(local and separator and domain and "." in domain and len(email) <= 254)


def _password_error(password: str) -> str | None:
    minimum = max(8, int(current_app.config["PASSWORD_MIN_LENGTH"]))
    maximum = max(minimum, int(current_app.config["PASSWORD_MAX_LENGTH"]))
    if len(password) > maximum:
        return f"Password must contain at most {maximum} characters."
    if len(password) < minimum:
        return f"Password must contain at least {minimum} characters."
    return None


def _valid_username(username: str) -> bool:
    maximum = max(1, int(current_app.config["USERNAME_MAX_LENGTH"]))
    return bool(
        username
        and len(username) <= maximum
        and not any(ord(character) < 32 or ord(character) == 127 for character in username)
    )


def _send_account_code_and_invalidate_on_failure(
    code_id: int,
    user: dict[str, object],
    code: str,
    purpose: str,
    ttl: int,
) -> bool:
    try:
        sent = send_account_code(
            str(user["email"]),
            str(user["username"]),
            code,
            purpose,
            ttl,
        )
    except Exception:
        current_app.logger.exception("Could not send an account code")
        sent = False
    if not sent:
        # A code that never left the server must not remain valid.
        consume_account_code(code_id)
    return sent


def _send_account_code_in_background(
    app: object,
    code_id: int,
    user: dict[str, object],
    code: str,
    purpose: str,
    ttl: int,
) -> None:
    with app.app_context():
        _send_account_code_and_invalidate_on_failure(code_id, user, code, purpose, ttl)


def _issue_account_code(user: dict[str, object], purpose: str, address: str) -> bool:
    """Create and send one code without ever returning it to the client."""
    ttl = max(60, int(current_app.config["ACCOUNT_CODE_TTL_SECONDS"]))
    # Registration and verification-only installations may never exercise the
    # password-reset endpoint, so purge here where every code lifecycle starts.
    # Keep at least twice the configured lifetime so a long-lived valid code
    # cannot be removed by another user's request.
    purge_old_account_codes(max(86400, ttl * 2))
    secret = str(current_app.config["ACCOUNT_CODE_PEPPER"])
    code = generate_account_code()
    code_id = create_account_code(
        int(user["id"]),
        purpose,
        account_code_digest(code, secret),
        ttl,
        address,
    )
    if current_app.config.get("MAIL_SEND_ASYNC"):
        app = current_app._get_current_object()
        try:
            _MAIL_EXECUTOR.submit(
                _send_account_code_in_background,
                app,
                code_id,
                dict(user),
                code,
                purpose,
                ttl,
            )
            return True
        except RuntimeError:
            current_app.logger.exception("Could not queue an account code")
            consume_account_code(code_id)
            return False
    return _send_account_code_and_invalidate_on_failure(code_id, user, code, purpose, ttl)


def _code_request_is_limited(user_id: int, purpose: str, address: str) -> bool:
    window = 3600
    per_user = max(1, int(current_app.config["ACCOUNT_CODE_USER_LIMIT_PER_HOUR"]))
    per_address = max(1, int(current_app.config["ACCOUNT_CODE_ADDRESS_LIMIT_PER_HOUR"]))
    return (
        count_recent_account_codes(
            purpose=purpose, since_seconds=window, user_id=user_id
        )
        >= per_user
        or count_recent_account_codes(
            purpose=purpose, since_seconds=window, address=address
        )
        >= per_address
    )


@bp.post("/api/register")
def register() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    username = str(data.get("username", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    if not username or not email or not password:
        return json_error("username, email and password are required.")
    if not _valid_username(username):
        return json_error("Username is too long or contains control characters.", field="username")
    if not _valid_email(email):
        return json_error("A valid email address is required.", field="email")
    if password_error := _password_error(password):
        return json_error(password_error, field="password")
    if get_user_by_username(username):
        return json_error("Username already exists.", 409, field="username")
    if get_user_by_email(email):
        return json_error("Email already exists.", 409, field="email")

    address = client_address()
    window_seconds = max(60, int(current_app.config["REGISTER_ATTEMPT_WINDOW_SECONDS"]))
    maximum = max(1, int(current_app.config["REGISTER_MAX_ATTEMPTS"]))
    purge_expired_registration_attempts(window_seconds)
    if count_recent_registration_attempts(address, window_seconds) >= maximum:
        response, status = json_error("Too many registrations from this address. Try again later.", 429)
        response.headers["Retry-After"] = str(window_seconds)
        return response, status
    record_registration_attempt(address)

    try:
        user = create_user(username, email, hash_password(password))
    except sqlite3.IntegrityError:
        return json_error("Username or email already exists.", 409)
    establish_user_session(user["id"])
    verification_sent = False
    if mail_is_configured() and not _code_request_is_limited(
        int(user["id"]), EMAIL_VERIFICATION_PURPOSE, address
    ):
        verification_sent = _issue_account_code(user, EMAIL_VERIFICATION_PURPOSE, address)
    return jsonify({"user": user, "verificationEmailSent": verification_sent}), 201


@bp.post("/api/login")
def login() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    username = str(data.get("username", data.get("identifier", ""))).strip()
    password = str(data.get("password", ""))

    if not username or not password:
        return json_error("username and password are required.")
    maximum_identifier = max(254, int(current_app.config["USERNAME_MAX_LENGTH"]))
    if len(username) > maximum_identifier or len(password) > int(current_app.config["PASSWORD_MAX_LENGTH"]):
        return json_error("Invalid credentials.", 401)

    address = client_address()
    window_seconds = int(current_app.config["LOGIN_ATTEMPT_WINDOW_SECONDS"])
    max_attempts = int(current_app.config["LOGIN_MAX_ATTEMPTS"])
    purge_expired_login_attempts(window_seconds)
    if count_recent_failed_logins(address, window_seconds) >= max_attempts:
        response, status = json_error("Too many failed sign-in attempts. Try again later.", 429)
        response.headers["Retry-After"] = str(window_seconds)
        return response, status

    user = get_user_by_email(username.lower()) if "@" in username else get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        record_failed_login(address)
        return json_error("Invalid credentials.", 401)
    if password_hash_needs_upgrade(user["password_hash"]):
        update_user_password_hash(user["id"], hash_password(password))

    establish_user_session(user["id"])
    return jsonify({"user": get_user_by_id(user["id"])}), 200


@bp.post("/api/logout")
def logout() -> tuple[object, int]:
    session.clear()
    return jsonify({"ok": True}), 200


@bp.get("/api/me")
def me() -> tuple[object, int]:
    user_id = current_user_id()
    if user_id is None or not session_is_current():
        session.clear()
        return jsonify({"authenticated": False, "user": None}), 200

    user = get_user_by_id(user_id)
    if user is None:
        session.clear()
        return jsonify({"authenticated": False, "user": None}), 200

    return jsonify({"authenticated": True, "user": user}), 200


@bp.post("/api/password-reset/request")
def request_password_reset() -> tuple[object, int]:
    if not mail_is_configured():
        return json_error("Password recovery is not available on this installation.", 503)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    email = str(data.get("email", "")).strip().lower()
    if not _valid_email(email):
        return json_error("A valid email address is required.", field="email")

    generic = {
        "ok": True,
        "message": "If the address belongs to an account, a temporary code has been sent.",
    }
    address = client_address()
    user = get_user_by_email(email)
    # Do the HMAC work even for unknown addresses, reducing the most obvious
    # timing difference without creating rows or sending mail to strangers.
    account_code_digest(generate_account_code(), str(current_app.config["ACCOUNT_CODE_PEPPER"]))
    if user and not _code_request_is_limited(int(user["id"]), PASSWORD_RESET_PURPOSE, address):
        _issue_account_code(user, PASSWORD_RESET_PURPOSE, address)
    return jsonify(generic), 202


@bp.post("/api/password-reset/confirm")
def confirm_password_reset() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    email = str(data.get("email", "")).strip().lower()
    code = str(data.get("code", "")).strip()
    password = str(data.get("password", ""))
    if not _valid_email(email) or len(code) != 6 or not code.isdigit():
        return json_error("The email address or temporary code is invalid.")
    if password_error := _password_error(password):
        return json_error(password_error, field="password")

    user = get_user_by_email(email)
    next_version = (
        reset_password_with_account_code(
            int(user["id"]),
            code,
            str(current_app.config["ACCOUNT_CODE_PEPPER"]),
            max(1, int(current_app.config["ACCOUNT_CODE_MAX_ATTEMPTS"])),
            lambda: hash_password(password),
        )
        if user
        else None
    )
    if next_version is None or user is None:
        return json_error("The email address or temporary code is invalid or expired.")

    establish_user_session(int(user["id"]), next_version)
    try:
        send_password_changed_notice(str(user["email"]), str(user["username"]))
    except Exception:
        current_app.logger.exception("Could not send the password-change notice")
    return jsonify({"ok": True, "user": get_user_by_id(int(user["id"]))}), 200


@bp.post("/api/email-verification/request")
@login_required
def request_email_verification() -> tuple[object, int]:
    if not mail_is_configured():
        return json_error("Email verification is not available on this installation.", 503)
    user = get_user_by_id(current_user_id() or 0)
    if user is None:
        session.clear()
        return json_error("Authentication required.", 401)
    if user["emailVerified"]:
        return jsonify({"ok": True, "verified": True}), 200
    address = client_address()
    if not _code_request_is_limited(int(user["id"]), EMAIL_VERIFICATION_PURPOSE, address):
        _issue_account_code(user, EMAIL_VERIFICATION_PURPOSE, address)
    return jsonify({"ok": True, "verified": False}), 202


@bp.post("/api/email-verification/confirm")
@login_required
def confirm_email_verification() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    code = str(data.get("code", "")).strip()
    user_id = current_user_id() or 0
    if len(code) != 6 or not code.isdigit():
        return json_error("The temporary code is invalid or expired.")
    if not verify_email_with_account_code(
        user_id,
        code,
        str(current_app.config["ACCOUNT_CODE_PEPPER"]),
        max(1, int(current_app.config["ACCOUNT_CODE_MAX_ATTEMPTS"])),
    ):
        return json_error("The temporary code is invalid or expired.")
    user = get_user_by_id(user_id)
    return jsonify({"ok": True, "user": user}), 200
