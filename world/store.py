"""Persistent, governed records with atomic history and optimistic concurrency."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import ast
import json
import re
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from . import rules
from .definition import Definition
from .schema import WorldError, references, require_type, validate_data, validate_value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


APPLY_HELP = (
    '{"steps": [{"create": TYPE, "data": {...}, "as": "name"}, {"update": TYPE, "id": ID or "$name", "changes": {...}, '
    '"expected_version": N}, {"delete": TYPE, "id": ID, "expected_version": N}], "reason": WHY}'
)
INDEX_ABOVE = 12  # a world with more types than this describes itself as an index first
WHERE_HELP = (
    "One expression over record and actor, such as: record.stage == 'won' and record.organization.owner == actor.id. "
    "Use ==, !=, <, <=, >, >=, in, not in, is None, and, or, not. A dot follows a reference to the record it names. "
    "'harbor' in record.name matches part of a text, ignoring case. There are no methods such as lower() or startswith()."
)


def _agent(value: Any) -> str | None:
    """The name of the software acting for the actor, or None when they act directly."""
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value)):
        raise WorldError(422, "invalid_agent", "An agent name is up to 64 letters, digits, dots, hyphens, or underscores.")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class WorldStore:
    """One database, with a new SQLite connection for every operation.

    Every writer holds a transaction while checking the actor, version,
    relationships, and capabilities. The audit event commits with the record.
    The definition supplies the types and rules; the team world is the default.
    """

    # FULL waits for the disk on every commit, so a saved change survives a power cut.
    # NORMAL does not wait. The database stays consistent, but a power cut can lose the last moments.
    synchronous = "FULL"

    def __init__(self, path: str | Path, definition: Definition | None = None) -> None:
        if definition is None:
            from worlds.team import DEFINITION as definition
        definition.check()
        self.definition = definition
        self.types = definition.types
        # Types whose constraints could be broken by a change to something they refer to,
        # plus the types through which such a type is reached.
        self._dependent = self._dependent_types()
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

    def _dependent_types(self) -> set[str]:
        """Types worth visiting when following references back from a changed record."""
        referrers: dict[str, set[str]] = {}
        for type_name, definition in self.types.items():
            for spec in definition["fields"].values():
                target = spec.get("target") or spec.get("items", {}).get("target")
                if target:
                    referrers.setdefault(target, set()).add(type_name)
        dependent = {type_name for type_name, definition in self.types.items() if definition.get("constraints")}
        while True:
            # A type with no constraints still leads somewhere if a dependent type refers to it.
            more = {target for target, sources in referrers.items() if sources & dependent} - dependent
            if not more:
                return dependent
            dependent |= more

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
        connection.execute(f"PRAGMA synchronous = {self.synchronous}")
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
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS refs (
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    field TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS refs_target ON refs (target_type, target_id);
                CREATE INDEX IF NOT EXISTS refs_source ON refs (source_type, source_id);
            """)
            # One index per field name, shared by every type that has the field, so that a lookup by
            # value does not read every record. An index per type and field would mean thousands
            # of indexes in a large world, and every write pays for each one.
            indexed = {
                field for definition in self.types.values() for field, spec in definition["fields"].items()
                if spec["type"] in ("reference", "enum", "date", "integer") or (spec["type"] == "text" and spec.get("max_length", 20000) <= 300)
            }
            for field in sorted(indexed):
                column = f"json_extract(data, '$.{field}')"
                connection.execute(
                    f'CREATE INDEX IF NOT EXISTS "field_{field}" ON records (type, {column}, id) '
                    f"WHERE {column} IS NOT NULL AND deleted_at IS NULL")
            # Databases from before agents were named gain the column, empty for old rows.
            for table in ("events", "views"):
                if "agent" not in [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN agent TEXT")
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
            if "refs" not in meta:
                # Databases from before references were indexed are indexed once here.
                for row in connection.execute("SELECT * FROM records WHERE deleted_at IS NULL").fetchall():
                    self._index_references(connection, self._decode(row))
                connection.execute("INSERT INTO world_meta (key, value) VALUES ('refs', '1')")
            if "seed_version" in meta:
                return
            now = _now()
            for type_name, record_id, data in self.definition.seed(datetime.now(timezone.utc).date()):
                data = validate_data(self.types, type_name, data)
                self._validate_relationships(connection, None, type_name, record_id, data, None)
                record = self._insert(connection, type_name, record_id, data, now)
                self._audit(connection, "create", "system", {
                    "requested_by": "system", "reason": self.definition.seed_reason, "agent": None,
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

    def _index_references(self, connection: sqlite3.Connection, record: dict[str, Any], *, live: bool = True) -> None:
        """Keep the index of who refers to whom in step with one record."""
        connection.execute("DELETE FROM refs WHERE source_type = ? AND source_id = ?", (record["type"], record["id"]))
        if live:
            connection.executemany(
                "INSERT INTO refs (source_type, source_id, field, target_type, target_id) VALUES (?, ?, ?, ?, ?)",
                [(record["type"], record["id"], field, target, target_id)
                 for field, target, target_id in references(self.types, record["type"], record["data"])],
            )

    def _narrow(
        self, connection: sqlite3.Connection, type_name: str, terms: Any,
    ) -> tuple[str, list[Any]]:
        """SQL for the terms that an index can answer. Terms it cannot use are skipped."""
        fields = self.types[type_name]["fields"]
        scalar = lambda value: value is None or type(value) in (str, int, bool)
        sql, params = "", []
        for path, operator, value in terms:
            values = value if operator == "in" else [value]
            if not 1 <= len(values) <= 500 or not all(scalar(item) for item in values):
                continue
            column = "id" if path[0] == "id" else f"json_extract(data, '$.{path[0]}')"
            if path[0] != "id" and path[0] not in fields:
                continue
            if len(path) == 2:
                # A path through a reference: find the records it could lead to, then match their IDs.
                spec = fields[path[0]]
                if spec["type"] != "reference" or (path[1] != "id" and path[1] not in self.types[spec["target"]]["fields"]):
                    continue
                inner_sql, inner_params = self._narrow(connection, spec["target"], [((path[1],), operator, value)])
                if not inner_sql:
                    continue
                targets = [row["id"] for row in connection.execute(
                    f"SELECT id FROM records WHERE type = '{spec['target']}' AND deleted_at IS NULL{inner_sql} LIMIT 501", inner_params)]
                if len(targets) > 500:
                    continue
                values = targets
                if not values:
                    return " AND 0", []
            if None in values:
                if len(values) > 1:
                    continue
                sql += f" AND {column} IS NULL"
            else:
                sql += f" AND {column} IN ({', '.join('?' for _ in values)})"
                params.extend(values)
        return sql, params

    def _all(
        self, connection: sqlite3.Connection, type_name: str | None = None, terms: Any = (),
        *, after: str | None = None, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Live records in order. With a type, `terms` narrow them through the indexes."""
        if type_name is None:
            return [self._decode(row) for row in connection.execute("SELECT * FROM records WHERE deleted_at IS NULL ORDER BY type, id")]
        narrowed, params = self._narrow(connection, type_name, terms)
        sql = f"SELECT * FROM records WHERE type = '{type_name}' AND deleted_at IS NULL{narrowed}"
        if after is not None:
            sql, params = sql + " AND id > ?", [*params, after]
        sql += " ORDER BY id"
        if limit is not None:
            sql, params = sql + " LIMIT ?", [*params, limit]
        return [self._decode(row) for row in connection.execute(sql, params)]

    def _actor(self, connection: sqlite3.Connection, actor_id: str) -> dict[str, Any]:
        if not isinstance(actor_id, str) or not actor_id:
            raise WorldError(401, "invalid_actor", "A valid actor is required.")
        actor = self._raw(connection, self.definition.actor_type, actor_id)
        if actor is None:
            raise WorldError(401, "invalid_actor", "The authenticated actor no longer exists.")
        return actor

    def _rules(
        self, connection: sqlite3.Connection, proposed: dict[str, Any] | None = None,
    ) -> rules.World:
        """The records as rules see them, with a proposed record in place of its saved state."""
        def lookup(type_name: str, record_id: str) -> dict[str, Any] | None:
            if proposed is not None and (type_name, record_id) == (proposed["type"], proposed["id"]):
                return proposed
            return self._raw(connection, type_name, record_id, include_deleted=True)

        def live(type_name: str, terms: Any = ()) -> list[dict[str, Any]]:
            records = self._all(connection, type_name, terms)
            if proposed is None or proposed["type"] != type_name:
                return records
            # The proposed state may match where the saved one does not, so it is always a candidate.
            return [record for record in records if record["id"] != proposed["id"]] + [proposed]

        return rules.World(self.types, lookup, live)

    def _holds(
        self, connection: sqlite3.Connection, expression: str, actor: dict[str, Any] | None,
        record: dict[str, Any], saved: dict[str, Any] | None, proposed: dict[str, Any] | None = None,
    ) -> bool:
        world = self._rules(connection, proposed)

        def can_read(node: rules.Node) -> bool:
            target = world.lookup(node.type, node.id)
            return bool(target and actor and self._can_read(connection, actor, target))

        evaluation = rules.Evaluation(world, world.node(actor), world.node(record), world.node(saved), can_read)
        return rules.holds(evaluation, expression)

    def _can_read(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> bool:
        return self._holds(connection, self.types[record["type"]]["rules"]["read"], actor, record, record)

    def _can_update(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> bool:
        """Judge the update rule for a record as saved, or as proposed against its saved state."""
        if record.get("deleted"):
            return False
        saved = self._raw(connection, record["type"], record["id"]) or record
        return self._holds(connection, self.types[record["type"]]["rules"]["update"], actor, record, saved, record)

    def _inbound(
        self, connection: sqlite3.Connection, type_name: str, record_id: str,
    ) -> list[dict[str, str]]:
        return [
            {"type": row["source_type"], "id": row["source_id"], "field": row["field"]}
            for row in connection.execute(
                "SELECT source_type, source_id, field FROM refs WHERE target_type = ? AND target_id = ? "
                "ORDER BY source_type, source_id, field", (type_name, record_id))
        ]

    def _inbound_for(self, type_name: str, record_id: str) -> list[dict[str, str]]:
        """The live records that refer to one record."""
        with self._transaction() as connection:
            return self._inbound(connection, type_name, record_id)

    def _can_delete(
        self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any],
    ) -> bool:
        if record.get("deleted") or self._inbound(connection, record["type"], record["id"]):
            return False
        if not self._holds(connection, self.types[record["type"]]["rules"]["delete"], actor, record, record):
            return False
        try:
            self._check_constraints(connection, actor, record, record, "delete")
        except WorldError:
            return False
        return True

    def _check_constraints(
        self, connection: sqlite3.Connection, actor: dict[str, Any] | None, record: dict[str, Any],
        saved: dict[str, Any] | None, operation: str,
    ) -> None:
        """Check this record's constraints, then those of every record that depends on it."""
        def check(subject: dict[str, Any], subject_saved: dict[str, Any] | None, applies: str, other: bool) -> None:
            for constraint in self.types[subject["type"]].get("constraints", []):
                if applies not in constraint.get("on", ("create", "update")):
                    continue
                if not self._holds(connection, constraint["check"], actor, subject, subject_saved, record):
                    details = {"fields": constraint.get("fields", []), "constraint": constraint["check"]}
                    if other:
                        details.update({"type": subject["type"], "id": subject["id"]})
                    message = constraint["message"] if not other else (
                        f"This change would break a rule for {subject['type']} {subject['id']}: {constraint['message']}")
                    raise WorldError(constraint.get("status", 422), constraint.get("code", "constraint_failed"), message, details)

        check(record, saved, operation, False)
        if operation != "update":
            return
        # A change can break a constraint written on a record that refers to this one,
        # directly or through other records. Follow the references back and re-check each.
        # Only types that have constraints can be broken, and only they or types leading to them need following.
        seen, queue = {(record["type"], record["id"])}, [(record["type"], record["id"])]
        while queue:
            for inbound in self._inbound(connection, *queue.pop()):
                key = (inbound["type"], inbound["id"])
                if key in seen or key[0] not in self._dependent:
                    continue
                seen.add(key)
                queue.append(key)
                other = self._raw(connection, *key)
                if other is not None and self.types[key[0]].get("constraints"):
                    check(other, other, "update", True)

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

    def describe(self, actor_id: str, only: list[str] | None = None) -> dict[str, Any]:
        """What exists and what this actor may do.

        A world with many types answers with an index of their names and
        descriptions, because the full description would be too much to read.
        `only` names the types to describe in full.
        """
        for type_name in only or []:
            require_type(self.types, type_name)
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            shared = {
                "name": self.definition.title, "version": self.definition.version,
                "actor": self._public(connection, actor, actor),
                "query": {"operators": ["eq", "ne", "contains", "lt", "lte", "gt", "gte", "in"], "combine": "and"},
                "mutations": {"concurrency": "expected_version is required for update and delete"},
            }
            if only is None and len(self.types) > INDEX_ABOVE:
                return {**shared, "type_index": {name: definition["description"] for name, definition in self.types.items()}}
            types = deepcopy(self.types if only is None else {name: self.types[name] for name in only})
            world = self._rules(connection)
            possible = lambda expression: rules.possible(world, world.node(actor), expression)
            for definition in types.values():
                stated = definition.pop("rules")
                definition["capabilities"] = {**{operation: possible(rule) for operation, rule in stated.items()}, "rules": stated}
                for field in definition["fields"].values():
                    if "write_rule" in field and not possible(field["write_rule"]):
                        field["writable"] = False
                if "constraints" in definition:
                    definition["constraints"] = [constraint["check"] for constraint in definition["constraints"]]
            return {**shared, "types": types}

    def list_records(
        self, actor_id: str, type_name: str | None = None, *, agent: str | None = None,
    ) -> list[dict[str, Any]]:
        if type_name is not None:
            require_type(self.types, type_name)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            return self._viewed(connection, actor_id, agent, "listed", [
                self._public(connection, actor, record) for record in self._all(connection, type_name)
                if self._can_read(connection, actor, record)
            ])

    def get_record(
        self, actor_id: str, type_name: str, record_id: str, *, agent: str | None = None,
    ) -> dict[str, Any]:
        require_type(self.types, type_name)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            record = self._get_authorized(connection, actor, type_name, record_id)
            return self._viewed(connection, actor_id, agent, "read", [self._public(connection, actor, record)])[0]

    def _viewed(
        self, connection: sqlite3.Connection, actor_id: str, agent: str | None, how: str,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Record that the actor was shown these records. Commits with the read."""
        now = _now()
        latest = connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        connection.executemany(
            "INSERT INTO views (type, record_id, at, actor, how, after_event, agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(record["type"], record["id"], now, actor_id, how, latest, _agent(agent)) for record in records],
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
        *, agent: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.find(actor_id, type_name, filters=filters, agent=agent)["records"]

    def find(
        self, actor_id: str, type_name: str, *, where: str | None = None,
        filters: list[dict[str, Any]] | None = None, include: list[str] | None = None,
        limit: int = 100, after: str | None = None, count: bool = False, agent: str | None = None,
    ) -> dict[str, Any]:
        """Records of one type that match, with the records they refer to if asked.

        `where` is an expression in the language of the rules, over `record` and
        `actor`. It sees only what the actor may read: a path through a record
        they cannot read gives None. `include` names reference fields whose
        records come back under `included`.

        At most `limit` records come back, in ID order. When there are more,
        `more` is true and `next` is the value to pass as `after`. With `count`,
        the answer is only how many match, and no record is shown.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise WorldError(400, "invalid_query", "limit must be a whole number from 1 to 1000.")
        if after is not None and not isinstance(after, str):
            raise WorldError(400, "invalid_query", "after must be the next value from an earlier answer.")
        fields = require_type(self.types, type_name)["fields"]
        filters = self._filters(type_name, [] if filters is None else filters)
        if where is not None:
            try:
                if not isinstance(where, str) or len(where) > 2000:
                    raise rules.RuleError("where must be an expression of up to 2,000 characters.")
                if sum(1 for _ in ast.walk(rules.parse(where, tuple(self.types)))) > 200:
                    raise rules.RuleError("where is too large.")
            except rules.RuleError as error:
                raise WorldError(400, "invalid_query", str(error), {"language": WHERE_HELP}) from error
        include = [] if include is None else include
        if not isinstance(include, list) or any(not isinstance(name, str) for name in include):
            raise WorldError(400, "invalid_query", "include must be a list of reference field names.")
        followable = [name for name, spec in fields.items() if spec["type"] == "reference" or spec.get("items", {}).get("type") == "reference"]
        if set(include) - set(followable):
            raise WorldError(400, "invalid_query", "include names a field that is not a reference.", {"references": followable})
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            readable: dict[tuple[str, str], bool] = {}

            def visible(record: dict[str, Any] | None) -> bool:
                if record is None:
                    return False
                key = (record["type"], record["id"])
                if key not in readable:
                    readable[key] = self._can_read(connection, actor, record)
                return readable[key]

            def lookup(kind: str, identifier: str) -> dict[str, Any] | None:
                record = self._raw(connection, kind, identifier)
                return record if visible(record) else None

            world = rules.World(self.types, lookup, lambda kind, terms=(): [
                record for record in self._all(connection, kind, terms) if visible(record)])
            tree = None if where is None else rules.parse(where, tuple(self.types))

            def evaluation(record: dict[str, Any]) -> rules.Evaluation:
                node = world.node(record)
                return rules.Evaluation(world, world.node(actor), node, node, lambda target: True)

            def matches(record: dict[str, Any]) -> bool:
                if not all(self._matches(record, item) for item in filters):
                    return False
                return where is None or rules.holds(evaluation(record), where)

            try:
                # Let the indexes find candidates for the parts of the query that name exact values.
                terms = [((item["field"],), "eq", item["value"]) for item in filters if item["op"] == "eq" and item["field"] in fields]
                if tree is not None:
                    terms += evaluation({"type": type_name, "id": "", "data": {}}).narrowing([tree], "record", {"actor": world.node(actor)})
                found, total, cursor, more = [], 0, after, False
                while not more:
                    chunk = self._all(connection, type_name, terms, after=cursor, limit=500)
                    for record in chunk:
                        if visible(record) and matches(record):
                            total += 1
                            if not count:
                                if len(found) == limit:
                                    more = True
                                    break
                                found.append(record)
                    if len(chunk) < 500:
                        break
                    cursor = chunk[-1]["id"]
            except rules.RuleError as error:
                raise WorldError(400, "invalid_query", str(error), {"language": WHERE_HELP}) from error
            if count:
                return {"count": total}
            included: dict[tuple[str, str], dict[str, Any]] = {}
            for record in found:
                for field, target, target_id in references(self.types, type_name, record["data"]):
                    if field.split("[")[0] in include and (target, target_id) not in included:
                        related = lookup(target, target_id)
                        if related is not None:
                            included[target, target_id] = related
            return {
                "records": self._viewed(connection, actor_id, agent, "found by a query", [
                    self._public(connection, actor, record) for record in found]),
                "included": self._viewed(connection, actor_id, agent, "included in a query", [
                    self._public(connection, actor, record) for record in included.values()]),
                "more": more, **({"next": found[-1]["id"]} if more else {}),
            }

    def _authorize_fields(
        self, connection: sqlite3.Connection, actor: dict[str, Any], proposed: dict[str, Any],
        before: dict[str, Any],
    ) -> None:
        fields = self.types[proposed["type"]]["fields"]
        refused = sorted(
            field for field, spec in fields.items()
            if "write_rule" in spec and proposed["data"].get(field) != before["data"].get(field)
            and not self._holds(connection, spec["write_rule"], actor, proposed, before, proposed)
        )
        if refused:
            raise WorldError(
                403, "permission_denied", "This actor is not allowed to change these fields.",
                {"fields": refused, "rules": {field: fields[field]["write_rule"] for field in refused}},
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
        proposed = {**(before or {}), "type": type_name, "id": record_id, "data": data}
        self._check_constraints(connection, actor, proposed, before, "update" if before else "create")

    def _provenance(
        self, connection: sqlite3.Connection, actor_id: str, action: str,
        provenance: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if provenance is None:
            provenance = {}
        if not isinstance(provenance, dict) or set(provenance) - {"requested_by", "reason", "evidence", "agent"}:
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
        return {"requested_by": requester, "reason": reason, "evidence": json.loads(serialized),
                "agent": _agent(provenance.get("agent"))}

    def _insert(
        self, connection: sqlite3.Connection, type_name: str, record_id: str,
        data: dict[str, Any], now: str,
    ) -> dict[str, Any]:
        connection.execute(
            "INSERT INTO records (type, id, data, version, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
            (type_name, record_id, _json(data), now, now),
        )
        record = self._raw(connection, type_name, record_id)
        self._index_references(connection, record)
        return record

    def _audit(
        self, connection: sqlite3.Connection, action: str, actor_id: str,
        provenance: dict[str, Any], before: dict[str, Any] | None,
        after: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO events (type, record_id, action, at, actor, requested_by, reason, evidence, before_state, after_state, agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                after["type"], after["id"], action, after["updated_at"], actor_id,
                provenance["requested_by"], provenance["reason"], _json(provenance["evidence"]),
                _json(before) if before is not None else None, _json(after), provenance["agent"],
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
        with self._transaction(write=True) as connection:
            return self._create(connection, self._actor(connection, actor_id), type_name, data, provenance)

    def _create(
        self, connection: sqlite3.Connection, actor: dict[str, Any], type_name: str,
        data: dict[str, Any], provenance: dict[str, Any] | None,
    ) -> dict[str, Any]:
        actor_id = actor["id"]
        require_type(self.types, type_name)
        data = validate_data(self.types, type_name, data, actor_id=actor_id)
        record_id = f"{type_name.lower()}-{uuid4().hex}"
        rule = self.types[type_name]["rules"]["create"]
        if not self._holds(connection, rule, actor, {"type": type_name, "id": record_id, "data": data}, None):
            raise WorldError(403, "permission_denied", "This actor is not allowed to create this record.", {"rule": rule})
        self._validate_relationships(connection, actor, type_name, record_id, data, None)
        provenance = self._provenance(connection, actor_id, "create", provenance)
        record = self._insert(connection, type_name, record_id, data, _now())
        self._audit(connection, "create", actor_id, provenance, None, record)
        return self._public(connection, actor, record)

    def update(
        self, actor_id: str, type_name: str, record_id: str, changes: dict[str, Any],
        expected_version: int, provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._transaction(write=True) as connection:
            return self._update(connection, self._actor(connection, actor_id), type_name, record_id, changes, expected_version, provenance)

    def _update(
        self, connection: sqlite3.Connection, actor: dict[str, Any], type_name: str, record_id: str,
        changes: dict[str, Any], expected_version: int, provenance: dict[str, Any] | None,
    ) -> dict[str, Any]:
        actor_id = actor["id"]
        require_type(self.types, type_name)
        before = self._get_authorized(connection, actor, type_name, record_id)
        if not self._can_update(connection, actor, before):
            raise WorldError(
                403, "permission_denied", "This actor is not allowed to update this record.",
                {"rule": self.types[type_name]["rules"]["update"]},
            )
        self._check_version(before, expected_version)
        changes = validate_data(self.types, type_name, changes, partial=True, actor_id=actor_id)
        if not changes:
            raise WorldError(422, "empty_changes", "An update must include at least one field.")
        data = validate_data(self.types, type_name, {**before["data"], **changes}, actor_id=actor_id)
        proposed = {**before, "data": data}
        self._authorize_fields(connection, actor, proposed, before)
        if not self._can_update(connection, actor, proposed):
            raise WorldError(
                403, "permission_denied", "This actor would not be allowed to update the record after this change.",
                {"rule": self.types[type_name]["rules"]["update"]},
            )
        self._validate_relationships(connection, actor, type_name, record_id, data, before)
        provenance = self._provenance(connection, actor_id, "update", provenance)
        connection.execute(
            "UPDATE records SET data = ?, version = version + 1, updated_at = ? WHERE type = ? AND id = ?",
            (_json(data), _now(), type_name, record_id),
        )
        after = self._raw(connection, type_name, record_id)
        self._index_references(connection, after)
        self._audit(connection, "update", actor_id, provenance, before, after)
        if type_name == self.definition.actor_type and record_id == actor_id:
            actor = after
        return self._public(connection, actor, after)

    def delete(
        self, actor_id: str, type_name: str, record_id: str, expected_version: int,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._transaction(write=True) as connection:
            return self._delete(connection, self._actor(connection, actor_id), type_name, record_id, expected_version, provenance)

    def _delete(
        self, connection: sqlite3.Connection, actor: dict[str, Any], type_name: str, record_id: str,
        expected_version: int, provenance: dict[str, Any] | None,
    ) -> dict[str, Any]:
        actor_id = actor["id"]
        require_type(self.types, type_name)
        before = self._get_authorized(connection, actor, type_name, record_id)
        rule = self.types[type_name]["rules"]["delete"]
        if not self._holds(connection, rule, actor, before, before):
            raise WorldError(403, "permission_denied", "This actor is not allowed to delete this record.", {"rule": rule})
        self._check_version(before, expected_version)
        inbound = self._inbound(connection, type_name, record_id)
        if inbound:
            raise WorldError(409, "record_referenced", "Remove live relationships before deleting this record.", {"references": inbound})
        self._check_constraints(connection, actor, before, before, "delete")
        provenance = self._provenance(connection, actor_id, "delete", provenance)
        now = _now()
        connection.execute(
            "UPDATE records SET version = version + 1, updated_at = ?, deleted_at = ? WHERE type = ? AND id = ?",
            (now, now, type_name, record_id),
        )
        after = self._raw(connection, type_name, record_id, include_deleted=True)
        self._index_references(connection, after, live=False)
        self._audit(connection, "delete", actor_id, provenance, before, after)
        return self._public(connection, actor, after)

    def apply(
        self, actor_id: str, steps: list[dict[str, Any]], provenance: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Make several changes as one. Either every step is saved or none is.

        A step is {"create": TYPE, "data": {...}}, {"update": TYPE, "id": ID,
        "changes": {...}, "expected_version": N}, or {"delete": TYPE, "id": ID,
        "expected_version": N}. A create may add "as": NAME, and a later step
        may then write "$NAME" where it needs that record's ID.
        """
        if not isinstance(steps, list) or not 1 <= len(steps) <= 50 or any(not isinstance(step, dict) for step in steps):
            raise WorldError(400, "invalid_steps", "steps must be a list of 1 to 50 objects.", {"expected": APPLY_HELP})
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            names: dict[str, str] = {}

            def resolve(value: Any) -> Any:
                if isinstance(value, str) and value.startswith("$") and value != "$actor":
                    if value[1:] not in names:
                        raise WorldError(422, "unknown_name", f"No earlier step is named {value[1:]}.", {"names": list(names)})
                    return names[value[1:]]
                if isinstance(value, list):
                    return [resolve(item) for item in value]
                return value

            results = []
            for index, step in enumerate(steps):
                actor = self._actor(connection, actor_id)  # an earlier step may have changed the actor's own record
                operations = [key for key in ("create", "update", "delete") if key in step]
                try:
                    if len(operations) != 1:
                        raise WorldError(400, "invalid_steps", "Each step is one create, update, or delete.", {"expected": APPLY_HELP})
                    operation, type_name = operations[0], step[operations[0]]
                    if operation == "create":
                        fields = step.get("data")
                        if not isinstance(fields, dict):
                            raise WorldError(422, "invalid_data", 'A create step gives the record\'s fields under "data".', {"expected": APPLY_HELP})
                        record = self._create(connection, actor, type_name, {key: resolve(value) for key, value in fields.items()}, provenance)
                        name = step.get("as")
                        if name is not None:
                            if not isinstance(name, str) or not name.isidentifier() or name == "actor" or name in names:
                                raise WorldError(422, "invalid_name", '"as" must be a new name made of letters, digits, and underscores.')
                            names[name] = record["id"]
                    elif operation == "update":
                        fields = step.get("changes")
                        if not isinstance(fields, dict):
                            raise WorldError(422, "invalid_data", 'An update step gives the changed fields under "changes".', {"expected": APPLY_HELP})
                        record = self._update(connection, actor, type_name, resolve(step.get("id")),
                                              {key: resolve(value) for key, value in fields.items()}, step.get("expected_version"), provenance)
                    else:
                        record = self._delete(connection, actor, type_name, resolve(step.get("id")), step.get("expected_version"), provenance)
                except WorldError as error:
                    # Leaving the transaction by an exception undoes the earlier steps.
                    error.details = {**error.details, "step": index, "saved": False}
                    error.message = f"Step {index} was refused, so nothing was saved. {error.message}"
                    raise
                results.append(record)
            return results

    def history(
        self, actor_id: str, type_name: str, record_id: str, *, agent: str | None = None,
    ) -> list[dict[str, Any]]:
        require_type(self.types, type_name)
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            record = self._get_authorized(connection, actor, type_name, record_id, include_deleted=True)
            self._viewed(connection, actor_id, agent, "history read", [record])
            return [
                {
                    "id": row["id"], "type": row["type"], "record_id": row["record_id"],
                    "action": row["action"], "at": row["at"], "actor": row["actor"], "agent": row["agent"],
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
        Who is the actor, followed by "via" and the agent when software acted for them.
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
        who = lambda row: row["actor"] + (f" via {row['agent']}" if row["agent"] else "")
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            if record_id is not None:
                self._get_authorized(connection, actor, type_name, record_id, include_deleted=True)
            entries = [
                (row["id"], 0, 0, row["at"], who(row), verbs[row["action"]], row["type"], row["record_id"], self._happened(row))
                for row in connection.execute(f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?", (*params, limit))
            ] + [
                (row["after_event"], 1, row["id"], row["at"], who(row), "viewed", row["type"], row["record_id"], row["how"].capitalize() + ".")
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
