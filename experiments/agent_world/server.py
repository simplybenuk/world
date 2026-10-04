"""Authenticated headless HTTP transport for the disposable fact World prototype."""

import argparse
from http.server import ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from client_gateway.planner import ClientError
from world.definition import WorldDefinition
from world.schema import WorldError
from world.server import AccessTokens, WorldHandler

from .domain import DEFAULT_DEFINITION, describe
from .store import FactWorldStore


class AgentWorldHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, access):
        self.store = store
        self.access = access
        self.session_cookie = "world_agent_experiment"
        super().__init__(address, AgentWorldHandler)


class AgentWorldHandler(WorldHandler):
    """Reuse credential/body handling, with no browser assets or model gateway."""

    server_version = "WorldAgentPrototype/0.1"

    def _dispatch(self, method):
        try:
            self.connection.settimeout(30)
            split = urlsplit(self.path)
            path = split.path
            if method == "GET" and path in {"/", "/health"}:
                return self._respond(200, {
                    "service": "World agent record experiment",
                    "status": "ok",
                    "prototype": True,
                    "world": self.server.store.definition.name,
                    "interface": "Connect an agent to the authenticated JSON protocol.",
                    "discovery": "/api/world",
                })
            if path.startswith(("/api/session", "/api/records", "/api/query")):
                return super()._dispatch(method)
            actor_id = self._actor()
            store = self.server.store
            if method == "GET" and path == "/api/world":
                return self._respond(200, describe(store, actor_id))
            if method == "POST" and path in {"/api/select", "/api/transact"}:
                self._check_origin()
                body = self._body()
                if path == "/api/select":
                    return self._respond(200, store.select(actor_id, body))
                if set(body) - {"operations", "reason", "evidence"}:
                    raise ClientError(400, "invalid_transaction", "Use operations, reason and evidence for a transaction.")
                return self._respond(200, store.transact(
                    actor_id, body.get("operations"), self._provenance(actor_id, body),
                ))
            parts = [unquote(part) for part in path.strip("/").split("/")]
            if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "facts"]:
                query = parse_qs(split.query)
                if set(query) - {"as_of"} or len(query.get("as_of", [])) > 1:
                    raise ClientError(400, "invalid_snapshot", "Use one integer as_of snapshot.")
                as_of = None
                if "as_of" in query:
                    try:
                        as_of = int(query["as_of"][0])
                    except ValueError:
                        raise ClientError(400, "invalid_snapshot", "as_of must be an integer snapshot.") from None
                return self._respond(200, store.facts(actor_id, parts[2], parts[3], as_of))
            if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "history"]:
                return self._respond(200, {"events": store.history(actor_id, parts[2], parts[3])})
            raise ClientError(404, "not_found", "This headless operation does not exist.")
        except (WorldError, ClientError) as error:
            self._respond(error.status, {"error": {
                "code": error.code, "message": error.message, "details": error.details,
            }})
        except (ValueError, UnicodeError, RecursionError):
            self._respond(400, {"error": {"code": "invalid_request", "message": "The request must contain valid JSON."}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("Agent World request failed")
            self._respond(500, {"error": {"code": "internal_error", "message": "The record service could not finish the request."}})


def main():
    parser = argparse.ArgumentParser(description="Run the third, headless World prototype on disposable CRM data.")
    parser.add_argument("command", nargs="?", choices=["serve", "access"], default="serve")
    parser.add_argument("--world", type=Path, default=DEFAULT_DEFINITION)
    parser.add_argument("--data-dir", type=Path, default=Path(".world/agent-world"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--actor", default="ben")
    parser.add_argument("--url", help="Base URL to print with the access credential")
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data_dir, 0o700)
    try:
        definition = WorldDefinition.load(args.world.expanduser())
        store = FactWorldStore(data_dir / "facts.sqlite3", definition)
        os.chmod(store.path, 0o600)
    except (WorldError, OSError, ValueError) as error:
        parser.error(str(error))
    access = AccessTokens(data_dir, definition.actor_ids)
    try:
        if args.command == "access":
            if args.actor not in access.tokens:
                parser.error("Unknown actor. Choose " + ", ".join(sorted(access.tokens)) + ".")
            print(json.dumps({
                "url": (args.url or f"http://{args.host}:{args.port}").rstrip("/"),
                "actor": args.actor,
                "authorization": "Bearer " + access.tokens[args.actor],
            }, indent=2))
            return
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        server = AgentWorldHTTPServer((args.host, args.port), store, access)
        logging.info("Headless World prototype listening on %s:%s", args.host, args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        store.close()


if __name__ == "__main__":
    main()
