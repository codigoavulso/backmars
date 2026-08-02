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


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
    DATABASE = BASE_DIR / "data" / "app.db"
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

    # Number of proxies in front of the app. Above zero the client address is
    # read from X-Forwarded-For; left at zero that header is ignored, so a
    # direct-to-Flask deployment cannot have the rate limit spoofed away.
    TRUSTED_PROXY_COUNT = _int_from_env("WEBMARS_USERS_TRUSTED_PROXIES", 0)
    DATABASE_BUSY_TIMEOUT_MS = _int_from_env("WEBMARS_USERS_DATABASE_BUSY_TIMEOUT_MS", 5000)


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "1") == "1"
