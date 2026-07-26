from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from auth import bp as auth_bp
from config import DevelopmentConfig, ProductionConfig
from db import ensure_database, init_app as init_db_app
from projects import bp as projects_bp
from version import APP_VERSION


def create_app(config_overrides: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    env = os.environ.get("WEBMARS_USERS_ENV", "development").lower()
    app.config.from_object(ProductionConfig if env == "production" else DevelopmentConfig)
    if config_overrides:
        app.config.update(config_overrides)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

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

    @app.after_request
    def add_cors_headers(response):
        allowed_origins = {
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        }
        raw_extra_origins = os.environ.get("WEBMARS_USERS_ALLOWED_ORIGINS", "")
        allowed_origins.update(
            origin.strip()
            for origin in raw_extra_origins.split(",")
            if origin.strip()
        )
        origin = request.headers.get("Origin", "").strip()
        if origin and origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Vary"] = "Origin"
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
