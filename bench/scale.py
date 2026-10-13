"""Build a company-sized world and time it.

    python3 -m bench.scale build --records 1000000    generate the database under artifacts/bench/scale
    python3 -m bench.scale measure                    size and single operations
    python3 -m bench.scale clients                    many clients at once over HTTP

Generated records are written straight into the database, with the same
history and reference index the engine would have written, because creating a
million records one request at a time would take hours.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import random
import shutil
import sqlite3
import statistics
from tempfile import TemporaryDirectory
import threading
import time

from world.schema import references
from world.server import AccessTokens, WorldHTTPServer
from world.store import WorldStore
import worlds
from worlds.enterprise import GENERATED, SITES, STATES

from .harness import Client, NoPlanner

ROOT = Path(__file__).resolve().parent.parent
HOME = ROOT / "artifacts" / "bench" / "scale" / "enterprise"
NOW = "2026-10-13T00:00:00.000Z"


def generate(count):
    """Yield (type, id, data). A tenth are instruments, three tenths calibrations, the rest spread over the generated types."""
    random.seed(7)
    people = [f"staff-{index}" for index in range(500)]
    for index, person in enumerate(people):
        yield "Person", person, {"name": f"Staff {index}", "email": f"staff{index}@example.test", "role": "technician"}
    instruments = count // 10
    for index in range(instruments):
        site = SITES[index % 3]
        # Leeds keeps no generated quarantined instruments, so the lab's counting task has the same answer at any size.
        state = random.choice(["in-service", "retired"] if site == "leeds" else ["in-service", "in-service", "quarantined", "retired"])
        yield "Instrument", f"gen-{index}", {"tag": f"GEN-{index:06d}", "kind": random.choice(["balance", "thermometer", "pipette", "gauge"]),
                                            "site": site, "state": state, "custodian": random.choice(people), "interval_days": random.choice([90, 180, 365])}
    for index in range(count * 3 // 10):
        result = random.choice(["pass", "pass", "pass", "adjusted", "fail"])
        yield "Calibration", f"gen-cal-{index}", {
            "instrument": f"gen-{random.randrange(instruments)}", "performed_on": f"2026-{random.randint(1, 9):02d}-{random.randint(1, 28):02d}",
            "technician": random.choice(people), "result": result, "drift_ppm": random.randint(0, 5000),
            "certificate": f"CERT-{index:07d}" if result == "pass" else None}
    for index in range(count // 20):
        closed = random.random() < 0.7
        yield "Deviation", f"gen-dev-{index}", {
            "instrument": f"gen-{random.randrange(instruments)}", "calibration": None, "severity": random.choice(["minor", "minor", "major", "critical"]),
            "disposition": random.choice(["use-as-is", "rework", "scrap"]) if closed else "open", "summary": f"Generated deviation {index}.",
            "closed_on": "2026-09-01" if closed else None, "raised_by": random.choice(people)}
    each = (count - instruments - count * 3 // 10 - count // 20) // len(GENERATED)
    for type_name in GENERATED:
        previous = worlds.load("enterprise").types[type_name]["fields"].get("related", {}).get("target")
        for index in range(each):
            state = random.choice(STATES)
            data = {"code": f"{type_name[:3].upper()}-{index:05d}", "title": f"{type_name} {index}", "state": state, "site": random.choice(SITES),
                    "owner": random.choice(people), "amount": random.randint(0, 100000), "due": "2026-12-01",
                    "closed_on": "2026-09-15" if state == "closed" else None}
            if previous:
                data["related"] = f"{previous.lower()}-gen-{random.randrange(each)}"
            yield type_name, f"{type_name.lower()}-gen-{index}", data


def build(args):
    if HOME.exists() and any(HOME.iterdir()):
        raise SystemExit(f"{HOME} already holds a database. Move it away to build another.")
    HOME.mkdir(parents=True, exist_ok=True)
    definition = worlds.load("enterprise")
    started = time.perf_counter()
    WorldStore(HOME / "world.sqlite3", definition).close()
    connection = sqlite3.connect(HOME / "world.sqlite3")
    connection.execute("PRAGMA synchronous = OFF")
    compact = lambda value: json.dumps(value, separators=(",", ":"))
    records, events, links, written = [], [], [], 0

    def flush():
        connection.executemany("INSERT INTO records (type, id, data, version, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)", records)
        connection.executemany("INSERT INTO events (type, record_id, action, at, actor, requested_by, reason, evidence, after_state) "
                               "VALUES (?, ?, 'create', ?, 'system', 'system', 'Generated for the scale test.', '[]', ?)", events)
        connection.executemany("INSERT INTO refs (source_type, source_id, field, target_type, target_id) VALUES (?, ?, ?, ?, ?)", links)
        connection.commit()
        records.clear(), events.clear(), links.clear()

    for type_name, record_id, data in generate(args.records):
        records.append((type_name, record_id, compact(data), NOW, NOW))
        events.append((type_name, record_id, NOW, compact({"id": record_id, "type": type_name, "data": data, "version": 1, "created_at": NOW, "updated_at": NOW})))
        links.extend((type_name, record_id, field, target, target_id) for field, target, target_id in references(definition.types, type_name, data))
        written += 1
        if len(records) == 50000:
            flush()
            print(f"{written:,} records, {time.perf_counter() - started:.0f}s", flush=True)
    flush()
    connection.execute("ANALYZE")  # lets SQLite choose the narrower index when a query names two fields
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    print(f"Built {written:,} records of {len(definition.types)} types in {time.perf_counter() - started:.0f}s: "
          f"{sum(file.stat().st_size for file in HOME.iterdir()) / 1e6:,.0f} MB")


def timed(action, repeat=20):
    samples = []
    for index in range(repeat):
        started = time.perf_counter()
        action(index)
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


@contextmanager
def working_copy():
    """A copy of the built database, so that measuring never changes what the agent tasks start from."""
    with TemporaryDirectory(prefix="world-scale-", dir=HOME.parent) as directory:
        shutil.copyfile(HOME / "world.sqlite3", Path(directory) / "world.sqlite3")
        yield Path(directory)


def measure(args):
    with working_copy() as home:
        measure_in(home)


def measure_in(home):
    definition = worlds.load("enterprise")
    store = WorldStore(home / "world.sqlite3", definition)
    connection = sqlite3.connect(home / "world.sqlite3")
    total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    connection.close()
    size = (HOME / "world.sqlite3").stat().st_size
    print(f"## {total:,} records of {len(definition.types)} types\n")
    print(f"Database on disk, with history and indexes: {size / 1e6:,.0f} MB ({size / total:,.0f} bytes a record)\n")
    operations = {
        "Describe the world (index of types)": lambda i: store.describe("kofi"),
        "Describe three types in full": lambda i: store.describe("kofi", ["Instrument", "Calibration", "Deviation"]),
        "Read one record by ID": lambda i: store.get_record("kofi", "Instrument", f"gen-{i * 37}"),
        "Find one record by an exact field": lambda i: store.find("kofi", "Instrument", where=f"record.tag == 'GEN-{i * 91:06d}'"),
        "Find through a reference": lambda i: store.find("kofi", "Deviation", where="record.instrument.tag == 'PIP-007' and record.disposition == 'open'"),
        "Count by two exact fields": lambda i: store.find("kofi", "Instrument", where="record.site == 'leeds' and record.state == 'quarantined'", count=True),
        "First 100 of a common value": lambda i: store.find("kofi", "Instrument", where="record.site == 'cork'"),
        "Count a common value": lambda i: store.find("kofi", "Instrument", where="record.site == 'cork'", count=True),
        "Search part of a text, no match": lambda i: store.find("kofi", "Instrument", where="'nowhere' in record.tag", count=True),
        "Update one record": lambda i: store.update("ada", "Instrument", f"gen-{i * 53 + 1}", {"interval_days": 100 + i},
                                                    store.get_record("ada", "Instrument", f"gen-{i * 53 + 1}")["version"]),
        "Create one record": lambda i: store.create("kofi", "Calibration", {"instrument": "bal-014", "performed_on": "2026-10-13", "result": "pass"}),
        "Audit lines for one record": lambda i: store.audit("ada", "Instrument", f"gen-{i * 53 + 1}"),
    }
    print("| Operation | Median |\n| --- | --- |")
    for name, action in operations.items():
        repeat = 3 if "common" in name or "Search" in name else 20
        print(f"| {name} | {timed(action, repeat):,.1f} ms |", flush=True)
    store.close()


def concurrency(definition, args):
    with working_copy() as home:
        concurrency_in(home, definition, args)


def concurrency_in(home, definition, args):
    """Many clients at once over HTTP, each reading, searching, and sometimes writing."""
    store = WorldStore(home / "world.sqlite3", definition)
    access = AccessTokens(home, definition.actors)
    store.synchronous = args.synchronous
    server = WorldHTTPServer(("127.0.0.1", 0), store, access, NoPlanner())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"\nMany clients at once, synchronous {args.synchronous}")
    print("\n| Clients at once | Requests a second | Median | 95th percentile | Refused |\n| --- | --- | --- | --- | --- |")
    try:
        for clients in (1, 10, 50):
            def work(seed):
                client, chance, spent, refused = Client(server.server_port, access.tokens["kofi"]), random.Random(seed), [], 0
                for _ in range(args.requests):
                    pick, number = chance.random(), chance.randrange(90000)
                    started = time.perf_counter()
                    if pick < 0.5:
                        status, _ = client.call("GET", f"/api/records/Instrument/gen-{number}")
                    elif pick < 0.9:
                        status, _ = client.call("POST", "/api/query", {"type": "Instrument", "where": f"record.tag == 'GEN-{number:06d}'"})
                    else:
                        status, _ = client.call("POST", "/api/records/Calibration", {
                            "data": {"instrument": f"gen-{number}", "performed_on": "2026-10-13", "result": "pass"}, "reason": "Scale test."})
                    spent.append((time.perf_counter() - started) * 1000)
                    refused += status >= 400
                return spent, refused

            started = time.perf_counter()
            with ThreadPoolExecutor(clients) as pool:
                results = list(pool.map(work, range(clients)))
            elapsed = time.perf_counter() - started
            spent = sorted(sample for samples, _ in results for sample in samples)
            print(f"| {clients} | {len(spent) / elapsed:,.0f} | {statistics.median(spent):,.1f} ms | "
                  f"{spent[int(len(spent) * 0.95)]:,.1f} ms | {sum(refused for _, refused in results)} |", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def main():
    parser = argparse.ArgumentParser(prog="python3 -m bench.scale", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="mode", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--records", type=int, default=1_000_000)
    measurer = commands.add_parser("measure")
    crowd = commands.add_parser("clients", help="Many clients at once over HTTP")
    crowd.add_argument("--requests", type=int, default=100, help="Requests each client makes")
    crowd.add_argument("--synchronous", choices=["FULL", "NORMAL"], default="FULL")
    args = parser.parse_args()
    {"build": build, "measure": measure, "clients": lambda args: concurrency(worlds.load("enterprise"), args)}[args.mode](args)


if __name__ == "__main__":
    main()
