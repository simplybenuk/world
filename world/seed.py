"""Legacy first-experiment seed, retained as a reference.

The shared prototype runtime seeds from definitions/*.world.json instead.
"""

from datetime import date, timedelta
from typing import Any


def seed_records(today: date) -> list[tuple[str, str, dict[str, Any]]]:
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
