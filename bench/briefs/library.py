"""A tool lending library."""

from . import Task, change

BRIEF = """
Elm Street Tool Library lends tools to its members. It needs to keep track of its members and staff, its tools, loans,
and reservations.

People, by ID:
- grace: Grace Okoye, who works at the library.
- dev: Dev Mehta, a member until 2027-03-31. Dev has done the safety induction.
- hana: Hana Sato, a member whose membership ran out on 2026-09-30.
- ian: Ian Ross, a member until 2027-01-31. Ian has not done the safety induction.

Tools, by ID:
- drill-01: a cordless drill.
- ladder-01: an extension ladder.
- saw-01: a circular saw. It is a power saw.
- sander-01: an orbital sander.

Loans so far, by ID. A loan records the tool, the member, the day it went out, the day it is due back, and the day it
came back.
- loan-ian-drill: the cordless drill to Ian, out 2026-10-10, due back 2026-10-24, not yet returned.
- loan-dev-sander: the orbital sander to Dev, out 2026-09-01, due back 2026-09-14, returned 2026-09-12.

How the library works:
- Staff see everything. Members see the tools, and their own loans and reservations, but not other members' loans.
- Staff record loans and returns. Members cannot lend tools to themselves.
- A loan is for at most 14 days.
- A tool can be out on only one loan at a time.
- Nobody may borrow once their membership has run out.
- Power saws are lent only to members who have done the safety induction.
- Members reserve tools for themselves. Staff can reserve for anyone.
- Staff record when a member has done the safety induction.
"""

PEOPLE = ("grace", "dev", "hana", "ian")
RECORDS = (("tool", "drill-01"), ("tool", "ladder-01"), ("tool", "saw-01"), ("tool", "sander-01"),
           ("loan", "loan-ian-drill"), ("loan", "loan-dev-sander"))

TASKS = [
    Task("who-has-drill", "grace", "Who has the cordless drill at the moment, and when is it due back?",
         answer=[("Ian",), ("2026-10-24", "24 October", "October 24", "24 Oct", "Oct 24", "24/10")]),
    Task("available", "dev", "Which tools could I borrow right now?", answer=[("ladder",), ("sander",), ("saw",)]),
    Task("lend", "grace", "Lend the extension ladder to Dev from 2026-10-17, due back 2026-10-24.",
         changes=[change("ladder-01", "dev", "2026-10-17", "2026-10-24")]),
    Task("return", "grace", "Ian brought the cordless drill back today, 2026-10-17. Record the return.",
         changes=[change("drill-01", "ian", "2026-10-17")]),
    Task("reserve", "dev", "Reserve the circular saw for me.", changes=[change("saw-01", "dev", actions=("create",))]),
    Task("induction", "grace", "Ian completed the safety induction today, 2026-10-17. Record it.",
         changes=[change("ian", (True, "2026-10-17"))]),
    Task("refuse-induction", "grace", "Lend the circular saw to Ian from 2026-10-17, due back 2026-10-24.",
         forbid=[change("saw-01", "ian")]),
    Task("refuse-expired", "grace", "Lend the orbital sander to Hana from 2026-10-17, due back 2026-10-24.",
         forbid=[change("sander-01", "hana")]),
    Task("refuse-on-loan", "grace", "Lend the cordless drill to Dev from 2026-10-17, due back 2026-10-24.",
         forbid=[change("drill-01", "dev", "2026-10-17")]),
    Task("refuse-too-long", "grace", "Lend the orbital sander to Dev from 2026-10-17, due back 2026-11-14.",
         forbid=[change("sander-01", "dev", "2026-11-14")]),
]


def _lend(served, tool, member, out, due):
    return served.client("grace").call("POST", "/api/records/Loan", {"data": {"tool": tool, "member": member, "out": out, "due": due}})


def _refused(response):
    return "World refused it, so it was not lent." if response[0] >= 400 else "Done."


SOLUTIONS = {
    "who-has-drill": lambda served, task: "Ian Ross has it, due back 2026-10-24.",
    "available": lambda served, task: "The ladder, the sander, and the saw.",
    "lend": lambda served, task: _lend(served, "ladder-01", "dev", "2026-10-17", "2026-10-24") and "Done.",
    "return": lambda served, task: served.client("grace").call("PATCH", "/api/records/Loan/loan-ian-drill", {
        "changes": {"returned": "2026-10-17"}, "expected_version": 1}) and "Done.",
    "reserve": lambda served, task: served.client("dev").call("POST", "/api/records/Reservation", {"data": {"tool": "saw-01"}}) and "Done.",
    "induction": lambda served, task: served.client("grace").call("PATCH", "/api/records/Person/ian", {
        "changes": {"inducted": True}, "expected_version": 1}) and "Done.",
    "refuse-induction": lambda served, task: _refused(_lend(served, "saw-01", "ian", "2026-10-17", "2026-10-24")),
    "refuse-expired": lambda served, task: _refused(_lend(served, "sander-01", "hana", "2026-10-17", "2026-10-24")),
    "refuse-on-loan": lambda served, task: _refused(_lend(served, "drill-01", "dev", "2026-10-17", "2026-10-24")),
    "refuse-too-long": lambda served, task: _refused(_lend(served, "sander-01", "dev", "2026-10-17", "2026-11-14")),
}
