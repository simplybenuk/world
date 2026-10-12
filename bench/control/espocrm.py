"""The control: the same sales team and the same tasks in EspoCRM, through its own API.

Start it with `docker compose -f bench/control/compose.yaml up -d`. Records are
matched by name, because EspoCRM assigns its own IDs.
"""

from base64 import b64encode
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import quote

from .. import crm
from ..crm import REFUSALS, Task

HOST, PORT = "127.0.0.1", 8791
ADMIN = "Basic " + b64encode(b"admin:control-admin").decode()
STAGES = {"lead": "Prospecting", "qualified": "Qualification", "proposal": "Proposal",
          "negotiation": "Negotiation", "won": "Closed Won", "lost": "Closed Lost"}
OPEN_STAGES = [STAGES[stage] for stage in crm.OPEN_STAGES]
RECORD_TYPES = ("Note", "Call", "Meeting", "Opportunity", "Contact", "Account")

# Reps read every account and contact, and only the opportunities assigned to them.
ROLES = {
    "Rep": {
        "Account": {"create": "yes", "read": "all", "edit": "own", "delete": "no", "stream": "all"},
        "Contact": {"create": "yes", "read": "all", "edit": "own", "delete": "no", "stream": "all"},
        "Opportunity": {"create": "yes", "read": "own", "edit": "own", "delete": "no", "stream": "own"},
        "Call": {"create": "yes", "read": "own", "edit": "own", "delete": "no"},
        "Meeting": {"create": "yes", "read": "own", "edit": "own", "delete": "no"},
    },
    "Viewer": {name: {"create": "no", "read": "all", "edit": "no", "delete": "no", "stream": "all"}
               for name in ("Account", "Contact", "Opportunity", "Call", "Meeting")},
}
USERS = {"priya": "Rep", "tom": "Rep", "nina": "Viewer"}

# World enforces this in its CRM definition. EspoCRM has no such rule as shipped,
# so an administrator adds it as a before-save script on the entity.
CLOSED_RULE = """if (
    (entity\\attributeFetched('stage') == 'Closed Won' || entity\\attributeFetched('stage') == 'Closed Lost')
    && env\\userAttribute('type') != 'admin'
) {
    recordService\\throwForbidden('A closed opportunity can only be changed by an administrator.');
}"""


class Api:
    """JSON requests to EspoCRM as one user."""

    def __init__(self, authorization=ADMIN, api_key=None, port=PORT):
        self.headers = {"X-Api-Key": api_key} if api_key else {"Authorization": authorization}
        self.port = port

    def call(self, method, path, body=None):
        connection = HTTPConnection(HOST, self.port, timeout=60)
        try:
            headers = dict(self.headers)
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, "/api/v1/" + path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                return response.status, json.loads(raw) if raw else None
            except ValueError:
                return response.status, raw.decode(errors="replace")
        finally:
            connection.close()

    def ok(self, method, path, body=None):
        status, value = self.call(method, path, body)
        if status >= 300:
            raise RuntimeError(f"EspoCRM rejected {method} {path}: {status} {value}")
        return value

    def all(self, entity, **where):
        query = "".join(
            f"&where[{index}][type]=equals&where[{index}][attribute]={quote(field)}&where[{index}][value]={quote(str(value))}"
            for index, (field, value) in enumerate(where.items()))
        return self.ok("GET", f"{entity}?maxSize=200{query}")["list"]

    def one(self, entity, **where):
        (record,) = self.all(entity, **where)
        return record


