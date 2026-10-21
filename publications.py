from __future__ import annotations

import re
import sqlite3
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from models import (
    create_publication,
    create_publication_revision,
    get_publication,
    get_user_by_id,
    list_publications,
    update_publication_metadata,
)
from utils import (
    MAX_SQLITE_INTEGER,
    coerce_project_document,
    current_user_id,
    json_error,
    login_required,
    valid_sqlite_id,
)

bp = Blueprint("publications", __name__)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")
DRIVE_PATH_PATTERN = re.compile(r"^[a-zA-Z]:")
RESERVED_SLUGS = {
    "about", "admin", "api", "cloud", "docs", "e", "edit", "example",
    "examples", "help", "library", "login", "mars", "new", "p", "public",
    "register", "remove", "settings", "teacher", "teachers", "webmars",
}
KINDS = {"example", "exercise", "solution", "lab", "library"}
DIFFICULTIES = {"beginner", "intermediate", "advanced", "mixed"}
PUBLIC_STATUSES = {"published", "unlisted"}
OWNER_STATUSES = PUBLIC_STATUSES | {"archived"}
SUPPORTED_FILE_EXTENSIONS = {".s", ".asm", ".mips", ".c", ".c0", ".h", ".h0", ".txt"}


def _text(value: Any, *, maximum: int, field: str, required: bool = False) -> str:
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value.strip()
    else:
        raise ValueError(f"{field} must be text.")
    if required and not result:
        raise ValueError(f"{field} is required.")
    if len(result) > maximum:
        raise ValueError(f"{field} is too long.")
    return result


