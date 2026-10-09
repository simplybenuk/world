"""HTTP record protocol and a separately implemented disposable client gateway."""

import argparse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
from urllib.parse import parse_qs, unquote, urlsplit

from client_gateway.planner import ClientError, CodexPlanner, strict_json
from world.store import WorldError, WorldStore
import worlds


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = Path(os.environ.get("WORLD_DATA_DIR", "~/.local/share/world")).expanduser()


# How to reach the record operations over HTTP. Part of the world's description,
# so that a client with nothing but a URL and a token does not have to guess.
OPERATIONS = {
    "list": "GET /api/records?type=TYPE",
    "query": 'POST /api/query {"type": TYPE, "filters": [{"field": FIELD, "op": OPERATOR, "value": VALUE}]}',
    "get": "GET /api/records/TYPE/ID",
    "create": 'POST /api/records/TYPE {"data": {FIELD: VALUE}, "reason": WHY}',
    "update": 'PATCH /api/records/TYPE/ID {"changes": {FIELD: VALUE}, "expected_version": VERSION, "reason": WHY}',
    "delete": 'DELETE /api/records/TYPE/ID {"expected_version": VERSION, "reason": WHY}',
    "history": "GET /api/history/TYPE/ID",
    "audit": "GET /api/audit or GET /api/audit/TYPE/ID, plain text lines of when, who, and what happened",
}


class AccessTokens:
    """Server-issued actor credentials; roles always come from the world."""

    def __init__(self, data_dir, actors=("ben", "maya", "alex", "sam")):
        self.path = Path(data_dir) / "access.json"
        if not self.path.exists():
            tokens = {actor: secrets.token_urlsafe(32) for actor in actors}
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(tokens, stream)
                stream.write("\n")
        self.tokens = json.loads(self.path.read_text())
        os.chmod(self.path, 0o600)

    def actor_for(self, token):
        if not isinstance(token, str) or len(token) > 200 or not token.isascii():
            return None
        for actor, credential in self.tokens.items():
            if hmac.compare_digest(token, credential):
                return actor
        return None


class WorldHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, access, planner=None, meter=None):
        self.store = store
        self.access = access
        self.planner = planner or CodexPlanner()
        self.meter = meter  # called with (method, path, status, response bytes) for measurement
        super().__init__(address, WorldHandler)