def setup():
    """Create the roles, the users, and the closed-opportunity rule once. Returns each user's ID and API key."""
    admin = Api()
    roles = {}
    for name, data in ROLES.items():
        found = admin.all("Role", name=name)
        body = {"name": name, "data": data, "assignmentPermission": "all", "userPermission": "all"}
        roles[name] = (admin.ok("PUT", f"Role/{found[0]['id']}", body) if found else admin.ok("POST", "Role", body))["id"]
    users = {}
    for name, role in USERS.items():
        found = admin.all("User", userName=name)
        user = found[0] if found else admin.ok("POST", "User", {
            "userName": name, "lastName": name.capitalize(), "type": "api", "authMethod": "ApiKey", "isActive": True,
            "rolesIds": [roles[role]]})
        users[name] = {"id": user["id"], "key": admin.ok("GET", f"User/{user['id']}")["apiKey"]}
    admin.ok("POST", "EntityManager/action/formula", {"scope": "Opportunity", "data": {"beforeSaveApiScript": CLOSED_RULE}})
    admin.call("POST", "Admin/rebuild")
    return users


def reset(users):
    """Remove every record and load the same seed as the CRM world."""
    import worlds
    admin = Api()
    for entity in RECORD_TYPES:
        for record in admin.all(entity):
            admin.ok("DELETE", f"{entity}/{record['id']}")
    ids = {"ben": admin.ok("GET", "App/user")["user"]["id"], **{name: user["id"] for name, user in users.items()}}
    keys = {name: user["key"] for name, user in users.items()}
    today = date.today()
    for type_name, record_id, data in worlds.load("crm").seed(today):
        if type_name == "Organization":
            ids[record_id] = admin.ok("POST", "Account", {
                "name": data["name"], "website": data["website"], "industry": data["industry"] if data["industry"] in ("Wholesale", "Healthcare") else None,
                "type": "Customer" if data["status"] == "customer" else None, "assignedUserId": ids[data["owner"]]})["id"]
            ids["owner:" + record_id] = ids[data["owner"]]
        elif type_name == "Contact":
            first, last = data["name"].split(" ", 1)
            ids[record_id] = admin.ok("POST", "Contact", {
                "firstName": first, "lastName": last, "title": data["job_title"], "emailAddress": data["email"],
                "phoneNumber": data["phone"], "accountId": ids[data["organization"]],
                "assignedUserId": ids["owner:" + data["organization"]]})["id"]
        elif type_name == "Opportunity":
            # EspoCRM requires an amount and a close date on every opportunity.
            ids[record_id] = admin.ok("POST", "Opportunity", {
                "name": data["name"], "accountId": ids[data["organization"]], "contactId": ids[data["primary_contact"]],
                "contactsIds": [ids[data["primary_contact"]]], "assignedUserId": ids[data["owner"]],
                "stage": STAGES[data["stage"]], "amount": data["amount"] or 0, "amountCurrency": "USD",
                "closeDate": data["close_date"] or (today + timedelta(days=90)).isoformat(),
                "description": f"Lost: {data['lost_reason']}" if data["lost_reason"] else None})["id"]
        elif type_name == "Interaction":
            parent = ("Opportunity", data["opportunity"]) if data["opportunity"] else ("Account", data["organization"])
            author = Api(api_key=keys[data["author"]]) if data["author"] in keys else admin
            author.ok("POST", "Note", {"type": "Post", "post": f"{data['kind'].capitalize()} on {data['occurred_on']}: {data['summary']}",
                                       "parentType": parent[0], "parentId": ids[parent[1]]})
    return ids


def _posts(admin, opportunity_id, since):
    return [note for note in admin.all("Note", parentId=opportunity_id) if note["type"] == "Post" and note["createdAt"] >= since]


def solve_pipeline(client):
    opportunities = client.ok("GET", "Opportunity?maxSize=200&select=name,amount,stage&where[0][type]=in&where[0][attribute]=stage"
                              + "".join(f"&where[0][value][]={quote(stage)}" for stage in OPEN_STAGES))["list"]
    return f"{len(opportunities)} open opportunities worth {sum(round(record['amount']) for record in opportunities)}"


def solve_win(client):
    opportunity = client.ok("GET", "Opportunity?select=name&where[0][type]=contains&where[0][attribute]=name&where[0][value]=renewal")["list"][0]
    client.ok("PUT", f"Opportunity/{opportunity['id']}", {"stage": "Closed Won", "closeDate": date.today().isoformat()})
    client.ok("POST", "Note", {"type": "Post", "post": "Northwind signed the renewal for $48,000.",
                               "parentType": "Opportunity", "parentId": opportunity["id"]})
    return "Marked the renewal as won and added a note."


