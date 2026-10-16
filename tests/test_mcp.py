"""Exercise the MCP endpoint over real HTTP, in both protocol eras."""

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from world.server import AccessTokens, WorldHTTPServer
from world.store import WorldStore


MODERN = "2026-07-28"


class MCPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = self.data_dir = Path(temporary.name)
        self.store = WorldStore(data_dir / "world.sqlite3")
        self.addCleanup(self.store.close)
        self.access = AccessTokens(data_dir)
        self.token = self.access.issue("ben", "claude")
        self.server = WorldHTTPServer(("127.0.0.1", 0), self.store, self.access)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        self.next_id = 0

    def post(self, message, headers=None, token=True, method="POST"):
        request_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token:
            request_headers["Authorization"] = "Bearer " + (self.token if token is True else token)
        request_headers.update(headers or {})
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, "/mcp", body=json.dumps(message).encode() if message is not None else None, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else None, response
        finally:
            connection.close()

    def modern(self, name, params=None, headers=None):
        self.next_id += 1
        params = {**(params or {}), "_meta": {"io.modelcontextprotocol/protocolVersion": MODERN,
                                              "io.modelcontextprotocol/clientCapabilities": {}}}
        sent = {"MCP-Protocol-Version": MODERN, "Mcp-Method": name}
        if name == "tools/call":
            sent["Mcp-Name"] = params["name"]
        sent.update(headers or {})
        return self.post({"jsonrpc": "2.0", "id": self.next_id, "method": name, "params": params}, sent)

    def call(self, tool, **arguments):
        status, body, _ = self.modern("tools/call", {"name": tool, "arguments": arguments})
        self.assertEqual(status, 200, body)
        result = body["result"]
        text = result["content"][0]["text"]
        return result["isError"], text if tool == "audit" and not result["isError"] else json.loads(text)

    def test_discover_and_list_tools(self):
        status, body, _ = self.modern("server/discover")
        self.assertEqual(status, 200)
        self.assertIn(MODERN, body["result"]["supportedVersions"])
        self.assertEqual(body["result"]["resultType"], "complete")
        status, body, _ = self.modern("tools/list")
        names = [tool["name"] for tool in body["result"]["tools"]]
        self.assertEqual(names, ["describe", "query", "get", "create", "update", "delete", "change", "history", "audit"])
        self.assertEqual(body["result"]["cacheScope"], "public")

    def test_tools_read_change_and_audit_as_person_via_agent(self):
        failed, world = self.call("describe")
        self.assertFalse(failed)
        self.assertEqual(world["agent"], "claude")
        self.assertNotIn("operations", world)
        failed, record = self.call("get", type="Task", id="documentation")
        self.assertFalse(failed)
        version = record["record"]["version"]
        failed, changed = self.call("update", type="Task", id="documentation", changes={"title": "Write the MCP docs"},
                                    expected_version=version, reason="Testing MCP")
        self.assertFalse(failed, changed)
        failed, found = self.call("query", type="Task", where="'mcp' in record.title")
        self.assertEqual([record["id"] for record in found["records"]], ["documentation"])
        failed, lines = self.call("audit", type="Task", id="documentation")
        self.assertRegex(lines, r"ben via claude +changed +Task documentation +title from")
        self.assertRegex(lines, r"ben via claude +viewed +Task documentation +Read")

    def test_a_token_issued_while_the_world_runs_works_at_once(self):
        token = AccessTokens(self.data_dir).issue("maya", "chatgpt")  # as `python3 -m world.server access` would
        self.token = token
        failed, lines = self.call("audit", type="Task", id="documentation")
        self.assertFalse(failed)
        self.assertEqual(AccessTokens(self.data_dir).issue("ben", "claude"), self.access.issue("ben", "claude"))

    def test_a_refused_change_is_a_tool_error_that_says_why(self):
        failed, refused = self.call("update", type="Task", id="documentation", changes={"title": "x"}, expected_version=999)
        self.assertTrue(failed)
        self.assertIn("message", refused["error"])
        failed, refused = self.call("get", type="Nothing", id="x")
        self.assertTrue(failed)
        self.assertEqual(refused["error"]["code"], "unknown_type")

    def test_legacy_clients_initialize_and_continue_without_meta(self):
        status, body, response = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(response.getheader("Mcp-Session-Id"))
        status, body, _ = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual((status, body), (202, None))
        status, body, _ = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "get", "arguments": {"type": "Task", "id": "documentation"}}}, {"MCP-Protocol-Version": "2025-06-18"})
        self.assertFalse(body["result"]["isError"])
        self.assertNotIn("resultType", body["result"])
        status, body, _ = self.post({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2024-01-01"}})
        self.assertEqual(body["result"]["protocolVersion"], "2025-11-25")

    def test_requests_that_must_be_refused(self):
        status, body, response = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=False)
        self.assertEqual(status, 401)
        self.assertTrue(response.getheader("WWW-Authenticate").startswith("Bearer"))
        status, body, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="not-a-token")
        self.assertEqual(status, 401)
        status, _, _ = self.post(None, method="GET")
        self.assertEqual(status, 405)
        status, body, _ = self.modern("tools/list", headers={"MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual((status, body["error"]["code"]), (400, -32020))
        status, body, _ = self.modern("tools/call", {"name": "get", "arguments": {}}, headers={"Mcp-Name": "delete"})
        self.assertEqual((status, body["error"]["code"]), (400, -32020))
        status, body, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                     "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}}},
                                    {"MCP-Protocol-Version": "1900-01-01", "Mcp-Method": "tools/list"})
        self.assertEqual((status, body["error"]["code"]), (400, -32022))
        self.assertIn(MODERN, body["error"]["data"]["supported"])
        status, body, _ = self.modern("resources/list")
        self.assertEqual((status, body["error"]["code"]), (404, -32601))
        status, body, _ = self.modern("tools/call", {"name": "drop_everything", "arguments": {}})
        self.assertEqual(body["error"]["code"], -32602)
        status, _, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
