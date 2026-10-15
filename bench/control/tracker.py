"""The control for the issue tracker: the same GitHub records, people, and rules in EspoCRM.

It runs as a second EspoCRM beside the one for the other domains, so that its
description holds only these entities:

    WORLD_CONTROL_PORT=8792 docker compose -p world-control-tracker -f bench/control/compose.yaml up -d
    python3 -m bench.control.tracker setup    entities, fields, links, teams, roles, rules, and users
    python3 -m bench.control.tracker load     write the GitHub records straight into EspoCRM's database

An administrator would build the tracker this way. Each project is a team, and
its maintainers are the team's members. A role lets maintainers change their
team's issues, and stops other users changing labels, assignees, milestones, and
locks. Before-save scripts hold the rules that roles cannot.
"""

import argparse
from collections import Counter
from hashlib import sha1
import json
import subprocess
import time

from . import espocrm
from .. import tracker
from ..crm import Task

espocrm.PORT = 8792
from .espocrm import Api, Meter, now  # noqa: E402,F401  (Meter and now are used by the runner)
from ..github import REPOS, ROOT, generate  # noqa: E402

COMPOSE = ["docker", "compose", "-p", "world-control-tracker", "-f", str(ROOT / "bench" / "control" / "compose.yaml")]
LOADED_AT = "2026-10-01 00:00:00"  # earlier than anything the API writes, so a reset can tell loaded rows apart
TRIAGED = ("assignees", "labels", "milestone", "locked")

ENTITIES = {
    "Person": {"fields": [{"name": "bot", "type": "bool"}], "links": []},
    "Project": {"fields": [], "links": []},
    "Label": {"fields": [], "links": [("project", "CProject", "labels", "manyToOne")]},
    "Milestone": {"fields": [{"name": "number", "type": "int", "min": 1}], "links": [("project", "CProject", "milestones", "manyToOne")]},
    "Issue": {
        "fields": [
            {"name": "number", "type": "int", "min": 1},
            {"name": "kind", "type": "enum", "options": ["issue", "pull_request"], "default": "issue", "required": True},
            {"name": "state", "type": "enum", "options": ["open", "closed", "merged"], "default": "open", "required": True},
            {"name": "closeReason", "type": "enum", "options": ["", "completed", "not_planned", "duplicate"], "default": ""},
            {"name": "locked", "type": "bool"},
            {"name": "openedOn", "type": "date", "required": True},
            {"name": "closedOn", "type": "date"},
        ],
        "links": [("project", "CProject", "issues", "manyToOne"), ("author", "CPerson", "authoredIssues", "manyToOne"),
                  ("milestone", "CMilestone", "issues", "manyToOne"), ("labels", "CLabel", "issues", "manyToMany"),
                  ("assignees", "CPerson", "assignedIssues", "manyToMany")],
    },
    "Comment": {
        "fields": [{"name": "postedOn", "type": "date", "required": True}],
        "links": [("issue", "CIssue", "comments", "manyToOne"), ("author", "CPerson", "comments", "manyToOne")],
    },
}
LINK_RELATIONS = {("Issue", "labels"): "cIssueLabel", ("Issue", "assignees"): "cIssueAssignee"}
MAINTAINERS = ("Project", "maintainers", "CPerson", "maintainedProjects")

SCRIPTS = {
    "CIssue": """if (state != 'open' && closedOn == null) {
    recordService\\throwBadRequest('A closed or merged issue needs its closed-on date.');
}
if (state == 'open' && (closedOn != null || (closeReason != null && closeReason != ''))) {
    recordService\\throwBadRequest('An open issue has no closed-on date or close reason.');
}
if (kind == 'issue' && state == 'closed' && (closeReason == null || closeReason == '')) {
    recordService\\throwBadRequest('A closed issue needs a close reason: completed, not_planned, or duplicate.');
}
if (kind != 'pull_request' && state == 'merged') {
    recordService\\throwBadRequest('Only a pull request can be merged.');
}
if (kind == 'pull_request' && closeReason != null && closeReason != '') {
    recordService\\throwBadRequest('A pull request has no close reason.');
}
if (closedOn != null && closedOn < openedOn) {
    recordService\\throwBadRequest('The closed-on date cannot be before the opened-on date.');
}
if (milestoneId != null && record\\attribute('CMilestone', milestoneId, 'projectId') != projectId) {
    recordService\\throwBadRequest('The milestone must belong to the issue\\'s project.');
}
if (entity\\isAttributeChanged('labelsIds')) {
    $i = 0;
    while ($i < array\\length(labelsIds)) {
        if (record\\attribute('CLabel', array\\at(labelsIds, $i), 'projectId') != projectId) {
            recordService\\throwBadRequest('Every label must belong to the issue\\'s project.');
        }
        $i = $i + 1;
    }
}""",
    "CComment": """if (
    entity\\isNew() && record\\attribute('CIssue', issueId, 'locked') == true && env\\userAttribute('type') != 'admin'
    && !array\\includes(env\\userAttribute('teamsIds'), array\\at(record\\attribute('CIssue', issueId, 'teamsIds'), 0))
) {
    recordService\\throwForbidden('This issue is locked. Only the project\\'s maintainers may comment on it.');
}""",
}

