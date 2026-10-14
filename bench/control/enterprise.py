"""The control at company size: the lab plus the same 197 generated types and million records in EspoCRM.

    python3 -m bench.control.enterprise setup    create the entities, fields, links, rules, and role rights
    python3 -m bench.control.enterprise load     write the generated records straight into EspoCRM's database

Loading goes straight into MariaDB, as `bench.scale build` goes straight into
SQLite, because a million API requests would take hours. EspoCRM keeps no
history for these rows, where World keeps one event for each.
"""

import argparse
from hashlib import sha1
import re
import subprocess
import time

from . import lab
from .espocrm import Api, Meter, now  # noqa: F401  (used by the runner)
from .lab import TASKS, checks, reset  # noqa: F401  (the lab's tasks run unchanged)
from ..scale import ROOT, generate
from worlds.enterprise import DEPARTMENTS, GENERATED, SITES, STATES, TYPES

COMPOSE = ["docker", "compose", "-f", str(ROOT / "bench" / "control" / "compose.yaml")]
LOADED_AT = "2026-10-01 00:00:00"  # earlier than anything the API writes, so a reset can tell loaded rows apart
FIELDS = [
    {"name": "title", "type": "varchar", "maxLength": 200, "required": True},
    {"name": "state", "type": "enum", "options": STATES, "default": "draft", "required": True},
    {"name": "site", "type": "enum", "options": SITES, "required": True},
    {"name": "amount", "type": "int", "min": 0},
    {"name": "due", "type": "date"},
    {"name": "closedOn", "type": "date"},
]
CLOSED_RULE = """if (state == 'closed' && closedOn == null) {
    recordService\\throwBadRequest('A closed record needs its closed-on date.');
}"""


def table(entity):
    """EspoCRM's table for an entity, as its ORM names it."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", entity).lower()


def espo_id(world_id):
    return sha1(world_id.encode()).hexdigest()[:17]


def setup():
    """Create every generated entity once, then give the lab's roles rights over them. Returns the lab's users."""
    users = lab.setup()
    admin = Api()
    scopes = admin.ok("GET", "Metadata")["scopes"]
    started, made = time.perf_counter(), 0
    for name in GENERATED:
        if "C" + name in scopes:
            continue
        department = TYPES[name]["description"].split(":")[0]
        admin.ok("POST", "EntityManager/action/createEntity", {
            "name": name, "labelSingular": name, "labelPlural": name + "s", "type": "Base", "stream": False})
        for field in FIELDS:
            admin.ok("POST", f"Admin/fieldManager/C{name}", {**field, "label": field["name"]})
        previous = TYPES[name]["fields"].get("related", {}).get("target")
        if previous:
            admin.ok("POST", "EntityManager/action/createLink", {
                "entity": "C" + name, "entityForeign": "C" + previous, "link": "related", "linkForeign": "followers" + name,
                "label": "Related", "labelForeign": name + "s", "linkType": "manyToOne"})
        admin.ok("POST", "EntityManager/action/formula", {"scope": "C" + name, "data": {"beforeSaveApiScript": CLOSED_RULE}})
        made += 1
        print(f"{made} entities created ({department}), {time.perf_counter() - started:.0f}s", flush=True)
    for role_name, rights in (("Lab technician", {"create": "yes", "read": "all", "edit": "own", "delete": "no", "stream": "all"}),
                              ("Lab auditor", {"create": "no", "read": "all", "edit": "no", "delete": "no", "stream": "all"})):
        role = admin.all("Role", name=role_name)[0]
        data = {**admin.ok("GET", f"Role/{role['id']}")["data"], **{"C" + name: rights for name in GENERATED}}
        admin.ok("PUT", f"Role/{role['id']}", {"data": data})
    admin.call("POST", "Admin/rebuild")
    return users


def quote(value):
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def rows(count, owner):
    """The rows of bench.scale's generated records, table by table, with EspoCRM's IDs."""
    for type_name, record_id, data in generate(count):
        if type_name == "Person":
            continue
        common = {"id": espo_id(record_id), "deleted": 0, "created_at": LOADED_AT, "modified_at": LOADED_AT, "assigned_user_id": owner}
        if type_name == "Instrument":
            yield "c_instrument", {**common, "name": data["tag"], "kind": data["kind"], "site": data["site"],
                                   "state": data["state"], "interval_days": data["interval_days"]}
        elif type_name == "Calibration":
            yield "c_calibration", {**common, "name": f"{data['instrument']} calibration", "instrument_id": espo_id(data["instrument"]),
                                    "performed_on": data["performed_on"], "result": data["result"], "drift_ppm": data["drift_ppm"],
                                    "certificate": data["certificate"]}
        elif type_name == "Deviation":
            yield "c_deviation", {**common, "name": data["summary"], "instrument_id": espo_id(data["instrument"]), "severity": data["severity"],
                                  "disposition": data["disposition"], "closed_on": data["closed_on"]}
        else:
            row = {**common, "name": data["code"], "title": data["title"], "state": data["state"], "site": data["site"],
                   "amount": data["amount"], "due": data["due"], "closed_on": data["closed_on"]}
            if "related" in data:
                row["related_id"] = espo_id(data["related"])
            yield "c_" + table(type_name), row


def load(count):
    owner = Api().all("User", userName="lena")[0]["id"]  # generated records belong to someone other than the agent's person
    started = time.perf_counter()
    process = subprocess.Popen([*COMPOSE, "exec", "-T", "db", "mariadb", "-uespocrm", "-pcontrol-db", "espocrm"],
                               stdin=subprocess.PIPE, text=True)
    batch, current, written = [], None, 0

    def flush():
        if batch:
            columns = list(batch[0])
            process.stdin.write(f"INSERT INTO {current} ({', '.join(columns)}) VALUES "
                                + ", ".join("(" + ", ".join(quote(row[column]) for column in columns) + ")" for row in batch) + ";\n")
            batch.clear()

    process.stdin.write("SET autocommit = 0; SET unique_checks = 0; SET foreign_key_checks = 0;\n")
    for name, row in rows(count, owner):
        if name != current or len(batch) == 1000 or (batch and list(batch[0]) != list(row)):
            flush()
            current = name
        batch.append(row)
        written += 1
        if written % 100000 == 0:
            process.stdin.write("COMMIT;\n")
            print(f"{written:,} rows, {time.perf_counter() - started:.0f}s", flush=True)
    flush()
    process.stdin.write("COMMIT;\n")
    process.stdin.close()
    if process.wait():
        raise SystemExit("MariaDB refused the load.")
    print(f"Loaded {written:,} rows in {time.perf_counter() - started:.0f}s")


def main():
    parser = argparse.ArgumentParser(prog="python3 -m bench.control.enterprise", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("setup")
    loader = commands.add_parser("load")
    loader.add_argument("--records", type=int, default=1_000_000)
    args = parser.parse_args()
    setup() if args.mode == "setup" else load(args.records)


if __name__ == "__main__":
    main()
