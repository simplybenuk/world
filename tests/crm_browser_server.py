"""Run the shared protocol against a temporary CRM world for browser checks."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client_gateway.planner import ClientError, CodexPlanner
from world import WorldDefinition
from world.server import AccessTokens, WorldHTTPServer
from world.store import WorldStore


class FixturePlanner:
    """Deterministic plans for these tests; --live uses the real Codex planner."""

    available = True

    def plan(self, description, records, message, context=None):
        opportunities = [record for record in records if record["type"] == "Opportunity"]
        actor_id = description["actor"]["id"]
        previous = (context or {}).get("view") or {}
        selected = [
            record for record in opportunities
            if record["data"].get("owner") == actor_id
            and record["data"]["stage"] not in ("won", "lost")
        ]
        selected.sort(key=lambda record: record["data"].get("close_date") or "9999-12-31")
        view = {
            "title": "Your open sales opportunities",
            "description": "A deterministic browser fixture, using the shared client.",
            "layout": "list", "type": "Opportunity",
            "record_ids": [record["id"] for record in selected],
            "fields": ["name", "organization", "owner", "stage", "amount_minor", "currency", "close_date"],
            "group_by": None,
        }
        proposals = []
        if message == "Show my open sales opportunities.":
            pass
        elif message == "Make this a pipeline board by stage.":
            if previous.get("type") == "Opportunity":
                ids = set(previous.get("record_ids", []))
                view["record_ids"] = [record["id"] for record in opportunities if record["id"] in ids]
            view.update({"layout": "board", "group_by": "stage"})
        elif message == "Move the Northstar renewal opportunity to proposal.":
            opportunity = next((record for record in opportunities if record["id"] == "northstar-renewal"), None)
            if opportunity is None:
                raise ClientError(404, "fixture_record_missing", "The fixture opportunity is not readable.")
            if previous.get("type") == "Opportunity":
                view = {**previous}
            proposals = [{
                "operation": "update", "type": "Opportunity", "id": opportunity["id"],
                "changes": {"stage": "proposal"}, "expected_version": opportunity["version"],
                "reason": message,
            }]
        else:
            raise ClientError(400, "fixture_message", "This browser fixture only accepts its three test requests. Use --live for arbitrary requests.")
        return {
            "message": "Review the stage change before saving." if proposals else "Here is your view.",
            "view": view, "proposals": proposals, "provider": "fixture",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    definition = WorldDefinition.load(ROOT / "definitions" / "crm.world.json")
    store = WorldStore(args.data_dir / "world.sqlite3", definition=definition)
    access = AccessTokens(args.data_dir, actors=definition.actor_ids)
    planner = CodexPlanner() if args.live else FixturePlanner()
    server = WorldHTTPServer(("127.0.0.1", 0), store, access, planner)
    print(json.dumps({"port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
