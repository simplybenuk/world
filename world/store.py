"""Persistent, governed records with atomic history and optimistic concurrency."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .definition import Definition
from .schema import WorldError, references, require_type, validate_data, validate_value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class _Records:
    """The live records a definition's rules may consult during one transaction."""

    def __init__(self, store: "WorldStore", connection: sqlite3.Connection) -> None:
        self._store, self._connection = store, connection

    def get(self, type_name: str, record_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        return self._store._raw(self._connection, type_name, record_id, include_deleted=include_deleted)

    def all(self, type_name: str | None = None) -> list[dict[str, Any]]:
        return self._store._all(self._connection, type_name)


class WorldStore:
    """One database, with a new SQLite connection for every operation.

    Every writer holds a transaction while checking the actor, version,
    relationships, and capabilities. The audit event commits with the record.
    The definition supplies the types and rules; the team world is the default.
    """

    def __init__(self, path: str | Path, definition: Definition | None = None) -> None:
        if definition is None:
            from worlds.team import DEFINITION as definition
        self.definition = definition
        self.types = definition.types
        self._keeper: sqlite3.Connection | None = None
        self._uri = str(path) == ":memory:"
        if self._uri:
            self.path = ":memory:"
            self._database = f"file:world-{uuid4().hex}?mode=memory&cache=shared"
            self._keeper = self._connect()
        else:
            database = Path(path).expanduser().resolve()
            database.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(database)
            self._database = self.path
        self._initialize()

    def close(self) -> None:
        """Release a shared in-memory database, if this store has one."""
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database, uri=self._uri, timeout=30, isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            busy = "locked" in str(error).lower() or "busy" in str(error).lower()
            raise WorldError(
                503 if busy else 500,
                "store_busy" if busy else "storage_error",
                "The world is busy. Try again." if busy else "The record transaction could not be completed.",
            ) from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS world_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    type TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version > 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    PRIMARY KEY (type, id)
                );
                CREATE INDEX IF NOT EXISTS records_live_type ON records (type, deleted_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete')),
                    at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    before_state TEXT,
                    after_state TEXT
                );
                CREATE INDEX IF NOT EXISTS events_record ON events (type, record_id, id);
                CREATE TRIGGER IF NOT EXISTS events_no_update
                    BEFORE UPDATE ON events BEGIN
                        SELECT RAISE(ABORT, 'World history is append-only');
                    END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                    BEFORE DELETE ON events BEGIN
                        SELECT RAISE(ABORT, 'World history is append-only');
                    END;
                CREATE TABLE IF NOT EXISTS views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    how TEXT NOT NULL,
                    after_event INTEGER NOT NULL  -- the latest event when this view happened, for ordering
                );
                CREATE INDEX IF NOT EXISTS views_record ON views (type, record_id, id);
                CREATE TRIGGER IF NOT EXISTS views_no_update
                    BEFORE UPDATE ON views BEGIN
                        SELECT RAISE(ABORT, 'World history is append-only');
                    END;
                CREATE TRIGGER IF NOT EXISTS views_no_delete
                    BEFORE DELETE ON views BEGIN
                        SELECT RAISE(ABORT, 'World history is append-only');
                    END;
            """)
        finally:
            connection.close()
        with self._transaction(write=True) as connection:
            meta = dict(connection.execute("SELECT key, value FROM world_meta").fetchall())
            # Databases from before worlds were named hold the team world.
            saved = meta.get("world", "team" if "seed_version" in meta else self.definition.name)
            if saved != self.definition.name:
                raise ValueError(f"{self.path} holds the {saved} world, not {self.definition.name}.")
            if "world" not in meta:
                connection.execute("INSERT INTO world_meta (key, value) VALUES ('world', ?)", (saved,))
            if "seed_version" in meta:
                return
            now = _now()
            for type_name, record_id, data in self.definition.seed(datetime.now(timezone.utc).date()):
                data = validate_data(self.types, type_name, data)
                self._validate_relationships(connection, None, type_name, record_id, data, None)
                record = self._insert(connection, type_name, record_id, data, now)
                self._audit(connection, "create", "system", {
                    "requested_by": "system", "reason": self.definition.seed_reason,
                    "evidence": [{"source": f"worlds.{self.definition.name}", "version": 1}],
                }, None, record)
            connection.execute("INSERT INTO world_meta (key, value) VALUES ('seed_version', '1')")

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        record = {
            "id": row["id"], "type": row["type"], "data": json.loads(row["data"]),
            "version": row["version"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if row["deleted_at"] is not None:
            record.update({"deleted": True, "deleted_at": row["deleted_at"]})
        return record

    def _raw(
        self, connection: sqlite3.Connection, type_name: str, record_id: str,
        *, include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        if not isinstance(record_id, str) or not record_id:
            raise WorldError(400, "invalid_id", "A record ID must be a nonempty string.")
        sql = "SELECT * FROM records WHERE type = ? AND id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = connection.execute(sql, (type_name, record_id)).fetchone()
        return self._decode(row) if row else None

    def _all(
        self, connection: sqlite3.Connection, type_name: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM records WHERE deleted_at IS NULL"
        params: tuple[str, ...] = ()
        if type_name is not None:
            sql += " AND type = ?"
            params = (type_name,)
        return [self._decode(row) for row in connection.execute(sql + " ORDER BY type, id", params)]

    def _actor(self, connection: sqlite3.Connection, actor_id: str) -> dict[str, Any]:
        if not isinstance(actor_id, str) or not actor_id:
            raise WorldError(401, "invalid_actor", "A valid actor is required.")
        actor = self._raw(connection, self.definition.actor_type, actor_id)
        if actor is None:
            raise WorldError(401, "invalid_actor", "The authenticated actor no longer exists.")
        return actor

    def _can_read(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> bool:
        return self.definition.can_read(_Records(self, connection), actor, record)

    def _can_update(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> bool:
        if record.get("deleted"):
            return False
        return self.definition.can_update(_Records(self, connection), actor, record)

    def _inbound(
        self, connection: sqlite3.Connection, type_name: str, record_id: str,
    ) -> list[dict[str, str]]:
        inbound = []
        for record in self._all(connection):
            for field, target, target_id in references(self.types, record["type"], record["data"]):
                if target == type_name and target_id == record_id:
                    inbound.append({"type": record["type"], "id": record["id"], "field": field})
        return inbound

    def _can_delete(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> bool:
        if record.get("deleted") or self._inbound(connection, record["type"], record["id"]):
            return False
        records = _Records(self, connection)
        try:
            self.definition.authorize_delete(records, actor, record)
            self.definition.check_delete(records, actor, record)
        except WorldError:
            return False
        return True

    def _capabilities(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> dict[str, bool]:
        return {
            "read": self._can_read(connection, actor, record),
            "update": self._can_update(connection, actor, record),
            "delete": self._can_delete(connection, actor, record),
        }

    def _public(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> dict[str, Any]:
        public = deepcopy(record)
        public["capabilities"] = self._capabilities(connection, actor, record)
        return public

    @staticmethod
    def _deny(message: str = "This actor is not allowed to make that change.") -> None:
        raise WorldError(403, "permission_denied", message)

    def _get_authorized(
        self, connection: sqlite3.Connection, actor: dict[str, Any], type_name: str,
        record_id: str, *, include_deleted: bool = False,
    ) -> dict[str, Any]:
        record = self._raw(connection, type_name, record_id, include_deleted=include_deleted)
        if record is None:
            raise WorldError(404, "not_found", "The record does not exist.", {"type": type_name, "id": record_id})
        if not self._can_read(connection, actor, record):
            self._deny("This actor is not allowed to read that record.")
        return record

    def describe(self, actor_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            types = deepcopy(self.types)
            capabilities = self.definition.type_capabilities(_Records(self, connection), actor)
            for type_name, definition in types.items():
                definition["capabilities"] = {**capabilities[type_name], "rules": definition.pop("rules")}
                for field in self.definition.protected_fields(actor, type_name):
                    definition["fields"][field]["writable"] = False
            return {
                "name": self.definition.title, "version": self.definition.version, "types": types,
                "actor": self._public(connection, actor, actor),
                "query": {"operators": ["eq", "ne", "contains", "lt", "lte", "gt", "gte", "in"], "combine": "and"},
                "mutations": {"concurrency": "expected_version is required for update and delete"},
            }

    def list_records(self, actor_id: str, type_name: str | None = None) -> list[dict[str, Any]]:
        if type_name is not None:
            require_type(self.types, type_name)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            return self._viewed(connection, actor_id, "listed", [
                self._public(connection, actor, record) for record in self._all(connection, type_name)
                if self._can_read(connection, actor, record)
            ])

    def get_record(self, actor_id: str, type_name: str, record_id: str) -> dict[str, Any]:
        require_type(self.types, type_name)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            record = self._get_authorized(connection, actor, type_name, record_id)
            return self._viewed(connection, actor_id, "read", [self._public(connection, actor, record)])[0]

    def _viewed(
        self, connection: sqlite3.Connection, actor_id: str, how: str, records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Record that the actor was shown these records. Commits with the read."""
        now = _now()
        latest = connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        connection.executemany(
            "INSERT INTO views (type, record_id, at, actor, how, after_event) VALUES (?, ?, ?, ?, ?, ?)",
            [(record["type"], record["id"], now, actor_id, how, latest) for record in records],
        )
        return records

    def _filters(self, type_name: str, filters: Any) -> list[dict[str, Any]]:
        if not isinstance(filters, list):
            raise WorldError(400, "invalid_query", "Filters must be a list.")
        fields = {
            **require_type(self.types, type_name)["fields"],
            "id": {"type": "text"}, "type": {"type": "enum", "values": list(self.types)},
            "version": {"type": "integer"},
            "created_at": {"type": "timestamp"}, "updated_at": {"type": "timestamp"},
        }
        operators = {"eq", "ne", "contains", "lt", "lte", "gt", "gte", "in"}
        validated = []
        for query_filter in filters:
            if not isinstance(query_filter, dict) or set(query_filter) != {"field", "op", "value"}:
                raise WorldError(400, "invalid_query", "Each filter requires field, op, and value.", {"keys": ["field", "op", "value"]})
            field, operation, value = query_filter["field"], query_filter["op"], query_filter["value"]
            if not isinstance(field, str) or field not in fields:
                raise WorldError(400, "invalid_query", "The filter names an unknown field.")
            if not isinstance(operation, str) or operation not in operators:
                raise WorldError(400, "invalid_query", "The filter uses an unknown operator.")
            spec = fields[field]
            kind = spec["type"]
            if operation == "contains" and kind not in ("text", "list"):
                raise WorldError(400, "invalid_query", "contains requires a text or list field.")
            if operation in {"lt", "lte", "gt", "gte"} and kind not in {"text", "date", "integer", "timestamp"}:
                raise WorldError(400, "invalid_query", "Ordering requires a text, date, or number field.")
            values = value if operation == "in" else [value]
            if operation == "in" and not isinstance(value, list):
                raise WorldError(400, "invalid_query", "The in operator requires a list of values.")
            value_spec = spec["items"] if operation == "contains" and kind == "list" else spec
            for item in values:
                if value_spec["type"] == "integer":
                    if type(item) is not int:
                        raise WorldError(400, "invalid_query", "This filter requires an integer.")
                elif value_spec["type"] == "timestamp":
                    if not isinstance(item, str):
                        raise WorldError(400, "invalid_query", "A timestamp filter requires a string.")
                    try:
                        datetime.fromisoformat(item.replace("Z", "+00:00"))
                    except ValueError as error:
                        raise WorldError(400, "invalid_query", "The timestamp filter is invalid.") from error
                else:
                    validate_value(field, item, value_spec)
            validated.append(deepcopy(query_filter))
        return validated

    @staticmethod
    def _matches(record: dict[str, Any], query_filter: dict[str, Any]) -> bool:
        field, operation, value = query_filter["field"], query_filter["op"], query_filter["value"]
        current = record[field] if field in {"id", "type", "version", "created_at", "updated_at"} else record["data"].get(field)
        if operation == "eq":
            return current == value
        if operation == "ne":
            return current != value
        if operation == "in":
            return current in value
        if current is None or value is None:
            return False
        if operation == "contains":
            return value.casefold() in current.casefold() if isinstance(current, str) else value in current
        if operation == "lt":
            return current < value
        if operation == "lte":
            return current <= value
        if operation == "gt":
            return current > value
        return current >= value

    def query(
        self, actor_id: str, type_name: str, filters: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        require_type(self.types, type_name)
        filters = self._filters(type_name, [] if filters is None else filters)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            return self._viewed(connection, actor_id, "found by a query", [
                self._public(connection, actor, record) for record in self._all(connection, type_name)
                if self._can_read(connection, actor, record) and all(self._matches(record, item) for item in filters)
            ])

    def _authorize_fields(
        self, actor: dict[str, Any], type_name: str, changes: dict[str, Any],
        before: dict[str, Any],
    ) -> None:
        changed_protected = [
            field for field in self.definition.protected_fields(actor, type_name)
            if field in changes and changes[field] != before["data"].get(field)
        ]
        if changed_protected:
            raise WorldError(
                403, "permission_denied", "This actor is not allowed to change these fields.",
                {"fields": sorted(changed_protected)},
            )

    def _validate_relationships(
        self, connection: sqlite3.Connection, actor: dict[str, Any] | None, type_name: str,
        record_id: str, data: dict[str, Any], before: dict[str, Any] | None,
    ) -> None:
        for field, target, target_id in references(self.types, type_name, data):
            if self._raw(connection, target, target_id) is None:
                raise WorldError(
                    422, "invalid_reference", "A relationship must point to a live record of the required type.",
                    {"field": field, "target": target, "id": target_id},
                )
        self.definition.check(_Records(self, connection), actor, type_name, record_id, data, before)

    def _provenance(
        self, connection: sqlite3.Connection, actor_id: str, action: str,
        provenance: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if provenance is None:
            provenance = {}
        if not isinstance(provenance, dict) or set(provenance) - {"requested_by", "reason", "evidence"}:
            raise WorldError(422, "invalid_provenance", "Provenance accepts requested_by, reason, and evidence. The actor comes from authentication.")
        requester = provenance.get("requested_by", actor_id)
        if not isinstance(requester, str) or not requester or self._raw(connection, self.definition.actor_type, requester) is None:
            raise WorldError(422, "invalid_provenance", f"requested_by must identify a live {self.definition.actor_type}.")
        reason = provenance.get("reason", f"Record {action} requested.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 20000:
            raise WorldError(422, "invalid_provenance", "reason must be nonempty text of at most 20000 characters.")
        evidence = provenance.get("evidence", [])
        try:
            serialized = _json(evidence)
        except (TypeError, ValueError, RecursionError) as error:
            raise WorldError(422, "invalid_provenance", "evidence must contain JSON values.") from error
        if len(serialized) > 65536:
            raise WorldError(422, "invalid_provenance", "evidence is too large.")
        return {"requested_by": requester, "reason": reason, "evidence": json.loads(serialized)}

    def _insert(
        self, connection: sqlite3.Connection, type_name: str, record_id: str,
        data: dict[str, Any], now: str,
    ) -> dict[str, Any]:
        connection.execute(
            "INSERT INTO records (type, id, data, version, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
            (type_name, record_id, _json(data), now, now),
        )
        return self._raw(connection, type_name, record_id)

    def _audit(
        self, connection: sqlite3.Connection, action: str, actor_id: str,
        provenance: dict[str, Any], before: dict[str, Any] | None,
        after: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO events (type, record_id, action, at, actor, requested_by, reason, evidence, before_state, after_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                after["type"], after["id"], action, after["updated_at"], actor_id,
                provenance["requested_by"], provenance["reason"], _json(provenance["evidence"]),
                _json(before) if before is not None else None, _json(after),
            ),
        )

    @staticmethod
    def _check_version(record: dict[str, Any], expected_version: Any) -> None:
        if type(expected_version) is not int or expected_version < 1:
            raise WorldError(
                428, "version_required", "expected_version must be the positive integer version of the record.",
                {"current_version": record["version"]},
            )
        if record["version"] != expected_version:
            raise WorldError(
                409, "version_conflict", "The record changed since it was read. Read it again before updating.",
                {"type": record["type"], "id": record["id"], "expected_version": expected_version, "current_version": record["version"]},
            )

    def create(
        self, actor_id: str, type_name: str, data: dict[str, Any],
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require_type(self.types, type_name)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            data = validate_data(self.types, type_name, data, actor_id=actor_id)
            self.definition.authorize_create(_Records(self, connection), actor, type_name, data)
            record_id = f"{type_name.lower()}-{uuid4().hex}"
            self._validate_relationships(connection, actor, type_name, record_id, data, None)
            provenance = self._provenance(connection, actor_id, "create", provenance)
            record = self._insert(connection, type_name, record_id, data, _now())
            self._audit(connection, "create", actor_id, provenance, None, record)
            return self._public(connection, actor, record)

    def update(
        self, actor_id: str, type_name: str, record_id: str, changes: dict[str, Any],
        expected_version: int, provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require_type(self.types, type_name)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            before = self._get_authorized(connection, actor, type_name, record_id)
            if not self._can_update(connection, actor, before):
                raise WorldError(
                    403, "permission_denied", "This actor is not allowed to update this record.",
                    {"rule": self.types[type_name]["rules"]["update"], "constraints": self.types[type_name].get("constraints", [])},
                )
            self._check_version(before, expected_version)
            changes = validate_data(self.types, type_name, changes, partial=True, actor_id=actor_id)
            if not changes:
                raise WorldError(422, "empty_changes", "An update must include at least one field.")
            self._authorize_fields(actor, type_name, changes, before)
            data = validate_data(self.types, type_name, {**before["data"], **changes}, actor_id=actor_id)
            proposed = {**before, "data": data}
            if not self._can_update(connection, actor, proposed):
                self._deny("This actor would not be allowed to update the record after this change.")
            self._validate_relationships(connection, actor, type_name, record_id, data, before)
            provenance = self._provenance(connection, actor_id, "update", provenance)
            connection.execute(
                "UPDATE records SET data = ?, version = version + 1, updated_at = ? WHERE type = ? AND id = ?",
                (_json(data), _now(), type_name, record_id),
            )
            after = self._raw(connection, type_name, record_id)
            self._audit(connection, "update", actor_id, provenance, before, after)
            if type_name == self.definition.actor_type and record_id == actor_id:
                actor = after
            return self._public(connection, actor, after)

    def delete(
        self, actor_id: str, type_name: str, record_id: str, expected_version: int,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require_type(self.types, type_name)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            before = self._get_authorized(connection, actor, type_name, record_id)
            records = _Records(self, connection)
            self.definition.authorize_delete(records, actor, before)
            self._check_version(before, expected_version)
            inbound = self._inbound(connection, type_name, record_id)
            if inbound:
                raise WorldError(409, "record_referenced", "Remove live relationships before deleting this record.", {"references": inbound})
            self.definition.check_delete(records, actor, before)
            provenance = self._provenance(connection, actor_id, "delete", provenance)
            now = _now()
            connection.execute(
                "UPDATE records SET version = version + 1, updated_at = ?, deleted_at = ? WHERE type = ? AND id = ?",
                (now, now, type_name, record_id),
            )
            after = self._raw(connection, type_name, record_id, include_deleted=True)
            self._audit(connection, "delete", actor_id, provenance, before, after)
            return self._public(connection, actor, after)

    def history(self, actor_id: str, type_name: str, record_id: str) -> list[dict[str, Any]]:
        require_type(self.types, type_name)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            record = self._get_authorized(connection, actor, type_name, record_id, include_deleted=True)
            self._viewed(connection, actor_id, "history read", [record])
            return [
                {
                    "id": row["id"], "type": row["type"], "record_id": row["record_id"],
                    "action": row["action"], "at": row["at"], "actor": row["actor"],
                    "requested_by": row["requested_by"], "reason": row["reason"],
                    "evidence": json.loads(row["evidence"]),
                    "before": json.loads(row["before_state"]) if row["before_state"] is not None else None,
                    "after": json.loads(row["after_state"]) if row["after_state"] is not None else None,
                }
                for row in connection.execute(
                    "SELECT * FROM events WHERE type = ? AND record_id = ? ORDER BY id", (type_name, record_id),
                )
            ]

    @staticmethod
    def _shown(value: Any) -> str:
        text = _json(value)
        return text if len(text) <= 120 else text[:117] + "..."

    def _happened(self, row: sqlite3.Row) -> str:
        """What one stored event did, as a phrase a person can read."""
        before = json.loads(row["before_state"])["data"] if row["before_state"] else {}
        after = json.loads(row["after_state"])["data"]
        if row["action"] == "create":
            what = "; ".join(f"{field} {self._shown(value)}" for field, value in after.items() if value not in (None, "", []))
        elif row["action"] == "update":
            what = "; ".join(
                f"{field} from {self._shown(before.get(field))} to {self._shown(value)}"
                for field, value in after.items() if value != before.get(field)
            ) or "no field changed"
        else:
            what = ""
        requester = "" if row["requested_by"] in (row["actor"], "system") else f" Requested by {row['requested_by']}."
        # A reason is free text. Keep it on its line so that it cannot pose as another entry.
        reason = " ".join(row["reason"].split())
        return f"{what + '. ' if what else ''}Reason: {reason}{requester}"

    def audit(
        self, actor_id: str, type_name: str | None = None, record_id: str | None = None,
        limit: int = 500,
    ) -> list[str]:
        """The latest audit lines for records this actor may read, oldest first.

        Each line gives when, who, what they did, the record, and the detail.
        Long values are shortened here; history() returns them in full.
        """
        if type_name is not None:
            require_type(self.types, type_name)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise WorldError(400, "invalid_limit", "limit must be a whole number from 1 to 10000.")
        where, params = "", ()
        if type_name is not None:
            where, params = " WHERE type = ?", (type_name,)
            if record_id is not None:
                where, params = where + " AND record_id = ?", (type_name, record_id)
        verbs = {"create": "created", "update": "changed", "delete": "deleted"}
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            if record_id is not None:
                self._get_authorized(connection, actor, type_name, record_id, include_deleted=True)
            entries = [
                (row["id"], 0, 0, row["at"], row["actor"], verbs[row["action"]], row["type"], row["record_id"], self._happened(row))
                for row in connection.execute(f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?", (*params, limit))
            ] + [
                (row["after_event"], 1, row["id"], row["at"], row["actor"], "viewed", row["type"], row["record_id"], row["how"].capitalize() + ".")
                for row in connection.execute(f"SELECT * FROM views{where} ORDER BY id DESC LIMIT ?", (*params, limit))
            ]
            readable: dict[tuple[str, str], bool] = {}
            lines = []
            # Writers run one at a time, so event order and each view's place in it are exact.
            for _, _, _, at, who, verb, kind, identifier, detail in sorted(entries)[-limit:]:
                key = (kind, identifier)
                if key not in readable:
                    record = self._raw(connection, kind, identifier, include_deleted=True)
                    readable[key] = bool(record and self._can_read(connection, actor, record))
                if readable[key]:
                    lines.append(f"{at}  {who}  {verb}  {kind} {identifier}  {detail}")
            return lines
