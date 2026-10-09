"""What a world must supply to the engine: its types, its seed, and its rules."""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from .schema import Types, WorldError


class Records(Protocol):
    """Live records as a rule sees them, inside the transaction being checked."""

    def get(self, type_name: str, record_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None: ...

    def all(self, type_name: str | None = None) -> list[dict[str, Any]]: ...


def deny(message: str = "This actor is not allowed to make that change.") -> None:
    raise WorldError(403, "permission_denied", message)


class Definition:
    """One world. The engine supplies storage, validation, versions, and history.

    The engine already rejects a reference to a missing record and the deletion
    of a record that others reference. Everything else about who may do what,
    and which states are valid, is decided here.
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

    def can_read(self, records: Records, actor: dict[str, Any], record: dict[str, Any]) -> bool:
        return True

    def can_update(self, records: Records, actor: dict[str, Any], record: dict[str, Any]) -> bool:
        """Checked against the saved record and again against the proposed one."""
        return False

    def type_capabilities(self, records: Records, actor: dict[str, Any]) -> dict[str, dict[str, bool]]:
        """Whether the actor can ever read, create, update, or delete each type."""
        raise NotImplementedError

    def protected_fields(self, actor: dict[str, Any], type_name: str) -> set[str]:
        """Fields this actor may not set to a new value."""
        return set()

    def authorize_create(
        self, records: Records, actor: dict[str, Any], type_name: str, data: dict[str, Any],
    ) -> None:
        deny()

    def authorize_delete(self, records: Records, actor: dict[str, Any], record: dict[str, Any]) -> None:
        deny()

    def check_delete(self, records: Records, actor: dict[str, Any], record: dict[str, Any]) -> None:
        """Raise WorldError when removing this record would break a constraint."""

    def check(
        self, records: Records, actor: dict[str, Any] | None, type_name: str, record_id: str,
        data: dict[str, Any], before: dict[str, Any] | None,
    ) -> None:
        """Raise WorldError when the proposed state breaks a constraint.

        The actor is None while seeding. References already point to live records.
        """