READ_ONLY = {"create": "no", "read": "all", "edit": "no", "delete": "no", "stream": "no"}
ROLES = {
    "Maintainer": {
        "CIssue": {"create": "yes", "read": "all", "edit": "team", "delete": "no", "stream": "no"},
        "CComment": {"create": "yes", "read": "all", "edit": "own", "delete": "no", "stream": "no"},
        "CLabel": {"create": "yes", "read": "all", "edit": "team", "delete": "no", "stream": "no"},
        "CMilestone": {"create": "yes", "read": "all", "edit": "team", "delete": "no", "stream": "no"},
        "CProject": {"create": "no", "read": "all", "edit": "team", "delete": "no", "stream": "no"},
        "CPerson": READ_ONLY,
    },
    "Contributor": {
        "CIssue": {"create": "yes", "read": "all", "edit": "own", "delete": "no", "stream": "no"},
        "CComment": {"create": "yes", "read": "all", "edit": "own", "delete": "no", "stream": "no"},
        **{name: READ_ONLY for name in ("CLabel", "CMilestone", "CProject", "CPerson")},
    },
}
# Contributors may open an issue but not triage one.
FIELDS = {"Contributor": {"CIssue": {field: {"read": "yes", "edit": "no"} for field in TRIAGED}}}
USERS = {"sundb": ("Maintainer", ["redis"]), "oresoftware": ("Contributor", []), "edison1105": ("Maintainer", ["vue-core", "vue-docs"])}


def setup():
    """Create the entities, links, rules, teams, roles, and users once. Returns each user's ID and API key."""
    admin = Api()
    scopes = admin.ok("GET", "Metadata")["scopes"]
    for name, definition in ENTITIES.items():
        if "C" + name in scopes:
            continue
        admin.ok("POST", "EntityManager/action/createEntity", {
            "name": name, "labelSingular": name, "labelPlural": name + "s", "type": "Base", "stream": False})
        for field in definition["fields"]:
            admin.ok("POST", f"Admin/fieldManager/C{name}", {**field, "label": field["name"]})
    links = [(name, *link) for name, definition in ENTITIES.items() for link in definition["links"]] + [(*MAINTAINERS, "manyToMany")]
    existing = admin.ok("GET", "Metadata")["entityDefs"]
    for name, link, foreign, back, kind in links:
        if link in existing.get("C" + name, {}).get("links", {}):
            continue
        body = {"entity": "C" + name, "entityForeign": foreign, "link": link, "linkForeign": back,
                "label": link.capitalize(), "labelForeign": back.capitalize(), "linkType": kind}
        if kind == "manyToMany":
            # The list field lets a client read and write an issue's labels or assignees with the issue itself.
            body.update({"relationName": LINK_RELATIONS.get((name, link), "cProjectMaintainer"), "linkMultipleField": True})
        admin.ok("POST", "EntityManager/action/createLink", body)
    # An issue's title and a comment's body can be longer than EspoCRM's default of 255 characters.
    admin.ok("PUT", "Admin/fieldManager/CIssue/name", {"type": "varchar", "maxLength": 760, "required": True})
    for scope, script in SCRIPTS.items():
        admin.ok("POST", "EntityManager/action/formula", {"scope": scope, "data": {"beforeSaveApiScript": script}})
    admin.call("POST", "Admin/rebuild")
    teams = {}
    for project in REPOS.values():
        found = admin.all("Team", name=project)
        teams[project] = (found[0] if found else admin.ok("POST", "Team", {"name": project}))["id"]
    roles = {}
    for name, data in ROLES.items():
        found = admin.all("Role", name=name)
        body = {"name": name, "data": data, "fieldData": FIELDS.get(name, {}), "assignmentPermission": "no", "userPermission": "no"}
        roles[name] = (admin.ok("PUT", f"Role/{found[0]['id']}", body) if found else admin.ok("POST", "Role", body))["id"]
    users = {}
    for name, (role, projects) in USERS.items():
        found = admin.all("User", userName=name)
        body = {"userName": name, "lastName": name, "type": "api", "authMethod": "ApiKey", "isActive": True,
                "rolesIds": [roles[role]], "teamsIds": [teams[project] for project in projects]}
        user = admin.ok("PUT", f"User/{found[0]['id']}", body) if found else admin.ok("POST", "User", body)
        users[name] = {"id": user["id"], "key": admin.ok("GET", f"User/{user['id']}")["apiKey"]}
    return users


