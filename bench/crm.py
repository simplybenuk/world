"""CRM tasks and faulty writes. Prompts use the domain's words, not the protocol's,
so the same tasks can be given to an agent working against another CRM."""

from dataclasses import dataclass
from typing import Callable

OPEN_STAGES = ["lead", "qualified", "proposal", "negotiation"]
REFUSALS = ("cannot", "can't", "can’t", "could not", "couldn't", "unable", "not allowed", "not permitted", "permission", "closed", "admin")


@dataclass
class Task:
    id: str
    actor: str
    prompt: str
    solve: Callable  # (client) -> answer; the fewest calls a client that knows the protocol needs
    check: Callable  # (store, answer) -> list of failures


def _find(client, type_name, field, text):
    """One query by a word from the request, as a client with no prior IDs must do."""
    status, body = client.call("POST", "/api/query", {"type": type_name, "filters": [{"field": field, "op": "contains", "value": text}]})
    (record,) = body["records"]
    return record


def _created(store, type_name, seeded):
    return [record for record in store.list_records("ben", type_name) if record["id"] not in seeded]


def _reason_given(store, type_name, record_id):
    """Whether the last change carries the agent's own reason rather than the server's default."""
    reason = store.history("ben", type_name, record_id)[-1]["reason"]
    return reason != "Changed through the World client"


def solve_pipeline(client):
    client.get("/api/world")
    _, body = client.call("POST", "/api/query", {"type": "Opportunity", "filters": [
        {"field": "owner", "op": "eq", "value": "priya"}, {"field": "stage", "op": "in", "value": OPEN_STAGES}]})
    return f"{len(body['records'])} open opportunities worth {sum(record['data']['amount'] or 0 for record in body['records'])}"


def check_pipeline(store, answer):
    digits = answer.replace(",", "").replace(".", "")
    return [] if "85000" in digits or "85k" in answer.lower() else ["The answer does not give the 85,000 total."]


def solve_win(client):
    client.get("/api/world")
    opportunity = _find(client, "Opportunity", "name", "renewal")
    today = opportunity["updated_at"][:10]
    client.call("PATCH", f"/api/records/Opportunity/{opportunity['id']}", {
        "changes": {"stage": "won", "close_date": today}, "expected_version": opportunity["version"],
        "reason": "Northwind signed the renewal."})
    client.call("POST", "/api/records/Interaction", {"data": {
        "kind": "note", "summary": "Northwind signed the renewal for $48,000.", "occurred_on": today,
        "organization": opportunity["data"]["organization"], "opportunity": opportunity["id"]},
        "reason": "Recording why the renewal was won."})
    return "Marked the renewal as won and added a note."


def check_win(store, answer):
    failures = []
    opportunity = store.get_record("ben", "Opportunity", "northwind-renewal")
    if opportunity["data"]["stage"] != "won" or opportunity["data"]["amount"] != 48000:
        failures.append("The renewal is not won at 48,000.")
    notes = [record for record in _created(store, "Interaction", SEEDED["Interaction"]) if record["data"]["opportunity"] == "northwind-renewal"]
    if not notes:
        failures.append("No interaction about the renewal was recorded.")
    return failures


def solve_lose(client):
    client.get("/api/world")
    opportunity = _find(client, "Opportunity", "name", "Harbor")
    client.call("PATCH", f"/api/records/Opportunity/{opportunity['id']}", {
        "changes": {"stage": "lost", "lost_reason": "Budget frozen."}, "expected_version": opportunity["version"],
        "reason": "Harbor Logistics froze their budget."})
    return "Marked the pilot as lost: budget frozen."


def check_lose(store, answer):
    data = store.get_record("ben", "Opportunity", "harbor-pilot")["data"]
    return [] if data["stage"] == "lost" and (data["lost_reason"] or "").strip() else ["The pilot is not lost with a reason."]


