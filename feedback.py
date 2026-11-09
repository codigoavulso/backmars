"""Submit voluntary webMARS feedback to the installation operator."""
import hashlib
import hmac
import json
import re
import smtplib
import time

from flask import jsonify, request
from db import get_db
from mailer import mail_is_configured, send_mail
from utils import client_address

IMAGE_LIMIT = 2 * 1024 * 1024
PROJECT_LIMIT = 1024 * 1024


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise ValueError("Unsupported image")


def reserve_attempt(app):
    now = int(time.time())
    address = hmac.new(str(app.secret_key).encode(), client_address().encode(), hashlib.sha256).hexdigest()
    db = get_db()
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM feedback_attempts WHERE received < ?", (now - 3600,))
        total, same_address = db.execute(
            "SELECT count(*), count(CASE WHEN address_hash=? THEN 1 END) FROM feedback_attempts", (address,)
        ).fetchone()
        if total >= 30 or same_address >= 4:
            return False
        db.execute("INSERT INTO feedback_attempts VALUES (?,?)", (address, now))
    return True


def init_app(app):
    def enabled():
        return bool(app.config["FEEDBACK_TO"] and mail_is_configured())

    @app.get("/api/feedback")
    def feedback_capability():
        return jsonify({"enabled": enabled()})

    @app.post("/api/feedback")
    def submit_feedback():
        if not enabled():
            return jsonify({"error": "Feedback is unavailable."}), 503
        if not request.headers.get("Origin") or request.mimetype != "multipart/form-data":
            return jsonify({"error": "Expected browser form."}), 400
        request.max_content_length = 3 * IMAGE_LIMIT + PROJECT_LIMIT + 65536
        request.max_form_memory_size = 65536
        request.max_form_parts = 8
        message = request.form.get("message", "").strip()
        build = request.form.get("build", "")
        if (not 1 <= len(message) <= 4000 or not re.fullmatch(r"[0-9]{12}", build)
                or set(request.form) - {"message", "build"} or set(request.files) - {"screenshots", "project"}):
            return jsonify({"error": "Invalid feedback."}), 400
        images = request.files.getlist("screenshots")
        projects = request.files.getlist("project")
        if len(images) > 3 or len(projects) > 1:
            return jsonify({"error": "Too many attachments."}), 400
        attachments = []
        try:
            for index, image in enumerate(images, 1):
                content = image.stream.read(IMAGE_LIMIT + 1)
                if len(content) > IMAGE_LIMIT:
                    return jsonify({"error": "Image too large."}), 413
                extension, mime = image_type(content)
                attachments.append((f"screenshot-{index}.{extension}", mime, content))
            if projects:
                content = projects[0].stream.read(PROJECT_LIMIT + 1)
                if len(content) > PROJECT_LIMIT:
                    return jsonify({"error": "Project too large."}), 413
                data = json.loads(content)
                if not isinstance(data, dict) or not isinstance(data.get("files"), list):
                    raise ValueError("Invalid project")
                attachments.append(("webmars-project.json", "application/json", content))
        except (ValueError, RecursionError):
            return jsonify({"error": "Invalid attachment."}), 400
        if not reserve_attempt(app):
            return jsonify({"error": "Too many submissions."}), 429
        try:
            if send_mail(app.config["FEEDBACK_TO"], "webMARS feedback",
                         f"Build: {build}\n\n{message}", attachments=attachments):
                return "", 204
        except (OSError, smtplib.SMTPException, ValueError):
            app.logger.warning("Feedback mail delivery failed")
        return jsonify({"error": "Feedback could not be delivered."}), 503
