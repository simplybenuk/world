"""A small sales team's CRM: organizations, contacts, opportunities, and interactions."""

from datetime import date, timedelta
from typing import Any

from world.definition import Definition, deny
from world.schema import WorldError


CLOSED = ("won", "lost")

TYPES: dict[str, dict[str, Any]] = {
    "Person": {
        "description": "Someone who uses the CRM. Their stored role governs their access.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "email": {"type": "text", "nullable": True, "default": None, "max_length": 254},
            "role": {
                "type": "enum", "required": True, "values": ["admin", "rep", "viewer"], "default": "rep",
                "write_rule": "actor.role == admin",
            },
        },
        "rules": {
            "read": "actor is authenticated",
            "create": "actor.role == admin",
            "update": "actor.role == admin or (actor.role == rep and actor.id == record.id); only admin can change role",
            "delete": "actor.role == admin and no live record references this person and another admin remains",
        },
    },
    "Organization": {
        "description": "A company the team sells to, with one person who owns the account.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 200},
            "website": {"type": "text", "nullable": True, "default": None, "max_length": 300},
            "industry": {"type": "text", "nullable": True, "default": None, "max_length": 120},
            "status": {
                "type": "enum", "required": True, "values": ["prospect", "customer", "former"],
                "default": "prospect",
            },
            "owner": {
                "type": "reference", "target": "Person", "required": True, "default": "$actor",
                "write_rule": "a rep creates organizations they own; only admin can change an existing owner",
            },
        },
        "constraints": ["owner.role is admin or rep"],
        "rules": {
            "read": "actor is authenticated",
            "create": "actor.role == admin or (actor.role == rep and record.owner == actor.id)",
            "update": "actor.role == admin or record.owner == actor.id; only admin can change owner",
            "delete": "actor.role == admin and no live record references this organization",
        },
    },
    "Contact": {
        "description": "A person who works at one organization.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "email": {"type": "text", "nullable": True, "default": None, "max_length": 254},
            "phone": {"type": "text", "nullable": True, "default": None, "max_length": 40},
            "job_title": {"type": "text", "nullable": True, "default": None, "max_length": 160},
            "organization": {"type": "reference", "target": "Organization", "required": True},
        },
        "constraints": [
            "a contact cannot move to another organization while an opportunity or interaction there names them",
        ],
        "rules": {
            "read": "actor is authenticated",
            "create": "actor.role == admin or organization.owner == actor.id",
            "update": "actor.role == admin or actor.id owns both the current and proposed organization",
            "delete": "actor.role == admin and no live record references this contact",
        },
    },
    "Opportunity": {
        "description": "A possible sale to one organization, moving through stages until it is won or lost.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 200},
            "organization": {"type": "reference", "target": "Organization", "required": True},
            "primary_contact": {"type": "reference", "target": "Contact", "nullable": True, "default": None},
            "owner": {
                "type": "reference", "target": "Person", "required": True, "default": "$actor",
                "write_rule": "a rep creates opportunities they own; only admin can change an existing owner",
            },
            "stage": {
                "type": "enum", "required": True,
                "values": ["lead", "qualified", "proposal", "negotiation", "won", "lost"], "default": "lead",
            },
            "amount": {"type": "integer", "nullable": True, "default": None, "min": 0, "unit": "whole US dollars"},
            "close_date": {"type": "date", "nullable": True, "default": None},
            "lost_reason": {"type": "text", "nullable": True, "default": None, "max_length": 2000},
        },
        "constraints": [
            "primary_contact is null or primary_contact.organization == organization",
            "owner.role is admin or rep",
            "stage == won requires amount > 0 and close_date",
            "stage == lost requires lost_reason",
            "once stage is won or lost, only admin can change the record",
        ],
        "rules": {
            "read": "actor.role is admin or viewer, or record.owner == actor.id, or record.organization.owner == actor.id",
            "create": "actor.role == admin or (actor.role == rep and record.owner == actor.id and organization.owner == actor.id)",
            "update": "actor.role == admin or (record.owner == actor.id and the saved stage is not won or lost); only admin can change owner",
            "delete": "actor.role == admin and no live record references this opportunity",
        },
    },
    "Interaction": {
        "description": "A call, email, meeting, or note about an organization, optionally tied to a contact or opportunity.",
        "fields": {
            "kind": {"type": "enum", "required": True, "values": ["call", "email", "meeting", "note"]},
            "summary": {"type": "text", "required": True, "min_length": 1, "max_length": 20000},
            "occurred_on": {"type": "date", "required": True},
            "organization": {"type": "reference", "target": "Organization", "required": True},
            "contact": {"type": "reference", "target": "Contact", "nullable": True, "default": None},
            "opportunity": {"type": "reference", "target": "Opportunity", "nullable": True, "default": None},
            "author": {
                "type": "reference", "target": "Person", "required": True, "default": "$actor",
                "write_rule": "a rep creates interactions they authored; only admin can change an existing author",
            },
        },
        "constraints": [
            "contact is null or contact.organization == organization",
            "opportunity is null or opportunity.organization == organization",
            "author.role is admin or rep",
        ],
        "rules": {
            "read": "opportunity is null or actor can read opportunity",
            "create": "actor.role == admin or (actor.role == rep and record.author == actor.id and (organization.owner == actor.id or opportunity.owner == actor.id))",
            "update": "actor.role == admin or record.author == actor.id; only admin can change author",
            "delete": "actor.role == admin",
        },
    },
}


