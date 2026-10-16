"""The issue tracker's rules, on a few records shaped like the GitHub data it holds."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from world import WorldError, WorldStore
from world.rules import reads
import worlds

ISSUE = {"project": "redis", "number": 1, "title": "Crash on start", "author": "oresoftware", "opened_on": "2024-01-02"}


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.definition = worlds.load("tracker")
        self.store = WorldStore(Path(self.temporary.name) / "world.sqlite3", self.definition)
        apply = lambda steps: self.store.apply("ben", steps, {"reason": "Test records."})
        apply([{"create": "Person", "data": {"login": login}} for login in ("sundb", "ORESoftware", "edison1105")])
        # IDs come from the server for records created over the protocol; the loader gives GitHub-shaped ones.
        self.people = {record["data"]["login"].lower(): record["id"] for record in self.store.list_records("ben", "Person")}
        self.people["ben"] = "ben"
        self.redis = apply([{"create": "Project", "data": {"name": "redis/redis", "maintainers": [self.people["sundb"]]}}])[0]["id"]
        self.vue = apply([{"create": "Project", "data": {"name": "vuejs/core", "maintainers": [self.people["edison1105"]]}}])[0]["id"]
        self.bug = apply([{"create": "Label", "data": {"project": self.redis, "name": "bug"}}])[0]["id"]
        self.vue_bug = apply([{"create": "Label", "data": {"project": self.vue, "name": "bug"}}])[0]["id"]
        self.issues = [record["id"] for record in apply([
            {"create": "Issue", "data": {**ISSUE, "project": self.redis, "number": number, "author": self.people["oresoftware"]}}
            for number in range(1, 6)])]
        self.locked = apply([{"create": "Issue", "data": {**ISSUE, "project": self.redis, "number": 9, "locked": True,
                                                         "author": self.people["sundb"]}}])[0]["id"]

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def as_(self, login):
        return self.people[login]

    def update(self, login, record_id, changes, type_name="Issue"):
        version = self.store.get_record("ben", type_name, record_id)["version"]
        return self.store.update(self.as_(login), type_name, record_id, changes, version)

    def assert_refused(self, status, action):
        with self.assertRaises(WorldError) as caught:
            action()
        self.assertEqual(caught.exception.status, status)
        return caught.exception

    def test_anyone_opens_an_issue_but_only_maintainers_triage_it(self):
        contributor = self.as_("oresoftware")
        opened = self.store.create(contributor, "Issue", {"project": self.redis, "title": "Leak in DEBUG", "opened_on": "2026-10-14"})
        self.assertEqual(opened["data"]["author"], contributor)
        self.assert_refused(403, lambda: self.store.create(contributor, "Issue", {
            "project": self.redis, "title": "Labelled", "opened_on": "2026-10-14", "labels": [self.bug]}))
        refused = self.assert_refused(403, lambda: self.update("oresoftware", opened["id"], {"labels": [self.bug]}))
        self.assertEqual(refused.details["fields"], ["labels"])
        self.update("sundb", opened["id"], {"labels": [self.bug], "assignees": [self.as_("sundb")]})
        self.assert_refused(403, lambda: self.update("edison1105", opened["id"], {"labels": []}))

    def test_an_author_may_close_their_own_issue_with_a_reason(self):
        issue = self.issues[0]
        error = self.assert_refused(422, lambda: self.update("oresoftware", issue, {"state": "closed", "closed_on": "2026-10-14"}))
        self.assertEqual(error.details["fields"], ["close_reason"])
        self.update("oresoftware", issue, {"state": "closed", "closed_on": "2026-10-14", "close_reason": "completed"})
        self.assert_refused(422, lambda: self.update("sundb", issue, {"state": "merged"}))
        self.assert_refused(403, lambda: self.update("edison1105", issue, {"title": "Renamed"}))

    def test_only_maintainers_comment_on_a_locked_issue(self):
        comment = {"issue": self.locked, "body": "Any news?", "posted_on": "2026-10-14"}
        self.assert_refused(403, lambda: self.store.create(self.as_("oresoftware"), "Comment", comment))
        self.store.create(self.as_("sundb"), "Comment", comment)
        self.store.create(self.as_("oresoftware"), "Comment", {**comment, "issue": self.issues[1]})

    def test_labels_and_milestones_belong_to_the_issues_project(self):
        error = self.assert_refused(422, lambda: self.update("ben", self.issues[2], {"labels": [self.vue_bug]}))
        self.assertEqual(error.code, "invalid_relationship")
        self.assert_refused(422, lambda: self.update("sundb", self.issues[2], {"locked": "yes"}))

    def test_a_change_no_constraint_reads_does_not_recheck_what_refers_to_it(self):
        self.assertNotIn(("Project", "maintainers"), set().union(*(
            reads(constraint["check"], self.definition.types, name, "Person")
            for name, definition in self.definition.types.items() for constraint in definition.get("constraints", []))))
        with mock.patch.object(self.store, "_inbound", wraps=self.store._inbound) as inbound:
            self.update("ben", self.redis, {"maintainers": [self.as_("sundb"), self.as_("oresoftware")]}, "Project")
        self.assertLess(inbound.call_count, len(self.issues))  # only the reply's own capabilities ask

    def test_moving_a_label_that_issues_carry_is_still_refused(self):
        self.update("sundb", self.issues[3], {"labels": [self.bug]})
        error = self.assert_refused(422, lambda: self.update("ben", self.bug, {"project": self.vue}, "Label"))
        self.assertIn("Every label must belong to the issue's project", error.message)


if __name__ == "__main__":
    unittest.main()
