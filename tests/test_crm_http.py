"""Run the CRM prototype through the same HTTP and planning adapters as the team."""

from http.client import HTTPConnection
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from client_gateway.planner import CodexPlanner
from world import WorldDefinition, WorldStore
from world.server import AccessTokens, WorldHTTPServer


ROOT = Path(__file__).resolve().parent.parent


class CRMPlanFixture(CodexPlanner):
    @property
    def available(self):
        return True

    def _generate(self, payload):
        opportunities = [record for record in payload["records"] if record["type"] == "Opportunity"]
        target_id = "northstar-pilot" if "skip" in payload["request"] else "northstar-renewal"
        target = next(record for record in opportunities if record["id"] == target_id)
        return {
            "message": "Ready to review.",
            "view": {
                "title": "Sales pipeline", "description": "HTTP prototype fixture.",
                "layout": "board", "type": "Opportunity", "fields": ["name", "stage", "amount_minor"],
                "record_ids": [record["id"] for record in opportunities], "group_by": "stage",
            },
            "proposals": [{
                "operation": "update", "type": "Opportunity", "id": target_id,
                "changes": [{"field": "stage", "value_json": json.dumps("won" if "skip" in payload["request"] else "proposal")}],
                "expected_version": target["version"], "reason": payload["request"],
            }],
        }


class CRMHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="world-crm-http-")
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        self.definition = WorldDefinition.load(ROOT / "definitions" / "crm.world.json")
        self.store = WorldStore(self.data_dir / "world.sqlite3", definition=self.definition)
        self.addCleanup(self.store.close)
        self.access = AccessTokens(self.data_dir, actors=self.definition.actor_ids)
        self.server = self.start_server(self.store, self.access)

    def start_server(self, store, access):
        server = WorldHTTPServer(("127.0.0.1", 0), store, access, CRMPlanFixture())
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        return server

    def request(self, method, path, body=None, *, actor="ben", cookie=None, server=None, access=None):
        server = server or self.server
        access = access or self.access
        headers = {}
        if actor is not None:
            headers["Authorization"] = "Bearer " + access.tokens[actor]
        if cookie is not None:
            headers["Cookie"] = cookie
        encoded = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            encoded = json.dumps(body)
        connection = HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def test_crm_definition_drives_http_discovery_and_numeric_query(self):
        status, description, _ = self.request("GET", "/api/world")
        self.assertEqual(status, 200)
        self.assertEqual(description["id"], "crm")
        self.assertEqual(description["actor_type"], "Person")
        self.assertEqual(set(description["types"]), {"Person", "Organization", "Contact", "Opportunity", "Interaction"})
        status, result, _ = self.request("POST", "/api/query", {
            "type": "Opportunity", "filters": [{"field": "amount_minor", "op": "gt", "value": 0}],
        })
        self.assertEqual(status, 200, result)
        self.assertTrue(result["records"])
        self.assertTrue(all(record["data"]["amount_minor"] > 0 for record in result["records"]))

    def test_reviewed_proposal_saves_once_with_history_and_conflicts_on_retry(self):
        before = self.store.get_record("ben", "Opportunity", "northstar-renewal")
        history_before = self.store.history("ben", "Opportunity", before["id"])
        status, plan, _ = self.request("POST", "/client/intent", {"message": "Move the Northstar renewal opportunity to proposal."})
        self.assertEqual(status, 200, plan)
        self.assertEqual(self.store.get_record("ben", "Opportunity", before["id"]), before)
        self.assertEqual(self.store.history("ben", "Opportunity", before["id"]), history_before)
        proposal = plan["proposals"][0]
        status, result, _ = self.request("POST", "/client/apply", {"proposal": proposal})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["record"]["data"]["stage"], "proposal")
        events = self.store.history("ben", "Opportunity", before["id"])
        self.assertEqual(len(events), len(history_before) + 1)
        self.assertEqual(events[-1]["actor"], "ben")
        self.assertEqual(events[-1]["before"]["data"]["stage"], "qualified")
        self.assertEqual(events[-1]["after"]["data"]["stage"], "proposal")
        status, result, _ = self.request("POST", "/client/apply", {"proposal": proposal})
        self.assertEqual(status, 409, result)
        self.assertEqual(self.store.history("ben", "Opportunity", before["id"]), events)

    def test_invalid_transition_is_rejected_during_generation_and_direct_apply(self):
        before = self.store.get_record("ben", "Opportunity", "northstar-pilot")
        events = self.store.history("ben", "Opportunity", before["id"])
        status, result, _ = self.request("POST", "/client/intent", {"message": "skip stages and win the pilot"})
        self.assertEqual(status, 502, result)
        self.assertEqual(result["error"]["code"], "invalid_plan")
        status, result, _ = self.request("POST", "/client/apply", {"proposal": {
            "operation": "update", "type": "Opportunity", "id": before["id"],
            "changes": {"stage": "won"}, "expected_version": before["version"], "reason": "Try an invalid transition",
        }})
        self.assertEqual(status, 422, result)
        self.assertEqual(result["error"]["code"], "invalid_transition")
        self.assertEqual(self.store.get_record("ben", "Opportunity", before["id"]), before)
        self.assertEqual(self.store.history("ben", "Opportunity", before["id"]), events)

    def test_member_can_accept_an_interaction_attributed_to_themselves(self):
        description = self.store.describe("maya")
        records = self.store.list_records("maya")
        changes = {"body": "Fictional follow-up", "contact": "northstar-jordan", "author": "maya",
                   "opportunity": None, "occurred": datetime.now(timezone.utc).date().isoformat()}
        raw_plan = {
            "message": "Ready to review this interaction.",
            "view": {"title": "Interactions", "description": "", "layout": "list", "type": "Interaction",
                     "fields": ["body", "author"], "record_ids": [], "group_by": None},
            "proposals": [{"operation": "create", "type": "Interaction", "id": None, "expected_version": None,
                           "changes": [{"field": field, "value_json": json.dumps(value)} for field, value in changes.items()],
                           "reason": "Record the fictional follow-up"}],
        }
        before = self.store.list_records("maya", "Interaction")
        plan = CodexPlanner.validate(raw_plan, description, records)
        self.assertEqual(self.store.list_records("maya", "Interaction"), before)
        status, result, _ = self.request("POST", "/client/apply", {"proposal": plan["proposals"][0]}, actor="maya")
        self.assertEqual(status, 201, result)
        created = result["record"]
        self.assertEqual(created["data"]["author"], "maya")
        self.assertEqual(self.store.history("maya", "Interaction", created["id"])[0]["actor"], "maya")

    def test_parallel_worlds_keep_separate_browser_cookies(self):
        team_dir = self.data_dir / "team"
        team_dir.mkdir()
        team_store = WorldStore(team_dir / "world.sqlite3")
        self.addCleanup(team_store.close)
        team_access = AccessTokens(team_dir, actors=team_store.definition.actor_ids)
        team_server = self.start_server(team_store, team_access)
        _, _, team_headers = self.request("POST", "/api/session", {"token": team_access.tokens["maya"]},
                                          actor=None, server=team_server, access=team_access)
        _, _, crm_headers = self.request("POST", "/api/session", {"token": self.access.tokens["sam"]}, actor=None)
        self.assertNotEqual(team_server.session_cookie, self.server.session_cookie)
        combined = "; ".join(headers["Set-Cookie"].split(";", 1)[0] for headers in [team_headers, crm_headers])
        status, result, _ = self.request("GET", "/api/session", actor=None, cookie=combined,
                                        server=team_server, access=team_access)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["actor"]["id"], "maya")
        status, result, _ = self.request("GET", "/api/session", actor=None, cookie=combined)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["actor"]["id"], "sam")
        status, _, crm_logout = self.request("DELETE", "/api/session", actor=None)
        self.assertEqual(status, 200)
        self.assertTrue(crm_logout["Set-Cookie"].startswith(self.server.session_cookie + "="))
        self.assertIn("Max-Age=0", crm_logout["Set-Cookie"])

    def test_access_tokens_can_be_issued_for_declared_identities(self):
        other_dir = self.data_dir / "other-actors"
        other_dir.mkdir()
        access = AccessTokens(other_dir, actors=["alex"])
        self.assertEqual(set(access.tokens), {"alex"})
        self.assertEqual(access.actor_for(access.tokens["alex"]), "alex")


if __name__ == "__main__":
    unittest.main()
