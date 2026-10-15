"""Issue-tracker tasks on real GitHub data: reading, counting, searching, single writes, related writes, and refusals.

Build the database first with `python3 -m bench.github build`. Each task works on its own copy.
Prompts use the words of the request, not the protocol's, so the same tasks can be given
to an agent working against EspoCRM. Every answer below was read from the data.
"""

from datetime import date
import json
import re

from .crm import REFUSALS, Task
from .github import HOME

WORLD = "tracker"
SOURCE = HOME / "world.sqlite3"
SEEDED = {}  # filled by the runner, unused here: new records are told apart by when they were created
FAULTS = []


def loaded_at():
    return json.loads((HOME / "build.json").read_text())["built_at"]


def _says(answer, *parts):
    """Whether the answer contains every part, ignoring case and thousands separators."""
    text = answer.lower().replace(",", "")
    return all(part.lower() in text for part in parts)


def _number(answer, value):
    return re.search(rf"(?<![\d.]){value}(?![\d.])", answer.replace(",", "")) is not None


def _new(store, type_name, **equals):
    """Records of this type created after the data was loaded, matching the given fields."""
    filters = [{"field": field, "op": "eq", "value": value} for field, value in equals.items()]
    filters.append({"field": "created_at", "op": "gt", "value": loaded_at()})
    return store.find("ben", type_name, filters=filters, limit=1000)["records"]


def _issue(store, record_id):
    return store.get_record("ben", "Issue", record_id)["data"]


def _refused(answer):
    return [] if any(word in answer.lower() for word in REFUSALS + ("locked", "maintainer")) else ["The answer does not say the change was not possible."]


def _query(client, type_name, where, **options):
    return client.call("POST", "/api/query", {"type": type_name, "where": where, **options})[1]


def _one(client, project, number):
    (issue,) = _query(client, "Issue", f"record.project == '{project}' and record.number == {number}")["records"]
    return issue


def _label(client, project, name):
    (label,) = _query(client, "Label", f"record.project == '{project}' and record.name == '{name}'")["records"]
    return label["id"]


# Reading


def solve_read_issue(client):
    client.get("/api/world")
    issue = _one(client, "redis", 3067)
    return f"#3067: {issue['data']['title']}. It is {issue['data']['state']}."


def check_read_issue(store, answer):
    # The title is "small bug/feature with GET * and particular key"; an answer may describe it rather than quote it.
    failures = [] if _says(answer, "particular key") or _says(answer, "get *") else ["The answer does not say what the issue reported."]
    return failures + ([] if "closed" in answer.lower() else ["The answer does not say the issue is closed."])


def solve_read_pr(client):
    client.get("/api/world")
    issue = _one(client, "redis", 13512)
    return f"{issue['data']['author']} opened it; it was merged on {issue['data']['closed_on']}."


def check_read_pr(store, answer):
    failures = [] if "oranagra" in answer.lower() else ["The answer does not name oranagra."]
    dated = re.search(r"2024-09-12|september 12|12 september|sep\.? 12|12 sep", answer.lower())
    return failures + ([] if dated else ["The answer does not give the merge date, 12 September 2024."])


def solve_read_labels(client):
    client.get("/api/world")
    _, body = client.call("POST", "/api/query", {
        "type": "Issue", "where": "record.project == 'vue-core' and record.number == 14761", "include": ["labels"]})
    return ", ".join(label["data"]["name"] for label in body["included"])


def check_read_labels(store, answer):
    missing = [name for name in ("suspense", "workaround", "p3-minor-bug") if name not in answer.lower()]
    return [f"The answer omits {', '.join(missing)}."] if missing else []


# Counting


def solve_count_label(client):
    client.get("/api/world")
    label = _label(client, "redis", "crash report")
    found = _query(client, "Issue", f"record.project == 'redis' and record.state == 'open' and record.kind == 'issue' and '{label}' in record.labels", count=True)
    return f"{found['count']} open issues."


def check_count_label(store, answer):
    return [] if _number(answer, 35) else ["The answer does not give 35."]


def solve_count_mine(client):
    client.get("/api/world")
    everything = _query(client, "Issue", "record.author == actor.id", count=True)["count"]
    still_open = _query(client, "Issue", "record.author == actor.id and record.state == 'open'", count=True)["count"]
    return f"{everything} issues, {still_open} still open."


def check_count_mine(store, answer):
    failures = [] if _number(answer, 108) else ["The answer does not give 108 issues in all."]
    return failures + ([] if _number(answer, 6) or "six" in answer.lower() else ["The answer does not give 6 still open."])