class WorldHandler(BaseHTTPRequestHandler):
    server_version = "World/0.1"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PUT(self):
        self._respond(405, {"error": {"code": "method_not_allowed", "message": "This method is not supported."}})

    def log_message(self, format, *args):
        # Do not log paths, request bodies, tokens or conversation contents.
        logging.info("%s request finished", self.command)

    def _dispatch(self, method):
        try:
            self.connection.settimeout(150)
            split = urlsplit(self.path)
            path = split.path
            if method == "GET" and path in {"/", "/index.html", "/app.js", "/styles.css"}:
                return self._static(path)
            if method == "GET" and path == "/health":
                return self._respond(200, {"status": "ok", "provider": "codex", "provider_available": self.server.planner.available,
                                           "world": self.server.store.definition.title, "protocol_version": "0.1"})
            body = self._body() if method in {"POST", "PATCH", "DELETE"} else {}
            if method in {"POST", "PATCH", "DELETE"}:
                self._check_origin()
            if path == "/api/session" and method == "POST":
                token = body.get("token")
                actor_id = self.server.access.actor_for(token)
                if not actor_id:
                    raise ClientError(401, "invalid_token", "That sign-in token is not valid.")
                actor = self.server.store.get_record(actor_id, self.server.store.definition.actor_type, actor_id)
                return self._respond(200, {"actor": actor}, cookie=self._cookie(token))
            if path == "/api/session" and method == "DELETE":
                return self._respond(200, {"signed_out": True}, cookie="world_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            actor_id = self._actor()
            store = self.server.store
            if path == "/api/session" and method == "GET":
                return self._respond(200, {"actor": store.get_record(actor_id, store.definition.actor_type, actor_id)})
            if path == "/api/world" and method == "GET":
                return self._respond(200, {**store.describe(actor_id), "operations": OPERATIONS})
            if path == "/api/records" and method == "GET":
                type_name = parse_qs(split.query).get("type", [None])[0]
                return self._respond(200, {"records": store.list_records(actor_id, type_name)})
            if path == "/api/query" and method == "POST":
                return self._respond(200, {"records": store.query(actor_id, body.get("type"), body.get("filters", []))})
            parts = [unquote(part) for part in path.strip("/").split("/")]
            if len(parts) == 3 and parts[:2] == ["api", "types"] and method == "GET":
                description = store.describe(actor_id)
                if parts[2] not in description["types"]:
                    raise ClientError(404, "unknown_type", "This record type does not exist.")
                return self._respond(200, {"name": parts[2], **description["types"][parts[2]]})
            if parts[:2] == ["api", "audit"] and len(parts) in (2, 4) and method == "GET":
                limit = parse_qs(split.query).get("limit", ["500"])[0]
                lines = store.audit(actor_id, *parts[2:], limit=int(limit) if limit.isdecimal() else 0)
                return self._text(200, "".join(line + "\n" for line in lines))
            if len(parts) == 4 and parts[:2] == ["api", "history"] and method == "GET":
                return self._respond(200, {"events": store.history(actor_id, parts[2], parts[3])})
            if len(parts) == 3 and parts[:2] == ["api", "records"] and method == "POST":
                self._require_object(body, "data")
                record = store.create(actor_id, parts[2], body.get("data"), self._provenance(actor_id, body))
                return self._respond(201, {"record": record})
            if len(parts) == 4 and parts[:2] == ["api", "records"]:
                type_name, record_id = parts[2:]
                if method == "GET":
                    return self._respond(200, {"record": store.get_record(actor_id, type_name, record_id)})
                if method == "PATCH":
                    self._require_object(body, "changes")
                    record = store.update(actor_id, type_name, record_id, body.get("changes"),
                                          body.get("expected_version"), self._provenance(actor_id, body))
                    return self._respond(200, {"record": record})
                if method == "DELETE":
                    store.delete(actor_id, type_name, record_id, body.get("expected_version"), self._provenance(actor_id, body))
                    return self._respond(200, {"deleted": True, "type": type_name, "id": record_id})
            if path == "/client/intent" and method == "POST":
                result = self.server.planner.plan(store.describe(actor_id), store.list_records(actor_id),
                                                 body.get("message"), body.get("context"))
                return self._respond(200, result)
            if path == "/client/apply" and method == "POST":
                return self._apply(actor_id, body)
            raise ClientError(404, "not_found", "This operation does not exist. GET /api/world lists the operations.")
        except (WorldError, ClientError) as error:
            self._respond(error.status, {"error": {"code": error.code, "message": error.message, "details": error.details}})
        except (ValueError, UnicodeError, RecursionError):
            self._respond(400, {"error": {"code": "invalid_request", "message": "The request must contain valid JSON."}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("World request failed")
            self._respond(500, {"error": {"code": "internal_error", "message": "The server could not finish this request."}})

    def _actor(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else None
        if token is None:
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
                token = jar["world_session"].value if "world_session" in jar else None
            except cookies.CookieError:
                token = None
        actor_id = self.server.access.actor_for(token)
        if not actor_id:
            raise ClientError(401, "authentication_required", "Sign in with a server-issued token to open this world.")
        return actor_id

    def _check_origin(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ClientError(403, "cross_origin_request", "Requests must come from this client.")
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            raise ClientError(403, "cross_origin_request", "Requests must come from this client.")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ClientError(400, "invalid_transfer_encoding", "Use a Content-Length header for JSON requests.")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ClientError(400, "invalid_length", "The request length is invalid.") from None
        if length < 0 or length > 128 * 1024:
            self.close_connection = True
            raise ClientError(413, "request_too_large", "The request is too large.")
        if not length:
            return {}
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.close_connection = True
            raise ClientError(415, "invalid_content_type", "Use application/json for record operations.")
        value = strict_json(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ClientError(400, "invalid_request", "The request must be a JSON object.")
        return value

    @staticmethod
    def _require_object(body, key):
        if not isinstance(body.get(key), dict):
            error = ClientError(422, "invalid_data", f'Send the record\'s fields as a JSON object under "{key}".')
            error.details = {"expected": OPERATIONS["create" if key == "data" else "update"]}
            raise error

    @staticmethod
    def _provenance(actor_id, body):
        reason = body.get("reason", "Changed through the World client")
        if not isinstance(reason, str) or len(reason) > 4000:
            raise ClientError(400, "invalid_reason", "A change reason must be text of up to 4,000 characters.")
        evidence = body.get("evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 20 or any(not isinstance(item, str) or len(item) > 2000 for item in evidence):
            raise ClientError(400, "invalid_evidence", "Evidence must be a list of up to 20 short text references.")
        return {"requested_by": actor_id, "reason": reason, "evidence": evidence}

    def _apply(self, actor_id, body):
        proposal = body.get("proposal")
        if not isinstance(proposal, dict):
            raise ClientError(400, "invalid_proposal", "A record change proposal is required.")
        operation, type_name = proposal.get("operation"), proposal.get("type")
        changes = proposal.get("changes")
        provenance = self._provenance(actor_id, {"reason": body.get("reason", proposal.get("reason", "Requested through the conversational client")),
                                                "evidence": body.get("evidence", [])})
        store = self.server.store
        if operation == "create":
            return self._respond(201, {"record": store.create(actor_id, type_name, changes, provenance)})
        if operation == "update":
            record = store.update(actor_id, type_name, proposal.get("id"), changes, proposal.get("expected_version"), provenance)
            return self._respond(200, {"record": record})
        if operation == "delete":
            store.delete(actor_id, type_name, proposal.get("id"), proposal.get("expected_version"), provenance)
            return self._respond(200, {"deleted": True, "type": type_name, "id": proposal.get("id")})
        raise ClientError(400, "invalid_proposal", "This record operation is not supported.")

    def _cookie(self, token):
        value = f"world_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800"
        if urlsplit(self.headers.get("Origin", "")).scheme == "https":
            value += "; Secure"
        return value

    def _headers(self, content_type, length, cookie=None):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _text(self, status, text):
        data = text.encode()
        if self.server.meter:
            self.server.meter(self.command, self.path, status, len(data))
        self.send_response(status)
        self._headers("text/plain; charset=utf-8", len(data))
        self.wfile.write(data)

    def _respond(self, status, value, cookie=None):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if self.server.meter:
            self.server.meter(self.command, self.path, status, len(data))
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(data), cookie)
        self.wfile.write(data)

    def _static(self, path):
        name = "index.html" if path in {"/", "/index.html"} else path[1:]
        location = ROOT / "client" / name
        if not location.is_file():
            raise ClientError(404, "client_unavailable", "The client files have not been installed.")
        content_types = {"index.html": "text/html", "app.js": "text/javascript", "styles.css": "text/css"}
        data = location.read_bytes()
        self.send_response(200)
        self._headers(content_types[name] + "; charset=utf-8", len(data))
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description="Run a World on this server.")
    parser.add_argument("--world", choices=worlds.AVAILABLE, default=os.environ.get("WORLD_DEFINITION", "team"),
                        help="World definition to serve; each needs its own data directory")
    parser.add_argument("command", nargs="?", choices=["serve", "access"], default="serve")
    parser.add_argument("--host", default=os.environ.get("WORLD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORLD_PORT", "8787")))
    parser.add_argument("--data-dir", type=Path, help="Defaults to ~/.local/share/world, with a subdirectory for worlds other than team")
    parser.add_argument("--actor", default="ben", help="Actor to sign in as for the access command")
    parser.add_argument("--url", default=os.environ.get("WORLD_URL"), help="Client base URL for the access command")
    args = parser.parse_args()
    definition = worlds.load(args.world)
    default_dir = DEFAULT_DATA_DIR if args.world == "team" else DEFAULT_DATA_DIR / args.world
    data_dir = (args.data_dir or default_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data_dir, 0o700)
    access = AccessTokens(data_dir, definition.actors)
    if args.command == "access":
        if args.actor not in access.tokens:
            parser.error(f"Unknown actor. Choose one of: {', '.join(access.tokens)}.")
        saved_url = data_dir / "client-url"
        base = args.url or (saved_url.read_text().strip() if saved_url.is_file() else f"http://localhost:{args.port}")
        print(base.rstrip("/") + "/#token=" + access.tokens[args.actor])
        return
    try:
        store = WorldStore(data_dir / "world.sqlite3", definition)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = WorldHTTPServer((args.host, args.port), store, access)
    logging.info("%s world listening on %s:%s; data in %s", args.world, args.host, args.port, data_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
