"""Prototype fact ledger with governed, bounded reads and atomic updates.

Accepted field assertions and retractions are authoritative. SQLite ``records``
is a rebuildable projection. This experiment reuses the existing World policies;
it still scans the small world in Python and makes no storage-speed claim.
Transactions accept one to ten distinct record updates. Inherited create/delete
operations also append facts, each in its own accepted commit.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from world.schema import WorldError, references, require_type, validate_data
from world.store import WorldStore, _json, _now


class FactWorldStore(WorldStore):
    """An isolated prototype, suitable only for disposable experiment databases."""

    def _preflight(self) -> None:
        """Inspect existing files read-only before enabling WAL or creating tables."""
        if self._uri:
            return
        database = Path(self.path)
        if not database.exists():
            return
        connection = None
        try:
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, isolation_level=None)
            connection.row_factory = sqlite3.Row
            schema = [row for row in connection.execute("SELECT type, name FROM sqlite_master")
                      if not row["name"].startswith("sqlite_")]
            tables = {row["name"] for row in schema if row["type"] == "table"}
            if not schema:
                return
            binding = None
            if "world_meta" in tables:
                binding = connection.execute("SELECT value FROM world_meta WHERE key = 'definition_id'").fetchone()
            if binding and binding["value"] != self.definition.id:
                raise WorldError(409, "definition_mismatch", "This database belongs to a different world definition.")
            required = {"world_meta", "records", "fact_commits", "accepted_facts"}
            if not required <= tables or not self._head(connection):
                raise WorldError(409, "not_fact_world", "Use a disposable fact-world database, not an existing record service or other database.")
            if not binding:
                raise WorldError(409, "ledger_unbound", "An accepted fact ledger requires its world binding.")
        except sqlite3.Error as error:
            raise WorldError(409, "not_fact_world", "The existing file is not a readable fact-world database.") from error
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        self._preflight()
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS world_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    type TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version > 0),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,
                    PRIMARY KEY (type, id)
                );
                CREATE INDEX IF NOT EXISTS records_live_type ON records (type, deleted_at);
                CREATE TABLE IF NOT EXISTS fact_commits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    actor TEXT NOT NULL, requested_by TEXT NOT NULL,
                    reason TEXT NOT NULL, evidence TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accepted_facts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id INTEGER NOT NULL REFERENCES fact_commits(id),
                    type TEXT NOT NULL, record_id TEXT NOT NULL,
                    attribute TEXT NOT NULL, value TEXT NOT NULL,
                    asserted INTEGER NOT NULL CHECK (asserted IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS accepted_facts_record
                    ON accepted_facts (type, record_id, transaction_id, sequence);
                CREATE TRIGGER IF NOT EXISTS facts_no_update BEFORE UPDATE ON accepted_facts
                    BEGIN SELECT RAISE(ABORT, 'Accepted facts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS facts_no_delete BEFORE DELETE ON accepted_facts
                    BEGIN SELECT RAISE(ABORT, 'Accepted facts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS commits_no_update BEFORE UPDATE ON fact_commits
                    BEGIN SELECT RAISE(ABORT, 'Accepted commits are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS commits_no_delete BEFORE DELETE ON fact_commits
                    BEGIN SELECT RAISE(ABORT, 'Accepted commits are append-only'); END;
            """)
        finally:
            connection.close()
        with self._transaction(write=True) as connection:
            binding = connection.execute("SELECT value FROM world_meta WHERE key = 'definition_id'").fetchone()
            if binding and binding["value"] != self.definition.id:
                raise WorldError(409, "definition_mismatch", "This database belongs to a different world definition.")
            if self._head(connection):
                if not binding:
                    raise WorldError(409, "ledger_unbound", "An accepted fact ledger requires its world binding.")
                self._rebuild(connection)
                self._validate_world(connection)
                if not self._admin_count(connection):
                    raise WorldError(409, "last_admin", "The accepted world must retain an admin.")
                return
            if connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] or connection.execute(
                "SELECT value FROM world_meta WHERE key = 'seed_version'"
            ).fetchone():
                raise WorldError(409, "not_fact_world", "Use a disposable fact-world database, not an existing record service database.")
            if not binding:
                connection.execute("INSERT INTO world_meta (key, value) VALUES ('definition_id', ?)", (self.definition.id,))
            now = _now()
            seed = []
            for type_name, record_id, data in self.definition.seed_records(datetime.now(timezone.utc).date()):
                data = validate_data(type_name, data, types=self.definition.types, actor_id=self.definition.actor_ids[0])
                seed.append(self._insert(connection, type_name, record_id, data, now))
            self._validate_world(connection)
            for record in seed:
                self._audit(connection, "create", "system", {
                    "requested_by": "system", "reason": "Initial records from the world definition.",
                    "evidence": [{"source": "world.definition", "id": self.definition.id, "version": self.definition.version}],
                }, None, record)
            connection.execute("INSERT INTO world_meta (key, value) VALUES ('seed_version', '1')")

    @staticmethod
    def _head(connection: sqlite3.Connection) -> int:
        return connection.execute("SELECT COALESCE(MAX(id), 0) FROM fact_commits").fetchone()[0]

    def head(self) -> int:
        with self._transaction() as connection:
            return self._head(connection)

    @staticmethod
    def _attributes(record: dict[str, Any] | None) -> dict[str, Any]:
        if record is None:
            return {}
        attributes = deepcopy(record["data"])
        for field in ("version", "created_at", "updated_at", "deleted_at"):
            if field in record:
                attributes[f"@{field}"] = record[field]
        return attributes

    def _audit(self, connection, action, actor_id, provenance, before, after) -> None:
        # Connections live for one transaction, so this temporary row groups all
        # its accepted changes without process-wide mutable transaction state.
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS fact_transaction_context (id INTEGER NOT NULL)")
        context = connection.execute("SELECT id FROM fact_transaction_context").fetchone()
        if context:
            transaction_id = context["id"]
        else:
            transaction_id = connection.execute(
                "INSERT INTO fact_commits (at, actor, requested_by, reason, evidence) VALUES (?, ?, ?, ?, ?)",
                (after["updated_at"], actor_id, provenance["requested_by"], provenance["reason"], _json(provenance["evidence"])),
            ).lastrowid
            connection.execute("INSERT INTO fact_transaction_context (id) VALUES (?)", (transaction_id,))
        previous, following = self._attributes(before), self._attributes(after)
        for attribute in sorted(set(previous) | set(following)):
            if attribute in previous and attribute in following and previous[attribute] == following[attribute]:
                continue
            if attribute in previous:
                connection.execute(
                    "INSERT INTO accepted_facts (transaction_id, type, record_id, attribute, value, asserted) VALUES (?, ?, ?, ?, ?, 0)",
                    (transaction_id, after["type"], after["id"], attribute, _json(previous[attribute])),
                )
            if attribute in following:
                connection.execute(
                    "INSERT INTO accepted_facts (transaction_id, type, record_id, attribute, value, asserted) VALUES (?, ?, ?, ?, ?, 1)",
                    (transaction_id, after["type"], after["id"], attribute, _json(following[attribute])),
                )
        # The accepted facts supply history. No duplicate before/after event log
        # is stored in this candidate.

    def _state(self, connection: sqlite3.Connection, snapshot: int) -> dict[tuple[str, str], dict[str, Any]]:
        attributes: dict[tuple[str, str], dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT * FROM accepted_facts WHERE transaction_id <= ? ORDER BY sequence", (snapshot,),
        ):
            record = attributes.setdefault((row["type"], row["record_id"]), {})
            if row["asserted"]:
                record[row["attribute"]] = json.loads(row["value"])
            else:
                record.pop(row["attribute"], None)
        records = {}
        for (type_name, record_id), values in attributes.items():
            record = {
                "type": type_name, "id": record_id,
                "data": {key: value for key, value in values.items() if not key.startswith("@")},
                "version": values["@version"], "created_at": values["@created_at"],
                "updated_at": values["@updated_at"],
            }
            if "@deleted_at" in values:
                record.update({"deleted": True, "deleted_at": values["@deleted_at"]})
            records[type_name, record_id] = record
        return records

    def _rebuild(self, connection: sqlite3.Connection) -> int:
        state = self._state(connection, self._head(connection))
        connection.execute("DELETE FROM records")
        for record in state.values():
            connection.execute(
                "INSERT INTO records (type, id, data, version, created_at, updated_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["type"], record["id"], _json(record["data"]), record["version"],
                 record["created_at"], record["updated_at"], record.get("deleted_at")),
            )
        return len(state)

    def rebuild_projection(self) -> dict[str, int]:
        """Replay accepted facts. This never reseeds or changes accepted history."""
        with self._transaction(write=True) as connection:
            count = self._rebuild(connection)
            self._validate_world(connection)
            return {"snapshot": self._head(connection), "records": count}

    def _current_state(self, connection: sqlite3.Connection) -> dict:
        """Read the trusted current projection once within a transaction."""
        return {(row["type"], row["id"]): self._decode(row)
                for row in connection.execute("SELECT * FROM records ORDER BY type, id")}

    def _read_metadata(self, state: dict) -> tuple[int, set[tuple[str, str]]]:
        live = [record for record in state.values() if not record.get("deleted")]
        admins = sum(record["type"] == self.definition.actor_type and record["data"]["role"] == "admin"
                     for record in live)
        inbound = {(target, target_id) for record in live
                   for _, target, target_id in references(record["type"], record["data"], types=self.definition.types)}
        return admins, inbound

    def _public(self, connection, actor, record, *, state=None, metadata=None):
        state = self._current_state(connection) if state is None else state
        admins, inbound = self._read_metadata(state) if metadata is None else metadata
        public = deepcopy(record)
        public["data"] = {
            field: self._project_path(actor["id"], record, field, state, state)
            for field in record["data"]
        }
        last_admin = (record["type"] == self.definition.actor_type and record["data"]["role"] == "admin" and admins == 1)
        public["capabilities"] = {
            "read": self._readable_state(actor, record, state),
            "update": self._updatable_state(actor, record, state),
            "delete": bool(actor["data"]["role"] == "admin" and not record.get("deleted")
                           and not last_admin and (record["type"], record["id"]) not in inbound),
        }
        return public

    def list_records(self, actor_id: str, type_name: str | None = None) -> list[dict[str, Any]]:
        if type_name is not None:
            require_type(type_name, self.definition.types)
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            state = self._current_state(connection)
            metadata = self._read_metadata(state)
            return [self._public(connection, actor, record, state=state, metadata=metadata)
                    for record in state.values() if not record.get("deleted")
                    and (type_name is None or record["type"] == type_name)
                    and self._readable_state(actor, record, state)]

    def get_record(self, actor_id: str, type_name: str, record_id: str) -> dict[str, Any]:
        require_type(type_name, self.definition.types)
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            record = self._get_authorized(connection, actor, type_name, record_id)
            return self._public(connection, actor, record)

    def query(self, actor_id: str, type_name: str, filters=None) -> list[dict[str, Any]]:
        require_type(type_name, self.definition.types)
        filters = self._filters(type_name, [] if filters is None else filters)
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            state = self._current_state(connection)
            metadata = self._read_metadata(state)
            result = []
            for record in state.values():
                if record["type"] != type_name or record.get("deleted") or not self._readable_state(actor, record, state):
                    continue
                public = self._public(connection, actor, record, state=state, metadata=metadata)
                if all(self._matches(public, item) for item in filters):
                    result.append(public)
            return result

    def describe(self, actor_id: str) -> dict[str, Any]:
        description = super().describe(actor_id)
        description.update({
            "snapshot": self.head(),
            "protocol": {
                "name": "agent-world-facts-prototype", "version": "0.1",
                "select": {"reference_paths": True, "reverse_includes": True, "consistent_snapshot": True,
                           "max_records": 100, "max_includes": 10, "max_fields": 32},
                "transact": {"operations": ["update"], "max_operations": 10,
                             "atomic": True, "expected_version": "required", "distinct_records": True},
                "history": {"as_of": "accepted commit integer", "permissions": "historical and current intersection"},
            },
            "storage": {
                "prototype": True, "authority": "accepted_facts and fact_commits",
                "projection": "records, rebuilt from facts on startup",
                "metadata_attributes": ["@version", "@created_at", "@updated_at", "@deleted_at"],
                "limits": "SQLite and whole-world Python scans; no storage performance claim",
            },
        })
        return description

    @staticmethod
    def _query_error(message: str) -> None:
        raise WorldError(400, "invalid_query", message)

    def _path(self, type_name: str, path: Any) -> None:
        if path in ("id", "type", "version", "created_at", "updated_at"):
            return
        if not isinstance(path, str):
            self._query_error("A selected path must be text.")
        try:
            self.definition.field_at_path(type_name, path)
        except WorldError as error:
            raise WorldError(400, "invalid_query", error.message) from error

    def _selection(self, type_name: str, selection: Any) -> list[str]:
        if not isinstance(selection, list) or not 1 <= len(selection) <= 32:
            self._query_error("select must contain one to thirty-two field paths.")
        for path in selection:
            self._path(type_name, path)
        if len(set(selection)) != len(selection):
            self._query_error("Selected paths must be distinct.")
        return selection

    def _limit(self, limit: Any) -> int:
        if type(limit) is not int or not 1 <= limit <= 100:
            self._query_error("A query limit must be an integer from one to one hundred.")
        return limit

    def _request(self, request: Any) -> dict[str, Any]:
        keys = {"type", "filters", "select", "include", "as_of", "limit"}
        if not isinstance(request, dict) or set(request) - keys or not {"type", "select"} <= set(request):
            self._query_error("A select request requires type and select, with optional filters, include, as_of, and limit.")
        type_name = request["type"]
        require_type(type_name, self.definition.types)
        filters = request.get("filters", [])
        if not isinstance(filters, list) or len(filters) > 32:
            self._query_error("A select request accepts at most thirty-two root-field filters.")
        try:
            filters = self._filters(type_name, filters)
        except WorldError as error:
            raise WorldError(400, "invalid_query", error.message) from error
        selection = self._selection(type_name, request["select"])
        includes = request.get("include", [])
        if not isinstance(includes, list) or len(includes) > 10:
            self._query_error("include must contain at most ten reverse relationships.")
        names = set()
        validated = []
        for include in includes:
            keys = {"name", "type", "via", "select", "order_by", "descending", "limit"}
            if not isinstance(include, dict) or set(include) - keys or not {"name", "type", "via", "select"} <= set(include):
                self._query_error("An include requires name, type, via, and select.")
            name = include["name"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or name in names:
                self._query_error("Include names must be distinct identifiers.")
            names.add(name)
            related_type = require_type(include["type"], self.definition.types)
            via = include["via"]
            if not isinstance(via, str) or via not in related_type["fields"]:
                self._query_error("An include via must name a root field on the related type.")
            descriptor = related_type["fields"][via]
            if descriptor["type"] != "reference" or descriptor["target"] != type_name:
                self._query_error("An include via must reference the requested root type.")
            selected = self._selection(include["type"], include["select"])
            if "order_by" in include:
                self._path(include["type"], include["order_by"])
            if type(include.get("descending", False)) is not bool:
                self._query_error("descending must be a boolean.")
            validated.append({**include, "select": selected, "limit": self._limit(include.get("limit", 100)),
                              "descending": include.get("descending", False)})
        return {**request, "filters": filters, "select": selection, "include": validated,
                "limit": self._limit(request.get("limit", 100))}

    @staticmethod
    def _resolve_state(record: dict[str, Any], path: str, state: dict, types: dict) -> Any:
        current = record
        parts = path.split(".")
        for index, field in enumerate(parts):
            value = current["data"].get(field)
            if index == len(parts) - 1 or value is None:
                return value
            descriptor = types[current["type"]]["fields"][field]
            current = state.get((descriptor["target"], value))
            if current is None:
                return None
        return None

    def _scope_state(self, record: dict[str, Any], state: dict) -> dict[str, Any] | None:
        scope = self.definition.types[record["type"]]["policy"]["scope"]
        if scope is None:
            return None
        if scope == "":
            return record
        target_id = self._resolve_state(record, scope, state, self.definition.types)
        return state.get((self.definition.scope_type(record["type"]), target_id))

    def _readable_state(self, actor: dict[str, Any], record: dict[str, Any], state: dict) -> bool:
        policy = self.definition.types[record["type"]]["policy"]
        if actor["data"]["role"] == "admin" or policy["read"] == "authenticated":
            return True
        scope = self._scope_state(record, state)
        return bool(scope and (scope["data"]["visibility"] == "team" or actor["id"] in scope["data"]["members"]))

    def _updatable_state(self, actor: dict[str, Any], record: dict[str, Any], state: dict) -> bool:
        if record.get("deleted"):
            return False
        if actor["data"]["role"] == "admin":
            return True
        if actor["data"]["role"] != "member":
            return False
        policy = self.definition.types[record["type"]]["policy"]
        if policy["update"] == "self":
            return actor["id"] == record["id"]
        if policy["update"] != "member":
            return False
        scope = self._scope_state(record, state)
        return bool(scope and actor["id"] in scope["data"]["members"])

    def _visible(self, actor_id: str, record: dict[str, Any], historical: dict, current: dict) -> bool:
        old_actor = historical.get((self.definition.actor_type, actor_id))
        actor = current.get((self.definition.actor_type, actor_id))
        now = current.get((record["type"], record["id"]))
        return bool(old_actor and actor and now and not old_actor.get("deleted") and not actor.get("deleted")
                    and self._readable_state(old_actor, record, historical) and self._readable_state(actor, now, current))

    def _project_path(self, actor_id: str, record: dict[str, Any], path: str, historical: dict, current: dict) -> Any:
        if path in ("id", "type", "version", "created_at", "updated_at"):
            return record[path]
        parts = path.split(".")
        for index, field in enumerate(parts):
            descriptor = self.definition.types[record["type"]]["fields"][field]
            value = record["data"].get(field)
            if descriptor["type"] == "reference" and value is not None:
                target = historical.get((descriptor["target"], value))
                if not target or target.get("deleted") or not self._visible(actor_id, target, historical, current):
                    return None
                if index < len(parts) - 1:
                    record = target
                    continue
            elif descriptor["type"] == "list" and value is not None:
                value = [record_id for record_id in value
                         if (target := historical.get((descriptor["items"]["target"], record_id)))
                         and not target.get("deleted") and self._visible(actor_id, target, historical, current)]
            return deepcopy(value)
        return None

    def _project(self, actor_id: str, record: dict[str, Any], selection: list[str], historical: dict, current: dict) -> dict:
        actor = current[self.definition.actor_type, actor_id]
        old_actor = historical[self.definition.actor_type, actor_id]
        now = current[record["type"], record["id"]]
        can_update = self._updatable_state(actor, now, current) and self._updatable_state(old_actor, record, historical)
        return {
            "type": record["type"], "id": record["id"], "version": record["version"],
            "values": {path: self._project_path(actor_id, record, path, historical, current) for path in selection},
            "next": {transition["field"]: deepcopy(transition["allowed"].get(record["data"][transition["field"]], [])) if can_update else []
                     for transition in self.definition.types[record["type"]].get("transitions", [])},
        }

    def _read_states(self, connection, actor_id, as_of):
        self._actor(connection, actor_id)
        head = self._head(connection)
        snapshot = head if as_of is None else as_of
        if type(snapshot) is not int or snapshot < 1 or snapshot > head or not connection.execute(
            "SELECT id FROM fact_commits WHERE id = ?", (snapshot,),
        ).fetchone():
            raise WorldError(400, "invalid_snapshot", "as_of must name an existing accepted commit.")
        current = self._state(connection, head)
        historical = current if snapshot == head else self._state(connection, snapshot)
        return snapshot, historical, current

    def select(self, actor_id: str, request: dict[str, Any]) -> dict[str, Any]:
        request = self._request(request)
        with self._transaction() as connection:
            snapshot, historical, current = self._read_states(connection, actor_id, request.get("as_of"))
            selected = []
            for key, record in sorted(historical.items()):
                if key[0] != request["type"] or record.get("deleted") or not self._visible(actor_id, record, historical, current):
                    continue
                # Predicates see the same redacted reference values as projection.
                safe = {**record, "data": {field: self._project_path(actor_id, record, field, historical, current)
                                          for field in self.definition.types[record["type"]]["fields"]}}
                if not all(self._matches(safe, item) for item in request["filters"]):
                    continue
                result = self._project(actor_id, record, request["select"], historical, current)
                result["related"] = {}
                for include in request["include"]:
                    related = [item for related_key, item in sorted(historical.items())
                               if related_key[0] == include["type"] and not item.get("deleted")
                               and item["data"].get(include["via"]) == record["id"]
                               and self._visible(actor_id, item, historical, current)]
                    if "order_by" in include:
                        path = include["order_by"]
                        def sort_key(item):
                            value = self._project_path(actor_id, item, path, historical, current)
                            return value is not None, value
                        try:
                            related.sort(key=sort_key, reverse=include["descending"])
                        except TypeError as error:
                            raise WorldError(400, "invalid_query", "order_by must select comparable scalar values.") from error
                    result["related"][include["name"]] = [
                        self._project(actor_id, item, include["select"], historical, current)
                        for item in related[:include["limit"]]
                    ]
                selected.append(result)
                if len(selected) == request["limit"]:
                    break
            return {"snapshot": snapshot, "records": selected}

    def transact(self, actor_id: str, operations: list[dict[str, Any]], provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(operations, list) or not 1 <= len(operations) <= 10:
            raise WorldError(400, "invalid_transaction", "A transaction requires one to ten distinct record updates.")
        with self._transaction(write=True) as connection:
            actor = self._actor(connection, actor_id)
            provenance = self._provenance(connection, actor_id, "transaction", provenance)
            pending = []
            seen = set()
            now = _now()
            for operation in operations:
                if not isinstance(operation, dict) or set(operation) != {"type", "id", "changes", "expected_version"}:
                    raise WorldError(400, "invalid_transaction", "Each update requires type, id, changes, and expected_version.")
                type_name, record_id = operation["type"], operation["id"]
                require_type(type_name, self.definition.types)
                before = self._get_authorized(connection, actor, type_name, record_id)
                key = type_name, record_id
                if key in seen:
                    raise WorldError(400, "duplicate_operation", "A transaction can update each record only once.")
                seen.add(key)
                if not self._can_update(connection, actor, before):
                    self._deny()
                self._check_version(before, operation["expected_version"])
                changes = validate_data(type_name, operation["changes"], types=self.definition.types, partial=True, actor_id=actor_id)
                if not changes:
                    raise WorldError(422, "empty_changes", "An update must include at least one field.")
                self._authorize_fields(actor, type_name, changes, before)
                data = validate_data(type_name, {**before["data"], **changes}, types=self.definition.types, actor_id=actor_id)
                after = {**before, "data": data, "version": before["version"] + 1, "updated_at": now}
                self._validate_transitions(before, after)
                pending.append((before, after))
            # Apply the proposed projection in the same uncommitted write
            # transaction, then validate the entire final state. This permits
            # related records to move together without invalid intermediates.
            for before, after in pending:
                connection.execute("UPDATE records SET data = ?, version = ?, updated_at = ? WHERE type = ? AND id = ?",
                                   (_json(after["data"]), after["version"], now, after["type"], after["id"]))
            for before, after in pending:
                if not self._can_update(connection, actor, after):
                    self._deny("The actor must be a member of both the current and destination scopes.")
            self._validate_world(connection)
            if not self._admin_count(connection):
                raise WorldError(409, "last_admin", "The world must retain at least one admin.")
            for before, after in pending:
                self._audit(connection, "update", actor_id, provenance, before, after)
            actor = self._actor(connection, actor_id)
            state = self._current_state(connection)
            metadata = self._read_metadata(state)
            return {"snapshot": self._head(connection),
                    "records": [self._public(connection, actor, after, state=state, metadata=metadata) for _, after in pending
                                if self._can_read(connection, actor, after)]}

    def update(self, actor_id, type_name, record_id, changes, expected_version, provenance=None):
        """Keep inherited CRUD callers on the same governed atomic update path."""
        result = self.transact(actor_id, [{"type": type_name, "id": record_id, "changes": changes,
                                          "expected_version": expected_version}], provenance)
        return result["records"][0]

    def _commit_provenance(self, connection, row, actor_id, current, states) -> dict[str, Any]:
        transaction = row["transaction_id"] if "transaction_id" in row.keys() else row["id"]
        previous = connection.execute("SELECT COALESCE(MAX(id), 0) FROM fact_commits WHERE id < ?", (transaction,)).fetchone()[0]
        for snapshot in (transaction, previous):
            if snapshot not in states:
                states[snapshot] = self._state(connection, snapshot)
        after, before = states[transaction], states[previous]
        visible = True
        for touched in connection.execute(
            "SELECT DISTINCT type, record_id FROM accepted_facts WHERE transaction_id = ?", (transaction,),
        ):
            key = touched["type"], touched["record_id"]
            if key not in after or not self._visible(actor_id, after[key], after, current) or (
                key in before and not self._visible(actor_id, before[key], before, current)
            ):
                visible = False
                break
        result = {"actor": row["actor"], "requested_by": row["requested_by"],
                  "reason": row["reason"] if visible else "Commit provenance is outside this actor's access.",
                  "evidence": json.loads(row["evidence"]) if visible else [], "at": row["at"]}
        if not visible:
            result["redacted"] = True
        return result

    def _authorize_historical_record(self, actor_id, type_name, record_id, historical, current):
        record = historical.get((type_name, record_id))
        if record is None:
            raise WorldError(404, "not_found", "The record does not exist in that snapshot.")
        if not self._visible(actor_id, record, historical, current):
            self._deny("This actor is not allowed to read that record in both snapshots.")
        return record

    def facts(self, actor_id: str, type_name: str, record_id: str, as_of: int | None = None) -> dict[str, Any]:
        """Return accepted user-field facts; reserved metadata stays in storage.

        Assertions are visible only if their accepted state was readable.
        Retractions additionally check the state whose old value they retract.
        All checks intersect with the actor's current permissions.
        """
        require_type(type_name, self.definition.types)
        if not isinstance(record_id, str) or not record_id:
            raise WorldError(400, "invalid_id", "A record ID must be a nonempty string.")
        with self._transaction() as connection:
            snapshot, historical, current = self._read_states(connection, actor_id, as_of)
            self._authorize_historical_record(actor_id, type_name, record_id, historical, current)
            states = {snapshot: historical}
            facts = []
            rows = connection.execute(
                "SELECT f.*, c.at, c.actor, c.requested_by, c.reason, c.evidence FROM accepted_facts f "
                "JOIN fact_commits c ON c.id = f.transaction_id "
                "WHERE f.type = ? AND f.record_id = ? AND f.transaction_id <= ? ORDER BY f.sequence",
                (type_name, record_id, snapshot),
            )
            for row in rows:
                if row["attribute"].startswith("@"):
                    continue
                at = row["transaction_id"]
                if at not in states:
                    states[at] = self._state(connection, at)
                state = states[at]
                record = state.get((type_name, record_id))
                if not record or not self._visible(actor_id, record, state, current):
                    continue
                if not row["asserted"]:
                    previous = connection.execute("SELECT COALESCE(MAX(id), 0) FROM fact_commits WHERE id < ?", (at,)).fetchone()[0]
                    if previous not in states:
                        states[previous] = self._state(connection, previous)
                    old = states[previous].get((type_name, record_id))
                    if not old or not self._visible(actor_id, old, states[previous], current):
                        continue
                # A reference fact must not disclose an unreadable target ID.
                value = json.loads(row["value"])
                probe = {**record, "data": {**record["data"], row["attribute"]: value}}
                safe = self._project_path(actor_id, probe, row["attribute"], state, current)
                if safe != value:
                    continue
                facts.append({"type": type_name, "id": record_id, "attribute": row["attribute"],
                              "value": value, "transaction": at, "asserted": bool(row["asserted"]),
                              "provenance": self._commit_provenance(connection, row, actor_id, current, states)})
            return {"snapshot": snapshot, "facts": facts}

    def history(self, actor_id: str, type_name: str, record_id: str) -> list[dict[str, Any]]:
        """Derive record history with permission checks on each historical state."""
        require_type(type_name, self.definition.types)
        if not isinstance(record_id, str) or not record_id:
            raise WorldError(400, "invalid_id", "A record ID must be a nonempty string.")
        with self._transaction() as connection:
            _, _, current = self._read_states(connection, actor_id, None)
            self._authorize_historical_record(actor_id, type_name, record_id, current, current)
            result = []
            states = {}
            rows = connection.execute(
                "SELECT c.* FROM fact_commits c WHERE EXISTS "
                "(SELECT 1 FROM accepted_facts f WHERE f.transaction_id = c.id AND f.type = ? AND f.record_id = ?) ORDER BY c.id",
                (type_name, record_id),
            )
            for row in rows:
                previous = connection.execute("SELECT COALESCE(MAX(id), 0) FROM fact_commits WHERE id < ?", (row["id"],)).fetchone()[0]
                before_state = self._state(connection, previous)
                after_state = self._state(connection, row["id"])
                before = before_state.get((type_name, record_id))
                after = after_state[type_name, record_id]
                if not self._visible(actor_id, after, after_state, current) or (
                    before is not None and not self._visible(actor_id, before, before_state, current)
                ):
                    continue
                # Project fields before returning full records so a history view
                # cannot circumvent the read path's reference redaction.
                def public_state(record, state):
                    if record is None:
                        return None
                    return {**deepcopy(record), "data": {
                        field: self._project_path(actor_id, record, field, state, current)
                        for field in self.definition.types[type_name]["fields"] if field in record["data"]
                    }}
                result.append({"id": row["id"], "transaction": row["id"], "type": type_name,
                               "record_id": record_id, "action": "create" if before is None else "delete" if after.get("deleted") else "update",
                               **self._commit_provenance(connection, row, actor_id, current, states), "before": public_state(before, before_state),
                               "after": public_state(after, after_state)})
            return result
