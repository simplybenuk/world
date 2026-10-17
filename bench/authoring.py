"""Can someone who has never seen World write a world, and can a fresh agent then use it?

    python3 -m bench.authoring check                                   the references, without a model
    python3 -m bench.authoring write --domain clinic --command bench/agents/claude.sh
    python3 -m bench.authoring run --domain clinic --command bench/agents/claude.sh

`write` gives a fresh agent a plain-language brief and docs/writing-a-world.md, and nothing else. It
answers with a world as JSON. If the engine refuses it, the agent is shown the error and asked again, up
to three times. `run` then gives each task to another fresh agent, with only a URL and a token, as in the
other experiments.

The checks cannot know the field names an author chose, so they look for values. A write is correct when
a change after the seed holds every expected value. A refusal is correct when no change holds the
forbidden values and the answer says the change was not made. A reading is correct when the answer
contains what the brief makes true.
"""

import argparse
import json
import re
from pathlib import Path
import sqlite3
import tempfile

from world.definition import load_json
from world.rules import RuleError
from world.store import WorldError, WorldStore
from .__main__ import OUTPUT, PROMPT, ROOT, UNAVAILABLE, invoke, table
from .briefs import DOMAINS
from .harness import running

BRIEFS = Path(__file__).resolve().parent / "briefs"
REFUSED = ("cannot", "can't", "can’t", "could not", "couldn't", "unable", "not allowed", "not permitted", "permission",
           "refused", "rejected", "denied", "forbidden", "403", "422", "not possible", "won't", "will not", "did not", "didn't",
           "wasn't", "was not", "no change", "not made", "not booked", "not added", "not created")


