"""What a world supplies to the engine: its types, their rules, and its seed records."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
from typing import Any

from .rules import RuleError, _mentions, named_fields, parse
from .schema import Types

OPERATIONS = ("read", "create", "update", "delete")
FIELD_TYPES = ("text", "enum", "integer", "boolean", "date", "reference", "list")


class Definition:
    """One world, as data. The engine supplies storage, validation, versions, and history.

    Each type has `rules` for read, create, update, and delete, and may have
    `constraints`. A field may have a `write_rule` saying who can change it.
    All are expressions in the language of `world.rules`, so the engine
    enforces exactly what a client is shown.

    The update rule is checked against the saved record and again against the
    proposed one. A constraint is a dict with `check`, the expression that must
    hold, and `message`. It may give `fields`, an error `code` and `status`,
    and `on`, the operations it applies to (create and update by default).
    The engine also re-checks the constraints of every record that refers to a
    changed one, so a constraint between two records is written once.

    The engine itself rejects a reference to a missing record and the deletion
    of a record that others reference.

    `rules` may give `define`, who may change the definition while the world runs. It is judged
    with `actor` alone. Without it, no one may.
    """

    name: str  # short identifier, saved in the database
    title: str
    version: str
    types: Types
    actor_type = "Person"
    actors: tuple[str, ...] = ()  # seeded actors that receive access tokens
    seed_reason = "Initial records."
    rules: dict[str, str] = {}

    def seed(self, today: date) -> list[tuple[str, str, dict[str, Any]]]:
        return []

    def check(self) -> None:
        """Parse every rule, so that a mistake fails when the world loads, not on a request."""
        names = tuple(self.types)
        for name in (*names, *(field for definition in self.types.values() for field in definition["fields"])):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
                raise RuleError(f"{name!r} is not a usable type or field name. Use letters, digits, and underscores.")
        if self.actor_type not in self.types:
            raise RuleError(f"The people who act are {self.actor_type!r}, which is not one of the types.")
        for type_name, definition in self.types.items():
            try:
                if not isinstance(definition, dict) or not isinstance(definition.get("fields"), dict):
                    raise RuleError("its fields must be an object of field names to field specs.")
                for field, spec in definition["fields"].items():
                    self._check_field(field, spec)
                for operation in OPERATIONS:
                    parse(definition["rules"][operation], names)
                    self._check_names(definition["rules"][operation], type_name, f"The {operation} rule")
                for field in definition["fields"].values():
                    if "write_rule" in field:
                        parse(field["write_rule"], names)
                        self._check_names(field["write_rule"], type_name, "A write rule")
                for constraint in definition.get("constraints", []):
                    parse(constraint["check"], names)
                    self._check_names(constraint["check"], type_name, "A constraint")
                    if not set(constraint.get("on", ())) <= {"create", "update", "delete"}:
                        raise RuleError("A constraint applies on create, update, or delete.")
            except (KeyError, TypeError) as error:
                raise RuleError(f"{type_name} is missing {error}.") from error
            except RuleError as error:
                raise RuleError(f"{type_name}: {error}") from error
        if not isinstance(self.rules, dict) or set(self.rules) - {"define"}:
            raise RuleError("The world's rules may give only define, who may change the definition.")
        if "define" in self.rules:
            tree = parse(self.rules["define"], names)
            if _mentions(tree, "record") or _mentions(tree, "saved"):
                raise RuleError("The define rule is judged with actor alone, so it cannot name record or saved.")
            self._check_names(self.rules["define"], self.actor_type, "The define rule")

    def _check_names(self, expression: str, type_name: str, what: str) -> None:
        """Refuse a rule that names a field no type has, which would otherwise fail only when judged."""
        for named_type, field in sorted(named_fields(expression, self.types, type_name, self.actor_type)):
            if field not in self.types[named_type]["fields"]:
                raise RuleError(f"{what} reads {named_type}.{field}, which is not a field: {expression}")

    def as_data(self) -> dict[str, Any]:
        """The definition as JSON data, without seed records: what a world stores and changes while it runs."""
        return json.loads(json.dumps({
            "name": self.name, "title": self.title, "version": self.version, "actor_type": self.actor_type,
            "rules": self.rules, "types": self.types}))

    def _check_field(self, field: str, spec: Any, inside: str = "") -> None:
        """Refuse a field the engine would not understand, naming what is wrong with it."""
        where = f"field {inside or field}"
        if not isinstance(spec, dict) or spec.get("type") not in FIELD_TYPES:
            raise RuleError(f"{where} needs a type, one of {', '.join(FIELD_TYPES)}.")
        if spec["type"] == "enum" and (not isinstance(spec.get("values"), list) or not spec["values"]
                                       or not all(isinstance(value, str) for value in spec["values"])):
            raise RuleError(f"{where} is an enum, so it needs values, a list of strings.")
        if spec["type"] == "reference" and spec.get("target") not in self.types:
            raise RuleError(f"{where} refers to {spec.get('target')!r}, which is not one of the types.")
        if spec["type"] == "list":
            if "items" not in spec:
                raise RuleError(f"{where} is a list, so it needs items, the spec of each item.")
            self._check_field(field, spec["items"], f"{field}'s items")


class DataDefinition(Definition):
    """A world written as JSON rather than Python: the same types and rules, with its seed records listed.

        {"name": "clinic", "title": "...", "version": "1", "actor_type": "Person",
         "rules": {"define": "actor.role == 'admin'"}, "types": {...}, "records": [{"type": "Person", "id": "okafor", "data": {...}}, ...]}

    Every record of the actor type is a person who can be given access. A record comes after the
    records it refers to.
    """

    def __init__(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise RuleError("A world is a JSON object with name, title, version, types, and records.")
        missing = [key for key in ("name", "title", "types") if key not in data]
        if missing:
            raise RuleError(f"The world is missing {', '.join(missing)}.")
        unknown = set(data) - {"name", "title", "version", "actor_type", "rules", "types", "records", "seed_reason"}
        if unknown:
            raise RuleError(f"The world has keys the engine does not know: {', '.join(sorted(unknown))}.")
        if not isinstance(data["name"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", data["name"]):
            raise RuleError("name is a short identifier in lower case, such as clinic.")
        if not isinstance(data["types"], dict) or not data["types"]:
            raise RuleError("types is an object of type names to type definitions.")
        records = data.get("records", [])
        if not isinstance(records, list) or not all(
                isinstance(record, dict) and set(record) == {"type", "id", "data"} and isinstance(record["data"], dict)
                for record in records):
            raise RuleError('records is a list of {"type": TYPE, "id": ID, "data": {FIELD: VALUE}}.')
        self.name, self.title, self.version = data["name"], str(data["title"]), str(data.get("version", "1"))
        self.types = data["types"]
        self.actor_type = data.get("actor_type", "Person")
        self.rules = data.get("rules", {})
        self.seed_reason = data.get("seed_reason", self.seed_reason)
        self.records = records
        self.actors = tuple(record["id"] for record in records if record["type"] == self.actor_type)

    def seed(self, today: date) -> list[tuple[str, str, dict[str, Any]]]:
        return [(record["type"], record["id"], record["data"]) for record in self.records]


def load_json(path: str | Path) -> DataDefinition:
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as error:
        raise RuleError(f"The world is not valid JSON: {error}.") from None
    return DataDefinition(data)

