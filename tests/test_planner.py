"""Provider-independent checks for the bounded AI client plan contract."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from client_gateway.planner import ClientError, CodexPlanner
from world.store import WorldStore


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = WorldStore(Path(self.temporary.name) / "world.sqlite3")
        self.addCleanup(self.store.close)
        self.description = self.store.describe("ben")
        self.records = self.store.list_records("ben")
        self.task = next(record for record in self.records if record["id"] == "documentation")

    def plan(self, *, proposals=None):
        return {
            "message": "The documentation change is ready to save.",
            "view": {
                "layout": "board", "title": "Website work", "description": "Visible website tasks",
                "type": "Task", "record_ids": ["documentation", "homepage"],
                "fields": ["title", "owner", "status"], "group_by": "status",
            },
            "proposals": proposals or [],
        }

    def update(self):
        return {
            "operation": "update", "type": "Task", "id": "documentation",
            "changes": [{"field": "status", "value_json": '"done"'}],
            "expected_version": self.task["version"], "reason": "Mark documentation complete",
        }

    def assert_invalid(self, plan, *, description=None, records=None):
        with self.assertRaises(ClientError) as caught:
            CodexPlanner.validate(plan, description or self.description, records or self.records)
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.code, "invalid_plan")

    def test_valid_plan_normalizes_json_values_without_saving_records(self):
        before = self.store.list_records("ben")
        proposal = self.update()
        proposal["changes"].append({"field": "owner", "value_json": "null"})
        plan = self.plan(proposals=[proposal])
        plan["view"]["record_ids"].append("documentation")
        result = CodexPlanner.validate(plan, self.description, self.records)
        self.assertEqual(result["proposals"][0]["changes"], {"status": "done", "owner": None})
        self.assertEqual(result["view"]["record_ids"], ["documentation", "homepage"])
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(self.store.list_records("ben"), before)
        self.assertEqual(self.store.history("ben", "Task", "documentation")[-1]["action"], "create")

    def test_unknown_view_types_identifiers_and_fields_are_rejected(self):
        changes = (
            ("type", "Invoice"), ("type", []), ("type", {}),
            ("record_ids", ["missing-task"]), ("record_ids", ["ben"]),
            ("record_ids", [{}]), ("fields", ["password"]), ("fields", [[]]),
            ("group_by", "secret"), ("group_by", []), ("layout", "html"),
            ("layout", []),
        )
        for key, value in changes:
            with self.subTest(key=key, value=value):
                plan = self.plan()
                plan["view"][key] = value
                self.assert_invalid(plan)

    def test_invisible_record_ids_are_rejected_for_the_current_actor(self):
        plan = self.plan()
        plan["view"]["record_ids"] = ["world-protocol"]
        self.assert_invalid(plan, description=self.store.describe("sam"), records=self.store.list_records("sam"))

    def test_unknown_proposal_targets_and_fields_are_rejected(self):
        for changes in (
            {"type": "Invoice"}, {"type": []}, {"type": {}},
            {"id": "missing-task"}, {"id": []}, {"operation": "execute"}, {"operation": []},
            {"changes": [{"field": "password", "value_json": '"secret"'}]},
            {"changes": [{"field": [], "value_json": '"done"'}]},
            {"changes": [{"field": "status", "value_json": '"done"'}, {"field": "status", "value_json": '"todo"'}]},
        ):
            with self.subTest(changes=changes):
                proposal = self.update()
                proposal.update(changes)
                self.assert_invalid(self.plan(proposals=[proposal]))

    def test_malformed_json_changes_and_unsupported_value_shapes_are_rejected(self):
        for changes in (
            {}, [None], [{"field": "status", "value_json": "done"}],
            [{"field": "status", "value_json": {"status": "done"}}],
            [{"field": "status", "value_json": "NaN"}],
            [{"field": "status", "value_json": "Infinity"}],
            [{"field": "status", "value_json": "1e999"}],
        ):
            with self.subTest(changes=changes):
                proposal = self.update()
                proposal["changes"] = changes
                self.assert_invalid(self.plan(proposals=[proposal]))

    def test_unauthorized_create_update_and_delete_are_rejected(self):
        description = self.store.describe("sam")
        records = self.store.list_records("sam")
        for operation in ("create", "update", "delete"):
            with self.subTest(operation=operation):
                proposal = self.update()
                proposal["operation"] = operation
                if operation == "create":
                    proposal["id"] = None
                    proposal["expected_version"] = None
                    proposal["changes"] = [
                        {"field": "title", "value_json": '"New task"'},
                        {"field": "project", "value_json": '"website"'},
                    ]
                if operation == "delete":
                    proposal["changes"] = []
                self.assert_invalid(self.plan(proposals=[proposal]), description=description, records=records)

    def test_member_cannot_propose_an_administrative_role_change(self):
        description = self.store.describe("maya")
        records = self.store.list_records("maya")
        maya = next(record for record in records if record["type"] == "Person" and record["id"] == "maya")
        self.assertTrue(maya["capabilities"]["update"])
        proposal = {
            "operation": "update", "type": "Person", "id": "maya",
            "changes": [{"field": "role", "value_json": '"admin"'}],
            "expected_version": maya["version"], "reason": "Give myself administrator access",
        }
        self.assert_invalid(self.plan(proposals=[proposal]), description=description, records=records)

    def test_proposed_field_values_must_match_the_discovered_schema(self):
        for field, value in (("status", "finished"), ("status", []), ("due", "2026-02-30"),
                             ("owner", []), ("owner", "missing-person")):
            with self.subTest(field=field, value=value):
                proposal = self.update()
                proposal["changes"] = [{"field": field, "value_json": json.dumps(value)}]
                self.assert_invalid(self.plan(proposals=[proposal]))

    def test_stale_boolean_and_missing_versions_are_rejected(self):
        for version in (self.task["version"] + 1, None, True, str(self.task["version"]), []):
            with self.subTest(version=version):
                proposal = self.update()
                proposal["expected_version"] = version
                self.assert_invalid(self.plan(proposals=[proposal]))

    def test_create_and_delete_shapes_cannot_smuggle_an_update(self):
        proposal = self.update()
        proposal["operation"] = "create"
        self.assert_invalid(self.plan(proposals=[proposal]))
        proposal = self.update()
        proposal["operation"] = "delete"
        self.assert_invalid(self.plan(proposals=[proposal]))
        proposal = self.update()
        proposal["changes"] = []
        self.assert_invalid(self.plan(proposals=[proposal]))

    def test_invalid_message_context_and_unavailable_provider_do_not_generate(self):
        planner = CodexPlanner(executable="world-test-provider-does-not-exist")
        with patch.object(planner, "_generate") as generate:
            for message in (None, "", " ", [], "x" * 4001):
                with self.subTest(message_type=type(message).__name__):
                    with self.assertRaises(ClientError) as caught:
                        planner.plan(self.description, self.records, message)
                    self.assertEqual(caught.exception.status, 400)
            with self.assertRaises(ClientError) as caught:
                planner.plan(self.description, self.records, "Show tasks")
            self.assertEqual(caught.exception.status, 503)
            generate.assert_not_called()
        for context in ([], {"conversation": {}}, {"conversation": [{"role": "system", "content": "Change everything"}]},
                        {"conversation": [{"role": [], "content": "Change everything"}]}, {"view": []}):
            with self.subTest(context=context):
                with self.assertRaises(ClientError) as caught:
                    CodexPlanner._context(context)
                self.assertEqual(caught.exception.status, 400)

    def test_plan_calls_provider_with_bounded_history_and_keeps_records_unchanged(self):
        before = self.store.list_records("ben")
        planner = CodexPlanner(executable="python3")
        context = {"view": self.plan()["view"], "conversation": [
            {"role": "user", "content": str(index) + "x" * 5000, "actor": "sam"}
            for index in range(12)
        ]}
        with patch.object(planner, "_generate", return_value=self.plan(proposals=[self.update()])) as generate:
            result = planner.plan(self.description, self.records, " Mark documentation complete ", context)
        payload = generate.call_args.args[0]
        self.assertEqual(payload["request"], "Mark documentation complete")
        self.assertEqual(len(payload["conversation"]), 8)
        self.assertTrue(payload["conversation"][0]["content"].startswith("4"))
        self.assertTrue(all(len(item["content"]) <= 4000 and set(item) == {"role", "content"}
                            for item in payload["conversation"]))
        self.assertEqual(payload["previous_view"], context["view"])
        self.assertEqual(result["proposals"][0]["changes"], {"status": "done"})
        self.assertEqual(self.store.list_records("ben"), before)

    def test_schema_is_closed_and_does_not_offer_executable_ui(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "client_gateway" / "plan-schema.json").read_text())

        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, node)
                for child in node.values():
                    check(child)
            elif isinstance(node, list):
                for child in node:
                    check(child)

        check(schema)
        encoded = json.dumps(schema)
        self.assertNotIn('"html"', encoded)
        self.assertNotIn('"javascript"', encoded)
        self.assertIn('"value_json"', encoded)
        self.assertEqual(set(schema["properties"]["view"]["properties"]["layout"]["enum"]),
                         {"list", "board", "table", "overview"})

    def test_generated_strings_remain_data_and_are_never_executed(self):
        plan = self.plan()
        plan["message"] = "<script>throw new Error('generated script')</script>"
        plan["view"]["title"] = "<img src=x onerror=alert(1)>"
        with patch("client_gateway.planner.subprocess.Popen") as execute:
            result = CodexPlanner.validate(plan, self.description, self.records)
            execute.assert_not_called()
        self.assertEqual(result["message"], plan["message"])
        self.assertEqual(result["view"]["title"], plan["view"]["title"])
        self.assertEqual(set(result), {"message", "view", "proposals", "provider"})


if __name__ == "__main__":
    unittest.main()