def solve_assigned(client):
    client.get("/api/world")
    found = _query(client, "Issue", "record.project == 'redis' and record.state == 'open' and actor.id in record.assignees")["records"]
    return ", ".join(f"#{issue['data']['number']}" for issue in found)


def check_assigned(store, answer):
    missing = [number for number in ("14382", "14266") if number not in answer]
    extra = [number for number in ("14838", "14879", "12963", "11156", "8702") if number in answer]
    return ([f"The answer omits {', '.join(missing)}."] if missing else []) + ([f"The answer lists closed issues {', '.join(extra)}."] if extra else [])


def solve_count_milestone(client):
    client.get("/api/world")
    (milestone,) = _query(client, "Milestone", "record.project == 'redis' and record.title == 'Redis 6.0.x'")["records"]
    everything = _query(client, "Issue", f"record.milestone == '{milestone['id']}'", count=True)["count"]
    still_open = _query(client, "Issue", f"record.milestone == '{milestone['id']}' and record.state == 'open'", count=True)["count"]
    return f"{everything} in the milestone, {still_open} still open."


def check_count_milestone(store, answer):
    failures = [] if _number(answer, 32) else ["The answer does not give 32 in all."]
    return failures + ([] if _number(answer, 3) or "three" in answer.lower() else ["The answer does not give 3 still open."])


# Searching


def solve_search_mine(client):
    client.get("/api/world")
    found = _query(client, "Issue", "record.author == actor.id and 'stdout' in record.title")["records"]
    return ", ".join(f"#{issue['data']['number']} ({issue['data']['state']})" for issue in found)


def check_search_mine(store, answer):
    failures = [] if "19584" in answer else ["The answer does not give issue 19584."]
    return failures + ([] if "open" in answer.lower() else ["The answer does not say it is still open."])


def solve_search_open(client):
    client.get("/api/world")
    found = _query(client, "Issue", "record.project == 'vue-core' and record.kind == 'issue' and record.state == 'open' "
                                    "and 'teleport' in record.title and 'empty' in record.title")["records"]
    return ", ".join(f"#{issue['data']['number']}" for issue in found)


def check_search_open(store, answer):
    return [] if "9782" in answer else ["The answer does not give issue 9782."]


def solve_search_comments(client):
    client.get("/api/world")
    issue = _one(client, "redis", 9767)
    found = _query(client, "Comment", f"record.issue == '{issue['id']}' and 'valgrind' in record.body")["records"]
    return ", ".join(comment["data"]["author"] for comment in found)


def check_search_comments(store, answer):
    return [] if "oranagra" in answer.lower() else ["The answer does not name oranagra."]


# Single writes


def solve_comment(client):
    client.get("/api/world")
    issue = _one(client, "redis", 15058)
    client.call("POST", "/api/records/Comment", {"data": {
        "issue": issue["id"], "body": "I see the same with 8.6.2 on Debian 12.", "posted_on": issue["updated_at"][:10]},
        "reason": "ORESoftware reported the same problem."})
    return "Commented on #15058."


def check_comment(store, answer):
    found = _new(store, "Comment", issue="redis-15058")
    if len(found) != 1:
        return [f"Expected one new comment on redis #15058, found {len(found)}."]
    data = found[0]["data"]
    failures = [] if data["author"] == "oresoftware" else ["The comment is not by ORESoftware."]
    return failures + ([] if _says(data["body"], "8.6.2", "debian 12") else ["The comment does not say what was asked."])


def solve_label(client):
    client.get("/api/world")
    issue = _one(client, "redis", 15205)
    label = _label(client, "redis", "crash report")
    client.call("PATCH", f"/api/records/Issue/{issue['id']}", {
        "changes": {"labels": issue["data"]["labels"] + [label]}, "expected_version": issue["version"], "reason": "A crash report."})
    return "Labelled #15205 crash report."


def check_label(store, answer):
    return [] if _issue(store, "redis-15205")["labels"] == ["redis-label-crash-report"] else ["redis #15205 is not labelled crash report alone."]


def solve_close(client):
    client.get("/api/world")
    issue = _one(client, "redis", 15063)
    client.call("PATCH", f"/api/records/Issue/{issue['id']}", {
        "changes": {"state": "closed", "close_reason": "not_planned", "closed_on": issue["updated_at"][:10]},
        "expected_version": issue["version"], "reason": "A question about deployment, not a change to redis."})
    return "Closed #15063 as not planned."


def check_close(store, answer):
    data = _issue(store, "redis-15063")
    return [] if data["state"] == "closed" and data["close_reason"] == "not_planned" else ["redis #15063 is not closed as not planned."]


