"""The Model Context Protocol over HTTP, at POST /mcp.

A world's operations as a few general tools, for agents that reach outside services through MCP
connectors rather than a shell. Each tool call goes through the same code as the HTTP API, so it is
checked, recorded, and audited alike, as the token's actor via its agent.

Speaks protocol version 2026-07-28, where every request carries its version in `_meta`, and the
earlier versions that begin with `initialize`. Neither keeps a session: every request stands alone.
"""

import base64
import json
import logging
from urllib.parse import quote, urlencode

from client_gateway.planner import ClientError
from world.store import WorldError


MODERN = ("2026-07-28",)
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "world", "version": "0.1"}
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"

INSTRUCTIONS = (
    "This is a World: records, the rules that govern them, and their history. Call describe first. It shows each "
    "type's fields, its rules as expressions, and what you may do. A refused change says why, so correct it and try "
    "again. Every read and change is recorded in the audit under your person and your agent."
)

_TYPE = {"type": "string", "description": "A record type, as named by describe"}
_ID = {"type": "string", "description": "A record's id"}
_VERSION = {"type": "integer", "description": "The record's current version, from get or query; a stale one is refused"}
_REASON = {"type": "string", "description": "Why, for the history and the audit"}
_FIELDS = lambda description: {"type": "object", "description": description, "additionalProperties": True}


def _tool(name, description, properties, required=(), **annotations):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": description, "inputSchema": schema, "annotations": annotations}


