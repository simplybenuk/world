"""HTTP record protocol and a separately implemented disposable client gateway."""

import argparse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from client_gateway.planner import ClientError, CodexPlanner, strict_json
from world import mcp
from world.store import WorldError, WorldStore
import worlds


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = Path(os.environ.get("WORLD_DATA_DIR", "~/.local/share/world")).expanduser()


# How to reach the record operations over HTTP. Part of the world's description,
# so that a client with nothing but a URL and a token does not have to guess.
OPERATIONS = {
    "list": "GET /api/records?type=TYPE, at most 100; when more is true, add &after=NEXT",
    "query": 'POST /api/query {"type": TYPE, "where": EXPRESSION, "include": [REFERENCE_FIELD]}. EXPRESSION is in the language of the rules, '
             "over record and actor, and may follow references: record.instrument.tag == 'X' and record.state != 'done'. "
             "'harbor' in record.name matches part of a text, ignoring case. "
             "Records named by include come back under included. At most 100 records come back, or limit up to 1000. "
             'When more is true, send next as "after" for the rest. "count": true returns only how many match',
    "get": "GET /api/records/TYPE/ID",
    "create": 'POST /api/records/TYPE {"data": {FIELD: VALUE}, "reason": WHY}',
    "update": 'PATCH /api/records/TYPE/ID {"changes": {FIELD: VALUE}, "expected_version": VERSION, "reason": WHY}',
    "delete": 'DELETE /api/records/TYPE/ID {"expected_version": VERSION, "reason": WHY}',
    "several changes at once": 'POST /api/changes {"steps": [{"create": TYPE, "data": {...}, "as": NAME}, '
                               '{"update": TYPE, "id": ID, "changes": {...}, "expected_version": VERSION}], "reason": WHY}. '
                               'All steps are saved or none. A later step may write "$NAME" for the ID of a record created earlier',
    "history": "GET /api/history/TYPE/ID",
    "audit": "GET /api/audit or GET /api/audit/TYPE/ID, plain text lines of when, who, and what happened",
}
# Shown only to those who may change the definition, so that others pay nothing to read it.
DEFINING = {
    "definition": "GET /api/definition, the definition as data with its revision",
    "change the definition": 'POST /api/definition {"types": {TYPE: {...the whole type...} or null}, "rename": {"Type.old": "new"}, '
                             '"values": {"Type.field": {"old": "new"}}, "expected_revision": N, "reason": WHY, "check": true}. '
                             "Refused, naming the records, if any record would not fit",
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

        # Access a person has given to software acting for them, and the tokens it holds. A grant lasts until it is
        # revoked; its tokens may expire and be renewed under it. In agent-access.json:
        # {"grants": [[actor, agent, given_at or None]], "tokens": {token: [actor, agent, expires_at or None]}}
        self.agent_path = Path(data_dir) / "agent-access.json"
        self.given, self.agents, self._agents_read = {}, {}, None
        self._lock = threading.Lock()
        self._reload_agents()

        # The key an authorization server presents to issue and revoke agent tokens. See issuer_key().
        self.issuer_path = Path(data_dir) / "issuer-key"

    def _reload_agents(self):
        """Read the grants and tokens again if the file changed, so access given while the world runs works at once."""
        try:
            changed = self.agent_path.stat().st_mtime_ns
        except FileNotFoundError:
            return
        if changed != self._agents_read:
            saved = json.loads(self.agent_path.read_text())
            if "tokens" not in saved:
                # Before v0.1.1 the file held only tokens, and a grant lasted while one was unexpired.
                tokens = {token: [*issued, None][:3] for token, issued in saved.items()}
                now = time.time()
                given = {(actor, agent): None for actor, agent, expires_at in tokens.values() if expires_at is None or expires_at > now}
            else:
                tokens = saved["tokens"]
                given = {(actor, agent): given_at for actor, agent, given_at in saved["grants"]}
            self.agents, self.given, self._agents_read = tokens, given, changed

    def _save_agents(self):
        now = time.time()
        self.agents = {token: issued for token, issued in self.agents.items()
                       if (issued[2] is None or issued[2] > now) and tuple(issued[:2]) in self.given}
        saved = {"grants": [[actor, agent, given_at] for (actor, agent), given_at in sorted(self.given.items())],
                 "tokens": self.agents}
        partial = self.agent_path.with_name(self.agent_path.name + ".partial")
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(saved, stream)
            stream.write("\n")
        os.replace(partial, self.agent_path)  # a reader in another process never sees half a file
        self._agents_read = self.agent_path.stat().st_mtime_ns

    def identify(self, token):
        """The actor a token signs in, and the agent it was issued to, if any."""
        if not isinstance(token, str) or len(token) > 200 or not token.isascii():
            return None, None
        for actor, credential in self.tokens.items():
            if hmac.compare_digest(token, credential):
                return actor, None
        self._reload_agents()
        now = time.time()
        for credential, (actor, agent, expires_at) in list(self.agents.items()):
            if (hmac.compare_digest(token, credential) and (expires_at is None or expires_at > now)
                    and (actor, agent) in self.given):
                return actor, agent
        return None, None

    def actor_for(self, token):
        return self.identify(token)[0]

    def issue(self, actor, agent, expires_in=None, *, seeded=True):
        """A token that acts as this actor and is recorded as this agent, giving the agent access if it had none."""
        return self.give(actor, agent, expires_in, seeded=seeded)[0]

    def give(self, actor, agent, expires_in=None, *, seeded=True, renew=False):
        """(token, new): a token for this agent to act as this actor, and whether this gave it access it did not have.

        Without expires_in, reissuing returns the same token. With it, every call gives a new token that
        stops working after that many seconds, as an OAuth access token does. Access outlasts its tokens:
        it is given once and ends only when revoked. With renew=True, a token is issued only under access
        already given, so renewing cannot give back access that was revoked. seeded=False skips the check
        against the seeded actors, for a caller that has checked the world itself.
        """
        if seeded and actor not in self.tokens:
            raise ValueError(f"Unknown actor. Choose one of: {', '.join(self.tokens)}.")
        if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", agent):
            raise ValueError("An agent name is up to 64 letters, digits, dots, hyphens, or underscores.")
        if expires_in is not None and (type(expires_in) is not int or not 60 <= expires_in <= 366 * 86400):
            raise ValueError("expires_in is a whole number of seconds from 60 to a year.")
        with self._lock:
            self._reload_agents()  # another process may have issued tokens since
            new = (actor, agent) not in self.given
            if new and renew:
                raise LookupError("This agent has no access to renew. It was revoked or never given.")
            if new:
                self.given[(actor, agent)] = round(time.time())
            elif expires_in is None:
                for token, issued in self.agents.items():
                    if issued == [actor, agent, None]:
                        return token, False
            token = secrets.token_urlsafe(32)
            self.agents[token] = [actor, agent, None if expires_in is None else round(time.time() + expires_in)]
            self._save_agents()
            return token, new

    def has_grant(self, actor, agent):
        """Whether this agent has been given access to act for this actor and it has not been revoked."""
        self._reload_agents()
        return (actor, agent) in self.given

    def revoke(self, token=None, actor=None, agent=None):
        """Stop one token, or end this actor's agent's access and every token it holds.

        Returns (working tokens stopped, the [actor, agent] of each access ended). Stopping one token ends no access.
        """
        with self._lock:
            self._reload_agents()
            stopped = [credential for credential, issued in self.agents.items()
                       if (token is not None and hmac.compare_digest(credential, token))
                       or (token is None and issued[:2] == [actor, agent])]
            now = time.time()
            working = 0
            for credential in stopped:
                expires_at = self.agents.pop(credential)[2]
                working += expires_at is None or expires_at > now
            ended = [[actor, agent]] if token is None and self.given.pop((actor, agent), False) is not False else []
            if stopped or ended:
                self._save_agents()
            return working, ended

    def grants(self, actor=None):
        """Who has given which agents access, when, and until when its tokens work; never the tokens themselves.

        expires_at is when the last of its tokens stops working, None if one never does, and tokens how many work now.
        Access with no working token still stands, until it is revoked.
        """
        self._reload_agents()
        now = time.time()
        found = []
        for (issued_actor, agent), given_at in sorted(self.given.items()):
            if actor is not None and issued_actor != actor:
                continue
            live = [expires_at for token_actor, token_agent, expires_at in self.agents.values()
                    if (token_actor, token_agent) == (issued_actor, agent) and (expires_at is None or expires_at > now)]
            found.append({"actor": issued_actor, "agent": agent, "given_at": given_at, "tokens": len(live),
                          "expires_at": None if not live or None in live else max(live)})
        return found

    def issuer_key(self, create=False):
        """The authorization server's key, made on first request with create=True; None if there is none."""
        if create and not self.issuer_path.exists():
            descriptor = os.open(self.issuer_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(32) + "\n")
        return self.issuer_path.read_text().strip() if self.issuer_path.exists() else None


class WorldHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256  # the default of 5 resets connections when many clients arrive at once

    def __init__(self, address, store, access, planner=None, meter=None, public_url=None, authorization_server=None):
        self.store = store
        # Where this world is reached, and where an MCP client signs in for a token (OAuth). Both or neither.
        self.public_url = public_url.rstrip("/") if public_url else None
        self.authorization_server = authorization_server
        self.access = access
        # The reference client's planner runs a model on this host, on its account. World provides no model,
        # so there is none unless whoever starts the server asks for it (main(): --codex or WORLD_CODEX_BIN).
        self.planner = planner
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
        self.agent = None
        try:
            self.connection.settimeout(150)
            split = urlsplit(self.path)
            path = split.path
            if method == "GET" and path in {"/", "/index.html", "/app.js", "/styles.css"}:
                return self._static(path)
            if method == "GET" and path == "/health":
                planner = self.server.planner
                return self._respond(200, {"status": "ok", "provider": "codex" if planner else None,
                                           "provider_available": bool(planner and planner.available),
                                           "world": self.server.store.definition.title, "protocol_version": "0.1"})
            if method == "GET" and path in {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"}:
                return self._resource_metadata()
            if path == "/mcp":
                return mcp.handle(self, method)
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
            if path == "/api/access":
                return self._access(method, split.query, body)
            if path == "/api/members":
                return self._members(method, body)
            actor_id = self._actor()
            store = self.server.store
            answer = self._operate(method, path, split.query, body, actor_id)
            if answer is not None:
                status, value = answer
                return self._text(status, value) if isinstance(value, str) else self._respond(status, value)
            if path == "/client/intent" and method == "POST":
                if self.server.planner is None:
                    raise ClientError(503, "provider_unavailable", "This world runs no model. Record browsing and editing still work, "
                                      "and agents connect at /mcp with their own.")
                # These records leave the service for a model, so the audit names the gateway.
                result = self.server.planner.plan(store.describe(actor_id), store.list_records(actor_id, agent=self.agent or "client-gateway"),
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

    def _operate(self, method, path, query, body, actor_id):
        """One record operation as (status, value), where text is a str; None when no operation has this path.
        The HTTP API and the MCP tools both come here, so they behave and audit alike."""
        store = self.server.store
        if path == "/api/session" and method == "GET":
            return (200, {"actor": store.get_record(actor_id, store.definition.actor_type, actor_id, agent=self.agent)})
        if path == "/api/world" and method == "GET":
            acting = {"agent": self.agent} if self.agent else {}
            named = parse_qs(query).get("types", [None])[0]
            description = store.describe(actor_id, named.split(",") if named else None)
            if "type_index" in description:
                acting["types"] = ("type_index lists every type. GET /api/world?types=A,B gives the fields, rules, "
                                   "and what you may do for the types you name.")
            defining = DEFINING if description["definition"]["capabilities"]["define"] else {}
            return (200, {**description, **acting, "operations": {**OPERATIONS, **defining}})
        if path == "/api/records" and method == "GET":
            query = parse_qs(query)
            type_name, limit = query.get("type", [None])[0], query.get("limit", ["100"])[0]
            if type_name is None:
                return (200, {"records": store.list_records(actor_id, agent=self.agent)})
            found = store.find(actor_id, type_name, limit=int(limit) if limit.isdecimal() else 0,
                               after=query.get("after", [None])[0], agent=self.agent)
            return (200, self._page(found, None))
        if path == "/api/query" and method == "POST":
            found = store.find(actor_id, body.get("type"), where=body.get("where"), filters=body.get("filters", []),
                               include=body.get("include"), limit=body.get("limit", 100), after=body.get("after"),
                               count=body.get("count") is True, agent=self.agent)
            return (200, self._page(found, body.get("include")))
        if path == "/api/definition" and method == "GET":
            revision = parse_qs(query).get("revision", [None])[0]
            return (200, store.definition_at(actor_id, int(revision) if revision and revision.isdecimal() else revision))
        if path == "/api/definition" and method == "POST":
            return (200, store.define(actor_id, body, agent=self.agent))
        if path == "/api/changes" and method == "POST":
            return (200, {"records": store.apply(actor_id, body.get("steps"), self._provenance(actor_id, body))})
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if len(parts) == 3 and parts[:2] == ["api", "types"] and method == "GET":
            if parts[2] not in store.types:
                raise ClientError(404, "unknown_type", "This record type does not exist.")
            return (200, {"name": parts[2], **store.describe(actor_id, [parts[2]])["types"][parts[2]]})
        if parts[:2] == ["api", "audit"] and len(parts) in (2, 4) and method == "GET":
            limit = parse_qs(query).get("limit", ["500"])[0]
            lines = store.audit(actor_id, *parts[2:], limit=int(limit) if limit.isdecimal() else 0)
            return (200, "".join(line + "\n" for line in lines))
        if len(parts) == 4 and parts[:2] == ["api", "history"] and method == "GET":
            return (200, {"events": store.history(actor_id, parts[2], parts[3], agent=self.agent)})
        if len(parts) == 3 and parts[:2] == ["api", "records"] and method == "POST":
            self._require_object(body, "data")
            record = store.create(actor_id, parts[2], body.get("data"), self._provenance(actor_id, body))
            return (201, {"record": record})
        if len(parts) == 4 and parts[:2] == ["api", "records"]:
            type_name, record_id = parts[2:]
            if method == "GET":
                return (200, {"record": store.get_record(actor_id, type_name, record_id, agent=self.agent)})
            if method == "PATCH":
                self._require_object(body, "changes")
                record = store.update(actor_id, type_name, record_id, body.get("changes"),
                                      body.get("expected_version"), self._provenance(actor_id, body))
                return (200, {"record": record})
            if method == "DELETE":
                store.delete(actor_id, type_name, record_id, body.get("expected_version"), self._provenance(actor_id, body))
                return (200, {"deleted": True, "type": type_name, "id": record_id})
        return None

    def _resource_metadata(self):
        """OAuth protected resource metadata (RFC 9728): where an MCP client signs in for a token to this world."""
        if not (self.server.public_url and self.server.authorization_server):
            raise ClientError(404, "not_found", "This world names no authorization server. Ask its operator for a token.")
        return self._respond(200, {
            "resource": self.server.public_url + "/mcp", "authorization_servers": [self.server.authorization_server],
            "bearer_methods_supported": ["header"], "resource_name": self.server.store.definition.title,
        })

    def challenge(self, error=None):
        """The WWW-Authenticate value for a refused token, naming the metadata when there is an authorization server."""
        value = 'Bearer realm="world"'
        if self.server.public_url and self.server.authorization_server:
            value += f', resource_metadata="{self.server.public_url}/.well-known/oauth-protected-resource/mcp"'
        return value + (f', error="{error}"' if error else "")

    def _require_issuer(self):
        authorization = self.headers.get("Authorization", "")
        key = self.server.access.issuer_key()
        if not key or not authorization.startswith("Bearer ") or not hmac.compare_digest(authorization[7:], key):
            raise ClientError(401, "issuer_required", "Only this world's authorization server may do this, with its issuer key.")

    def _members(self, method, body):
        """For the authorization server only: add someone invited by a person already in the world."""
        self._require_issuer()
        if method != "POST":
            raise ClientError(405, "method_not_allowed", "Use POST.")
        by = body.get("by")
        if not self.server.store.actor_exists(by):
            raise ClientError(422, "unknown_actor", 'Name who invited them with "by", someone already in this world.')
        _, via = self._granter(body, by)
        if not isinstance(body.get("data"), dict):
            raise ClientError(422, "invalid_data", 'Send the new person\'s fields as a JSON object under "data".')
        member = self.server.store.add_member(by, body.get("id"), body["data"], body.get("reason"), by_agent=via)
        return self._respond(201, {"member": member})

    def _access(self, method, query, body):
        """For the authorization server only: issue, list, and revoke the tokens agents act with."""
        self._require_issuer()
        access = self.server.access
        if method == "GET":
            return self._respond(200, {"grants": access.grants(parse_qs(query).get("actor", [None])[0])})
        if method == "POST":
            actor, agent, expires_in = body.get("actor"), body.get("agent"), body.get("expires_in")
            if not self.server.store.actor_exists(actor):
                raise ClientError(422, "unknown_actor", "No one in this world has that id.")
            by, via = self._granter(body, actor)
            try:
                token, new = access.give(actor, agent, expires_in, seeded=False, renew=body.get("renew") is True)
            except ValueError as error:
                raise ClientError(422, "invalid_access", str(error)) from None
            except LookupError as error:
                raise ClientError(409, "no_access", str(error)) from None
            if new:  # renewing is not new access, so it adds no line, however long since the last token
                self.server.store.record_access(by, "gave", actor, agent, "", by_agent=via)
            issued = {"access_token": token, "token_type": "Bearer", "actor": actor, "agent": agent}
            return self._respond(201, {**issued, **({"expires_in": expires_in} if expires_in is not None else {})})
        if method == "DELETE":
            if isinstance(body.get("token"), str):
                by, via = self._granter(body, None)
                stopped, ended = access.revoke(token=body["token"])  # one token; the access stands
            elif isinstance(body.get("actor"), str) and isinstance(body.get("agent"), str):
                by, via = self._granter(body, body["actor"])
                stopped, ended = access.revoke(actor=body["actor"], agent=body["agent"])
            else:
                raise ClientError(422, "invalid_access", 'Name a "token", or an "actor" and an "agent".')
            for actor, agent in ended:
                self.server.store.record_access(by or actor, "revoked", actor, agent, "", by_agent=via)
            return self._respond(200, {"revoked": stopped, "ended": len(ended)})
        raise ClientError(405, "method_not_allowed", "Use GET, POST, or DELETE.")

    def _granter(self, body, actor):
        """Who gave or revoked access, for the audit: "by", or else the person acted for; and "via", the software they used."""
        by, via = body.get("by", actor), body.get("via")
        if by is not None and not self.server.store.actor_exists(by):
            raise ClientError(422, "unknown_actor", "No one in this world has that id.")
        if via is not None and (not isinstance(via, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", via)):
            raise ClientError(422, "invalid_access", "via is the name of the software used, up to 64 letters, digits, dots, hyphens, or underscores.")
        return by, via

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
        actor_id, self.agent = self.server.access.identify(token)
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
        # A browser cannot send a cross-site JSON content type without asking first, so the cookie
        # session needs this check. A request that carries a bearer token does not.
        bearer = self.headers.get("Authorization", "").startswith("Bearer ")
        if not bearer and self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.close_connection = True
            raise ClientError(415, "invalid_content_type", "Use application/json for record operations.")
        value = strict_json(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ClientError(400, "invalid_request", "The request must be a JSON object.")
        return value

    @staticmethod
    def _page(found, include):
        """A query's answer, without the keys that say nothing."""
        if "count" in found:
            return found
        page = {"records": found["records"]}
        if include:
            page["included"] = found["included"]
        if found["more"]:
            page.update({"more": True, "next": found["next"]})
        return page

    @staticmethod
    def _require_object(body, key):
        if not isinstance(body.get(key), dict):
            error = ClientError(422, "invalid_data", f'Send the record\'s fields as a JSON object under "{key}".')
            error.details = {"expected": OPERATIONS["create" if key == "data" else "update"]}
            raise error

    def _provenance(self, actor_id, body):
        reason = body.get("reason", "No reason given.")  # the browser client always sends one; agents may not
        if not isinstance(reason, str) or len(reason) > 4000:
            raise ClientError(400, "invalid_reason", "A change reason must be text of up to 4,000 characters.")
        evidence = body.get("evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 20 or any(not isinstance(item, str) or len(item) > 2000 for item in evidence):
            raise ClientError(400, "invalid_evidence", "Evidence must be a list of up to 20 short text references.")
        return {"requested_by": actor_id, "reason": reason, "evidence": evidence, "agent": self.agent}

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

    def _headers(self, content_type, length, cookie=None, extra=None):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _text(self, status, text):
        data = text.encode()
        if self.server.meter:
            self.server.meter(self.command, self.path, status, len(data))
        self.send_response(status)
        self._headers("text/plain; charset=utf-8", len(data))
        self.wfile.write(data)

    def _respond(self, status, value, cookie=None, headers=None):
        data = b"" if value is None else json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if self.server.meter:
            self.server.meter(self.command, self.path, status, len(data))
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(data), cookie, headers)
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
    parser.add_argument("--world", default=os.environ.get("WORLD_DEFINITION", "team"),
                        help=f"World definition to serve: one of {', '.join(worlds.AVAILABLE)}, or a JSON file. "
                        "Each needs its own data directory")
    parser.add_argument("command", nargs="?", choices=["serve", "access", "issuer"], default="serve",
                        help="serve the world; access prints a token; issuer prints the key an authorization server uses "
                        "to issue agent tokens at /api/access, making it on first use")
    parser.add_argument("--host", default=os.environ.get("WORLD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORLD_PORT", "8787")))
    parser.add_argument("--data-dir", type=Path, help="Defaults to ~/.local/share/world, with a subdirectory for worlds other than team")
    parser.add_argument("--actor", default="ben", help="Actor to sign in as for the access command")
    parser.add_argument("--agent", help="For the access command: issue a token for software acting as the actor. "
                        "The audit records its reads and changes as ACTOR via AGENT")
    parser.add_argument("--url", default=os.environ.get("WORLD_URL"), help="Client base URL for the access command")
    parser.add_argument("--public-url", default=os.environ.get("WORLD_PUBLIC_URL"),
                        help="The URL this world is reached at, such as https://example.com; needed with --authorization-server")
    parser.add_argument("--authorization-server", default=os.environ.get("WORLD_AUTHORIZATION_SERVER"),
                        help="The OAuth authorization server where MCP clients sign in for a token to this world")
    parser.add_argument("--world-from-file", action="store_true",
                        help="Apply the --world definition even if the world's definition was changed while it ran")
    parser.add_argument("--codex", action="store_true",
                        help="Let the reference client ask Codex on this host to plan views, on this host's Codex account. "
                        "Setting WORLD_CODEX_BIN does the same. Off otherwise: World provides no model")
    args = parser.parse_args()
    definition = worlds.load(args.world)
    default_dir = DEFAULT_DATA_DIR if definition.name == "team" else DEFAULT_DATA_DIR / definition.name
    data_dir = (args.data_dir or default_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data_dir, 0o700)
    access = AccessTokens(data_dir, definition.actors)
    if args.command == "issuer":
        print(access.issuer_key(create=True))
        return
    if bool(args.public_url) != bool(args.authorization_server):
        parser.error("--public-url and --authorization-server go together.")
    if args.command == "access":
        if args.actor not in access.tokens:
            parser.error(f"Unknown actor. Choose one of: {', '.join(access.tokens)}.")
        saved_url = data_dir / "client-url"
        base = args.url or (saved_url.read_text().strip() if saved_url.is_file() else f"http://localhost:{args.port}")
        if args.agent:
            new = not access.has_grant(args.actor, args.agent)
            try:
                token = access.issue(args.actor, args.agent)
            except ValueError as error:
                parser.error(str(error))
            if new:
                store = WorldStore(data_dir / "world.sqlite3", definition)
                try:
                    store.record_access(args.actor, "gave", args.actor, args.agent, "at the command line")
                finally:
                    store.close()
            print(token)
            return
        print(base.rstrip("/") + "/#token=" + access.tokens[args.actor])
        return
    try:
        store = WorldStore(data_dir / "world.sqlite3", definition, from_file=args.world_from_file)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    planner = CodexPlanner() if args.codex or os.environ.get("WORLD_CODEX_BIN") else None
    server = WorldHTTPServer((args.host, args.port), store, access, planner, public_url=args.public_url,
                             authorization_server=args.authorization_server)
    logging.info("%s world listening on %s:%s; data in %s", args.world, args.host, args.port, data_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
