"""How an authorization server gives agents access to a world, and how an MCP client finds it."""

from http.client import HTTPConnection
import subprocess
import sys
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

    def test_giving_and_revoking_access_is_in_the_audit(self):
        self.start()
        key = self.access.issuer_key(create=True)
        for _ in range(2):  # renewing is not new access, so it adds no second line
            self.request("POST", "/api/access", {"actor": "maya", "agent": "claude-ai", "expires_in": 3600, "via": "teamworld"}, key)
        self.request("DELETE", "/api/access", {"actor": "maya", "agent": "claude-ai", "by": "ben", "via": "teamworld"}, key)
        status, lines, _ = self.request("GET", "/api/audit/Person/maya", token=self.access.tokens["maya"])
        access = [line for line in lines.splitlines() if " access " in line]
        self.assertEqual(len(access), 2, lines)
        self.assertRegex(access[0], r"maya via teamworld +gave access +Person maya +claude-ai may act as maya\.$")
        self.assertRegex(access[1], r"ben via teamworld +revoked access +Person maya +claude-ai may no longer act as maya\.$")
        status, lines, _ = self.request("GET", "/api/audit", token=self.access.tokens["maya"])
        self.assertIn("gave access", lines)
        status, body, _ = self.request("POST", "/api/access", {"actor": "maya", "agent": "claude-ai", "by": "nobody"}, key)
        self.assertEqual(status, 422)
        self.assertFalse(self.access.has_grant("maya", "claude-ai"))

    def access_lines(self, actor="maya"):
        status, lines, _ = self.request("GET", f"/api/audit/Person/{actor}", token=self.access.tokens[actor])
        return [line for line in lines.splitlines() if " access " in line]

    def test_access_outlasts_its_tokens(self):
        self.start()
        key = self.access.issuer_key(create=True)
        body = {"actor": "maya", "agent": "claude-ai", "expires_in": 3600, "via": "teamworld"}
        status, first, _ = self.request("POST", "/api/access", body, key)
        later = time.time() + 2 * 86400  # every token has expired over the weekend
        with mock.patch("world.server.time.time", return_value=later):
            status, listed, _ = self.request("GET", "/api/access?actor=maya", token=key)
            self.assertEqual([(grant["agent"], grant["tokens"]) for grant in listed["grants"]], [("claude-ai", 0)])
            status, renewed, _ = self.request("POST", "/api/access", {**body, "renew": True}, key)
            self.assertEqual(status, 201)
            self.assertEqual(self.access.identify(renewed["access_token"]), ("maya", "claude-ai"))
            self.assertEqual(self.access.identify(first["access_token"]), (None, None))
        self.assertEqual(len(self.access_lines()), 1)  # renewing on Monday is not access given on Monday

    def test_renewing_cannot_give_back_revoked_access(self):
        self.start()
        key = self.access.issuer_key(create=True)
        body = {"actor": "maya", "agent": "claude-ai", "expires_in": 3600, "via": "teamworld"}
        status, _, _ = self.request("POST", "/api/access", {**body, "renew": True}, key)
        self.assertEqual(status, 409)  # never given
        self.request("POST", "/api/access", body, key)
        self.request("DELETE", "/api/access", {"actor": "maya", "agent": "claude-ai", "via": "teamworld"}, key)
        status, refused, _ = self.request("POST", "/api/access", {**body, "renew": True}, key)
        self.assertEqual((status, refused["error"]["code"]), (409, "no_access"))
        self.assertEqual(len(self.access_lines()), 2)  # gave and revoked, and nothing for the refused renewals

    def test_revoking_access_with_no_working_token_is_in_the_audit(self):
        self.start()
        key = self.access.issuer_key(create=True)
        self.request("POST", "/api/access", {"actor": "maya", "agent": "claude-ai", "expires_in": 60}, key)
        with mock.patch("world.server.time.time", return_value=time.time() + 120):
            status, revoked, _ = self.request("DELETE", "/api/access", {"actor": "maya", "agent": "claude-ai"}, key)
        self.assertEqual((revoked["revoked"], revoked["ended"]), (0, 1))
        self.assertRegex(self.access_lines()[-1], r"revoked access +Person maya +claude-ai may no longer act as maya\.$")

    def test_stopping_one_token_leaves_the_access(self):
        self.start()
        key = self.access.issuer_key(create=True)
        body = {"actor": "maya", "agent": "claude-ai", "expires_in": 3600}
        first = self.request("POST", "/api/access", body, key)[1]["access_token"]
        second = self.request("POST", "/api/access", body, key)[1]["access_token"]
        status, revoked, _ = self.request("DELETE", "/api/access", {"token": first}, key)
        self.assertEqual((revoked["revoked"], revoked["ended"]), (1, 0))
        self.assertEqual(self.access.identify(first), (None, None))
        self.assertEqual(self.access.identify(second), ("maya", "claude-ai"))
        self.assertTrue(self.access.has_grant("maya", "claude-ai"))
        self.assertEqual(len(self.access_lines()), 1)

    def test_access_is_only_for_someone_in_the_world_whoever_gives_it(self):
        self.start()
        key = self.access.issuer_key(create=True)
        status, body, _ = self.request("POST", "/api/access", {"actor": "nobody", "agent": "claude", "by": "maya"}, key)
        self.assertEqual((status, body["error"]["code"]), (422, "unknown_actor"))
        self.assertFalse(self.access.has_grant("nobody", "claude"))

    def test_the_audit_keeps_access_and_views_in_the_order_they_happened(self):
        self.start()
        key = self.access.issuer_key(create=True)
        token = self.request("POST", "/api/access", {"actor": "maya", "agent": "claude-ai", "expires_in": 3600}, key)[1]["access_token"]
        time.sleep(0.002)
        self.request("GET", "/api/records/Person/maya", token=token)
        lines = self.request("GET", "/api/audit/Person/maya", token=self.access.tokens["maya"])[1].splitlines()
        gave = next(index for index, line in enumerate(lines) if "gave access" in line)
        viewed = next(index for index, line in enumerate(lines) if "maya via claude-ai  viewed" in line)
        self.assertLess(gave, viewed, lines)

    def test_tokens_saved_before_grants_still_work(self):
        self.start()
        token, expired = "t" * 43, "e" * 43
        (self.data_dir / "agent-access.json").write_text(json.dumps({
            token: ["maya", "claude-ai", round(time.time() + 3600)], expired: ["ben", "codex", round(time.time() - 10)]}))
        access = AccessTokens(self.data_dir)
        self.assertEqual(access.identify(token), ("maya", "claude-ai"))
        self.assertTrue(access.has_grant("maya", "claude-ai"))
        self.assertFalse(access.has_grant("ben", "codex"))  # it had lapsed under the old rule
        access.issue("maya", "claude-ai", 3600)
        saved = json.loads((self.data_dir / "agent-access.json").read_text())
        self.assertEqual(saved["grants"], [["maya", "claude-ai", None]])
        self.assertIn(token, saved["tokens"])

    def test_access_given_at_the_command_line_is_in_the_audit(self):
        self.start()
        command = [sys.executable, "-m", "world.server", "access", "--data-dir", str(self.data_dir), "--actor", "ben", "--agent", "codex"]
        first = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
        again = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(first, again)
        lines = [line for line in self.store.audit("ben", "Person", "ben") if "gave access" in line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("codex may act as ben at the command line."))

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