def _normalize_metadata(data: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    nested = data.get("metadata")
    supplied = dict(nested) if isinstance(nested, dict) else {}
    supplied.update({key: value for key, value in data.items() if key != "metadata"})
    source = dict(existing or {})
    source.update(supplied)

    slug = _text(source.get("slug"), maximum=64, field="slug", required=True).lower()
    if not SLUG_PATTERN.fullmatch(slug) or slug in RESERVED_SLUGS:
        raise ValueError(
            "slug must use lowercase letters, numbers and single hyphens, and must not be reserved."
        )
    title = _text(
        source.get("title"),
        maximum=int(current_app.config["PUBLICATION_MAX_TITLE_LENGTH"]),
        field="title",
        required=True,
    )
    description = _text(
        source.get("description"),
        maximum=int(current_app.config["PUBLICATION_MAX_DESCRIPTION_LENGTH"]),
        field="description",
    )
    instructions = _text(
        source.get("instructions"),
        maximum=int(current_app.config["PUBLICATION_MAX_INSTRUCTIONS_LENGTH"]),
        field="instructions",
    )

    raw_tags = source.get("tags", [])
    if not isinstance(raw_tags, list):
        raise ValueError("tags must be an array.")
    tags: list[str] = []
    for raw_tag in raw_tags:
        tag = _text(raw_tag, maximum=32, field="tag").lower()
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > int(current_app.config["PUBLICATION_MAX_TAGS"]):
        raise ValueError("Too many tags.")

    language = _text(source.get("language", "en"), maximum=24, field="language").lower()
    if not LANGUAGE_PATTERN.fullmatch(language):
        raise ValueError("language must be a BCP 47-style language tag.")
    difficulty = _text(source.get("difficulty", "beginner"), maximum=24, field="difficulty").lower()
    if difficulty not in DIFFICULTIES:
        raise ValueError("difficulty is invalid.")
    kind = _text(source.get("kind", "example"), maximum=24, field="kind").lower()
    if kind not in KINDS:
        raise ValueError("kind is invalid.")
    license_name = _text(source.get("license", "unspecified"), maximum=64, field="license") or "unspecified"
    status = _text(source.get("status", "published"), maximum=16, field="status").lower()
    if status not in OWNER_STATUSES:
        raise ValueError("status is invalid.")
    return {
        "slug": slug,
        "title": title,
        "description": description,
        "instructions": instructions,
        "tags": tags,
        "language": language,
        "difficulty": difficulty,
        "kind": kind,
        "license": license_name,
        "status": status,
    }


def _safe_public_path(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Every public file path must be text.")
    raw = value.strip()
    if not raw or raw.startswith(("/", "\\")) or DRIVE_PATH_PATTERN.match(raw):
        raise ValueError("Every public file must use a relative path.")
    if "\x00" in raw or any(ord(character) < 32 for character in raw):
        raise ValueError("A public file path contains invalid characters.")
    normalized = raw.replace("\\", "/")
    segments = normalized.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise ValueError("A public file path contains an unsafe segment.")
    if len(segments) > int(current_app.config["PUBLICATION_MAX_PATH_DEPTH"]):
        raise ValueError("A public file path is nested too deeply.")
    if len(normalized) > 256:
        raise ValueError("A public file path is too long.")
    lowered = segments[-1].lower()
    extension = f".{lowered.rsplit('.', 1)[-1]}" if "." in lowered else ""
    if extension not in SUPPORTED_FILE_EXTENSIONS:
        raise ValueError("A public project contains an unsupported file type.")
    return normalized


def _coerce_public_project(payload: Any) -> dict[str, Any]:
    # Public interchange v1, emitted by webMARS itself, represents files as an
    # ordered array. Cloud project documents retain the older object mapping,
    # so only this publication boundary opts into accepting both shapes.
    source = coerce_project_document(payload, allow_file_list=True)
    if isinstance(source.get("project"), dict) and not source.get("files"):
        source = coerce_project_document(source["project"], allow_file_list=True)

    raw_files = source.get("files")
    entries: list[tuple[Any, Any, str]] = []
    if isinstance(raw_files, dict):
        entries = [(path, text, "") for path, text in raw_files.items()]
    elif isinstance(raw_files, list):
        for entry in raw_files:
            if not isinstance(entry, dict):
                raise ValueError("Every public file must be an object.")
            entries.append(
                (
                    entry.get("path", entry.get("name")),
                    entry.get("source", ""),
                    entry.get("id", ""),
                )
            )
    else:
        raise ValueError("A public project must contain files.")

    maximum_files = max(1, int(current_app.config["PUBLICATION_MAX_FILES"]))
    if not entries or len(entries) > maximum_files:
        raise ValueError(f"A public project must contain between 1 and {maximum_files} files.")
    maximum_file_bytes = max(1, int(current_app.config["PUBLICATION_MAX_FILE_BYTES"]))
    maximum_total_bytes = max(maximum_file_bytes, int(current_app.config["PUBLICATION_MAX_TOTAL_BYTES"]))
    files: list[dict[str, str]] = []
    used_paths: set[str] = set()
    id_to_path: dict[str, str] = {}
    total_bytes = 0
    for raw_path, raw_source, file_id in entries:
        path = _safe_public_path(raw_path)
        key = path.casefold()
        if key in used_paths:
            raise ValueError("Public file paths must be unique without case distinctions.")
        used_paths.add(key)
        if not isinstance(raw_source, str):
            raise ValueError("Every public file source must be text.")
        text = raw_source
        if "\x00" in text:
            raise ValueError("Public files must contain text, not NUL bytes.")
        byte_length = len(text.encode("utf-8"))
        if byte_length > maximum_file_bytes:
            raise ValueError("A public file is too large.")
        total_bytes += byte_length
        if total_bytes > maximum_total_bytes:
            raise ValueError("The public project is too large.")
        files.append({"path": path, "source": text})
        if file_id and not isinstance(file_id, str):
            raise ValueError("Every public file id must be text.")
        if file_id:
            id_to_path[file_id] = path

    active_path_value = source.get("activeFilePath", source.get("activeFile", ""))
    if not active_path_value and source.get("activeFileId"):
        active_path_value = id_to_path.get(str(source["activeFileId"]), "")
    try:
        active_path = _safe_public_path(active_path_value) if active_path_value else files[0]["path"]
    except ValueError:
        active_path = files[0]["path"]
    if active_path.casefold() not in used_paths:
        active_path = files[0]["path"]

    name = _text(source.get("name", "public-project"), maximum=120, field="project name") or "public-project"
    description = _text(source.get("description", ""), maximum=2000, field="project description")
    # This is a deliberately small, inert interchange format.  Remote IDs,
    # cloud addresses, preferences, window layout, state slots and timestamps
    # are never stored in a publication revision.
    return {
        "kind": "webmars-public-project",
        "version": 1,
        "name": name,
        "description": description,
        "files": files,
        "activeFilePath": active_path,
    }


def _project_from(data: dict[str, Any]) -> dict[str, Any]:
    if "project" not in data:
        raise ValueError("project is required.")
    return _coerce_public_project(data.get("project"))


def _webmars_version(data: dict[str, Any]) -> str:
    return _text(data.get("webmarsVersion"), maximum=32, field="webmarsVersion")


def _verified_owner() -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    user = get_user_by_id(current_user_id() or 0)
    if user is None:
        return None, json_error("Authentication required.", 401)
    if current_app.config.get("REQUIRE_VERIFIED_EMAIL_FOR_PUBLISH") and not user["emailVerified"]:
        return None, json_error(
            "Confirm your email address before publishing.",
            403,
            code="email_not_verified",
        )
    return user, None


def _list_window() -> tuple[int, int]:
    maximum = max(1, int(current_app.config["PUBLICATION_LIST_LIMIT"]))
    limit = min(maximum, max(1, int(request.args.get("limit", maximum))))
    offset = max(0, int(request.args.get("offset", 0)))
    if offset > MAX_SQLITE_INTEGER:
        raise ValueError("offset is too large.")
    return limit, offset


@bp.get("/api/publications")
def browse_publications() -> tuple[object, int]:
    query = str(request.args.get("q", "")).strip()
    try:
        limit, offset = _list_window()
    except (TypeError, ValueError):
        return json_error("limit and offset must be integers.")
    page = list_publications(query=query, limit=limit + 1, offset=offset)
    return jsonify({
        "publications": page[:limit],
        "limit": limit,
        "offset": offset,
        "hasMore": len(page) > limit,
    }), 200


@bp.get("/api/publications/mine")
@login_required
def own_publications() -> tuple[object, int]:
    try:
        limit, offset = _list_window()
    except (TypeError, ValueError):
        return json_error("limit and offset must be integers.")
    page = list_publications(
        limit=limit + 1,
        offset=offset,
        owner_id=current_user_id(),
        query=str(request.args.get("q", "")).strip(),
    )
    return jsonify({
        "publications": page[:limit],
        "limit": limit,
        "offset": offset,
        "hasMore": len(page) > limit,
    }), 200


@bp.get("/api/publications/<slug>")
def read_publication(slug: str) -> tuple[object, int]:
    publication = get_publication(slug)
    if publication is None:
        return json_error("Publication not found.", 404)
    response = jsonify({"publication": publication})
    response.headers["Cache-Control"] = "public, max-age=60"
    return response, 200


@bp.get("/api/publications/<slug>/revisions/<int:revision>")
def read_publication_revision(slug: str, revision: int) -> tuple[object, int]:
    if not valid_sqlite_id(revision):
        return json_error("Publication not found.", 404)
    publication = get_publication(slug, revision)
    if publication is None:
        return json_error("Publication not found.", 404)
    response = jsonify({"publication": publication})
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response, 200


@bp.post("/api/publications")
@login_required
def publish() -> tuple[object, int]:
    user, error = _verified_owner()
    if error is not None:
        return error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    try:
        metadata = _normalize_metadata(data)
        if metadata["status"] not in PUBLIC_STATUSES:
            raise ValueError("A new publication must be published or unlisted.")
        project = _project_from(data)
        publication = create_publication(
            int(user["id"]), metadata, project, _webmars_version(data)
        )
    except ValueError as error_value:
        return json_error(str(error_value))
    except sqlite3.IntegrityError:
        return json_error(
            "That public name has already been claimed on this BackMars installation.",
            409,
            field="slug",
            code="slug_taken",
        )
    response = jsonify({"publication": publication})
    response.headers["Location"] = f"/api/publications/{publication['slug']}"
    return response, 201


@bp.post("/api/publications/<slug>/revisions")
@login_required
def publish_revision(slug: str) -> tuple[object, int]:
    user, error = _verified_owner()
    if error is not None:
        return error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    try:
        project = _project_from(data)
        outcome, publication = create_publication_revision(
            int(user["id"]), slug, project, _webmars_version(data)
        )
    except ValueError as error_value:
        return json_error(str(error_value))
    if outcome == "missing":
        return json_error("Publication not found.", 404)
    if outcome == "forbidden":
        return json_error("Only the owner can publish a revision.", 403)
    if outcome == "archived":
        return json_error("Restore the publication before adding a revision.", 409)
    return jsonify({"publication": publication}), 201


@bp.put("/api/publications/<slug>")
@login_required
def edit_publication(slug: str) -> tuple[object, int]:
    user, error = _verified_owner()
    if error is not None:
        return error
    user_id = int(user["id"])
    existing = get_publication(slug, include_archived_for_owner=user_id)
    if existing is None:
        # This deliberately does not reveal whether another user owns a private row.
        return json_error("Publication not found.", 404)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("A JSON object is required.")
    try:
        metadata = _normalize_metadata(data, existing)
    except ValueError as error_value:
        return json_error(str(error_value))
    if metadata["slug"] != existing["slug"].lower():
        return json_error("A public name is permanent and cannot be changed.", field="slug")
    outcome, publication = update_publication_metadata(user_id, slug, metadata)
    if outcome == "forbidden":
        return json_error("Publication not found.", 404)
    return jsonify({"publication": publication}), 200


@bp.delete("/api/publications/<slug>")
@login_required
def archive_publication(slug: str) -> tuple[object, int]:
    user_id = current_user_id() or 0
    existing = get_publication(slug, include_archived_for_owner=user_id)
    if existing is None:
        return json_error("Publication not found.", 404)
    existing["status"] = "archived"
    metadata = _normalize_metadata(existing, existing)
    outcome, publication = update_publication_metadata(user_id, slug, metadata)
    if outcome != "updated":
        return json_error("Publication not found.", 404)
    return jsonify({"publication": publication, "archived": True}), 200
