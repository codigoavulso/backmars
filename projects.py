from __future__ import annotations

import sqlite3

from flask import Blueprint, jsonify, request

from models import (
    create_project as create_project_record,
    delete_project as delete_project_record,
    get_project,
    list_projects_for_user,
    update_project as update_project_record,
    utc_now,
)
from utils import coerce_project_document, current_user_id, json_error, login_required

bp = Blueprint("projects", __name__)

DUPLICATE_NAME_MESSAGE = "A project with that name already exists."


def _with_etag(payload: dict[str, object], status: int):
    """Attach the version token as a header as well as inside the body.

    The header is the standard place; the body copy survives proxies that strip
    it and spares the browser from depending on the CORS expose-header grant.
    """
    response = jsonify(payload)
    project = payload.get("project")
    etag = project.get("etag") if isinstance(project, dict) else None
    if etag:
        response.headers["ETag"] = f'"{etag}"'
    return response, status


def _requested_etag() -> str | None:
    raw = request.headers.get("If-Match", "").strip()
    if not raw:
        return None
    if raw == "*":
        return "*"
    # Accept the quoted form, a weak validator and a bare token alike.
    return raw.removeprefix("W/").strip('"')


def _stale_response(stored: dict[str, object] | None):
    response = jsonify({
        "error": "The stored project changed since it was loaded.",
        "code": "stale_version",
        "project": stored,
    })
    if stored and stored.get("etag"):
        response.headers["ETag"] = f'"{stored["etag"]}"'
    return response, 412


@bp.get("/api/projects")
@login_required
def list_projects() -> tuple[object, int]:
    include_documents = request.args.get("include", "").strip().lower() == "documents"
    return jsonify({
        "projects": list_projects_for_user(current_user_id() or 0, include_documents=include_documents),
        # Without this the browser has to compare its own clock against
        # updated_at, and a skewed client picks the wrong sync direction and
        # quietly overwrites the newer copy.
        "serverTime": utc_now(),
    }), 200


@bp.post("/api/projects")
@login_required
def create_project() -> tuple[object, int]:
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    name = str(data.get("name", "")).strip() or "Untitled Project"
    raw_project = data.get("project", data.get("project_json"))

    try:
        project = coerce_project_document(raw_project)
    except ValueError as exc:
        return json_error(str(exc))

    try:
        created = create_project_record(current_user_id() or 0, name, project)
    except sqlite3.IntegrityError:
        return json_error(DUPLICATE_NAME_MESSAGE, 409, field="name")
    return _with_etag({"project": created}, 201)


@bp.get("/api/projects/<int:project_id>")
@login_required
def read_project(project_id: int) -> tuple[object, int]:
    project = get_project(current_user_id() or 0, project_id)
    if project is None:
        return json_error("Project not found.", 404)
    return _with_etag({"project": project}, 200)


@bp.put("/api/projects/<int:project_id>")
@login_required
def update_project(project_id: int) -> tuple[object, int]:
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    name = str(data.get("name", "")).strip() if "name" in data else None
    has_project_payload = "project" in data or "project_json" in data
    raw_project = data.get("project", data.get("project_json"))

    try:
        project = coerce_project_document(raw_project) if has_project_payload else None
    except ValueError as exc:
        return json_error(str(exc))

    if name == "":
        return json_error("Project name cannot be empty.")
    if name is None and project is None:
        return json_error("Nothing to update.")

    user_id = current_user_id() or 0
    expected_etag = _requested_etag()
    if expected_etag is None:
        return json_error("If-Match is required when updating a project.", 428, code="precondition_required")

    try:
        outcome, updated = update_project_record(
            user_id,
            project_id,
            name=name,
            project=project,
            expected_etag=expected_etag,
        )
    except sqlite3.IntegrityError:
        return json_error(DUPLICATE_NAME_MESSAGE, 409, field="name")
    if outcome == "missing":
        return json_error("Project not found.", 404)
    if outcome == "stale":
        return _stale_response(updated)
    return _with_etag({"project": updated}, 200)


@bp.delete("/api/projects/<int:project_id>")
@login_required
def delete_project(project_id: int) -> tuple[object, int]:
    expected_etag = _requested_etag()
    if expected_etag is None:
        return json_error("If-Match is required when deleting a project.", 428, code="precondition_required")
    outcome, stored = delete_project_record(
        current_user_id() or 0,
        project_id,
        expected_etag=expected_etag,
    )
    if outcome == "missing":
        return json_error("Project not found.", 404)
    if outcome == "stale":
        return _stale_response(stored)
    return jsonify({"deleted": True, "id": project_id}), 200
