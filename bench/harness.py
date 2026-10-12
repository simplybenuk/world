"""A disposable, metered world server and a plain HTTP client for measurements."""

from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading

from world.server import AccessTokens, WorldHTTPServer
from world.store import WorldStore
import worlds


class NoPlanner:
    """World provides no model, so measurements run without the client gateway."""

    available = False


class Client:
    """Bearer-token JSON requests against one running world."""

    def __init__(self, port, token):
        self.port, self.token = port, token

    def call(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def get(self, path):
        return self.call("GET", path)[1]


class Running:
    def __init__(self, server, store, access):
        self.server, self.store, self.access = server, store, access
        self.requests = []  # (method, path, status, response bytes), in order
        server.meter = lambda *request: self.requests.append(request)
        self.url = f"http://127.0.0.1:{server.server_port}"

    def client(self, actor):
        return Client(self.server.server_port, self.access.tokens[actor])

    def agent_token(self, actor, agent):
        """A token whose reads and changes the audit records as the actor via this agent."""
        return self.access.issue(actor, agent)

    def usage(self):
        return {
            "calls": len(self.requests),
            "rejected": sum(status >= 400 for _, _, status, _ in self.requests),
            "bytes_read": sum(size for _, _, _, size in self.requests),
        }


@contextmanager
def running(world="crm"):
    """A freshly seeded world in a temporary directory, served on a free local port."""
    definition = worlds.load(world)
    with TemporaryDirectory(prefix="world-bench-") as directory:
        store = WorldStore(Path(directory) / "world.sqlite3", definition)
        access = AccessTokens(directory, definition.actors)
        server = WorldHTTPServer(("127.0.0.1", 0), store, access, NoPlanner())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield Running(server, store, access)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            store.close()