def _admin_count(records):
    return sum(person["data"]["role"] == "admin" for person in records.all("Person"))


def _owns(records, actor, type_name, record_id):
    record = records.get(type_name, record_id) if record_id else None
    return bool(record and record["data"]["owner"] == actor["id"])


def _broken(message, **details):
    raise WorldError(422, "invalid_relationship", message, details)


def _require_seller(records, field, person_id):
    if records.get("Person", person_id)["data"]["role"] not in ("admin", "rep"):
        _broken(f"{field} must be a person whose role is admin or rep.", field=field, allowed_roles=["admin", "rep"])


def _same_organization(records, field, target, target_id, organization):
    if target_id is not None and records.get(target, target_id)["data"]["organization"] != organization:
        _broken(f"{field} must belong to the same organization as this record.", field=field, organization=organization)


class CRM(Definition):
    name = "crm"
    title = "CRM"
    version = "0.1.0"
    types = TYPES
    actors = ("ben", "priya", "tom", "nina")
    seed_reason = "Initial sales team."

    def seed(self, today: date) -> list[tuple[str, str, dict[str, Any]]]:
        def day(offset: int) -> str:
            return (today + timedelta(days=offset)).isoformat()

        def organization(name, status, owner, industry, website):
            return {"name": name, "status": status, "owner": owner, "industry": industry, "website": website}

        def contact(name, job_title, organization, email, phone=None):
            return {"name": name, "job_title": job_title, "organization": organization, "email": email, "phone": phone}

        def opportunity(name, organization, primary_contact, owner, stage, amount, close, lost_reason=None):
            return {"name": name, "organization": organization, "primary_contact": primary_contact, "owner": owner,
                    "stage": stage, "amount": amount, "close_date": close, "lost_reason": lost_reason}

        def interaction(kind, summary, occurred, organization, contact, opportunity, author):
            return {"kind": kind, "summary": summary, "occurred_on": occurred, "organization": organization,
                    "contact": contact, "opportunity": opportunity, "author": author}

        return [
            ("Person", "ben", {"name": "Ben", "email": "ben@example.test", "role": "admin"}),
            ("Person", "priya", {"name": "Priya", "email": "priya@example.test", "role": "rep"}),
            ("Person", "tom", {"name": "Tom", "email": "tom@example.test", "role": "rep"}),
            ("Person", "nina", {"name": "Nina", "email": "nina@example.test", "role": "viewer"}),
            ("Organization", "northwind", organization("Northwind Traders", "customer", "priya", "Wholesale", "https://northwind.example")),
            ("Organization", "harbor", organization("Harbor Logistics", "prospect", "priya", "Freight", "https://harbor.example")),
            ("Organization", "alder", organization("Alder Clinics", "prospect", "tom", "Healthcare", "https://alder.example")),
            ("Organization", "pine", organization("Pine Studio", "former", "tom", "Design", None)),
            ("Contact", "dana-northwind", contact("Dana Okafor", "Head of Operations", "northwind", "dana@northwind.example", "+1 555 0101")),
            ("Contact", "luis-northwind", contact("Luis Ferreira", "Procurement Lead", "northwind", "luis@northwind.example")),
            ("Contact", "mei-harbor", contact("Mei Tanaka", "Chief Financial Officer", "harbor", "mei@harbor.example", "+1 555 0144")),
            ("Contact", "omar-alder", contact("Omar Haddad", "IT Director", "alder", "omar@alder.example")),
            ("Contact", "ruth-alder", contact("Ruth Lindqvist", "Practice Manager", "alder", "ruth@alder.example", "+1 555 0170")),
            ("Contact", "ivo-pine", contact("Ivo Petrov", "Founder", "pine", "ivo@pine.example")),
            ("Opportunity", "northwind-renewal", opportunity("Northwind annual renewal", "northwind", "dana-northwind", "priya", "negotiation", 48000, day(6))),
            ("Opportunity", "northwind-warehouse", opportunity("Northwind warehouse add-on", "northwind", "luis-northwind", "priya", "qualified", 15000, day(40))),
            ("Opportunity", "harbor-pilot", opportunity("Harbor Logistics pilot", "harbor", "mei-harbor", "priya", "proposal", 22000, day(-3))),
            ("Opportunity", "alder-rollout", opportunity("Alder Clinics rollout", "alder", "omar-alder", "tom", "lead", None, None)),
            ("Opportunity", "alder-training", opportunity("Alder Clinics staff training", "alder", "ruth-alder", "tom", "won", 9000, day(-20))),
            ("Opportunity", "pine-redesign", opportunity("Pine Studio redesign", "pine", "ivo-pine", "tom", "lost", 12000, day(-60), "Chose a cheaper agency.")),
            ("Interaction", "northwind-renewal-call", interaction("call", "Dana agreed the renewal price. Waiting on a signed order form.", day(-2), "northwind", "dana-northwind", "northwind-renewal", "priya")),
            ("Interaction", "northwind-warehouse-email", interaction("email", "Sent Luis the warehouse add-on overview.", day(-9), "northwind", "luis-northwind", "northwind-warehouse", "priya")),
            ("Interaction", "harbor-pilot-meeting", interaction("meeting", "Presented the pilot proposal to Mei. She asked for references.", day(-12), "harbor", "mei-harbor", "harbor-pilot", "priya")),
            ("Interaction", "alder-intro", interaction("call", "Intro call with Omar about a clinic-wide rollout.", day(-5), "alder", "omar-alder", "alder-rollout", "tom")),
            ("Interaction", "alder-training-note", interaction("note", "Training delivered and invoiced.", day(-20), "alder", "ruth-alder", "alder-training", "tom")),
            ("Interaction", "pine-check-in", interaction("note", "Ivo may revisit the redesign next year.", day(-30), "pine", "ivo-pine", None, "tom")),
        ]

    def can_read(self, records, actor, record):
        if actor["data"]["role"] in ("admin", "viewer"):
            return True
        if record["type"] == "Interaction":
            opportunity_id = record["data"]["opportunity"]
            record = records.get("Opportunity", opportunity_id, include_deleted=True) if opportunity_id else None
            if record is None:
                return True
        if record["type"] != "Opportunity":
            return True
        organization = records.get("Organization", record["data"]["organization"], include_deleted=True)
        return actor["id"] in (record["data"]["owner"], organization["data"]["owner"])

    def can_update(self, records, actor, record):
        role, type_name, data = actor["data"]["role"], record["type"], record["data"]
        if role == "admin":
            return True
        if role != "rep":
            return False
        if type_name == "Person":
            return record["id"] == actor["id"]
        if type_name == "Contact":
            return _owns(records, actor, "Organization", data["organization"])
        if type_name == "Interaction":
            return data["author"] == actor["id"]
        if type_name == "Opportunity":
            # Judged on the saved stage, so that an owner can still close an open opportunity.
            saved = records.get("Opportunity", record["id"])
            if saved and saved["data"]["stage"] in CLOSED:
                return False
        return data["owner"] == actor["id"]

    def type_capabilities(self, records, actor):
        role = actor["data"]["role"]
        return {
            type_name: {
                "read": True,
                "create": role == "admin" or (role == "rep" and type_name != "Person"),
                "update": role in ("admin", "rep"),
                "delete": role == "admin",
            }
            for type_name in self.types
        }

    def protected_fields(self, actor, type_name):
        if actor["data"]["role"] == "admin":
            return set()
        return {"Person": {"role"}, "Organization": {"owner"}, "Opportunity": {"owner"},
                "Interaction": {"author"}}.get(type_name, set())

    def authorize_create(self, records, actor, type_name, data):
        role = actor["data"]["role"]
        if role == "admin":
            return
        if role != "rep" or type_name == "Person":
            deny()
        owns_organization = type_name != "Organization" and _owns(records, actor, "Organization", data["organization"])
        if type_name == "Organization" and data["owner"] != actor["id"]:
            deny("A rep can only create an organization they own.")
        if type_name == "Contact" and not owns_organization:
            deny("Only the organization's owner can add a contact to it.")
        if type_name == "Opportunity" and not (data["owner"] == actor["id"] and owns_organization):
            deny("A rep can only open an opportunity they own, for an organization they own.")
        if type_name == "Interaction":
            if data["author"] != actor["id"]:
                deny("A rep can only record an interaction attributed to themselves.")
            if not (owns_organization or _owns(records, actor, "Opportunity", data["opportunity"])):
                deny("A rep can only record interactions for an organization or opportunity they own.")

    def authorize_delete(self, records, actor, record):
        if actor["data"]["role"] != "admin":
            deny("Only an admin can delete records.")

    def check_delete(self, records, actor, record):
        if record["type"] == "Person" and record["data"]["role"] == "admin" and _admin_count(records) == 1:
            raise WorldError(409, "last_admin", "The world must retain at least one admin.")

    def check(self, records, actor, type_name, record_id, data, before):
        if type_name == "Person":
            if before and before["data"]["role"] == "admin" and data["role"] != "admin" and _admin_count(records) == 1:
                raise WorldError(409, "last_admin", "The world must retain at least one admin.")
        elif type_name == "Organization":
            _require_seller(records, "owner", data["owner"])
        elif type_name == "Contact":
            for other in records.all("Opportunity"):
                if other["data"]["primary_contact"] == record_id and other["data"]["organization"] != data["organization"]:
                    _broken("This contact is the primary contact of an opportunity at their current organization.",
                            field="organization", type="Opportunity", id=other["id"])
            for other in records.all("Interaction"):
                if other["data"]["contact"] == record_id and other["data"]["organization"] != data["organization"]:
                    _broken("An interaction at the current organization names this contact.",
                            field="organization", type="Interaction", id=other["id"])
        elif type_name == "Opportunity":
            _require_seller(records, "owner", data["owner"])
            _same_organization(records, "primary_contact", "Contact", data["primary_contact"], data["organization"])
            for other in records.all("Interaction"):
                if other["data"]["opportunity"] == record_id and other["data"]["organization"] != data["organization"]:
                    _broken("An interaction at the current organization is tied to this opportunity.",
                            field="organization", type="Interaction", id=other["id"])
            if data["stage"] == "won":
                missing = [field for field in ("amount", "close_date") if not data[field]]
                if missing:
                    raise WorldError(
                        422, "constraint_failed", "A won opportunity needs an amount above zero and a close date.",
                        {"fields": missing, "constraint": "stage == won requires amount > 0 and close_date"},
                    )
            if data["stage"] == "lost" and not (data["lost_reason"] or "").strip():
                raise WorldError(
                    422, "constraint_failed", "A lost opportunity needs a lost_reason.",
                    {"fields": ["lost_reason"], "constraint": "stage == lost requires lost_reason"},
                )
        elif type_name == "Interaction":
            _require_seller(records, "author", data["author"])
            _same_organization(records, "contact", "Contact", data["contact"], data["organization"])
            _same_organization(records, "opportunity", "Opportunity", data["opportunity"], data["organization"])


DEFINITION = CRM()
