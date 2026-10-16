"""A world written as JSON loads, seeds, and is refused with a useful message when it is wrong."""

import json
from pathlib import Path
import tempfile
import unittest

from bench.authoring import validate
from bench.briefs import DOMAINS
from world.definition import DataDefinition, load_json
from world.rules import RuleError
from world.store import WorldStore
import worlds

BRIEFS = Path(__file__).resolve().parent.parent / "bench" / "briefs"


class JSONWorldTests(unittest.TestCase):
    def test_the_reference_worlds_load_and_seed(self):
        for name, domain in DOMAINS.items():
            with self.subTest(name):
                self.assertIsNone(validate(domain, BRIEFS / f"{name}-reference.json"))
                definition = worlds.load(str(BRIEFS / f"{name}-reference.json"))
                self.assertEqual(definition.actors, domain.PEOPLE)

    def test_a_wrong_world_is_refused_with_what_is_wrong(self):
        world = json.loads((BRIEFS / "clinic-reference.json").read_text())
        cases = [
            (lambda w: w.pop("types"), "missing types"),
            (lambda w: w["types"]["Appointment"]["fields"]["vet"].update(target="Doctor"), "refers to 'Doctor'"),
            (lambda w: w["types"]["Appointment"]["fields"]["reason"].pop("values"), "needs values"),
            (lambda w: w["types"]["Weight"]["fields"]["tenths_of_kg"].update(type="decimal"), "needs a type"),
            (lambda w: w["types"]["Weight"]["rules"].pop("delete"), "missing 'delete'"),
            (lambda w: w.update(actor_type="Staff"), "not one of the types"),
            (lambda w: w["types"]["Prescription"]["rules"].update(create="actor.role.upper() == 'VET'"), "Prescription"),
        ]
        for spoil, expected in cases:
            spoiled = json.loads(json.dumps(world))
            spoil(spoiled)
            with self.subTest(expected), self.assertRaises(RuleError) as raised:
                DataDefinition(spoiled).check()
            self.assertIn(expected, str(raised.exception))

    def test_a_seed_record_that_breaks_the_world_is_refused(self):
        world = json.loads((BRIEFS / "clinic-reference.json").read_text())
        world["records"].append({"type": "Appointment", "id": "bad", "data": {"animal": "nobody", "vet": "okafor",
                                                                             "date": "2026-11-05", "reason": "check-up"}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clinic.json"
            path.write_text(json.dumps(world))
            self.assertIn("nobody", validate(DOMAINS["clinic"], path) or "")
            with self.assertRaises(Exception):
                WorldStore(Path(directory) / "world.sqlite3", load_json(path))


if __name__ == "__main__":
    unittest.main()
