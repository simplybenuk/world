"""Build the issue-tracker world from real GitHub data, and time it.

    python3 -m bench.github download    fetch the Parquet files into artifacts/data/open-github-issues
    python3 -m bench.github build       write the database under artifacts/bench/tracker
    python3 -m bench.github verify      judge every loaded issue by the world's own constraints
    python3 -m bench.github measure     size and single operations, on a copy

The data is the open-index/open-github-issues dataset on Hugging Face, licensed
ODC-By 1.0: seven repositories, every issue, pull request, and comment.
Records are written straight into the database, with the history and reference
index the engine would have written, as `bench.scale build` does.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import time
import urllib.request

from world.schema import WorldError, references, validate_data
from world.store import WorldStore
import worlds

from .parquet import ParquetFile
from .scale import timed

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "artifacts" / "data" / "open-github-issues"
HOME = ROOT / "artifacts" / "bench" / "tracker"
DATASET = "open-index/open-github-issues"
SOURCE_URL = f"https://huggingface.co/datasets/{DATASET}/resolve/main/data"
# Repository -> the project's ID in the world.
REPOS = {
    "facebook/react": "react", "microsoft/TypeScript": "typescript", "redis/redis": "redis", "etcd-io/etcd": "etcd",
    "duckdb/duckdb": "duckdb", "vuejs/core": "vue-core", "vuejs/docs": "vue-docs",
}
TABLES = ("issues", "comments", "pull_requests")
MAINTAINING = ("OWNER", "MEMBER", "COLLABORATOR")
REASON = f"Imported from the {DATASET} dataset on Hugging Face."


def person_id(login):
    """GitHub logins are letters, digits, and hyphens, compared without case. Bots end in [bot]."""
    return login.lower().replace("[bot]", ".bot")


def download(args):
    for table in TABLES:
        for repo in REPOS:
            target = DATA / "raw" / table / repo / "0.parquet"
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            print(f"{table} {repo}", flush=True)
            with urllib.request.urlopen(f"{SOURCE_URL}/{table}/{repo}/0.parquet") as response, open(target.with_suffix(".part"), "wb") as file:
                shutil.copyfileobj(response, file)
            target.with_suffix(".part").rename(target)
    sums = {str(path.relative_to(DATA / "raw")): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((DATA / "raw").rglob("0.parquet"))}
    (DATA / "SHA256SUMS.json").write_text(json.dumps(sums, indent=2) + "\n")
    print(f"{len(sums)} files, {sum(path.stat().st_size for path in (DATA / 'raw').rglob('0.parquet')) / 1e6:,.0f} MB")


def read(table, repo, columns):
    return ParquetFile(DATA / "raw" / table / repo / "0.parquet").read(columns)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "label"


def generate(notes):
    """Yield (type, id, data, source URL), counting every adjustment to the raw data in `notes`."""
    people, maintainers = {}, defaultdict(set)

    def person(login):
        people.setdefault(person_id(login), login)
        return person_id(login)

    for repo, project in REPOS.items():
        merged = {row["number"]: row["merged_at"] for row in read("pull_requests", repo, ["number", "merged_at"])}
        labels, milestones, numbers = {}, {}, set()
        issues = []
        for row in read("issues", repo, ["number", "is_pull_request", "title", "body", "state", "state_reason", "author",
                                         "created_at", "closed_at", "labels", "assignees", "milestone_title",
                                         "milestone_number", "locked"]):
            kind = "pull_request" if row["is_pull_request"] else "issue"
            if row["state"] == "open":
                state, reason = "open", None
                notes["open issues marked reopened, loaded as open"] += row["state_reason"] == "reopened"
            elif kind == "pull_request":
                state, reason = ("merged" if merged.get(row["number"]) else "closed"), None
            else:
                state, reason = "closed", row["state_reason"] or None
                notes["closed issues with no close reason"] += reason is None
            label_ids = []
            for name in json.loads(row["labels"]) or []:
                if name not in labels:
                    identifier, suffix = f"{project}-label-{slug(name)}", 2
                    while identifier in labels.values():
                        identifier, suffix = f"{project}-label-{slug(name)}-{suffix}", suffix + 1
                    labels[name] = identifier
                if labels[name] not in label_ids:
                    label_ids.append(labels[name])
            assignees = list(dict.fromkeys(person(login) for login in json.loads(row["assignees"]) or []))
            milestone = None
            if row["milestone_number"]:
                milestone = f"{project}-milestone-{row['milestone_number']}"
                milestones[milestone] = (row["milestone_title"], row["milestone_number"])
            numbers.add(row["number"])
            issues.append((f"{project}-{row['number']}", {
                "project": project, "number": row["number"], "kind": kind, "title": row["title"], "body": row["body"] or "",
                "state": state, "close_reason": reason, "author": person(row["author"]), "assignees": assignees,
                "labels": label_ids, "milestone": milestone, "locked": bool(row["locked"]),
                "opened_on": row["created_at"][:10], "closed_on": row["closed_at"][:10] if state != "open" else None,
            }, f"https://github.com/{repo}/{'pull' if kind == 'pull_request' else 'issues'}/{row['number']}"))
        for name, identifier in labels.items():
            yield "Label", identifier, {"project": project, "name": name}, f"https://github.com/{repo}/labels"
        for identifier, (title, number) in milestones.items():
            yield "Milestone", identifier, {"project": project, "title": title, "number": number}, f"https://github.com/{repo}/milestone/{number}"
        yield from (("Issue", identifier, data, url) for identifier, data, url in issues)
        del issues
        for row in read("comments", repo, ["id", "issue_number", "author", "body", "created_at", "author_association"]):
            if row["issue_number"] not in numbers:
                notes["comments on an issue missing from the dataset, left out"] += 1
                continue
            if row["author_association"] in MAINTAINING and not row["author"].endswith("[bot]"):
                maintainers[project].add(person(row["author"]))
            yield "Comment", f"comment-{row['id']}", {
                "issue": f"{project}-{row['issue_number']}", "author": person(row["author"]), "body": row["body"],
                "posted_on": row["created_at"][:10]}, f"https://github.com/{repo}/issues/{row['issue_number']}#issuecomment-{row['id']}"
        yield "Project", project, {"name": repo, "description": f"The GitHub repository {repo}.",
                                   "maintainers": sorted(maintainers[project])}, f"https://github.com/{repo}"
    for identifier, login in sorted(people.items()):
        yield "Person", identifier, {"login": login, "role": "member", "bot": login.endswith("[bot]")}, f"https://github.com/{login.removesuffix('[bot]')}"


def build(args):
    if HOME.exists() and any(HOME.iterdir()):
        raise SystemExit(f"{HOME} already holds a database. Move it away to build another.")
    HOME.mkdir(parents=True, exist_ok=True)
    definition = worlds.load("tracker")
    started = time.perf_counter()
    WorldStore(HOME / "world.sqlite3", definition).close()
    connection = sqlite3.connect(HOME / "world.sqlite3")
    connection.execute("PRAGMA synchronous = OFF")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    compact = lambda value: json.dumps(value, separators=(",", ":"))
    records, events, links = [], [], []
    notes, counts, rejected = Counter(), Counter(), Counter()

    def flush():
        connection.executemany("INSERT INTO records (type, id, data, version, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)", records)
        connection.executemany("INSERT INTO events (type, record_id, action, at, actor, requested_by, reason, evidence, after_state) "
                               "VALUES (?, ?, 'create', ?, 'system', 'system', ?, ?, ?)", events)
        connection.executemany("INSERT INTO refs (source_type, source_id, field, target_type, target_id) VALUES (?, ?, ?, ?, ?)", links)
        connection.commit()
        records.clear(), events.clear(), links.clear()

    for type_name, record_id, data, url in generate(notes):
        if type_name == "Person" and record_id == "ben":
            raise SystemExit("A GitHub login collides with the world's administrator, ben.")
        try:
            data = validate_data(definition.types, type_name, data)
        except WorldError as error:
            rejected[f"{type_name}: {error.message}"] += 1
            continue
        records.append((type_name, record_id, compact(data), now, now))
        events.append((type_name, record_id, now, REASON, compact([{"source": url}]),
                       compact({"id": record_id, "type": type_name, "data": data, "version": 1, "created_at": now, "updated_at": now})))
        links.extend((type_name, record_id, field, target, target_id) for field, target, target_id in references(definition.types, type_name, data))
        counts[type_name] += 1
        if len(records) == 20000:
            flush()
            print(f"{sum(counts.values()):,} records, {time.perf_counter() - started:.0f}s", flush=True)
    flush()
    connection.execute("ANALYZE")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    summary = {"records": dict(counts), "adjusted": dict(notes), "rejected": dict(rejected), "built_at": now,
               "seconds": round(time.perf_counter() - started), "bytes": (HOME / "world.sqlite3").stat().st_size}
    (HOME / "build.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


def verify(args):
    """Judge every loaded issue by the definition's constraints, through the engine, on a copy."""
    with working_copy() as home:
        store = WorldStore(home / "world.sqlite3", worlds.load("tracker"))
        failures, checked, started = Counter(), 0, time.perf_counter()
        with store._transaction() as connection:
            for record in store._all(connection, "Issue"):
                try:
                    store._check_constraints(connection, None, record, None, "create")
                except WorldError as error:
                    failures[error.message] += 1
                checked += 1
        store.close()
    print(f"Checked {checked:,} issues in {time.perf_counter() - started:.0f}s.")
    for message, count in failures.most_common():
        print(f"{count:,}  {message}")
    if not failures:
        print("Every issue satisfies every constraint.")


