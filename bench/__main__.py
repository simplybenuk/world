"""Run the measurements.

    python3 -m bench            measurements that need no model
    python3 -m bench agent      give each task to an agent that has only a URL and a token
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time

from . import crm
from .harness import running
from world.store import WorldStore
import worlds

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "artifacts" / "bench"


def tokens(size):
    """A rough estimate for comparing sizes. JSON often costs more tokens than this."""
    return round(size / 4)


def source_size(*paths):
    files = [file for path in paths for file in ([ROOT / path] if (ROOT / path).is_file() else sorted((ROOT / path).rglob("*")))
             if file.is_file() and "__pycache__" not in file.parts]
    return sum(file.stat().st_size for file in files)


def measure_size():
    with tempfile.TemporaryDirectory() as directory:
        store = WorldStore(Path(directory) / "world.sqlite3", worlds.load("crm"))
        records = len(store.list_records("ben"))
        store.close()
        database = sum(file.stat().st_size for file in Path(directory).iterdir())
    return {
        "engine and HTTP protocol (world/)": source_size("world"),
        "CRM definition (worlds/crm.py)": source_size("worlds/crm.py"),
        "team definition (worlds/team.py)": source_size("worlds/team.py"),
        f"seeded CRM database ({records} records with history)": database,
        "reference client, not part of World (client/, client_gateway/)": source_size("client", "client_gateway"),
    }


def measure_discovery():
    rows = []
    with running() as world:
        for actor in world.store.definition.actors:
            client = world.client(actor)
            sizes = []
            for path in ("/api/world", "/api/records"):
                client.get(path)
                sizes.append(world.requests[-1][3])
            role = world.store.get_record(actor, "Person", actor)["data"]["role"]
            rows.append({"actor": actor, "role": role, "describe_bytes": sizes[0], "all_records_bytes": sizes[1],
                         "records": len(world.store.list_records(actor))})
    return rows


def measure_tasks():
    rows = []
    for task in crm.TASKS:
        with running() as world:
            answer = task.solve(world.client(task.actor))
            rows.append({"task": task.id, **world.usage(), "failures": task.check(world.store, answer)})
    return rows


def measure_recovery():
    rows = []
    for fault in crm.FAULTS:
        with running() as world:
            client = world.client(fault.actor)
            status, body = client.call(fault.method, fault.path, fault.body)
            error = body.get("error", {})
            before = len(world.requests)
            repaired = fault.repair(error, client, fault) if status >= 400 else None
            lookups = len(world.requests) - before
            retry = client.call(fault.method, *repaired)[0] if repaired else None
            rows.append({
                "fault": fault.id, "mistake": fault.mistake, "status": status, "code": error.get("code"),
                "details": sorted(error.get("details", {})), "lookups": lookups, "retry_status": retry,
                "recovered_in_one_call": bool(retry and retry < 300 and lookups == 0),
            })
    return rows


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def report(args):
    crm.SEEDED.update(seeded_ids())
    result = {"size": measure_size(), "discovery": measure_discovery(), "tasks": measure_tasks(), "recovery": measure_recovery()}
    print("## Size\n")
    print(table(["Part", "Bytes"], [(name, f"{size:,}") for name, size in result["size"].items()]))
    print("\n## Discovery cost\n")
    print(table(["Actor", "Role", "Describe world", "Rough tokens", "Read every visible record", "Rough tokens", "Records"], [
        (row["actor"], row["role"], f"{row['describe_bytes']:,} B", f"{tokens(row['describe_bytes']):,}",
         f"{row['all_records_bytes']:,} B", f"{tokens(row['all_records_bytes']):,}", row["records"]) for row in result["discovery"]]))
    print("\n## Round trips per task, scripted shortest path\n")
    print(table(["Task", "Calls", "Bytes read", "Rough tokens", "Outcome"], [
        (row["task"], row["calls"], f"{row['bytes_read']:,}", f"{tokens(row['bytes_read']):,}", "; ".join(row["failures"]) or "correct")
        for row in result["tasks"]]))
    print("\n## Recovery from a rejected request\n")
    print(table(["Mistake", "Rejected with", "Details given", "Extra lookups", "Retry", "Recovered in one call"], [
        (row["mistake"], f"{row['status']} {row['code']}", ", ".join(row["details"]) or "none", row["lookups"],
         row["retry_status"] or "no retry possible", "yes" if row["recovered_in_one_call"] else "no") for row in result["recovery"]]))
    recovered = sum(row["recovered_in_one_call"] for row in result["recovery"])
    print(f"\n{recovered} of {len(result['recovery'])} mistakes can be corrected in one call from the error's details alone.")
    save("report.json", result)


def seeded_ids():
    with running() as world:
        return {name: {record["id"] for record in world.store.list_records("ben", name)} for name in world.store.types}


def save(name, value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(json.dumps(value, indent=2) + "\n")
    print(f"\nSaved to {(OUTPUT / name).relative_to(ROOT)}")


PROMPT = """A record service is running at {url}. It speaks JSON over HTTP.
Send the header "Authorization: Bearer {token}" with every request.
Start with GET {url}/api/world. You have no other documentation.
Use curl. Do not read or write local files.

