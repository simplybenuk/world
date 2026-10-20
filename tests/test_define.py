"""A world's definition changes while it runs: governed, checked against every record, and in the audit."""

from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from world.definition import DataDefinition
from world.schema import WorldError
from world.server import AccessTokens, WorldHTTPServer
from world.store import WorldStore


ADMIN = "actor.role == 'admin'"
EVERYONE = "actor.id is not None"
WORLD = {
    "name": "studio", "title": "Studio", "version": "1", "actor_type": "Person",
    "rules": {"define": ADMIN},
    "types": {
        "Person": {
            "description": "Someone in the studio.",
            "fields": {"name": {"type": "text", "required": True},
                       "role": {"type": "enum", "values": ["admin", "member"], "default": "member", "write_rule": ADMIN}},
            "rules": {"read": EVERYONE, "create": ADMIN, "update": ADMIN, "delete": ADMIN},
        },
        "Task": {
            "description": "Something to do.",
            "fields": {"title": {"type": "text", "required": True, "max_length": 200},
                       "status": {"type": "enum", "values": ["todo", "doing", "done"], "default": "todo"},
                       "assignee": {"type": "reference", "target": "Person", "nullable": True, "default": None}},
            "rules": {"read": EVERYONE, "create": EVERYONE, "update": EVERYONE, "delete": EVERYONE},
        },
    },
    "records": [
        {"type": "Person", "id": "ben", "data": {"name": "Ben", "role": "admin"}},
        {"type": "Person", "id": "maya", "data": {"name": "Maya"}},
    ],
}


def world(change=None):
    data = deepcopy(WORLD)
    if change:
        change(data)
    return DataDefinition(data)


def task_type(store):
    return deepcopy(store.definition.as_data()["types"]["Task"])


class DefineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "world.sqlite3"
        self.store = self.open()
        self.tasks = [self.store.create("ben", "Task", {"title": title}, {"reason": "Seeded for the test."})
                      for title in ("Write the post", "Book venue")]

    def open(self, definition=None, **options):
        store = WorldStore(self.path, definition or world(), **options)
        self.addCleanup(store.close)
        return store

    def define(self, store=None, **change):
        store = store or self.store
        return store.define("ben", {"expected_revision": store._revision, "reason": "Testing.", **change}, agent="claude")

    def refused(self, code, **change):
        before = self.store.definition_at("ben")
        with self.assertRaises(WorldError) as raised:
            self.define(**change)
        self.assertEqual(raised.exception.code, code, raised.exception.message)
        self.assertEqual(self.store.definition_at("ben"), before)  # nothing was saved
        return raised.exception

    def test_a_world_starts_at_revision_one_and_describe_says_who_may_change_it(self):
        self.assertEqual(self.store.describe("ben")["definition"],
                         {"revision": 1, "capabilities": {"define": True, "rules": {"define": ADMIN}}})
        self.assertFalse(self.store.describe("maya")["definition"]["capabilities"]["define"])
        self.assertEqual(self.store.definition_at("ben")["definition"]["types"]["Task"], WORLD["types"]["Task"])

    def test_adding_a_type_and_a_field_is_saved_and_in_the_audit(self):
        task = task_type(self.store)
        task["fields"]["idea"] = {"type": "reference", "target": "PostIdea", "nullable": True}
        idea = {"description": "An idea for a post.", "fields": {"text": {"type": "text", "required": True}},
                "rules": {"read": EVERYONE, "create": EVERYONE, "update": EVERYONE, "delete": EVERYONE}}
        result = self.define(types={"PostIdea": idea, "Task": task}, reason="Keep post ideas as their own type.")
        self.assertEqual(result, {"revision": 2, "saved": True, "records": {"Task": {"checked": 2, "rewritten": 0}},
                                  "summary": "Added type PostIdea. Task: added field idea."})
        created = self.store.create("maya", "PostIdea", {"text": "Pi hardware"})
        self.store.update("maya", "Task", self.tasks[0]["id"], {"idea": created["id"]}, 1)
        line = [line for line in self.store.audit("maya") if "changed definition" in line][0]
        self.assertRegex(line, r"ben via claude  changed definition  World 2  Added type PostIdea\. Task: added field idea\. "
                               r"Reason: Keep post ideas as their own type\.$")
        # The world keeps it: reopened with the file it started from, it runs revision 2.
        reopened = self.open()
        self.assertIn("PostIdea", reopened.types)
        self.assertEqual(reopened.describe("ben")["definition"]["revision"], 2)

    def test_only_those_the_define_rule_allows_may_change_it(self):
        with self.assertRaises(WorldError) as raised:
            self.store.define("maya", {"expected_revision": 1, "title": "Mine"})
        self.assertEqual((raised.exception.status, raised.exception.details["rule"]), (403, ADMIN))
        no_rule = WorldStore(":memory:", world(lambda data: data.pop("rules")))
        self.addCleanup(no_rule.close)
        with self.assertRaisesRegex(WorldError, "No one may change"):
            no_rule.define("ben", {"expected_revision": 1, "title": "Mine"})
        self.assertFalse(no_rule.describe("ben")["definition"]["capabilities"]["define"])

    def test_a_stale_or_missing_revision_is_refused(self):
        with self.assertRaises(WorldError) as raised:
            self.store.define("ben", {"title": "Renamed"})
        self.assertEqual(raised.exception.status, 428)
        self.define(title="Renamed")
        with self.assertRaises(WorldError) as raised:
            self.store.define("ben", {"expected_revision": 1, "title": "Again"})
        self.assertEqual((raised.exception.code, raised.exception.details["current_revision"]), ("revision_conflict", 2))

    def test_a_field_with_a_default_is_filled_in_and_each_record_rewritten_in_its_history(self):
        task = task_type(self.store)
        task["fields"]["priority"] = {"type": "enum", "values": ["low", "high"], "required": True, "default": "low"}
        self.assertEqual(self.define(types={"Task": task})["records"], {"Task": {"checked": 2, "rewritten": 2}})
        record = self.store.get_record("ben", "Task", self.tasks[0]["id"])
        self.assertEqual((record["data"]["priority"], record["version"]), ("low", 2))
        event = self.store.history("ben", "Task", self.tasks[0]["id"])[-1]
        self.assertEqual((event["actor"], event["agent"], event["reason"]), ("ben", "claude", "Definition revision 2: Testing."))
        self.assertTrue(any('priority from null to "low"' in line for line in self.store.audit("ben")))
        # A client holding the old version reads again, since it read the record under the old definition.
        with self.assertRaises(WorldError) as raised:
            self.store.update("ben", "Task", self.tasks[0]["id"], {"status": "done"}, 1)
        self.assertEqual(raised.exception.code, "version_conflict")

    def test_a_required_field_no_record_has_is_refused_naming_the_records(self):
        task = task_type(self.store)
        task["fields"]["due"] = {"type": "date", "required": True}
        error = self.refused("definition_refused", types={"Task": task})
        self.assertEqual(sorted(error.details["records"]), sorted(task["id"] for task in self.tasks))
        self.assertIn("2 Task records would not fit the new definition, so nothing changed. due is required.", error.message)
        self.assertNotIn("due", self.store.types["Task"]["fields"])
        # The agent's way through: add it optional, fill it in, then require it.
        task["fields"]["due"] = {"type": "date", "nullable": True}
        self.define(types={"Task": task})
        for record in self.tasks:
            self.store.update("ben", "Task", record["id"], {"due": "2026-11-01"}, 1)
        task["fields"]["due"] = {"type": "date", "required": True}
        self.assertEqual(self.define(types={"Task": task})["revision"], 3)

    def test_removing_a_field_rewrites_records_and_history_keeps_the_values(self):
        self.store.update("ben", "Task", self.tasks[0]["id"], {"assignee": "maya"}, 1)
        task = task_type(self.store)
        del task["fields"]["assignee"]
        self.assertEqual(self.define(types={"Task": task})["summary"], "Task: removed field assignee.")
        self.assertNotIn("assignee", self.store.get_record("ben", "Task", self.tasks[0]["id"])["data"])
        self.assertEqual(self.store.history("ben", "Task", self.tasks[0]["id"])[-1]["before"]["data"]["assignee"], "maya")
        self.assertTrue(any('assignee from "maya" to null' in line for line in self.store.audit("ben")))
        self.assertEqual(self.store._inbound_for("Person", "maya"), [])  # the reference index forgot it

    def test_a_renamed_field_and_renamed_values_carry_over(self):
        self.store.update("ben", "Task", self.tasks[0]["id"], {"assignee": "maya", "status": "doing"}, 1)
        task = task_type(self.store)
        task["fields"]["owner"] = task["fields"].pop("assignee")
        task["fields"]["status"]["values"] = ["backlog", "doing", "done"]
        task["fields"]["status"]["default"] = "backlog"
        result = self.define(types={"Task": task}, rename={"Task.assignee": "owner"}, values={"Task.status": {"todo": "backlog"}})
        self.assertEqual(result["summary"], "Task: renamed assignee to owner, changed field status, status value todo became backlog.")
        first, second = (self.store.get_record("ben", "Task", task["id"])["data"] for task in self.tasks)
        self.assertEqual((first["owner"], first["status"], second["status"]), ("maya", "doing", "backlog"))
        self.assertEqual(self.store._inbound_for("Person", "maya"), [{"type": "Task", "id": self.tasks[0]["id"], "field": "owner"}])

    def test_a_value_records_hold_cannot_be_dropped_without_saying_where_it_goes(self):
        task = task_type(self.store)
        task["fields"]["status"]["values"] = ["doing", "done"]
        task["fields"]["status"]["default"] = "doing"
        error = self.refused("definition_refused", types={"Task": task})
        self.assertEqual(error.details["code"], "invalid_enum")
        self.refused("invalid_values", types={"Task": task}, values={"Task.status": {"todo": "someday"}})

    def test_narrowing_a_field_or_adding_a_constraint_is_refused_when_records_break_it(self):
        task = task_type(self.store)
        task["fields"]["title"]["max_length"] = 12
        error = self.refused("definition_refused", types={"Task": task})
        self.assertEqual(error.details["records"], [self.tasks[0]["id"]])  # "Book venue" fits
        task = task_type(self.store)
        task["constraints"] = [{"check": "record.assignee is not None", "message": "Every task needs someone.", "fields": ["assignee"]}]
        error = self.refused("definition_refused", types={"Task": task})
        self.assertEqual((error.details["code"], len(error.details["records"])), ("constraint_failed", 2))
        self.assertIn("Every task needs someone.", error.message)

    def test_a_rule_naming_a_missing_field_is_refused(self):
        task = task_type(self.store)
        task["rules"]["update"] = "record.owner == actor.id"
        error = self.refused("invalid_definition", types={"Task": task})
        self.assertIn("Task.owner, which is not a field", error.message)

    def test_a_type_is_removed_only_when_it_has_no_records(self):
        error = self.refused("type_in_use", types={"Task": None})
        self.assertEqual(len(error.details["records"]), 2)
        for record in self.tasks:
            self.store.delete("ben", "Task", record["id"], 1)
        self.assertEqual(self.define(types={"Task": None})["summary"], "Removed type Task.")
        self.assertNotIn("Task", self.store.types)
        # Its records' lines are judged by the last definition that had it, so they stay readable.
        self.assertTrue(any(f"Task {self.tasks[0]['id']}" in line for line in self.store.audit("maya")))

    def test_no_one_may_lock_themselves_out(self):
        error = self.refused("define_locked_out", rules={"define": "actor.role == 'owner'"})
        self.assertIn("ben could no longer change the definition", error.message)
        self.refused("define_locked_out", rules={})

    def test_check_reports_and_saves_nothing(self):
        task = task_type(self.store)
        task["fields"]["priority"] = {"type": "enum", "values": ["low", "high"], "default": "low"}
        result = self.define(types={"Task": task}, check=True)
        self.assertEqual((result["saved"], result["revision"], result["records"]["Task"]["rewritten"]), (False, 2, 2))
        self.assertEqual(self.store._revision, 1)
        self.assertNotIn("priority", self.store.get_record("ben", "Task", self.tasks[0]["id"])["data"])
        self.assertFalse(any("changed definition" in line for line in self.store.audit("ben")))

    def test_another_store_on_the_same_world_follows_a_change(self):
        other = self.open()
        self.define(title="Studio Two")
        self.assertEqual(other.describe("maya")["name"], "Studio Two")

    def test_a_changed_start_up_definition_is_applied_by_system(self):
        def add_field(data):
            data["types"]["Task"]["fields"]["notes"] = {"type": "text", "nullable": True}
        store = self.open(world(add_field))
        self.assertIn("notes", store.types["Task"]["fields"])
        self.assertTrue(any("system  changed definition  World 2  Task: added field notes." in line for line in store.audit("ben")))
        self.assertEqual(self.open(world(add_field))._revision, 2)  # the same file again changes nothing

    def test_a_start_up_definition_that_would_break_records_stops_the_world(self):
        def require_due(data):
            data["types"]["Task"]["fields"]["due"] = {"type": "date", "required": True}
        with self.assertRaisesRegex(ValueError, "cannot be applied. 2 Task records would not fit"):
            self.open(world(require_due))

    def test_when_both_the_file_and_the_running_world_changed_neither_is_discarded(self):
        self.define(title="Changed while running")
        retitled = lambda data: data.update(title="Changed in the file")
        with self.assertRaisesRegex(ValueError, "changed while it ran, to revision 2"):
            self.open(world(retitled))
        self.assertEqual(self.open()._revision, 2)  # the file it was started with still starts it
        self.assertEqual(self.open(world(retitled), from_file=True).definition.title, "Changed in the file")

    def test_putting_the_running_definition_into_the_file_starts_it(self):
        self.define(title="Changed while running")
        self.assertEqual(self.open(world(lambda data: data.update(title="Changed while running")))._revision, 2)


class DefineOverMCPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name)
        self.store = WorldStore(data_dir / "world.sqlite3", world())
        self.addCleanup(self.store.close)
        self.access = AccessTokens(data_dir, ("ben", "maya"))
        self.token = self.access.issue("ben", "claude")
        self.server = WorldHTTPServer(("127.0.0.1", 0), self.store, self.access)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)

    def call(self, tool, **arguments):
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", "/mcp", body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                                "params": {"name": tool, "arguments": arguments}}),
                               headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
            result = json.loads(connection.getresponse().read())["result"]
        finally:
            connection.close()
        text = result["content"][0]["text"]
        return result["isError"], text if tool == "audit" else json.loads(text)

    def test_an_agent_reads_the_definition_then_changes_it(self):
        failed, shown = self.call("define")
        self.assertFalse(failed)
        self.assertEqual(shown["revision"], 1)
        task = shown["definition"]["types"]["Task"]
        task["fields"]["idea"] = {"type": "text", "nullable": True}
        failed, checked = self.call("define", types={"Task": task}, expected_revision=1, check=True)
        self.assertEqual((failed, checked["saved"]), (False, False))
        failed, result = self.call("define", types={"Task": task}, expected_revision=1, reason="Ideas live on tasks.")
        self.assertEqual((failed, result["revision"], result["saved"]), (False, 2, True))
        _, described = self.call("describe")
        self.assertIn("idea", described["types"]["Task"]["fields"])
        _, audit = self.call("audit")
        self.assertIn("ben via claude  changed definition  World 2  Task: added field idea. Reason: Ideas live on tasks.", audit)

    def test_a_refusal_comes_back_as_a_tool_error(self):
        failed, refused = self.call("define", types={"Task": None}, expected_revision=1)
        self.assertFalse(failed)  # no records yet, so removing the type is allowed
        failed, refused = self.call("define", types={"Nope": None}, expected_revision=2)
        self.assertTrue(failed)
        self.assertEqual(refused["error"]["code"], "invalid_definition")


if __name__ == "__main__":
    unittest.main()