def solve_open_issue(client):
    client.get("/api/world")
    today = date.today().isoformat()
    client.call("POST", "/api/records/Issue", {"data": {
        "project": "redis", "title": "SCAN with MATCH skips keys after a resize",
        "body": "While the table grows, SCAN with a MATCH pattern returns fewer keys than KEYS does.", "opened_on": today},
        "reason": "ORESoftware reported a bug."})
    return "Opened the issue."


def check_open_issue(store, answer):
    found = [issue for issue in _new(store, "Issue", project="redis") if "scan" in issue["data"]["title"].lower()]
    if len(found) != 1:
        return [f"Expected one new redis issue about SCAN, found {len(found)}."]
    data = found[0]["data"]
    failures = [] if data["author"] == "oresoftware" and data["state"] == "open" and data["kind"] == "issue" else ["The issue is not an open issue by ORESoftware."]
    return failures + ([] if _says(data["title"], "resize") and _says(data["body"], "keys") else ["The title or description is not the one asked for."])


# Related writes


def solve_triage(client):
    me = client.get("/api/world")["actor"]["id"]
    issue = _one(client, "redis", 15009)
    label = _label(client, "redis", "crash report")
    client.call("POST", "/api/changes", {"reason": "sundb is investigating the FT.AGGREGATE crash.", "steps": [
        {"update": "Issue", "id": issue["id"], "expected_version": issue["version"],
         "changes": {"assignees": [me], "labels": [label]}},
        {"create": "Comment", "data": {"issue": issue["id"], "body": "Looking into this.", "posted_on": issue["updated_at"][:10]}}]})
    return "Assigned to me, labelled, and commented."


def check_triage(store, answer):
    data, failures = _issue(store, "redis-15009"), []
    if data["assignees"] != ["sundb"]:
        failures.append("redis #15009 is not assigned to sundb alone.")
    if data["labels"] != ["redis-label-crash-report"]:
        failures.append("redis #15009 is not labelled crash report.")
    if not any(_says(comment["data"]["body"], "looking into") and comment["data"]["author"] == "sundb" for comment in _new(store, "Comment", issue="redis-15009")):
        failures.append("No comment from sundb saying they are looking into it.")
    return failures


def solve_duplicate(client):
    client.get("/api/world")
    issue = _one(client, "redis", 15211)
    client.call("POST", "/api/changes", {"reason": "Duplicate of #15208.", "steps": [
        {"update": "Issue", "id": issue["id"], "expected_version": issue["version"],
         "changes": {"state": "closed", "close_reason": "duplicate", "closed_on": issue["updated_at"][:10]}},
        {"create": "Comment", "data": {"issue": issue["id"], "body": "Duplicate of #15208.", "posted_on": issue["updated_at"][:10]}}]})
    return "Closed #15211 as a duplicate of #15208."


def check_duplicate(store, answer):
    data, failures = _issue(store, "redis-15211"), []
    if data["state"] != "closed" or data["close_reason"] != "duplicate":
        failures.append("redis #15211 is not closed as a duplicate.")
    if not any("15208" in comment["data"]["body"] for comment in _new(store, "Comment", issue="redis-15211")):
        failures.append("No comment on #15211 points to #15208.")
    if _issue(store, "redis-15208")["state"] != "open":
        failures.append("redis #15208 was closed too.")
    return failures


def solve_milestone(client):
    client.get("/api/world")
    first, second = _one(client, "redis", 15145), _one(client, "redis", 15181)
    client.call("POST", "/api/changes", {"reason": "Planning Redis 8.8.", "steps": [
        {"create": "Milestone", "as": "milestone", "data": {"project": "redis", "title": "Redis 8.8"}},
        *({"update": "Issue", "id": issue["id"], "expected_version": issue["version"], "changes": {"milestone": "$milestone"}}
          for issue in (first, second))]})
    return "Created Redis 8.8 with #15145 and #15181."


def check_milestone(store, answer):
    found = [record for record in _new(store, "Milestone", project="redis") if "8.8" in record["data"]["title"]]
    if len(found) != 1:
        return [f"Expected one new redis milestone named Redis 8.8, found {len(found)}."]
    missing = [record_id for record_id in ("redis-15145", "redis-15181") if _issue(store, record_id)["milestone"] != found[0]["id"]]
    return [f"{', '.join(missing)} not in the new milestone."] if missing else []


# Refusals


def solve_locked(client):
    client.get("/api/world")
    issue = _one(client, "typescript", 22446)
    assert issue["data"]["locked"]
    return "I cannot comment: the issue is locked, and only the project's maintainers may comment on it."


def check_locked(store, answer):
    failures = ["A comment was added to the locked issue."] if _new(store, "Comment", issue="typescript-22446") else []
    return failures + _refused(answer)


