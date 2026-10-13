"""A small working team: people, projects, tasks, and comments."""

from datetime import date, timedelta
from typing import Any

from world.definition import Definition

ADMIN = "actor.role == 'admin'"
RELATIONSHIP = {"code": "invalid_relationship"}
LAST_ADMIN = {"message": "The world must retain at least one admin.", "fields": ["role"], "code": "last_admin", "status": 409}

TYPES: dict[str, dict[str, Any]] = {
    "Person": {
        "description": "A person whose stored role governs their access.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "email": {"type": "text", "nullable": True, "default": None, "max_length": 254},
            "role": {"type": "enum", "required": True, "values": ["admin", "member", "viewer"], "default": "member", "write_rule": ADMIN},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": ADMIN,
            "update": f"{ADMIN} or (actor.role == 'member' and actor.id == record.id)",
            "delete": ADMIN,
        },
        "constraints": [
            {"check": "count(p for p in Person if p.role == 'admin') >= 1", "on": ("update",), **LAST_ADMIN},
            {"check": "record.role != 'admin' or count(p for p in Person if p.role == 'admin') > 1", "on": ("delete",), **LAST_ADMIN},
        ],
    },
    "Project": {
        "description": "A project containing tasks and a list of team members.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "description": {"type": "text", "default": "", "max_length": 20000},
            "members": {
                "type": "list", "required": True, "items": {"type": "reference", "target": "Person"},
                "unique": True, "max_items": 1000, "write_rule": ADMIN,
            },
            "visibility": {"type": "enum", "required": True, "values": ["team", "private"], "default": "team", "write_rule": ADMIN},
        },
        "rules": {
            "read": f"{ADMIN} or record.visibility == 'team' or actor.id in record.members",
            "create": ADMIN,
            "update": f"{ADMIN} or (actor.role == 'member' and actor.id in record.members)",
            "delete": ADMIN,
        },
    },
    "Task": {
        "description": "Work belonging to one project, optionally assigned to a project member.",
        "fields": {
            "title": {"type": "text", "required": True, "min_length": 1, "max_length": 300},
            "description": {"type": "text", "default": "", "max_length": 20000},
            "project": {"type": "reference", "target": "Project", "required": True},
            "owner": {"type": "reference", "target": "Person", "nullable": True, "default": None},
            "status": {"type": "enum", "required": True, "values": ["todo", "doing", "done"], "default": "todo"},
            "due": {"type": "date", "nullable": True, "default": None},
        },
        "rules": {
            "read": "can_read(record.project)",
            "create": f"{ADMIN} or (actor.role == 'member' and actor.id in record.project.members)",
            "update": f"{ADMIN} or (actor.role == 'member' and actor.id in record.project.members)",
            "delete": ADMIN,
        },
        "constraints": [{
            "check": "record.owner is None or record.owner in record.project.members",
            "message": "A task owner must be a member of its project.", "fields": ["owner"], **RELATIONSHIP,
        }],
    },
    "Comment": {
        "description": "A comment attached to a task, attributed to a member of its project.",
        "fields": {
            "body": {"type": "text", "required": True, "min_length": 1, "max_length": 20000},
            "task": {"type": "reference", "target": "Task", "required": True},
            "author": {"type": "reference", "target": "Person", "required": True, "default": "$actor", "write_rule": ADMIN},
        },
        "rules": {
            "read": "can_read(record.task)",
            "create": f"{ADMIN} or (actor.role == 'member' and actor.id in record.task.project.members and record.author == actor.id)",
            "update": f"{ADMIN} or (actor.role == 'member' and actor.id in record.task.project.members)",
            "delete": ADMIN,
        },
        "constraints": [{
            "check": "record.author in record.task.project.members",
            "message": "A comment author must be a member of the task's project.", "fields": ["author"], **RELATIONSHIP,
        }],
    },
}


class Team(Definition):
    name = "team"
    title = "World"
    version = "0.2.0"
    types = TYPES
    actors = ("ben", "maya", "alex", "sam")
    seed_reason = "Initial working team."

    def seed(self, today: date) -> list[tuple[str, str, dict[str, Any]]]:
        def due(days: int) -> str:
            return (today + timedelta(days=days)).isoformat()

        return [
            ("Person", "ben", {"name": "Ben", "email": "ben@example.test", "role": "admin"}),
            ("Person", "maya", {"name": "Maya", "email": "maya@example.test", "role": "member"}),
            ("Person", "alex", {"name": "Alex", "email": "alex@example.test", "role": "member"}),
            ("Person", "sam", {"name": "Sam", "email": "sam@example.test", "role": "viewer"}),
            ("Project", "website", {
                "name": "Website", "description": "Prepare the team website for launch.",
                "members": ["ben", "maya", "alex"], "visibility": "team",
            }),
            ("Project", "world", {
                "name": "World", "description": "Test persistent records with disposable clients.",
                "members": ["ben", "alex"], "visibility": "private",
            }),
            ("Task", "documentation", {
                "title": "Write documentation", "description": "Explain the website launch and support process.",
                "project": "website", "owner": "ben", "status": "todo", "due": due(-1),
            }),
            ("Task", "homepage", {
                "title": "Finish the homepage", "description": "Review the copy and publish the final content.",
                "project": "website", "owner": "maya", "status": "doing", "due": due(0),
            }),
            ("Task", "accessibility", {
                "title": "Check website accessibility", "description": "Check keyboard navigation and contrast.",
                "project": "website", "owner": "alex", "status": "todo", "due": due(2),
            }),
            ("Task", "launch-announcement", {
                "title": "Write the launch announcement", "description": "The announcement draft is ready for launch.",
                "project": "website", "owner": "maya", "status": "done", "due": due(-2),
            }),
            ("Task", "world-protocol", {
                "title": "Describe the record protocol", "description": "Make types, relationships, and permissions discoverable.",
                "project": "world", "owner": "alex", "status": "doing", "due": due(-2),
            }),
            ("Task", "client-experiment", {
                "title": "Try a second client", "description": "Reopen the world and confirm that changes remain.",
                "project": "world", "owner": "ben", "status": "todo", "due": due(3),
            }),
            ("Comment", "documentation-notes", {
                "body": "Include the launch checklist and who to contact for support.",
                "task": "documentation", "author": "maya",
            }),
            ("Comment", "protocol-notes", {
                "body": "The client should discover rules before attempting a change.",
                "task": "world-protocol", "author": "ben",
            }),
        ]


DEFINITION = Team()
