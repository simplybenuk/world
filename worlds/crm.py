"""A small sales team's CRM: organizations, contacts, opportunities, and interactions."""

from datetime import date, timedelta
from typing import Any

from world.definition import Definition

ADMIN = "actor.role == 'admin'"
REP = "actor.role == 'rep'"
OWNS_ORGANIZATION = "record.organization.owner == actor.id"
RELATIONSHIP = {"code": "invalid_relationship"}
LAST_ADMIN = {"message": "The world must retain at least one admin.", "fields": ["role"], "code": "last_admin", "status": 409}


def seller(field):
    return {"check": f"record.{field}.role in ('admin', 'rep')", "fields": [field], **RELATIONSHIP,
            "message": f"{field} must be a person whose role is admin or rep."}


def same_organization(field):
    return {"check": f"record.{field} is None or record.{field}.organization == record.organization", "fields": [field],
            **RELATIONSHIP, "message": f"{field} must belong to the same organization as this record."}


TYPES: dict[str, dict[str, Any]] = {
    "Person": {
        "description": "Someone who uses the CRM. Their stored role governs their access.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 160},
            "email": {"type": "text", "nullable": True, "default": None, "max_length": 254},
            "role": {"type": "enum", "required": True, "values": ["admin", "rep", "viewer"], "default": "rep", "write_rule": ADMIN},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": ADMIN,
            "update": f"{ADMIN} or ({REP} and actor.id == record.id)",
            "delete": ADMIN,
        },
        "constraints": [
            {"check": "count(p for p in Person if p.role == 'admin') >= 1", "on": ("update",), **LAST_ADMIN},
            {"check": "record.role != 'admin' or count(p for p in Person if p.role == 'admin') > 1", "on": ("delete",), **LAST_ADMIN},
        ],
    },
    "Organization": {
        "description": "A company the team sells to, with one person who owns the account.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 200},
            "website": {"type": "text", "nullable": True, "default": None, "max_length": 300},
            "industry": {"type": "text", "nullable": True, "default": None, "max_length": 120},
            "status": {"type": "enum", "required": True, "values": ["prospect", "customer", "former"], "default": "prospect"},
            "owner": {"type": "reference", "target": "Person", "required": True, "default": "$actor", "write_rule": ADMIN},
        },
        "rules": {
            "read": "actor.id is not None",
            "create": f"{ADMIN} or ({REP} and record.owner == actor.id)",
            "update": f"{ADMIN} or ({REP} and record.owner == actor.id)",
            "delete": ADMIN,
        },
        "constraints": [seller("owner")],
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
        "rules": {
            "read": "actor.id is not None",
            "create": f"{ADMIN} or ({REP} and {OWNS_ORGANIZATION})",
            "update": f"{ADMIN} or ({REP} and {OWNS_ORGANIZATION})",
            "delete": ADMIN,
        },
    },
    "Opportunity": {
        "description": "A possible sale to one organization, moving through stages until it is won or lost.",
        "fields": {
            "name": {"type": "text", "required": True, "min_length": 1, "max_length": 200},
            "organization": {"type": "reference", "target": "Organization", "required": True},
            "primary_contact": {"type": "reference", "target": "Contact", "nullable": True, "default": None},
            "owner": {"type": "reference", "target": "Person", "required": True, "default": "$actor", "write_rule": ADMIN},
            "stage": {
                "type": "enum", "required": True,
                "values": ["lead", "qualified", "proposal", "negotiation", "won", "lost"], "default": "lead",
            },
            "amount": {"type": "integer", "nullable": True, "default": None, "min": 0, "unit": "whole US dollars"},
            "close_date": {"type": "date", "nullable": True, "default": None},
            "lost_reason": {"type": "text", "nullable": True, "default": None, "max_length": 2000},
        },
        "rules": {
            "read": f"actor.role in ('admin', 'viewer') or record.owner == actor.id or {OWNS_ORGANIZATION}",
            "create": f"{ADMIN} or ({REP} and record.owner == actor.id and {OWNS_ORGANIZATION})",
            "update": f"{ADMIN} or ({REP} and record.owner == actor.id and saved.stage not in ('won', 'lost'))",
            "delete": ADMIN,
        },
        "constraints": [
            seller("owner"),
            same_organization("primary_contact"),
            {"check": "record.stage != 'won' or (record.amount > 0 and record.close_date is not None)",
             "message": "A won opportunity needs an amount above zero and a close date.", "fields": ["amount", "close_date"]},
            {"check": "record.stage != 'lost' or record.lost_reason",
             "message": "A lost opportunity needs a lost_reason.", "fields": ["lost_reason"]},
        ],
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
            "author": {"type": "reference", "target": "Person", "required": True, "default": "$actor", "write_rule": ADMIN},
        },
        "rules": {
            "read": "record.opportunity is None or can_read(record.opportunity)",
            "create": f"{ADMIN} or ({REP} and record.author == actor.id and ({OWNS_ORGANIZATION} or record.opportunity.owner == actor.id))",
            "update": f"{ADMIN} or ({REP} and record.author == actor.id)",
            "delete": ADMIN,
        },
        "constraints": [seller("author"), same_organization("contact"), same_organization("opportunity")],
    },
}


class CRM(Definition):
    name = "crm"
    title = "CRM"
    version = "0.2.0"
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


DEFINITION = CRM()
