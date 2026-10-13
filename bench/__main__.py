"""Run the measurements.

    python3 -m bench                    measurements that need no model
    python3 -m bench control            the same, for the EspoCRM control (needs bench/control running)
    python3 -m bench agent              give each task to an agent that has only a URL and a token
    python3 -m bench agent --control    give the same tasks to the same agent against EspoCRM
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import crm, lab
from .harness import running
from world.store import WorldStore
import worlds

DOMAINS = {"crm": crm, "lab": lab}
CONTROLS = {"crm": "espocrm", "lab": "lab"}
domain = crm  # set from --domain

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "artifacts" / "bench"


def admin():
    """The world's first seeded actor, who can read everything."""
    return worlds.load(domain.WORLD).actors[0]


def control_module():
    from importlib import import_module
    return import_module(f"bench.control.{CONTROLS[domain.WORLD]}")


def tokens(size):
    """A rough estimate for comparing sizes. JSON often costs more tokens than this."""
    return round(size / 4)


def source_size(*paths):
    files = [file for path in paths for file in ([ROOT / path] if (ROOT / path).is_file() else sorted((ROOT / path).rglob("*")))
             if file.is_file() and "__pycache__" not in file.parts]
    return sum(file.stat().st_size for file in files)


def measure_size():
    with tempfile.TemporaryDirectory() as directory:
        store = WorldStore(Path(directory) / "world.sqlite3", worlds.load(domain.WORLD))
        records = len(store.list_records(admin()))
        store.close()
        database = sum(file.stat().st_size for file in Path(directory).iterdir())
    return {
        "engine and HTTP protocol (world/)": source_size("world"),
        f"{domain.WORLD} definition (worlds/{domain.WORLD}.py)": source_size(f"worlds/{domain.WORLD}.py"),
        f"seeded database ({records} records with history)": database,
        "reference client, not part of World (client/, client_gateway/)": source_size("client", "client_gateway"),
    }


def measure_discovery():
    rows = []
    with running(domain.WORLD) as world:
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
    for task in domain.TASKS:
        with running(domain.WORLD) as world:
            started = time.perf_counter()
            answer = task.solve(world.client(task.actor))
            seconds = time.perf_counter() - started
            rows.append({"task": task.id, **world.usage(), "milliseconds": round(seconds * 1000, 1),
                         "failures": task.check(world.store, answer)})
    return rows


def runtime_size():
    """The Python interpreter and standard library this service runs on."""
    library = Path(sys.modules["os"].__file__).parent
    files = [file for file in library.rglob("*") if file.is_file() and "site-packages" not in file.parts and "__pycache__" not in file.parts]
    return sum(file.stat().st_size for file in files) + Path(sys.executable).resolve().stat().st_size


def resident_memory():
    """Memory held by an idle World server with the seeded CRM, in bytes."""
    with tempfile.TemporaryDirectory() as directory:
        process = subprocess.Popen([sys.executable, "-m", "world.server", "--world", domain.WORLD, "--data-dir", directory, "--port", "0"],
                                   cwd=ROOT, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.5)
            status = Path(f"/proc/{process.pid}/status").read_text()
            return int(next(line for line in status.splitlines() if line.startswith("VmRSS")).split()[1]) * 1024
        finally:
            process.terminate()
            process.wait()


def docker(*arguments):
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, check=True).stdout.strip()