def solve_contact(client):
    client.get("/api/world")
    opportunity = _find(client, "Opportunity", "name", "Harbor")
    _, body = client.call("POST", "/api/records/Contact", {"data": {
        "name": "Jo Park", "job_title": "Operations Manager", "email": "jo@harbor.example",
        "organization": opportunity["data"]["organization"]}, "reason": "New contact at Harbor Logistics."})
    client.call("PATCH", f"/api/records/Opportunity/{opportunity['id']}", {
        "changes": {"primary_contact": body["record"]["id"]}, "expected_version": opportunity["version"],
        "reason": "Jo Park is now the main contact for the pilot."})
    return "Added Jo Park and made them the pilot's primary contact."


def check_contact(store, answer):
    contacts = [record for record in _created(store, "Contact", SEEDED["Contact"]) if record["data"]["name"] == "Jo Park"]
    if len(contacts) != 1:
        return [f"Expected one new contact named Jo Park, found {len(contacts)}."]
    failures = []
    if contacts[0]["data"]["organization"] != "harbor" or contacts[0]["data"]["email"] != "jo@harbor.example":
        failures.append("Jo Park is not at Harbor Logistics with the given email.")
    if store.get_record("ben", "Opportunity", "harbor-pilot")["data"]["primary_contact"] != contacts[0]["id"]:
        failures.append("Jo Park is not the pilot's primary contact.")
    return failures


def solve_closed(client):
    client.get("/api/world")
    opportunity = _find(client, "Opportunity", "name", "training")
    assert not opportunity["capabilities"]["update"]
    return "I cannot change it: the opportunity is closed and only an admin can change it."


def check_closed(store, answer):
    failures = []
    opportunity = store.get_record("ben", "Opportunity", "alder-training")
    if opportunity["version"] != 1:
        failures.append("The closed opportunity was changed.")
    if not any(word in answer.lower() for word in REFUSALS):
        failures.append("The answer does not say the change was not possible.")
    return failures


SEEDED = {}  # type -> seeded IDs, filled by the runner from a fresh world

TASKS = [
    Task("pipeline", "priya", "I'm Priya. What is the total amount of the open opportunities I own?", solve_pipeline, check_pipeline),
    Task("win", "priya", "I'm Priya. Northwind signed the annual renewal today for $48,000. Mark that opportunity as won and record a note saying why.", solve_win, check_win),
    Task("lose", "priya", "I'm Priya. The Harbor Logistics pilot is dead because their budget was frozen. Record that.", solve_lose, check_lose),
    Task("contact", "priya", "I'm Priya. Add a new contact at Harbor Logistics: Jo Park, Operations Manager, jo@harbor.example. Make them the primary contact on the pilot opportunity.", solve_contact, check_contact),
    Task("closed", "tom", "I'm Tom. Change the amount on the Alder Clinics staff training opportunity to 9,500.", solve_closed, check_closed),
]


@dataclass
class Fault:
    """A plausible wrong request, and how to correct it.

    `repair` returns the corrected body or path. It reads the error's details
    first and falls back to `client` lookups, which are counted. A fault is
    recovered in one call when the retry succeeds with no lookups.
    """

    id: str
    actor: str
    mistake: str
    method: str
    path: str
    body: dict
    repair: Callable  # (error, client, fault) -> (path, body) or None when no retry can succeed


def _version(error, client, path):
    current = error["details"].get("current_version")
    return current if current is not None else client.get(path)["record"]["version"]


def repair_version(error, client, fault):
    return fault.path, {**fault.body, "expected_version": _version(error, client, fault.path)}


def repair_enum(error, client, fault):
    values = error["details"].get("values") or client.get("/api/types/Opportunity")["fields"]["stage"]["values"]
    return fault.path, {**fault.body, "changes": {"stage": next(value for value in values if value in "closed-won")}}


