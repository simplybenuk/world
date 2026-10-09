"""Validation of JSON records against a world's types. No client presentation lives here."""

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


Types = dict[str, dict[str, Any]]


def require_type(types: Types, type_name: str) -> dict[str, Any]:
    if not isinstance(type_name, str) or type_name not in types:
        raise WorldError(400, "unknown_type", "Unknown record type.", {"type": type_name, "types": list(types)})
    return types[type_name]


def _invalid(field: str, message: str) -> None:
    raise WorldError(422, "invalid_field", message, {"field": field})


def validate_value(field: str, value: Any, spec: dict[str, Any]) -> None:
    """Check a field's JSON shape. The store validates references separately."""
    if value is None:
        if spec.get("nullable"):
            return
        _invalid(field, f"{field} cannot be null.")
    kind = spec["type"]
    if kind == "integer":
        if type(value) is not int:
            _invalid(field, f"{field} must be a whole number.")
        if value < spec.get("min", -2**53) or value > spec.get("max", 2**53):
            _invalid(field, f"{field} is outside the allowed range.")
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
    types: Types, type_name: str, data: Any, *, partial: bool = False,
    actor_id: str | None = None,
) -> dict[str, Any]:
    fields = require_type(types, type_name)["fields"]
    if not isinstance(data, dict):
        raise WorldError(422, "invalid_data", "Record data must be a JSON object.")
    unknown = set(data) - set(fields)
    if unknown:
        raise WorldError(
            422, "unknown_field", "Record data contains unknown fields.",
            {"fields": sorted(str(field) for field in unknown), "allowed": list(fields)},
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
                raise WorldError(422, "required_field", f"{field} is required.", {"field": field, "spec": spec})
            else:
                continue
        validate_value(field, validated[field], spec)
    return validated


def references(
    types: Types, type_name: str, data: dict[str, Any],
) -> Iterator[tuple[str, str, str]]:
    """Yield field paths, target types, and record IDs for every relationship."""
    for field, spec in require_type(types, type_name)["fields"].items():
        value = data.get(field)
        if value is None:
            continue
        if spec["type"] == "reference":
            yield field, spec["target"], value
        elif spec["type"] == "list" and spec["items"]["type"] == "reference":
            for index, record_id in enumerate(value):
                yield f"{field}[{index}]", spec["items"]["target"], record_id