def control(args):
    """Size, self-description, and scripted tasks for EspoCRM, to set beside the World report."""
    espocrm = control_module()
    compose = ["compose", "-f", str(ROOT / "bench" / "control" / "compose.yaml")]
    users = espocrm.setup()
    ids = espocrm.reset(users)
    version = espocrm.Api().ok("GET", "Settings")["version"]
    images = {name: int(docker("image", "inspect", name, "--format", "{{.Size}}")) for name in ("espocrm/espocrm:latest", "mariadb:11.4")}
    measured = lambda service, path: int(docker(*compose, "exec", "-T", service, "du", "-sb", path).split()[0])
    memory = {}
    for line in docker("stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}").splitlines():
        name, used = line.split()[:2]
        if name.startswith("world-control-"):
            memory[name] = used
    size = {
        f"EspoCRM {version} release archive, compressed": 42_630_269,  # EspoCRM-10.0.9.zip on its GitHub release
        "EspoCRM application files, installed": measured("espocrm", "/var/www/html"),
        "EspoCRM container image (application, PHP, Apache)": images["espocrm/espocrm:latest"],
        "MariaDB container image": images["mariadb:11.4"],
        "seeded database directory": measured("db", "/var/lib/mysql"),
    }
    world_side = {
        f"World engine, protocol, and {domain.WORLD} definition": source_size("world", f"worlds/{domain.WORLD}.py", "worlds/__init__.py"),
        "Python interpreter and standard library": runtime_size(),
        "World server, idle memory": resident_memory(),
    }
    discovery = []
    for actor in ("priya", "nina"):
        meter = espocrm.Meter()
        espocrm.Api(api_key=users[actor]["key"], port=meter.port).ok("GET", "Metadata")
        discovery.append({"actor": actor, "metadata_bytes": meter.usage()["bytes_read"]})
        meter.close()
    tasks = []
    for task in espocrm.TASKS:
        ids = espocrm.reset(users)
        started_at, meter = espocrm.now(), espocrm.Meter()
        started = time.perf_counter()
        answer = task.solve(espocrm.Api(api_key=users[task.actor]["key"], port=meter.port))
        seconds = time.perf_counter() - started
        tasks.append({"task": task.id, **meter.usage(), "milliseconds": round(seconds * 1000, 1),
                      "failures": espocrm.checks(ids, started_at)[task.id](answer)})
        meter.close()
    mistakes = espocrm.measure_faults(users) if hasattr(espocrm, "measure_faults") else []
    print(f"## Control size: EspoCRM {version}\n")
    print(table(["Part", "Bytes"], [(name, f"{value:,}") for name, value in size.items()]))
    print("\nIdle memory: " + ", ".join(f"{name} {used}" for name, used in sorted(memory.items())))
    print("\n## World, with what it needs to run\n")
    print(table(["Part", "Bytes"], [(name, f"{value:,}") for name, value in world_side.items()]))
    print("\n## Control self-description\n")
    print(table(["Actor", "GET Metadata", "Rough tokens"], [
        (row["actor"], f"{row['metadata_bytes']:,} B", f"{tokens(row['metadata_bytes']):,}") for row in discovery]))
    print("\n## Control round trips per task, scripted shortest path, no discovery call\n")
    print(table(["Task", "Calls", "Bytes read", "Milliseconds", "Outcome"], [
        (row["task"], row["calls"], f"{row['bytes_read']:,}", row["milliseconds"], "; ".join(row["failures"]) or "correct") for row in tasks]))
    print("\n## Control response to the same mistakes\n")
    print(table(["Mistake", "Response", "What it said", "Result"], [
        (row["mistake"], row["status"], row["reason"] or "nothing", "rejected" if row["status"] >= 300 else row["harm"] or "accepted")
        for row in mistakes]))
    print(f"\n{sum(row['status'] >= 300 for row in mistakes)} of {len(mistakes)} rejected, "
          f"{sum(bool(row['harm']) for row in mistakes)} accepted with a wrong result.")
    save(f"control-{domain.WORLD}.json", {"version": version, "size": size, "memory": memory, "world": world_side, "discovery": discovery,
                          "tasks": tasks, "mistakes": mistakes})


