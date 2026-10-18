"""World provides no model: the reference client's planner runs only when whoever starts the server asks."""

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent


class NoModelTests(unittest.TestCase):
    def serve(self, *flags, env=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {key: value for key, value in os.environ.items() if key != "WORLD_CODEX_BIN"}
        process = subprocess.Popen([sys.executable, "-m", "world.server", "--data-dir", directory.name, "--port", str(port), *flags],
                                   cwd=ROOT, env={**environment, **(env or {})}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.terminate)
        token = None
        for _ in range(100):
            try:
                health = self.request(port, "GET", "/health")[1]
                token = json.loads((Path(directory.name) / "access.json").read_text())["ben"]
                return port, health, token
            except (OSError, ValueError):
                time.sleep(0.05)
        self.fail("the world did not start")

    def request(self, port, method, path, body=None, token=None):
        connection = HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})}
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        answer = json.loads(response.read())
        connection.close()
        return response.status, answer

    def test_no_model_unless_asked(self):
        port, health, token = self.serve()
        self.assertEqual((health["provider"], health["provider_available"]), (None, False))
        status, answer = self.request(port, "POST", "/client/intent", {"message": "show my tasks"}, token)
        self.assertEqual((status, answer["error"]["code"]), (503, "provider_unavailable"))
        self.assertIn("/mcp", answer["error"]["message"])

    def test_asking_with_the_flag_or_the_variable(self):
        self.assertEqual(self.serve("--codex")[1]["provider"], "codex")
        health = self.serve(env={"WORLD_CODEX_BIN": sys.executable})[1]
        self.assertEqual((health["provider"], health["provider_available"]), ("codex", True))


if __name__ == "__main__":
    unittest.main()
