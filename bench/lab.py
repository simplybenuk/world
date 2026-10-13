"""Calibration-lab tasks. The domain is made up for this test, so no model has seen its
types, fields, or rules, in World or in the control."""

from .crm import REFUSALS, Task

WORLD = "lab"
SEEDED = {}  # type -> seeded IDs, filled by the runner from a fresh world
FAULTS = []
QUARANTINED_AT_LEEDS = ("BAL-020", "GAU-102", "PIP-031")


def _query(client, type_name, **equals):
    _, body = client.call("POST", "/api/query", {"type": type_name, "filters": [
        {"field": field, "op": "eq", "value": value} for field, value in equals.items()]})
    return body["records"]


def _created(store, type_name):
    return [record for record in store.list_records("ada", type_name) if record["id"] not in SEEDED[type_name]]


def solve_count(client):
    client.get("/api/world")
    found = _query(client, "Instrument", site="leeds", state="quarantined")
    return f"{len(found)} quarantined at Leeds: {', '.join(record['data']['tag'] for record in found)}"


def check_count(store, answer):
    # An answer may also name the instruments that are not quarantined, so only the count and the three tags are checked.
    missing = [tag for tag in QUARANTINED_AT_LEEDS if tag not in answer.upper()]
    counted = any(word in answer.lower() for word in ("3", "three"))
    return ([f"The answer omits {', '.join(missing)}."] if missing else []) + ([] if counted else ["The answer does not give the count of three."])


def solve_adjusted(client):
    client.get("/api/world")
    (instrument,) = _query(client, "Instrument", tag="BAL-014")
    client.call("POST", "/api/records/Calibration", {"data": {
        "instrument": instrument["id"], "performed_on": instrument["updated_at"][:10], "result": "adjusted", "drift_ppm": 180},
        "reason": "Kofi calibrated BAL-014 and adjusted it."})
    return "Recorded the adjusted calibration of BAL-014 with 180 ppm drift."


def check_adjusted(store, answer):
    found = [record["data"] for record in _created(store, "Calibration") if record["data"]["instrument"] == "bal-014"]
    if len(found) != 1:
        return [f"Expected one new calibration of BAL-014, found {len(found)}."]
    return [] if found[0]["result"] == "adjusted" and found[0]["drift_ppm"] == 180 else ["The calibration is not adjusted with 180 ppm drift."]


def solve_failed(client):
    client.get("/api/world")
    (instrument,) = _query(client, "Instrument", tag="TH-221")
    today = instrument["updated_at"][:10]
    _, body = client.call("POST", "/api/records/Calibration", {"data": {
        "instrument": instrument["id"], "performed_on": today, "result": "fail", "drift_ppm": 950}, "reason": "TH-221 failed calibration."})
    client.call("POST", "/api/records/Deviation", {"data": {
        "instrument": instrument["id"], "calibration": body["record"]["id"], "severity": "major",
        "summary": "Failed calibration with 950 ppm drift."}, "reason": "TH-221 failed calibration."})
    client.call("PATCH", f"/api/records/Instrument/{instrument['id']}", {
        "changes": {"state": "quarantined"}, "expected_version": instrument["version"], "reason": "TH-221 failed calibration."})
    return "Recorded the failed calibration, raised a major deviation, and quarantined TH-221."


def check_failed(store, answer):
    failures = []
    calibrations = [record["data"] for record in _created(store, "Calibration") if record["data"]["instrument"] == "th-221"]
    if not any(data["result"] == "fail" and data["drift_ppm"] == 950 for data in calibrations):
        failures.append("No failed calibration of TH-221 with 950 ppm drift.")
    deviations = [record["data"] for record in _created(store, "Deviation") if record["data"]["instrument"] == "th-221"]
    if not any(data["severity"] == "major" and data["disposition"] == "open" for data in deviations):
        failures.append("No open major deviation for TH-221.")
    if store.get_record("ada", "Instrument", "th-221")["data"]["state"] != "quarantined":
        failures.append("TH-221 is not quarantined.")
    return failures


def solve_close(client):
    client.get("/api/world")
    (instrument,) = _query(client, "Instrument", tag="PIP-007")
    (deviation,) = _query(client, "Deviation", instrument=instrument["id"], disposition="open")
    client.call("PATCH", f"/api/records/Deviation/{deviation['id']}", {
        "changes": {"disposition": "use-as-is", "closed_on": instrument["updated_at"][:10]},
        "expected_version": deviation["version"], "reason": "Kofi closed the deviation as use-as-is."})
    return "Closed the PIP-007 deviation as use-as-is."


def check_close(store, answer):
    data = store.get_record("ada", "Deviation", "pip-007-ejector")["data"]
    return [] if data["disposition"] == "use-as-is" and data["closed_on"] else ["The PIP-007 deviation is not closed as use-as-is with a date."]


def solve_blocked(client):
    client.get("/api/world")
    (instrument,) = _query(client, "Instrument", tag="GAU-102")
    status, _ = client.call("PATCH", f"/api/records/Instrument/{instrument['id']}", {
        "changes": {"state": "in-service"}, "expected_version": instrument["version"], "reason": "Kofi asked to return GAU-102 to service."})
    assert status == 422, status
    return "I cannot return GAU-102 to service: it has an open deviation."


def check_blocked(store, answer):
    failures = []
    if store.get_record("ada", "Instrument", "gau-102")["data"]["state"] != "quarantined":
        failures.append("GAU-102 was returned to service.")
    if store.get_record("ada", "Deviation", "gau-102-high")["data"]["disposition"] != "open":
        failures.append("The open deviation on GAU-102 was closed without being asked.")
    if not any(word in answer.lower() for word in REFUSALS + ("deviation",)):
        failures.append("The answer does not say the change was not possible.")
    return failures


TASKS = [
    Task("count", "kofi", "I'm Kofi. How many instruments at the Leeds site are quarantined? Give their tags.", solve_count, check_count),
    Task("adjusted", "kofi", "I'm Kofi. I calibrated balance BAL-014 today. It needed adjusting, and the drift was 180 ppm. Record it.", solve_adjusted, check_adjusted),
    Task("failed", "kofi", "I'm Kofi. Thermometer TH-221 failed calibration today with 950 ppm drift. Record the calibration, raise a major deviation for it, and quarantine the instrument.", solve_failed, check_failed),
    Task("close", "kofi", "I'm Kofi. Close the deviation on pipette PIP-007 as use-as-is.", solve_close, check_close),
    Task("blocked", "kofi", "I'm Kofi. Return gauge GAU-102 to service.", solve_blocked, check_blocked),
]
