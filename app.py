from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import bp as auth_bp
from config import BASE_DIR, DEV_SECRET_KEY, DevelopmentConfig, ProductionConfig
from db import ensure_database
from db import init_app as init_db_app
from mailer import mail_is_configured
from projects import bp as projects_bp
from publications import bp as publications_bp
from version import APP_VERSION

MIN_PRODUCTION_SECRET_BYTES = 32


def _secret_has_minimum_strength(value: object) -> bool:
    if isinstance(value, bytes):
        encoded = value
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
    else:
        return False
    return len(encoded) >= MIN_PRODUCTION_SECRET_BYTES


def allowed_origins() -> set[str]:
    origins = {
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    }
    origins.update(
        origin.strip()
        for origin in os.environ.get("WEBMARS_USERS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    )
    return origins


def create_app(config_overrides: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    env = os.environ.get("WEBMARS_USERS_ENV", "").strip().lower()
    if env not in {"development", "production"}:
        raise RuntimeError(
            "WEBMARS_USERS_ENV must be set explicitly to 'development' or 'production'. "
            "Refusing to guess because an implicit development mode would allow the "
            "public development SECRET_KEY in a production service."
        )
    is_production = env == "production"
    app.config.from_object(ProductionConfig if is_production else DevelopmentConfig)
    if config_overrides:
        app.config.update(config_overrides)
    if not app.config.get("ACCOUNT_CODE_PEPPER_IS_SEPARATE"):
        # Tests and embedded deployments often override SECRET_KEY after the
        # Config class has been imported; keep the documented fallback aligned.
        app.config["ACCOUNT_CODE_PEPPER"] = app.config["SECRET_KEY"]
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    if app.config.get("SECRET_KEY") == DEV_SECRET_KEY:
        if is_production:
            raise RuntimeError(
                "SECRET_KEY is still the development placeholder. Session cookies "
                "would be signed with a publicly known key, letting anyone forge a "
                "session for any account. Set SECRET_KEY before starting in production."
            )
        app.logger.warning(
            "SECRET_KEY is the development placeholder; sessions are forgeable. "
            "Never run this configuration outside a local machine."
        )
    if is_production and not _secret_has_minimum_strength(app.config.get("SECRET_KEY")):
        raise RuntimeError(
            f"SECRET_KEY must contain at least {MIN_PRODUCTION_SECRET_BYTES} bytes in production."
        )
    mail_enabled = bool(app.config.get("MAIL_ENABLED"))
    if mail_enabled:
        if not app.config.get("MAIL_FROM") or not app.config.get("SMTP_HOST"):
            raise RuntimeError(
                "Transactional mail is enabled but BACKMARS_MAIL_FROM or "
                "BACKMARS_SMTP_HOST is missing."
            )
        if app.config.get("SMTP_USERNAME") and not app.config.get("SMTP_PASSWORD"):
            raise RuntimeError(
                "BACKMARS_SMTP_USERNAME is configured but BACKMARS_SMTP_PASSWORD is missing."
            )
        security = str(app.config.get("SMTP_SECURITY", "")).lower()
        if security not in {"starttls", "ssl", "tls", "none"}:
            raise RuntimeError(
                "BACKMARS_SMTP_SECURITY must be starttls, ssl, tls or none."
            )
        if is_production and security == "none":
            raise RuntimeError("Production transactional mail must use TLS.")
    mail_configured = bool(
        callable(app.config.get("MAIL_SEND_FUNCTION"))
        or (
            mail_enabled
            and app.config.get("MAIL_FROM")
            and app.config.get("SMTP_HOST")
        )
    )
    if is_production and mail_configured:
        if not app.config.get("ACCOUNT_CODE_PEPPER_IS_SEPARATE"):
            raise RuntimeError(
                "BACKMARS_ACCOUNT_CODE_PEPPER must be a separate random secret in production."
            )
        if not _secret_has_minimum_strength(app.config.get("ACCOUNT_CODE_PEPPER")):
            raise RuntimeError(
                "BACKMARS_ACCOUNT_CODE_PEPPER must contain at least "
                f"{MIN_PRODUCTION_SECRET_BYTES} bytes in production."
            )
    if is_production:
        database_path = Path(app.config["DATABASE"]).expanduser()
        if not database_path.is_absolute() or database_path.resolve().is_relative_to(BASE_DIR.resolve()):
            raise RuntimeError(
                "Production DATABASE must be an absolute path outside the BackMars code checkout."
            )
        if app.config.get("REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH") and not mail_configured:
            raise RuntimeError(
                "Email verification is required for publishing, but transactional mail is disabled. "
                "Configure SMTP or explicitly set BACKMARS_REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH=0."
            )

    # Only trust X-Forwarded-For when a proxy is declared, otherwise a client
    # could set the header itself and step around the login rate limit.
    trusted_proxies = int(app.config.get("TRUSTED_PROXY_COUNT", 0) or 0)
    if trusted_proxies > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxies, x_proto=trusted_proxies)

    init_db_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(publications_bp)

    with app.app_context():
        ensure_database()

    from social_auth import init_app as init_social_auth
    from account_statistics import init_app as init_account_statistics
    init_social_auth(app)
    init_account_statistics(app)

    @app.get("/api/health")
    def health() -> tuple[dict[str, object], int]:
        return {
            "ok": True,
            "service": "webmars-users",
            "version": APP_VERSION,
            "mailConfigured": mail_is_configured(),
        }, 200

    @app.route("/api", methods=["OPTIONS"])
    @app.route("/api/<path:_path>", methods=["OPTIONS"])
    def preflight(_path: str | None = None) -> tuple[str, int]:
        return "", 204

    @app.before_request
    def reject_untrusted_browser_writes():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        origin = request.headers.get("Origin", "").strip()
        if origin and origin not in allowed_origins():
            return jsonify({"error": "Origin is not allowed to modify this service."}), 403
        return None

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get("Origin", "").strip()
        if origin:
            response.vary.add("Origin")
        if origin and origin in allowed_origins():
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            # If-Match carries the optimistic-locking token on project writes,
            # and ETag has to be exposed or cross-origin callers cannot read it
            # back off the response.
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, If-Match, X-WebMARS-Context"
            response.headers["Access-Control-Expose-Headers"] = "ETag"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return response

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> tuple[object, int]:
        return jsonify({"error": error.description}), error.code or 500

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception) -> tuple[object, int]:
        app.logger.exception("Unhandled error", exc_info=error)
        return jsonify({"error": "Internal server error."}), 500

    return app


app = create_app()
