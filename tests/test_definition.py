"""Bounded definition grammar and database identity for the shared engine."""

from contextlib import closing
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from world import WorldDefinition, WorldError, WorldStore


TEAM_PATH = Path(__file__).resolve().parents[1] / "definitions" / "team.world.json"


class DefinitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.document = json.loads(TEAM_PATH.read_text())

    def assert_error(self, status, code, operation):
        with self.assertRaises(WorldError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status, caught.exception.message)
        self.assertEqual(caught.exception.code, code)

    def store(self, path="world.sqlite3", definition=None):
        store = WorldStore(self.directory / path, definition)
        self.addCleanup(store.close)
        return store

    def test_loader_rejects_unknown_grammar_and_invalid_relationship_paths(self):
        invalid = []

        document = deepcopy(self.document)
        document["expression"] = "actor.role == admin"
        invalid.append(document)

        document = deepcopy(self.document)
        document["types"]["Task"]["fields"]["status"]["type"] = "computed"
        invalid.append(document)

        document = deepcopy(self.document)
        document["types"]["Task"]["policy"]["scope"] = "title.project"
        invalid.append(document)

        document = deepcopy(self.document)
        document["types"]["Task"]["policy"]["create"] = "expression"
        invalid.append(document)

        document = deepcopy(self.document)
        document["types"]["Task"]["constraints"][0]["members"] = "project.name"
        invalid.append(document)

        document = deepcopy(self.document)
        document["types"]["Task"]["constraints"] = [{"kind": "custom", "expression": "owner in project.members"}]
        invalid.append(document)

        document = deepcopy(self.document)
        document["types"]["Task"]["constraints"] = [{"kind": "same_reference", "left": "owner", "right": "project"}]
        invalid.append(document)

        document = deepcopy(self.document)
        document["types"]["Project"]["fields"]["members"]["nullable"] = True
        invalid.append(document)

        document = deepcopy(self.document)
        document["types"]["Person"]["policy"]["admin_fields"] = []
        invalid.append(document)

        for index, document in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_error(400, "invalid_definition", lambda: WorldDefinition(document))

    def test_integer_bounds_use_minimum_and_maximum_and_reject_invalid_descriptors(self):
        self.document["types"]["Task"]["fields"]["estimate"] = {
            "type": "integer", "minimum": -2, "maximum": 8, "required": True, "default": 0,
        }
        definition = WorldDefinition(self.document)
        store = self.store(definition=definition)
        record = store.update("maya", "Task", "homepage", {"estimate": -2}, 1)
        self.assertEqual(record["data"]["estimate"], -2)
        record = store.update("maya", "Task", "homepage", {"estimate": 8}, record["version"])
        for value in (-3, 9, True, 1.5):
            with self.subTest(value=value):
                self.assert_error(422, "invalid_field", lambda: store.update(
                    "maya", "Task", "homepage", {"estimate": value}, record["version"],
                ))
        for descriptor in (
            {"min": 0}, {"max": 8}, {"minimum": True}, {"minimum": 9, "maximum": 8},
            {"minimum": 1, "default": 0},
        ):
            with self.subTest(descriptor=descriptor):
                document = deepcopy(self.document)
                document["types"]["Task"]["fields"]["estimate"].update(descriptor)
                self.assert_error(400, "invalid_definition", lambda: WorldDefinition(document))

    def test_transitions_require_complete_known_enum_successors(self):
        valid = {"field": "status", "allowed": {"todo": ["doing"], "doing": ["done"], "done": []}}
        self.document["types"]["Task"]["transitions"] = [valid]
        WorldDefinition(self.document)
        for transition in (
            {"field": "title", "allowed": {}},
            {"field": "status", "allowed": {"todo": ["doing"]}},
            {"field": "status", "allowed": {"todo": ["invented"], "doing": ["done"], "done": []}},
            {"field": "status", "allowed": {"todo": ["doing", "doing"], "doing": ["done"], "done": []}},
        ):
            with self.subTest(transition=transition):
                document = deepcopy(self.document)
                document["types"]["Task"]["transitions"] = [transition]
                self.assert_error(400, "invalid_definition", lambda: WorldDefinition(document))

    def test_admin_fields_protect_creation_and_updates_while_allowing_declared_defaults(self):
        self.document["types"]["Task"]["fields"]["internal_note"] = {"type": "text", "default": ""}
        self.document["types"]["Task"]["policy"]["admin_fields"] = ["internal_note"]
        store = self.store(definition=WorldDefinition(self.document))
        before = store.list_records("ben")
        self.assert_error(403, "permission_denied", lambda: store.create("maya", "Task", {
            "title": "Protected note", "project": "website", "internal_note": "Admin review",
        }))
        self.assertEqual(store.list_records("ben"), before)
        record = store.create("maya", "Task", {"title": "Default note", "project": "website"})
        self.assertEqual(record["data"]["internal_note"], "")
        self.assert_error(403, "permission_denied", lambda: store.update(
            "maya", "Task", record["id"], {"internal_note": "Admin review"}, record["version"],
        ))
        self.assertEqual(len(store.history("ben", "Task", record["id"])), 1)
        changed = store.update("ben", "Task", record["id"], {"internal_note": "Admin review"}, record["version"])
        self.assertEqual(changed["data"]["internal_note"], "Admin review")

    def test_load_rejects_duplicate_keys_nonfinite_values_and_unreadable_files(self):
        path = self.directory / "invalid.world.json"
        for content in ('{"id":"team","id":"other"}', '{"seed":NaN}', '{'):
            with self.subTest(content=content):
                path.write_text(content)
                self.assert_error(400, "invalid_definition", lambda: WorldDefinition.load(path))
        self.assert_error(400, "invalid_definition", lambda: WorldDefinition.load(self.directory / "missing.json"))

    def test_seed_requires_valid_typed_ids_live_targets_and_an_admin(self):
        invalid = []
        document = deepcopy(self.document)
        document["seed"].append(deepcopy(document["seed"][0]))
        invalid.append(document)
        document = deepcopy(self.document)
        document["seed"][0]["data"]["role"] = "member"
        invalid.append(document)
        document = deepcopy(self.document)
        document["seed"][6]["data"]["owner"] = "missing"
        invalid.append(document)
        document = deepcopy(self.document)
        document["seed"][6]["data"]["due"] = {"relative_days": True}
        invalid.append(document)
        document = deepcopy(self.document)
        document["seed"][6]["data"]["title"] = {"relative_days": 0}
        invalid.append(document)
        for index, document in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_error(400, "invalid_definition", lambda: WorldDefinition(document))

    def test_reversed_seed_order_and_declared_actor_type_use_the_same_engine(self):
        self.document["id"] = "different-actors"
        self.document["actor_type"] = "Member"
        self.document["types"]["Member"] = self.document["types"].pop("Person")
        for spec in self.document["types"].values():
            for field in spec["fields"].values():
                reference = field.get("items", field)
                if reference.get("target") == "Person":
                    reference["target"] = "Member"
        for record in self.document["seed"]:
            if record["type"] == "Person":
                record["type"] = "Member"
        self.document["seed"].reverse()
        definition = WorldDefinition(self.document)
        store = self.store(definition=definition)
        self.assertEqual(definition.actor_type, "Member")
        self.assertEqual(set(definition.actor_ids), {"ben", "maya", "alex", "sam"})
        self.assertEqual(store.describe("maya")["actor"]["type"], "Member")
        self.assertEqual(store.describe("maya")["actor_type"], "Member")
        self.assertEqual(len(store.list_records("ben")), 14)
        changed = store.update("maya", "Member", "maya", {"name": "Maya R."}, 1)
        self.assertEqual(changed["data"]["name"], "Maya R.")
        comment = store.create("maya", "Comment", {"body": "Shared actor default", "task": "homepage"})
        self.assertEqual(comment["data"]["author"], "maya")
        self.assertEqual(store.history("maya", "Comment", comment["id"])[0]["requested_by"], "maya")
        self.assert_error(400, "unknown_type", lambda: store.get_record("ben", "Person", "ben"))
        self.assert_error(403, "permission_denied", lambda: store.update("maya", "Member", "maya", {"role": "admin"}, 2))
        self.assert_error(409, "last_admin", lambda: store.update("ben", "Member", "ben", {"role": "member"}, 1))

    def test_database_binding_rejects_another_id_without_record_or_history_changes(self):
        store = self.store()
        before = store.list_records("ben")
        events = store.history("ben", "Task", "documentation")
        self.document["id"] = "other-world"
        other = WorldDefinition(self.document)
        self.assert_error(409, "definition_mismatch", lambda: WorldStore(store.path, other))
        self.assertEqual(store.list_records("ben"), before)
        self.assertEqual(store.history("ben", "Task", "documentation"), events)
        reopened = self.store(definition=WorldDefinition.default())
        self.assertEqual(reopened.list_records("ben"), before)
        with closing(sqlite3.connect(store.path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM world_meta WHERE key = 'definition_id'").fetchone()[0], "team")
            schema = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'records'").fetchone()[0]
            self.assertNotIn("type IN", schema)

    def test_legacy_team_database_binds_without_reseeding_or_migrating_its_table(self):
        path = self.directory / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("""
                CREATE TABLE records (
                    type TEXT NOT NULL CHECK (type IN ('Person', 'Project', 'Task', 'Comment')),
                    id TEXT NOT NULL, data TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version > 0),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,
                    PRIMARY KEY (type, id)
                )
            """)
        original = self.store("legacy.sqlite3")
        original.update("ben", "Task", "documentation", {"status": "done"}, 1)
        original.delete("ben", "Comment", "documentation-notes", 1)
        records = original.list_records("ben")
        history = original.history("ben", "Task", "documentation")
        with closing(sqlite3.connect(path)) as connection:
            table = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'records'").fetchone()[0]
            event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            connection.execute("DELETE FROM world_meta WHERE key = 'definition_id'")
            connection.commit()
        self.document["id"] = "other-world"
        self.assert_error(409, "definition_mismatch", lambda: WorldStore(path, WorldDefinition(self.document)))
        reopened = self.store("legacy.sqlite3")
        self.assertEqual(reopened.list_records("ben"), records)
        self.assertEqual(reopened.history("ben", "Task", "documentation"), history)
        self.assertEqual([event["action"] for event in reopened.history("ben", "Comment", "documentation-notes")], ["create", "delete"])
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT sql FROM sqlite_master WHERE name = 'records'").fetchone()[0], table)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], event_count)
            self.assertEqual(connection.execute("SELECT value FROM world_meta WHERE key = 'definition_id'").fetchone()[0], "team")

    def test_relative_seed_dates_do_not_mutate_the_loaded_definition(self):
        definition = WorldDefinition.default()
        early = definition.seed_records(date(2026, 10, 1))
        later = definition.seed_records(date(2026, 10, 8))
        self.assertEqual(next(data["due"] for _, record_id, data in early if record_id == "documentation"), "2026-09-30")
        self.assertEqual(next(data["due"] for _, record_id, data in later if record_id == "documentation"), "2026-10-07")
        self.assertEqual(definition.seed_records(date(2026, 10, 1)), early)


if __name__ == "__main__":
    unittest.main()
