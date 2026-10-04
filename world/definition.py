"""Load the deliberately small vocabulary used by the two World experiments."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import re
from typing import Any

from .schema import WorldError, references, validate_data, validate_value


DEFAULT_DEFINITION = Path(__file__).resolve().parents[1] / "definitions" / "team.world.json"
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _invalid(message: str) -> None:
    raise WorldError(400, "invalid_definition", message)


def _keys(value: Any, allowed: set[str], required: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        _invalid(f"{label} contains unsupported keys or is missing required keys.")


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{label} must be nonempty text.")


class WorldDefinition:
    """A validated JSON definition. Policies are fixed operations, never code."""

    def __init__(self, document: dict[str, Any]) -> None:
        _keys(document, {"id", "name", "version", "actor_type", "types", "seed"},
              {"id", "name", "version", "actor_type", "types", "seed"}, "World definition")
        for key in ("id", "name", "version", "actor_type"):
            _text(document[key], key)
        if not _RECORD_ID.fullmatch(document["id"]):
            _invalid("The definition id must be a stable identifier.")
        self.id = document["id"]
        self.name = document["name"]
        self.version = document["version"]
        self.actor_type = document["actor_type"]
        self.types = deepcopy(document["types"])
        self._seed = deepcopy(document["seed"])
        self._validate()

    @classmethod
    def load(cls, path: str | Path) -> WorldDefinition:
        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    _invalid(f"Duplicate definition key: {key}.")
                result[key] = value
            return result

        def constant(_):
            _invalid("A definition must contain finite JSON values.")

        try:
            document = json.loads(
                Path(path).expanduser().read_text(), object_pairs_hook=object_pairs,
                parse_constant=constant,
            )
        except (OSError, ValueError, RecursionError) as error:
            raise WorldError(400, "invalid_definition", "The world definition must be a readable JSON file.") from error
        return cls(document)

    @classmethod
    def default(cls) -> WorldDefinition:
        return cls.load(DEFAULT_DEFINITION)

    def field_at_path(self, type_name: str, path: str) -> dict[str, Any]:
        """Resolve descriptors along references, with no expression language."""
        if not isinstance(path, str) or not path or len(path.split(".")) > 8:
            _invalid("A relationship path must name one to eight fields.")
        parts = path.split(".")
        current = type_name
        for index, part in enumerate(parts):
            fields = self.types[current]["fields"]
            if part not in fields:
                _invalid(f"Unknown field in relationship path: {type_name}.{path}.")
            descriptor = fields[part]
            if index == len(parts) - 1:
                return descriptor
            if descriptor["type"] != "reference":
                _invalid("Relationship paths can only traverse reference fields.")
            current = descriptor["target"]
        raise AssertionError("A validated path has a final field")

    def scope_type(self, type_name: str) -> str | None:
        scope = self.types[type_name]["policy"]["scope"]
        if scope is None:
            return None
        if scope == "":
            return type_name
        descriptor = self.field_at_path(type_name, scope)
        if descriptor["type"] != "reference":
            _invalid("A scope path must resolve to a record reference.")
        return descriptor["target"]

    def _field(self, field: str, spec: Any) -> None:
        if not isinstance(field, str) or not _IDENTIFIER.fullmatch(field):
            _invalid(f"Invalid field name: {field}.")
        common = {"type", "required", "nullable", "default", "description", "write_rule"}
        vocabulary = {
            "text": {"min_length", "max_length"}, "enum": {"values"},
            "date": set(), "reference": {"target"},
            "list": {"items", "unique", "min_items", "max_items"},
            "integer": {"minimum", "maximum"},
        }
        if not isinstance(spec, dict) or not isinstance(spec.get("type"), str) or spec["type"] not in vocabulary:
            _invalid(f"Unsupported field type for {field}.")
        kind = spec["type"]
        _keys(spec, common | vocabulary[kind], {"type"}, f"Field {field}")
        for key in ("required", "nullable", "unique"):
            if key in spec and type(spec[key]) is not bool:
                _invalid(f"{field}.{key} must be a boolean.")
        for key in ("description", "write_rule"):
            if key in spec:
                _text(spec[key], f"{field}.{key}")
        for key in ("minimum", "maximum", "min_length", "max_length", "min_items", "max_items"):
            if key in spec and (type(spec[key]) is not int or (key not in {"minimum", "maximum"} and spec[key] < 0)):
                _invalid(f"{field}.{key} must be an integer in the supported range.")
        for lower, upper in (("minimum", "maximum"), ("min_length", "max_length"), ("min_items", "max_items")):
            if lower in spec and upper in spec and spec[lower] > spec[upper]:
                _invalid(f"{field} has an inverted minimum and maximum.")
        if kind == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values) or len(values) != len(set(values)):
                _invalid(f"{field} must declare unique enum strings.")
        elif kind == "reference":
            if not isinstance(spec.get("target"), str) or spec["target"] not in self.types:
                _invalid(f"{field} must reference a declared type.")
        elif kind == "list":
            if not isinstance(spec.get("items"), dict) or spec["items"].get("type") != "reference":
                _invalid("This experiment supports lists of record references only.")
            self._field(field, spec["items"])
            if set(spec["items"]) - {"type", "target"}:
                _invalid("List reference items accept type and target only.")
        if "default" in spec:
            default = spec["default"]
            if default == "$actor":
                if kind != "reference" or spec["target"] != self.actor_type:
                    _invalid("$actor is only supported as an actor reference default.")
            else:
                try:
                    validate_value(field, default, spec)
                except WorldError as error:
                    _invalid(f"Invalid default for {field}: {error.message}")

    def _validate(self) -> None:
        if not isinstance(self.types, dict) or not self.types or self.actor_type not in self.types:
            _invalid("A definition must declare types and its actor type.")
        for type_name, spec in self.types.items():
            if not isinstance(type_name, str) or not _IDENTIFIER.fullmatch(type_name):
                _invalid(f"Invalid type name: {type_name}.")
            _keys(spec, {"description", "fields", "rules", "policy", "constraints", "transitions"}, {"fields", "policy"}, f"Type {type_name}")
            if "description" in spec:
                _text(spec["description"], f"{type_name}.description")
            if not isinstance(spec["fields"], dict) or not spec["fields"]:
                _invalid(f"{type_name} must declare fields.")
            for field, field_spec in spec["fields"].items():
                self._field(field, field_spec)
            rules = spec.get("rules", {})
            if not isinstance(rules, dict) or set(rules) - {"read", "create", "update", "delete"} or any(not isinstance(rule, str) for rule in rules.values()):
                _invalid("Rule descriptions must describe supported operations as text.")
        for type_name, spec in self.types.items():
            self._policy(type_name, spec["policy"])
            self._constraints(type_name, spec.get("constraints", []))
            self._transitions(type_name, spec.get("transitions", []))
        actor = self.types[self.actor_type]
        role = actor["fields"].get("role", {})
        if role.get("type") != "enum" or set(role.get("values", [])) != {"admin", "member", "viewer"} or role.get("nullable"):
            _invalid("The actor type must declare the admin, member, and viewer role enum.")
        if not (role.get("required") or "default" in role) or "role" not in actor["policy"]["admin_fields"] or actor["policy"]["create"] != "admin":
            _invalid("Actor roles must be present and writable only by admins.")
        self._validate_seed()

    def _policy(self, type_name: str, policy: Any) -> None:
        keys = {"scope", "read", "create", "update", "delete", "admin_fields", "author_field"}
        _keys(policy, keys, keys - {"author_field"}, f"{type_name}.policy")
        if any(not isinstance(policy[key], str) for key in ("read", "create", "update", "delete")) or policy["read"] not in {"authenticated", "scope"} or policy["create"] not in {"admin", "member"} or policy["update"] not in {"admin", "member", "self"} or policy["delete"] != "admin":
            _invalid("A policy uses an unsupported operation rule.")
        scope = policy["scope"]
        if scope is not None and not isinstance(scope, str):
            _invalid("A scope must be null, the current record, or a reference path.")
        fields = self.types[type_name]["fields"]
        protected = policy["admin_fields"]
        if not isinstance(protected, list) or any(not isinstance(field, str) or field not in fields for field in protected) or len(protected) != len(set(protected)):
            _invalid("admin_fields must name unique fields of this record.")
        scope_type = self.scope_type(type_name)
        if (policy["read"] == "scope" or policy["create"] == "member" or policy["update"] == "member") and scope_type is None:
            _invalid("Scoped reads and member mutations require a scope.")
        if policy["update"] == "self" and type_name != self.actor_type:
            _invalid("The self update rule is only supported for the actor type.")
        if scope_type is not None:
            scope_fields = self.types[scope_type]["fields"]
            members = scope_fields.get("members", {})
            visibility = scope_fields.get("visibility", {})
            if members.get("type") != "list" or members.get("items", {}).get("target") != self.actor_type or not members.get("required") or members.get("nullable"):
                _invalid("A scope must declare required members as actor references.")
            if visibility.get("type") != "enum" or set(visibility.get("values", [])) != {"team", "private"} or visibility.get("nullable") or not (visibility.get("required") or "default" in visibility):
                _invalid("A scope must declare team/private visibility.")
        if "author_field" in policy:
            author = policy["author_field"]
            descriptor = fields.get(author, {}) if isinstance(author, str) else {}
            if descriptor.get("type") != "reference" or descriptor.get("target") != self.actor_type or author not in protected:
                _invalid("author_field must name an admin-protected actor reference.")

    def _constraints(self, type_name: str, constraints: Any) -> None:
        if not isinstance(constraints, list):
            _invalid("Constraints must be a list of supported relationship checks.")
        for constraint in constraints:
            if not isinstance(constraint, dict):
                _invalid("A constraint must be an object.")
            kind = constraint.get("kind")
            if kind == "member_of":
                keys = {"kind", "field", "members", "nullable"}
                _keys(constraint, keys, keys - {"nullable"}, "member_of constraint")
                field = self.field_at_path(type_name, constraint["field"])
                members = self.field_at_path(type_name, constraint["members"])
                if field["type"] != "reference" or members["type"] != "list" or field["target"] != members["items"]["target"]:
                    _invalid("member_of requires a reference and a list of references to the same type.")
            elif kind == "same_reference":
                keys = {"kind", "left", "right", "nullable"}
                _keys(constraint, keys, keys - {"nullable"}, "same_reference constraint")
                left = self.field_at_path(type_name, constraint["left"])
                right = self.field_at_path(type_name, constraint["right"])
                if left["type"] != "reference" or right["type"] != "reference" or left["target"] != right["target"]:
                    _invalid("same_reference requires references to the same record type.")
            else:
                _invalid("This constraint kind is not supported.")
            if "nullable" in constraint and type(constraint["nullable"]) is not bool:
                _invalid("A constraint's nullable flag must be a boolean.")

    def _transitions(self, type_name: str, transitions: Any) -> None:
        if not isinstance(transitions, list):
            _invalid("Transitions must be a list.")
        declared = set()
        for transition in transitions:
            _keys(transition, {"field", "allowed"}, {"field", "allowed"}, "Transition")
            field = transition["field"]
            descriptor = self.types[type_name]["fields"].get(field, {}) if isinstance(field, str) else {}
            if descriptor.get("type") != "enum" or descriptor.get("nullable") or not (descriptor.get("required") or "default" in descriptor) or field in declared:
                _invalid("Each transition must name a unique, present, nonnullable enum field.")
            declared.add(field)
            allowed = transition["allowed"]
            values = descriptor["values"]
            if not isinstance(allowed, dict) or set(allowed) != set(values):
                _invalid("Transitions must declare the allowed next values for every enum value.")
            for successors in allowed.values():
                if not isinstance(successors, list) or any(not isinstance(value, str) or value not in values for value in successors) or len(successors) != len(set(successors)):
                    _invalid("Allowed transitions must contain unique known enum values.")

    def _validate_seed(self) -> None:
        if not isinstance(self._seed, list) or not self._seed:
            _invalid("A definition must seed its actors and initial records.")
        identities = []
        seen = set()
        for record in self._seed:
            _keys(record, {"type", "id", "data"}, {"type", "id", "data"}, "Seed record")
            if not isinstance(record["type"], str) or record["type"] not in self.types or not isinstance(record["id"], str) or not _RECORD_ID.fullmatch(record["id"]):
                _invalid("A seed must use a declared type and a valid record ID.")
            key = record["type"], record["id"]
            if key in seen:
                _invalid("Seed records must have unique typed IDs.")
            seen.add(key)
            if record["type"] == self.actor_type:
                identities.append(record["id"])
        self.actor_ids = tuple(identities)
        if not identities:
            _invalid("The seed must include authenticated actors.")
        normalized = []
        try:
            for type_name, record_id, data in self.seed_records(date(2000, 1, 1)):
                data = validate_data(type_name, data, types=self.types, actor_id=identities[0])
                normalized.append((type_name, record_id, data))
                for _, target, target_id in references(type_name, data, types=self.types):
                    if (target, target_id) not in seen:
                        _invalid("Seed relationships must refer to seeded records.")
        except (WorldError, OverflowError) as error:
            _invalid(f"Invalid seed data: {error}")
        if not any(type_name == self.actor_type and data["role"] == "admin" for type_name, _, data in normalized):
            _invalid("The seed must include at least one admin.")

    def seed_records(self, today: date) -> list[tuple[str, str, dict[str, Any]]]:
        records = []
        for record in self._seed:
            data = deepcopy(record["data"])
            if not isinstance(data, dict):
                _invalid("Seed data must be a JSON object.")
            for field, value in data.items():
                if isinstance(value, dict) and set(value) == {"relative_days"}:
                    descriptor = self.types[record["type"]]["fields"].get(field, {})
                    if descriptor.get("type") != "date" or type(value["relative_days"]) is not int:
                        _invalid("relative_days is only supported for date seeds.")
                    data[field] = (today + timedelta(days=value["relative_days"])).isoformat()
            records.append((record["type"], record["id"], data))
        return records
