"""Supplemental meanings for the fictional CRM used in the third experiment."""

from copy import deepcopy
import json
from pathlib import Path

from world.definition import WorldDefinition

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEFINITION = ROOT / "definitions" / "crm.world.json"


def load_fixture(distractors=0):
    """Add repeatable, unrelated fictional accounts for a small scale comparison."""
    if type(distractors) is not int or not 0 <= distractors <= 1000:
        raise ValueError("distractors must be an integer between 0 and 1000")
    document = json.loads(DEFAULT_DEFINITION.read_text())
    for index in range(distractors):
        organization = f"synthetic-{index:04d}"
        contact = organization + "-contact"
        opportunity = organization + "-opportunity"
        document["seed"].extend([
            {"type": "Organization", "id": organization, "data": {
                "name": f"Fictional account {index}",
                "description": "Unrelated account used only to measure the experiment.",
                "members": ["ben", "maya", "alex"], "visibility": "team",
            }},
            {"type": "Contact", "id": contact, "data": {
                "name": f"Fictional contact {index}",
                "email": f"contact-{index}@synthetic.example", "organization": organization,
            }},
            {"type": "Opportunity", "id": opportunity, "data": {
                "name": f"Fictional opportunity {index}", "organization": organization,
                "contact": contact, "owner": "maya", "stage": "qualified",
                "amount_minor": 100000 + index, "currency": "GBP",
                "close_date": {"relative_days": 10},
            }},
            {"type": "Interaction", "id": organization + "-interaction", "data": {
                "body": f"Fictional conversation {index}; unrelated to Northstar.",
                "contact": contact, "opportunity": opportunity, "author": "maya",
                "occurred": {"relative_days": -1},
            }},
        ])
    return WorldDefinition(document)


def describe(store, actor_id):
    """Keep enforcement in the definition; add discoverable domain explanations."""
    description = deepcopy(store.describe(actor_id))
    meanings = json.loads(Path(__file__).with_name("meanings.json").read_text())
    for type_name, fields in meanings["fields"].items():
        if type_name not in description["types"]:
            continue
        for field_name, meaning in fields.items():
            if field_name in description["types"][type_name]["fields"]:
                description["types"][type_name]["fields"][field_name]["meaning"] = meaning
    description["experiment"] = {
        "id": "agent-world",
        "status": "prototype",
        "data": "fictional CRM",
        "meanings": "Supplemental descriptions; the World definition and store enforce validity.",
    }
    # Both comparison contracts receive the same size hint. This count comes
    # from authorized current records and is guidance, not a snapshot promise.
    counts = {type_name: 0 for type_name in description["types"]}
    # Count permission-filtered roots without constructing every record's full
    # capability envelope. Both engines use this same discovery calculation.
    with store._transaction() as connection:
        actor = store._actor(connection, actor_id)
        for record in store._all(connection):
            if store._can_read(connection, actor, record):
                counts[record["type"]] += 1
    description["record_counts"] = {
        "visible_total": sum(counts.values()),
        "by_type": counts,
        "scope": "Authorized current records observed for discovery; query results may change.",
    }
    return description
