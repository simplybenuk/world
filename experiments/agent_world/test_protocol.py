"""Focused HTTP acceptance checks on disposable third-experiment data."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from experiments.agent_world.domain import load_fixture
from experiments.agent_world.client import HTTPFactAdapter
from experiments.agent_world.server import AgentWorldHTTPServer
from experiments.agent_world.store import FactWorldStore
from world.server import AccessTokens


class HeadlessProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="world-headless-http-")
        self.store = FactWorldStore(Path(self.directory.name) / "facts.sqlite3", load_fixture())
        self.access = AccessTokens(self.directory.name, self.store.definition.actor_ids)
        self.server = AgentWorldHTTPServer(("127.0.0.1", 0), self.store, self.access)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.seed = self.store.head()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.directory.cleanup()

    def request(self, path, *, method="GET", actor="ben", body=None):
        headers = {}
        if actor:
            headers["Authorization"] = "Bearer " + self.access.tokens[actor]
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def select(self, **extra):
        return self.request("/api/select", method="POST", body={
            "type": "Opportunity",
            "filters": [{"field": "organization", "op": "eq", "value": "northstar"}],
            "select": ["name", "stage", "owner.name"],
            **extra,
        })

    def test_service_description_and_discovery_have_no_browser(self):
        status, service = self.request("/", actor=None)
        self.assertEqual(status, 200)
        self.assertTrue(service["prototype"])
        self.assertNotIn("records", service)
        self.assertEqual(self.request("/app.js")[0], 404)
        self.assertEqual(self.request("/client/intent", method="POST", body={})[0], 404)
        status, description = self.request("/api/world")
        self.assertEqual(status, 200)
        self.assertEqual(description["types"]["Opportunity"]["fields"]["amount_minor"]["meaning"]["unit"], "minor_currency_unit")
        self.assertFalse(hasattr(self.server, "planner"))

    def test_authentication_and_joined_read(self):
        self.assertEqual(self.request("/api/select", method="POST", actor=None, body={})[0], 401)
        status, result = self.select(include=[{
            "name": "latest", "type": "Interaction", "via": "opportunity",
            "select": ["body", "author.name"], "order_by": "occurred", "descending": True, "limit": 1,
        }])
        self.assertEqual(status, 200)
        self.assertEqual(result["snapshot"], self.seed)
        self.assertEqual({record["id"] for record in result["records"]}, {"northstar-renewal", "northstar-pilot"})
        renewal = next(record for record in result["records"] if record["id"] == "northstar-renewal")
        self.assertEqual(renewal["values"]["owner.name"], "Ben")
        self.assertEqual(renewal["next"]["stage"], ["proposal", "lost"])
        self.assertIn("Prepare a written proposal", renewal["related"]["latest"][0]["values"]["body"])

    def test_accepted_and_historical_values_have_provenance(self):
        status, accepted = self.request("/api/transact", method="POST", body={
            "operations": [{"type": "Opportunity", "id": "northstar-renewal", "changes": {"stage": "proposal"}, "expected_version": 1}],
            "reason": "Prepare the proposal", "evidence": ["Interaction#renewal-discovery"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(accepted["snapshot"], self.seed + 1)
        status, old = self.select(as_of=self.seed)
        self.assertEqual(status, 200)
        self.assertEqual(next(record for record in old["records"] if record["id"] == "northstar-renewal")["values"]["stage"], "qualified")
        status, current = self.select()
        self.assertEqual(status, 200)
        self.assertEqual(next(record for record in current["records"] if record["id"] == "northstar-renewal")["values"]["stage"], "proposal")
        status, facts = self.request("/api/facts/Opportunity/northstar-renewal")
        self.assertEqual(status, 200)
        self.assertIn("Prepare the proposal", json.dumps(facts))
        self.assertIn("Interaction#renewal-discovery", json.dumps(facts))

    def test_invalid_second_change_rolls_back_the_whole_request(self):
        status, error = self.request("/api/transact", method="POST", body={
            "operations": [
                {"type": "Opportunity", "id": "northstar-renewal", "changes": {"stage": "proposal"}, "expected_version": 1},
                {"type": "Opportunity", "id": "northstar-pilot", "changes": {"stage": "won"}, "expected_version": 1},
            ],
        })
        self.assertEqual(status, 422, error)
        self.assertEqual(self.store.head(), self.seed)
        self.assertEqual(self.store.get_record("ben", "Opportunity", "northstar-renewal")["version"], 1)
        self.assertEqual(self.store.get_record("ben", "Opportunity", "northstar-renewal")["data"]["stage"], "qualified")

    def test_viewer_and_private_records(self):
        status, result = self.request("/api/select", method="POST", actor="sam", body={
            "type": "Opportunity", "select": ["name", "organization.name"],
            "filters": [{"field": "organization", "op": "eq", "value": "atlas"}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["records"], [])
        self.assertEqual(self.request("/api/facts/Opportunity/atlas-expansion", actor="sam")[0], 403)
        self.assertEqual(self.request("/api/transact", method="POST", actor="sam", body={
            "operations": [{"type": "Opportunity", "id": "northstar-renewal", "changes": {"stage": "proposal"}, "expected_version": 1}],
        })[0], 403)

    def test_legacy_update_records_facts_and_bad_snapshot_is_client_error(self):
        status, record = self.request("/api/records/Opportunity/northstar-renewal", method="PATCH", body={
            "changes": {"stage": "proposal"}, "expected_version": 1,
        })
        self.assertEqual(status, 200, record)
        self.assertEqual(self.store.head(), self.seed + 1)
        self.assertEqual(self.request("/api/facts/Opportunity/northstar-renewal?as_of=invalid")[0], 400)
        self.assertEqual(self.request("/api/select", method="POST", body={"type": "Opportunity", "select": ["invented"]})[0], 400)

    def test_external_agent_adapter_reads_http_and_cannot_auto_apply(self):
        adapter = HTTPFactAdapter(self.url, self.access.tokens["maya"], "maya")
        description = adapter.call("describe")
        self.assertEqual(description["actor"]["id"], "maya")
        result = adapter.call("select", {
            "type": "Opportunity", "filters": [{"field": "organization", "op": "eq", "value": "northstar"}],
            "select": ["name", "stage", "owner.name"],
        })
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(adapter.metrics()["logical_service_operations"], 2)
        bulk = adapter.call("list")
        self.assertEqual(len(bulk), 10)
        self.assertNotIn("atlas-expansion", {record["id"] for record in bulk})
        history = adapter.call("history", {"type": "Opportunity", "id": "northstar-renewal"})
        self.assertEqual(history[0]["after"]["data"]["stage"], "qualified")
        record = adapter.call("get", {"type": "Opportunity", "id": "northstar-renewal"})
        self.assertEqual(record["data"]["stage"], "qualified")
        records = adapter.call("query", {"type": "Opportunity", "filters": [{"field": "id", "op": "eq", "value": "northstar-renewal"}]})
        self.assertEqual([record["id"] for record in records], ["northstar-renewal"])
        self.assertEqual(description["record_counts"]["visible_total"], 10)
        with self.assertRaises(ValueError):
            adapter.apply([{"type": "Opportunity", "id": "northstar-renewal", "expected_version": 1,
                            "changes": {"stage": "proposal"}, "reason": "Unaccepted proposal"}])
        self.assertEqual(self.store.head(), self.seed)


if __name__ == "__main__":
    unittest.main()