def measure_recovery():
    rows = []
    for fault in domain.FAULTS:
        with running(domain.WORLD) as world:
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
    domain.SEEDED.update(seeded_ids())
    result = {"size": measure_size(), "discovery": measure_discovery(), "tasks": measure_tasks(), "recovery": measure_recovery()}
    print("## Size\n")
    print(table(["Part", "Bytes"], [(name, f"{size:,}") for name, size in result["size"].items()]))
    print("\n## Discovery cost\n")
    print(table(["Actor", "Role", "Describe world", "Rough tokens", "Read every visible record", "Rough tokens", "Records"], [
        (row["actor"], row["role"], f"{row['describe_bytes']:,} B", f"{tokens(row['describe_bytes']):,}",
         f"{row['all_records_bytes']:,} B", f"{tokens(row['all_records_bytes']):,}", row["records"]) for row in result["discovery"]]))
    print("\n## Round trips per task, scripted shortest path\n")
    print(table(["Task", "Calls", "Bytes read", "Rough tokens", "Milliseconds", "Outcome"], [
        (row["task"], row["calls"], f"{row['bytes_read']:,}", f"{tokens(row['bytes_read']):,}", row["milliseconds"],
         "; ".join(row["failures"]) or "correct") for row in result["tasks"]]))
    print("\n## Recovery from a rejected request\n")
    print(table(["Mistake", "Rejected with", "Details given", "Extra lookups", "Retry", "Recovered in one call"], [
        (row["mistake"], f"{row['status']} {row['code']}", ", ".join(row["details"]) or "none", row["lookups"],
         row["retry_status"] or "no retry possible", "yes" if row["recovered_in_one_call"] else "no") for row in result["recovery"]]))
    recovered = sum(row["recovered_in_one_call"] for row in result["recovery"])
    print(f"\n{recovered} of {len(result['recovery'])} mistakes can be corrected in one call from the error's details alone.")
    save(f"report-{domain.WORLD}.json", result)


def seeded_ids():
    with running(domain.WORLD) as world:
        return {name: {record["id"] for record in world.store.list_records(admin(), name)} for name in world.store.types}