def repair_constraint(error, client, fault):
    fields = error["details"].get("fields")
    if fields is None:
        fields = [name for name in ("amount", "close_date") if name in " ".join(client.get("/api/types/Opportunity")["constraints"])]
    supplied = {"amount": 30000, "close_date": "2026-10-01"}
    return fault.path, {**fault.body, "changes": {**fault.body["changes"], **{name: supplied[name] for name in fields}}}


def repair_unknown_field(error, client, fault):
    allowed = error["details"].get("allowed") or list(client.get("/api/types/Opportunity")["fields"])
    return fault.path, {**fault.body, "changes": {next(name for name in allowed if name == "amount"): 50000}}


def repair_required(error, client, fault):
    spec = error["details"].get("spec") or client.get("/api/types/Interaction")["fields"][error["details"]["field"]]
    assert spec["type"] == "date"
    return fault.path, {**fault.body, "data": {**fault.body["data"], error["details"]["field"]: "2026-10-01"}}


def repair_reference(error, client, fault):
    candidates = error["details"].get("candidates")
    if candidates is None:
        _, body = client.call("POST", "/api/query", {"type": "Organization", "filters": [{"field": "name", "op": "contains", "value": "Harbor"}]})
        candidates = [record["id"] for record in body["records"]]
    return fault.path, {**fault.body, "data": {**fault.body["data"], "organization": candidates[0]}}


def repair_type(error, client, fault):
    types = error["details"].get("types") or list(client.get("/api/world")["types"])
    return f"/api/records/{next(name for name in types if name == 'Opportunity')}", fault.body


def repair_filter(error, client, fault):
    keys = error["details"].get("keys")
    if keys is None:
        return None  # the shape of a filter is stated only in the message's prose
    return fault.path, {**fault.body, "filters": [dict(zip(keys, ("stage", "eq", "won")))]}


def repair_denied(error, client, fault):
    return None


FAULTS = [
    Fault("stale-version", "priya", "Update with an out-of-date version", "PATCH", "/api/records/Opportunity/harbor-pilot",
          {"changes": {"amount": 25000}, "expected_version": 7}, repair_version),
    Fault("missing-version", "priya", "Update without a version", "PATCH", "/api/records/Opportunity/harbor-pilot",
          {"changes": {"amount": 25000}}, repair_version),
    Fault("wrong-enum", "priya", "Stage written as closed-won", "PATCH", "/api/records/Opportunity/northwind-renewal",
          {"changes": {"stage": "closed-won"}, "expected_version": 1}, repair_enum),
    Fault("constraint", "tom", "Won without an amount or close date", "PATCH", "/api/records/Opportunity/alder-rollout",
          {"changes": {"stage": "won"}, "expected_version": 1}, repair_constraint),
    Fault("unknown-field", "priya", "Amount written as value", "PATCH", "/api/records/Opportunity/harbor-pilot",
          {"changes": {"value": 50000}, "expected_version": 1}, repair_unknown_field),
    Fault("missing-required", "priya", "Interaction without its date", "POST", "/api/records/Interaction",
          {"data": {"kind": "call", "summary": "Spoke to Mei.", "organization": "harbor"}}, repair_required),
    Fault("wrong-reference", "priya", "Organization given by a guessed ID", "POST", "/api/records/Contact",
          {"data": {"name": "Jo Park", "organization": "harbor-logistics"}}, repair_reference),
    Fault("wrong-type", "priya", "Opportunity called Deal", "POST", "/api/records/Deal",
          {"data": {"name": "Harbor expansion", "organization": "harbor"}}, repair_type),
    Fault("wrong-filter", "priya", "Filter key written as operator", "POST", "/api/query",
          {"type": "Opportunity", "filters": [{"field": "stage", "operator": "eq", "value": "won"}]}, repair_filter),
    Fault("denied", "tom", "Change to a closed opportunity", "PATCH", "/api/records/Opportunity/alder-training",
          {"changes": {"amount": 9500}, "expected_version": 1}, repair_denied),
]