def solve_other_project(client):
    client.get("/api/world")
    issue = _one(client, "redis", 15269)
    status, _ = client.call("PATCH", f"/api/records/Issue/{issue['id']}", {
        "changes": {"labels": [_label(client, "redis", "crash report")]}, "expected_version": issue["version"]})
    assert status == 403, status
    return "I cannot label it: only redis's maintainers may change its labels, and I am not one."


def check_other_project(store, answer):
    failures = ["redis #15269 was labelled."] if _issue(store, "redis-15269")["labels"] else []
    return failures + _refused(answer)


def solve_assign(client):
    client.get("/api/world")
    issue = _one(client, "typescript", 19584)
    status, _ = client.call("PATCH", f"/api/records/Issue/{issue['id']}", {
        "changes": {"assignees": ["ryancavanaugh"]}, "expected_version": issue["version"]})
    assert status == 403, status
    return "I cannot assign it: only TypeScript's maintainers may assign issues."


def check_assign(store, answer):
    failures = ["typescript #19584 was assigned."] if _issue(store, "typescript-19584")["assignees"] else []
    return failures + _refused(answer)


TASKS = [
    Task("read-issue", "oresoftware", "I'm ORESoftware. What did I report in redis issue 3067, and is it still open?", solve_read_issue, check_read_issue),
    Task("read-pr", "sundb", "I'm sundb. Who opened redis pull request 13512, and when was it merged?", solve_read_pr, check_read_pr),
    Task("read-labels", "edison1105", "I'm edison1105. Which labels are on vuejs/core issue 14761?", solve_read_labels, check_read_labels),
    Task("count-label", "sundb", "I'm sundb. How many open redis issues, not counting pull requests, are labelled crash report?", solve_count_label, check_count_label),
    Task("count-mine", "oresoftware", "I'm ORESoftware. How many issues have I opened across all the projects here, and how many of them are still open?", solve_count_mine, check_count_mine),
    Task("assigned", "sundb", "I'm sundb. Which open redis issues are assigned to me?", solve_assigned, check_assigned),
    Task("count-milestone", "sundb", "I'm sundb. How many issues and pull requests are in the redis milestone Redis 6.0.x, and how many of them are still open?", solve_count_milestone, check_count_milestone),
    Task("search-mine", "oresoftware", "I'm ORESoftware. I once asked TypeScript for a way to hook into tsc --watch through stdout. What is that issue's number, and is it still open?", solve_search_mine, check_search_mine),
    Task("search-open", "edison1105", "I'm edison1105. Is there an open vuejs/core issue, not a pull request, about Teleport failing when its target is empty? Give its number.", solve_search_open, check_search_open),
    Task("search-comments", "sundb", "I'm sundb. Who mentioned valgrind in the comments on redis issue 9767?", solve_search_comments, check_search_comments),
    Task("comment", "oresoftware", "I'm ORESoftware. Add a comment to redis issue 15058 saying: I see the same with 8.6.2 on Debian 12.", solve_comment, check_comment),
    Task("label", "sundb", "I'm sundb. Label redis issue 15205 as a crash report.", solve_label, check_label),
    Task("close", "sundb", "I'm sundb. Close redis issue 15063 as not planned. It is a deployment question, not a problem with redis.", solve_close, check_close),
    Task("open-issue", "oresoftware", "I'm ORESoftware. Open a new redis issue titled \"SCAN with MATCH skips keys after a resize\", saying: While the table grows, SCAN with a MATCH pattern returns fewer keys than KEYS does.", solve_open_issue, check_open_issue),
    Task("triage", "sundb", "I'm sundb. Take redis issue 15009: assign it to me, label it as a crash report, and add a comment saying I'm looking into it.", solve_triage, check_triage),
    Task("duplicate", "sundb", "I'm sundb. Redis issue 15211 duplicates issue 15208. Close 15211 as a duplicate and leave a comment pointing to 15208.", solve_duplicate, check_duplicate),
    Task("milestone", "sundb", "I'm sundb. Create a redis milestone called Redis 8.8 and put issues 15145 and 15181 in it.", solve_milestone, check_milestone),
    Task("locked", "oresoftware", "I'm ORESoftware. Comment on TypeScript issue 22446 saying: This still happens in TypeScript 5.9.", solve_locked, check_locked),
    Task("other-project", "edison1105", "I'm edison1105. Label redis issue 15269 as a crash report.", solve_other_project, check_other_project),
    Task("assign", "oresoftware", "I'm ORESoftware. Assign my TypeScript issue 19584 to RyanCavanaugh.", solve_assign, check_assign),
]
