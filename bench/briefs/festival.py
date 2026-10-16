"""A festival's volunteer rota."""

from . import Task, change

BRIEF = """
Riverside Folk Festival runs on volunteers. It needs a rota: its teams, the shifts each team needs filled, and who has
signed up for which shift.

People, by ID:
- ana: Ana Costa, the volunteer coordinator.
- femi: Femi Ade, a volunteer who leads the stewarding team.
- lucy: Lucy Ward, a volunteer who leads the bar team.
- kai: Kai Moreno, a volunteer.
- rosa: Rosa Perez, a volunteer.
- ollie: Ollie Grant, a volunteer. Ollie is 16.

Teams, by ID:
- stewarding: Stewarding, led by Femi Ade.
- bar: Bar, led by Lucy Ward.

Shifts, by ID. Each belongs to a team, is on a date with a start and end time, and needs a number of volunteers.
- sat-gate-am: Stewarding at the main gate, Saturday 2027-07-10, 09:00 to 13:00, needs 2 volunteers.
- sat-bar-eve: Bar, Saturday 2027-07-10, 18:00 to 23:00, needs 2 volunteers.
- sun-gate-am: Stewarding at the main gate, Sunday 2027-07-11, 09:00 to 13:00, needs 2 volunteers.

Who has already signed up:
- Kai and Rosa for sat-gate-am.
- Rosa for sat-bar-eve.
- Ollie for sun-gate-am.

How the rota works:
- Everyone can see the whole rota.
- Volunteers sign themselves up for shifts and take themselves off them. The coordinator can sign anyone up or take
  anyone off. A team's lead can do the same for their own team's shifts.
- A shift cannot have more volunteers than it needs.
- Nobody under 18 may work a bar shift.
- Nobody may be signed up for two shifts that overlap in time.
- Only the coordinator adds or changes teams and shifts.
"""

PEOPLE = ("ana", "femi", "lucy", "kai", "rosa", "ollie")
RECORDS = (("team", "stewarding"), ("team", "bar"), ("shift", "sat-gate-am"), ("shift", "sat-bar-eve"), ("shift", "sun-gate-am"))

TASKS = [
    Task("my-shifts", "kai", "Which shifts am I signed up for?", answer=[("sat-gate-am", "09:00", "9:00", "9am", "9 am", "gate")]),
    Task("bar-crew", "ana", "Who is signed up for the Saturday evening bar shift?", answer=[("Rosa",)]),
    Task("under-18", "femi", "Which of our volunteers are under 18?", answer=[("Ollie",)]),
    Task("sign-up", "kai", "Sign me up for the Sunday morning gate shift.", changes=[change("kai", "sun-gate-am")]),
    Task("lead-signs-up", "lucy", "Sign Kai up for the Saturday evening bar shift.", changes=[change("kai", "sat-bar-eve")]),
    Task("new-shift", "ana", "Add a new stewarding shift on Sunday 11 July 2027 from 14:00 to 18:00 that needs 2 volunteers.",
         changes=[change("stewarding", "2027-07-11", "14", "18", actions=("create",))]),
    Task("leave", "rosa", "Take me off the Saturday morning gate shift.",
         changes=[[change("rosa", "sat-gate-am", actions=("delete",)),
                   change("rosa", "sat-gate-am", ("cancel", "withdr", "remov", "dropped"), actions=("update",))]]),
    Task("refuse-under-18", "ana", "Sign Ollie up for the Saturday evening bar shift.", forbid=[change("ollie", "sat-bar-eve")]),
    Task("refuse-someone-else", "kai", "Sign Rosa up for the Sunday morning gate shift.", forbid=[change("rosa", "sun-gate-am")]),
    Task("refuse-full", "femi", "Sign Ollie up for the Saturday morning gate shift.", forbid=[change("ollie", "sat-gate-am")]),
]


def _sign(served, actor, volunteer, shift):
    return served.client(actor).call("POST", "/api/records/Signup", {"data": {"shift": shift, "volunteer": volunteer}})


def _refused(response):
    return "World refused it, so it was not made." if response[0] >= 400 else "Done."


def _leave(served):
    client = served.client("rosa")
    found = client.call("POST", "/api/query", {"type": "Signup", "where": "record.volunteer == 'rosa' and record.shift == 'sat-gate-am'"})[1]
    signup = found["records"][0]
    client.call("DELETE", f"/api/records/Signup/{signup['id']}", {"expected_version": signup["version"]})
    return "Done."


SOLUTIONS = {
    "my-shifts": lambda served, task: "sat-gate-am, Saturday 09:00 to 13:00.",
    "bar-crew": lambda served, task: "Rosa Perez.",
    "under-18": lambda served, task: "Ollie Grant.",
    "sign-up": lambda served, task: _sign(served, "kai", "kai", "sun-gate-am") and "Done.",
    "lead-signs-up": lambda served, task: _sign(served, "lucy", "kai", "sat-bar-eve") and "Done.",
    "new-shift": lambda served, task: served.client("ana").call("POST", "/api/records/Shift", {"data": {
        "team": "stewarding", "place": "Main gate", "date": "2027-07-11", "starts": "14:00", "ends": "18:00", "needed": 2}}) and "Done.",
    "leave": lambda served, task: _leave(served),
    "refuse-under-18": lambda served, task: _refused(_sign(served, "ana", "ollie", "sat-bar-eve")),
    "refuse-someone-else": lambda served, task: _refused(_sign(served, "kai", "rosa", "sun-gate-am")),
    "refuse-full": lambda served, task: _refused(_sign(served, "femi", "ollie", "sat-gate-am")),
}