def espo_id(world_id):
    """EspoCRM's ID for a record of the world, so both hold the same records under predictable names."""
    return sha1(world_id.encode()).hexdigest()[:17]


def quote(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace("'", "''").replace("\x00", "\\0")
    return "'" + text + "'"


def sql(statements):
    """Run SQL in EspoCRM's database and return the output."""
    return subprocess.run([*COMPOSE, "exec", "-T", "db", "mariadb", "-uespocrm", "-pcontrol-db", "--default-character-set=utf8mb4",
                           "-N", "espocrm"], input=statements, capture_output=True, text=True, check=True).stdout


def rows(teams, admin_id):
    """(table, row) for every record that bench.github writes into World, with the same adjustments."""
    common = lambda world_id: {"id": espo_id(world_id), "deleted": 0, "created_at": LOADED_AT, "modified_at": LOADED_AT,
                               "created_by_id": admin_id}
    team = lambda world_id, entity, project: ("entity_team", {"entity_id": espo_id(world_id), "team_id": teams[project],
                                                              "entity_type": entity, "deleted": 0})
    for type_name, record_id, data, url in generate(Counter()):
        if type_name == "Person":
            yield "c_person", {**common(record_id), "name": data["login"], "bot": data["bot"]}
        elif type_name == "Project":
            yield "c_project", {**common(record_id), "name": data["name"], "description": data["description"]}
            yield team(record_id, "CProject", record_id)
            for person in data["maintainers"]:
                yield "c_project_maintainer", {"c_project_id": espo_id(record_id), "c_person_id": espo_id(person), "deleted": 0}
        elif type_name == "Label":
            yield "c_label", {**common(record_id), "name": data["name"], "project_id": espo_id(data["project"])}
            yield team(record_id, "CLabel", data["project"])
        elif type_name == "Milestone":
            yield "c_milestone", {**common(record_id), "name": data["title"], "number": data["number"], "project_id": espo_id(data["project"])}
            yield team(record_id, "CMilestone", data["project"])
        elif type_name == "Issue":
            yield "c_issue", {**common(record_id), "name": data["title"], "description": data["body"], "number": data["number"],
                              "kind": data["kind"], "state": data["state"], "close_reason": data["close_reason"], "locked": data["locked"],
                              "opened_on": data["opened_on"], "closed_on": data["closed_on"], "project_id": espo_id(data["project"]),
                              "author_id": espo_id(data["author"]), "milestone_id": espo_id(data["milestone"]) if data["milestone"] else None}
            yield team(record_id, "CIssue", data["project"])
            for label in data["labels"]:
                yield "c_issue_label", {"c_issue_id": espo_id(record_id), "c_label_id": espo_id(label), "deleted": 0}
            for person in data["assignees"]:
                yield "c_issue_assignee", {"c_issue_id": espo_id(record_id), "c_person_id": espo_id(person), "deleted": 0}
        elif type_name == "Comment":
            project, number = data["issue"].rsplit("-", 1)
            yield "c_comment", {**common(record_id), "name": f"Comment by {data['author']} on {project} #{number}",
                                "description": data["body"], "posted_on": data["posted_on"], "issue_id": espo_id(data["issue"]),
                                "author_id": espo_id(data["author"])}


BACKED_UP = ("c_issue", "c_issue_label", "c_issue_assignee", "c_label", "c_milestone", "c_project", "c_project_maintainer", "c_person", "entity_team")


def load():
    admin = Api()
    admin_id = admin.ok("GET", "App/user")["user"]["id"]
    teams = {team["name"]: team["id"] for team in admin.ok("GET", "Team?maxSize=200")["list"]}
    if int(sql("SELECT COUNT(*) FROM c_issue;").strip()):
        raise SystemExit("EspoCRM already holds issues. Remove the world-control-tracker volumes to load again.")
    started = time.perf_counter()
    process = subprocess.Popen([*COMPOSE, "exec", "-T", "db", "mariadb", "-uespocrm", "-pcontrol-db", "--default-character-set=utf8mb4",
                                "espocrm"], stdin=subprocess.PIPE, text=True)
    batch, size, current, written = [], 0, None, 0

    def flush():
        if batch:
            columns = list(batch[0])
            process.stdin.write(f"INSERT INTO {current} ({', '.join(columns)}) VALUES "
                                + ", ".join("(" + ", ".join(quote(row[column]) for column in columns) + ")" for row in batch) + ";\n")
            batch.clear()

    process.stdin.write("SET autocommit = 0; SET unique_checks = 0; SET foreign_key_checks = 0;\n")
    for name, row in rows(teams, admin_id):
        length = sum(len(str(value)) for value in row.values())
        # Keep each statement well under MariaDB's 16 MB packet limit; some bodies are 250 KB.
        if name != current or len(batch) == 1000 or size + length > 4_000_000 or (batch and list(batch[0]) != list(row)):
            flush()
            current, size = name, 0
        batch.append(row)
        size += length
        written += 1
        if written % 100000 == 0:
            process.stdin.write("COMMIT;\n")
            print(f"{written:,} rows, {time.perf_counter() - started:.0f}s", flush=True)
    flush()
    process.stdin.write("COMMIT;\n")
    # A copy of every table a task can change, so that each task starts from the loaded state.
    process.stdin.write("".join(f"DROP TABLE IF EXISTS loaded_{table}; CREATE TABLE loaded_{table} LIKE {table}; "
                                f"INSERT INTO loaded_{table} SELECT * FROM {table}; COMMIT;\n" for table in BACKED_UP))
    process.stdin.close()
    if process.wait():
        raise SystemExit("MariaDB refused the load.")
    print(f"Loaded {written:,} rows in {time.perf_counter() - started:.0f}s")


def reset(users):
    """Return EspoCRM to the loaded records: remove what tasks created, and restore what they changed."""
    statements = ["SET foreign_key_checks = 0;"]
    for table in ("c_comment", "c_issue", "c_label", "c_milestone", "c_project", "c_person"):
        statements.append(f"DELETE FROM {table} WHERE created_at > '{LOADED_AT}';")
    for table in ("c_issue", "c_label", "c_milestone", "c_project", "c_person"):
        statements.append(f"REPLACE INTO {table} SELECT * FROM loaded_{table} WHERE id IN "
                          f"(SELECT id FROM (SELECT id FROM {table} WHERE modified_at > '{LOADED_AT}' OR deleted = 1) AS changed);")
    statements.append(f"UPDATE c_comment SET deleted = 0 WHERE deleted = 1 AND created_at <= '{LOADED_AT}';")
    # Links can change without touching either record, so their tables are compared whole.
    for table in ("c_issue_label", "c_issue_assignee", "c_project_maintainer", "entity_team"):
        statements.append(f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM loaded_{table});")
        statements.append(f"INSERT INTO {table} SELECT * FROM loaded_{table} WHERE id NOT IN (SELECT id FROM {table});")
        statements.append(f"REPLACE INTO {table} SELECT * FROM loaded_{table} WHERE id IN "
                          f"(SELECT id FROM (SELECT t.id FROM {table} t JOIN loaded_{table} l USING (id) WHERE t.deleted != l.deleted) AS changed);")
    sql("\n".join(statements) + "\n")
    return {}


def _where(entity, *conditions, select="name", size=200):
    """A list request. Each condition is (type, attribute, value); a list value is sent as an array."""
    query = f"{entity}?maxSize={size}" + (f"&select={select}" if select else "")
    for index, (kind, attribute, value) in enumerate(conditions):
        query += f"&where[{index}][type]={kind}&where[{index}][attribute]={attribute}"
        for item in (value if isinstance(value, list) else [value]):
            query += f"&where[{index}][value]{'[]' if isinstance(value, list) else ''}={espocrm.quote(str(item))}"
    return query


def _project(client, repo):
    return client.ok("GET", _where("CProject", ("equals", "name", repo)))["list"][0]["id"]


def _issue(client, repo, number, select="name,state,closedOn,authorName,locked"):
    project = _project(client, repo)
    return client.ok("GET", _where("CIssue", ("equals", "projectId", project), ("equals", "number", number), select=select))["list"][0]


def _label(client, project, name):
    return client.ok("GET", _where("CLabel", ("equals", "projectId", project), ("equals", "name", name)))["list"][0]["id"]


def _me(client):
    login = client.ok("GET", "App/user")["user"]["userName"]
    return client.ok("GET", _where("CPerson", ("equals", "name", login)))["list"][0]["id"]


def _count(client, entity, *conditions):
    return client.ok("GET", _where(entity, *conditions, size=1))["total"]


def solve_read_issue(client):
    issue = _issue(client, "redis/redis", 3067)
    return f"#3067: {issue['name']}. It is {issue['state']}."


def solve_read_pr(client):
    issue = _issue(client, "redis/redis", 13512)
    return f"{issue['authorName']} opened it; it was merged on {issue['closedOn']}."


def solve_read_labels(client):
    issue = _issue(client, "vuejs/core", 14761, select="name")
    return ", ".join(client.ok("GET", f"CIssue/{issue['id']}")["labelsNames"].values())


def solve_count_label(client):
    project = _project(client, "redis/redis")
    label = _label(client, project, "crash report")
    total = _count(client, "CIssue", ("equals", "projectId", project), ("equals", "state", "open"), ("equals", "kind", "issue"),
                   ("linkedWith", "labels", [label]))
    return f"{total} open issues."


def solve_count_mine(client):
    me = _me(client)
    return f"{_count(client, 'CIssue', ('equals', 'authorId', me))} issues, " \
           f"{_count(client, 'CIssue', ('equals', 'authorId', me), ('equals', 'state', 'open'))} still open."


def solve_assigned(client):
    me, project = _me(client), _project(client, "redis/redis")
    found = client.ok("GET", _where("CIssue", ("equals", "projectId", project), ("equals", "state", "open"),
                                    ("linkedWith", "assignees", [me]), select="number"))["list"]
    return ", ".join(f"#{issue['number']}" for issue in found)


def solve_count_milestone(client):
    project = _project(client, "redis/redis")
    milestone = client.ok("GET", _where("CMilestone", ("equals", "projectId", project), ("equals", "name", "Redis 6.0.x")))["list"][0]["id"]
    return f"{_count(client, 'CIssue', ('equals', 'milestoneId', milestone))} in the milestone, " \
           f"{_count(client, 'CIssue', ('equals', 'milestoneId', milestone), ('equals', 'state', 'open'))} still open."


def solve_search_mine(client):
    me = _me(client)
    found = client.ok("GET", _where("CIssue", ("equals", "authorId", me), ("contains", "name", "stdout"), select="number,state"))["list"]
    return ", ".join(f"#{issue['number']} ({issue['state']})" for issue in found)


def solve_search_open(client):
    project = _project(client, "vuejs/core")
    found = client.ok("GET", _where("CIssue", ("equals", "projectId", project), ("equals", "kind", "issue"), ("equals", "state", "open"),
                                    ("contains", "name", "teleport"), ("contains", "name", "empty"), select="number"))["list"]
    return ", ".join(f"#{issue['number']}" for issue in found)


def solve_search_comments(client):
    issue = _issue(client, "redis/redis", 9767)
    found = client.ok("GET", _where("CComment", ("equals", "issueId", issue["id"]), ("contains", "description", "valgrind"), select="authorName"))["list"]
    return ", ".join(comment["authorName"] for comment in found)


def _comment(client, issue, me, body):
    client.ok("POST", "CComment", {"name": "Comment", "description": body, "postedOn": now()[:10], "issueId": issue, "authorId": me})


def solve_comment(client):
    issue, me = _issue(client, "redis/redis", 15058), _me(client)
    _comment(client, issue["id"], me, "I see the same with 8.6.2 on Debian 12.")
    return "Commented on #15058."


def solve_label(client):
    issue = _issue(client, "redis/redis", 15205, select="name,projectId")
    client.ok("POST", f"CIssue/{issue['id']}/labels", {"id": _label(client, issue["projectId"], "crash report")})
    return "Labelled #15205 crash report."


def solve_close(client):
    issue = _issue(client, "redis/redis", 15063)
    client.ok("PUT", f"CIssue/{issue['id']}", {"state": "closed", "closeReason": "not_planned", "closedOn": now()[:10]})
    return "Closed #15063 as not planned."


def solve_open_issue(client):
    project, me = _project(client, "redis/redis"), _me(client)
    client.ok("POST", "CIssue", {"name": "SCAN with MATCH skips keys after a resize", "projectId": project, "authorId": me,
                                 "description": "While the table grows, SCAN with a MATCH pattern returns fewer keys than KEYS does.",
                                 "openedOn": now()[:10], "kind": "issue", "state": "open"})
    return "Opened the issue."


def solve_triage(client):
    issue, me = _issue(client, "redis/redis", 15009, select="name,projectId"), _me(client)
    client.ok("PUT", f"CIssue/{issue['id']}", {"assigneesIds": [me], "labelsIds": [_label(client, issue["projectId"], "crash report")]})
    _comment(client, issue["id"], me, "Looking into this.")
    return "Assigned to me, labelled, and commented."


def solve_duplicate(client):
    issue, me = _issue(client, "redis/redis", 15211), _me(client)
    client.ok("PUT", f"CIssue/{issue['id']}", {"state": "closed", "closeReason": "duplicate", "closedOn": now()[:10]})
    _comment(client, issue["id"], me, "Duplicate of #15208.")
    return "Closed #15211 as a duplicate of #15208."


def solve_milestone(client):
    project = _project(client, "redis/redis")
    milestone = client.ok("POST", "CMilestone", {"name": "Redis 8.8", "projectId": project})["id"]
    found = client.ok("GET", _where("CIssue", ("equals", "projectId", project), ("in", "number", [15145, 15181])))["list"]
    for issue in found:
        client.ok("PUT", f"CIssue/{issue['id']}", {"milestoneId": milestone})
    return "Created Redis 8.8 with #15145 and #15181."


def solve_locked(client):
    issue = _issue(client, "microsoft/TypeScript", 22446)
    assert issue["locked"]
    return "I cannot comment: the issue is locked, and only the project's maintainers may comment on it."


def solve_other_project(client):
    issue = _issue(client, "redis/redis", 15269, select="name,projectId")
    status, _ = client.call("PUT", f"CIssue/{issue['id']}", {"labelsIds": [_label(client, issue["projectId"], "crash report")]})
    assert status == 403, status
    return "I cannot label it: only redis's maintainers may change its labels, and I am not one."


def solve_assign(client):
    issue = _issue(client, "microsoft/TypeScript", 19584)
    person = client.ok("GET", _where("CPerson", ("equals", "name", "RyanCavanaugh")))["list"][0]["id"]
    status, _ = client.call("PUT", f"CIssue/{issue['id']}", {"assigneesIds": [person]})
    assert status == 403, status
    return "I cannot assign it: only TypeScript's maintainers may assign issues."


def checks(ids, started):
    """Checkers for the same outcomes as bench.tracker, read from EspoCRM as the administrator."""
    admin = Api()
    issue = lambda world_id: admin.ok("GET", f"CIssue/{espo_id(world_id)}")
    users = {user["userName"]: user["id"] for user in admin.ok("GET", "User?maxSize=200&select=userName")["list"]}

    def new(entity, *conditions):
        # A reset removes whatever was created after the load, so anything newer is the task's. EspoCRM's
        # "after" counts whole seconds, so the task's own start time would miss a record made within the same second.
        return admin.ok("GET", _where(entity, *conditions, ("after", "createdAt", LOADED_AT), select=None))["list"]

    def by(record, login):
        """Whether a person wrote this: named as its author, or created it through their own key."""
        return record.get("authorId") == espo_id(login) or record.get("createdById") == users.get(login)

    def comments(world_id, login, *words):
        return [record for record in new("CComment", ("equals", "issueId", espo_id(world_id)))
                if by(record, login) and tracker._says(record.get("description") or "", *words)]

    def comment(answer):
        found = new("CComment", ("equals", "issueId", espo_id("redis-15058")))
        if len(found) != 1:
            return [f"Expected one new comment on redis #15058, found {len(found)}."]
        failures = [] if by(found[0], "oresoftware") else ["The comment is not by ORESoftware."]
        return failures + ([] if tracker._says(found[0].get("description") or "", "8.6.2", "debian 12") else ["The comment does not say what was asked."])

    def label(answer):
        return [] if issue("redis-15205")["labelsIds"] == [espo_id("redis-label-crash-report")] else ["redis #15205 is not labelled crash report alone."]

    def close(answer):
        record = issue("redis-15063")
        return [] if record["state"] == "closed" and record["closeReason"] == "not_planned" else ["redis #15063 is not closed as not planned."]

    def open_issue(answer):
        found = [record for record in new("CIssue", ("equals", "projectId", espo_id("redis"))) if "scan" in record["name"].lower()]
        if len(found) != 1:
            return [f"Expected one new redis issue about SCAN, found {len(found)}."]
        record, failures = found[0], []
        if not by(record, "oresoftware") or record["state"] != "open" or record["kind"] != "issue":
            failures.append("The issue is not an open issue by ORESoftware.")
        if not (tracker._says(record["name"], "resize") and tracker._says(record.get("description") or "", "keys")):
            failures.append("The title or description is not the one asked for.")
        return failures

    def triage(answer):
        record, failures = issue("redis-15009"), []
        if record["assigneesIds"] != [espo_id("sundb")]:
            failures.append("redis #15009 is not assigned to sundb alone.")
        if record["labelsIds"] != [espo_id("redis-label-crash-report")]:
            failures.append("redis #15009 is not labelled crash report.")
        if not comments("redis-15009", "sundb", "looking into"):
            failures.append("No comment from sundb saying they are looking into it.")
        return failures

    def duplicate(answer):
        record, failures = issue("redis-15211"), []
        if record["state"] != "closed" or record["closeReason"] != "duplicate":
            failures.append("redis #15211 is not closed as a duplicate.")
        if not comments("redis-15211", "sundb", "15208"):
            failures.append("No comment on #15211 points to #15208.")
        if issue("redis-15208")["state"] != "open":
            failures.append("redis #15208 was closed too.")
        return failures

    def milestone(answer):
        found = [record for record in new("CMilestone", ("equals", "projectId", espo_id("redis"))) if "8.8" in record["name"]]
        if len(found) != 1:
            return [f"Expected one new redis milestone named Redis 8.8, found {len(found)}."]
        missing = [world_id for world_id in ("redis-15145", "redis-15181") if issue(world_id)["milestoneId"] != found[0]["id"]]
        return [f"{', '.join(missing)} not in the new milestone."] if missing else []

    def locked(answer):
        failures = ["A comment was added to the locked issue."] if new("CComment", ("equals", "issueId", espo_id("typescript-22446"))) else []
        return failures + tracker._refused(answer)

    def other_project(answer):
        return (["redis #15269 was labelled."] if issue("redis-15269")["labelsIds"] else []) + tracker._refused(answer)

    def assign(answer):
        return (["typescript #19584 was assigned."] if issue("typescript-19584")["assigneesIds"] else []) + tracker._refused(answer)

    # Reading, counting, and searching are judged by the answer alone, as for World.
    answered = {task.id: (lambda check: lambda answer: check(None, answer))(task.check) for task in tracker.TASKS[:10]}
    return {**answered, "comment": comment, "label": label, "close": close, "open-issue": open_issue, "triage": triage,
            "duplicate": duplicate, "milestone": milestone, "locked": locked, "other-project": other_project, "assign": assign}


SOLVERS = {name.removeprefix("solve_").replace("_", "-"): value for name, value in globals().items() if name.startswith("solve_")}
TASKS = [Task(task.id, task.actor, task.prompt, SOLVERS[task.id], None) for task in tracker.TASKS]


def main():
    parser = argparse.ArgumentParser(prog="python3 -m bench.control.tracker", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("setup")
    commands.add_parser("load")
    args = parser.parse_args()
    print(json.dumps(setup(), indent=2)) if args.mode == "setup" else load()


if __name__ == "__main__":
    main()
