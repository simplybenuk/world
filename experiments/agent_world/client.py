"""A small HTTP client and read/proposal agent for the headless prototype."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from client_gateway.planner import strict_json
from world.schema import WorldError

from .agent import FactAdapter, MeasuredAdapter, run_agent


class HTTPFactAdapter(MeasuredAdapter):
    """Read and propose against a real service. Automatic acceptance is disabled."""

    mode = "facts"
    read_operations = FactAdapter.read_operations
    contract = {**deepcopy(FactAdapter.contract), "transport": "authenticated headless HTTP"}

    def __init__(self, url, credential, actor="ben", *, timeout=30):
        split = urlsplit(url)
        if split.scheme not in {"http", "https"} or not split.hostname or split.username or split.password or split.query or split.fragment:
            raise ValueError("Use a plain http or https service URL without credentials, query or fragment.")
        if not isinstance(credential, str) or not credential or len(credential) > 200:
            raise ValueError("Use the private bearer credential issued by this prototype.")
        self.url = url.rstrip("/")
        self.credential = credential.removeprefix("Bearer ")
        self.timeout = timeout
        store = SimpleNamespace(definition=SimpleNamespace(types={}))
        super().__init__(store, actor, disposable=False)

    def _http(self, method, path, body=None):
        headers = {"Authorization": "Bearer " + self.credential}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        except (URLError, TimeoutError) as error:
            raise WorldError(503, "service_unavailable", "The headless service could not be reached.") from error
        with response:
            value = strict_json(response.read().decode())
            if response.status >= 400:
                error = value.get("error", {})
                raise WorldError(response.status, error.get("code", "service_error"),
                                 error.get("message", "The service rejected the request."), error.get("details"))
        return value

    def dispatch(self, operation, args):
        if operation == "describe":
            description = self._http("GET", "/api/world")
            self.store.definition.types = description["types"]
            self.actor = description["actor"]["id"]
            return description
        if operation == "select":
            return self._http("POST", "/api/select", args)
        if operation == "facts":
            path = "/api/facts/" + quote(args["type"], safe="") + "/" + quote(args["id"], safe="")
            if args.get("as_of") is not None:
                path += "?" + urlencode({"as_of": args["as_of"]})
            return self._http("GET", path)
        if operation == "history":
            path = "/api/history/" + quote(args["type"], safe="") + "/" + quote(args["id"], safe="")
            return self._http("GET", path)["events"]
        if operation in {"list", "list_records"}:
            path = "/api/records"
            if args.get("type") is not None:
                path += "?" + urlencode({"type": args["type"]})
            return self._http("GET", path)["records"]
        if operation == "query":
            return self._http("POST", "/api/query", args)["records"]
        if operation == "get":
            path = "/api/records/" + quote(args["type"], safe="") + "/" + quote(args["id"], safe="")
            return self._http("GET", path)["record"]
        if operation == "transact":
            return self._http("POST", "/api/transact", args)
        raise WorldError(400, "unsupported_operation", "Use a declared read operation or an explicit transaction.")


def main():
    parser = argparse.ArgumentParser(description="Read the headless World through HTTP, or ask an agent for an answer/proposal.")
    parser.add_argument("command", choices=["describe", "select", "facts", "transact", "agent"])
    parser.add_argument("--url", default="http://127.0.0.1:8789")
    parser.add_argument("--data-dir", type=Path, default=Path(".world/agent-world"))
    parser.add_argument("--actor", default="ben")
    parser.add_argument("--query", default="northstar_pipeline", help="Named request in queries.json")
    parser.add_argument("--request-file", type=Path, help="JSON select request or explicitly accepted transaction")
    parser.add_argument("--request", help="Human request for the bounded agent; produces reads and proposals")
    parser.add_argument("--type", default="Opportunity")
    parser.add_argument("--id", default="northstar-renewal")
    parser.add_argument("--as-of", type=int)
    parser.add_argument("--output", type=Path, help="Optional local result file; contains fictional records, never credentials")
    args = parser.parse_args()
    try:
        tokens = strict_json((args.data_dir.expanduser() / "access.json").read_text())
        if args.actor not in tokens:
            parser.error("The prototype has no credential for that actor.")
        adapter = HTTPFactAdapter(args.url, tokens[args.actor], args.actor)
        if args.command == "agent":
            if not args.request or len(args.request) > 4000:
                parser.error("agent requires --request containing up to 4,000 characters.")
            result = run_agent(adapter, args.request, apply=False)
        elif args.command == "describe":
            result = adapter.call("describe")
        elif args.command == "facts":
            result = adapter.call("facts", {"type": args.type, "id": args.id, "as_of": args.as_of})
        else:
            if args.request_file:
                request = strict_json(args.request_file.read_text())
            elif args.command == "select":
                queries = strict_json(Path(__file__).with_name("queries.json").read_text())
                if args.query not in queries:
                    parser.error("Unknown saved query.")
                request = queries[args.query]
            else:
                parser.error("transact requires --request-file with an explicitly accepted transaction.")
            if args.command == "select" and args.as_of is not None:
                request = {**request, "as_of": args.as_of}
            result = adapter.call(args.command, request)
    except (OSError, ValueError, WorldError) as error:
        parser.error(str(error))
    output = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        print(str(args.output))
    else:
        print(output, end="")
    if "error" in result or args.command == "agent" and result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
