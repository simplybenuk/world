"""Focused evidence scenarios for the disposable fact-ledger experiment.

Run with ``python3 -m unittest experiments.agent_world.test_store -v``.
All databases live in temporary directories. No scenario contacts the live CRM.
"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from world import WorldDefinition, WorldError, WorldStore
from .store import FactWorldStore


CRM_PATH = Path(__file__).resolve().parents[2] / "definitions" / "crm.world.json"


class FactWorldScenarios(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "facts.sqlite3"
        self.definition = WorldDefinition.load(CRM_PATH)
        self.store = FactWorldStore(self.path, self.definition)
        self.addCleanup(self.store.close)

    def operation(self, type_name, record_id, changes, version=1):
        return {"type": type_name, "id": record_id, "changes": changes, "expected_version": version}

    def rejected(self, code, operation):
        head = self.store.head()
        records = self.store.list_records("ben")
        facts = self.store.facts("ben", "Opportunity", "northstar-renewal")
        with self.assertRaises(WorldError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.store.head(), head)
        self.assertEqual(self.store.list_records("ben"), records)
        self.assertEqual(self.store.facts("ben", "Opportunity", "northstar-renewal"), facts)

    def test_one_seed_snapshot_and_bounded_joined_read(self):
        self.assertEqual(self.store.head(), 1)
        self.assertEqual(len(self.store.list_records("ben")), 14)
        result = self.store.select("maya", {
            "type": "Opportunity", "filters": [{"field": "id", "op": "eq", "value": "northstar-renewal"}],
            "select": ["name", "organization.name", "contact.email", "owner.name", "amount_minor", "currency"],
            "include": [{"name": "latest_interaction", "type": "Interaction", "via": "opportunity",
                         "select": ["body", "author.name", "occurred"], "order_by": "occurred", "descending": True, "limit": 1}],
        })
        self.assertEqual(result["snapshot"], 1)
        record = result["records"][0]
        self.assertEqual(record["values"]["organization.name"], "Northstar Labs")
        self.assertEqual(record["values"]["owner.name"], "Ben")
        self.assertEqual(record["next"]["stage"], ["proposal", "lost"])
        self.assertEqual(record["related"]["latest_interaction"][0]["id"], "renewal-discovery")
        self.assertEqual(self.store.head(), 1)

    def test_accepted_change_has_provenance_and_time_travel(self):
        accepted = self.store.transact("maya", [self.operation("Opportunity", "northstar-renewal", {"stage": "proposal"})], {
            "requested_by": "ben", "reason": "Northstar requested a proposal.", "evidence": [{"source": "fictional-call", "id": "call-1"}],
        })
        self.assertEqual(accepted["snapshot"], 2)
        self.assertEqual(accepted["records"][0]["version"], 2)
        current = self.store.select("ben", {"type": "Opportunity", "select": ["stage"],
                                           "filters": [{"field": "id", "op": "eq", "value": "northstar-renewal"}]})
        earlier = self.store.select("ben", {"type": "Opportunity", "select": ["stage"], "as_of": 1,
                                           "filters": [{"field": "id", "op": "eq", "value": "northstar-renewal"}]})
        self.assertEqual(current["records"][0]["values"]["stage"], "proposal")
        self.assertEqual(earlier["records"][0]["values"]["stage"], "qualified")
        self.assertEqual(earlier["records"][0]["version"], 1)
        stages = [fact for fact in self.store.facts("ben", "Opportunity", "northstar-renewal")["facts"] if fact["attribute"] == "stage"]
        self.assertEqual([(fact["value"], fact["asserted"], fact["transaction"]) for fact in stages],
                         [("qualified", True, 1), ("qualified", False, 2), ("proposal", True, 2)])
        self.assertEqual(stages[-1]["provenance"]["actor"], "maya")
        self.assertEqual(stages[-1]["provenance"]["requested_by"], "ben")
        self.assertEqual([event["transaction"] for event in self.store.history("ben", "Opportunity", "northstar-renewal")], [1, 2])

    def test_stale_skipped_transition_viewer_and_duplicate_do_not_commit(self):
        self.store.transact("maya", [self.operation("Opportunity", "northstar-renewal", {"stage": "proposal"})])
        self.rejected("version_conflict", lambda: self.store.transact("maya", [self.operation("Opportunity", "northstar-renewal", {"stage": "lost"})]))
        self.rejected("invalid_transition", lambda: self.store.transact("ben", [self.operation("Opportunity", "northstar-pilot", {"stage": "won"})]))
        self.rejected("permission_denied", lambda: self.store.transact("sam", [self.operation("Opportunity", "northstar-pilot", {"stage": "qualified"})]))
        self.rejected("duplicate_operation", lambda: self.store.transact("ben", [
            self.operation("Opportunity", "northstar-pilot", {"stage": "qualified"}),
            self.operation("Opportunity", "northstar-pilot", {"name": "Changed twice"}),
        ]))
        self.rejected("invalid_transaction", lambda: self.store.transact("ben", [self.operation("Opportunity", "northstar-pilot", {"name": "Overflow"})] * 11))
        self.assertEqual(self.store.select("sam", {"type": "Opportunity", "select": ["name"]})["records"][0]["next"]["stage"], [])

    def test_invalid_final_relationship_rolls_back_entire_transaction(self):
        self.rejected("invalid_relationship", lambda: self.store.transact("ben", [
            self.operation("Opportunity", "northstar-renewal", {"stage": "proposal"}),
            self.operation("Contact", "northstar-jordan", {"organization": "atlas"}),
        ]))

    def test_related_records_can_move_together_in_one_accepted_commit(self):
        operations = [
            self.operation("Contact", "northstar-jordan", {"organization": "atlas"}),
            self.operation("Opportunity", "northstar-renewal", {"organization": "atlas", "owner": "alex"}),
            self.operation("Opportunity", "northstar-pilot", {"organization": "atlas", "owner": "alex"}),
            self.operation("Interaction", "renewal-discovery", {"author": "alex"}),
        ]
        result = self.store.transact("ben", operations)
        self.assertEqual(result["snapshot"], 2)
        self.assertEqual(len(result["records"]), 4)
        self.assertTrue(all(record["version"] == 2 for record in result["records"]))
        for operation in operations:
            facts = self.store.facts("ben", operation["type"], operation["id"])["facts"]
            self.assertEqual(max(fact["transaction"] for fact in facts), 2)
        self.assertEqual(self.store.select("sam", {"type": "Opportunity", "select": ["name"]})["records"], [])

    def test_revoked_access_blocks_old_snapshots_facts_and_history(self):
        self.store.transact("ben", [self.operation("Organization", "northstar", {"visibility": "private"})])
        self.assertEqual(self.store.select("sam", {"type": "Opportunity", "select": ["name"], "as_of": 1})["records"], [])
        with self.assertRaises(WorldError) as caught:
            self.store.facts("sam", "Opportunity", "northstar-renewal", as_of=1)
        self.assertEqual(caught.exception.code, "permission_denied")
        with self.assertRaises(WorldError):
            self.store.history("sam", "Opportunity", "northstar-renewal")

    def test_new_access_does_not_reveal_private_era_values(self):
        self.store.transact("ben", [self.operation("Organization", "atlas", {"visibility": "team"}),
                                    self.operation("Interaction", "atlas-proposal-review", {"body": "A public update."})])
        self.assertEqual(self.store.select("sam", {"type": "Opportunity", "select": ["name"], "as_of": 1})["records"][0]["id"], "northstar-pilot")
        private_body = "Lee is reviewing the expansion proposal with the studio's finance lead."
        facts = self.store.facts("sam", "Interaction", "atlas-proposal-review")["facts"]
        self.assertEqual([fact["value"] for fact in facts if fact["attribute"] == "body"], ["A public update."])
        self.assertNotIn(private_body, json.dumps(facts))
        self.assertEqual(self.store.history("sam", "Interaction", "atlas-proposal-review"), [])

    def test_private_interval_is_hidden_after_access_returns(self):
        self.store.transact("ben", [self.operation("Organization", "northstar", {"visibility": "private"})])
        self.store.transact("ben", [self.operation("Interaction", "renewal-discovery", {"body": "Private pricing concession."})])
        self.store.transact("ben", [self.operation("Organization", "northstar", {"visibility": "team"}, 2),
                                    self.operation("Interaction", "renewal-discovery", {"body": "Public renewal update."}, 2)])
        facts = self.store.facts("sam", "Interaction", "renewal-discovery")["facts"]
        history = self.store.history("sam", "Interaction", "renewal-discovery")
        self.assertNotIn("Private pricing concession.", json.dumps(facts))
        self.assertNotIn("Private pricing concession.", json.dumps(history))
        self.assertEqual([event["transaction"] for event in history], [1])

    def test_shared_commit_provenance_does_not_leak_a_private_record(self):
        self.store.transact("ben", [self.operation("Opportunity", "northstar-renewal", {"stage": "proposal"}),
                                   self.operation("Opportunity", "atlas-expansion", {"amount_minor": 5100000})], {
            "reason": "atlas-expansion includes a private concession of 5100000 USD minor units.",
            "evidence": [{"record": "atlas-expansion", "private_amount_minor": 5100000}],
        })
        facts = self.store.facts("sam", "Opportunity", "northstar-renewal")["facts"]
        history = self.store.history("sam", "Opportunity", "northstar-renewal")
        self.assertNotIn("atlas-expansion", json.dumps(facts))
        self.assertNotIn("5100000", json.dumps(history))
        self.assertTrue(facts[-1]["provenance"]["redacted"])
        self.assertEqual(facts[-1]["provenance"]["evidence"], [])
        self.assertIn("atlas-expansion", json.dumps(self.store.facts("ben", "Opportunity", "northstar-renewal")))

    def test_unreadable_reference_names_ids_and_filter_predicates_are_redacted(self):
        document = json.loads(CRM_PATH.read_text())
        document["id"] = "crm-reference-probe"
        document["types"]["Portfolio"] = {
            "fields": {"name": {"type": "text", "required": True},
                       "organization": {"type": "reference", "target": "Organization", "required": True},
                       "organizations": {"type": "list", "items": {"type": "reference", "target": "Organization"}, "required": True}},
            "policy": {"scope": None, "read": "authenticated", "create": "admin", "update": "admin",
                       "delete": "admin", "admin_fields": []},
        }
        document["seed"].append({"type": "Portfolio", "id": "public-pointer", "data": {
            "name": "Visible portfolio", "organization": "atlas", "organizations": ["northstar", "atlas"],
        }})
        probe = FactWorldStore(Path(self.temporary.name) / "probe.sqlite3", WorldDefinition(document))
        self.addCleanup(probe.close)
        selected = probe.select("sam", {"type": "Portfolio", "select": ["name", "organization", "organization.name"]})
        self.assertEqual(selected["records"][0]["values"], {"name": "Visible portfolio", "organization": None, "organization.name": None})
        self.assertEqual(probe.select("sam", {"type": "Portfolio", "select": ["name"],
                                               "filters": [{"field": "organization", "op": "eq", "value": "atlas"}]})["records"], [])
        self.assertNotIn("atlas", json.dumps(probe.facts("sam", "Portfolio", "public-pointer")))
        self.assertIsNone(probe.history("sam", "Portfolio", "public-pointer")[0]["after"]["data"]["organization"])
        expected = {"name": "Visible portfolio", "organization": None, "organizations": ["northstar"]}
        self.assertEqual(probe.get_record("sam", "Portfolio", "public-pointer")["data"], expected)
        self.assertEqual(probe.list_records("sam", "Portfolio")[0]["data"], expected)
        self.assertEqual(probe.query("sam", "Portfolio")[0]["data"], expected)
        self.assertEqual(probe.query("sam", "Portfolio", [{"field": "organization", "op": "eq", "value": "atlas"}]), [])
        self.assertEqual(probe.query("sam", "Portfolio", [{"field": "organizations", "op": "contains", "value": "atlas"}]), [])
        self.assertEqual(probe.query("sam", "Portfolio", [{"field": "organizations", "op": "contains", "value": "northstar"}])[0]["data"], expected)

    def test_normal_current_record_shapes_match_the_existing_crm_store(self):
        conventional = WorldStore(Path(self.temporary.name) / "normal-crm.sqlite3", self.definition)
        self.addCleanup(conventional.close)
        def without_clock(records):
            return [{key: value for key, value in record.items() if key not in {"created_at", "updated_at"}} for record in records]
        for actor in ("ben", "maya", "sam"):
            self.assertEqual(without_clock(self.store.list_records(actor)), without_clock(conventional.list_records(actor)))
            self.assertEqual(without_clock(self.store.query(actor, "Opportunity", [{"field": "stage", "op": "eq", "value": "qualified"}])),
                             without_clock(conventional.query(actor, "Opportunity", [{"field": "stage", "op": "eq", "value": "qualified"}])))
            candidate = self.store.get_record(actor, "Opportunity", "northstar-renewal")
            baseline = conventional.get_record(actor, "Opportunity", "northstar-renewal")
            self.assertEqual(set(candidate), set(baseline))
            self.assertEqual(without_clock([candidate]), without_clock([baseline]))

    def test_restart_and_explicit_rebuild_restore_corrupted_projection_without_reseed(self):
        self.store.transact("maya", [self.operation("Opportunity", "northstar-renewal", {"stage": "proposal"})])
        before = self.store.list_records("ben")
        facts = self.store.facts("ben", "Opportunity", "northstar-renewal")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE records SET data = '{}' WHERE type = 'Opportunity' AND id = 'northstar-renewal'")
        reopened = FactWorldStore(self.path, self.definition)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.list_records("ben"), before)
        self.assertEqual(reopened.head(), 2)
        self.assertEqual(reopened.facts("ben", "Opportunity", "northstar-renewal"), facts)
        self.assertEqual([event["transaction"] for event in reopened.history("ben", "Opportunity", "northstar-renewal")], [1, 2])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'events'").fetchone())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM records")
        self.assertEqual(reopened.rebuild_projection(), {"snapshot": 2, "records": 14})
        self.assertEqual(reopened.list_records("ben"), before)

    def test_append_only_ledger_and_definition_binding(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            for sql in ("UPDATE accepted_facts SET value = 'null'", "DELETE FROM accepted_facts",
                        "UPDATE fact_commits SET reason = 'rewritten'", "DELETE FROM fact_commits"):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)
        with self.assertRaises(WorldError) as caught:
            FactWorldStore(self.path, WorldDefinition.default())
        self.assertEqual(caught.exception.code, "definition_mismatch")
        self.assertEqual(self.store.head(), 1)
        conventional = WorldStore(Path(self.temporary.name) / "conventional.sqlite3", self.definition)
        self.addCleanup(conventional.close)
        with self.assertRaises(WorldError) as caught:
            FactWorldStore(conventional.path, self.definition)
        self.assertEqual(caught.exception.code, "not_fact_world")
        self.assertEqual(len(conventional.list_records("ben")), 14)

    def test_rejected_startup_leaves_existing_databases_unchanged(self):
        def database_state(path):
            with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as connection:
                schema = list(connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"))
                journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
                content = {}
                for name, in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
                    content[name] = list(connection.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'))
            return schema, journal, content, Path(path).read_bytes()

        def rejected_unchanged(path, definition, code):
            before = database_state(path)
            with self.assertRaises(WorldError) as caught:
                FactWorldStore(path, definition)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(database_state(path), before)

        conventional = WorldStore(Path(self.temporary.name) / "owned.sqlite3", self.definition)
        self.addCleanup(conventional.close)
        rejected_unchanged(conventional.path, self.definition, "not_fact_world")
        rejected_unchanged(conventional.path, WorldDefinition.default(), "definition_mismatch")
        rejected_unchanged(self.path, WorldDefinition.default(), "definition_mismatch")

        empty_owned = Path(self.temporary.name) / "empty-owned.sqlite3"
        with closing(sqlite3.connect(empty_owned)) as connection, connection:
            connection.executescript("CREATE TABLE world_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL); CREATE TABLE records (id TEXT);")
        rejected_unchanged(empty_owned, self.definition, "not_fact_world")

        unrelated = Path(self.temporary.name) / "unrelated.sqlite3"
        with closing(sqlite3.connect(unrelated)) as connection, connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('keep this record')")
        rejected_unchanged(unrelated, self.definition, "not_fact_world")

    def test_a_fresh_empty_file_can_initialize(self):
        empty = Path(self.temporary.name) / "empty-file.sqlite3"
        empty.touch()
        store = FactWorldStore(empty, self.definition)
        self.addCleanup(store.close)
        self.assertEqual(store.head(), 1)
        self.assertEqual(len(store.list_records("ben")), 14)

    def test_inherited_create_delete_are_replayable(self):
        record = self.store.create("maya", "Contact", {"name": "A second contact", "email": "second@example.test", "organization": "northstar"})
        self.assertEqual(self.store.head(), 2)
        deleted = self.store.delete("ben", "Contact", record["id"], record["version"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.store.head(), 3)
        self.store.rebuild_projection()
        self.assertEqual([event["action"] for event in self.store.history("ben", "Contact", record["id"])], ["create", "delete"])
        old = self.store.select("ben", {"type": "Contact", "select": ["name"], "as_of": 2,
                                        "filters": [{"field": "id", "op": "eq", "value": record["id"]}]})
        self.assertEqual(old["records"][0]["values"]["name"], "A second contact")
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'events'").fetchone())

    def test_invalid_query_shapes_are_world_errors(self):
        valid = {"type": "Opportunity", "select": ["name"]}
        invalid = [
            {**valid, "limit": 101}, {**valid, "limit": True}, {**valid, "as_of": 0},
            {**valid, "select": ["organization.unknown"]}, {**valid, "select": ["stage", "stage"]},
            {**valid, "filters": [{"field": "organization.name", "op": "eq", "value": "Atlas Studio"}]},
            {**valid, "include": [{"name": "wrong", "type": "Interaction", "via": "author", "select": ["body"]}]},
        ]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(WorldError):
                self.store.select("ben", request)


if __name__ == "__main__":
    unittest.main()
