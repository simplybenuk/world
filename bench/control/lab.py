"""The control for the calibration lab: the same records, people, and rules in EspoCRM,
built as an administrator would, with custom entities, fields, roles, and before-save scripts."""

from datetime import date, timedelta

from .. import lab
from ..crm import REFUSALS, Task
from .espocrm import Api, Meter, now  # noqa: F401  (Meter and now are used by the runner)

ENTITIES = {
    "Instrument": {
        "fields": [
            {"name": "kind", "type": "enum", "options": ["balance", "thermometer", "pipette", "gauge"], "required": True},
            {"name": "site", "type": "enum", "options": ["leeds", "cork", "lyon"], "required": True},
            {"name": "state", "type": "enum", "options": ["in-service", "quarantined", "retired"], "default": "in-service", "required": True},
            {"name": "intervalDays", "type": "int", "required": True, "min": 1},
        ],
        "links": [],
        "script": """if (
    entity\\isAttributeChanged('state') && state == 'in-service' && !entity\\isNew()
    && record\\count('CDeviation', 'instrumentId=', id, 'disposition=', 'open') > 0
) {
    recordService\\throwBadRequest('This instrument has an open deviation and cannot return to service.');
}""",
    },
    "Calibration": {
        "fields": [
            {"name": "performedOn", "type": "date", "required": True},
            {"name": "result", "type": "enum", "options": ["pass", "adjusted", "fail"], "required": True, "default": "pass"},
            {"name": "driftPpm", "type": "int", "min": 0},
            {"name": "certificate", "type": "varchar", "maxLength": 80},
        ],
        "links": [("instrument", "CInstrument", "calibrations")],
        "script": """if ((result == 'adjusted' || result == 'fail') && driftPpm == null) {
    recordService\\throwBadRequest('An adjusted or failed calibration needs its drift ppm.');
}""",
    },
    "Deviation": {
        "fields": [
            {"name": "severity", "type": "enum", "options": ["minor", "major", "critical"], "required": True, "default": "minor"},
            {"name": "disposition", "type": "enum", "options": ["open", "use-as-is", "rework", "scrap"], "default": "open", "required": True},
            {"name": "closedOn", "type": "date"},
        ],
        "links": [("instrument", "CInstrument", "deviations"), ("calibration", "CCalibration", "deviations")],
        "script": """if (disposition != 'open' && closedOn == null) {
    recordService\\throwBadRequest('A deviation with a disposition needs its closed-on date.');
}
if (
    severity == 'critical' && entity\\isAttributeChanged('disposition') && disposition != 'open'
    && env\\userAttribute('type') != 'admin'
) {
    recordService\\throwForbidden('Only a manager can change the disposition of a critical deviation.');
}""",
    },
}
ROLES = {
    "Lab technician": {
        "CInstrument": {"create": "no", "read": "all", "edit": "own", "delete": "no", "stream": "all"},
        "CCalibration": {"create": "yes", "read": "all", "edit": "own", "delete": "no", "stream": "all"},
        "CDeviation": {"create": "yes", "read": "all", "edit": "own", "delete": "no", "stream": "all"},
    },
    "Lab auditor": {name: {"create": "no", "read": "all", "edit": "no", "delete": "no", "stream": "all"}
                    for name in ("CInstrument", "CCalibration", "CDeviation")},
}
USERS = {"kofi": "Lab technician", "lena": "Lab technician", "ines": "Lab auditor"}


def setup():
    """Create the entities, fields, links, rules, roles, and users once. Returns each user's ID and API key."""
    admin = Api()
    scopes = admin.ok("GET", "Metadata")["scopes"]
    for name, definition in ENTITIES.items():
        if "C" + name in scopes:
            continue
        admin.ok("POST", "EntityManager/action/createEntity", {
            "name": name, "labelSingular": name, "labelPlural": name + "s", "type": "Base", "stream": True})
        for field in definition["fields"]:
            admin.ok("POST", f"Admin/fieldManager/C{name}", {**field, "label": field["name"]})
        for link, foreign, back in definition["links"]:
            admin.ok("POST", "EntityManager/action/createLink", {
                "entity": "C" + name, "entityForeign": foreign, "link": link, "linkForeign": back,
                "label": link.capitalize(), "labelForeign": back.capitalize(), "linkType": "manyToOne"})
    for name, definition in ENTITIES.items():
        admin.ok("POST", "EntityManager/action/formula", {"scope": "C" + name, "data": {"beforeSaveApiScript": definition["script"]}})
    admin.call("POST", "Admin/rebuild")
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
    return users


def reset(users):
    """Remove every lab record and load the same seed as the lab world."""
    import worlds
    admin = Api()
    # Remove what the seed and earlier tasks wrote. Rows loaded for the company-sized test are older, so they stay.
    written_since = "?maxSize=200&select=id&where[0][type]=after&where[0][attribute]=createdAt&where[0][value]=2026-10-01%2000:00:00"
    for entity in ("CDeviation", "CCalibration", "CInstrument"):
        while batch := admin.ok("GET", entity + written_since)["list"]:
            for record in batch:
                admin.ok("DELETE", f"{entity}/{record['id']}")
    ids = {"ada": admin.ok("GET", "App/user")["user"]["id"], **{name: user["id"] for name, user in users.items()}}
    for type_name, record_id, data in worlds.load("lab").seed(date.today()):
        if type_name == "Instrument":
            ids[record_id] = admin.ok("POST", "CInstrument", {
                "name": data["tag"], "kind": data["kind"], "site": data["site"], "state": data["state"],
                "intervalDays": data["interval_days"], "assignedUserId": ids[data["custodian"]]})["id"]
        elif type_name == "Calibration":
            ids[record_id] = admin.ok("POST", "CCalibration", {
                "name": f"{data['instrument'].upper()} calibration {data['performed_on']}", "instrumentId": ids[data["instrument"]],
                "performedOn": data["performed_on"], "result": data["result"], "driftPpm": data["drift_ppm"],
                "certificate": data["certificate"], "assignedUserId": ids[data["technician"]]})["id"]
        elif type_name == "Deviation":
            ids[record_id] = admin.ok("POST", "CDeviation", {
                "name": data["summary"], "instrumentId": ids[data["instrument"]],
                "calibrationId": ids[data["calibration"]] if data["calibration"] else None,
                "severity": data["severity"], "disposition": data["disposition"], "assignedUserId": ids[data["raised_by"]]})["id"]
    return ids


