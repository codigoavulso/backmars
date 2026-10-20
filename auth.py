from __future__ import annotations

import re
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, current_app, jsonify, request, session

from mailer import mail_is_configured, send_account_code, send_password_changed_notice
from models import (
    account_code_digest,
    consume_account_code,
    create_account_code,
    create_user,
    discard_login_attempt,
    generate_account_code,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    get_user_session_version,
    purge_old_account_codes,
    reserve_login_attempt,
    reserve_registration_attempt,
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
# Unknown users still traverse the same password verifier as known users.  The
# random value is generated once per worker; it is not a credential and never
# leaves process memory.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


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
    if (
        not email
        or len(email) > 254
        or email.count("@") != 1
        or any(character.isspace() or ord(character) < 32 for character in email)
    ):
        return False
    local, domain = email.rsplit("@", 1)
    if not local or len(local.encode("utf-8")) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_domain) > 253 or "." not in ascii_domain:
        return False
    labels = ascii_domain.split(".")
    return all(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and re.fullmatch(r"[A-Za-z0-9-]+", label)
        for label in labels
    )


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
        user_limit=max(1, int(current_app.config["ACCOUNT_CODE_USER_LIMIT_PER_HOUR"])),
        address_limit=max(
            1, int(current_app.config["ACCOUNT_CODE_ADDRESS_LIMIT_PER_HOUR"])
        ),
    )
    if code_id is None:
        return False
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


@bp.post("/api/register")
def register() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    raw_username = data.get("username", "")
    raw_email = data.get("email", "")
    raw_password = data.get("password", "")
    if not all(
        isinstance(value, str)
        for value in (raw_username, raw_email, raw_password)
    ):
        return json_error("username, email and password must be text.")
    username = raw_username.strip()
    email = raw_email.strip().lower()
    password = raw_password

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
    if reserve_registration_attempt(address, window_seconds, maximum) is None:
        response, status = json_error("Too many registrations from this address. Try again later.", 429)
        response.headers["Retry-After"] = str(window_seconds)
        return response, status

    try:
        user = create_user(username, email, hash_password(password))
    except sqlite3.IntegrityError:
        return json_error("Username or email already exists.", 409)
    establish_user_session(user["id"])
    verification_sent = False
    if mail_is_configured():
        verification_sent = _issue_account_code(user, EMAIL_VERIFICATION_PURPOSE, address)
    return jsonify({"user": user, "verificationEmailSent": verification_sent}), 201


@bp.post("/api/login")
def login() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    raw_username = data.get("username", data.get("identifier", ""))
    raw_password = data.get("password", "")
    if not isinstance(raw_username, str) or not isinstance(raw_password, str):
        return json_error("Invalid credentials.", 401)
    username = raw_username.strip()
    password = raw_password

    if not username or not password:
        return json_error("username and password are required.")
    maximum_identifier = max(254, int(current_app.config["USERNAME_MAX_LENGTH"]))
    if len(username) > maximum_identifier or len(password) > int(current_app.config["PASSWORD_MAX_LENGTH"]):
        return json_error("Invalid credentials.", 401)

    address = client_address()
    window_seconds = max(1, int(current_app.config["LOGIN_ATTEMPT_WINDOW_SECONDS"]))
    max_attempts = max(1, int(current_app.config["LOGIN_MAX_ATTEMPTS"]))
    attempt_id = reserve_login_attempt(address, window_seconds, max_attempts)
    if attempt_id is None:
        response, status = json_error("Too many failed sign-in attempts. Try again later.", 429)
        response.headers["Retry-After"] = str(window_seconds)
        return response, status

    user = get_user_by_email(username.lower()) if "@" in username else get_user_by_username(username)
    # Social-only users also traverse the expensive dummy verifier. An empty
    # hash must neither authenticate nor reveal which email uses social login.
    has_password = bool(user and user["password_hash"])
    password_hash = user["password_hash"] if has_password else _DUMMY_PASSWORD_HASH
    password_is_valid = verify_password(password, password_hash)
    if not user or not has_password or not password_is_valid:
        return json_error("Invalid credentials.", 401)
    # The reservation protected the expensive verifier from a concurrent burst.
    # A valid login is not a failed attempt, so remove only its own row; earlier
    # failures from the same address remain and cannot be cleared by an attacker.
    discard_login_attempt(attempt_id)
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
    raw_email = data.get("email", "")
    if not isinstance(raw_email, str):
        return json_error("A valid email address is required.", field="email")
    email = raw_email.strip().lower()
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
    if user:
        _issue_account_code(user, PASSWORD_RESET_PURPOSE, address)
    return jsonify(generic), 202


@bp.post("/api/password-reset/confirm")
def confirm_password_reset() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    raw_email = data.get("email", "")
    raw_code = data.get("code", "")
    raw_password = data.get("password", "")
    if not all(
        isinstance(value, str)
        for value in (raw_email, raw_code, raw_password)
    ):
        return json_error("The email address or temporary code is invalid.")
    email = raw_email.strip().lower()
    code = raw_code.strip()
    password = raw_password
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
    _issue_account_code(user, EMAIL_VERIFICATION_PURPOSE, address)
    return jsonify({"ok": True, "verified": False}), 202


@bp.post("/api/email-verification/confirm")
@login_required
def confirm_email_verification() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    raw_code = data.get("code", "")
    if not isinstance(raw_code, str):
        return json_error("The temporary code is invalid or expired.")
    code = raw_code.strip()
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
