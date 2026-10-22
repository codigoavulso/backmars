from __future__ import annotations

import sqlite3

from flask import Blueprint, current_app, jsonify, request

from models import (
    create_project as create_project_record,
    get_project_by_client_id,
)
from models import (
    delete_project as delete_project_record,
)
from models import (
    get_project,
    list_projects_for_user,
    list_projects_page_for_user,
    purge_project,
    restore_project,
    trash_project,
    utc_now,
)
from models import (
    update_project as update_project_record,
)
from utils import (
    coerce_project_document,
    current_user_id,
    json_error,
    login_required,
    valid_sqlite_id,
)

bp = Blueprint("projects", __name__)

DUPLICATE_NAME_MESSAGE = "A project with that name already exists."


def _project_name(value: object, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str):
        raise ValueError("Project name must be text.")
    name = value.strip()
    if not name:
        if default is not None:
            return default
        raise ValueError("Project name cannot be empty.")
    maximum = max(1, int(current_app.config["PROJECT_MAX_NAME_LENGTH"]))
    if len(name) > maximum:
        raise ValueError(f"Project name must contain at most {maximum} characters.")
    return name


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


@bp.get("/api/v2/projects")
@login_required
def list_projects_v2() -> tuple[object, int]:
    """Paginated metadata contract used by the unified project manager."""
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = max(1, min(100, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        return json_error("offset and limit must be integers.", 400)
    query = str(request.args.get("q", "")).strip()
    scope = str(request.args.get("scope", "active")).strip().lower()
    if scope not in {"active", "trash"}:
        return json_error("scope must be active or trash.", 400)
    sort = str(request.args.get("sort", "updated")).strip()
    order = str(request.args.get("order", "desc")).strip()
    include_documents = request.args.get("include", "").strip().lower() == "documents"
    projects, has_more, next_offset = list_projects_page_for_user(
        current_user_id() or 0,
        offset=offset,
        limit=limit,
        query=query,
        include_documents=include_documents,
        scope=scope,
        sort=sort,
        order=order,
    )
    return jsonify({
        "projects": projects,
        "offset": offset,
        "limit": limit,
        "nextOffset": next_offset if has_more else None,
        "hasMore": has_more,
        "serverTime": utc_now(),
    }), 200


@bp.post("/api/v2/projects")
@login_required
def create_project_v2() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    client_project_id = str(data.get("clientProjectId", "")).strip()
    if not client_project_id or len(client_project_id) > 128:
        return json_error("clientProjectId is required and must contain at most 128 characters.", field="clientProjectId")
    try:
        name = _project_name(data.get("name"), default="Untitled Project")
        project = coerce_project_document(data.get("document", data.get("project")))
    except ValueError as exc:
        return json_error(str(exc))
    user_id = current_user_id() or 0
    existing = get_project_by_client_id(user_id, client_project_id, include_deleted=True)
    if existing is not None:
        same_name = str(existing.get("name", "")) == name
        same_document = existing.get("project") == project
        if same_name and same_document and not existing.get("deletedAt"):
            return _with_etag({"project": existing}, 200)
        return json_error("That client project identity is already registered with different data.", 409, code="idempotency_conflict")
    try:
        created = create_project_record(user_id, name, project, client_project_id=client_project_id)
    except sqlite3.IntegrityError:
        return json_error(DUPLICATE_NAME_MESSAGE, 409, code="name_conflict", field="name")
    return _with_etag({"project": created}, 201)


@bp.get("/api/v2/projects/<int:project_id>")
@login_required
def read_project_v2(project_id: int) -> tuple[object, int]:
    if not valid_sqlite_id(project_id):
        return json_error("Project not found.", 404)
    include_deleted = request.args.get("scope", "active").strip().lower() == "trash"
    project = get_project(current_user_id() or 0, project_id, include_deleted=include_deleted)
    if project is None:
        return json_error("Project not found.", 404)
    return _with_etag({"project": project}, 200)


@bp.put("/api/v2/projects/<int:project_id>")
@login_required
def update_project_v2(project_id: int) -> tuple[object, int]:
    if not valid_sqlite_id(project_id):
        return json_error("Project not found.", 404)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    try:
        name = _project_name(data.get("name")) if "name" in data else None
        has_document = "document" in data or "project" in data
        document = coerce_project_document(data.get("document", data.get("project"))) if has_document else None
    except ValueError as exc:
        return json_error(str(exc))
    if name is None and document is None:
        return json_error("Nothing to update.")
    expected = _requested_etag()
    if expected is None:
        return json_error("If-Match is required when updating a project.", 428, code="precondition_required")
    outcome, updated = update_project_record(
        current_user_id() or 0,
        project_id,
        name=name,
        project=document,
        expected_etag=expected,
    )
    if outcome == "missing":
        return json_error("Project not found.", 404)
    if outcome == "stale":
        return _stale_response(updated)
    return _with_etag({"project": updated}, 200)


@bp.post("/api/v2/projects/<int:project_id>/trash")
@login_required
def trash_project_v2(project_id: int) -> tuple[object, int]:
    if not valid_sqlite_id(project_id):
        return json_error("Project not found.", 404)
    expected = _requested_etag()
    if expected is None:
        return json_error("If-Match is required when moving a project to the recycle bin.", 428, code="precondition_required")
    outcome, project = trash_project(current_user_id() or 0, project_id, expected_etag=expected)
    if outcome == "missing":
        return json_error("Project not found.", 404)
    if outcome == "stale":
        return _stale_response(project)
    return _with_etag({"project": project}, 200)


@bp.post("/api/v2/projects/<int:project_id>/restore")
@login_required
def restore_project_v2(project_id: int) -> tuple[object, int]:
    if not valid_sqlite_id(project_id):
        return json_error("Project not found.", 404)
    expected = _requested_etag()
    if expected is None:
        return json_error("If-Match is required when restoring a project.", 428, code="precondition_required")
    try:
        outcome, project = restore_project(current_user_id() or 0, project_id, expected_etag=expected)
    except sqlite3.IntegrityError:
        return json_error(DUPLICATE_NAME_MESSAGE, 409, code="name_conflict", field="name")
    if outcome == "missing":
        return json_error("Project not found.", 404)
    if outcome == "stale":
        return _stale_response(project)
    return _with_etag({"project": project}, 200)


@bp.delete("/api/v2/projects/<int:project_id>")
@login_required
def purge_project_v2(project_id: int) -> tuple[object, int]:
    if not valid_sqlite_id(project_id):
        return json_error("Project not found.", 404)
    expected = _requested_etag()
    if expected is None:
        return json_error("If-Match is required when permanently deleting a project.", 428, code="precondition_required")
    outcome, project = purge_project(current_user_id() or 0, project_id, expected_etag=expected)
    if outcome == "missing":
        return json_error("Project not found.", 404)
    if outcome == "stale":
        return _stale_response(project)
    return jsonify({"deleted": True}), 200


@bp.post("/api/projects")
@login_required
def create_project() -> tuple[object, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    try:
        name = _project_name(data.get("name"), default="Untitled Project")
    except ValueError as exc:
        return json_error(str(exc), field="name")
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
    if not valid_sqlite_id(project_id):
        return json_error("Project not found.", 404)
    project = get_project(current_user_id() or 0, project_id)
    if project is None:
        return json_error("Project not found.", 404)
    return _with_etag({"project": project}, 200)


@bp.put("/api/projects/<int:project_id>")
@login_required
def update_project(project_id: int) -> tuple[object, int]:
    if not valid_sqlite_id(project_id):
        return json_error("Project not found.", 404)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    try:
        name = _project_name(data.get("name")) if "name" in data else None
    except ValueError as exc:
        return json_error(str(exc), field="name")
    has_project_payload = "project" in data or "project_json" in data
    raw_project = data.get("project", data.get("project_json"))

    try:
        project = coerce_project_document(raw_project) if has_project_payload else None
    except ValueError as exc:
        return json_error(str(exc))

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
    if not valid_sqlite_id(project_id):
        return json_error("Project not found.", 404)
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
