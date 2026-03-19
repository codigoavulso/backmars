from __future__ import annotations

from flask import Blueprint, jsonify, request

from models import (
    create_project as create_project_record,
    delete_project as delete_project_record,
    get_project,
    list_projects_for_user,
    update_project as update_project_record,
)
from utils import coerce_project_document, current_user_id, json_error, login_required

bp = Blueprint("projects", __name__)


@bp.get("/api/projects")
@login_required
def list_projects() -> tuple[object, int]:
    return jsonify({"projects": list_projects_for_user(current_user_id() or 0)}), 200


@bp.post("/api/projects")
@login_required
def create_project() -> tuple[object, int]:
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip() or "Untitled Project"
    raw_project = data.get("project", data.get("project_json"))

    try:
        project = coerce_project_document(raw_project)
    except ValueError as exc:
        return json_error(str(exc))

    created = create_project_record(current_user_id() or 0, name, project)
    return jsonify({"project": created}), 201


@bp.get("/api/projects/<int:project_id>")
@login_required
def read_project(project_id: int) -> tuple[object, int]:
    project = get_project(current_user_id() or 0, project_id)
    if project is None:
        return json_error("Project not found.", 404)
    return jsonify({"project": project}), 200


@bp.put("/api/projects/<int:project_id>")
@login_required
def update_project(project_id: int) -> tuple[object, int]:
    data = request.get_json(silent=True) or {}
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

    updated = update_project_record(current_user_id() or 0, project_id, name=name, project=project)
    if updated is None:
        return json_error("Project not found.", 404)
    return jsonify({"project": updated}), 200


@bp.delete("/api/projects/<int:project_id>")
@login_required
def delete_project(project_id: int) -> tuple[object, int]:
    deleted = delete_project_record(current_user_id() or 0, project_id)
    if not deleted:
        return json_error("Project not found.", 404)
    return jsonify({"deleted": True, "id": project_id}), 200
