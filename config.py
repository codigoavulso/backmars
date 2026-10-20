from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# The placeholder every fresh checkout starts with. Production refuses to boot
# while this is still the signing key, because a known key means anyone can mint
# a session cookie for any user id.
DEV_SECRET_KEY = "dev-change-me"


def _int_from_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool_from_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def _bool_from_aliases(*names: str, default: bool = False) -> bool:
    value = _env_first(*names, default="")
    if value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_from_aliases(*names: str, default: int) -> int:
    try:
        return int(_env_first(*names, default=str(default)))
    except (TypeError, ValueError):
        return default


class Config:
    GOOGLE_CLIENT_ID = os.environ.get("BACKMARS_GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("BACKMARS_GOOGLE_CLIENT_SECRET", "")
    FACEBOOK_CLIENT_ID = os.environ.get("BACKMARS_FACEBOOK_CLIENT_ID", "")
    FACEBOOK_CLIENT_SECRET = os.environ.get("BACKMARS_FACEBOOK_CLIENT_SECRET", "")
    # Set explicitly to a supported Graph API version before enabling Facebook.
    FACEBOOK_API_VERSION = os.environ.get("BACKMARS_FACEBOOK_API_VERSION", "")
    OAUTH_CALLBACK_BASE = os.environ.get("BACKMARS_OAUTH_CALLBACK_BASE", "").rstrip("/")
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
    DATABASE = Path(
        _env_first(
            "BACKMARS_DATABASE",
            "WEBMARS_USERS_DATABASE",
            default=str(BASE_DIR / "data" / "app.db"),
        )
    )
    SCHEMA = BASE_DIR / "schema.sql"
    JSON_SORT_KEYS = False
    SESSION_COOKIE_HTTPONLY = True
    # Lax is right while the API and the app share a registrable domain
    # (backmars.nfiles.top and webmars.nfiles.top both sit under nfiles.top).
    # Moving the API to another domain makes the request cross-site and the
    # cookie stops being sent: that deployment needs "None" plus Secure.
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = timedelta(days=365)
    SESSION_REFRESH_EACH_REQUEST = False

    # A project is one JSON document; the browser caps its own workspace at 1 MB,
    # but nothing stopped a hand-written request from posting far more.
    MAX_CONTENT_LENGTH = _int_from_env("WEBMARS_USERS_MAX_CONTENT_LENGTH", 4 * 1024 * 1024)

    # Failed logins allowed from one address before it is turned away. Keyed on
    # the address alone: keying on the username would let anyone lock a user out
    # of their own account.
    LOGIN_MAX_ATTEMPTS = _int_from_env("WEBMARS_USERS_LOGIN_MAX_ATTEMPTS", 20)
    LOGIN_ATTEMPT_WINDOW_SECONDS = _int_from_env("WEBMARS_USERS_LOGIN_WINDOW_SECONDS", 900)
    REGISTER_MAX_ATTEMPTS = _int_from_env("BACKMARS_REGISTER_MAX_ATTEMPTS", 20)
    REGISTER_ATTEMPT_WINDOW_SECONDS = _int_from_env(
        "BACKMARS_REGISTER_WINDOW_SECONDS", 3600
    )

    # Number of proxies in front of the app. Above zero the client address is
    # read from X-Forwarded-For; left at zero that header is ignored, so a
    # direct-to-Flask deployment cannot have the rate limit spoofed away.
    TRUSTED_PROXY_COUNT = _int_from_env("WEBMARS_USERS_TRUSTED_PROXIES", 0)
    DATABASE_BUSY_TIMEOUT_MS = _int_from_env("WEBMARS_USERS_DATABASE_BUSY_TIMEOUT_MS", 5000)

    # The public URL is used only in transactional email copy.  It is supplied
    # by each deployment; no nfiles.top address is baked into the service.
    WEBMARS_PUBLIC_URL = os.environ.get("WEBMARS_PUBLIC_URL", "http://localhost:8080").rstrip("/")

    MAIL_ENABLED = _bool_from_aliases("BACKMARS_MAIL_ENABLED", "MAIL_ENABLED", default=False)
    MAIL_FROM = _env_first("BACKMARS_MAIL_FROM", "MAIL_FROM").strip()
    MAIL_FROM_NAME = _env_first("BACKMARS_MAIL_FROM_NAME", "MAIL_FROM_NAME", default="webMARS").strip() or "webMARS"
    SMTP_HOST = _env_first("BACKMARS_SMTP_HOST", "SMTP_HOST").strip()
    SMTP_PORT = _int_from_aliases("BACKMARS_SMTP_PORT", "SMTP_PORT", default=587)
    SMTP_USERNAME = _env_first("BACKMARS_SMTP_USERNAME", "SMTP_USERNAME").strip()
    SMTP_PASSWORD = _env_first("BACKMARS_SMTP_PASSWORD", "SMTP_PASSWORD")
    SMTP_SECURITY = _env_first("BACKMARS_SMTP_SECURITY", "SMTP_SECURITY", default="starttls").strip().lower()
    SMTP_TIMEOUT_SECONDS = _int_from_aliases("BACKMARS_SMTP_TIMEOUT_SECONDS", "SMTP_TIMEOUT_SECONDS", default=15)
    MAIL_SEND_ASYNC = _bool_from_aliases(
        "BACKMARS_MAIL_SEND_ASYNC", "MAIL_SEND_ASYNC", default=False
    )
    ACCOUNT_CODE_PEPPER = _env_first(
        "BACKMARS_ACCOUNT_CODE_PEPPER",
        "BACKMARS_PASSWORD_RESET_PEPPER",
        default=SECRET_KEY,
    )
    ACCOUNT_CODE_PEPPER_IS_SEPARATE = bool(
        _env_first("BACKMARS_ACCOUNT_CODE_PEPPER", "BACKMARS_PASSWORD_RESET_PEPPER")
    )

    ACCOUNT_CODE_TTL_SECONDS = _int_from_env("BACKMARS_CODE_TTL_SECONDS", 600)
    ACCOUNT_CODE_MAX_ATTEMPTS = _int_from_env("BACKMARS_CODE_MAX_ATTEMPTS", 5)
    ACCOUNT_CODE_USER_LIMIT_PER_HOUR = _int_from_env("BACKMARS_CODE_USER_LIMIT_PER_HOUR", 5)
    ACCOUNT_CODE_ADDRESS_LIMIT_PER_HOUR = _int_from_env("BACKMARS_CODE_ADDRESS_LIMIT_PER_HOUR", 20)
    PASSWORD_MIN_LENGTH = _int_from_env("BACKMARS_PASSWORD_MIN_LENGTH", 8)
    PASSWORD_MAX_LENGTH = _int_from_env("BACKMARS_PASSWORD_MAX_LENGTH", 1024)
    USERNAME_MAX_LENGTH = _int_from_env("BACKMARS_USERNAME_MAX_LENGTH", 64)
    PROJECT_MAX_NAME_LENGTH = _int_from_env("BACKMARS_PROJECT_MAX_NAME_LENGTH", 120)
    REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH = _bool_from_env(
        "BACKMARS_REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH", True
    )

    PUBLICATION_LIST_LIMIT = _int_from_env("BACKMARS_PUBLICATION_LIST_LIMIT", 50)
    PUBLICATION_MAX_TAGS = _int_from_env("BACKMARS_PUBLICATION_MAX_TAGS", 8)
    PUBLICATION_MAX_TITLE_LENGTH = _int_from_env("BACKMARS_PUBLICATION_MAX_TITLE_LENGTH", 120)
    PUBLICATION_MAX_DESCRIPTION_LENGTH = _int_from_env("BACKMARS_PUBLICATION_MAX_DESCRIPTION_LENGTH", 2000)
    PUBLICATION_MAX_INSTRUCTIONS_LENGTH = _int_from_env("BACKMARS_PUBLICATION_MAX_INSTRUCTIONS_LENGTH", 12000)
    PUBLICATION_MAX_FILES = _int_from_env("BACKMARS_PUBLICATION_MAX_FILES", 128)
    PUBLICATION_MAX_FILE_BYTES = _int_from_env("BACKMARS_PUBLICATION_MAX_FILE_BYTES", 512 * 1024)
    # Matches webMARS' default local-project ceiling, so every accepted public
    # snapshot can be opened by an unmodified client.
    PUBLICATION_MAX_TOTAL_BYTES = _int_from_env("BACKMARS_PUBLICATION_MAX_TOTAL_BYTES", 1024 * 1024)
    PUBLICATION_MAX_PATH_DEPTH = _int_from_env("BACKMARS_PUBLICATION_MAX_PATH_DEPTH", 8)


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "1") == "1"
    MAIL_SEND_ASYNC = _bool_from_aliases(
        "BACKMARS_MAIL_SEND_ASYNC", "MAIL_SEND_ASYNC", default=True
    )