@contextmanager
def working_copy():
    """A copy of the built database, so that measuring never changes what the agent tasks start from."""
    with TemporaryDirectory(prefix="world-tracker-", dir=HOME.parent) as directory:
        shutil.copyfile(HOME / "world.sqlite3", Path(directory) / "world.sqlite3")
        yield Path(directory)


def measure(args):
    with working_copy() as home:
        store = WorldStore(home / "world.sqlite3", worlds.load("tracker"))
        connection = sqlite3.connect(home / "world.sqlite3")
        total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        parts = {name: connection.execute("SELECT SUM(pgsize) FROM dbstat WHERE name LIKE ?", (pattern,)).fetchone()[0] or 0
                 for name, pattern in (("Records", "records"), ("History", "events"), ("Field indexes", "field_%"),
                                       ("Reference index", "refs%"), ("Other indexes", "sqlite_autoindex_records%"))} \
            if connection.execute("SELECT 1 FROM pragma_compile_options WHERE compile_options = 'ENABLE_DBSTAT_VTAB'").fetchone() else {}
        # Issue numbers have gaps where GitHub issues were deleted or moved, so sample IDs that exist.
        sample = lambda project: [row[0] for row in connection.execute(
            "SELECT id FROM records WHERE type = 'Issue' AND json_extract(data, '$.project') = ? ORDER BY id LIMIT 20 OFFSET 500", (project,))]
        redis, typescript = sample("redis"), sample("typescript")
        connection.close()
        size = (HOME / "world.sqlite3").stat().st_size
        print(f"## {total:,} records\n\nDatabase on disk, with history and indexes: {size / 1e6:,.0f} MB ({size / total:,.0f} bytes a record)\n")
        if parts:
            print("| Part | Size |\n| --- | --- |")
            for name, value in parts.items():
                print(f"| {name} | {value / 1e6:,.0f} MB |")
            print()
        operations = {
            "Describe the world": lambda i: store.describe("sundb"),
            "Read one issue by ID": lambda i: store.get_record("sundb", "Issue", redis[i]),
            "Find an issue by project and number": lambda i: store.find("sundb", "Issue", where=f"record.project == 'redis' and record.number == {redis[i].split('-')[1]}"),
            "Comments on one issue": lambda i: store.find("sundb", "Comment", where=f"record.issue == '{typescript[i]}'"),
            "Count open issues in one project": lambda i: store.find("sundb", "Issue", where="record.project == 'redis' and record.state == 'open' and record.kind == 'issue'", count=True),
            "Count issues with one label": lambda i: store.find("sundb", "Issue", where="record.project == 'redis' and 'redis-label-release-notes' in record.labels", count=True),
            "First 100 of one author's issues": lambda i: store.find("sundb", "Issue", where="record.author == 'oranagra'"),
            "Search titles in one project": lambda i: store.find("sundb", "Issue", where="record.project == 'redis' and 'cluster' in record.title", count=True),
            "Search titles everywhere": lambda i: store.find("sundb", "Issue", where="'memory leak' in record.title", count=True),
            "Comment on an issue": lambda i: store.create("sundb", "Comment", {"issue": redis[i], "body": "Measuring.", "posted_on": "2026-10-14"}),
            "Label an issue": lambda i: store.update("sundb", "Issue", redis[i], {"milestone": None, "labels": ["redis-label-release-notes"]},
                                                     store.get_record("sundb", "Issue", redis[i])["version"]),
            "Audit lines for one issue": lambda i: store.audit("ben", "Issue", redis[i]),
            # Before the engine skipped constraints that cannot read the change, this re-checked every redis issue: 70 s.
            "Add a maintainer to a project": lambda i: store.update("ben", "Project", "redis", {
                "maintainers": store.get_record("ben", "Project", "redis")["data"]["maintainers"] + ["oresoftware", "edison1105", "trusktr"][i:i + 1]},
                store.get_record("ben", "Project", "redis")["version"]),
        }
        print("| Operation | Median |\n| --- | --- |")
        for name, action in operations.items():
            repeat = 3 if "Count" in name or "Search" in name or "100" in name or "maintainer" in name else 10
            print(f"| {name} | {timed(action, repeat):,.1f} ms |", flush=True)
        store.close()


def main():
    parser = argparse.ArgumentParser(prog="python3 -m bench.github", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="mode", required=True)
    for name in ("download", "build", "verify", "measure"):
        commands.add_parser(name)
    args = parser.parse_args()
    {"download": download, "build": build, "verify": verify, "measure": measure}[args.mode](args)


if __name__ == "__main__":
    main()
