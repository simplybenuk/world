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

from .definition import WorldDefinition
from .schema import WorldError, references, require_type, validate_data, validate_value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class WorldStore:
    """One database, with a new SQLite connection for every operation.

    Every writer holds a transaction while checking the actor, version,
    relationships, and capabilities. The audit event commits with the record.
    """

    def __init__(self, path: str | Path, definition: WorldDefinition | None = None) -> None:
        self.definition = definition if definition is not None else WorldDefinition.default()
        if not isinstance(self.definition, WorldDefinition):
            raise WorldError(400, "invalid_definition", "Pass a loaded WorldDefinition to the store.")
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
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

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
            """)
        finally:
            connection.close()
        with self._transaction(write=True) as connection:
            binding = connection.execute("SELECT value FROM world_meta WHERE key = 'definition_id'").fetchone()
            seeded = connection.execute("SELECT value FROM world_meta WHERE key = 'seed_version'").fetchone()
            existing = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if binding and binding["value"] != self.definition.id:
                raise WorldError(409, "definition_mismatch", "This database belongs to a different world definition.")
            if not binding and (seeded or existing) and self.definition.id != WorldDefinition.default().id:
                raise WorldError(409, "definition_mismatch", "An existing unbound database can only be opened with the original team definition.")
            if not binding:
                connection.execute("INSERT INTO world_meta (key, value) VALUES ('definition_id', ?)", (self.definition.id,))
            if seeded or existing:
                self._validate_world(connection)
                if not seeded:
                    connection.execute("INSERT INTO world_meta (key, value) VALUES ('seed_version', '1')")
                return
            now = _now()
            for type_name, record_id, data in self.definition.seed_records(datetime.now(timezone.utc).date()):
                data = validate_data(type_name, data, types=self.definition.types, actor_id=self.definition.actor_ids[0])
                record = self._insert(connection, type_name, record_id, data, now)
                self._audit(connection, "create", "system", {
                    "requested_by": "system", "reason": "Initial records from the world definition.",
                    "evidence": [{"source": "world.definition", "id": self.definition.id, "version": self.definition.version}],
                }, None, record)
            self._validate_world(connection)
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

    def _resolve(
        self, connection: sqlite3.Connection, record: dict[str, Any], path: str,
        *, include_deleted: bool = False,
        records: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> Any:
        """Follow a validated field path, resolving references using their types."""
        parts = path.split(".")
        current = record
        for index, field in enumerate(parts):
            descriptor = self.definition.types[current["type"]]["fields"][field]
            value = current["data"].get(field)
            if index == len(parts) - 1:
                return value
            if value is None:
                return None
            key = descriptor["target"], value
            current = records.get(key) if records is not None else self._raw(connection, *key, include_deleted=include_deleted)
            if current is None:
                return None
        return None

    def _scope_for(
        self, connection: sqlite3.Connection, record: dict[str, Any],
        *, include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        scope = self.definition.types[record["type"]]["policy"]["scope"]
        if scope is None:
            return None
        if scope == "":
            return record
        target_id = self._resolve(connection, record, scope, include_deleted=include_deleted)
        return self._raw(connection, self.definition.scope_type(record["type"]), target_id, include_deleted=include_deleted) if target_id is not None else None

    def _can_read(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> bool:
        policy = self.definition.types[record["type"]]["policy"]
        if actor["data"]["role"] == "admin" or policy["read"] == "authenticated":
            return True
        scope = self._scope_for(connection, record, include_deleted=True)
        return bool(scope and (
            scope["data"]["visibility"] == "team" or actor["id"] in scope["data"]["members"]
        ))

    def _can_update(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> bool:
        if record.get("deleted"):
            return False
        role = actor["data"]["role"]
        if role == "admin":
            return True
        if role != "member":
            return False
        policy = self.definition.types[record["type"]]["policy"]
        if policy["update"] == "admin":
            return False
        if policy["update"] == "self":
            return record["id"] == actor["id"]
        scope = self._scope_for(connection, record)
        return bool(scope and actor["id"] in scope["data"]["members"])

    def _inbound(
        self, connection: sqlite3.Connection, type_name: str, record_id: str,
    ) -> list[dict[str, str]]:
        inbound = []
        for record in self._all(connection):
            for field, target, target_id in references(record["type"], record["data"], types=self.definition.types):
                if target == type_name and target_id == record_id:
                    inbound.append({"type": record["type"], "id": record["id"], "field": field})
        return inbound

    def _admin_count(self, connection: sqlite3.Connection) -> int:
        return sum(actor["data"]["role"] == "admin" for actor in self._all(connection, self.definition.actor_type))

    def _capabilities(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> dict[str, bool]:
        admin = actor["data"]["role"] == "admin"
        last_admin = (
            record["type"] == self.definition.actor_type and record["data"]["role"] == "admin"
            and self._admin_count(connection) == 1
        )
        return {
            "read": self._can_read(connection, actor, record),
            "update": self._can_update(connection, actor, record),
            "delete": bool(
                admin and not record.get("deleted") and not last_admin
                and not self._inbound(connection, record["type"], record["id"])
            ),
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
            types = deepcopy(self.definition.types)
            role = actor["data"]["role"]
            for type_name, definition in types.items():
                policy = definition["policy"]
                scope_type = self.definition.scope_type(type_name)
                membership = bool(scope_type and any(
                    actor_id in scope["data"]["members"]
                    for scope in self._all(connection, scope_type)
                ))
                definition["capabilities"] = {
                    "read": True,
                    "create": role == "admin" or (role == "member" and policy["create"] == "member" and membership),
                    "update": role == "admin" or (role == "member" and (
                        policy["update"] == "self" or (policy["update"] == "member" and membership)
                    )),
                    "delete": role == "admin",
                    "rules": deepcopy(definition.get("rules", {})),
                }
            return {
                "id": self.definition.id, "name": self.definition.name,
                "version": self.definition.version, "actor_type": self.definition.actor_type,
                "types": types,
                "actor": self._public(connection, actor, actor),
                "query": {"operators": ["eq", "ne", "contains", "lt", "lte", "gt", "gte", "in"], "combine": "and"},
                "mutations": {"concurrency": "expected_version is required for update and delete"},
            }

    def list_records(self, actor_id: str, type_name: str | None = None) -> list[dict[str, Any]]:
        if type_name is not None:
            require_type(type_name, self.definition.types)
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            return [
                self._public(connection, actor, record) for record in self._all(connection, type_name)
                if self._can_read(connection, actor, record)
            ]

    def get_record(self, actor_id: str, type_name: str, record_id: str) -> dict[str, Any]:
        require_type(type_name, self.definition.types)
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            record = self._get_authorized(connection, actor, type_name, record_id)
            return self._public(connection, actor, record)

    def _filters(self, type_name: str, filters: Any) -> list[dict[str, Any]]:
        if not isinstance(filters, list):
            raise WorldError(400, "invalid_query", "Filters must be a list.")
        fields = {
            **require_type(type_name, self.definition.types)["fields"],
            "id": {"type": "text"}, "type": {"type": "enum", "values": list(self.definition.types)},
            "version": {"type": "integer"},
            "created_at": {"type": "timestamp"}, "updated_at": {"type": "timestamp"},
        }
        operators = {"eq", "ne", "contains", "lt", "lte", "gt", "gte", "in"}
        validated = []
        for query_filter in filters:
            if not isinstance(query_filter, dict) or set(query_filter) != {"field", "op", "value"}:
                raise WorldError(400, "invalid_query", "Each filter requires field, op, and value.")
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
                        raise WorldError(400, "invalid_query", "An integer filter requires an integer.")
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
        require_type(type_name, self.definition.types)
        filters = self._filters(type_name, [] if filters is None else filters)
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            return [
                self._public(connection, actor, record) for record in self._all(connection, type_name)
                if self._can_read(connection, actor, record) and all(self._matches(record, item) for item in filters)
            ]

    def _authorize_create(
        self, connection: sqlite3.Connection, actor: dict[str, Any], type_name: str,
        data: dict[str, Any], provided: dict[str, Any],
    ) -> None:
        if actor["data"]["role"] == "admin":
            return
        policy = self.definition.types[type_name]["policy"]
        if actor["data"]["role"] != "member" or policy["create"] != "member":
            self._deny()
        scope = self._scope_for(connection, {"type": type_name, "data": data})
        if not scope or actor["id"] not in scope["data"]["members"]:
            self._deny("Only scope members can create records in this scope.")
        author = policy.get("author_field")
        if author and data.get(author) != actor["id"]:
            self._deny("A member can only create a record attributed to themselves.")
        protected = sorted(set(provided) & (set(policy["admin_fields"]) - {author}))
        if protected:
            raise WorldError(
                403, "permission_denied", "Only an admin can set these fields.",
                {"fields": protected},
            )

    def _authorize_fields(
        self, actor: dict[str, Any], type_name: str, changes: dict[str, Any],
        before: dict[str, Any],
    ) -> None:
        if actor["data"]["role"] == "admin":
            return
        protected = self.definition.types[type_name]["policy"]["admin_fields"]
        changed_protected = [
            field for field in protected
            if field in changes and changes[field] != before["data"].get(field)
        ]
        if changed_protected:
            raise WorldError(
                403, "permission_denied", "Only an admin can change these fields.",
                {"fields": sorted(changed_protected)},
            )

    def _validate_world(
        self, connection: sqlite3.Connection, proposed: dict[str, Any] | None = None,
    ) -> None:
        """Check all live relationships against one proposed transactional state."""
        records = {(record["type"], record["id"]): record for record in self._all(connection)}
        if proposed is not None:
            records[proposed["type"], proposed["id"]] = proposed
        for record in records.values():
            type_name = record["type"]
            data = validate_data(type_name, record["data"], types=self.definition.types)
            for field, target, target_id in references(type_name, data, types=self.definition.types):
                if (target, target_id) not in records:
                    raise WorldError(
                        422, "invalid_reference", "A relationship must point to a live record of the required type.",
                        {"type": type_name, "id": record["id"], "field": field, "target": target, "target_id": target_id},
                    )
        for record in records.values():
            for constraint in self.definition.types[record["type"]].get("constraints", []):
                if constraint["kind"] == "member_of":
                    field = constraint["field"]
                    value = self._resolve(connection, record, field, records=records)
                    members = self._resolve(connection, record, constraint["members"], records=records)
                    valid = (value is None and constraint.get("nullable", False)) or (
                        value is not None and isinstance(members, list) and value in members
                    )
                    message = "The referenced record must belong to the declared members."
                else:
                    field = constraint["left"]
                    left = self._resolve(connection, record, field, records=records)
                    right = self._resolve(connection, record, constraint["right"], records=records)
                    valid = (constraint.get("nullable", False) and (left is None or right is None)) or (
                        left is not None and right is not None and left == right
                    )
                    message = "The declared relationships must refer to the same record."
                if not valid:
                    raise WorldError(
                        422, "invalid_relationship", message,
                        {"type": record["type"], "id": record["id"], "field": field},
                    )

    def _validate_transitions(self, before: dict[str, Any], proposed: dict[str, Any]) -> None:
        for transition in self.definition.types[before["type"]].get("transitions", []):
            field = transition["field"]
            current, following = before["data"].get(field), proposed["data"].get(field)
            if current != following and following not in transition["allowed"].get(current, []):
                raise WorldError(
                    422, "invalid_transition", "This value cannot advance directly to the proposed value.",
                    {"field": field, "current": current, "proposed": following, "allowed": transition["allowed"].get(current, [])},
                )

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
            raise WorldError(422, "invalid_provenance", "requested_by must identify a live actor.")
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
            raise WorldError(428, "version_required", "expected_version must be the positive integer version of the record.")
        if record["version"] != expected_version:
            raise WorldError(
                409, "version_conflict", "The record changed since it was read. Read it again before updating.",
                {"type": record["type"], "id": record["id"], "expected_version": expected_version, "current_version": record["version"]},
            )

    def create(
        self, actor_id: str, type_name: str, data: dict[str, Any],
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require_type(type_name, self.definition.types)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            provided = data
            data = validate_data(type_name, data, types=self.definition.types, actor_id=actor_id)
            self._authorize_create(connection, actor, type_name, data, provided)
            record_id = f"{type_name.lower()}-{uuid4().hex}"
            self._validate_world(connection, {"type": type_name, "id": record_id, "data": data})
            provenance = self._provenance(connection, actor_id, "create", provenance)
            record = self._insert(connection, type_name, record_id, data, _now())
            self._audit(connection, "create", actor_id, provenance, None, record)
            return self._public(connection, actor, record)

    def update(
        self, actor_id: str, type_name: str, record_id: str, changes: dict[str, Any],
        expected_version: int, provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require_type(type_name, self.definition.types)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            before = self._get_authorized(connection, actor, type_name, record_id)
            if not self._can_update(connection, actor, before):
                self._deny()
            self._check_version(before, expected_version)
            changes = validate_data(type_name, changes, types=self.definition.types, partial=True, actor_id=actor_id)
            if not changes:
                raise WorldError(422, "empty_changes", "An update must include at least one field.")
            self._authorize_fields(actor, type_name, changes, before)
            data = validate_data(type_name, {**before["data"], **changes}, types=self.definition.types, actor_id=actor_id)
            proposed = {**before, "data": data}
            if not self._can_update(connection, actor, proposed):
                self._deny("The actor must be a member of both the current and destination scopes.")
            if type_name == self.definition.actor_type and before["data"]["role"] == "admin" and data["role"] != "admin" and self._admin_count(connection) == 1:
                raise WorldError(409, "last_admin", "The world must retain at least one admin.")
            self._validate_transitions(before, proposed)
            self._validate_world(connection, proposed)
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
        require_type(type_name, self.definition.types)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            before = self._get_authorized(connection, actor, type_name, record_id)
            if actor["data"]["role"] != "admin":
                self._deny("Only an admin can delete records.")
            self._check_version(before, expected_version)
            inbound = self._inbound(connection, type_name, record_id)
            if inbound:
                raise WorldError(409, "record_referenced", "Remove live relationships before deleting this record.", {"references": inbound})
            if type_name == self.definition.actor_type and before["data"]["role"] == "admin" and self._admin_count(connection) == 1:
                raise WorldError(409, "last_admin", "The world must retain at least one admin.")
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
        require_type(type_name, self.definition.types)
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._get_authorized(connection, actor, type_name, record_id, include_deleted=True)
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
