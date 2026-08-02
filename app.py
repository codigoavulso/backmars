from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import bp as auth_bp
from config import DEV_SECRET_KEY, DevelopmentConfig, ProductionConfig
from db import ensure_database, init_app as init_db_app
from projects import bp as projects_bp
from version import APP_VERSION


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

    # Only trust X-Forwarded-For when a proxy is declared, otherwise a client
    # could set the header itself and step around the login rate limit.
    trusted_proxies = int(app.config.get("TRUSTED_PROXY_COUNT", 0) or 0)
    if trusted_proxies > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxies, x_proto=trusted_proxies)

    init_db_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(projects_bp)

    with app.app_context():
        ensure_database()

    @app.get("/api/health")
    def health() -> tuple[dict[str, object], int]:
        return {
            "ok": True,
            "service": "webmars-users",
            "version": APP_VERSION,
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
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, If-Match"
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
