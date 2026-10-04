"""Ask Codex for a bounded view plan. Never execute generated code."""

import datetime as dt
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading

from world.schema import validate_value, WorldError


def strict_json(value):
    def reject_constant(_):
        raise ValueError("Non-finite numbers are not JSON values")
    def finite_float(text):
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("Non-finite numbers are not JSON values")
        return number
    return json.loads(value, parse_constant=reject_constant, parse_float=finite_float)


class ClientError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = {}


class CodexPlanner:
    def __init__(self, *, executable=None, model=None, timeout=120):
        self.executable = executable or os.environ.get("WORLD_CODEX_BIN", "codex")
        self.model = model or os.environ.get("WORLD_MODEL")
        self.timeout = timeout
        self._slots = threading.BoundedSemaphore(2)
        self.root = Path(__file__).resolve().parent

    @property
    def available(self):
        return shutil.which(self.executable) is not None

    def plan(self, description, records, message, context=None):
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise ClientError(400, "invalid_message", "Enter a request of up to 4,000 characters.")
        if not self.available:
            raise ClientError(503, "provider_unavailable", "Codex is unavailable on this server. Record browsing and editing still work.")
        if not self._slots.acquire(blocking=False):
            raise ClientError(429, "planner_busy", "Both generation slots are busy. Try again shortly.")
        try:
            context = self._context(context)
            payload = {
                "current_date": dt.datetime.now(dt.timezone.utc).date().isoformat(),
                "world": description,
                "records": records,
                "previous_view": context.get("view"),
                "conversation": context.get("conversation", []),
                "request": message.strip(),
            }
            plan = self._generate(payload)
            return self.validate(plan, description, records)
        finally:
            self._slots.release()

    @staticmethod
    def _context(context):
        if context is None:
            return {}
        if not isinstance(context, dict):
            raise ClientError(400, "invalid_context", "The conversation context must be an object.")
        conversation = context.get("conversation", [])
        if not isinstance(conversation, list):
            raise ClientError(400, "invalid_context", "The conversation must be a list.")
        cleaned = []
        for item in conversation[-8:]:
            if not isinstance(item, dict) or not isinstance(item.get("role"), str) or item["role"] not in {"user", "assistant"} or not isinstance(item.get("content"), str):
                raise ClientError(400, "invalid_context", "The conversation contains an invalid message.")
            cleaned.append({"role": item["role"], "content": item["content"][:4000]})
        view = context.get("view")
        if view is not None and (not isinstance(view, dict) or len(json.dumps(view)) > 16000):
            raise ClientError(400, "invalid_context", "The previous view is invalid.")
        return {"conversation": cleaned, "view": view}

    def _generate(self, payload):
        with tempfile.TemporaryDirectory(prefix="world-view-") as workspace:
            output = Path(workspace) / "plan.json"
            command = [
                self.executable, "exec", "--ignore-user-config", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "-C", workspace,
                "-c", "features.shell_tool=false", "-c", "features.unified_exec=false",
                "-c", "features.multi_agent=false", "-c", "features.apps=false",
                "-c", "features.hooks=false", "-c", "features.shell_snapshot=false",
                "-c", 'web_search="disabled"',
                "-c", "project_doc_max_bytes=0", "-c", "skills.max_context_tokens=1",
                "-c", 'model_reasoning_effort="low"',
                "-c", "model_instructions_file=" + json.dumps(str(self.root / "instructions.md")),
                "--output-schema", str(self.root / "plan-schema.json"),
                "--output-last-message", str(output), "--color", "never",
            ]
            if self.model:
                command.extend(["--model", self.model])
            command.append("-")
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                _, diagnostic = process.communicate(json.dumps(payload, ensure_ascii=False), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise ClientError(504, "generation_timeout", "The view took too long to generate. Your records are unchanged. Try again.") from None
            if process.returncode or not output.exists():
                # Provider diagnostics may contain request context; keep them out of HTTP responses.
                lowered = diagnostic.lower()
                if "rate limit" in lowered or "usage limit" in lowered:
                    message = "Codex has reached its usage limit. Try again later. You can still browse and edit records."
                elif "not logged" in lowered or "unauthorized" in lowered or "authentication" in lowered:
                    message = "The server's Codex login needs attention. Run codex login on the server."
                else:
                    message = "Codex could not generate this view. Try again. You can still browse and edit records."
                raise ClientError(502, "generation_failed", message)
            try:
                return strict_json(output.read_text())
            except (ValueError, OSError):
                raise ClientError(502, "invalid_plan", "Codex returned an invalid view. Try again.") from None

    @staticmethod
    def validate(plan, description, records):
        def invalid():
            raise ClientError(502, "invalid_plan", "Codex returned a view that does not match this world. Try again.")

        if not isinstance(plan, dict) or not isinstance(plan.get("message"), str) or len(plan["message"]) > 8000:
            invalid()
        types = description["types"]
        view = plan.get("view")
        if not isinstance(view, dict) or not isinstance(view.get("type"), str) or view["type"] not in types or not isinstance(view.get("layout"), str) or view["layout"] not in {"list", "board", "table", "overview"}:
            invalid()
        fields = types[view["type"]]["fields"]
        for key in ("title", "description"):
            if not isinstance(view.get(key), str) or len(view[key]) > 2000:
                invalid()
        if not isinstance(view.get("fields"), list) or any(not isinstance(f, str) or f not in fields for f in view["fields"]):
            invalid()
        if view.get("group_by") is not None and (not isinstance(view["group_by"], str) or view["group_by"] not in fields):
            invalid()
        by_id = {(r["type"], r["id"]): r for r in records}
        if not isinstance(view.get("record_ids"), list) or any(not isinstance(i, str) or (view["type"], i) not in by_id for i in view["record_ids"]):
            invalid()
        view["record_ids"] = list(dict.fromkeys(view["record_ids"]))
        if not isinstance(plan.get("proposals"), list) or len(plan["proposals"]) > 10:
            invalid()
        proposals = []
        for proposal in plan["proposals"]:
            if not isinstance(proposal, dict) or not isinstance(proposal.get("type"), str) or proposal["type"] not in types or not isinstance(proposal.get("operation"), str) or proposal["operation"] not in {"create", "update", "delete"}:
                invalid()
            type_name = proposal["type"]
            operation = proposal["operation"]
            if not isinstance(proposal.get("reason"), str) or len(proposal["reason"]) > 4000:
                invalid()
            changes = proposal.get("changes")
            if not isinstance(changes, list):
                invalid()
            normalized = {}
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get("field"), str) or change["field"] not in types[type_name]["fields"] or change["field"] in normalized or not isinstance(change.get("value_json"), str):
                    invalid()
                try:
                    normalized[change["field"]] = strict_json(change["value_json"])
                except ValueError:
                    invalid()
            if operation == "create":
                if proposal.get("id") is not None or proposal.get("expected_version") is not None or not types[type_name]["capabilities"].get("create"):
                    invalid()
            else:
                record_id = proposal.get("id")
                if not isinstance(record_id, str) or (type_name, record_id) not in by_id:
                    invalid()
                record = by_id[type_name, record_id]
                version = proposal.get("expected_version")
                if type(version) is not int or version != record["version"] or not record["capabilities"].get(operation):
                    invalid()
            if operation == "delete" and normalized:
                invalid()
            if operation == "update" and not normalized:
                invalid()
            if operation == "create":
                for field, descriptor in types[type_name]["fields"].items():
                    if descriptor.get("required") and field not in normalized and "default" not in descriptor:
                        invalid()
            actor = description["actor"]
            policy = types[type_name].get("policy", {})
            for field, value in normalized.items():
                descriptor = types[type_name]["fields"][field]
                try:
                    validate_value(field, value, descriptor)
                except WorldError:
                    invalid()
                if actor["data"].get("role") != "admin":
                    author_field = policy.get("author_field")
                    protected = field in policy.get("admin_fields", []) and not (
                        operation == "create" and field == author_field
                    )
                    previous = by_id.get((type_name, proposal.get("id")), {}).get("data", {}).get(field)
                    if protected and (operation == "create" or value != previous):
                        invalid()
                    if operation == "create" and author_field == field and value != actor["id"]:
                        invalid()
                targets = [(descriptor["target"], value)] if descriptor["type"] == "reference" and value is not None else []
                if descriptor["type"] == "list" and descriptor.get("items", {}).get("type") == "reference":
                    targets = [(descriptor["items"]["target"], item) for item in value]
                if any((target, target_id) not in by_id for target, target_id in targets):
                    invalid()
            if operation == "update":
                before = by_id[type_name, proposal["id"]]["data"]
                for transition in types[type_name].get("transitions", []):
                    field = transition["field"]
                    if field in normalized and normalized[field] != before[field]:
                        if normalized[field] not in transition["allowed"].get(before[field], []):
                            invalid()
            proposals.append({**proposal, "changes": normalized})
        return {"message": plan["message"], "view": view, "proposals": proposals, "provider": "codex"}
