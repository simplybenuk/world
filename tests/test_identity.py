"""Persistent subjects are authenticated, not assertions in record data or display names."""

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from world.definition import DataDefinition
from world.server import AccessTokens, WorldHTTPServer
from world.store import WorldStore
from world.rules import RuleError


def definition():
    no = {"read": "False", "create": "False", "update": "False", "delete": "False"}
    public = "caller.identity is not None or caller.agent == 'teamworld'"
    return DataDefinition({"name": "identities", "title": "Identity test", "actor_type": "Person",
        "rules": {"define": "caller.identity is None and actor.role == 'admin'"},
        "types": {
            "Person": {"fields": {"name": {"type": "text"}, "email": {"type": "text"}, "role": {"type": "text"}},
                "rules": {**no, "read": "caller.identity is None"}},
            "Agent": {"fields": {"name": {"type": "text"}, "owner": {"type": "reference", "target": "Person"}},
                "rules": {**no, "read": public, "update": "record.id == caller.identity and record.owner == actor.id"}},
            "Note": {"fields": {
                "author": {"type": "reference", "target": "Agent", "required": True, "default": "$identity", "write_rule": "False"},
                "body": {"type": "text", "required": True, "max_length": 200}},
                "rules": {**no, "read": public,
                    "create": "caller.identity is not None and record.author == caller.identity and record.author.owner == actor.id",
                    "update": "record.author == caller.identity"}},
            "Memory": {"fields": {
                "author": {"type": "reference", "target": "Agent", "required": True, "default": "$identity", "write_rule": "False"},
                "body": {"type": "text", "required": True}},
                "rules": {**no, "read": "record.author == caller.identity or caller.identity is None and record.author.owner == actor.id",
                    "create": "record.author == caller.identity and record.author.owner == actor.id"}}},
        "records": [
            {"type": "Person", "id": "ben", "data": {"name": "Ben", "email": "private@example.test", "role": "admin"}},
            {"type": "Agent", "id": "agent-pip", "data": {"name": "Pip", "owner": "ben"}},
            {"type": "Agent", "id": "agent-wren", "data": {"name": "Wren", "owner": "ben"}}]})


class IdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = WorldStore(self.directory / "world.sqlite3", definition())
        self.addCleanup(self.store.close)
        self.access = AccessTokens(self.directory, ["ben"])
        self.issuer = self.access.issuer_key(create=True)
        self.server = WorldHTTPServer(("127.0.0.1", 0), self.store, self.access)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        self.pip = self.issue("codex-pip", "agent-pip")
        self.wren = self.issue("claude-wren", "agent-wren")

    def request(self, method, path, body=None, token=None, connection=None):
        connection = connection or HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, json.dumps(body).encode() if body is not None else None,
                {"Content-Type": "application/json", "Authorization": "Bearer " + (token or self.pip)})
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data.decode()
        finally:
            if connection is not getattr(self, "kept", None):
                connection.close()

    def issue(self, agent, identity, **extra):
        status, data = self.request("POST", "/api/access", {"actor": "ben", "agent": agent, "identity": identity,
            "expires_in": 3600, **extra}, self.issuer)
        self.assertEqual(status, 201, data)
        return data["access_token"]

    def mcp(self, tool, arguments=None, token=None):
        status, data = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}}}, token)
        if status != 200:
            return status, data
        result = data["result"]
        value = result["content"][0]["text"]
        return not result["isError"], value if tool == "audit" and not result["isError"] else json.loads(value)

    def test_two_same_owner_subjects_cannot_forge_or_edit_each_other(self):
        status, made = self.request("POST", "/api/records/Note", {"data": {"body": "hello"}})
        self.assertEqual(status, 201, made)
        record = made["record"]
        self.assertEqual(record["data"]["author"], "agent-pip")
        status, _ = self.request("POST", "/api/records/Note", {"data": {"author": "agent-wren", "body": "forged"}, "identity": "agent-wren"})
        self.assertEqual(status, 403)
        ok, refused = self.mcp("change", {"steps": [
            {"create": "Note", "data": {"body": "rolled back"}},
            {"create": "Note", "data": {"author": "agent-pip", "body": "forged"}}]}, self.wren)
        self.assertFalse(ok, refused)
        ok, found = self.mcp("query", {"type": "Note"})
        self.assertEqual(len(found["records"]), 1)
        ok, _ = self.mcp("update", {"type": "Note", "id": record["id"], "changes": {"body": "overwrite"}, "expected_version": 1}, self.wren)
        self.assertFalse(ok)
        ok, changed = self.mcp("update", {"type": "Note", "id": record["id"], "changes": {"body": "follow up"}, "expected_version": 1})
        self.assertTrue(ok, changed)
        ok, _ = self.mcp("define", {"rules": {"define": "True"}})
        self.assertFalse(ok)
        ok, history = self.mcp("history", {"type": "Note", "id": record["id"]})
        self.assertEqual([event["identity"] for event in history["events"]], ["agent-pip", "agent-pip"])
        ok, audit = self.mcp("audit")
        self.assertIn("ben via codex-pip as agent-pip", audit)

    def test_untrusted_query_cannot_inspect_unreadable_actor(self):
        for email in ['private@example.test', 'wrong@example.test']:
            ok, result = self.mcp('query', {'type': 'Agent', 'where': f"actor.email == '{email}'"})
            self.assertTrue(ok, result)
            self.assertEqual(result['records'], [])
        ok, result = self.mcp('query', {'type': 'Agent', 'where': 'record.id == caller.identity'})
        self.assertTrue(ok, result)
        self.assertEqual([record['id'] for record in result['records']], ['agent-pip'])

    def test_private_history_and_query_include_respect_subject(self):
        ok, made = self.mcp("create", {"type": "Memory", "data": {"body": "private"}})
        self.assertTrue(ok, made)
        identifier = made["record"]["id"]
        ok, _ = self.mcp("history", {"type": "Memory", "id": identifier}, self.wren)
        self.assertFalse(ok)
        ok, found = self.mcp("query", {"type": "Memory"}, self.wren)
        self.assertEqual(found["records"], [])
        ok, found = self.mcp("query", {"type": "Agent", "include": ["owner"]})
        self.assertNotIn("private@example.test", json.dumps(found))
        ok, desc = self.mcp("describe")
        self.assertEqual(desc["caller"], {"agent": "codex-pip", "identity": "agent-pip"})
        self.assertEqual(desc["actor"], {"type": "Person", "id": "ben"})

    def test_definition_and_session_paths_do_not_expose_private_actor(self):
        status, value = self.request("GET", "/api/definition")
        self.assertEqual(status, 200)
        self.assertNotIn("records", value["definition"])
        self.assertNotIn("private@example.test", json.dumps(value))
        ok, value = self.mcp("define")
        self.assertTrue(ok)
        self.assertNotIn("records", value["definition"])
        self.assertNotIn("private@example.test", json.dumps(value))
        self.assertEqual(self.request("POST", "/api/session", {"token": self.pip})[0], 403)
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            for path, expected in (("/api/session", 403), ("/api/world", 200)):
                connection.request("GET", path, headers={"Cookie": "world_session=" + self.pip})
                response = connection.getresponse()
                value = json.loads(response.read())
                self.assertEqual(response.status, expected)
                self.assertNotIn("private@example.test", json.dumps(value))
                if expected == 200:
                    self.assertEqual(value["caller"]["identity"], "agent-pip")
        finally:
            connection.close()

    def test_identity_binding_survives_reload_refresh_and_revocation(self):
        renewed = self.issue("codex-pip", "agent-pip", renew=True)
        reloaded = AccessTokens(self.directory, ["ben"])
        self.assertEqual(reloaded.principal(renewed), ("ben", "codex-pip", "agent-pip"))
        for identity in ("agent-wren", None, "bad identity"):
            status, _ = self.request("POST", "/api/access", {"actor": "ben", "agent": "codex-pip", "identity": identity}, self.issuer)
            self.assertEqual(status, 422)
        status, _ = self.request("DELETE", "/api/access", {"actor": "ben", "agent": "codex-pip"}, self.issuer)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/world", token=renewed)[0], 401)
        self.assertEqual(self.request("POST", "/api/access", {"actor": "ben", "agent": "codex-pip", "identity": "agent-pip", "renew": True}, self.issuer)[0], 409)
        self.issue("codex-pip", "agent-wren")
        self.assertEqual(self.request("GET", "/api/world", token=renewed)[0], 401)

    def test_context_is_reset_on_keepalive_and_separate_threads(self):
        self.kept = HTTPConnection(*self.server.server_address, timeout=5)
        self.addCleanup(self.kept.close)
        for token, expected in ((self.pip, "agent-pip"), (self.wren, "agent-wren"), (self.access.tokens["ben"], None)):
            status, value = self.request("GET", "/api/world", token=token, connection=self.kept)
            self.assertEqual(status, 200)
            self.assertEqual(value["caller"]["identity"], expected)
        tokens = [self.pip, self.wren] * 5
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda token: self.request("GET", "/api/world", token=token)[1]["caller"]["identity"], tokens))
        self.assertEqual(values, ["agent-pip", "agent-wren"] * 5)
        self.assertIsNone(self.store.describe("ben")["caller"]["identity"])

    def test_rate_limit_is_shared_across_tokens_and_connections(self):
        self.server.identity_rate_limit = 2
        second = self.issue("another-pip", "agent-pip")
        self.assertEqual(self.request("GET", "/api/world")[0], 200)
        self.assertEqual(self.request("GET", "/api/world", token=second)[0], 200)
        self.assertEqual(self.request("GET", "/api/world")[0], 429)
        self.assertEqual(self.mcp("describe")[0], 429)
        self.assertEqual(self.request("GET", "/api/world", token=self.wren)[0], 200)

    def test_work_budget_refuses_expensive_requests_and_rolls_back_batches(self):
        self.server.rule_step_limit = 50
        ok, refused = self.mcp("change", {"steps": [
            {"create": "Note", "data": {"body": "must roll back"}} for _ in range(20)]})
        self.assertFalse(ok)
        self.assertEqual(refused["error"]["code"], "work_limit")
        self.server.rule_step_limit = 100000
        ok, found = self.mcp("query", {"type": "Note"})
        self.assertTrue(ok)
        self.assertEqual(found["records"], [])
        self.server.rule_step_limit = 1
        status, refused = self.request("GET", "/api/world")
        self.assertEqual(status, 429)
        self.assertEqual(refused["error"]["code"], "work_limit")

    def test_bad_caller_fields_fail_when_definition_loads(self):
        data = definition().as_data()
        data["types"]["Note"]["rules"]["create"] = "caller.secret == 'x'"
        with self.assertRaises(RuleError):
            DataDefinition(data).check()