def save(name, value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(json.dumps(value, indent=2) + "\n")
    print(f"\nSaved to {(OUTPUT / name).relative_to(ROOT)}")


PROMPT = """A record service is running at {url}. Its JSON API is under {url}/api.
Send the header "Authorization: Bearer {token}" with every request.
GET {url}/api/world describes it. You have no other documentation.
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


CONTROL_PROMPT = """A record service is running at {url}. It is EspoCRM, and its JSON API is under {url}/api/v1.
Send the header "X-Api-Key: {token}" with every request.
GET {url}/api/v1/Metadata describes it. You have no other documentation.
Use curl. Do not read or write local files.

{task}

Finish with a short plain-text answer: what you found or changed, or why you could not."""


@contextmanager
def world_target(task, label):
    """A fresh World for one task: its prompt, its meter, and its checker."""
    with running(domain.WORLD) as world:
        def finish(answer):
            changed = [(row["type"], row["id"]) for row in changed_records(world.store)]
            return task.check(world.store, answer), all(reason_given(world.store, *record) for record in changed)
        yield PROMPT.format(url=world.url, token=world.agent_token(task.actor, label), task=task.prompt), world, finish


@contextmanager
def control_target(task, users):
    espocrm = control_module()
    ids = espocrm.reset(users)
    started, meter = espocrm.now(), espocrm.Meter()
    try:
        finish = lambda answer: (espocrm.checks(ids, started)[task.id](answer), None)
        yield CONTROL_PROMPT.format(url=meter.url, token=users[task.actor]["key"], task=task.prompt), meter, finish
    finally:
        meter.close()


def agent(args):
    domain.SEEDED.update(seeded_ids())
    tasks = [task for task in domain.TASKS if not args.task or task.id in args.task]
    users = None
    if args.control:
        users = control_module().setup()
    rows = []
    for run in range(1, args.repeat + 1):
        for task in tasks:
            target = control_target(task, users) if args.control else world_target(task, args.label)
            with target as (prompt, meter, finish), tempfile.TemporaryDirectory(prefix="world-agent-") as workspace:
                answer_file = Path(workspace) / "answer.txt"
                command = shlex.split(args.command) if args.command else codex_command(workspace, str(answer_file))
                environment = {**os.environ, "WORLD_ANSWER_FILE": str(answer_file)}
                started = time.monotonic()
                try:
                    process = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=args.timeout,
                                             cwd=workspace, env=environment)
                    output, timed_out = process.stdout, False
                except subprocess.TimeoutExpired:
                    output, timed_out = "", True
                seconds = round(time.monotonic() - started, 1)
                answer = answer_file.read_text() if answer_file.exists() else output
                failures, own_reasons = (["The agent timed out."], None) if timed_out else finish(answer)
                if not meter.requests:
                    failures = ["The agent made no requests."]
                rows.append({
                    "task": task.id, "run": run, "correct": not failures, "failures": failures, **meter.usage(), "seconds": seconds,
                    "requests": [list(request) for request in meter.requests], "answer": answer.strip(), "own_reasons": own_reasons,
                    "audit": [] if args.control else [line for line in meter.store.audit(admin(), limit=10000) if "  system  " not in line],
                })
                print(f"{task.id} run {run}: {'correct' if not failures else '; '.join(failures)} "
                      f"({rows[-1]['calls']} calls, {rows[-1]['rejected']} rejected, {seconds}s)", flush=True)
    print()
    reasons = {True: "yes", False: "no", None: "n/a"}
    print(table(["Task", "Run", "Correct", "Calls", "Rejected", "Bytes read", "Rough tokens", "Seconds", "Gave its own reasons"], [
        (row["task"], row["run"], "yes" if row["correct"] else "no", row["calls"], row["rejected"], f"{row['bytes_read']:,}",
         f"{tokens(row['bytes_read']):,}", row["seconds"], reasons[row["own_reasons"]]) for row in rows]))
    print(f"\n{sum(row['correct'] for row in rows)} of {len(rows)} task runs correct, {sum(row['calls'] for row in rows)} calls, "
          f"{sum(row['rejected'] for row in rows)} rejected.")
    save(f"agent-{domain.WORLD}-{'control-' if args.control else ''}{args.label}.json", rows)


def reason_given(store, type_name, record_id):
    """Whether the last change carries the agent's own reason, not the server's default."""
    return store.history(admin(), type_name, record_id)[-1]["reason"] != "Changed through the World client"


def changed_records(store):
    """Records an actor other than the seed has written."""
    return [record for record in store.list_records(admin())
            if any(event["actor"] != "system" for event in store.history(admin(), record["type"], record["id"]))]


def main():
    parser = argparse.ArgumentParser(prog="python3 -m bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="mode")
    runner = commands.add_parser("agent", help="Run the tasks with an agent; this uses that agent's model account")
    commands.add_parser("control", help="Measure the EspoCRM control without a model")
    runner.add_argument("--command", help="Agent command. It receives the prompt on standard input and WORLD_ANSWER_FILE in its "
                        "environment. Its answer is that file, or else its standard output. Default: codex exec")
    runner.add_argument("--control", action="store_true", help="Run against EspoCRM instead of World")
    runner.add_argument("--repeat", type=int, default=1, help="Run every task this many times")
    runner.add_argument("--task", action="append", help="Run only this task; repeatable")
    runner.add_argument("--label", default="codex", help="Name for the saved results file")
    runner.add_argument("--timeout", type=int, default=600)
    for command in (parser, *commands.choices.values()):
        command.add_argument("--domain", choices=list(DOMAINS), default=argparse.SUPPRESS, help="Which world and tasks to use; default crm")
    args = parser.parse_args()
    global domain
    domain = DOMAINS[getattr(args, "domain", "crm")]
    {"agent": agent, "control": control}.get(args.mode, report)(args)


if __name__ == "__main__":
    main()