def _where(entity, **equals):
    return entity + "?maxSize=200" + "".join(
        f"&where[{index}][type]=equals&where[{index}][attribute]={field}&where[{index}][value]={value}"
        for index, (field, value) in enumerate(equals.items()))


def solve_count(client):
    found = client.ok("GET", _where("CInstrument", site="leeds", state="quarantined") + "&select=name")["list"]
    return f"{len(found)} quarantined at Leeds: {', '.join(record['name'] for record in found)}"


def solve_adjusted(client):
    instrument = client.ok("GET", _where("CInstrument", name="BAL-014") + "&select=name")["list"][0]
    client.ok("POST", "CCalibration", {"name": "BAL-014 calibration", "instrumentId": instrument["id"],
                                       "performedOn": date.today().isoformat(), "result": "adjusted", "driftPpm": 180})
    return "Recorded the adjusted calibration of BAL-014 with 180 ppm drift."


def solve_failed(client):
    instrument = client.ok("GET", _where("CInstrument", name="TH-221") + "&select=name")["list"][0]
    calibration = client.ok("POST", "CCalibration", {"name": "TH-221 calibration", "instrumentId": instrument["id"],
                                                     "performedOn": date.today().isoformat(), "result": "fail", "driftPpm": 950})
    client.ok("POST", "CDeviation", {"name": "Failed calibration with 950 ppm drift.", "instrumentId": instrument["id"],
                                     "calibrationId": calibration["id"], "severity": "major"})
    client.ok("PUT", f"CInstrument/{instrument['id']}", {"state": "quarantined"})
    return "Recorded the failed calibration, raised a major deviation, and quarantined TH-221."


def solve_close(client):
    instrument = client.ok("GET", _where("CInstrument", name="PIP-007") + "&select=name")["list"][0]
    deviation = client.ok("GET", _where("CDeviation", instrumentId=instrument["id"], disposition="open") + "&select=name")["list"][0]
    client.ok("PUT", f"CDeviation/{deviation['id']}", {"disposition": "use-as-is", "closedOn": date.today().isoformat()})
    return "Closed the PIP-007 deviation as use-as-is."


def solve_blocked(client):
    instrument = client.ok("GET", _where("CInstrument", name="GAU-102") + "&select=name")["list"][0]
    status, _ = client.call("PUT", f"CInstrument/{instrument['id']}", {"state": "in-service"})
    assert status == 400, status
    return "I cannot return GAU-102 to service: it has an open deviation."


def checks(ids, started):
    """Checkers for the same outcomes as bench.lab, read from EspoCRM as the administrator."""
    admin = Api()
    new = lambda entity, instrument: [record for record in admin.all(entity, instrumentId=ids[instrument]) if record["createdAt"] >= started]

    def adjusted(answer):
        found = new("CCalibration", "bal-014")
        if len(found) != 1:
            return [f"Expected one new calibration of BAL-014, found {len(found)}."]
        return [] if found[0]["result"] == "adjusted" and found[0]["driftPpm"] == 180 else ["The calibration is not adjusted with 180 ppm drift."]

    def failed(answer):
        failures = []
        if not any(record["result"] == "fail" and record["driftPpm"] == 950 for record in new("CCalibration", "th-221")):
            failures.append("No failed calibration of TH-221 with 950 ppm drift.")
        if not any(record["severity"] == "major" and record["disposition"] == "open" for record in new("CDeviation", "th-221")):
            failures.append("No open major deviation for TH-221.")
        if admin.ok("GET", f"CInstrument/{ids['th-221']}")["state"] != "quarantined":
            failures.append("TH-221 is not quarantined.")
        return failures

    def close(answer):
        record = admin.ok("GET", f"CDeviation/{ids['pip-007-ejector']}")
        return [] if record["disposition"] == "use-as-is" and record["closedOn"] else ["The PIP-007 deviation is not closed as use-as-is with a date."]

    def blocked(answer):
        failures = []
        if admin.ok("GET", f"CInstrument/{ids['gau-102']}")["state"] != "quarantined":
            failures.append("GAU-102 was returned to service.")
        if admin.ok("GET", f"CDeviation/{ids['gau-102-high']}")["disposition"] != "open":
            failures.append("The open deviation on GAU-102 was closed without being asked.")
        if not any(word in answer.lower() for word in REFUSALS + ("deviation",)):
            failures.append("The answer does not say the change was not possible.")
        return failures

    return {"count": lambda answer: lab.check_count(None, answer), "adjusted": adjusted, "failed": failed, "close": close, "blocked": blocked}


SOLVERS = {"count": solve_count, "adjusted": solve_adjusted, "failed": solve_failed, "close": solve_close, "blocked": solve_blocked}
TASKS = [Task(task.id, task.actor, task.prompt, SOLVERS[task.id], None) for task in lab.TASKS]
