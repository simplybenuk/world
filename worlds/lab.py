"""A calibration lab: instruments, their calibrations, and the deviations raised against them."""

from datetime import date, timedelta
from typing import Any

from world.definition import Definition, deny
from world.schema import WorldError


TYPES: dict[str, dict[str, Any]] = {
    "Person": {
        "description": "Someone who works in the lab. Their stored role governs their access.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "email": {"type": "text", "nullable": True, "default": None, "max_length": 254},
            "role": {
                "type": "enum", "required": True, "values": ["manager", "technician", "auditor"], "default": "technician",
                "write_rule": "actor.role == manager",
            },
        },
        "rules": {
            "read": "actor is authenticated",
            "create": "actor.role == manager",
            "update": "actor.role == manager or (actor.role == technician and actor.id == record.id); only manager can change role",
            "delete": "actor.role == manager and no live record references this person and another manager remains",
        },
    },
    "Instrument": {
        "description": "A measuring instrument the lab keeps calibrated, looked after by one custodian.",
        "fields": {
            "tag": {"type": "text", "required": True, "min_length": 1, "max_length": 40},
            "kind": {"type": "enum", "required": True, "values": ["balance", "thermometer", "pipette", "gauge"]},
            "site": {"type": "enum", "required": True, "values": ["leeds", "cork", "lyon"]},
            "state": {"type": "enum", "required": True, "values": ["in-service", "quarantined", "retired"], "default": "in-service"},
            "custodian": {"type": "reference", "target": "Person", "required": True, "write_rule": "actor.role == manager"},
            "interval_days": {"type": "integer", "required": True, "min": 1, "max": 3650},
        },
        "constraints": [
            "custodian.role is manager or technician",
            "state cannot change to in-service while a Deviation for this instrument has disposition open",
        ],
        "rules": {
            "read": "actor is authenticated",
            "create": "actor.role == manager",
            "update": "actor.role == manager or record.custodian == actor.id; only manager can change custodian",
            "delete": "actor.role == manager and no live record references this instrument",
        },
    },
    "Calibration": {
        "description": "One calibration of one instrument, with its result and measured drift.",
        "fields": {
            "instrument": {"type": "reference", "target": "Instrument", "required": True},
            "performed_on": {"type": "date", "required": True},
            "technician": {
                "type": "reference", "target": "Person", "required": True, "default": "$actor",
                "write_rule": "a technician records their own calibrations; only manager can change an existing technician",
            },
            "result": {"type": "enum", "required": True, "values": ["pass", "adjusted", "fail"]},
            "drift_ppm": {"type": "integer", "nullable": True, "default": None, "min": 0},
            "certificate": {"type": "text", "nullable": True, "default": None, "max_length": 80},
        },
        "constraints": ["result adjusted or fail requires drift_ppm", "technician.role is manager or technician"],
        "rules": {
            "read": "actor is authenticated",
            "create": "actor.role == manager or (actor.role == technician and record.technician == actor.id)",
            "update": "actor.role == manager or record.technician == actor.id; only manager can change technician",
            "delete": "actor.role == manager and no live record references this calibration",
        },
    },
    "Deviation": {
        "description": "A problem found with an instrument, open until someone decides its disposition.",
        "fields": {
            "instrument": {"type": "reference", "target": "Instrument", "required": True},
            "calibration": {"type": "reference", "target": "Calibration", "nullable": True, "default": None},
            "severity": {"type": "enum", "required": True, "values": ["minor", "major", "critical"]},
            "disposition": {"type": "enum", "required": True, "values": ["open", "use-as-is", "rework", "scrap"], "default": "open"},
            "summary": {"type": "text", "required": True, "min_length": 1, "max_length": 2000},
            "closed_on": {"type": "date", "nullable": True, "default": None},
            "raised_by": {
                "type": "reference", "target": "Person", "required": True, "default": "$actor",
                "write_rule": "a technician raises deviations in their own name; only manager can change it afterwards",
            },
        },
        "constraints": [
            "calibration is null or calibration.instrument == instrument",
            "disposition other than open requires closed_on",
            "only manager can change the disposition of a critical deviation",
        ],
        "rules": {
            "read": "actor is authenticated",
            "create": "actor.role == manager or (actor.role == technician and record.raised_by == actor.id)",
            "update": "actor.role == manager or record.raised_by == actor.id or record.instrument.custodian == actor.id; only manager can change raised_by",
            "delete": "actor.role == manager",
        },
    },
}