def solve_lose(client):
    opportunity = client.ok("GET", "Opportunity?select=name&where[0][type]=contains&where[0][attribute]=name&where[0][value]=Harbor")["list"][0]
    client.ok("PUT", f"Opportunity/{opportunity['id']}", {"stage": "Closed Lost", "description": "Lost: budget frozen."})
    return "Marked the pilot as lost: budget frozen."


def solve_contact(client):
    opportunity = client.ok("GET", "Opportunity?select=name,accountId&where[0][type]=contains&where[0][attribute]=name&where[0][value]=Harbor")["list"][0]
    contact = client.ok("POST", "Contact", {"firstName": "Jo", "lastName": "Park", "title": "Operations Manager",
                                            "emailAddress": "jo@harbor.example", "accountId": opportunity["accountId"]})
    client.ok("PUT", f"Opportunity/{opportunity['id']}", {"contactId": contact["id"]})
    return "Added Jo Park and made them the pilot's primary contact."


def solve_closed(client):
    opportunity = client.ok("GET", "Opportunity?select=name,stage&where[0][type]=contains&where[0][attribute]=name&where[0][value]=training")["list"][0]
    status, _ = client.call("PUT", f"Opportunity/{opportunity['id']}", {"amount": 9500})
    assert status == 403, status
    return "I cannot change it: the opportunity is closed and only an administrator can change it."


def checks(ids, started):
    """Checkers for the same outcomes as bench.crm, read from EspoCRM as the administrator."""
    admin = Api()

    def opportunity(name):
        return admin.ok("GET", f"Opportunity/{ids[name]}")

    def win(answer):
        record, failures = opportunity("northwind-renewal"), []
        if record["stage"] != "Closed Won" or round(record["amount"]) != 48000:
            failures.append("The renewal is not won at 48,000.")
        if not _posts(admin, record["id"], started) and not record.get("description"):
            failures.append("No note about the renewal was recorded.")
        return failures

    def lose(answer):
        record = opportunity("harbor-pilot")
        said = " ".join([record.get("description") or ""] + [note.get("post") or "" for note in _posts(admin, record["id"], started)])
        return [] if record["stage"] == "Closed Lost" and "budget" in said.lower() else ["The pilot is not lost with a reason."]

    def contact(answer):
        found = admin.all("Contact", lastName="Park")
        if len(found) != 1:
            return [f"Expected one new contact named Jo Park, found {len(found)}."]
        failures = []
        if found[0].get("accountId") != ids["harbor"] or found[0].get("emailAddress") != "jo@harbor.example":
            failures.append("Jo Park is not at Harbor Logistics with the given email.")
        if opportunity("harbor-pilot").get("contactId") != found[0]["id"]:
            failures.append("Jo Park is not the pilot's primary contact.")
        return failures

    def closed(answer):
        failures = []
        if round(opportunity("alder-training")["amount"]) != 9000:
            failures.append("The closed opportunity was changed.")
        if not any(word in answer.lower() for word in REFUSALS):
            failures.append("The answer does not say the change was not possible.")
        return failures

    return {"pipeline": lambda answer: crm.check_pipeline(None, answer), "win": win, "lose": lose, "contact": contact, "closed": closed}


SOLVERS = {"pipeline": solve_pipeline, "win": solve_win, "lose": solve_lose, "contact": solve_contact, "closed": solve_closed}
TASKS = [Task(task.id, task.actor, task.prompt, SOLVERS[task.id], None) for task in crm.TASKS]


