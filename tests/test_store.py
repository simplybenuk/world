"""Exercise persistence and server-enforced rules without a client."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from world import WorldError, WorldStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "world.sqlite3"
        self.store = WorldStore(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def assert_error(self, status, operation, code=None):
        with self.assertRaises(WorldError) as context:
            operation()
        self.assertEqual(context.exception.status, status)
        if code is not None:
            self.assertEqual(context.exception.code, code)
        self.assertIsInstance(context.exception.message, str)
        self.assertIsInstance(context.exception.details, dict)
        return context.exception

    def test_schema_is_json_and_reports_actor_and_permissions(self):
        schema = self.store.describe("maya")
        self.assertEqual(set(schema["types"]), {"Person", "Project", "Task", "Comment"})
        self.assertEqual(schema["actor"]["id"], "maya")
        self.assertEqual(schema["actor"]["data"]["role"], "member")
        self.assertTrue(schema["types"]["Task"]["capabilities"]["create"])
        self.assertFalse(schema["types"]["Person"]["capabilities"]["create"])
        self.assertEqual(schema["types"]["Task"]["fields"]["project"]["target"], "Project")
        self.assertIn("expected_version", schema["mutations"]["concurrency"])
        json.dumps(schema)
        schema["types"]["Task"]["fields"]["status"]["values"].append("invented")
        self.assertNotIn("invented", self.store.describe("ben")["types"]["Task"]["fields"]["status"]["values"])
        self.assertFalse(self.store.describe("sam")["types"]["Task"]["capabilities"]["update"])

    def test_seed_runs_once_and_changes_survive_reopen(self):
        initial = self.store.list_records("ben")
        self.assertEqual(len(initial), 14)
        record = self.store.get_record("ben", "Task", "documentation")
        self.assertEqual(record["data"]["project"], "website")
        self.store.update("ben", "Task", "documentation", {"status": "done"}, record["version"], {
            "reason": "Documentation published", "evidence": {"commit": "abc123"},
        })
        reopened = WorldStore(self.path)
        self.assertEqual(len(reopened.list_records("ben")), len(initial))
        self.assertEqual(reopened.get_record("ben", "Task", "documentation")["data"]["status"], "done")
        events = reopened.history("ben", "Task", "documentation")
        self.assertEqual(len(events), 2)
        event = events[-1]
        self.assertEqual(event["actor"], "ben")
        self.assertEqual(event["requested_by"], "ben")
        self.assertEqual(event["reason"], "Documentation published")
        self.assertEqual(event["evidence"], {"commit": "abc123"})
        self.assertEqual(event["before"]["data"]["status"], "todo")
        self.assertEqual(event["after"]["version"], 2)
        reopened.close()

    def test_private_records_are_hidden_from_reads_queries_and_history(self):
        self.assertNotIn("world", {record["id"] for record in self.store.list_records("maya", "Project")})
        self.assertNotIn("world-protocol", {record["id"] for record in self.store.query("sam", "Task")})
        self.assertEqual(len(self.store.list_records("alex", "Project")), 2)
        self.assertEqual(self.store.get_record("sam", "Project", "website")["data"]["visibility"], "team")
        self.assert_error(403, lambda: self.store.get_record("maya", "Project", "world"), "permission_denied")
        self.assert_error(403, lambda: self.store.get_record("sam", "Task", "world-protocol"))
        self.assert_error(403, lambda: self.store.history("maya", "Comment", "protocol-notes"))

    def test_viewer_cannot_mutate_even_an_accessible_record_or_self(self):
        task = self.store.get_record("sam", "Task", "documentation")
        self.assertEqual(task["capabilities"], {"read": True, "update": False, "delete": False})
        self.assert_error(403, lambda: self.store.update("sam", "Task", "documentation", {"status": "done"}, 1))
        self.assert_error(403, lambda: self.store.create("sam", "Task", {"title": "New", "project": "website"}))
        self.assert_error(403, lambda: self.store.update("sam", "Person", "sam", {"name": "Different"}, 1))
        self.assert_error(403, lambda: self.store.delete("sam", "Task", "homepage", 1))
        self.assertEqual(self.store.get_record("ben", "Task", "documentation")["version"], 1)
        self.assertEqual(len(self.store.history("ben", "Task", "documentation")), 1)

    def test_member_changes_a_task_but_cannot_elevate_access(self):
        changed = self.store.update("maya", "Task", "documentation", {"status": "doing"}, 1)
        self.assertEqual(changed["version"], 2)
        self.assertFalse(changed["capabilities"]["delete"])
        self.assert_error(403, lambda: self.store.update("maya", "Person", "maya", {"role": "admin"}, 1))
        self.assert_error(403, lambda: self.store.update("maya", "Project", "website", {"members": ["maya"]}, 1))
        self.assert_error(403, lambda: self.store.update("maya", "Project", "website", {"visibility": "private"}, 1))
        self.assert_error(403, lambda: self.store.create("maya", "Person", {"name": "Intruder", "role": "admin"}))
        self.assert_error(403, lambda: self.store.create("maya", "Project", {"name": "Other", "members": ["maya"]}))
        self.assert_error(403, lambda: self.store.delete("maya", "Task", "homepage", 1))
        profile = self.store.update("maya", "Person", "maya", {"name": "Maya R."}, 1)
        self.assertEqual(profile["data"]["name"], "Maya R.")

    def test_actor_is_resolved_from_current_database_role(self):
        self.assert_error(401, lambda: self.store.describe({"id": "ben", "role": "admin"}), "invalid_actor")
        self.assert_error(401, lambda: self.store.describe("missing"))
        self.store.update("ben", "Person", "maya", {"role": "viewer"}, 1)
        self.assertFalse(self.store.get_record("maya", "Task", "documentation")["capabilities"]["update"])
        self.assert_error(403, lambda: self.store.update("maya", "Task", "documentation", {"status": "done"}, 1))

    def test_member_must_have_access_to_both_projects_on_a_move(self):
        self.assert_error(403, lambda: self.store.create("maya", "Task", {"title": "Private", "project": "world"}))
        self.assert_error(403, lambda: self.store.update("maya", "Task", "homepage", {"project": "world", "owner": "alex"}, 1))
        self.assertEqual(self.store.get_record("ben", "Task", "homepage")["data"]["project"], "website")

    def test_defaults_and_comment_attribution_are_enforced(self):
        task = self.store.create("maya", "Task", {"title": "Check links", "project": "website"})
        self.assertEqual(task["data"]["status"], "todo")
        self.assertIsNone(task["data"]["due"])
        self.assertIsNone(task["data"]["owner"])
        comment = self.store.create("maya", "Comment", {"body": "All links checked.", "task": task["id"]})
        self.assertEqual(comment["data"]["author"], "maya")
        self.assert_error(403, lambda: self.store.create("maya", "Comment", {"body": "Pretend", "task": task["id"], "author": "ben"}))
        self.assert_error(403, lambda: self.store.update("maya", "Comment", comment["id"], {"author": "ben"}, 1))

    def test_invalid_fields_never_change_record_or_history(self):
        invalid_changes = [
            ({"status": "finished"}, "invalid_enum"),
            ({"project": "does-not-exist"}, "invalid_reference"),
            ({"owner": "website"}, "invalid_reference"),
            ({"due": "2026-02-30"}, "invalid_field"),
            ({"due": "20261003"}, "invalid_field"),
            ({"due": "tomorrow"}, "invalid_field"),
            ({"title": "  "}, "invalid_field"),
            ({"title": 12}, "invalid_field"),
            ({"project": None}, "invalid_field"),
            ({"record_version": 2}, "unknown_field"),
        ]
        for changes, code in invalid_changes:
            with self.subTest(changes=changes):
                self.assert_error(422, lambda: self.store.update("ben", "Task", "documentation", changes, 1), code)
        self.assert_error(422, lambda: self.store.create("ben", "Task", {"project": "website"}), "required_field")
        self.assert_error(422, lambda: self.store.create("ben", "Task", []), "invalid_data")
        self.assert_error(422, lambda: self.store.update("ben", "Task", "documentation", {}, 1), "empty_changes")
        self.assertEqual(self.store.get_record("ben", "Task", "documentation")["version"], 1)
        self.assertEqual(len(self.store.history("ben", "Task", "documentation")), 1)

    def test_relationship_changes_cannot_invalidate_other_records(self):
        self.assert_error(422, lambda: self.store.update("ben", "Task", "documentation", {"owner": "sam"}, 1), "invalid_relationship")
        self.assert_error(422, lambda: self.store.update("ben", "Task", "documentation", {"project": "world"}, 1), "invalid_relationship")
        self.assert_error(422, lambda: self.store.update("ben", "Project", "website", {"members": ["ben", "alex"]}, 1), "invalid_relationship")
        self.assert_error(422, lambda: self.store.create("ben", "Comment", {"body": "Outside", "task": "documentation", "author": "sam"}), "invalid_relationship")
        self.assert_error(422, lambda: self.store.create("ben", "Project", {"name": "Duplicate", "members": ["ben", "ben"]}), "invalid_field")

    def test_query_filters_support_dates_membership_and_and(self):
        today = datetime.now(timezone.utc).date().isoformat()
        tasks = self.store.query("ben", "Task", [
            {"field": "status", "op": "ne", "value": "done"},
            {"field": "due", "op": "lte", "value": today},
            {"field": "project", "op": "eq", "value": "website"},
        ])
        self.assertEqual({task["id"] for task in tasks}, {"documentation", "homepage"})
        self.assertEqual([task["id"] for task in self.store.query("ben", "Task", [{"field": "title", "op": "contains", "value": "DOCUMENTATION"}])], ["documentation"])
        projects = self.store.query("ben", "Project", [{"field": "members", "op": "contains", "value": "maya"}])
        self.assertEqual([project["id"] for project in projects], ["website"])
        assigned = self.store.query("ben", "Task", [{"field": "owner", "op": "in", "value": ["maya", "alex"]}])
        self.assertEqual(len(assigned), 4)
        self.assertEqual(len(self.store.query("ben", "Task", [{"field": "due", "op": "gt", "value": today}])), 2)
        self.assertEqual(len(self.store.query("ben", "Task", [{"field": "version", "op": "gte", "value": 1}])), 6)
        self.assertEqual(len(self.store.query("ben", "Task", [{"field": "due", "op": "lt", "value": today}])), 3)
        for filters in [{}, [{"field": "secret", "op": "eq", "value": "x"}], [{"field": "status", "op": "invented", "value": "done"}], [{"field": "status", "op": "in", "value": "done"}], [{"field": "status", "op": {}, "value": "done"}]]:
            self.assert_error(400, lambda: self.store.query("ben", "Task", filters), "invalid_query")

    def test_version_is_required_and_stale_writes_leave_no_event(self):
        for value in [None, True, 1.0, "1", 0, -1]:
            self.assert_error(428, lambda: self.store.update("ben", "Task", "homepage", {"status": "done"}, value), "version_required")
        self.store.update("ben", "Task", "homepage", {"status": "done"}, 1)
        error = self.assert_error(409, lambda: self.store.update("ben", "Task", "homepage", {"status": "todo"}, 1), "version_conflict")
        self.assertEqual(error.details["current_version"], 2)
        self.assert_error(409, lambda: self.store.delete("ben", "Task", "homepage", 1), "version_conflict")
        self.assertEqual(len(self.store.history("ben", "Task", "homepage")), 2)
        self.assertEqual(self.store.get_record("ben", "Task", "homepage")["data"]["status"], "done")

    def test_two_concurrent_writers_do_not_lose_a_change(self):
        other = WorldStore(self.path)
        barrier = Barrier(2)

        def write(store, title):
            barrier.wait(timeout=5)
            try:
                return store.update("ben", "Task", "homepage", {"title": title}, 1, {"reason": title})
            except WorldError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(write, self.store, "First writer")
            second = executor.submit(write, other, "Second writer")
            results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual([result.code for result in results if isinstance(result, WorldError)], ["version_conflict"])
        reopened = WorldStore(self.path)
        record = reopened.get_record("ben", "Task", "homepage")
        self.assertEqual(record["version"], 2)
        self.assertEqual(len(reopened.history("ben", "Task", "homepage")), 2)
        self.assertEqual(reopened.history("ben", "Task", "homepage")[-1]["reason"], record["data"]["title"])
        other.close()
        reopened.close()

    def test_audit_database_failure_rolls_back_the_record(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""
                CREATE TRIGGER reject_test_update_event BEFORE INSERT ON events
                WHEN NEW.action = 'update' BEGIN SELECT RAISE(ABORT, 'test audit failure'); END
            """)
        self.assert_error(500, lambda: self.store.update("ben", "Task", "documentation", {"status": "done"}, 1), "storage_error")
        self.assertEqual(self.store.get_record("ben", "Task", "documentation")["version"], 1)
        self.assertEqual(self.store.get_record("ben", "Task", "documentation")["data"]["status"], "todo")
        self.assertEqual(len(self.store.history("ben", "Task", "documentation")), 1)

    def test_audit_failure_rolls_back_create_and_delete(self):
        count = len(self.store.list_records("ben"))
        with patch.object(self.store, "_audit", side_effect=RuntimeError("Audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.store.create("ben", "Task", {"title": "Must roll back", "project": "website"})
            with self.assertRaises(RuntimeError):
                self.store.delete("ben", "Task", "homepage", 1)
        self.assertEqual(len(self.store.list_records("ben")), count)
        self.assertEqual(self.store.get_record("ben", "Task", "homepage")["version"], 1)
        self.assertEqual(len(self.store.history("ben", "Task", "homepage")), 1)

    def test_audit_is_append_only(self):
        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE events SET actor = 'sam'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM events")
        self.assertEqual(len(self.store.history("ben", "Task", "documentation")), 1)

    def test_provenance_cannot_override_authenticated_actor(self):
        self.assert_error(422, lambda: self.store.update("ben", "Task", "documentation", {"status": "done"}, 1, {"actor": "maya"}), "invalid_provenance")
        self.assert_error(422, lambda: self.store.update("ben", "Task", "documentation", {"status": "done"}, 1, {"requested_by": "nonexistent"}), "invalid_provenance")
        self.assert_error(422, lambda: self.store.update("ben", "Task", "documentation", {"status": "done"}, 1, {"evidence": float("nan")}), "invalid_provenance")
        self.store.update("maya", "Task", "documentation", {"status": "done"}, 1, {"requested_by": "ben", "reason": "Ben asked Maya to finish", "evidence": ["reviewed"]})
        event = self.store.history("ben", "Task", "documentation")[-1]
        self.assertEqual(event["actor"], "maya")
        self.assertEqual(event["requested_by"], "ben")

    def test_delete_keeps_history_and_is_not_reseeded(self):
        record = self.store.create("ben", "Task", {"title": "Disposable record", "project": "website"})
        self.assertTrue(record["capabilities"]["delete"])
        deleted = self.store.delete("ben", "Task", record["id"], 1, {"reason": "No longer needed"})
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["version"], 2)
        self.assertFalse(deleted["capabilities"]["update"])
        self.assert_error(404, lambda: self.store.get_record("ben", "Task", record["id"]))
        self.assert_error(422, lambda: self.store.create("ben", "Comment", {"body": "Dangling", "task": record["id"]}), "invalid_reference")
        reopened = WorldStore(self.path)
        history = reopened.history("ben", "Task", record["id"])
        self.assertEqual([event["action"] for event in history], ["create", "delete"])
        self.assertTrue(history[-1]["after"]["deleted"])
        self.assertNotIn(record["id"], {item["id"] for item in reopened.list_records("ben", "Task")})
        self.store.delete("ben", "Task", "homepage", 1)
        self.assert_error(404, lambda: WorldStore(self.path).get_record("ben", "Task", "homepage"))
        reopened.close()

    def test_deletion_cannot_leave_dangling_references(self):
        error = self.assert_error(409, lambda: self.store.delete("ben", "Task", "documentation", 1), "record_referenced")
        self.assertIn({"type": "Comment", "id": "documentation-notes", "field": "task"}, error.details["references"])
        self.assert_error(409, lambda: self.store.delete("ben", "Project", "website", 1), "record_referenced")
        self.assert_error(409, lambda: self.store.delete("ben", "Person", "maya", 1), "record_referenced")
        self.assertFalse(self.store.get_record("ben", "Task", "documentation")["capabilities"]["delete"])
        self.store.delete("ben", "Comment", "documentation-notes", 1)
        self.assertTrue(self.store.get_record("ben", "Task", "documentation")["capabilities"]["delete"])
        self.store.delete("ben", "Task", "documentation", 1)
        self.assertEqual(len(self.store.history("ben", "Task", "documentation")), 2)

    def test_world_keeps_an_admin_and_returns_current_role_capabilities(self):
        self.assert_error(409, lambda: self.store.update("ben", "Person", "ben", {"role": "member"}, 1), "last_admin")
        self.store.create("ben", "Person", {"name": "Other admin", "role": "admin"})
        changed = self.store.update("ben", "Person", "ben", {"role": "member"}, 1)
        self.assertFalse(changed["capabilities"]["delete"])
        self.assertFalse(self.store.describe("ben")["types"]["Person"]["capabilities"]["create"])


if __name__ == "__main__":
    unittest.main()
