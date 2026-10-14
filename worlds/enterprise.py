"""A company-sized world for scale tests: the calibration lab plus about 200 generated types.

The lab's types, rules, and seed are used unchanged, so the lab's tasks can be
run here against the same records among a million others. The other types are
generated from the lists below. They are plausible, not realistic.
"""

from datetime import date
from typing import Any

from world.definition import Definition
from worlds import lab

MANAGER = "actor.role == 'manager'"
STAFF = "actor.role == 'technician'"

# Department -> the kinds of record it keeps. Quality also holds the lab's own three types.
DEPARTMENTS = {
    "Sales": "Lead Account Quote SalesOrder Territory PriceList Discount Forecast Commission Renewal",
    "Procurement": "Supplier Requisition PurchaseOrder GoodsReceipt SupplierInvoice Tender Contract Catalogue Approval Rebate",
    "Inventory": "Item Warehouse Bin StockCount Transfer Lot Reservation Adjustment Replenishment Scrap",
    "Manufacturing": "WorkOrder BillOfMaterials Routing WorkCentre Operation Batch Shift Downtime Yield Changeover",
    "Quality": "Inspection NonConformance CorrectiveAction AuditFinding Specification Sample Certificate",
    "Maintenance": "Asset MaintenancePlan WorkRequest SparePart Shutdown Lubrication MeterReading Permit Failure Overhaul",
    "People": "Employee Position Absence Appraisal Vacancy Applicant Onboarding Grievance Skill Timesheet",
    "Payroll": "PayRun Payslip Deduction Allowance TaxCode PensionScheme Bonus Overtime Expense Garnishment",
    "Finance": "LedgerAccount JournalEntry Budget CostCentre Invoice Payment CreditNote BankStatement Accrual FixedAsset",
    "Projects": "Project Milestone WorkPackage Risk Issue ChangeRequest Deliverable Stakeholder Estimate LessonLearned",
    "Support": "Ticket Customer ServiceLevel KnowledgeArticle Escalation Callback Warranty Return Survey Outage",
    "Logistics": "Shipment Carrier Route Delivery Pallet CustomsEntry FreightInvoice Dock Manifest Consignment",
    "Facilities": "Building Room Lease UtilityReading CleaningRound AccessCard Visitor ParkingBay Incident Evacuation",
    "Legal": "Matter Agreement Clause Dispute Patent Trademark Licence Notice PowerOfAttorney Filing",
    "Technology": "System Server Deployment ChangeTicket Vulnerability Backup UserAccount SoftwareLicence TlsCertificate Runbook",
    "Marketing": "Campaign Segment CreativeAsset Event Sponsorship PressRelease Channel Offer Brief Testimonial",
    "Research": "Experiment Hypothesis Prototype Formulation Trial Protocol Reagent Notebook Publication Grant",
    "Compliance": "Obligation Control Policy Attestation Exemption Regulator Breach Assessment Register Training",
    "Fleet": "Vehicle Driver Trip FuelCard VehicleService Tyre Fine Insurance Telematics Disposal",
    "Safety": "Hazard NearMiss SafetyWalk PermitToWork Injury Drill Equipment Datasheet Toolbox Isolation",
}
STATES = ["draft", "active", "on-hold", "closed"]
SITES = ["leeds", "cork", "lyon"]


def generated(name: str, department: str, previous: str | None) -> dict[str, Any]:
    """One generated type: a coded record with a state, an owner, a site, and a link to its neighbour."""
    fields: dict[str, Any] = {
        "code": {"type": "text", "required": True, "min_length": 1, "max_length": 40},
        "title": {"type": "text", "required": True, "min_length": 1, "max_length": 200},
        "state": {"type": "enum", "required": True, "values": STATES, "default": "draft"},
        "site": {"type": "enum", "required": True, "values": SITES},
        "owner": {"type": "reference", "target": "Person", "required": True, "default": "$actor", "write_rule": MANAGER},
        "amount": {"type": "integer", "nullable": True, "default": None, "min": 0},
        "due": {"type": "date", "nullable": True, "default": None},
        "closed_on": {"type": "date", "nullable": True, "default": None},
    }
    if previous:
        fields["related"] = {"type": "reference", "target": previous, "nullable": True, "default": None}
    return {
        "description": f"{department}: one {name} record, owned by a person at a site.",
        "fields": fields,
        "rules": {
            "read": "actor.id is not None",
            "create": f"{MANAGER} or ({STAFF} and record.owner == actor.id)",
            "update": f"{MANAGER} or ({STAFF} and record.owner == actor.id)",
            "delete": MANAGER,
        },
        "constraints": [{"check": "record.state != 'closed' or record.closed_on is not None",
                         "message": "A closed record needs its closed_on date.", "fields": ["closed_on"]}],
    }


def build_types() -> dict[str, dict[str, Any]]:
    types = dict(lab.TYPES)
    for department, names in DEPARTMENTS.items():
        previous = None
        for name in names.split():
            types[name] = generated(name, department, previous)
            previous = name
    return types


TYPES = build_types()
GENERATED = [name for name in TYPES if name not in lab.TYPES]


class Enterprise(Definition):
    name = "enterprise"
    title = "Company records"
    version = "0.1.0"
    types = TYPES
    actors = lab.Lab.actors
    seed_reason = "Initial company records."

    def seed(self, today: date) -> list[tuple[str, str, dict[str, Any]]]:
        return lab.DEFINITION.seed(today)


DEFINITION = Enterprise()
