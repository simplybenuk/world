"""A small working team: people, projects, tasks, and comments."""

from datetime import date, timedelta
from typing import Any

from world.definition import Definition, deny
from world.schema import WorldError


TYPES: dict[str, dict[str, Any]] = {
    "Person": {
        "description": "A person whose stored role governs their access.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "email": {"type": "text", "nullable": True, "default": None, "max_length": 254},
            "role": {
                "type": "enum",
                "required": True,
                "values": ["admin", "member", "viewer"],
                "default": "member",
                "write_rule": "actor.role == admin",
            },
        },
        "rules": {
            "read": "actor is authenticated",
            "create": "actor.role == admin",
            "update": "actor.role == admin or (actor.role == member and actor.id == record.id); only admin can change role",
            "delete": "actor.role == admin and no live record references this person and another admin remains",
        },
    },
    "Project": {
        "description": "A project containing tasks and a list of team members.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "description": {"type": "text", "default": "", "max_length": 20000},
            "members": {
                "type": "list",
                "required": True,
                "items": {"type": "reference", "target": "Person"},
                "unique": True,
                "max_items": 1000,
                "write_rule": "actor.role == admin",
            },
            "visibility": {
                "type": "enum",
                "required": True,
                "values": ["team", "private"],
                "default": "team",
                "write_rule": "actor.role == admin",
            },
        },
        "rules": {
            "read": "actor.role == admin or record.visibility == team or actor.id in record.members",
            "create": "actor.role == admin",
            "update": "actor.role == admin or (actor.role == member and actor.id in record.members); only admin can change members or visibility",
            "delete": "actor.role == admin and no live record references this project",
        },
    },
    "Task": {
        "description": "Work belonging to one project, optionally assigned to a project member.",
        "fields": {
            "title": {"type": "text", "required": True, "min_length": 1, "max_length": 300},
            "description": {"type": "text", "default": "", "max_length": 20000},
            "project": {"type": "reference", "target": "Project", "required": True},
            "owner": {"type": "reference", "target": "Person", "nullable": True, "default": None},
            "status": {
                "type": "enum",
                "required": True,
                "values": ["todo", "doing", "done"],
                "default": "todo",
            },
            "due": {"type": "date", "nullable": True, "default": None},
        },
        "constraints": ["owner is null or owner belongs to project.members"],
        "rules": {
            "read": "actor can read record.project",
            "create": "actor.role == admin or (actor.role == member and actor.id in project.members)",
            "update": "actor.role == admin or (actor.role == member and actor.id in current project.members and actor.id in proposed project.members)",
            "delete": "actor.role == admin and no live record references this task",
        },
    },
    "Comment": {
        "description": "A comment attached to a task, attributed to a member of its project.",
        "fields": {
            "body": {"type": "text", "required": True, "min_length": 1, "max_length": 20000},
            "task": {"type": "reference", "target": "Task", "required": True},
            "author": {
                "type": "reference",
                "target": "Person",
                "required": True,
                "default": "$actor",
                "write_rule": "actor.role == admin or new record.author == actor.id; only admin can change an existing author",
            },
        },
        "constraints": ["author belongs to task.project.members"],
        "rules": {
            "read": "actor can read record.task.project",
            "create": "actor.role == admin or (actor.role == member and actor.id in task.project.members and record.author == actor.id)",
            "update": "actor.role == admin or (actor.role == member and actor.id in current task.project.members and actor.id in proposed task.project.members); only admin can change author",
            "delete": "actor.role == admin",
        },
    },
}


def _project_for(records, record, *, include_deleted=False):
    type_name, data = record["type"], record["data"]
    if type_name == "Project":
        return record
    if type_name == "Comment":
        task = records.get("Task", data["task"], include_deleted=include_deleted)
        return _project_for(records, task, include_deleted=include_deleted) if task else None
    if type_name == "Task":
        return records.get("Project", data["project"], include_deleted=include_deleted)
    return None


def _admin_count(records):
    return sum(person["data"]["role"] == "admin" for person in records.all("Person"))