READ = {"readOnlyHint": True, "openWorldHint": False}
TOOLS = [
    _tool("describe", "What this world holds and what you may do: each type's fields, rules, and your permissions. "
          "Call it first. A large world lists only its types' names in type_index; name types to describe them in full.",
          {"types": {"type": "array", "items": {"type": "string"}, "description": "Types to describe in full"}}, **READ),
    _tool("query", "Find records of one type. where is an expression in the language of the rules, over record and actor, "
          "and may follow references: record.instrument.tag == 'X' and record.state != 'done'. "
          "'harbor' in record.name matches part of a text, ignoring case. Without where, every record you may read. "
          "Records named by include come back under included. At most 100 come back, or limit up to 1000; "
          "when more is true, pass next as after for the rest. count: true returns only how many match.",
          {"type": _TYPE, "where": {"type": "string"}, "include": {"type": "array", "items": {"type": "string"}},
           "limit": {"type": "integer"}, "after": {"type": "string"}, "count": {"type": "boolean"}}, ["type"], **READ),
    _tool("get", "One record by type and id.", {"type": _TYPE, "id": _ID}, ["type", "id"], **READ),
    _tool("create", "Create a record.", {"type": _TYPE, "data": _FIELDS("The record's fields"), "reason": _REASON},
          ["type", "data"], readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    _tool("update", "Change some of a record's fields.",
          {"type": _TYPE, "id": _ID, "changes": _FIELDS("Only the fields to change"), "expected_version": _VERSION, "reason": _REASON},
          ["type", "id", "changes", "expected_version"], readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    _tool("delete", "Delete a record.", {"type": _TYPE, "id": _ID, "expected_version": _VERSION, "reason": _REASON},
          ["type", "id", "expected_version"], readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    _tool("change", "Several creates and updates, all saved or none. A step is {\"create\": TYPE, \"data\": {...}, \"as\": NAME} "
          "or {\"update\": TYPE, \"id\": ID, \"changes\": {...}, \"expected_version\": VERSION}. "
          "A later step may write \"$NAME\" for the id of a record created earlier.",
          {"steps": {"type": "array", "items": {"type": "object"}}, "reason": _REASON}, ["steps"],
          readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    _tool("history", "Every version of one record: what changed, when, who, and why.", {"type": _TYPE, "id": _ID}, ["type", "id"], **READ),
    _tool("audit", "Plain-text lines of when, who, and what happened: for the whole world, or for one record with type and id.",
          {"type": _TYPE, "id": _ID, "limit": {"type": "integer", "description": "At most this many lines, newest last; 500 by default"}}, **READ),
]


def _text(arguments, key):
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ClientError(422, "invalid_arguments", f"{key} is required, as text.")
    return quote(value, safe="")


def _pick(arguments, *keys):
    return {key: arguments[key] for key in keys if key in arguments}


def _request(name, arguments):
    """The HTTP operation behind a tool: method, path, query string, and body."""
    if name == "describe":
        types = arguments.get("types")
        if types is not None and (not isinstance(types, list) or not all(isinstance(item, str) for item in types)):
            raise ClientError(422, "invalid_arguments", "types is a list of type names.")
        return "GET", "/api/world", urlencode({"types": ",".join(types)}) if types else "", {}
    if name == "query":
        _text(arguments, "type")
        return "POST", "/api/query", "", _pick(arguments, "type", "where", "include", "limit", "after", "count")
    if name == "change":
        return "POST", "/api/changes", "", _pick(arguments, "steps", "reason")
    if name == "audit":
        if "type" in arguments or "id" in arguments:
            return "GET", f"/api/audit/{_text(arguments, 'type')}/{_text(arguments, 'id')}", urlencode(_pick(arguments, "limit")), {}
        return "GET", "/api/audit", urlencode(_pick(arguments, "limit")), {}
    if name == "create":
        return "POST", f"/api/records/{_text(arguments, 'type')}", "", _pick(arguments, "data", "reason")
    record = f"{_text(arguments, 'type')}/{_text(arguments, 'id')}"
    if name == "get":
        return "GET", f"/api/records/{record}", "", {}
    if name == "history":
        return "GET", f"/api/history/{record}", "", {}
    if name == "update":
        return "PATCH", f"/api/records/{record}", "", _pick(arguments, "changes", "expected_version", "reason")
    return "DELETE", f"/api/records/{record}", "", _pick(arguments, "expected_version", "reason")


def _call(handler, actor_id, name, arguments):
    try:
        method, path, query, body = _request(name, arguments)
        _, value = handler._operate(method, path, query, body, actor_id)
        if name == "describe":
            # The HTTP forms of the operations would mislead here: these tools are the operations.
            value.pop("operations", None)
            if "types" in value and isinstance(value["types"], str):
                value["types"] = "type_index lists every type. Call describe with types to see the fields, rules, and what you may do."
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except (WorldError, ClientError) as error:
        refused = {"error": {"code": error.code, "message": error.message, "details": error.details}}
        return {"content": [{"type": "text", "text": json.dumps(refused, ensure_ascii=False, separators=(",", ":"))}], "isError": True}
    except ValueError:
        return {"content": [{"type": "text", "text": "The arguments are not valid for this tool."}], "isError": True}


def _error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _header_value(value):
    """A header value, decoded if the client sent it in the =?base64?...?= form."""
    if value and value.startswith("=?base64?") and value.endswith("?="):
        return base64.b64decode(value[9:-2], validate=True).decode()
    return value


def handle(handler, method):
    """Answer one request to the MCP endpoint."""
    if method != "POST":
        return handler._respond(405, _error(None, -32600, "Send each JSON-RPC message by POST."), headers={"Allow": "POST"})
    try:
        handler._check_origin()
    except ClientError:
        return handler._respond(403, _error(None, -32600, "Requests from another site are refused."))
    try:
        message = handler._body()
    except ClientError as error:
        return handler._respond(error.status, _error(None, -32700, error.message))
    except (ValueError, UnicodeError, RecursionError):
        return handler._respond(400, _error(None, -32700, "Send one JSON-RPC message as a JSON object."))
    request_id, name, params = message.get("id"), message.get("method"), message.get("params", {})
    if message.get("jsonrpc") != "2.0" or not isinstance(name, str) or not isinstance(params, dict):
        return handler._respond(400, _error(request_id, -32600, "This is not a JSON-RPC 2.0 request."))
    try:
        actor_id = handler._actor()
    except ClientError as error:
        return handler._respond(401, _error(request_id, -32000, error.message), headers={"WWW-Authenticate": 'Bearer realm="world"'})
    if "id" not in message:
        return handler._respond(202, None)  # a notification, such as notifications/initialized

    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version, header = meta.get(VERSION_KEY), handler.headers.get("MCP-Protocol-Version")
    if name == "initialize":
        requested = params.get("protocolVersion")
        return handler._respond(200, {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": requested if requested in LEGACY else LEGACY[0], "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}})
    modern = version is not None or header in MODERN
    supported = {"supported": [*MODERN, *LEGACY], "requested": version or header}
    if modern:
        if version not in MODERN:
            return handler._respond(400, _error(request_id, -32022, "Unsupported protocol version", supported))
        try:
            mismatch = (header != version or handler.headers.get("Mcp-Method") != name
                        or (name == "tools/call" and _header_value(handler.headers.get("Mcp-Name")) != params.get("name")))
        except (ValueError, UnicodeError):
            mismatch = True
        if mismatch:
            return handler._respond(400, _error(request_id, -32020, "Header mismatch: MCP-Protocol-Version, Mcp-Method, and Mcp-Name "
                                                                     "must be present and match the request body."))
    elif header is not None and header not in LEGACY:
        return handler._respond(400, _error(request_id, -32022, "Unsupported protocol version", supported))

    if name == "server/discover":
        result = {"supportedVersions": [*MODERN, *LEGACY], "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS,
                  "ttlMs": 3600000, "cacheScope": "public"}
    elif name == "ping" and not modern:
        result = {}
    elif name == "tools/list":
        result = {"tools": TOOLS, **({"ttlMs": 3600000, "cacheScope": "public"} if modern else {})}
    elif name == "tools/call":
        arguments = params.get("arguments", {})
        if params.get("name") not in {tool["name"] for tool in TOOLS} or not isinstance(arguments, dict):
            return handler._respond(200, _error(request_id, -32602, f"Unknown tool or arguments: {params.get('name')}. tools/list names the tools."))
        try:
            result = _call(handler, actor_id, params["name"], arguments)
        except Exception:
            logging.exception("MCP tool call failed")
            return handler._respond(500, _error(request_id, -32603, "The server could not finish this request."))
    else:
        return handler._respond(404 if modern else 200, _error(request_id, -32601, f"Method not found: {name}"))
    if modern:
        result = {"resultType": "complete", **result, "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}
    return handler._respond(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