class Meter:
    """A local proxy in front of EspoCRM that counts requests, as World's server does for itself."""

    def __init__(self):
        self.requests = []
        meter = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def forward(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                upstream = HTTPConnection(HOST, PORT, timeout=120)
                try:
                    headers = {key: value for key, value in self.headers.items() if key.lower() not in ("host", "connection")}
                    upstream.request(self.command, self.path, body, headers)
                    response = upstream.getresponse()
                    data = response.read()
                finally:
                    upstream.close()
                meter.requests.append((self.command, self.path, response.status, len(data)))
                self.send_response(response.status)
                for key in ("Content-Type", "X-Status-Reason"):
                    if response.getheader(key):
                        self.send_header(key, response.getheader(key))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = forward

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((HOST, 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.url = f"http://{HOST}:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def usage(self):
        return {"calls": len(self.requests), "rejected": sum(status >= 400 for _, _, status, _ in self.requests),
                "bytes_read": sum(size for _, _, _, size in self.requests)}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def faults(ids):
    """The ten mistakes of bench.crm.FAULTS, as a client of EspoCRM would make them.

    Each entry is (id, actor, method, path, body, extra headers, harm). `harm`
    reads the result as the administrator and returns what went wrong when
    EspoCRM accepted the request, or None.
    """
    admin = Api()
    harbor, rollout, training, renewal = (f"Opportunity/{ids[name]}" for name in ("harbor-pilot", "alder-rollout", "alder-training", "northwind-renewal"))
    amount = lambda path, expected: None if round(admin.ok("GET", path)["amount"]) == expected else "the amount did not change"
    return [
        ("stale-version", "priya", "PUT", harbor, {"amount": 25000}, {"X-Version-Number": "7"}, lambda body: "an out-of-date write was saved"),
        ("missing-version", "priya", "PUT", harbor, {"amount": 25000}, {}, lambda body: None),
        ("wrong-enum", "priya", "PUT", renewal, {"stage": "closed-won"}, {}, lambda body: "an unknown stage was saved"),
        ("constraint", "tom", "PUT", rollout, {"stage": "Closed Won"}, {}, lambda body: "won with an amount of zero"),
        ("unknown-field", "priya", "PUT", harbor, {"value": 50000}, {}, lambda body: amount(harbor, 50000)),
        ("missing-required", "priya", "POST", "Call", {"name": "Spoke to Mei", "status": "Held", "parentType": "Account", "parentId": ids["harbor"]}, {},
         lambda body: "a call with no date was saved"),
        ("wrong-reference", "priya", "POST", "Contact", {"firstName": "Jo", "lastName": "Park", "accountId": "harbor-logistics"}, {},
         lambda body: "the contact was saved with no organization" if not admin.ok("GET", f"Contact/{body['id']}").get("accountName") else None),
        ("wrong-type", "priya", "POST", "Deal", {"name": "Harbor expansion", "accountId": ids["harbor"]}, {}, lambda body: "a record of an unknown type was saved"),
        ("wrong-filter", "priya", "GET", "Opportunity?where[0][type]=equals&where[0][field]=stage&where[0][value]=Proposal", None, {},
         lambda body: None if body["total"] == 1 else f"{body['total']} records came back where 1 matches"),
        ("denied", "tom", "PUT", training, {"amount": 9500}, {}, lambda body: "a closed opportunity was changed"),
    ]


def measure_faults(users):
    rows = []
    mistakes = {fault.id: fault.mistake for fault in crm.FAULTS}
    for index in range(len(crm.FAULTS)):
        ids = reset(users)
        identifier, actor, method, path, body, extra, harm = faults(ids)[index]
        connection = HTTPConnection(HOST, PORT, timeout=60)
        try:
            headers = {"X-Api-Key": users[actor]["key"], **extra, **({"Content-Type": "application/json"} if body is not None else {})}
            connection.request(method, "/api/v1/" + path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            raw = response.read().decode(errors="replace")
            reason = response.getheader("X-Status-Reason") or ""
        finally:
            connection.close()
        accepted = response.status < 300
        rows.append({
            "fault": identifier, "mistake": mistakes[identifier], "status": response.status, "reason": reason, "body": raw[:400],
            "harm": harm(json.loads(raw)) if accepted else None,
        })
    return rows


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
