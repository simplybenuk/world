"""Exercise the record protocol over real HTTP without an AI provider."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from client_gateway.planner import CodexPlanner
from world.server import AccessTokens, WorldHTTPServer
from world.store import WorldStore


class StubPlanner(CodexPlanner):
    """Keep input and output validation, replacing only the paid provider call."""

    def __init__(self):
        super().__init__()
        self.payloads = []

    @property
    def available(self):
        return True

    def _generate(self, payload):
        self.payloads.append(deepcopy(payload))
        tasks = [record for record in payload["records"] if record["type"] == "Task"]
        return {
            "message": "Here are the visible tasks.",
            "view": {
                "layout": "table", "title": "Visible tasks", "description": "",
                "type": "Task", "record_ids": [record["id"] for record in tasks],
                "fields": ["title", "status", "owner"], "group_by": None,
            },
            "proposals": [],
        }


class HTTPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.database = self.data_dir / "world.sqlite3"
        self.store = WorldStore(self.database)
        self.addCleanup(self.store.close)
        self.access = AccessTokens(self.data_dir)
        self.planner = StubPlanner()
        self.server = WorldHTTPServer(("127.0.0.1", 0), self.store, self.access, self.planner)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.host, self.port = self.server.server_address

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, *, actor="ben", headers=None, raw=None):
        request_headers = {}
        if actor is not None:
            request_headers["Authorization"] = "Bearer " + self.access.tokens[actor]
        if raw is not None:
            encoded = raw
            request_headers["Content-Type"] = "application/json"
        elif body is not None:
            encoded = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        else:
            encoded = None
        request_headers.update(headers or {})
        connection = HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=encoded, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            response_headers = dict(response.getheaders())
            if response_headers.get("Content-Type", "").startswith("application/json"):
                data = json.loads(data)
            return response.status, data, response_headers
        finally:
            connection.close()

    def record(self, type_name="Task", record_id="documentation", actor="ben"):
        status, data, _ = self.request("GET", f"/api/records/{type_name}/{record_id}", actor=actor)
        self.assertEqual(status, 200, data)
        return data["record"]

    def assert_error(self, status, result, expected_status, code=None):
        self.assertEqual(status, expected_status, result)
        self.assertIn("error", result)
        if code is not None:
            self.assertEqual(result["error"]["code"], code)

    def test_anonymous_and_header_identity_cannot_access_the_world(self):
        for method, path, body in [
            ("GET", "/api/world", None),
            ("GET", "/api/records?type=Task", None),
            ("GET", "/api/history/Task/documentation", None),
            ("POST", "/client/intent", {"message": "Show tasks", "actor": "ben"}),
            ("POST", "/client/apply", {"actor": "ben", "proposal": {}}),
        ]:
            with self.subTest(method=method, path=path):
                status, result, _ = self.request(
                    method, path, body, actor=None,
                    headers={"X-Actor": "ben", "X-Actor-Role": "admin"},
                )
                self.assert_error(status, result, 401, "authentication_required")
        self.assertEqual(self.planner.payloads, [])

    def test_cookie_sign_in_and_bearer_tokens_resolve_server_issued_actors(self):
        status, result, headers = self.request(
            "POST", "/api/session", {"token": self.access.tokens["maya"], "actor": "ben"}, actor=None
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["actor"]["id"], "maya")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, result, _ = self.request(
            "GET", "/api/session", actor=None, headers={"Cookie": cookie, "X-Actor": "ben"}
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["actor"]["id"], "maya")
        status, result, _ = self.request("GET", "/api/session", actor="sam")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["actor"]["id"], "sam")
        status, result, _ = self.request(
            "GET", "/api/session", actor=None,
            headers={"Cookie": cookie, "Authorization": "Bearer incorrect"},
        )
        self.assert_error(status, result, 401)

    def test_invalid_credentials_are_errors_even_for_non_ascii_input(self):
        for token in ("incorrect", "é", ["ben"], {"actor": "ben"}, None):
            with self.subTest(token=token):
                status, result, _ = self.request("POST", "/api/session", {"token": token}, actor=None)
                self.assert_error(status, result, 401, "invalid_token")
        status, result, _ = self.request(
            "GET", "/api/session", actor=None, headers={"Authorization": "Bearer é"}
        )
        self.assert_error(status, result, 401)

    def test_credentials_persist_privately_and_sign_out_expires_the_cookie(self):
        restored = AccessTokens(self.data_dir)
        self.assertEqual(restored.tokens, self.access.tokens)
        self.assertEqual(self.access.path.stat().st_mode & 0o777, 0o600)
        status, result, headers = self.request("DELETE", "/api/session")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["signed_out"])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_discovery_and_queries_respect_actor_visibility(self):
        status, world, _ = self.request("GET", "/api/world", actor="sam")
        self.assertEqual(status, 200, world)
        self.assertEqual(set(world["types"]), {"Person", "Project", "Task", "Comment"})
        self.assertFalse(world["types"]["Task"]["capabilities"]["create"])
        status, schema, _ = self.request("GET", "/api/types/Task", actor="sam")
        self.assertEqual(status, 200, schema)
        self.assertIn("status", schema["fields"])
        for actor in ("sam", "maya"):
            with self.subTest(actor=actor):
                status, result, _ = self.request("GET", "/api/records?type=Task", actor=actor)
                self.assertEqual(status, 200, result)
                ids = {record["id"] for record in result["records"]}
                self.assertIn("documentation", ids)
                self.assertNotIn("world-protocol", ids)
                self.assertNotIn("client-experiment", ids)
                for path in ("/api/records/Task/world-protocol", "/api/history/Task/world-protocol"):
                    status, result, _ = self.request("GET", path, actor=actor)
                    self.assertIn(status, (403, 404), result)
                    self.assertIn("error", result)
                status, result, _ = self.request(
                    "POST", "/api/query", {"type": "Task", "filters": [
                        {"field": "project", "op": "eq", "value": "world"}
                    ]}, actor=actor,
                )
                self.assertEqual(status, 200, result)
                self.assertEqual(result["records"], [])
        status, result, _ = self.request("GET", "/api/records?type=Task", actor="alex")
        self.assertEqual(status, 200, result)
        self.assertIn("world-protocol", {record["id"] for record in result["records"]})
        status, result, _ = self.request("POST", "/api/query", {"type": "Task", "filters": [
            {"field": "status", "op": "eq", "value": "done"},
            {"field": "project", "op": "eq", "value": "website"},
        ]})
        self.assertEqual(status, 200, result)
        self.assertEqual([record["id"] for record in result["records"]], ["launch-announcement"])

    def test_viewer_cannot_write_or_inject_an_administrative_proposal(self):
        before = self.record()
        attempts = [
            ("PATCH", "/api/records/Task/documentation", {
                "changes": {"status": "done"}, "expected_version": before["version"],
                "actor": "ben", "requested_by": "ben",
            }),
            ("POST", "/client/apply", {"actor": "ben", "proposal": {
                "operation": "update", "type": "Task", "id": "documentation",
                "changes": {"status": "done"}, "expected_version": before["version"],
                "reason": "Spoofed administrator",
            }}),
            ("POST", "/client/apply", {"proposal": {
                "operation": "create", "type": "Task", "id": None,
                "changes": {"title": "Unauthorized task", "project": "website"},
                "expected_version": None, "reason": "Spoofed administrator",
            }}),
            ("POST", "/client/apply", {"proposal": {
                "operation": "delete", "type": "Comment", "id": "documentation-notes",
                "changes": {}, "expected_version": self.record("Comment", "documentation-notes")["version"],
                "reason": "Spoofed administrator",
            }}),
        ]
        for method, path, body in attempts:
            with self.subTest(method=method, operation=body.get("proposal", {}).get("operation")):
                status, result, _ = self.request(method, path, body, actor="sam", headers={
                    "X-Actor": "ben", "X-Actor-Role": "admin",
                })
                self.assert_error(status, result, 403)
        self.assertEqual(self.record(), before)

    def test_member_cannot_escalate_role_and_provenance_uses_authenticated_actor(self):
        maya = self.record("Person", "maya")
        status, result, _ = self.request("PATCH", "/api/records/Person/maya", {
            "changes": {"role": "admin"}, "expected_version": maya["version"], "actor": "ben",
        }, actor="maya", headers={"X-Actor": "ben"})
        self.assert_error(status, result, 403)
        before = self.record()
        status, result, _ = self.request("PATCH", "/api/records/Task/documentation", {
            "changes": {"status": "doing"}, "expected_version": before["version"],
            "actor": "ben", "requested_by": "ben", "reason": "Start documentation", "evidence": ["meeting-notes"],
        }, actor="maya")
        self.assertEqual(status, 200, result)
        status, result, _ = self.request("GET", "/api/history/Task/documentation")
        self.assertEqual(status, 200, result)
        event = result["events"][-1]
        self.assertEqual(event["actor"], "maya")
        self.assertEqual(event["requested_by"], "maya")
        self.assertEqual(event["reason"], "Start documentation")
        self.assertEqual(event["evidence"], ["meeting-notes"])

    def test_two_simultaneous_edits_have_one_winner_and_persist_history(self):
        before = self.record()
        barrier = threading.Barrier(2)

        def update(status):
            barrier.wait(timeout=5)
            return self.request("PATCH", "/api/records/Task/documentation", {
                "changes": {"status": status}, "expected_version": before["version"],
                "reason": f"Set documentation to {status}", "evidence": ["test-review"],
            })

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ("doing", "done")))
        self.assertEqual(sorted(response[0] for response in results), [200, 409], results)
        winner = next(result for status, result, _ in results if status == 200)["record"]
        self.assertEqual(winner["version"], before["version"] + 1)
        self.server.store = WorldStore(self.database)
        self.assertEqual(self.record(), winner)
        status, result, _ = self.request("GET", "/api/history/Task/documentation")
        self.assertEqual(status, 200, result)
        updates = [event for event in result["events"] if event["action"] == "update"]
        self.assertEqual(len(updates), 1, result)
        self.assertEqual(updates[0]["actor"], "ben")
        self.assertEqual(updates[0]["evidence"], ["test-review"])
        self.assertIn(winner["data"]["status"], updates[0]["reason"])
        self.assertEqual(updates[0]["before"]["data"], before["data"])
        self.assertEqual(updates[0]["after"]["data"], winner["data"])
        self.assertEqual(updates[0]["after"]["version"], winner["version"])

    def test_mutations_require_an_integer_version_before_saving(self):
        before = self.record()
        for version in (None, True, "1", [], 0):
            with self.subTest(version=version):
                status, result, _ = self.request("PATCH", "/api/records/Task/documentation", {
                    "changes": {"status": "done"}, "expected_version": version,
                })
                self.assert_error(status, result, 428)
        status, result, _ = self.request("PATCH", "/api/records/Task/documentation", {
            "changes": {"status": "done"},
        })
        self.assert_error(status, result, 428)
        self.assertEqual(self.record(), before)

    def test_record_validation_rejects_bad_values_without_an_edit(self):
        before = self.record()
        for changes in ({"status": "finished"}, {"due": "2026-02-30"}, {"owner": []},
                        {"unexpected": "value"}, {"title": " "}, ["status", "done"]):
            with self.subTest(changes=changes):
                status, result, _ = self.request("PATCH", "/api/records/Task/documentation", {
                    "changes": changes, "expected_version": before["version"],
                })
                self.assert_error(status, result, 422)
        self.assertEqual(self.record(), before)

    def test_malformed_and_unsupported_inputs_return_client_errors(self):
        attempts = [
            ("POST", "/api/query", {"type": []}),
            ("POST", "/api/query", {"type": "Task", "filters": {}}),
            ("POST", "/api/query", {"type": "Task", "filters": [{"field": [], "op": "eq", "value": "done"}]}),
            ("POST", "/api/query", {"type": "Task", "filters": [{"field": "status", "op": [], "value": "done"}]}),
            ("POST", "/api/query", {"type": "Task", "filters": [{"field": "status", "op": "execute", "value": "done"}]}),
            ("POST", "/client/apply", {"proposal": []}),
            ("POST", "/client/apply", {"proposal": {"operation": "execute", "type": "Task"}}),
            ("POST", "/client/apply", {"proposal": {"operation": "create", "type": [], "changes": {}}}),
            ("PATCH", "/api/records/Task/documentation", {"reason": [], "changes": {"status": "done"}, "expected_version": 1}),
            ("PATCH", "/api/records/Task/documentation", {"evidence": {}, "changes": {"status": "done"}, "expected_version": 1}),
            ("POST", "/client/intent", {"message": []}),
            ("POST", "/client/intent", {"message": "Show tasks", "context": []}),
        ]
        for method, path, body in attempts:
            with self.subTest(method=method, path=path, body=body):
                status, result, _ = self.request(method, path, body)
                self.assert_error(status, result, 400)
        for raw in (b"[]", b"{", b'{"token":NaN}'):
            with self.subTest(raw=raw):
                status, result, _ = self.request("POST", "/api/session", actor=None, raw=raw)
                self.assert_error(status, result, 400)
        status, result, _ = self.request("POST", "/api/query", raw=b'{"type":[1e999]}')
        self.assert_error(status, result, 400)

    def test_cross_origin_requests_are_rejected_before_record_changes(self):
        before = self.record()
        body = {"changes": {"status": "done"}, "expected_version": before["version"]}
        for headers in (
            {"Origin": "http://other.example"},
            {"Origin": "null"},
            {"Origin": f"http://{self.host}:{self.port}", "Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                status, result, _ = self.request("PATCH", "/api/records/Task/documentation", body, headers=headers)
                self.assert_error(status, result, 403, "cross_origin_request")
        self.assertEqual(self.record(), before)
        status, result, _ = self.request("PATCH", "/api/records/Task/documentation", body,
                                         headers={"Origin": f"http://{self.host}:{self.port}"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["record"]["data"]["status"], "done")
        status, result, _ = self.request("POST", "/api/session", {"token": self.access.tokens["ben"]},
                                         actor=None, headers={"Origin": "http://other.example"})
        self.assert_error(status, result, 403)

    def test_static_assets_and_health_do_not_publish_records_or_credentials(self):
        private_task = self.record("Task", "world-protocol")
        for path in ("/", "/index.html", "/app.js", "/styles.css", "/health"):
            with self.subTest(path=path):
                status, data, headers = self.request("GET", path, actor=None)
                self.assertEqual(status, 200, data)
                text = json.dumps(data) if isinstance(data, dict) else data.decode("utf-8")
                for token in self.access.tokens.values():
                    self.assertNotIn(token, text)
                self.assertNotIn(private_task["data"]["description"], text)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        for path in ("/access.json", "/.git/config", "/../world/server.py", "/%2e%2e/access.json"):
            with self.subTest(path=path):
                status, result, _ = self.request("GET", path, actor=None)
                self.assertIn(status, (401, 403, 404), result)

    def test_intent_receives_only_visible_records_and_never_saves(self):
        before = self.record()
        status, result, _ = self.request("POST", "/client/intent", {
            "message": "Show visible tasks", "actor": "ben",
            "context": {"conversation": [{"role": "user", "content": "Show tasks"}]},
        }, actor="sam")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["view"]["layout"], "table")
        self.assertEqual(result["proposals"], [])
        self.assertEqual(len(self.planner.payloads), 1)
        supplied = self.planner.payloads[0]
        self.assertNotIn("world-protocol", {record["id"] for record in supplied["records"]})
        self.assertEqual(supplied["request"], "Show visible tasks")
        self.assertEqual(self.record(), before)

    def test_missing_ai_provider_returns_an_error_while_records_still_work(self):
        before = self.record()
        self.server.planner = CodexPlanner(executable="world-test-provider-does-not-exist")
        status, result, _ = self.request("POST", "/client/intent", {"message": "Show tasks"})
        self.assert_error(status, result, 503, "provider_unavailable")
        self.assertNotIn("view", result)
        status, health, _ = self.request("GET", "/health", actor=None)
        self.assertEqual(status, 200, health)
        self.assertFalse(health["provider_available"])
        self.assertEqual(self.record(), before)

    def test_explicitly_accepted_proposal_is_saved_and_stale_acceptance_conflicts(self):
        before = self.record()
        proposal = {
            "operation": "update", "type": "Task", "id": "documentation",
            "changes": {"status": "done"}, "expected_version": before["version"],
            "reason": "Mark the documentation task as complete",
        }
        status, result, _ = self.request("POST", "/client/apply", {"proposal": proposal})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["record"]["data"]["status"], "done")
        status, result, _ = self.request("POST", "/client/apply", {"proposal": proposal})
        self.assert_error(status, result, 409)
        self.assertEqual(self.record()["version"], before["version"] + 1)


if __name__ == "__main__":
    unittest.main()
