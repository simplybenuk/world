"""Disposable headless CRM comparison. Scripted clients and real model runs.

These measurements are logical in-process service requests, not HTTP/MCP cost.
Serialized UTF-8 bytes are not token counts. The baseline uses targeted queries.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from world import WorldStore

from .agent import CONTRACT_REVISION, CodexTurnRunner, CrudAdapter, FactAdapter, run_agent
from .domain import load_fixture


OPEN_STAGES = ["lead", "qualified", "proposal"]
HISTORY_REASON = "Fictional customer requested a proposal in the headless experiment."
PIPELINE_REQUEST = """Show Northstar Labs' open pipeline. Include each opportunity's ID,
name, current stage, amount_minor, currency, organization_name, owner_name,
contact_name, next_stages, and latest_interaction. Open means lead, qualified or
proposal. latest_interaction is null or an object with id, body, occurred and
    author_name. Choose the smallest interaction ID when latest dates tie.
    Return answer_json as {\"opportunities\":[...]} sorted by ID.
Fetch the records through the supplied contract. Do not propose changes."""
PROPOSAL_REQUEST = """Prepare an update moving the opportunity named Northstar renewal
to proposal. Fetch its current state and check permissions and stage transitions.
Return one reviewable proposal with its observed expected_version and only the
stage change. Return answer_json with id, current stage and integer version,
using observed values. This is a review request; do not apply it."""
HISTORY_REQUEST = """For Northstar renewal, compare its seed state with its current
state after the experiment's proposal update. The seed snapshot is 1 in the fact
contract. In the CRUD contract, use the seed create event. Return answer_json as
{\"id\":\"observed ID\",\"then_stage\":\"...\",\"then_version\":\"observed integer\",\"now_stage\":\"...\",
\"now_version\":\"observed integer\",\"changed_by\":\"...\",\"reason\":\"...\"}.
Use integers for both versions and observed values for every field.
Return changed_by as the stored actor ID from the accepted change's provenance.
Do not propose changes."""


def need(response):
    if isinstance(response, dict) and "error" in response:
        raise RuntimeError(json.dumps(response["error"]))
    return response


def database_size(path):
    parts = {name: Path(str(path) + suffix).stat().st_size if Path(str(path) + suffix).exists() else 0
             for name, suffix in (("database_bytes", ""), ("wal_bytes", "-wal"), ("shm_bytes", "-shm"))}
    return {**parts, "total_bytes": sum(parts.values())}


@contextmanager
def disposable_pair(definition):
    from .store import FactWorldStore
    with tempfile.TemporaryDirectory(prefix="world-headless-comparison-") as directory:
        root = Path(directory)
        stores = {"crud": WorldStore(root / "crud.sqlite3", definition),
                  "facts": FactWorldStore(root / "facts.sqlite3", definition)}
        try:
            yield stores, {mode: root / (mode + ".sqlite3") for mode in stores}
        finally:
            for store in stores.values():
                store.close()


def adapter_for(mode, store, actor="maya"):
    return (CrudAdapter if mode == "crud" else FactAdapter)(store, actor, disposable=True)


def expected_pipeline(definition, actor="maya"):
    """Independent oracle from the fictional definition, not a service response."""
    seed = {(type_name, record_id): data for type_name, record_id, data
            in definition.seed_records(datetime.now(timezone.utc).date())}
    organizations = {record_id: data for (kind, record_id), data in seed.items()
                     if kind == "Organization" and data["name"] == "Northstar Labs"}
    role = seed[("Person", actor)]["role"]
    answer = []
    for (kind, record_id), data in sorted(seed.items()):
        if kind != "Opportunity" or data["organization"] not in organizations or data["stage"] not in OPEN_STAGES:
            continue
        organization = organizations[data["organization"]]
        readable = role == "admin" or organization["visibility"] == "team" or actor in organization["members"]
        if not readable:
            continue
        interactions = [(rid, item) for (typ, rid), item in seed.items()
                        if typ == "Interaction" and item["opportunity"] == record_id]
        interactions.sort(key=lambda item: item[0])
        interactions.sort(key=lambda item: item[1]["occurred"], reverse=True)
        latest = None
        if interactions:
            interaction_id, interaction = interactions[0]
            latest = {"id": interaction_id, "body": interaction["body"], "occurred": interaction["occurred"],
                      "author_name": seed[("Person", interaction["author"])]["name"]}
        can_update = role == "admin" or role == "member" and actor in organization["members"]
        transitions = next(t for t in definition.types["Opportunity"]["transitions"] if t["field"] == "stage")
        answer.append({"id": record_id, "name": data["name"], "stage": data["stage"],
                       "amount_minor": data["amount_minor"], "currency": data["currency"],
                       "organization_name": organization["name"],
                       "owner_name": seed[("Person", data["owner"])]["name"] if data["owner"] else None,
                       "contact_name": seed[("Contact", data["contact"])]["name"] if data["contact"] else None,
                       "next_stages": transitions["allowed"][data["stage"]] if can_update else [],
                       "latest_interaction": latest})
    return {"opportunities": answer}


def scripted_pipeline(adapter):
    description = need(adapter.call("describe"))
    if adapter.mode == "facts":
        organizations = need(adapter.call("select", {"type": "Organization", "filters": [
            {"field": "name", "op": "eq", "value": "Northstar Labs"}], "select": ["name"]}))["records"]
        query = json.loads(Path(__file__).with_name("queries.json").read_text())["northstar_pipeline"]
        query["filters"][0]["value"] = organizations[0]["id"]
        records = need(adapter.call("select", query))["records"]
        answer = []
        for record in records:
            values = record["values"]
            related = record["related"]["latest_interaction"]
            latest = None
            if related:
                interaction = related[0]
                latest = {"id": interaction["id"], "body": interaction["values"]["body"],
                          "occurred": interaction["values"]["occurred"],
                          "author_name": interaction["values"]["author.name"]}
            answer.append({"id": record["id"], **{key: values[key] for key in ("name", "stage", "amount_minor", "currency")},
                           "organization_name": values["organization.name"], "owner_name": values["owner.name"],
                           "contact_name": values["contact.name"], "next_stages": record["next"]["stage"],
                           "latest_interaction": latest})
        return {"opportunities": sorted(answer, key=lambda item: item["id"])}
    organizations = need(adapter.call("query", {"type": "Organization", "filters": [
        {"field": "name", "op": "eq", "value": "Northstar Labs"}]}))
    organization_by_id = {record["id"]: record for record in organizations}
    records = need(adapter.call("query", {"type": "Opportunity", "filters": [
        {"field": "organization", "op": "in", "value": list(organization_by_id)},
        {"field": "stage", "op": "in", "value": OPEN_STAGES}]}))
    contact_ids = sorted({record["data"]["contact"] for record in records if record["data"]["contact"]})
    contacts = need(adapter.call("query", {"type": "Contact", "filters": [
        {"field": "id", "op": "in", "value": contact_ids}]})) if contact_ids else []
    interactions = need(adapter.call("query", {"type": "Interaction", "filters": [
        {"field": "opportunity", "op": "in", "value": [record["id"] for record in records]}]})) if records else []
    latest_by_id = latest_interactions(interactions)
    person_ids = {record["data"]["owner"] for record in records if record["data"]["owner"]}
    person_ids.update(interaction["data"]["author"] for interaction in latest_by_id.values())
    people = need(adapter.call("query", {"type": "Person", "filters": [
        {"field": "id", "op": "in", "value": sorted(person_ids)}]})) if person_ids else []
    return normalize_record_pipeline(description, records, organizations, contacts, people, latest_by_id)


def latest_interactions(interactions):
    latest_by_id = {}
    ordered = sorted(interactions, key=lambda record: record["id"])
    ordered.sort(key=lambda record: record["data"]["occurred"], reverse=True)
    for interaction in ordered:
        latest_by_id.setdefault(interaction["data"]["opportunity"], interaction)
    return latest_by_id


def normalize_record_pipeline(description, records, organizations, contacts, people, latest_by_id):
    """Same public-record normalization for targeted and bulk compatibility reads."""
    organization_by_id = {record["id"]: record for record in organizations}
    contact_by_id = {record["id"]: record for record in contacts}
    people_by_id = {record["id"]: record for record in people}
    transitions = next(t for t in description["types"]["Opportunity"]["transitions"] if t["field"] == "stage")
    answer = []
    for record in records:
        data = record["data"]
        interaction = latest_by_id.get(record["id"])
        latest = None if interaction is None else {"id": interaction["id"], "body": interaction["data"]["body"],
                "occurred": interaction["data"]["occurred"], "author_name": people_by_id[interaction["data"]["author"]]["data"]["name"]}
        answer.append({"id": record["id"], **{key: data[key] for key in ("name", "stage", "amount_minor", "currency")},
                       "organization_name": organization_by_id[data["organization"]]["data"]["name"],
                       "owner_name": people_by_id[data["owner"]]["data"]["name"] if data["owner"] else None,
                       "contact_name": contact_by_id[data["contact"]]["data"]["name"] if data["contact"] else None,
                       "next_stages": transitions["allowed"][data["stage"]] if record["capabilities"]["update"] else [],
                       "latest_interaction": latest})
    return {"opportunities": sorted(answer, key=lambda item: item["id"])}


def scripted_bulk_pipeline(adapter):
    description = need(adapter.call("describe"))
    all_records = need(adapter.call("list"))
    by_type = {type_name: [record for record in all_records if record["type"] == type_name]
               for type_name in description["types"]}
    organizations = [record for record in by_type["Organization"] if record["data"]["name"] == "Northstar Labs"]
    organization_ids = {record["id"] for record in organizations}
    opportunities = [record for record in by_type["Opportunity"] if
                     record["data"]["organization"] in organization_ids and record["data"]["stage"] in OPEN_STAGES]
    opportunity_ids = {record["id"] for record in opportunities}
    interactions = [record for record in by_type["Interaction"] if record["data"]["opportunity"] in opportunity_ids]
    return normalize_record_pipeline(description, opportunities, organizations, by_type["Contact"],
                                     by_type["Person"], latest_interactions(interactions))


def prepare_history(store, mode):
    operation = {"type": "Opportunity", "id": "northstar-renewal", "changes": {"stage": "proposal"}, "expected_version": 1}
    if mode == "facts":
        store.transact("maya", [operation], {"reason": HISTORY_REASON})
    else:
        store.update("maya", "Opportunity", operation["id"], operation["changes"], 1, {"reason": HISTORY_REASON})


def scripted_history(adapter):
    need(adapter.call("describe"))
    # Both stores expose versioned record history. Use that existing capability
    # rather than charging the fact contract for manually assembling its events.
    events = need(adapter.call("history", {"type": "Opportunity", "id": "northstar-renewal"}))
    then, now, provenance = events[0]["after"], events[-1]["after"], events[-1]
    return {"id": now["id"], "then_stage": then["data"]["stage"], "then_version": then["version"],
            "now_stage": now["data"]["stage"], "now_version": now["version"],
            "changed_by": provenance["actor"], "reason": provenance["reason"]}


def rollback_scenario(adapter, invalid=False):
    need(adapter.call("describe"))
    if adapter.mode == "facts":
        before = need(adapter.call("select", {"type": "Opportunity", "filters": [
            {"field": "organization", "op": "eq", "value": "northstar"}], "select": ["stage"]}))
        operations = [
            {"type": "Opportunity", "id": "northstar-renewal", "changes": {"stage": "proposal"}, "expected_version": 1},
            {"type": "Opportunity", "id": "northstar-pilot", "changes": {"stage": "won" if invalid else "qualified"},
             "expected_version": 1 if invalid else 999},
        ]
        response = adapter.call("transact", {"operations": operations, "provenance": {"reason": "Deliberately invalid disposable batch."}})
        after = need(adapter.call("select", {"type": "Opportunity", "filters": [
            {"field": "organization", "op": "eq", "value": "northstar"}], "select": ["stage"]}))
        return {"rejected": "error" in response, "error": response.get("error"),
                "unchanged": before == after, "atomic_batch_supported": True}
    before = need(adapter.call("query", {"type": "Opportunity", "filters": [
        {"field": "organization", "op": "eq", "value": "northstar"}]}))
    first = adapter.call("update", {"type": "Opportunity", "id": "northstar-renewal", "changes": {"stage": "proposal"},
                                 "expected_version": 1, "reason": "First operation in a sequential disposable batch."})
    second = adapter.call("update", {"type": "Opportunity", "id": "northstar-pilot",
                 "changes": {"stage": "won" if invalid else "qualified"}, "expected_version": 1 if invalid else 999,
                 "reason": "Deliberately invalid second operation."})
    after = need(adapter.call("query", {"type": "Opportunity", "filters": [
        {"field": "organization", "op": "eq", "value": "northstar"}]}))
    return {"rejected": "error" in second, "error": second.get("error"), "unchanged": before == after,
            "atomic_batch_supported": False, "first_operation_committed": "error" not in first}


def permission_scenario(adapter):
    description = need(adapter.call("describe"))
    filters = [{"field": "id", "op": "in", "value": ["atlas-expansion", "northstar-pilot", "northstar-renewal"]}]
    if adapter.mode == "facts":
        records = need(adapter.call("select", {"type": "Opportunity", "filters": filters, "select": ["name", "stage"]}))["records"]
        visible = [record["id"] for record in records]
        response = adapter.call("transact", {"operations": [{"type": "Opportunity", "id": "northstar-renewal",
            "changes": {"stage": "proposal"}, "expected_version": 1}], "provenance": {"reason": "Viewer denial check."}})
        private_response = adapter.call("transact", {"operations": [{"type": "Opportunity", "id": "atlas-expansion",
            "changes": {"stage": "won"}, "expected_version": 1}], "provenance": {"reason": "Private organization denial check."}})
    else:
        records = need(adapter.call("query", {"type": "Opportunity", "filters": filters}))
        visible = [record["id"] for record in records]
        response = adapter.call("update", {"type": "Opportunity", "id": "northstar-renewal", "changes": {"stage": "proposal"},
            "expected_version": 1, "reason": "Viewer denial check."})
        private_response = adapter.call("update", {"type": "Opportunity", "id": "atlas-expansion", "changes": {"stage": "won"},
            "expected_version": 1, "reason": "Private organization denial check."})
    return {"actor_role": description["actor"]["data"]["role"], "visible": sorted(visible),
            "private_atlas_hidden": "atlas-expansion" not in visible,
            "update_denied": response.get("error", {}).get("status") == 403,
            "northstar_update_permitted": "error" not in response,
            "atlas_update_denied": private_response.get("error", {}).get("status") == 403}


def run_scripted(*, distractors=0):
    definition = load_fixture(distractors)
    expected = expected_pipeline(definition)
    history_expected = {"id": "northstar-renewal", "then_stage": "qualified", "then_version": 1,
                        "now_stage": "proposal", "now_version": 2, "changed_by": "maya", "reason": HISTORY_REASON}
    report = report_base("scripted_clients", distractors)
    scenarios = report["scenarios"]
    for scenario in ("pipeline", "pipeline_bulk", "history", "stale_batch", "invalid_transition_batch", "member_permissions", "viewer_permissions"):
        comparison = {}
        with disposable_pair(definition) as (stores, paths):
            for mode, store in stores.items():
                adapter = adapter_for(mode, store, "sam" if scenario == "viewer_permissions" else "maya")
                if scenario in {"pipeline", "pipeline_bulk"}:
                    answer = scripted_bulk_pipeline(adapter) if scenario == "pipeline_bulk" else scripted_pipeline(adapter)
                    correct = answer == expected
                elif scenario == "history":
                    prepare_history(store, mode)
                    answer = scripted_history(adapter)
                    correct = answer == history_expected
                elif scenario in {"stale_batch", "invalid_transition_batch"}:
                    answer = rollback_scenario(adapter, invalid=scenario == "invalid_transition_batch")
                    correct = answer["rejected"] and answer["unchanged"]
                else:
                    answer = permission_scenario(adapter)
                    correct = answer["private_atlas_hidden"] and answer["atlas_update_denied"] and (
                        answer["update_denied"] if scenario == "viewer_permissions" else answer["northstar_update_permitted"])
                expected_behavior = correct
                if scenario in {"stale_batch", "invalid_transition_batch"} and mode == "crud":
                    expected_behavior = answer["rejected"] and answer["first_operation_committed"] and not answer["unchanged"]
                comparison[mode] = {"correct": bool(correct), "expected_behavior_observed": bool(expected_behavior),
                                    "answer": answer, "metrics": adapter.metrics(),
                                    "database_size": database_size(paths[mode]), "service_calls": adapter.calls}
        scenarios[scenario] = comparison
    report["all_checks_passed"] = all(run["expected_behavior_observed"] for comparison in scenarios.values() for run in comparison.values())
    report["all_tasks_correct"] = all(run["correct"] for comparison in scenarios.values() for run in comparison.values())
    report["expected_pipeline"] = expected
    report["expected_history"] = history_expected
    return report


def report_base(kind, distractors):
    return {"experiment": "agent-world", "status": "prototype", "kind": kind,
            "at": datetime.now(timezone.utc).isoformat(), "distractors": distractors,
            "contract_revision": CONTRACT_REVISION,
            "contracts": {"crud": deepcopy(CrudAdapter.contract), "facts": deepcopy(FactAdapter.contract)},
            "fixture_records": 14 + 4 * distractors, "scenarios": {},
            "measurement": {"transport": "in-process adapters; HTTP and MCP overhead excluded",
                "bytes": "compact UTF-8 JSON logical requests and responses, including discovery; not tokens",
                "time": "single-run service wall time; not a statistical latency benchmark",
                "storage": "database plus extant WAL and SHM files; historical scenarios include one accepted change",
                "setup": "fresh identical fictional fixtures per scenario; initialization and pre-scenario changes excluded",
                "baseline": "available CRUD reads; targeted and bulk pipeline controls; no atomic batch or shared cross-call snapshot",
                "history": "same simple answer, different privacy contract; CRUD checks current access only"}}


def live_correctness(result, scenario, definition, *, apply=False):
    if result["status"] != "completed":
        return False, "Agent did not complete."
    if scenario == "pipeline":
        return result["answer"] == expected_pipeline(definition) and not result["proposals"], "Exact fixture-derived pipeline answer required."
    if scenario == "history":
        expected = {"id": "northstar-renewal", "then_stage": "qualified", "then_version": 1,
                    "now_stage": "proposal", "now_version": 2, "changed_by": "maya", "reason": HISTORY_REASON}
        return result["answer"] == expected and not result["proposals"], "Exact seed/current stages, versions and provenance required."
    proposals = result["proposals"]
    expected_answer = {"id": "northstar-renewal", "stage": "qualified", "version": 1}
    correct = result["answer"] == expected_answer and len(proposals) == 1
    if correct:
        proposal = proposals[0]
        correct = proposal["type"] == "Opportunity" and proposal["id"] == "northstar-renewal" and proposal["expected_version"] == 1 and proposal["changes"] == {"stage": "proposal"}
        observed = False
        for call in result.get("service_calls", []):
            operation = call["request"]["operation"]
            args, response = call["request"]["arguments"], call["response"]
            if isinstance(response, dict) and "error" in response:
                continue
            if operation in {"query", "get", "list"}:
                records = response if isinstance(response, list) else [response]
                observed = observed or any(record.get("type") == "Opportunity" and record.get("id") == proposal["id"]
                    and record.get("version") == proposal["expected_version"] and record.get("data", {}).get("stage") == "qualified"
                    and record.get("capabilities", {}).get("update") is True for record in records)
            elif result["mode"] == "facts" and operation == "select" and args.get("as_of") is None:
                observed = observed or any(record.get("type") == "Opportunity" and record.get("id") == proposal["id"]
                    and record.get("version") == proposal["expected_version"] and record.get("values", {}).get("stage") == "qualified"
                    and "proposal" in record.get("next", {}).get("stage", []) for record in response.get("records", []))
        correct = correct and observed
    if apply:
        correct = correct and result["applied"]
    return bool(correct), "One observed, versioned qualified-to-proposal update and a successful permitted current-state read required."


def run_live(*, trials=1, distractors=0, scenarios=None, timeout=120, max_turns=8, apply=False, runner=None):
    scenarios = scenarios or ["pipeline", "propose-stage"]
    definition = load_fixture(distractors)
    report = report_base("real_codex_agent", distractors)
    report["agent"] = {"trials": trials, "max_turns": max_turns, "timeout_per_turn": timeout,
                        "apply": apply, "conversation": "fresh scenario; explicit discovery and transcript replay each model turn",
                        "mutations": "reviewable proposals by default; --apply only changes disposable fixtures",
                        "usage": "provider-reported Codex usage, missing counts remain null; no token estimates"}
    runner = runner or CodexTurnRunner(timeout=timeout)
    requests = {"pipeline": PIPELINE_REQUEST, "propose-stage": PROPOSAL_REQUEST, "history": HISTORY_REQUEST}
    for scenario in scenarios:
        runs = []
        for trial in range(trials):
            with disposable_pair(definition) as (stores, paths):
                # Alternate ordering to reduce a simple first-mode ordering bias.
                modes = ("crud", "facts") if trial % 2 == 0 else ("facts", "crud")
                comparison = {}
                for mode in modes:
                    if scenario == "history":
                        prepare_history(stores[mode], mode)
                    result = run_agent(adapter_for(mode, stores[mode]), requests[scenario], runner=runner,
                                       max_turns=max_turns, apply=apply and scenario == "propose-stage")
                    correct, assertion = live_correctness(result, scenario, definition, apply=apply and scenario == "propose-stage")
                    result.update({"correct": correct, "assertion": assertion, "database_size": database_size(paths[mode])})
                    comparison[mode] = result
                runs.append({"trial": trial + 1, "modes": comparison})
        report["scenarios"][scenario] = runs
    report["all_checks_passed"] = all(run["correct"] for runs in report["scenarios"].values()
                                      for trial in runs for run in trial["modes"].values())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Preserve JSON evidence at this path.")
    parser.add_argument("--live", action="store_true", help="Run the real Codex tool loop instead of scripted clients.")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--distractors", type=int, default=0)
    parser.add_argument("--scenario", action="append", choices=("pipeline", "propose-stage", "history"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--apply", action="store_true", help="Apply model proposals only to fresh disposable experiment databases.")
    args = parser.parse_args(argv)
    if not 1 <= args.trials <= 3 or not 1 <= args.max_turns <= 12 or not 1 <= args.timeout <= 180:
        parser.error("Use 1..3 trials, 1..12 turns, and a timeout of 1..180 seconds.")
    if not 0 <= args.distractors <= 1000:
        parser.error("Use 0..1000 distractor accounts.")
    if args.apply and not args.live:
        parser.error("--apply is only available for real agent runs against disposable fixtures.")
    report = run_live(trials=args.trials, distractors=args.distractors, scenarios=args.scenario,
                      timeout=args.timeout, max_turns=args.max_turns, apply=args.apply) if args.live else run_scripted(distractors=args.distractors)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"kind": report["kind"], "all_checks_passed": report["all_checks_passed"],
                      "scenarios": list(report["scenarios"]), "output": str(args.output) if args.output else None}))
    return 0 if report["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
