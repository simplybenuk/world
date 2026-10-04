"""The CRM definition exercises the same governed record engine as the team world."""

from contextlib import closing
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from client_gateway.planner import CodexPlanner
from world import WorldDefinition, WorldError, WorldStore


CRM_PATH = Path(__file__).resolve().parents[1] / "definitions" / "crm.world.json"


class CRMTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "crm.sqlite3"
        self.definition = WorldDefinition.load(CRM_PATH)
        self.store = WorldStore(self.path, definition=self.definition)
        self.addCleanup(self.store.close)

    def record(self, type_name, record_id, actor="ben"):
        return self.store.get_record(actor, type_name, record_id)

    def assert_error(self, status, operation, code=None):
        with self.assertRaises(WorldError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status, caught.exception.message)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def audit_events(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT * FROM events ORDER BY id").fetchall()

    def assert_rejected_mutation(self, status, operation, code=None):
        before = self.store.list_records("ben")
        events = self.audit_events()
        error = self.assert_error(status, operation, code)
        self.assertEqual(self.store.list_records("ben"), before)
        self.assertEqual(self.audit_events(), events)
        return error

    def assert_rejected_edit(self, status, type_name, record_id, changes, *, actor="ben", code=None):
        version = self.record(type_name, record_id)["version"]
        return self.assert_rejected_mutation(
            status, lambda: self.store.update(actor, type_name, record_id, changes, version), code,
        )

    def test_definition_drives_discovery_without_team_types(self):
        description = self.store.describe("maya")
        self.assertEqual(self.definition.id, "crm")
        self.assertEqual(self.definition.actor_type, "Person")
        self.assertEqual(set(self.definition.actor_ids), {"ben", "maya", "alex", "sam"})
        self.assertEqual(description["name"], "CRM World")
        self.assertEqual(description["version"], "0.2-prototype")
        self.assertEqual(set(description["types"]), {"Person", "Organization", "Contact", "Opportunity", "Interaction"})
        self.assertEqual(description["actor"]["id"], "maya")
        opportunity = description["types"]["Opportunity"]
        self.assertEqual(opportunity["fields"]["amount_minor"]["type"], "integer")
        self.assertEqual(opportunity["fields"]["amount_minor"]["minimum"], 0)
        self.assertEqual(opportunity["fields"]["amount_minor"]["maximum"], 1000000000000)
        self.assertEqual(opportunity["fields"]["stage"]["values"], ["lead", "qualified", "proposal", "won", "lost"])
        self.assertTrue(opportunity["capabilities"]["create"])
        self.assertFalse(description["types"]["Organization"]["capabilities"]["create"])
        self.assert_error(400, lambda: self.store.list_records("ben", "Task"), "unknown_type")
        self.assert_error(400, lambda: self.store.list_records("ben", "Project"), "unknown_type")
        json.dumps(description)

    def test_relative_seed_dates_are_resolved_without_changing_the_definition(self):
        first = {record_id: data for _, record_id, data in self.definition.seed_records(date(2026, 10, 3))}
        later = {record_id: data for _, record_id, data in self.definition.seed_records(date(2026, 10, 10))}
        self.assertEqual(first["northstar-renewal"]["close_date"], "2026-10-01")
        self.assertEqual(later["northstar-renewal"]["close_date"], "2026-10-08")
        self.assertEqual(first["renewal-discovery"]["occurred"], "2026-09-29")
        self.assertEqual(first["northstar-renewal"]["close_date"], "2026-10-01")
        first["northstar-renewal"]["close_date"] = "2000-01-01"
        repeated = {record_id: data for _, record_id, data in self.definition.seed_records(date(2026, 10, 3))}
        self.assertEqual(repeated["northstar-renewal"]["close_date"], "2026-10-01")

    def test_generic_contact_crud_has_durable_provenance_and_history(self):
        contact = self.store.create("maya", "Contact", {
            "name": "Taylor Brooks", "email": "taylor@northstar.example", "organization": "northstar",
        }, {"reason": "Added a fictional renewal stakeholder", "evidence": ["demo-import"]})
        self.assertEqual(contact["version"], 1)
        self.assertTrue(contact["capabilities"]["update"])
        self.assertFalse(contact["capabilities"]["delete"])
        updated = self.store.update("maya", "Contact", contact["id"], {
            "email": "taylor.brooks@northstar.example",
        }, contact["version"], {"reason": "Corrected the fictional email", "evidence": ["demo-review"]})
        self.assertEqual(updated["version"], 2)
        reopened = WorldStore(self.path, definition=WorldDefinition.load(CRM_PATH))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_record("maya", "Contact", contact["id"]), updated)
        history = reopened.history("ben", "Contact", contact["id"])
        self.assertEqual([event["action"] for event in history], ["create", "update"])
        self.assertIsNone(history[0]["before"])
        self.assertEqual(history[0]["reason"], "Added a fictional renewal stakeholder")
        self.assertEqual(history[0]["evidence"], ["demo-import"])
        self.assertEqual(history[-1]["actor"], "maya")
        self.assertEqual(history[-1]["requested_by"], "maya")
        self.assertEqual(history[-1]["reason"], "Corrected the fictional email")
        self.assertEqual(history[-1]["evidence"], ["demo-review"])
        self.assertEqual(history[-1]["before"]["data"]["email"], "taylor@northstar.example")
        self.assertEqual(history[-1]["after"]["data"]["email"], "taylor.brooks@northstar.example")
        deleted = reopened.delete("ben", "Contact", contact["id"], updated["version"], {"reason": "Finished the isolated trial"})
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["version"], 3)
        self.assert_error(404, lambda: self.store.get_record("ben", "Contact", contact["id"]))
        self.assertEqual([event["action"] for event in self.store.history("ben", "Contact", contact["id"])],
                         ["create", "update", "delete"])
        again = WorldStore(self.path, definition=self.definition)
        self.addCleanup(again.close)
        self.assertNotIn(contact["id"], {record["id"] for record in again.list_records("ben", "Contact")})

    def test_sales_stages_advance_through_proposal_to_won(self):
        pilot = self.record("Opportunity", "northstar-pilot")
        qualified = self.store.update("maya", "Opportunity", pilot["id"], {"stage": "qualified"}, pilot["version"])
        self.assertEqual(qualified["data"]["stage"], "qualified")
        self.assertEqual(qualified["version"], pilot["version"] + 1)
        original = self.record("Opportunity", "northstar-renewal")
        self.assertEqual(original["data"]["stage"], "qualified")
        proposal = self.store.update("maya", "Opportunity", original["id"], {"stage": "proposal"}, original["version"],
                                     {"reason": "Prepared the renewal proposal"})
        won = self.store.update("maya", "Opportunity", original["id"], {"stage": "won"}, proposal["version"],
                               {"reason": "Customer accepted the fictional proposal"})
        self.assertEqual(won["data"]["stage"], "won")
        self.assertEqual(won["version"], original["version"] + 2)
        updates = self.store.history("ben", "Opportunity", original["id"])[1:]
        self.assertEqual([event["before"]["data"]["stage"] for event in updates], ["qualified", "proposal"])
        self.assertEqual([event["after"]["data"]["stage"] for event in updates], ["proposal", "won"])
        self.assert_rejected_edit(422, "Opportunity", original["id"], {"stage": "qualified"}, code="invalid_transition")

    def test_skipping_sales_stages_fails_even_for_an_admin(self):
        for actor in ("ben", "maya"):
            with self.subTest(actor=actor):
                self.assert_rejected_edit(422, "Opportunity", "northstar-pilot", {"stage": "won"},
                                          actor=actor, code="invalid_transition")
        pilot = self.record("Opportunity", "northstar-pilot")
        lost = self.store.update("maya", "Opportunity", pilot["id"], {"stage": "lost"}, pilot["version"])
        self.assertEqual(lost["data"]["stage"], "lost")
        self.assert_rejected_edit(422, "Opportunity", pilot["id"], {"stage": "proposal"}, code="invalid_transition")

    def test_money_requires_bounded_integer_minor_units(self):
        for value in (-1, True, 125.5, "125", 1000000000001):
            with self.subTest(value=value):
                self.assert_rejected_edit(422, "Opportunity", "northstar-renewal", {"amount_minor": value})
        for changes in ({"currency": "EUR"}, {"close_date": "2026-02-30"}):
            with self.subTest(changes=changes):
                self.assert_rejected_edit(422, "Opportunity", "northstar-renewal", changes)
        opportunity = self.record("Opportunity", "northstar-renewal")
        for value in (0, 1000000000000):
            with self.subTest(accepted_boundary=value):
                opportunity = self.store.update("maya", "Opportunity", opportunity["id"],
                                                {"amount_minor": value}, opportunity["version"])
                self.assertEqual(opportunity["data"]["amount_minor"], value)
                self.assertEqual(self.record("Opportunity", opportunity["id"])["data"]["amount_minor"], value)
        updated = self.store.update("maya", "Opportunity", opportunity["id"],
                                    {"amount_minor": 2500000, "close_date": None}, opportunity["version"])
        self.assertEqual(updated["data"]["amount_minor"], 2500000)
        self.assertIsNone(updated["data"]["close_date"])
        self.assertEqual(updated["data"]["stage"], "qualified")

    def test_opportunity_defaults_allow_an_unassigned_early_lead(self):
        lead = self.store.create("maya", "Opportunity", {"name": "Northstar future work", "organization": "northstar"})
        self.assertEqual(lead["data"]["stage"], "lead")
        self.assertEqual(lead["data"]["amount_minor"], 0)
        self.assertEqual(lead["data"]["currency"], "GBP")
        self.assertIsNone(lead["data"]["contact"])
        self.assertIsNone(lead["data"]["owner"])
        self.assertIsNone(lead["data"]["close_date"])

    def test_update_transitions_do_not_restrict_the_starting_stage_of_new_opportunities(self):
        opportunity = self.store.create("maya", "Opportunity", {
            "name": "Fictional previously completed sale", "organization": "northstar", "stage": "won",
        })
        self.assertEqual(opportunity["data"]["stage"], "won")
        self.assertEqual(opportunity["version"], 1)
        self.assert_rejected_edit(422, "Opportunity", opportunity["id"], {"stage": "lead"}, code="invalid_transition")

    def test_contact_and_owner_must_belong_to_the_opportunity_organization(self):
        self.assert_rejected_edit(422, "Opportunity", "northstar-renewal", {"contact": "atlas-lee"}, code="invalid_relationship")
        self.assert_rejected_edit(422, "Opportunity", "northstar-renewal", {"owner": "sam"}, code="invalid_relationship")
        self.assert_rejected_edit(422, "Opportunity", "northstar-renewal", {"contact": "missing-contact"}, code="invalid_reference")
        self.assert_rejected_mutation(422, lambda: self.store.create("ben", "Opportunity", {
            "name": "Incorrect customer link", "organization": "northstar", "contact": "atlas-lee",
        }), "invalid_relationship")

    def test_contact_moves_and_member_removals_cannot_invalidate_existing_relationships(self):
        self.assert_rejected_edit(422, "Contact", "northstar-jordan", {"organization": "atlas"}, code="invalid_relationship")
        self.assert_rejected_edit(422, "Organization", "northstar", {"members": ["ben", "alex"]}, code="invalid_relationship")
        self.assertEqual(self.record("Opportunity", "northstar-renewal")["data"]["organization"], "northstar")
        self.assertEqual(self.record("Contact", "northstar-jordan")["data"]["organization"], "northstar")

    def test_contact_moves_require_membership_in_current_and_destination_organizations(self):
        contact = self.store.create("maya", "Contact", {
            "name": "Casey Morgan", "email": "casey@northstar.example", "organization": "northstar",
        })
        self.assert_rejected_edit(403, "Contact", contact["id"], {"organization": "atlas"}, actor="maya")
        moved = self.store.update("alex", "Contact", contact["id"], {"organization": "atlas"}, contact["version"])
        self.assertEqual(moved["data"]["organization"], "atlas")
        self.assertEqual(moved["version"], 2)
        self.assert_error(403, lambda: self.record("Contact", contact["id"], actor="maya"))
        self.assert_rejected_edit(403, "Contact", contact["id"], {"organization": "northstar"}, actor="maya")
        returned = self.store.update("alex", "Contact", contact["id"], {"organization": "northstar"}, moved["version"])
        self.assertEqual(returned["data"]["organization"], "northstar")
        self.assertEqual(returned["version"], 3)
        self.assertEqual(self.record("Contact", contact["id"], actor="maya")["data"], returned["data"])
        self.assertEqual([event["action"] for event in self.store.history("ben", "Contact", contact["id"])],
                         ["create", "update", "update"])

    def test_interactions_alone_preserve_contact_scope_and_author_membership(self):
        organization = self.store.create("ben", "Organization", {
            "name": "Fictional customer follow-up", "members": ["ben", "maya", "alex"],
        })
        contact = self.store.create("maya", "Contact", {
            "name": "Robin Taylor", "email": "robin@follow-up.example", "organization": organization["id"],
        })
        interaction = self.store.create("maya", "Interaction", {
            "body": "A fictional introductory call", "contact": contact["id"], "occurred": "2026-10-03",
        })
        self.assertIsNone(interaction["data"]["opportunity"])
        self.assert_rejected_edit(422, "Contact", contact["id"], {"organization": "atlas"}, code="invalid_relationship")
        self.assert_rejected_edit(422, "Organization", organization["id"], {"members": ["ben", "alex"]},
                                  code="invalid_relationship")
        self.assertEqual(self.record("Interaction", interaction["id"])["data"]["author"], "maya")

    def test_opportunity_moves_preserve_customer_links_of_existing_interactions(self):
        opportunity = self.store.create("alex", "Opportunity", {
            "name": "Fictional future discussion", "organization": "northstar", "owner": "alex",
        })
        interaction = self.store.create("alex", "Interaction", {
            "body": "A fictional discussion of future work", "contact": "northstar-jordan",
            "opportunity": opportunity["id"], "occurred": "2026-10-03",
        })
        self.assertIsNone(opportunity["data"]["contact"])
        self.assert_rejected_edit(422, "Opportunity", opportunity["id"], {"organization": "atlas"},
                                  actor="alex", code="invalid_relationship")
        self.assertEqual(self.record("Interaction", interaction["id"])["data"]["opportunity"], opportunity["id"])

    def test_interaction_authorship_and_customer_links_are_enforced(self):
        occurred = datetime.now(timezone.utc).date().isoformat()
        data = {"body": "A fictional customer follow-up", "contact": "northstar-jordan", "occurred": occurred}
        interaction = self.store.create("maya", "Interaction", data)
        self.assertEqual(interaction["data"]["author"], "maya")
        self.assertIsNone(interaction["data"]["opportunity"])
        self.assert_rejected_mutation(403, lambda: self.store.create("maya", "Interaction", {**data, "author": "ben"}))
        self.assert_rejected_edit(403, "Interaction", interaction["id"], {"author": "ben"}, actor="maya")
        self.assert_rejected_mutation(422, lambda: self.store.create("ben", "Interaction", {
            **data, "opportunity": "atlas-expansion",
        }), "invalid_relationship")
        self.assert_rejected_mutation(422, lambda: self.store.create("ben", "Interaction", {
            **data, "author": "sam",
        }), "invalid_relationship")
        self.assert_rejected_edit(422, "Interaction", "renewal-discovery", {"opportunity": "atlas-expansion"}, code="invalid_relationship")

    def test_private_organization_visibility_applies_through_all_reference_paths(self):
        private = {
            "Organization": "atlas", "Contact": "atlas-lee",
            "Opportunity": "atlas-expansion", "Interaction": "atlas-proposal-review",
        }
        for actor in ("maya", "sam"):
            for type_name, record_id in private.items():
                with self.subTest(actor=actor, type_name=type_name):
                    self.assertNotIn(record_id, {record["id"] for record in self.store.list_records(actor, type_name)})
                    self.assertEqual(self.store.query(actor, type_name, [{"field": "id", "op": "eq", "value": record_id}]), [])
                    self.assert_error(403, lambda: self.store.get_record(actor, type_name, record_id))
                    self.assert_error(403, lambda: self.store.history(actor, type_name, record_id))
        self.assertEqual({record["id"] for record in self.store.list_records("sam", "Opportunity")},
                         {"northstar-renewal", "northstar-pilot"})
        self.assertIn("atlas-expansion", {record["id"] for record in self.store.list_records("alex", "Opportunity")})
        self.assert_rejected_edit(403, "Opportunity", "northstar-renewal", {
            "organization": "atlas", "contact": None, "owner": None,
        }, actor="maya")

    def test_viewer_and_protected_fields_cannot_bypass_declared_policies(self):
        self.assert_rejected_edit(403, "Opportunity", "northstar-renewal", {"stage": "proposal"}, actor="sam")
        self.assert_rejected_mutation(403, lambda: self.store.create("sam", "Contact", {
            "name": "Unauthorized contact", "email": "blocked@northstar.example", "organization": "northstar",
        }))
        self.assert_rejected_edit(403, "Person", "sam", {"name": "Changed"}, actor="sam")
        self.assert_rejected_edit(403, "Person", "maya", {"role": "admin"}, actor="maya")
        self.assert_rejected_edit(403, "Person", "ben", {"name": "Changed"}, actor="maya")
        self.assert_rejected_edit(403, "Organization", "northstar", {"visibility": "private"}, actor="maya")
        self.assert_rejected_edit(403, "Organization", "northstar", {"members": ["ben", "maya", "alex", "sam"]}, actor="maya")
        organization = self.record("Organization", "northstar")
        updated = self.store.update("maya", "Organization", "northstar", {"description": "Updated fictional customer context"}, organization["version"])
        self.assertEqual(updated["data"]["description"], "Updated fictional customer context")

    def test_deletion_requires_admin_and_removal_of_live_customer_links(self):
        pilot = self.record("Opportunity", "northstar-pilot")
        self.assert_rejected_mutation(403, lambda: self.store.delete("maya", "Opportunity", pilot["id"], pilot["version"]))
        for type_name, record_id in (("Contact", "northstar-jordan"), ("Opportunity", "northstar-pilot"),
                                     ("Organization", "northstar")):
            with self.subTest(type_name=type_name):
                record = self.record(type_name, record_id)
                self.assertFalse(record["capabilities"]["delete"])
                self.assert_rejected_mutation(409, lambda: self.store.delete("ben", type_name, record_id, record["version"]),
                                               "record_referenced")
        introduction = self.record("Interaction", "pilot-introduction")
        self.store.delete("ben", "Interaction", introduction["id"], introduction["version"])
        self.assertTrue(self.record("Opportunity", pilot["id"])["capabilities"]["delete"])
        deleted = self.store.delete("ben", "Opportunity", pilot["id"], pilot["version"])
        self.assertTrue(deleted["deleted"])
        reopened = WorldStore(self.path, definition=self.definition)
        self.addCleanup(reopened.close)
        self.assertNotIn(pilot["id"], {record["id"] for record in reopened.list_records("ben", "Opportunity")})
        self.assert_error(404, lambda: reopened.get_record("ben", "Interaction", introduction["id"]))
        for type_name, record_id in (("Opportunity", pilot["id"]), ("Interaction", introduction["id"])):
            self.assertEqual([event["action"] for event in reopened.history("ben", type_name, record_id)], ["create", "delete"])

    def test_queries_use_integer_and_date_fields_and_filter_private_records(self):
        today = datetime.now(timezone.utc).date().isoformat()
        overdue = self.store.query("ben", "Opportunity", [
            {"field": "stage", "op": "in", "value": ["lead", "qualified", "proposal"]},
            {"field": "close_date", "op": "lt", "value": today},
            {"field": "amount_minor", "op": "gte", "value": 1000000},
        ])
        self.assertEqual([record["id"] for record in overdue], ["northstar-renewal"])
        public = self.store.query("sam", "Opportunity", [{"field": "amount_minor", "op": "gte", "value": 1000000}])
        self.assertEqual([record["id"] for record in public], ["northstar-renewal"])
        self.assertIn("atlas-expansion", {record["id"] for record in self.store.query("alex", "Opportunity", [
            {"field": "amount_minor", "op": "gte", "value": 1000000},
        ])})

    def test_stale_versions_reject_a_second_update_without_an_extra_event(self):
        initial = self.record("Opportunity", "northstar-renewal")
        winner = self.store.update("maya", "Opportunity", initial["id"], {"amount_minor": 2600000}, initial["version"],
                                  {"reason": "Revised the fictional offer"})
        history = self.store.history("ben", "Opportunity", initial["id"])
        self.assert_rejected_mutation(409, lambda: self.store.update("ben", "Opportunity", initial["id"],
                                                                   {"stage": "proposal"}, initial["version"]), "version_conflict")
        self.assertEqual(self.record("Opportunity", initial["id"]), {**winner, "capabilities": initial["capabilities"]})
        self.assertEqual(self.store.history("ben", "Opportunity", initial["id"]), history)

    def test_an_existing_crm_database_cannot_be_reopened_as_the_team_world(self):
        before = self.store.list_records("ben")
        self.assert_error(409, lambda: WorldStore(self.path), "definition_mismatch")
        reopened = WorldStore(self.path, definition=self.definition)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.list_records("ben"), before)

    def test_the_generic_planner_uses_crm_fields_without_saving_a_proposal(self):
        before = self.store.list_records("maya")
        opportunity = self.record("Opportunity", "northstar-renewal", actor="maya")
        plan = {
            "message": "The renewal proposal is ready to save.",
            "view": {
                "title": "Sales pipeline", "description": "Visible opportunities by stage",
                "type": "Opportunity", "layout": "board", "record_ids": ["northstar-renewal", "northstar-pilot"],
                "fields": ["name", "owner", "stage", "amount_minor", "currency"], "group_by": "stage",
            },
            "proposals": [{
                "operation": "update", "type": "Opportunity", "id": "northstar-renewal",
                "expected_version": opportunity["version"], "reason": "Move Northstar renewal to proposal",
                "changes": [{"field": "stage", "value_json": '"proposal"'}],
            }],
        }
        result = CodexPlanner.validate(plan, self.store.describe("maya"), before)
        self.assertEqual(result["view"]["type"], "Opportunity")
        self.assertEqual(result["proposals"][0]["changes"], {"stage": "proposal"})
        self.assertEqual(self.store.list_records("maya"), before)
        self.assertEqual(len(self.store.history("maya", "Opportunity", "northstar-renewal")), 1)


if __name__ == "__main__":
    unittest.main()