{task}

Finish with a short plain-text answer: what you found or changed, or why you could not."""


def codex_command(workspace, answer):
    return [
        shutil.which("codex") or "codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true",
        "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"', "-C", workspace,
        "--output-last-message", answer, "--color", "never", "-",
    ]


def agent(args):
    crm.SEEDED.update(seeded_ids())
    tasks = [task for task in crm.TASKS if not args.task or task.id in args.task]
    rows = []
    for task in tasks:
        with running() as world, tempfile.TemporaryDirectory(prefix="world-agent-") as workspace:
            answer_file = Path(workspace) / "answer.txt"
            prompt = PROMPT.format(url=world.url, token=world.access.tokens[task.actor], task=task.prompt)
            command = shlex.split(args.command) if args.command else codex_command(workspace, str(answer_file))
            environment = {**os.environ, "WORLD_URL": world.url, "WORLD_TOKEN": world.access.tokens[task.actor],
                           "WORLD_ANSWER_FILE": str(answer_file)}
            started = time.monotonic()
            try:
                process = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=args.timeout,
                                         cwd=workspace, env=environment)
                output, timed_out = process.stdout, False
            except subprocess.TimeoutExpired:
                output, timed_out = "", True
            seconds = round(time.monotonic() - started, 1)
            answer = answer_file.read_text() if answer_file.exists() else output
            failures = ["The agent timed out."] if timed_out else task.check(world.store, answer)
            changed = [(row["type"], row["id"]) for row in changed_records(world.store)]
            rows.append({
                "task": task.id, "correct": not failures, "failures": failures, **world.usage(), "seconds": seconds,
                "requests": [list(request) for request in world.requests], "answer": answer.strip(),
                "own_reasons": all(crm._reason_given(world.store, *record) for record in changed),
            })
            print(f"{task.id}: {'correct' if not failures else '; '.join(failures)} "
                  f"({rows[-1]['calls']} calls, {rows[-1]['rejected']} rejected, {seconds}s)", flush=True)
    print()
    print(table(["Task", "Correct", "Calls", "Rejected", "Bytes read", "Rough tokens", "Seconds", "Gave its own reasons"], [
        (row["task"], "yes" if row["correct"] else "no", row["calls"], row["rejected"], f"{row['bytes_read']:,}",
         f"{tokens(row['bytes_read']):,}", row["seconds"], "yes" if row["own_reasons"] else "no") for row in rows]))
    print(f"\n{sum(row['correct'] for row in rows)} of {len(rows)} tasks correct.")
    save(f"agent-{args.label}.json", rows)


def changed_records(store):
    """Records an actor other than the seed has written."""
    return [record for record in store.list_records("ben")
            if any(event["actor"] != "system" for event in store.history("ben", record["type"], record["id"]))]


def main():
    parser = argparse.ArgumentParser(prog="python3 -m bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="mode")
    runner = commands.add_parser("agent", help="Run the tasks with an agent; this uses that agent's model account")
    runner.add_argument("--command", help="Agent command. It receives the prompt on standard input and WORLD_URL, WORLD_TOKEN, "
                        "and WORLD_ANSWER_FILE in its environment. Its answer is that file, or else its standard output. Default: codex exec")
    runner.add_argument("--task", action="append", choices=[task.id for task in crm.TASKS], help="Run only this task; repeatable")
    runner.add_argument("--label", default="codex", help="Name for the saved results file")
    runner.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    (agent if args.mode == "agent" else report)(args)


if __name__ == "__main__":
    main()