class Team(Definition):
    name = "team"
    title = "World"
    version = "0.1.0"
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

    def can_read(self, records, actor, record):
        if actor["data"]["role"] == "admin" or record["type"] == "Person":
            return True
        project = _project_for(records, record, include_deleted=True)
        return bool(project and (
            project["data"]["visibility"] == "team" or actor["id"] in project["data"]["members"]
        ))

    def can_update(self, records, actor, record):
        role = actor["data"]["role"]
        if role == "admin":
            return True
        if role != "member":
            return False
        if record["type"] == "Person":
            return record["id"] == actor["id"]
        project = _project_for(records, record)
        return bool(project and actor["id"] in project["data"]["members"])

    def type_capabilities(self, records, actor):
        role = actor["data"]["role"]
        membership = any(actor["id"] in project["data"]["members"] for project in records.all("Project"))
        return {
            type_name: {
                "read": True,
                "create": role == "admin" or (role == "member" and membership and type_name in ("Task", "Comment")),
                "update": role == "admin" or (role == "member" and (type_name == "Person" or membership)),
                "delete": role == "admin",
            }
            for type_name in self.types
        }

    def protected_fields(self, actor, type_name):
        if actor["data"]["role"] == "admin":
            return set()
        return {"Person": {"role"}, "Project": {"members", "visibility"}, "Comment": {"author"}}.get(type_name, set())

    def authorize_create(self, records, actor, type_name, data):
        if actor["data"]["role"] == "admin":
            return
        if actor["data"]["role"] != "member" or type_name not in ("Task", "Comment"):
            deny()
        project = _project_for(records, {"type": type_name, "data": data})
        if not project or actor["id"] not in project["data"]["members"]:
            deny("Only project members can create records in this project.")
        if type_name == "Comment" and data["author"] != actor["id"]:
            deny("A member can only create a comment attributed to themselves.")

    def authorize_delete(self, records, actor, record):
        if actor["data"]["role"] != "admin":
            deny("Only an admin can delete records.")

    def check_delete(self, records, actor, record):
        if record["type"] == "Person" and record["data"]["role"] == "admin" and _admin_count(records) == 1:
            raise WorldError(409, "last_admin", "The world must retain at least one admin.")

    def check(self, records, actor, type_name, record_id, data, before):
        if type_name == "Person":
            if before and before["data"]["role"] == "admin" and data["role"] != "admin" and _admin_count(records) == 1:
                raise WorldError(409, "last_admin", "The world must retain at least one admin.")
        elif type_name == "Task":
            project = records.get("Project", data["project"])
            if data["owner"] is not None and data["owner"] not in project["data"]["members"]:
                raise WorldError(422, "invalid_relationship", "A task owner must be a member of its project.", {"field": "owner"})
            for comment in records.all("Comment"):
                if comment["data"]["task"] == record_id and comment["data"]["author"] not in project["data"]["members"]:
                    raise WorldError(
                        422, "invalid_relationship", "Moving this task would leave a comment author outside the project.",
                        {"type": "Comment", "id": comment["id"], "field": "author"},
                    )
        elif type_name == "Comment":
            project = _project_for(records, {"type": type_name, "data": data})
            if data["author"] not in project["data"]["members"]:
                raise WorldError(422, "invalid_relationship", "A comment author must be a member of the task's project.", {"field": "author"})
        elif type_name == "Project":
            tasks = {task["id"]: task for task in records.all("Task") if task["data"]["project"] == record_id}
            for task in tasks.values():
                owner = task["data"]["owner"]
                if owner is not None and owner not in data["members"]:
                    raise WorldError(
                        422, "invalid_relationship", "A project member still owns a task in this project.",
                        {"type": "Task", "id": task["id"], "field": "owner"},
                    )
            for comment in records.all("Comment"):
                if comment["data"]["task"] in tasks and comment["data"]["author"] not in data["members"]:
                    raise WorldError(
                        422, "invalid_relationship", "A project member still authored a comment in this project.",
                        {"type": "Comment", "id": comment["id"], "field": "author"},
                    )


DEFINITION = Team()
