"""A calibration lab: instruments, their calibrations, and the deviations raised against them."""

from datetime import date, timedelta
from typing import Any

from world.definition import Definition

MANAGER = "actor.role == 'manager'"
TECHNICIAN = "actor.role == 'technician'"
RELATIONSHIP = {"code": "invalid_relationship"}
LAST_MANAGER = {"message": "The lab must retain at least one manager.", "fields": ["role"], "code": "last_manager", "status": 409}


def staff(field):
    return {"check": f"record.{field}.role in ('manager', 'technician')", "fields": [field], **RELATIONSHIP,
            "message": f"{field} must be a manager or technician."}


TYPES: dict[str, dict[str, Any]] = {
    "Person": {
        "description": "Someone who works in the lab. Their stored role governs their access.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "email": {"type": "text", "nullable": True, "default": None, "max_length": 254},
            "role": {"type": "enum", "required": True, "values": ["manager", "technician", "auditor"], "default": "technician", "write_rule": MANAGER},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": MANAGER,
            "update": f"{MANAGER} or ({TECHNICIAN} and actor.id == record.id)",
            "delete": MANAGER,
        },
        "constraints": [
            {"check": "count(p for p in Person if p.role == 'manager') >= 1", "on": ("update",), **LAST_MANAGER},
            {"check": "record.role != 'manager' or count(p for p in Person if p.role == 'manager') > 1", "on": ("delete",), **LAST_MANAGER},
        ],
    },
    "Instrument": {
        "description": "A measuring instrument the lab keeps calibrated, looked after by one custodian.",
        "fields": {
            "tag": {"type": "text", "required": True, "min_length": 1, "max_length": 40},
            "kind": {"type": "enum", "required": True, "values": ["balance", "thermometer", "pipette", "gauge"]},
            "site": {"type": "enum", "required": True, "values": ["leeds", "cork", "lyon"]},
            "state": {"type": "enum", "required": True, "values": ["in-service", "quarantined", "retired"], "default": "in-service"},
            "custodian": {"type": "reference", "target": "Person", "required": True, "write_rule": MANAGER},
            "interval_days": {"type": "integer", "required": True, "min": 1, "max": 3650},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": MANAGER,
            "update": f"{MANAGER} or ({TECHNICIAN} and record.custodian == actor.id)",
            "delete": MANAGER,
        },
        "constraints": [
            staff("custodian"),
            {"check": "not (saved is not None and changed('state') and record.state == 'in-service')"
                      " or not any(d.disposition == 'open' for d in Deviation if d.instrument == record.id)",
             "message": "This instrument has an open deviation and cannot return to service.", "fields": ["state"]},
        ],
    },
    "Calibration": {
        "description": "One calibration of one instrument, with its result and measured drift.",
        "fields": {
            "instrument": {"type": "reference", "target": "Instrument", "required": True},
            "performed_on": {"type": "date", "required": True},
            "technician": {"type": "reference", "target": "Person", "required": True, "default": "$actor", "write_rule": MANAGER},
            "result": {"type": "enum", "required": True, "values": ["pass", "adjusted", "fail"]},
            "drift_ppm": {"type": "integer", "nullable": True, "default": None, "min": 0},
            "certificate": {"type": "text", "nullable": True, "default": None, "max_length": 80},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": f"{MANAGER} or ({TECHNICIAN} and record.technician == actor.id)",
            "update": f"{MANAGER} or ({TECHNICIAN} and record.technician == actor.id)",
            "delete": MANAGER,
        },
        "constraints": [
            staff("technician"),
            {"check": "record.result == 'pass' or record.drift_ppm is not None",
             "message": "An adjusted or failed calibration needs its drift_ppm.", "fields": ["drift_ppm"]},
        ],
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
            "raised_by": {"type": "reference", "target": "Person", "required": True, "default": "$actor", "write_rule": MANAGER},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": f"{MANAGER} or ({TECHNICIAN} and record.raised_by == actor.id)",
            "update": f"{MANAGER} or ({TECHNICIAN} and (record.raised_by == actor.id or record.instrument.custodian == actor.id))",
            "delete": MANAGER,
        },
        "constraints": [
            {"check": "record.calibration is None or record.calibration.instrument == record.instrument", "fields": ["calibration"],
             "message": "calibration must be for the same instrument as this deviation.", **RELATIONSHIP},
            {"check": "record.disposition == 'open' or record.closed_on is not None",
             "message": "A deviation with a disposition needs its closed_on date.", "fields": ["closed_on"]},
            {"check": f"not (record.severity == 'critical' and changed('disposition') and record.disposition != 'open') or {MANAGER}",
             "message": "Only a manager can change the disposition of a critical deviation.", "fields": ["disposition"],
             "code": "permission_denied", "status": 403},
        ],
    },
}


class Lab(Definition):
    name = "lab"
    title = "Calibration lab"
    version = "0.2.0"
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


DEFINITION = Lab()