def _failed(message, fields, constraint):
    raise WorldError(422, "constraint_failed", message, {"fields": fields, "constraint": constraint})


def _require_staff(records, field, person_id):
    if records.get("Person", person_id)["data"]["role"] not in ("manager", "technician"):
        raise WorldError(422, "invalid_relationship", f"{field} must be a manager or technician.",
                         {"field": field, "allowed_roles": ["manager", "technician"]})


class Lab(Definition):
    name = "lab"
    title = "Calibration lab"
    version = "0.1.0"
    types = TYPES
    actors = ("ada", "kofi", "lena", "ines")
    seed_reason = "Initial lab records."

    def seed(self, today: date) -> list[tuple[str, str, dict[str, Any]]]:
        def day(offset: int) -> str:
            return (today + timedelta(days=offset)).isoformat()

        def instrument(tag, kind, site, state, custodian, interval):
            return ("Instrument", tag.lower(), {"tag": tag, "kind": kind, "site": site, "state": state,
                                                "custodian": custodian, "interval_days": interval})

        def calibration(record_id, instrument, offset, technician, result, drift, certificate):
            return ("Calibration", record_id, {"instrument": instrument, "performed_on": day(offset), "technician": technician,
                                               "result": result, "drift_ppm": drift, "certificate": certificate})

        def deviation(record_id, instrument, calibration, severity, summary, raised_by):
            return ("Deviation", record_id, {"instrument": instrument, "calibration": calibration, "severity": severity,
                                             "disposition": "open", "summary": summary, "closed_on": None, "raised_by": raised_by})

        return [
            ("Person", "ada", {"name": "Ada", "email": "ada@example.test", "role": "manager"}),
            ("Person", "kofi", {"name": "Kofi", "email": "kofi@example.test", "role": "technician"}),
            ("Person", "lena", {"name": "Lena", "email": "lena@example.test", "role": "technician"}),
            ("Person", "ines", {"name": "Ines", "email": "ines@example.test", "role": "auditor"}),
            instrument("BAL-014", "balance", "leeds", "in-service", "kofi", 180),
            instrument("BAL-020", "balance", "leeds", "quarantined", "kofi", 180),
            instrument("TH-221", "thermometer", "leeds", "in-service", "kofi", 365),
            instrument("TH-305", "thermometer", "cork", "in-service", "lena", 365),
            instrument("PIP-007", "pipette", "leeds", "in-service", "kofi", 90),
            instrument("PIP-031", "pipette", "leeds", "quarantined", "lena", 90),
            instrument("GAU-102", "gauge", "leeds", "quarantined", "kofi", 365),
            instrument("GAU-118", "gauge", "lyon", "retired", "lena", 365),
            instrument("GAU-140", "gauge", "cork", "quarantined", "lena", 365),
            calibration("bal-014-last", "bal-014", -175, "kofi", "pass", 12, "LDS-2231"),
            calibration("th-305-last", "th-305", -40, "lena", "adjusted", 310, "CRK-0984"),
            calibration("gau-102-last", "gau-102", -8, "kofi", "fail", 20400, None),
            calibration("pip-007-last", "pip-007", -30, "kofi", "pass", 90, "LDS-2290"),
            deviation("pip-007-ejector", "pip-007", None, "minor", "Tip ejector sticks on every tenth use.", "kofi"),
            deviation("gau-102-high", "gau-102", "gau-102-last", "major", "Reads 2% high above 6 bar.", "kofi"),
            deviation("bal-020-dropped", "bal-020", None, "critical", "Dropped in transit. Pan support is bent.", "lena"),
        ]

    def can_update(self, records, actor, record):
        role, type_name, data = actor["data"]["role"], record["type"], record["data"]
        if role == "manager":
            return True
        if role != "technician":
            return False
        if type_name == "Person":
            return record["id"] == actor["id"]
        if type_name == "Instrument":
            return data["custodian"] == actor["id"]
        if type_name == "Calibration":
            return data["technician"] == actor["id"]
        instrument = records.get("Instrument", data["instrument"])
        return actor["id"] in (data["raised_by"], instrument["data"]["custodian"] if instrument else None)

    def type_capabilities(self, records, actor):
        role = actor["data"]["role"]
        return {
            type_name: {
                "read": True,
                "create": role == "manager" or (role == "technician" and type_name in ("Calibration", "Deviation")),
                "update": role in ("manager", "technician"),
                "delete": role == "manager",
            }
            for type_name in self.types
        }

    def protected_fields(self, actor, type_name):
        if actor["data"]["role"] == "manager":
            return set()
        return {"Person": {"role"}, "Instrument": {"custodian"}, "Calibration": {"technician"},
                "Deviation": {"raised_by"}}.get(type_name, set())

    def authorize_create(self, records, actor, type_name, data):
        role = actor["data"]["role"]
        if role == "manager":
            return
        if role != "technician" or type_name not in ("Calibration", "Deviation"):
            deny()
        if type_name == "Calibration" and data["technician"] != actor["id"]:
            deny("A technician can only record a calibration they performed.")
        if type_name == "Deviation" and data["raised_by"] != actor["id"]:
            deny("A technician can only raise a deviation in their own name.")

    def authorize_delete(self, records, actor, record):
        if actor["data"]["role"] != "manager":
            deny("Only a manager can delete records.")

    def _last_manager(self, records):
        return sum(person["data"]["role"] == "manager" for person in records.all("Person")) == 1

    def check_delete(self, records, actor, record):
        if record["type"] == "Person" and record["data"]["role"] == "manager" and self._last_manager(records):
            raise WorldError(409, "last_manager", "The lab must retain at least one manager.")

    def check(self, records, actor, type_name, record_id, data, before):
        if type_name == "Person":
            if before and before["data"]["role"] == "manager" and data["role"] != "manager" and self._last_manager(records):
                raise WorldError(409, "last_manager", "The lab must retain at least one manager.")
        elif type_name == "Instrument":
            _require_staff(records, "custodian", data["custodian"])
            if data["state"] == "in-service" and before and before["data"]["state"] != "in-service":
                blocking = [other["id"] for other in records.all("Deviation")
                            if other["data"]["instrument"] == record_id and other["data"]["disposition"] == "open"]
                if blocking:
                    raise WorldError(
                        422, "constraint_failed", "This instrument has an open deviation and cannot return to service.",
                        {"fields": ["state"], "open_deviations": blocking,
                         "constraint": "state cannot change to in-service while a Deviation for this instrument has disposition open"},
                    )
        elif type_name == "Calibration":
            _require_staff(records, "technician", data["technician"])
            if data["result"] in ("adjusted", "fail") and data["drift_ppm"] is None:
                _failed("An adjusted or failed calibration needs its drift_ppm.", ["drift_ppm"], "result adjusted or fail requires drift_ppm")
        elif type_name == "Deviation":
            calibration = records.get("Calibration", data["calibration"]) if data["calibration"] else None
            if calibration and calibration["data"]["instrument"] != data["instrument"]:
                raise WorldError(422, "invalid_relationship", "calibration must be for the same instrument as this deviation.",
                                 {"field": "calibration", "instrument": data["instrument"]})
            if data["disposition"] != "open" and data["closed_on"] is None:
                _failed("A deviation with a disposition needs its closed_on date.", ["closed_on"], "disposition other than open requires closed_on")
            changed = before is None or before["data"]["disposition"] != data["disposition"]
            if data["severity"] == "critical" and changed and data["disposition"] != "open" and actor and actor["data"]["role"] != "manager":
                deny("Only a manager can change the disposition of a critical deviation.")


DEFINITION = Lab()
