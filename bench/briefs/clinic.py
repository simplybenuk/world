"""A veterinary clinic."""

from . import Task, change

BRIEF = """
Harbourside Vets is a small veterinary clinic. It needs to keep track of its staff, the animals it treats and
their owners, appointments, prescriptions, and weights.

Staff, by ID:
- okafor: Dr Amara Okafor, a vet.
- lind: Dr Erik Lind, a vet.
- sam: Sam Patel, a receptionist.
- jo: Jo Reyes, a veterinary nurse.

Owners, by ID. Owners do not use the system themselves.
- shah: Priya Shah, phone 07700 900123.
- becker: Tom Becker, phone 07700 900456.

Animals, by ID:
- rex: Rex, a dog, born 2019-04-02, owned by Priya Shah.
- mittens: Mittens, a cat, born 2021-06-15, owned by Tom Becker.
- biscuit: Biscuit, a rabbit, owned by Priya Shah. Biscuit died on 2026-08-20.

Appointments already booked, by ID. Each is for one animal, with one vet, on a date, for a vaccination, a
check-up, or surgery.
- appt-rex-1103: Rex, vaccination with Dr Okafor on 2026-11-03.
- appt-mittens-1104: Mittens, check-up with Dr Lind on 2026-11-04.
- appt-rex-1104: Rex, check-up with Dr Lind on 2026-11-04.

How the clinic works:
- All staff can see everything.
- Receptionists and vets book appointments. Only vets may book surgery.
- An appointment must be with a vet.
- No appointment may be booked for an animal that has died.
- Appointments are cancelled, never deleted, and anyone who can book one can cancel it.
- Only vets may prescribe. A prescription names the animal, the medicine, the dose, and the vet who prescribed it,
  who must be the vet writing it.
- Nurses and vets record an animal's weight, in kilograms to one decimal place, with the date it was taken.
- Receptionists and vets add and change owners and animals, including recording that an animal has died.
"""

PEOPLE = ("okafor", "lind", "sam", "jo")
RECORDS = (("owner", "shah"), ("owner", "becker"), ("animal", "rex"), ("animal", "mittens"), ("animal", "biscuit"),
           ("appointment", "appt-rex-1103"), ("appointment", "appt-mittens-1104"), ("appointment", "appt-rex-1104"))

TASKS = [
    Task("read-vet", "sam", "Who is Rex's appointment on 3 November 2026 with?", answer=[("Okafor",)]),
    Task("read-owner", "jo", "Who owns Mittens, and what kind of animal is Mittens?", answer=[("Becker",), ("cat",)]),
    Task("count-day", "sam", "How many appointments are booked for 4 November 2026?", answer=[(r"re:\b(2|two)\b",)]),
    Task("book", "sam", "Book Mittens in for a vaccination with Dr Lind on 12 November 2026.",
         changes=[change("mittens", "lind", "2026-11-12", "vaccin")]),
    Task("prescribe", "okafor", "Prescribe Rex carprofen, 50 mg twice a day.", changes=[change("rex", "carprofen", "okafor")]),
    Task("weigh", "jo", "Rex was weighed today at 31.5 kg. Record it.", changes=[change("rex", ("31.5", 31500, 315, "31,5"))]),
    Task("cancel", "sam", "Mr Becker called to cancel Mittens' check-up on 4 November. Cancel it.",
         changes=[change("appt-mittens-1104", "cancel", actions=("update",))]),
    Task("refuse-died", "sam", "Book Biscuit in for a check-up with Dr Okafor on 10 November 2026.",
         forbid=[change("biscuit", "2026-11-10")]),
    Task("refuse-nurse-prescribes", "jo", "Prescribe Mittens amoxicillin, 50 mg once a day.", forbid=[change("amoxicillin")]),
    Task("refuse-surgery", "sam", "Book Rex in for surgery with Dr Okafor on 20 November 2026.", forbid=[change("rex", "2026-11-20")]),
]


def _try(client, method, path, body=None):
    return client.call(method, path, body)


def _book(served, actor, animal, vet, day, reason):
    return _try(served.client(actor), "POST", "/api/records/Appointment",
                {"data": {"animal": animal, "vet": vet, "date": day, "reason": reason}})


def _solve_refusal(status):
    return "World refused it, so it was not booked." if status >= 400 else "Done."


SOLUTIONS = {
    "read-vet": lambda served, task: "Dr Okafor.",
    "read-owner": lambda served, task: "Tom Becker; Mittens is a cat.",
    "count-day": lambda served, task: "There are 2 appointments.",
    "book": lambda served, task: _book(served, "sam", "mittens", "lind", "2026-11-12", "vaccination") and "Booked.",
    "prescribe": lambda served, task: _try(served.client("okafor"), "POST", "/api/records/Prescription", {"data": {
        "animal": "rex", "medicine": "carprofen", "dose": "50 mg twice a day"}}) and "Prescribed.",
    "weigh": lambda served, task: _try(served.client("jo"), "POST", "/api/records/Weight", {"data": {
        "animal": "rex", "tenths_of_kg": 315, "taken_on": "2026-10-17"}}) and "Recorded.",
    "cancel": lambda served, task: _try(served.client("sam"), "PATCH", "/api/records/Appointment/appt-mittens-1104", {
        "changes": {"status": "cancelled"}, "expected_version": 1}) and "Cancelled.",
    "refuse-died": lambda served, task: _solve_refusal(_book(served, "sam", "biscuit", "okafor", "2026-11-10", "check-up")[0]),
    "refuse-nurse-prescribes": lambda served, task: _solve_refusal(_try(served.client("jo"), "POST", "/api/records/Prescription", {
        "data": {"animal": "mittens", "medicine": "amoxicillin", "dose": "50 mg once a day", "vet": "okafor"}})[0]),
    "refuse-surgery": lambda served, task: _solve_refusal(_book(served, "sam", "rex", "okafor", "2026-11-20", "surgery")[0]),
}
