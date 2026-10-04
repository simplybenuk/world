"""Prototype headless agent. Codex proposes JSON commands; Python executes reads.

The model has no shell, browser, file, or CRM credentials. Every model turn gets
the same explicit discovery and transcript format for either record contract.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

from client_gateway.planner import strict_json
from world.schema import WorldError, validate_data

from .domain import describe


CONTRACT_REVISION = "available-reads-v2"


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def public_error(error):
    return {"error": {"status": error.status, "code": error.code,
                      "message": error.message, "details": error.details}}


class MeasuredAdapter:
    """Count logical service operations, without HTTP or MCP transport overhead."""

    def __init__(self, store, actor="maya", *, disposable=False):
        self.store = store
        self.actor = actor
        self.disposable = disposable
        self.calls = []

    def call(self, operation, arguments=None):
        arguments = {} if arguments is None else arguments
        request = {"operation": operation, "arguments": arguments}
        started = time.perf_counter()
        try:
            response = self.dispatch(operation, arguments)
        except WorldError as error:
            response = public_error(error)
        except (KeyError, TypeError, ValueError) as error:
            response = {"error": {"status": 400, "code": "invalid_arguments", "message": str(error)}}
        elapsed = time.perf_counter() - started
        self.calls.append({"request": deepcopy(request), "response": deepcopy(response),
                           "request_bytes": json_bytes(request), "response_bytes": json_bytes(response),
                           "service_seconds": elapsed})
        return response

    def metrics(self):
        discovery = [call for call in self.calls if call["request"]["operation"] == "describe"]
        return {"logical_service_operations": len(self.calls), "discovery_operations": len(discovery),
                "task_operations": len(self.calls) - len(discovery),
                "request_bytes": sum(call["request_bytes"] for call in self.calls),
                "response_bytes": sum(call["response_bytes"] for call in self.calls),
                "discovery_request_bytes": sum(call["request_bytes"] for call in discovery),
                "discovery_response_bytes": sum(call["response_bytes"] for call in discovery),
                "service_seconds": sum(call["service_seconds"] for call in self.calls)}

    def apply(self, proposals):
        if not self.disposable:
            raise ValueError("Agent apply is restricted to this experiment's disposable databases.")
        if self.mode == "facts":
            operations = [{key: proposal[key] for key in ("type", "id", "changes", "expected_version")}
                          for proposal in proposals]
            return [self.call("transact", {"operations": operations,
                         "provenance": {"reason": "; ".join(p["reason"] for p in proposals)}})] if proposals else []
        # The existing API commits each operation separately. Do not imply batch atomicity.
        return [self.call("update", proposal) for proposal in proposals]


class CrudAdapter(MeasuredAdapter):
    mode = "crud"
    read_operations = {"query", "get", "list", "history"}
    contract = {
        "mode": "crud", "revision": CONTRACT_REVISION, "transport": "in-process prototype adapter",
        "operations": {
            "list": {"arguments": {"type": "optional declared type; omit for all visible records"},
                     "returns": "full authorized current records with data, version and capabilities"},
            "query": {"arguments": {"type": "declared type", "filters": "AND list of {field,op,value}; root fields only"},
                      "returns": "full authorized records with data, version and capabilities"},
            "get": {"arguments": {"type": "declared type", "id": "record ID"}, "returns": "one full authorized record"},
            "history": {"arguments": {"type": "declared type", "id": "record ID"},
                        "returns": "ordered events with before/after, actor, reason and evidence"},
        },
        "notes": ["Discovery record_counts reports visible graph size. A bulk list trades fewer calls for more returned data.",
                  "Cache records and use id/in filters for needed references.",
                  "Queries use separate transactions; there is no cross-call snapshot or atomic batch update.",
                  "History authorization checks current access; it does not promise historical visibility intersection.",
                  "Allowed next stages come from discovery transitions and each record's update capability."],
    }

    def dispatch(self, operation, args):
        if operation == "describe":
            return describe(self.store, self.actor)
        if operation == "query":
            return self.store.query(self.actor, args["type"], args.get("filters", []))
        if operation == "list":
            return self.store.list_records(self.actor, args.get("type"))
        if operation == "get":
            return self.store.get_record(self.actor, args["type"], args["id"])
        if operation == "history":
            return self.store.history(self.actor, args["type"], args["id"])
        if operation == "update":
            return self.store.update(self.actor, args["type"], args["id"], args["changes"],
                                     args["expected_version"], {"reason": args["reason"]})
        raise WorldError(400, "unsupported_operation", "This adapter does not expose that operation.")


class FactAdapter(MeasuredAdapter):
    mode = "facts"
    read_operations = {"select", "facts", "history", "query", "get", "list"}
    contract = {
        "mode": "facts", "revision": CONTRACT_REVISION, "transport": "in-process prototype adapter",
        "operations": {
            "list": deepcopy(CrudAdapter.contract["operations"]["list"]),
            "query": deepcopy(CrudAdapter.contract["operations"]["query"]),
            "get": deepcopy(CrudAdapter.contract["operations"]["get"]),
            "select": {"arguments": {"type": "declared type", "filters": "AND list of {field,op,value}; root fields only",
                       "select": "list of root fields or reference paths, e.g. owner.name; id/version always returned",
                       "include": "optional [{name,type,via,select,order_by?,descending?,limit?}]; via is a reverse reference field",
                       "as_of": "optional integer snapshot from discovery; omit for current", "limit": "optional integer, maximum 100"},
                       "returns": "{snapshot,records:[{type,id,version,values,related,next}]}"},
            "facts": {"arguments": {"type": "declared type", "id": "record ID", "as_of": "optional integer snapshot"},
                      "returns": "accepted assertion/retraction history and transaction provenance"},
            "history": {"arguments": {"type": "declared type", "id": "record ID"},
                        "returns": "ordered ledger-derived events with versioned before/after, actor, reason and evidence; current and historical access enforced"},
        },
        "notes": ["Discovery record_counts reports visible graph size. A bulk list trades fewer calls for more returned data.",
                  "list, query and get are current-record compatibility reads, with restricted references redacted.",
                  "values keys are the requested paths; related[name] contains included records.",
                  "next.stage lists allowed successors when this actor can update the record.",
                  "A select uses one snapshot; historical reads intersect current and historical access.",
                  "Proposed updates can commit in one validated transaction, with expected versions."],
    }

    def dispatch(self, operation, args):
        if operation == "describe":
            return describe(self.store, self.actor)
        if operation == "select":
            return self.store.select(self.actor, args)
        if operation == "query":
            return self.store.query(self.actor, args["type"], args.get("filters", []))
        if operation == "get":
            return self.store.get_record(self.actor, args["type"], args["id"])
        if operation == "list":
            return self.store.list_records(self.actor, args.get("type"))
        if operation == "facts":
            return self.store.facts(self.actor, args["type"], args["id"], as_of=args.get("as_of"))
        if operation == "history":
            return self.store.history(self.actor, args["type"], args["id"])
        if operation == "transact":
            return self.store.transact(self.actor, args["operations"], args.get("provenance"))
        raise WorldError(400, "unsupported_operation", "This adapter does not expose that operation.")


def parse_usage_events(stdout):
    """Keep provider-reported counts. Never turn response bytes into token claims."""
    events, usage, reported_model = [], None, None
    for line in stdout.splitlines():
        try:
            event = strict_json(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        summary = {"type": event_type}
        for key in ("model", "model_name"):
            if isinstance(event.get(key), str):
                reported_model = event[key]
                summary[key] = event[key]
        raw = event.get("usage")
        if event_type == "turn.completed" and isinstance(raw, dict):
            keys = ("input_tokens", "cached_input_tokens", "output_tokens")
            if all(type(raw.get(key)) is int and raw[key] >= 0 for key in keys):
                usage = {key: raw[key] for key in keys}
                summary["usage"] = usage
        if event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed", "error"}:
            events.append(summary)
    return {"usage": usage, "events": events, "reported_model": reported_model}


class CodexTurnRunner:
    def __init__(self, *, executable=None, model=None, timeout=120):
        self.executable = executable or os.environ.get("WORLD_CODEX_BIN", "codex")
        self.model = model or os.environ.get("WORLD_MODEL")
        self.timeout = timeout
        self.root = Path(__file__).resolve().parent

    def generate(self, payload):
        if not shutil.which(self.executable):
            return {"ok": False, "error": "provider_unavailable", "usage": None, "events": []}
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="world-agent-turn-") as workspace:
            output = Path(workspace) / "command.json"
            command = [self.executable, "exec", "--ignore-user-config", "--ephemeral",
                       "--skip-git-repo-check", "--sandbox", "read-only", "-C", workspace,
                       "-c", "features.shell_tool=false", "-c", "features.unified_exec=false",
                       "-c", "features.multi_agent=false", "-c", "features.apps=false",
                       "-c", "features.hooks=false", "-c", "features.shell_snapshot=false",
                       "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
                       "-c", "skills.max_context_tokens=1",
                       "-c", "model_instructions_file=" + json.dumps(str(self.root / "agent_instructions.md")),
                       "--output-schema", str(self.root / "agent_schema.json"),
                       "--output-last-message", str(output), "--color", "never", "--json"]
            if self.model:
                command.extend(["--model", self.model])
            command.append("-")
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, start_new_session=True)
            except OSError:
                return {"ok": False, "error": "provider_unavailable", "usage": None, "events": [],
                        "provider_seconds": time.perf_counter() - started, "configured_model": self.model}
            timed_out = False
            try:
                stdout, stderr = process.communicate(json.dumps(payload, ensure_ascii=False), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            result = parse_usage_events(stdout)
            # Only copy the model label, not provider diagnostics or credentials.
            model_line = re.search(r"(?m)^model:\s*([^\s]+)", stderr)
            if model_line:
                result["reported_model"] = model_line.group(1)
            result.update({"provider_seconds": time.perf_counter() - started,
                           "configured_model": self.model, "exit_code": process.returncode,
                           "prompt_bytes": json_bytes(payload)})
            if timed_out or process.returncode or not output.exists():
                lowered = stderr.lower()
                error = "generation_timeout" if timed_out else "generation_failed"
                if not timed_out and ("usage limit" in lowered or "rate limit" in lowered):
                    error = "provider_usage_limit"
                result.update({"ok": False, "error": error})
                return result
            try:
                text = output.read_text()
                result.update({"ok": True, "command": strict_json(text), "output_bytes": len(text.encode("utf-8"))})
            except (ValueError, OSError):
                result.update({"ok": False, "error": "invalid_provider_json"})
            return result


def validate_command(command, adapter):
    if not isinstance(command, dict) or set(command) != {"kind", "operation", "arguments_json", "answer_json", "proposals"}:
        raise ValueError("Command must match the bounded agent schema.")
    if command["kind"] not in {"tool", "answer"}:
        raise ValueError("Unknown command kind.")
    for field in ("arguments_json", "answer_json"):
        if not isinstance(command[field], str) or len(command[field]) > 24000:
            raise ValueError("Command JSON text must be bounded.")
    arguments = strict_json(command["arguments_json"])
    answer = strict_json(command["answer_json"])
    if not isinstance(arguments, dict) or not isinstance(command["proposals"], list) or len(command["proposals"]) > 10:
        raise ValueError("Arguments or proposals are invalid.")
    if command["kind"] == "tool":
        if command["operation"] not in adapter.read_operations or command["proposals"] or answer is not None:
            raise ValueError("A tool command can perform exactly one supported read.")
        return arguments, None, []
    if command["operation"] != "none" or arguments or not isinstance(answer, dict):
        raise ValueError("An answer command must contain an answer object and no tool request.")
    proposals = []
    for proposal in command["proposals"]:
        if not isinstance(proposal, dict) or set(proposal) != {"type", "id", "changes", "expected_version", "reason"}:
            raise ValueError("Proposal must be a typed, versioned update.")
        if not isinstance(proposal["type"], str) or proposal["type"] not in adapter.store.definition.types:
            raise ValueError("Proposal type is unknown.")
        if not isinstance(proposal["id"], str) or not proposal["id"] or type(proposal["expected_version"]) is not int or proposal["expected_version"] < 1:
            raise ValueError("Proposal ID or expected version is invalid.")
        if not isinstance(proposal["reason"], str) or not proposal["reason"].strip() or len(proposal["reason"]) > 2000:
            raise ValueError("Proposal needs a bounded reason.")
        if not isinstance(proposal["changes"], list) or not proposal["changes"]:
            raise ValueError("Proposal needs changes.")
        changes = {}
        for change in proposal["changes"]:
            if not isinstance(change, dict) or set(change) != {"field", "value_json"} or not isinstance(change["field"], str) or change["field"] in changes or not isinstance(change["value_json"], str):
                raise ValueError("Proposal changes are invalid.")
            changes[change["field"]] = strict_json(change["value_json"])
        changes = validate_data(proposal["type"], changes, types=adapter.store.definition.types,
                                partial=True, actor_id=adapter.actor)
        proposals.append({**proposal, "changes": changes})
    return arguments, answer, proposals


def run_agent(adapter, request, *, runner=None, max_turns=8, apply=False):
    """Fresh bounded session. No retry hides a failed provider or invalid command."""
    runner = runner or CodexTurnRunner()
    discovery = adapter.call("describe")
    contract = deepcopy(adapter.contract)
    transcript, turns = [], []
    result = {"status": "budget_exhausted", "answer": None, "proposals": [], "turns": turns,
              "actor": adapter.actor, "mode": adapter.mode, "request": request,
              "applied": False, "transcript": transcript,
              "contract_revision": contract["revision"], "contract": contract}
    for number in range(1, max_turns + 1):
        payload = {"request": request, "world": discovery, "contract": contract,
                   "transcript": transcript, "turn": number, "turn_budget": max_turns}
        turn = runner.generate(payload)
        turns.append(turn)
        if not turn.get("ok"):
            result.update({"status": "provider_failed", "error": turn.get("error", "generation_failed")})
            break
        command = turn.get("command")
        try:
            arguments, answer, proposals = validate_command(command, adapter)
        except (ValueError, TypeError, WorldError, KeyError) as error:
            result.update({"status": "invalid_command", "error": str(error)})
            break
        if command["kind"] == "answer":
            result.update({"status": "completed", "answer": answer, "proposals": proposals})
            if apply:
                responses = adapter.apply(proposals)
                result.update({"apply_responses": responses, "applied": bool(proposals) and all("error" not in r for r in responses)})
                if any("error" in response for response in responses):
                    result["status"] = "apply_failed"
            break
        try:
            response = adapter.call(command["operation"], arguments)
        except (KeyError, TypeError, ValueError) as error:
            # A malformed model request is observable rather than a server crash.
            response = {"error": {"status": 400, "code": "invalid_arguments", "message": str(error)}}
        transcript.append({"request": {"operation": command["operation"], "arguments": arguments}, "response": response})
    usages = [turn["usage"] for turn in turns if turn.get("usage") is not None]
    result["usage"] = {"source": "Codex turn.completed usage events", "complete": len(usages) == len(turns),
                       "reported_turns": len(usages), "model_turns": len(turns),
                       **{key: sum(usage[key] for usage in usages) if usages else None
                          for key in ("input_tokens", "cached_input_tokens", "output_tokens")}}
    result["metrics"] = adapter.metrics()
    result["service_calls"] = adapter.calls
    return result
