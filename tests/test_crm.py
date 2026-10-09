"""The CRM world runs on the same engine as the team world."""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from world import WorldError, WorldStore
import worlds


class CRMTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "world.sqlite3"
        self.store = WorldStore(self.path, worlds.load("crm"))

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def assert_error(self, status, action, code=None):
        with self.assertRaises(WorldError) as caught:
            action()
        self.assertEqual(caught.exception.status, status)
        if code:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def ids(self, actor, type_name):
        return {record["id"] for record in self.store.list_records(actor, type_name)}

    def test_description_reports_types_and_each_actors_capabilities(self):
        description = self.store.describe("priya")
        self.assertEqual(description["name"], "CRM")
        self.assertEqual(list(description["types"]), ["Person", "Organization", "Contact", "Opportunity", "Interaction"])
        self.assertEqual(description["types"]["Opportunity"]["fields"]["amount"]["type"], "integer")
        self.assertIs(description["types"]["Opportunity"]["fields"]["owner"]["writable"], False)
        self.assertNotIn("writable", self.store.describe("ben")["types"]["Opportunity"]["fields"]["owner"])
        self.assertFalse(description["types"]["Person"]["capabilities"]["create"])
        self.assertTrue(description["types"]["Opportunity"]["capabilities"]["create"])
        viewer = self.store.describe("nina")["types"]
        self.assertFalse(any(viewer[name]["capabilities"][verb] for name in viewer for verb in ("create", "update", "delete")))

    def test_reps_see_their_own_pipeline_and_everyone_sees_organizations(self):
        self.assertEqual(self.ids("priya", "Opportunity"), {"northwind-renewal", "northwind-warehouse", "harbor-pilot"})
        self.assertEqual(self.ids("tom", "Opportunity"), {"alder-rollout", "alder-training", "pine-redesign"})
        self.assertEqual(len(self.ids("nina", "Opportunity")), 6)
        self.assertEqual(len(self.ids("priya", "Organization")), 4)
        # Interactions tied to another rep's opportunity are hidden; untied ones are not.
        self.assertEqual(self.ids("priya", "Interaction"),
                         {"northwind-renewal-call", "northwind-warehouse-email", "harbor-pilot-meeting", "pine-check-in"})
        self.assert_error(403, lambda: self.store.get_record("priya", "Opportunity", "alder-rollout"))
        self.assert_error(403, lambda: self.store.history("priya", "Opportunity", "alder-rollout"))

    def test_an_assigned_opportunity_is_visible_to_its_owner_and_the_account_owner(self):
        self.store.update("ben", "Opportunity", "alder-rollout", {"owner": "priya"}, 1)
        self.assertIn("alder-rollout", self.ids("priya", "Opportunity"))
        self.assertIn("alder-rollout", self.ids("tom", "Opportunity"))
        self.assertIn("alder-intro", self.ids("priya", "Interaction"))

    def test_winning_requires_an_amount_and_close_date(self):
        error = self.assert_error(422, lambda: self.store.update("tom", "Opportunity", "alder-rollout", {"stage": "won"}, 1), "constraint_failed")
        self.assertEqual(error.details["fields"], ["amount", "close_date"])
        self.assert_error(422, lambda: self.store.update("tom", "Opportunity", "alder-rollout", {"stage": "won", "amount": 0, "close_date": "2026-10-01"}, 1), "constraint_failed")
        self.assert_error(422, lambda: self.store.update("tom", "Opportunity", "alder-rollout", {"amount": -1}, 1), "invalid_field")
        self.assert_error(422, lambda: self.store.update("tom", "Opportunity", "alder-rollout", {"amount": 1.5}, 1), "invalid_field")
        won = self.store.update("tom", "Opportunity", "alder-rollout", {"stage": "won", "amount": 30000, "close_date": "2026-10-01"}, 1,
                                {"reason": "Order form signed."})
        self.assertEqual(won["data"]["stage"], "won")
        self.assertEqual(self.store.history("tom", "Opportunity", "alder-rollout")[-1]["reason"], "Order form signed.")

    def test_losing_requires_a_reason(self):
        error = self.assert_error(422, lambda: self.store.update("priya", "Opportunity", "harbor-pilot", {"stage": "lost"}, 1), "constraint_failed")
        self.assertEqual(error.details["fields"], ["lost_reason"])
        self.store.update("priya", "Opportunity", "harbor-pilot", {"stage": "lost", "lost_reason": "Budget frozen."}, 1)

    def test_a_closed_opportunity_can_only_be_changed_by_an_admin(self):
        closed = self.store.get_record("tom", "Opportunity", "alder-training")
        self.assertFalse(closed["capabilities"]["update"])
        self.assert_error(403, lambda: self.store.update("tom", "Opportunity", "alder-training", {"amount": 9500}, 1))
        self.assertTrue(self.store.get_record("tom", "Opportunity", "alder-rollout")["capabilities"]["update"])
        reopened = self.store.update("ben", "Opportunity", "alder-training", {"stage": "negotiation"}, 1)
        self.assertEqual(reopened["version"], 2)
        self.store.update("tom", "Opportunity", "alder-training", {"amount": 9500}, 2)

    def test_related_records_must_share_an_organization(self):
        self.assert_error(422, lambda: self.store.update("priya", "Opportunity", "harbor-pilot", {"primary_contact": "dana-northwind"}, 1), "invalid_relationship")
        self.assert_error(422, lambda: self.store.create("priya", "Interaction", {
            "kind": "call", "summary": "Wrong contact.", "occurred_on": "2026-10-01", "organization": "harbor", "contact": "dana-northwind",
        }), "invalid_relationship")
        self.assert_error(422, lambda: self.store.update("ben", "Contact", "mei-harbor", {"organization": "northwind"}, 1), "invalid_relationship")
        self.assert_error(422, lambda: self.store.update("ben", "Opportunity", "harbor-pilot", {"organization": "northwind", "primary_contact": None}, 1), "invalid_relationship")
        self.assert_error(422, lambda: self.store.update("ben", "Organization", "harbor", {"owner": "nina"}, 1), "invalid_relationship")

    def test_reps_write_only_within_their_own_accounts(self):
        created = self.store.create("priya", "Interaction", {
            "kind": "email", "summary": "Sent references to Mei.", "occurred_on": "2026-10-02",
            "organization": "harbor", "contact": "mei-harbor", "opportunity": "harbor-pilot",
        })
        self.assertEqual(created["data"]["author"], "priya")
        self.assert_error(403, lambda: self.store.create("priya", "Interaction", {
            "kind": "note", "summary": "Not my account.", "occurred_on": "2026-10-02", "organization": "alder",
        }))
        self.assert_error(403, lambda: self.store.create("priya", "Interaction", {
            "kind": "note", "summary": "Not me.", "occurred_on": "2026-10-02", "organization": "harbor", "author": "tom",
        }))
        self.assert_error(403, lambda: self.store.create("priya", "Contact", {"name": "Someone", "organization": "alder"}))
        self.assert_error(403, lambda: self.store.create("priya", "Opportunity", {"name": "Poach", "organization": "alder"}))
        self.assert_error(403, lambda: self.store.update("priya", "Organization", "alder", {"status": "customer"}, 1))
        self.assert_error(403, lambda: self.store.update("priya", "Organization", "harbor", {"owner": "tom"}, 1))
        self.assert_error(403, lambda: self.store.update("tom", "Interaction", created["id"], {"summary": "Edited."}, 1))
        organization = self.store.create("priya", "Organization", {"name": "Quay Foods"})
        self.assertEqual(organization["data"]["owner"], "priya")
        self.store.create("priya", "Opportunity", {"name": "Quay starter", "organization": organization["id"]})

    def test_viewers_and_deletion(self):
        self.assert_error(403, lambda: self.store.update("nina", "Organization", "harbor", {"status": "customer"}, 1))
        self.assert_error(403, lambda: self.store.create("nina", "Organization", {"name": "Nope"}))
        self.assert_error(403, lambda: self.store.delete("priya", "Interaction", "pine-check-in", 1))
        self.assert_error(409, lambda: self.store.delete("ben", "Organization", "pine", 1), "record_referenced")
        self.assert_error(409, lambda: self.store.update("ben", "Person", "ben", {"role": "rep"}, 1), "last_admin")
        self.store.delete("ben", "Interaction", "pine-check-in", 1)
        self.assertEqual(self.store.history("ben", "Interaction", "pine-check-in")[-1]["action"], "delete")

    def test_query_supports_integer_fields(self):
        large = self.store.query("ben", "Opportunity", [{"field": "amount", "op": "gte", "value": 20000}])
        self.assertEqual({record["id"] for record in large}, {"northwind-renewal", "harbor-pilot"})
        self.assert_error(400, lambda: self.store.query("ben", "Opportunity", [{"field": "amount", "op": "gte", "value": "20000"}]))

    def test_a_database_belongs_to_one_world(self):
        self.store.close()
        with self.assertRaisesRegex(ValueError, "holds the crm world"):
            WorldStore(self.path)
        self.assertEqual(WorldStore(self.path, worlds.load("crm")).describe("ben")["name"], "CRM")

    def test_a_database_from_before_worlds_were_named_holds_the_team_world(self):
        legacy = Path(self.temporary.name) / "legacy.sqlite3"
        WorldStore(legacy).close()
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute("DELETE FROM world_meta WHERE key = 'world'")
            connection.commit()
        with self.assertRaisesRegex(ValueError, "holds the team world"):
            WorldStore(legacy, worlds.load("crm"))
        self.assertEqual(len(WorldStore(legacy).list_records("ben", "Task")), 6)

    def test_rejections_carry_what_a_client_needs_to_correct_them(self):
        update = lambda changes, version=1: self.store.update("priya", "Opportunity", "harbor-pilot", changes, version)
        self.assertEqual(self.assert_error(428, lambda: update({"amount": 1}, None)).details, {"current_version": 1})
        self.assertIn("amount", self.assert_error(422, lambda: update({"value": 1}), "unknown_field").details["allowed"])
        self.assertIn("Opportunity", self.assert_error(400, lambda: self.store.create("priya", "Deal", {})).details["types"])
        missing = self.assert_error(422, lambda: self.store.create("priya", "Interaction", {"kind": "call", "summary": "x", "organization": "harbor"}))
        self.assertEqual(missing.details["spec"]["type"], "date")
        denied = self.assert_error(403, lambda: self.store.update("tom", "Opportunity", "alder-training", {"amount": 1}, 1))
        self.assertIn("only admin", " ".join(denied.details["constraints"]))

    def test_audit_lines_say_when_who_and_what_for_every_create_view_change_and_deletion(self):
        self.store.get_record("priya", "Opportunity", "harbor-pilot")
        self.store.query("priya", "Opportunity", [{"field": "stage", "op": "eq", "value": "proposal"}])
        self.store.update("priya", "Opportunity", "harbor-pilot", {"stage": "lost", "lost_reason": "Budget frozen."}, 1,
                          {"reason": "Mei called.\n2026-01-01T00:00:00.000Z  ben  deleted  Opportunity harbor-pilot  Forged."})
        self.store.history("priya", "Opportunity", "harbor-pilot")
        lines = self.store.audit("priya", "Opportunity", "harbor-pilot")
        self.assertEqual([line.split("  ")[1:3] for line in lines],
                         [["system", "created"], ["priya", "viewed"], ["priya", "viewed"], ["priya", "changed"], ["priya", "viewed"]])
        self.assertTrue(all(line.split("  ")[3] == "Opportunity harbor-pilot" for line in lines))
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z  ")
        self.assertIn('name "Harbor Logistics pilot"', lines[0])
        self.assertTrue(lines[1].endswith("Read."))
        self.assertTrue(lines[2].endswith("Found by a query."))
        self.assertIn('stage from "proposal" to "lost"; lost_reason from null to "Budget frozen."', lines[3])
        self.assertIn("Reason: Mei called. 2026-01-01", lines[3])
        self.assertTrue(lines[4].endswith("History read."))
        times = [line.split("  ")[0] for line in lines]
        self.assertEqual(times, sorted(times))

        self.store.delete("ben", "Interaction", "pine-check-in", 1, {"reason": "Duplicate."})
        deleted = self.store.audit("ben", "Interaction", "pine-check-in")[-1]
        self.assertIn("  ben  deleted  Interaction pine-check-in  Reason: Duplicate.", deleted)

    def test_the_audit_shows_only_records_the_actor_may_read(self):
        self.store.get_record("tom", "Opportunity", "alder-rollout")
        self.store.list_records("priya")
        everything = self.store.audit("ben", limit=10000)
        self.assertTrue(any("  tom  viewed  Opportunity alder-rollout  Read." in line for line in everything))
        self.assertTrue(any("  priya  viewed  Organization alder  Listed." in line for line in everything))
        mine = self.store.audit("priya", limit=10000)
        self.assertFalse(any("alder-rollout" in line for line in mine))
        self.assertTrue(any("Opportunity harbor-pilot" in line for line in mine))
        self.assert_error(403, lambda: self.store.audit("priya", "Opportunity", "alder-rollout"))
        self.assertEqual(len(self.store.audit("ben", limit=3)), 3)
        self.assert_error(400, lambda: self.store.audit("ben", limit=0))

    def test_views_cannot_be_rewritten(self):
        self.store.get_record("ben", "Person", "ben")
        with closing(sqlite3.connect(self.path)) as connection:
            for statement in ("DELETE FROM views", "UPDATE views SET actor = 'nina'"):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(statement)


if __name__ == "__main__":
    unittest.main()
