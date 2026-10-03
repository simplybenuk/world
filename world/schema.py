"""JSON record definitions and validation. No client presentation lives here."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import re
from typing import Any, Iterator


class WorldError(Exception):
    """An error that a protocol adapter can return without guessing a status."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


TYPE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "Person": {
        "description": "A person whose stored role governs their access.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "email": {"type": "text", "nullable": True, "default": None, "max_length": 254},
            "role": {
                "type": "enum",
                "required": True,
                "values": ["admin", "member", "viewer"],
                "default": "member",
                "write_rule": "actor.role == admin",
            },
        },
        "rules": {
            "read": "actor is authenticated",
            "create": "actor.role == admin",
            "update": "actor.role == admin or (actor.role == member and actor.id == record.id); only admin can change role",
            "delete": "actor.role == admin and no live record references this person and another admin remains",
        },
    },
    "Project": {
        "description": "A project containing tasks and a list of team members.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "description": {"type": "text", "default": "", "max_length": 20000},
            "members": {
                "type": "list",
                "required": True,
                "items": {"type": "reference", "target": "Person"},
                "unique": True,
                "max_items": 1000,
                "write_rule": "actor.role == admin",
            },
            "visibility": {
                "type": "enum",
                "required": True,
                "values": ["team", "private"],
                "default": "team",
                "write_rule": "actor.role == admin",
            },
        },
        "rules": {
            "read": "actor.role == admin or record.visibility == team or actor.id in record.members",
            "create": "actor.role == admin",
            "update": "actor.role == admin or (actor.role == member and actor.id in record.members); only admin can change members or visibility",
            "delete": "actor.role == admin and no live record references this project",
        },
    },
    "Task": {
        "description": "Work belonging to one project, optionally assigned to a project member.",
        "fields": {
            "title": {"type": "text", "required": True, "min_length": 1, "max_length": 300},
            "description": {"type": "text", "default": "", "max_length": 20000},
            "project": {"type": "reference", "target": "Project", "required": True},
            "owner": {"type": "reference", "target": "Person", "nullable": True, "default": None},
            "status": {
                "type": "enum",
                "required": True,
                "values": ["todo", "doing", "done"],
                "default": "todo",
            },
            "due": {"type": "date", "nullable": True, "default": None},
        },
        "constraints": ["owner is null or owner belongs to project.members"],
        "rules": {
            "read": "actor can read record.project",
            "create": "actor.role == admin or (actor.role == member and actor.id in project.members)",
            "update": "actor.role == admin or (actor.role == member and actor.id in current project.members and actor.id in proposed project.members)",
            "delete": "actor.role == admin and no live record references this task",
        },
    },
    "Comment": {
        "description": "A comment attached to a task, attributed to a member of its project.",
        "fields": {
            "body": {"type": "text", "required": True, "min_length": 1, "max_length": 20000},
            "task": {"type": "reference", "target": "Task", "required": True},
            "author": {
                "type": "reference",
                "target": "Person",
                "required": True,
                "default": "$actor",
                "write_rule": "actor.role == admin or new record.author == actor.id; only admin can change an existing author",
            },
        },
        "constraints": ["author belongs to task.project.members"],
        "rules": {
            "read": "actor can read record.task.project",
            "create": "actor.role == admin or (actor.role == member and actor.id in task.project.members and record.author == actor.id)",
            "update": "actor.role == admin or (actor.role == member and actor.id in current task.project.members and actor.id in proposed task.project.members); only admin can change author",
            "delete": "actor.role == admin",
        },
    },
}

WORLD_NAME = "World"
SCHEMA_VERSION = "0.1.0"


def require_type(type_name: str) -> dict[str, Any]:
    if not isinstance(type_name, str) or type_name not in TYPE_DEFINITIONS:
        raise WorldError(400, "unknown_type", "Unknown record type.", {"type": type_name})
    return TYPE_DEFINITIONS[type_name]


def _invalid(field: str, message: str) -> None:
    raise WorldError(422, "invalid_field", message, {"field": field})


def validate_value(field: str, value: Any, spec: dict[str, Any]) -> None:
    """Check a field's JSON shape. The store validates references separately."""
    if value is None:
        if spec.get("nullable"):
            return
        _invalid(field, f"{field} cannot be null.")
    kind = spec["type"]
    if kind in ("text", "enum", "date", "reference"):
        if not isinstance(value, str):
            _invalid(field, f"{field} must be a string.")
        if len(value) > spec.get("max_length", 20000):
            _invalid(field, f"{field} is too long.")
    if kind == "text":
        if spec.get("min_length", 0) and not value.strip():
            _invalid(field, f"{field} must not be blank.")
    elif kind == "enum":
        if value not in spec["values"]:
            raise WorldError(
                422, "invalid_enum", f"{field} is not an allowed value.",
                {"field": field, "values": spec["values"]},
            )
    elif kind == "date":
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            _invalid(field, f"{field} must be a real date in YYYY-MM-DD format.")
        if parsed.isoformat() != value:
            _invalid(field, f"{field} must use YYYY-MM-DD format.")
    elif kind == "reference":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            _invalid(field, f"{field} must contain a record ID.")
    elif kind == "list":
        if not isinstance(value, list):
            _invalid(field, f"{field} must be a list.")
        if len(value) > spec.get("max_items", 1000):
            _invalid(field, f"{field} has too many items.")
        for index, item in enumerate(value):
            validate_value(f"{field}[{index}]", item, spec["items"])
        if spec.get("unique") and len(value) != len(set(value)):
            _invalid(field, f"{field} must not contain duplicate references.")


def validate_data(
    type_name: str, data: Any, *, partial: bool = False, actor_id: str | None = None
) -> dict[str, Any]:
    fields = require_type(type_name)["fields"]
    if not isinstance(data, dict):
        raise WorldError(422, "invalid_data", "Record data must be a JSON object.")
    unknown = set(data) - set(fields)
    if unknown:
        raise WorldError(
            422, "unknown_field", "Record data contains unknown fields.",
            {"fields": sorted(str(field) for field in unknown)},
        )
    validated = deepcopy(data)
    for field, spec in fields.items():
        if field not in validated:
            if partial:
                continue
            if "default" in spec:
                default = spec["default"]
                validated[field] = actor_id if default == "$actor" else deepcopy(default)
            elif spec.get("required"):
                raise WorldError(422, "required_field", f"{field} is required.", {"field": field})
            else:
                continue
        validate_value(field, validated[field], spec)
    return validated


def references(type_name: str, data: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    """Yield field paths, target types, and record IDs for every relationship."""
    for field, spec in require_type(type_name)["fields"].items():
        value = data.get(field)
        if value is None:
            continue
        if spec["type"] == "reference":
            yield field, spec["target"], value
        elif spec["type"] == "list" and spec["items"]["type"] == "reference":
            for index, record_id in enumerate(value):
                yield f"{field}[{index}]", spec["items"]["target"], record_id
