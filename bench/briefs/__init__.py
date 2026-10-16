"""The domains for the authoring test, each a brief in plain words with tasks and checks.

Each brief names the people and records the tasks need, with their IDs, so that the checks can find them
whatever an author calls the fields. Everything else is the author's choice. `<domain>-reference.json` is
a world written to the brief by hand, which `python3 -m bench.authoring check` uses to test the checks.
"""

from dataclasses import dataclass, field


@dataclass
class Task:
    id: str
    actor: str
    prompt: str
    answer: list = field(default_factory=list)   # each entry: the answer must contain one of these texts
    changes: list = field(default_factory=list)  # each entry: some change after the seed must match it, or one of a list
    forbid: list = field(default_factory=list)   # no change after the seed may match any of these


def change(*values, actions=("create", "update")):
    """A change that holds every value: text found inside a field's text, a number or boolean equal to a field,
    or a tuple of alternatives. A record's own ID counts as one of its values."""
    return {"values": values, "actions": actions}


from . import clinic, festival, library  # noqa: E402

DOMAINS = {"clinic": clinic, "festival": festival, "library": library}
