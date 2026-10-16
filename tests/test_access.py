"""How an authorization server gives agents access to a world, and how an MCP client finds it."""

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from world.server import AccessTokens, WorldHTTPServer
from world.store import WorldStore


class AccessTests(unittest.TestCase):
    def start(self, **options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        self.store = WorldStore(self.data_dir / "world.sqlite3")
        self.addCleanup(self.store.close)
        self.access = AccessTokens(self.data_dir)
        self.server = WorldHTTPServer(("127.0.0.1", 0), self.store, self.access, **options)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)

    def request(self, method, path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
            response = connection.getresponse()
            data = response.read()
            parsed = json.loads(data) if data and response.getheader("Content-Type", "").startswith("application/json") else data.decode()
            return response.status, parsed, response
        finally:
            connection.close()

    def mcp(self, token):
        return self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "get", "arguments": {"type": "Task", "id": "documentation"}}}, token)

    def test_an_mcp_client_is_told_where_to_sign_in(self):
        self.start(public_url="https://world.example/", authorization_server="https://sign-in.example")
        status, metadata, _ = self.request("GET", "/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(status, 200)
        self.assertEqual(metadata["resource"], "https://world.example/mcp")
        self.assertEqual(metadata["authorization_servers"], ["https://sign-in.example"])
        status, _, response = self.mcp(None)
        self.assertEqual(status, 401)
        challenge = response.getheader("WWW-Authenticate")
        self.assertIn('resource_metadata="https://world.example/.well-known/oauth-protected-resource/mcp"', challenge)
        self.assertNotIn("error=", challenge)
        _, _, response = self.mcp("not-a-token")
        self.assertIn('error="invalid_token"', response.getheader("WWW-Authenticate"))

    def test_without_an_authorization_server_there_is_no_metadata(self):
        self.start()
        status, _, _ = self.request("GET", "/.well-known/oauth-protected-resource")
        self.assertEqual(status, 404)
        _, _, response = self.mcp(None)
        self.assertNotIn("resource_metadata", response.getheader("WWW-Authenticate"))

    def test_the_authorization_server_issues_lists_and_revokes_agent_access(self):
        self.start()
        status, _, _ = self.request("POST", "/api/access", {"actor": "maya", "agent": "claude-ai"}, "anything")
        self.assertEqual(status, 401)  # no issuer key yet
        key = self.access.issuer_key(create=True)
        status, _, _ = self.request("POST", "/api/access", {"actor": "maya", "agent": "claude-ai"}, self.access.tokens["ben"])
        self.assertEqual(status, 401)  # a person's token cannot issue access

        status, issued, _ = self.request("POST", "/api/access", {"actor": "maya", "agent": "claude-ai", "expires_in": 3600}, key)
        self.assertEqual((status, issued["token_type"], issued["expires_in"]), (201, "Bearer", 3600))
        status, body, _ = self.mcp(issued["access_token"])
        self.assertFalse(body["result"]["isError"])
        status, lines, _ = self.request("GET", "/api/audit/Task/documentation", token=issued["access_token"])
        self.assertIn("maya via claude-ai", lines)

        status, listed, _ = self.request("GET", "/api/access?actor=maya", token=key)
        self.assertEqual([(grant["actor"], grant["agent"]) for grant in listed["grants"]], [("maya", "claude-ai")])
        self.assertNotIn(issued["access_token"], json.dumps(listed))

        status, revoked, _ = self.request("DELETE", "/api/access", {"actor": "maya", "agent": "claude-ai"}, key)
        self.assertEqual(revoked["revoked"], 1)
        status, _, _ = self.mcp(issued["access_token"])
        self.assertEqual(status, 401)

    def test_issuing_is_refused_for_someone_not_in_the_world_or_a_bad_name(self):
        self.start()
        key = self.access.issuer_key(create=True)
        status, body, _ = self.request("POST", "/api/access", {"actor": "nobody", "agent": "claude"}, key)
        self.assertEqual((status, body["error"]["code"]), (422, "unknown_actor"))
        status, body, _ = self.request("POST", "/api/access", {"actor": "maya", "agent": "Claude Desktop"}, key)
        self.assertEqual((status, body["error"]["code"]), (422, "invalid_access"))
        status, body, _ = self.request("POST", "/api/access", {"actor": "maya", "agent": "claude", "expires_in": 5}, key)
        self.assertEqual(status, 422)

    def test_an_expired_token_stops_working(self):
        self.start()
        token = self.access.issue("maya", "claude-ai", 60)
        self.assertEqual(self.access.identify(token), ("maya", "claude-ai"))
        with mock.patch("world.server.time.time", return_value=time.time() + 61):
            self.assertEqual(self.access.identify(token), (None, None))
            status, _, _ = self.mcp(token)
            self.assertEqual(status, 401)

    def test_an_expiring_token_is_new_each_time_and_a_lasting_one_is_reused(self):
        self.start()
        self.assertNotEqual(self.access.issue("maya", "claude-ai", 3600), self.access.issue("maya", "claude-ai", 3600))
        self.assertEqual(self.access.issue("maya", "codex"), self.access.issue("maya", "codex"))
        self.assertEqual(AccessTokens(self.data_dir).identify(self.access.issue("maya", "codex")), ("maya", "codex"))


if __name__ == "__main__":
    unittest.main()
