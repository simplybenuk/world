"""Persistent, governed records with atomic history and optimistic concurrency."""

from __future__ import annotations

from contextlib import contextmanager
import copy
from copy import deepcopy
from datetime import datetime, timezone
import ast
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from . import rules
from .caller import as_caller, current
from .definition import DataDefinition, Definition
from .rules import RuleError
from .schema import WorldError, references, require_type, validate_data, validate_value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


APPLY_HELP = (
    '{"steps": [{"create": TYPE, "data": {...}, "as": "name"}, {"update": TYPE, "id": ID or "$name", "changes": {...}, '
    '"expected_version": N}, {"delete": TYPE, "id": ID, "expected_version": N}], "reason": WHY}'
)
INDEX_ABOVE = 12  # a world with more types than this describes itself as an index first
DEFINE_HELP = (
    '{"types": {TYPE: {...the whole type...} or null to remove it}, "rename": {"Type.old_field": "new_field"}, '
    '"values": {"Type.field": {"old value": "new value"}}, "rules": {"define": EXPRESSION}, "title": TITLE, '
    '"expected_revision": N, "reason": WHY, "check": true to see what would happen without saving}'
)
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


class _Checked(Exception):
    """Leaves a change's transaction, undoing it, with what the change would have done."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("checked")
        self.result = result


class WorldStore:
    """One database, with a new SQLite connection for every operation.

    Every writer holds a transaction while checking the actor, version,
    relationships, and capabilities. The audit event commits with the record.
    The definition supplies the types and rules; the team world is the default.
    """

    # FULL waits for the disk on every commit, so a saved change survives a power cut.
    # NORMAL does not wait. The database stays consistent, but a power cut can lose the last moments.
    synchronous = "FULL"
    as_caller = staticmethod(as_caller)

    def __init__(self, path: str | Path, definition: Definition | None = None, *, from_file: bool = False) -> None:
        """Open or create the world at path. The definition creates a new world. An existing world runs the
        definition it holds, which the given one changes only as docs/changing-a-world.md describes;
        from_file applies the given one even over changes made while the world ran."""
        if definition is None:
            from worlds.team import DEFINITION as definition
        definition.check()
        self._adopt(definition)
        self._from_file = from_file
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

    def _adopt(self, definition: Definition, revision: int = 0) -> None:
        """Run this definition, with what the engine works out from it."""
        types = definition.types
        # Every field any constraint can read, or None when that cannot be known.
        reads: set[tuple[str, str]] | None = set()
        for type_name, spec in types.items():
            for constraint in spec.get("constraints", []):
                found = rules.reads(constraint["check"], types, type_name, definition.actor_type)
                reads = None if found is None or reads is None else reads | found
        # Types whose constraints could be broken by a change to something they refer to,
        # plus the types through which such a type is reached.
        dependent = self._dependent_types(types)
        self.definition, self.types, self._dependent, self._constraint_reads, self._revision = (
            definition, types, dependent, reads, revision)

    def _held(self, data: dict[str, Any]) -> Definition:
        """A stored definition. The people seeded when the world was created come from the one it started with."""
        definition = DataDefinition(data)
        definition.actors, definition.seed_reason = self.definition.actors, self.definition.seed_reason
        return definition

    @staticmethod
    def _dependent_types(types: dict[str, Any]) -> set[str]:
        """Types worth visiting when following references back from a changed record."""
        referrers: dict[str, set[str]] = {}
        for type_name, definition in types.items():
            for spec in definition["fields"].values():
                target = spec.get("target") or spec.get("items", {}).get("target")
                if target:
                    referrers.setdefault(target, set()).add(type_name)
        dependent = {type_name for type_name, definition in types.items() if definition.get("constraints")}
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
            if self._revision:
                self._follow(connection)
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
                -- Who let software act for whom, and when that stopped. The record is the person acted for.
                CREATE TABLE IF NOT EXISTS access (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    agent TEXT,
                    action TEXT NOT NULL CHECK (action IN ('gave', 'revoked')),
                    detail TEXT NOT NULL,
                    after_event INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS access_record ON access (type, record_id, id);
                CREATE TRIGGER IF NOT EXISTS access_no_update
                    BEFORE UPDATE ON access BEGIN
                        SELECT RAISE(ABORT, 'World history is append-only');
                    END;
                CREATE TRIGGER IF NOT EXISTS access_no_delete
                    BEFORE DELETE ON access BEGIN
                        SELECT RAISE(ABORT, 'World history is append-only');
                    END;
                -- Every definition the world has run, as JSON. The latest is the one it runs.
                CREATE TABLE IF NOT EXISTS definitions (
                    revision INTEGER PRIMARY KEY,
                    at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    agent TEXT,
                    reason TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    definition TEXT NOT NULL,
                    after_event INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS definitions_no_update
                    BEFORE UPDATE ON definitions BEGIN
                        SELECT RAISE(ABORT, 'World history is append-only');
                    END;
                CREATE TRIGGER IF NOT EXISTS definitions_no_delete
                    BEFORE DELETE ON definitions BEGIN
                        SELECT RAISE(ABORT, 'World history is append-only');
                    END;
            """)
            # Databases from before agents were named gain the column, empty for old rows.
            for table in ("events", "views"):
                if "agent" not in [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN agent TEXT")
            for table in ("events", "views", "access", "definitions"):
                if "identity" not in [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN identity TEXT")
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
            self._settle_definition(connection, meta, new="seed_version" not in meta)
            self._create_indexes(connection)
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

    def _create_indexes(self, connection: sqlite3.Connection) -> None:
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

    def _settle_definition(self, connection: sqlite3.Connection, meta: dict[str, str], *, new: bool) -> None:
        """Decide which definition the world runs: the one it holds, or the one it was started with.

        A world created before definitions were kept takes the one it was started with as its first.
        After that, a changed start-up definition is applied as a change by system, unless the world's
        definition has also been changed while it ran. Then neither is discarded: the world will not start,
        unless from_file says to apply the start-up one over it.
        """
        given = self.definition.as_data()
        fingerprint = hashlib.sha256(json.dumps(given, sort_keys=True).encode()).hexdigest()
        latest = connection.execute("SELECT * FROM definitions ORDER BY revision DESC LIMIT 1").fetchone()
        if latest is None:
            reason = "The world's first definition." if new else "The definition the world ran when it began keeping them."
            self._save_definition(connection, 1, "system", None, reason, "First definition.", given)
            self._started_with(connection, fingerprint, 1)
            self._revision = 1
            return
        self._adopt(self._held(json.loads(latest["definition"])), latest["revision"])
        if meta.get("definition_file") == fingerprint:
            return
        if given == json.loads(latest["definition"]):
            self._started_with(connection, fingerprint, latest["revision"])
            return
        if latest["revision"] != int(meta.get("definition_file_revision", "0")) and not self._from_file:
            raise ValueError(
                f"{self.path} has had its definition changed while it ran, to revision {latest['revision']}, and the "
                "definition it was started with has changed too. Start it with the definition it holds, from "
                "GET /api/definition, or apply the new one over it with --world-from-file.")
        try:
            staged, _, _ = self._change(connection, None, given, None, None, "The definition the world is started with changed.", None)
        except WorldError as error:
            raise ValueError(f"The definition {self.path} was started with cannot be applied. {error.message} "
                             f"{json.dumps(error.details)}") from None
        self._adopt(staged.definition, staged._revision)
        self._started_with(connection, fingerprint, staged._revision)

    @staticmethod
    def _started_with(connection: sqlite3.Connection, fingerprint: str, revision: int) -> None:
        connection.executemany("INSERT OR REPLACE INTO world_meta (key, value) VALUES (?, ?)",
                               [("definition_file", fingerprint), ("definition_file_revision", str(revision))])

    def _follow(self, connection: sqlite3.Connection) -> None:
        """Run a definition another process has changed since this one last looked."""
        latest = connection.execute("SELECT revision, definition FROM definitions ORDER BY revision DESC LIMIT 1").fetchone()
        if latest is not None and latest["revision"] != self._revision:
            self._adopt(self._held(json.loads(latest["definition"])), latest["revision"])

    @staticmethod
    def _save_definition(
        connection: sqlite3.Connection, revision: int, actor_id: str, agent: str | None, reason: str,
        summary: str, data: dict[str, Any],
    ) -> None:
        latest = connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        connection.execute(
            "INSERT INTO definitions (revision, at, actor, agent, reason, summary, definition, after_event, identity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (revision, _now(), actor_id, agent or current().agent, reason, summary, _json(data), latest, current().identity))

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

    def record_access(self, actor_id: str, action: str, person: str, agent: str, detail: str, *, by_agent: str | None = None) -> None:
        """Add an audit line that actor_id gave an agent access to act as person, or revoked it.

        The line belongs to the person's record, so whoever may read that record sees it.
        """
        if action not in ("gave", "revoked"):
            raise ValueError("action is gave or revoked")
        with self._transaction(write=True) as connection:
            self._actor(connection, actor_id)
            latest = connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            connection.execute(
                "INSERT INTO access (type, record_id, at, actor, agent, action, detail, after_event) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.definition.actor_type, person, _now(), actor_id, _agent(by_agent), action,
                 f"{_agent(agent)} {'may act as' if action == 'gave' else 'may no longer act as'} {person}" + (f" {detail}" if detail else "") + ".",
                 latest))

    def actor_exists(self, actor_id: Any) -> bool:
        """Whether someone in the world has this id. For issuing access, so it reads no record and leaves no audit line."""
        if not isinstance(actor_id, str) or not actor_id:
            return False
        with self._transaction() as connection:
            return self._raw(connection, self.definition.actor_type, actor_id) is not None

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

    def _may_read(self, connection: sqlite3.Connection, actor: dict[str, Any], record: dict[str, Any]) -> bool:
        """Whether the actor may read a record, judged by the last definition that had its type if it has been removed."""
        if record["type"] in self.types:
            return self._can_read(connection, actor, record)
        for row in connection.execute("SELECT revision, definition FROM definitions ORDER BY revision DESC"):
            data = json.loads(row["definition"])
            if record["type"] in data["types"]:
                then = copy.copy(self)
                then._adopt(self._held(data), row["revision"])
                return then._can_read(connection, actor, record)
        return False

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
        changed = {field for field in {*record["data"], *(saved or {}).get("data", {})}
                   if record["data"].get(field) != (saved or {}).get("data", {}).get(field)}
        if self._constraint_reads is not None and not {(record["type"], field) for field in changed} & self._constraint_reads:
            return  # no constraint reads what changed, so none that was met can now fail
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
                "actor": self._public(connection, actor, actor) if self._can_read(connection, actor, actor) else {"type": actor["type"], "id": actor["id"]},
                "caller": {"agent": current().agent, "identity": current().identity},
                "query": {"operators": ["eq", "ne", "contains", "lt", "lte", "gt", "gte", "in"], "combine": "and"},
                "mutations": {"concurrency": "expected_version is required for update and delete"},
                "definition": {"revision": self._revision, "capabilities": {
                    "define": self._may_define(connection, actor), "rules": deepcopy(self.definition.rules)}},
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
            "INSERT INTO views (type, record_id, at, actor, how, after_event, agent, identity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(record["type"], record["id"], now, actor_id, how, latest, _agent(agent or current().agent), current().identity) for record in records],
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
            # A caller-supplied query must not turn an unreadable account into a field-value oracle.
            query_actor = world.node(actor if visible(actor) else {"type": actor["type"], "id": actor["id"], "data": {}})

            def evaluation(record: dict[str, Any]) -> rules.Evaluation:
                node = world.node(record)
                return rules.Evaluation(world, query_actor, node, node, lambda target: True)

            def matches(record: dict[str, Any]) -> bool:
                if not all(self._matches(record, item) for item in filters):
                    return False
                return where is None or rules.holds(evaluation(record), where)

            try:
                # Let the indexes find candidates for the parts of the query that name exact values.
                terms = [((item["field"],), "eq", item["value"]) for item in filters if item["op"] == "eq" and item["field"] in fields]
                if tree is not None:
                    terms += evaluation({"type": type_name, "id": "", "data": {}}).narrowing([tree], "record", {"actor": query_actor, "caller": current()})
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
                "agent": _agent(current().agent or provenance.get("agent"))}

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
            "INSERT INTO events (type, record_id, action, at, actor, requested_by, reason, evidence, before_state, after_state, agent, identity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                after["type"], after["id"], action, after["updated_at"], actor_id,
                provenance["requested_by"], provenance["reason"], _json(provenance["evidence"]),
                _json(before) if before is not None else None, _json(after), provenance["agent"], current().identity,
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

    def add_member(
        self, by: str, member_id: Any, data: dict[str, Any], reason: Any = None, *, by_agent: str | None = None,
    ) -> dict[str, Any]:
        """Add a person to the world, for an authorization server inviting them on behalf of someone already in it.

        The new record is of the actor type and has the id given, so it can be given access at once. The type's
        create rule is not asked: the server's key is the authority, as seeding is at start-up. Fields and
        constraints are checked as for any record, and the audit names who invited them.
        """
        actor_type = self.definition.actor_type
        if not isinstance(member_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", member_id):
            raise WorldError(422, "invalid_id", "A member's id is up to 64 lowercase letters, digits, hyphens, or underscores.")
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, by)
            if self._raw(connection, actor_type, member_id, include_deleted=True) is not None:
                raise WorldError(409, "already_exists", f"Someone in this world already has the id {member_id}.")
            data = validate_data(self.types, actor_type, data, actor_id=by)
            proposed = {"type": actor_type, "id": member_id, "data": data}
            # Who may invite is the server's to decide, but what they may grant is the world's: a guarded field
            # set to anything but its default needs the inviter to pass its write rule.
            fields = self.types[actor_type]["fields"]
            default = lambda spec: by if spec.get("default") == "$actor" else spec.get("default")
            refused = sorted(
                field for field, spec in fields.items()
                if "write_rule" in spec and ("default" not in spec or data.get(field) != default(spec))
                and not self._holds(connection, spec["write_rule"], actor, proposed, None, proposed)
            )
            if refused:
                raise WorldError(
                    403, "permission_denied", "The person inviting them is not allowed to set these fields.",
                    {"fields": refused, "rules": {field: fields[field]["write_rule"] for field in refused}},
                )
            self._validate_relationships(connection, actor, actor_type, member_id, data, None)
            provenance = self._provenance(connection, by, "create", {
                "reason": reason if reason is not None else f"Invited by {by}.", "agent": by_agent})
            record = self._insert(connection, actor_type, member_id, data, _now())
            self._audit(connection, "create", by, provenance, None, record)
            return {"type": actor_type, "id": member_id, "version": record["version"]}

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
                if isinstance(value, str) and value.startswith("$") and value not in {"$actor", "$identity"}:
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

    def definition_at(self, actor_id: str, revision: Any = None) -> dict[str, Any]:
        """The definition the world runs, or an earlier revision of it, with who made it and why."""
        with self._transaction() as connection:
            self._actor(connection, actor_id)
            if revision is None:
                row = connection.execute("SELECT * FROM definitions ORDER BY revision DESC LIMIT 1").fetchone()
            elif type(revision) is int:
                row = connection.execute("SELECT * FROM definitions WHERE revision = ?", (revision,)).fetchone()
            else:
                row = None
            if row is None:
                raise WorldError(404, "not_found", "There is no such revision of the definition.", {"revision": self._revision})
            return {"revision": row["revision"], "at": row["at"], "by": row["actor"], "agent": row["agent"], "identity": row["identity"],
                    "reason": row["reason"], "summary": row["summary"], "definition": json.loads(row["definition"])}

    def _may_define(self, connection: sqlite3.Connection, actor: dict[str, Any]) -> bool:
        rule = self.definition.rules.get("define")
        return bool(rule) and self._holds(connection, rule, actor, None, None)  # type: ignore[arg-type]

    def define(self, actor_id: str, change: Any, *, agent: str | None = None) -> dict[str, Any]:
        """Change the world's definition while it runs. See docs/changing-a-world.md.

        `types` replaces or adds each type it names, or removes it when given null. `rename` carries a field's
        values to its new name, and `values` an enum's values to new ones. The change is refused, and nothing is
        saved, if any live record would not fit it. With `check`, nothing is saved either way.
        """
        keys = {"types", "rename", "values", "rules", "title", "expected_revision", "reason", "check"}
        if not isinstance(change, dict) or set(change) - keys:
            raise WorldError(400, "invalid_definition", "That is not a change to the definition.", {"expected": DEFINE_HELP})
        reason = change.get("reason", "No reason given.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise WorldError(422, "invalid_provenance", "reason must be nonempty text of at most 4000 characters.")
        try:
            with self._transaction(write=True) as connection:
                actor = self._actor(connection, actor_id)
                rule = self.definition.rules.get("define")
                if not self._may_define(connection, actor):
                    raise WorldError(403, "permission_denied", "This actor is not allowed to change the definition."
                                     if rule else "No one may change this world's definition while it runs.", {"rule": rule})
                expected = change.get("expected_revision")
                if type(expected) is not int:
                    raise WorldError(428, "revision_required", "expected_revision must be the definition's revision, from describe.",
                                     {"current_revision": self._revision})
                if expected != self._revision:
                    raise WorldError(409, "revision_conflict", "The definition changed since it was read. Read it again.",
                                     {"expected_revision": expected, "current_revision": self._revision})
                data = self.definition.as_data()
                given = change.get("types", {})
                if not isinstance(given, dict):
                    raise WorldError(422, "invalid_definition", "types maps each type to its whole definition, or to null to remove it.")
                for type_name, spec in given.items():
                    if spec is None and type_name not in data["types"]:
                        raise WorldError(422, "invalid_definition", f"There is no type {type_name} to remove.", {"types": list(data["types"])})
                    if spec is None:
                        del data["types"][type_name]
                    else:
                        data["types"][type_name] = deepcopy(spec)
                if "rules" in change:
                    data["rules"] = deepcopy(change["rules"])
                if "title" in change:
                    data["title"] = change["title"]
                if data == self.definition.as_data() and not change.get("rename") and not change.get("values"):
                    return {"revision": self._revision, "summary": "No change.", "records": {}, "saved": False}
                staged, summary, records = self._change(connection, actor, data, change.get("rename"), change.get("values"), reason, agent)
                if not staged._may_define(connection, staged._actor(connection, actor_id)):
                    raise WorldError(409, "define_locked_out", f"After this change, {actor_id} could no longer change the "
                                     "definition, so it was refused.", {"rule": staged.definition.rules.get("define")})
                result = {"revision": staged._revision, "summary": summary, "records": records}
                if change.get("check") is True:
                    raise _Checked({**result, "saved": False})
        except _Checked as checked:
            return checked.result
        self._adopt(staged.definition, staged._revision)
        return {**result, "saved": True}

    def _change(
        self, connection: sqlite3.Connection, actor: dict[str, Any] | None, data: dict[str, Any],
        rename: Any, values: Any, reason: str, agent: str | None,
    ) -> tuple["WorldStore", str, dict[str, dict[str, int]]]:
        """Save a new definition in this transaction, and rewrite the records it changes.

        Returns a copy of this store that runs the new definition, a summary, and how many records of each
        changed type were checked and rewritten. Raises if any live record would not fit, so the caller's
        transaction saves nothing.
        """
        try:
            new = self._held(data)
            new.check()
        except RuleError as error:
            raise WorldError(422, "invalid_definition", str(error)) from None
        if new.name != self.definition.name:
            raise WorldError(422, "invalid_definition", f"A world keeps its name, {self.definition.name}.")
        if new.actor_type != self.definition.actor_type:
            raise WorldError(422, "invalid_definition", f"The people who act stay {self.definition.actor_type}.")
        old, types = self.types, new.types
        renamed, mapped = self._renames(rename, old, types), self._value_maps(values, types)
        for type_name in sorted(set(old) - set(types)):
            live = self._all(connection, type_name, limit=21)
            if live:
                raise WorldError(409, "type_in_use", f"{type_name} still has records. Delete them before removing the type.",
                                 {"type": type_name, "records": [record["id"] for record in live[:20]], "more": len(live) > 20})
        staged = copy.copy(self)
        staged._adopt(new, self._revision + 1)
        actor_id = actor["id"] if actor else "system"
        summary = self._summary(self.definition, new, renamed, mapped)
        self._save_definition(connection, staged._revision, actor_id, agent, reason, summary, new.as_data())
        staged._create_indexes(connection)
        provenance = {"requested_by": actor_id, "reason": f"Definition revision {staged._revision}: {reason}",
                      "evidence": [], "agent": agent}
        problems: dict[tuple[str, str, str], tuple[WorldError, list[str]]] = {}
        records: dict[str, dict[str, int]] = {}
        changed = sorted(name for name in types if name in old and (types[name] != old[name] or name in mapped))
        for type_name in changed:
            counts = records.setdefault(type_name, {"checked": 0, "rewritten": 0})
            for record in self._all(connection, type_name):
                counts["checked"] += 1
                fields = dict(record["data"])
                for was, now in renamed.get(type_name, {}).items():
                    if was in fields:
                        fields[now] = fields.pop(was)
                for field, mapping in mapped.get(type_name, {}).items():
                    value = fields.get(field)
                    if isinstance(value, list):
                        fields[field] = [mapping.get(item, item) if isinstance(item, str) else item for item in value]
                    elif isinstance(value, str):
                        fields[field] = mapping.get(value, value)
                fields = {field: value for field, value in fields.items() if field in types[type_name]["fields"]}
                try:
                    fields = validate_data(types, type_name, fields)
                    for field, target, target_id in references(types, type_name, fields):
                        if self._raw(connection, target, target_id) is None:
                            raise WorldError(422, "invalid_reference", f"{field} must point to a live {target}.",
                                             {"field": field, "target": target})
                except WorldError as error:
                    problems.setdefault((type_name, error.code, error.message), (error, []))[1].append(record["id"])
                    continue
                if fields != record["data"]:
                    connection.execute("UPDATE records SET data = ?, version = version + 1, updated_at = ? WHERE type = ? AND id = ?",
                                       (_json(fields), _now(), type_name, record["id"]))
                    staged._audit(connection, "update", actor_id, provenance, record, staged._raw(connection, type_name, record["id"]))
                    counts["rewritten"] += 1
                staged._index_references(connection, {**record, "data": fields})
        # A new constraint, or a record rewritten, may leave a constraint unmet anywhere, even on a type
        # the change did not name, since a constraint may count the records of any type.
        if any(counts["rewritten"] for counts in records.values()) or any(
                old.get(name, {}).get("constraints") != spec.get("constraints") for name, spec in types.items()):
            judge = staged._raw(connection, new.actor_type, actor_id) if actor else None
            for type_name, spec in types.items():
                checks = [constraint for constraint in spec.get("constraints", []) if "update" in constraint.get("on", ("create", "update"))]
                for record in staged._all(connection, type_name) if checks else []:
                    for constraint in checks:
                        if not staged._holds(connection, constraint["check"], judge, record, record):
                            error = WorldError(constraint.get("status", 422), constraint.get("code", "constraint_failed"),
                                               constraint["message"], {"constraint": constraint["check"]})
                            problems.setdefault((type_name, error.code, error.message), (error, []))[1].append(record["id"])
                            break
        if problems:
            (type_name, _, _), (error, ids) = next(iter(problems.items()))
            raise WorldError(409, "definition_refused", f"{len(ids)} {type_name} record{'' if len(ids) == 1 else 's'} would not fit "
                             f"the new definition, so nothing changed. {error.message}",
                             {**error.details, "type": type_name, "code": error.code, "records": ids[:20],
                              "more": max(0, len(ids) - 20), "other_problems": len(problems) - 1})
        return staged, summary, records

    @staticmethod
    def _renames(rename: Any, old: dict[str, Any], types: dict[str, Any]) -> dict[str, dict[str, str]]:
        """{"Type.old": "new"} as {type: {old: new}}, refused unless each names a field that moves to a new name."""
        if not rename:
            return {}
        if not isinstance(rename, dict):
            raise WorldError(422, "invalid_rename", 'rename is {"Type.old_field": "new_field"}.')
        found: dict[str, dict[str, str]] = {}
        for key, now in rename.items():
            type_name, _, was = key.partition(".")
            if type_name not in old or type_name not in types or was not in old[type_name]["fields"]:
                raise WorldError(422, "invalid_rename", f"{key} is not a field of the world as it is.")
            if not isinstance(now, str) or now not in types[type_name]["fields"]:
                raise WorldError(422, "invalid_rename", f"{key} is renamed to {now!r}, which the new {type_name} does not have.")
            if was in types[type_name]["fields"] or now in old[type_name]["fields"]:
                raise WorldError(422, "invalid_rename", f"{key} can be renamed to {now} only if {type_name} keeps neither "
                                 "the old name nor an existing field by the new one.")
            found.setdefault(type_name, {})[was] = now
        return found

    @staticmethod
    def _value_maps(values: Any, types: dict[str, Any]) -> dict[str, dict[str, dict[str, str]]]:
        """{"Type.field": {"old": "new"}} as {type: {field: {old: new}}}, each new value one the field allows."""
        if not values:
            return {}
        if not isinstance(values, dict):
            raise WorldError(422, "invalid_values", 'values is {"Type.field": {"old value": "new value"}}.')
        found: dict[str, dict[str, dict[str, str]]] = {}
        for key, mapping in values.items():
            type_name, _, field = key.partition(".")
            spec = types.get(type_name, {}).get("fields", {}).get(field)
            if spec and spec["type"] == "list":
                spec = spec["items"]
            if not spec or spec["type"] != "enum":
                raise WorldError(422, "invalid_values", f"{key} is not an enum field of the new definition.")
            if not isinstance(mapping, dict) or not all(isinstance(value, str) and value in spec["values"] for value in mapping.values()):
                raise WorldError(422, "invalid_values", f"{key} maps each old value to one of {', '.join(spec['values'])}.")
            found.setdefault(type_name, {})[field] = mapping
        return found

    @staticmethod
    def _summary(before: Definition, after: Definition, renamed: dict[str, dict[str, str]],
                 mapped: dict[str, dict[str, dict[str, str]]]) -> str:
        """What a change did, as sentences a person can read in the audit."""
        old, new = before.types, after.types
        parts = [f"Added type {name}." for name in new if name not in old]
        parts += [f"Removed type {name}." for name in old if name not in new]
        for name, now in new.items():
            was = old.get(name)
            if was is None or (was == now and name not in mapped):
                continue
            moved = renamed.get(name, {})
            notes = [f"renamed {field} to {to}" for field, to in moved.items()]
            notes += [f"added field {field}" for field in now["fields"] if field not in was["fields"] and field not in moved.values()]
            notes += [f"removed field {field}" for field in was["fields"] if field not in now["fields"] and field not in moved]
            notes += [f"changed field {field}" for field in now["fields"]
                      if field in was["fields"] and now["fields"][field] != was["fields"][field]]
            notes += [f"{field} value {a} became {b}" for field, mapping in mapped.get(name, {}).items() for a, b in mapping.items()]
            for key, label in (("rules", "rules"), ("constraints", "constraints"), ("description", "description")):
                if now.get(key) != was.get(key):
                    notes.append(f"changed {label}")
            parts.append(f"{name}: {', '.join(notes) or 'changed'}.")
        if before.rules != after.rules:
            parts.append("Changed who may change the definition.")
        if before.title != after.title:
            parts.append(f"Renamed the world {after.title}.")
        return " ".join(parts) or "No change."

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
                    "action": row["action"], "at": row["at"], "actor": row["actor"], "agent": row["agent"], "identity": row["identity"],
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
            # A field a definition change removed is in before only.
            what = "; ".join(
                f"{field} from {self._shown(before.get(field))} to {self._shown(after.get(field))}"
                for field in [*after, *(field for field in before if field not in after)] if after.get(field) != before.get(field)
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

        Each line gives when, who, what they did, the record, and the detail. Giving or revoking an agent's
        access is a line on the record of the person it acts for.
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
        who = lambda row: row["actor"] + (f" via {row['agent']}" if row["agent"] else "") + (f" as {row['identity']}" if row["identity"] else "")
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            if record_id is not None:
                self._get_authorized(connection, actor, type_name, record_id, include_deleted=True)
            entries = [
                (row["id"], 0, "", 0, 0, row["at"], who(row), verbs[row["action"]], row["type"], row["record_id"], self._happened(row))
                for row in connection.execute(f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?", (*params, limit))
            ] + [
                (row["after_event"], 1, row["at"], 0, row["id"], row["at"], who(row), "viewed", row["type"], row["record_id"], row["how"].capitalize() + ".")
                for row in connection.execute(f"SELECT * FROM views{where} ORDER BY id DESC LIMIT ?", (*params, limit))
            ] + [
                (row["after_event"], 1, row["at"], 1, row["id"], row["at"], who(row), f"{row['action']} access", row["type"], row["record_id"], row["detail"])
                for row in connection.execute(f"SELECT * FROM access{where} ORDER BY id DESC LIMIT ?", (*params, limit))
            ] + [
                # A change to the definition, which everyone may read, as they may read it through describe.
                (row["after_event"], 1, row["at"], 2, row["revision"], row["at"], who(row), "changed definition", "World",
                 str(row["revision"]), f"{row['summary']} Reason: {' '.join(row['reason'].split())}")
                for row in (connection.execute("SELECT * FROM definitions WHERE revision > 1 ORDER BY revision DESC LIMIT ?", (limit,))
                            if type_name is None else [])
            ]
            readable: dict[tuple[str, str], bool] = {}
            lines = []
            # Writers run one at a time, so event order and each view's place in it are exact. Views and access
            # lines between the same two events go in the order they were written.
            for *_, at, who, verb, kind, identifier, detail in sorted(entries)[-limit:]:
                key = (kind, identifier)
                if key not in readable:
                    record = None if kind == "World" else self._raw(connection, kind, identifier, include_deleted=True)
                    readable[key] = kind == "World" or bool(record and self._may_read(connection, actor, record))
                if readable[key]:
                    lines.append(f"{at}  {who}  {verb}  {kind} {identifier}  {detail}")
            return lines
