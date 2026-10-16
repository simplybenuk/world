"""An issue tracker for open-source projects, holding real public data.

The records come from seven GitHub repositories in the open-index/open-github-issues
dataset on Hugging Face (ODC-By 1.0). `python3 -m bench.github build` loads them.
The rules follow GitHub's: anyone may open an issue or comment, a project's
maintainers triage its issues, and only maintainers may comment on a locked one.
"""

from datetime import date
from typing import Any

from world.definition import Definition

ADMIN = "actor.role == 'admin'"
TRIAGE = f"{ADMIN} or actor.id in record.project.maintainers"
LAST_ADMIN = {"message": "The world must retain at least one admin.", "fields": ["role"], "code": "last_admin", "status": 409}
RELATIONSHIP = {"code": "invalid_relationship"}
LONG_TEXT = 262144  # GitHub's own limit is 65,536 characters, but older issues exceed it

TYPES: dict[str, dict[str, Any]] = {
    "Person": {
        "description": "Someone with a GitHub login who opened, commented on, or was assigned an issue. Their stored role governs their access here.",
        "fields": {
            "login": {"type": "text", "required": True, "min_length": 1, "max_length": 100},
            "role": {"type": "enum", "required": True, "values": ["admin", "member"], "default": "member", "write_rule": ADMIN},
            "bot": {"type": "boolean", "required": True, "default": False, "write_rule": ADMIN},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": ADMIN,
            "update": f"{ADMIN} or actor.id == record.id",
            "delete": ADMIN,
        },
        "constraints": [
            {"check": "count(p for p in Person if p.role == 'admin') >= 1", "on": ("update",), **LAST_ADMIN},
            {"check": "record.role != 'admin' or count(p for p in Person if p.role == 'admin') > 1", "on": ("delete",), **LAST_ADMIN},
        ],
    },
    "Project": {
        "description": "One GitHub repository, with the people who maintain it. Maintainers triage the project's issues.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 200},
            "description": {"type": "text", "default": "", "max_length": 2000},
            "maintainers": {
                "type": "list", "required": True, "items": {"type": "reference", "target": "Person"},
                "unique": True, "max_items": 1000, "write_rule": ADMIN,
            },
        },
        "rules": {
            "read": "actor.id is not None",
            "create": ADMIN,
            "update": f"{ADMIN} or actor.id in record.maintainers",
            "delete": ADMIN,
        },
    },
    "Label": {
        "description": "A label that a project's maintainers put on its issues.",
        "fields": {
            "project": {"type": "reference", "target": "Project", "required": True, "write_rule": ADMIN},
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 100},
        },
        "rules": {"read": "actor.id is not None", "create": TRIAGE, "update": TRIAGE, "delete": TRIAGE},
    },
    "Milestone": {
        "description": "A named target, such as a release, that a project's issues can be planned against.",
        "fields": {
            "project": {"type": "reference", "target": "Project", "required": True, "write_rule": ADMIN},
            "title": {"type": "text", "required": True, "min_length": 1, "max_length": 200},
            "number": {"type": "integer", "nullable": True, "default": None, "min": 1, "write_rule": ADMIN},
        },
        "rules": {"read": "actor.id is not None", "create": TRIAGE, "update": TRIAGE, "delete": TRIAGE},
    },
    "Issue": {
        "description": "An issue or pull request in one project. Its number is the one GitHub gave it, and is empty for issues opened here.",
        "fields": {
            "project": {"type": "reference", "target": "Project", "required": True, "write_rule": ADMIN},
            "number": {"type": "integer", "nullable": True, "default": None, "min": 1, "write_rule": ADMIN},
            "kind": {"type": "enum", "required": True, "values": ["issue", "pull_request"], "default": "issue", "write_rule": ADMIN},
            "title": {"type": "text", "required": True, "min_length": 1, "max_length": 1024},
            "body": {"type": "text", "default": "", "max_length": LONG_TEXT},
            "state": {"type": "enum", "required": True, "values": ["open", "closed", "merged"], "default": "open"},
            "close_reason": {"type": "enum", "nullable": True, "default": None, "values": ["completed", "not_planned", "duplicate"]},
            "author": {"type": "reference", "target": "Person", "required": True, "default": "$actor", "write_rule": ADMIN},
            "assignees": {"type": "list", "default": [], "items": {"type": "reference", "target": "Person"},
                          "unique": True, "max_items": 100, "write_rule": TRIAGE},
            "labels": {"type": "list", "default": [], "items": {"type": "reference", "target": "Label"},
                       "unique": True, "max_items": 100, "write_rule": TRIAGE},
            "milestone": {"type": "reference", "target": "Milestone", "nullable": True, "default": None, "write_rule": TRIAGE},
            "locked": {"type": "boolean", "required": True, "default": False, "write_rule": TRIAGE},
            "opened_on": {"type": "date", "required": True},
            "closed_on": {"type": "date", "nullable": True, "default": None},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": f"{ADMIN} or (record.author == actor.id and record.kind == 'issue' and record.state == 'open' and "
                      "(actor.id in record.project.maintainers or "
                      "(not record.assignees and not record.labels and record.milestone is None and not record.locked)))",
            "update": f"{TRIAGE} or record.author == actor.id",
            "delete": ADMIN,
        },
        "constraints": [
            {"check": "record.state == 'open' or record.closed_on is not None",
             "message": "A closed or merged issue needs its closed_on date.", "fields": ["closed_on"]},
            {"check": "record.state != 'open' or (record.closed_on is None and record.close_reason is None)",
             "message": "An open issue has no closed_on date or close_reason.", "fields": ["closed_on", "close_reason"]},
            {"check": "record.kind != 'issue' or record.state != 'closed' or record.close_reason is not None",
             "message": "A closed issue needs a close_reason: completed, not_planned, or duplicate.", "fields": ["close_reason"]},
            {"check": "record.kind == 'pull_request' or record.state != 'merged'",
             "message": "Only a pull request can be merged.", "fields": ["state"]},
            {"check": "record.kind == 'issue' or record.close_reason is None",
             "message": "A pull request has no close_reason.", "fields": ["close_reason"]},
            {"check": "record.closed_on is None or record.closed_on >= record.opened_on",
             "message": "closed_on cannot be before opened_on.", "fields": ["closed_on"]},
            {"check": "not any(l.project != record.project for l in Label if l.id in record.labels)",
             "message": "Every label must belong to the issue's project.", "fields": ["labels"], **RELATIONSHIP},
            {"check": "record.milestone is None or record.milestone.project == record.project",
             "message": "The milestone must belong to the issue's project.", "fields": ["milestone"], **RELATIONSHIP},
        ],
    },
    "Comment": {
        "description": "A comment on an issue or pull request. Only the project's maintainers may comment on a locked issue.",
        "fields": {
            "issue": {"type": "reference", "target": "Issue", "required": True, "write_rule": ADMIN},
            "author": {"type": "reference", "target": "Person", "required": True, "default": "$actor", "write_rule": ADMIN},
            "body": {"type": "text", "required": True, "min_length": 1, "max_length": LONG_TEXT},
            "posted_on": {"type": "date", "required": True},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": f"{ADMIN} or (record.author == actor.id and (not record.issue.locked or actor.id in record.issue.project.maintainers))",
            "update": f"{ADMIN} or record.author == actor.id or actor.id in record.issue.project.maintainers",
            "delete": f"{ADMIN} or actor.id in record.issue.project.maintainers",
        },
    },
}


class Tracker(Definition):
    name = "tracker"
    title = "Open-source issue tracker"
    version = "0.1.0"
    types = TYPES
    # A redis maintainer, an outside contributor to several projects, and a Vue maintainer, all real.
    actors = ("ben", "sundb", "oresoftware", "edison1105")
    seed_reason = "World administrator."

    def seed(self, today: date) -> list[tuple[str, str, dict[str, Any]]]:
        # Everyone else comes from the dataset. See bench/github.py.
        return [("Person", "ben", {"login": "ben", "role": "admin", "bot": False})]


DEFINITION = Tracker()
