"""Isolated HTTP fixture for browser verification; never uses the deployed database."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client_gateway.planner import CodexPlanner
from world.server import AccessTokens, WorldHTTPServer
from world.store import WorldStore


class FixturePlanner:
    """Fixed responses exercise the renderer without consuming model usage."""

    available = True

    def plan(self, description, records, message, context=None):
        tasks = [record for record in records if record["type"] == "Task"]
        previous = (context or {}).get("view") or {}
        visual = "visual" in message.lower()
        website = "website" in message.lower()
        complete = "complete" in message.lower()
        selected = [record for record in tasks if record["data"]["project"] == "website"] if website else [record for record in tasks if record["data"]["owner"] == description["actor"]["id"] and record["data"]["status"] != "done"]
        if visual and previous.get("record_ids"):
            selected = [record for record in tasks if record["id"] in previous["record_ids"]]
        selected.sort(key=lambda record: record["data"].get("due") or "9999-12-31")
        view = {"title": "Website project" if website else "Your attention", "description": "Browser test fixture.",
                "layout": "board" if visual else "overview" if website else "list", "type": "Task",
                "record_ids": [record["id"] for record in selected], "fields": ["title", "project", "owner", "status", "due"],
                "group_by": "status" if visual else None}
        proposals = []
        if complete:
            task = next(record for record in tasks if record["id"] == "documentation")
            proposals = [{"operation": "update", "type": "Task", "id": task["id"], "changes": {"status": "done"},
                          "expected_version": task["version"], "reason": message}]
        return {"message": "Ready to save this change." if complete else "Here is your view.",
                "view": view, "proposals": proposals, "provider": "fixture"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    store = WorldStore(args.data_dir / "world.sqlite3")
    server = WorldHTTPServer(("127.0.0.1", 0), store, AccessTokens(args.data_dir), CodexPlanner() if args.live else FixturePlanner())
    print(json.dumps({"port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