def _flatten(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten(item)
    elif isinstance(value, list):
        for item in value:
            yield from _flatten(item)
    elif value is not None:
        yield value


def _holds(state, record_id, wanted):
    found = [record_id, *_flatten(state)]
    for alternatives in wanted:
        alternatives = alternatives if isinstance(alternatives, tuple) else (alternatives,)
        if not any(_matches(value, alternative) for value in found for alternative in alternatives):
            return False
    return True


def _matches(value, wanted):
    if isinstance(wanted, bool) or isinstance(value, bool):
        return value is wanted
    if isinstance(wanted, (int, float)):
        return isinstance(value, (int, float)) and value == wanted
    return isinstance(value, str) and wanted.lower() in value.lower()


def changes_after_seed(path):
    """Every create, update, and delete made by someone other than the seed, oldest first."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT action, record_id, before_state, after_state FROM events WHERE actor != 'system' ORDER BY id")
        return [(action, record_id, json.loads(before) if before else None, json.loads(after) if after else None)
                for action, record_id, before, after in rows.fetchall()]
    finally:
        connection.close()


def _found(events, spec):
    for action, record_id, before, after in events:
        if action in spec["actions"] and _holds(before if action == "delete" else after, record_id, spec["values"]):
            return True
    return False


def judge(task, events, answer):
    failures = []
    for alternatives in task.answer:
        if not any(re.search(alternative[3:], answer, re.IGNORECASE) if alternative.startswith("re:")
                   else alternative.lower() in answer.lower() for alternative in alternatives):
            failures.append(f"The answer does not say {' or '.join(alternatives)}.")
    for specs in task.changes:
        specs = specs if isinstance(specs, list) else [specs]
        if not any(_found(events, spec) for spec in specs):
            failures.append(f"No change holds {', '.join(map(str, specs[0]['values']))}.")
    for spec in task.forbid:
        if _found(events, spec):
            failures.append(f"A change holds {', '.join(map(str, spec['values']))}, which should have been refused.")
    if task.forbid and not any(word in answer.lower() for word in REFUSED):
        failures.append("The answer does not say the change was not made.")
    return failures


AUTHOR_PROMPT = """You are setting up a system of record called World for an organisation. World holds the records, \
enforces the rules, and keeps the history. People will use it only through their own AI agents, so there are no screens \
to design. Below are the organisation's brief and World's reference for writing a world.

Answer with one JSON object and nothing else: the world. Its shape is
{{"name": "...", "title": "...", "version": "1", "actor_type": "Person", "types": {{...}}, "records": [{{"type": "...", "id": "...", "data": {{...}}}}]}}

- types is an object of type names to type definitions, as in the reference, written as JSON (true, false, null).
- records are the records that exist on day one. Give every record the brief names the ID the brief gives it. List a \
record after the records it refers to.
- Every record of the actor type is a person who will act. Make the rules decide who may do what.
- Write every rule in the brief as a rule or a constraint, so that World refuses what the brief forbids.

# The brief

{brief}

# Writing a world

{reference}
"""

RETRY = """

# Your previous answer

World refused it:

{error}

Your previous answer was:

{previous}

Answer again with the whole corrected world, as one JSON object and nothing else."""


def _extract(answer):
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        raise RuleError("The answer holds no JSON object.")
    try:
        return json.loads(answer[start:end + 1])
    except json.JSONDecodeError as error:
        raise RuleError(f"The answer is not valid JSON: {error}.") from None


def validate(domain, path):
    """Load the world as the engine would, seed it, and check that the brief's records exist. Returns an error or None."""
    try:
        definition = load_json(path)
        with tempfile.TemporaryDirectory() as directory:
            store = WorldStore(Path(directory) / "world.sqlite3", definition)
            try:
                missing = [f"{type_hint} {record_id}" for type_hint, record_id in domain.RECORDS
                           if not any(record["id"] == record_id for record in definition.records)]
                people = [person for person in domain.PEOPLE if person not in definition.actors]
            finally:
                store.close()
    except (RuleError, WorldError, ValueError, KeyError, TypeError) as error:
        message = getattr(error, "message", None) or str(error)
        details = getattr(error, "details", None)
        return message + (f" Details: {json.dumps(details)}" if details else "")
    if people:
        return f"These people from the brief are not records of the actor type: {', '.join(people)}."
    if missing:
        return f"These records from the brief are missing, by ID: {', '.join(missing)}."
    return None


def write(args):
    domain = DOMAINS[args.domain]
    reference = (ROOT / "docs" / "writing-a-world.md").read_text()
    prompt = AUTHOR_PROMPT.format(brief=domain.BRIEF.strip(), reference=reference)
    target = OUTPUT / "authoring" / f"{args.domain}-{args.label}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    attempts = []
    current = prompt
    for attempt in range(1, 4):
        answer, timed_out, seconds, status = invoke(args.command, current, args.timeout)
        if status == UNAVAILABLE and not timed_out:
            print(f"attempt {attempt}: provider unavailable; run write again later")
            return
        try:
            world = _extract(answer)
            target.write_text(json.dumps(world, indent=2) + "\n")
            error = validate(domain, target)
        except RuleError as refused:
            error = str(refused)
        attempts.append({"attempt": attempt, "seconds": seconds, "timed_out": timed_out, "error": error, "answer_bytes": len(answer)})
        print(f"attempt {attempt}: {'accepted' if not error else 'refused: ' + error} ({seconds}s)", flush=True)
        if not error:
            break
        current = prompt + RETRY.format(error=error, previous=answer.strip()[:60000])
    (target.with_suffix(".attempts.json")).write_text(json.dumps(attempts, indent=2) + "\n")
    if attempts[-1]["error"]:
        print(f"The world was refused {len(attempts)} times; the last answer is in {target.relative_to(ROOT)}.")
    else:
        print(f"Saved to {target.relative_to(ROOT)}")


def run(args):
    domain = DOMAINS[args.domain]
    world = Path(args.world) if args.world else OUTPUT / "authoring" / f"{args.domain}-{args.label}.json"
    name = f"authoring-{args.domain}-{args.label}.json"
    rows = []
    for task in domain.TASKS:
        if args.task and task.id not in args.task:
            continue
        for discarded in range(7):
            with running(str(world)) as served:
                prompt = PROMPT.format(url=served.url, token=served.agent_token(task.actor, args.label), task=task.prompt)
                answer, timed_out, seconds, status = invoke(args.command, prompt, args.timeout)
                if status == UNAVAILABLE and not timed_out and discarded < 6:
                    print(f"{task.id}: provider unavailable, attempt discarded", flush=True)
                    continue
                failures = ["The agent timed out."] if timed_out else judge(task, changes_after_seed(served.store.path), answer)
                writes = [request for request in served.requests if request[0] in ("POST", "PATCH", "DELETE")
                          and not request[1].startswith("/api/query")]
                rows.append({"task": task.id, "correct": not failures, "failures": failures, **served.usage(), "seconds": seconds,
                             "refused_by_world": any(status >= 400 for _, _, status, _ in writes),
                             "requests": [list(request) for request in served.requests], "answer": answer.strip()})
                break
        print(f"{task.id}: {'correct' if not failures else '; '.join(failures)} ({rows[-1]['calls']} calls, {seconds}s)", flush=True)
        (OUTPUT / name).write_text(json.dumps(rows, indent=2) + "\n")
    print()
    print(table(["Task", "Correct", "Calls", "Rejected", "A write was refused", "Seconds"], [
        (row["task"], "yes" if row["correct"] else "no", row["calls"], row["rejected"], "yes" if row["refused_by_world"] else "no",
         row["seconds"]) for row in rows]))
    print(f"\n{sum(row['correct'] for row in rows)} of {len(rows)} correct. Saved to {(OUTPUT / name).relative_to(ROOT)}")


def check(args):
    """Without a model: each reference world loads, every check fails on an untouched world, and every check
    passes when the reference solution's change is made and its answer given."""
    rows = []
    for domain_name, domain in DOMAINS.items():
        reference = BRIEFS / f"{domain_name}-reference.json"
        error = validate(domain, reference)
        rows.append((domain_name, "reference world", "loads" if not error else error))
        for task in domain.TASKS:
            untouched = judge(task, [], "")
            with running(str(reference)) as served:
                answer = domain.SOLUTIONS[task.id](served, task)
                solved = judge(task, changes_after_seed(served.store.path), answer)
            rows.append((domain_name, task.id, "ok" if untouched and not solved else
                         f"untouched passes" if not untouched else "; ".join(solved)))
    print(table(["Domain", "Task", "Check"], rows))


def main():
    parser = argparse.ArgumentParser(prog="python3 -m bench.authoring", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("check", help="Check the briefs' references and checks without a model")
    for name in ("write", "run"):
        command = commands.add_parser(name)
        command.add_argument("--domain", choices=list(DOMAINS), required=True)
        command.add_argument("--command", help="Agent command, as for python3 -m bench agent. Default: codex exec")
        command.add_argument("--label", default="claude", help="Names the written world and the results")
        command.add_argument("--timeout", type=int, default=900 if name == "write" else 600)
    commands.choices["run"].add_argument("--world", help="The world to run against; default the one write saved")
    commands.choices["run"].add_argument("--task", action="append", help="Run only this task; repeatable")
    args = parser.parse_args()
    {"check": check, "write": write, "run": run}[args.mode](args)


if __name__ == "__main__":
    main()
