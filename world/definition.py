"""What a world supplies to the engine: its types, their rules, and its seed records."""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from .rules import RuleError, parse
from .schema import Types

OPERATIONS = ("read", "create", "update", "delete")


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
    """

    name: str  # short identifier, saved in the database
    title: str
    version: str
    types: Types
    actor_type = "Person"
    actors: tuple[str, ...] = ()  # seeded actors that receive access tokens
    seed_reason = "Initial records."

    def seed(self, today: date) -> list[tuple[str, str, dict[str, Any]]]:
        return []

    def check(self) -> None:
        """Parse every rule, so that a mistake fails when the world loads, not on a request."""
        names = tuple(self.types)
        for name in (*names, *(field for definition in self.types.values() for field in definition["fields"])):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
                raise RuleError(f"{name!r} is not a usable type or field name. Use letters, digits, and underscores.")
        for type_name, definition in self.types.items():
            try:
                for operation in OPERATIONS:
                    parse(definition["rules"][operation], names)
                for field in definition["fields"].values():
                    if "write_rule" in field:
                        parse(field["write_rule"], names)
                for constraint in definition.get("constraints", []):
                    parse(constraint["check"], names)
                    if not set(constraint.get("on", ())) <= {"create", "update", "delete"}:
                        raise RuleError("A constraint applies on create, update, or delete.")
            except KeyError as error:
                raise RuleError(f"{type_name} is missing {error}.") from error
            except RuleError as error:
                raise RuleError(f"{type_name}: {error}") from error
