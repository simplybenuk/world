"""Rules are parsed into a checked syntax tree and walked. Nothing in them runs as Python."""

import unittest

from world import WorldError, WorldStore
from world.definition import Definition
from world.rules import UNKNOWN, Evaluation, RuleError, World, parse, possible
import worlds


class RuleLanguageTest(unittest.TestCase):
    def test_only_the_documented_forms_parse(self):
        types = ("Person", "Task")
        for accepted in (
            "actor.role == 'admin' or record.owner == actor.id",
            "record.owner is None or record.owner in record.project.members",
            "not any(t.status == 'todo' for t in Task if t.owner == record.id)",
            "count(p for p in Person if p.role == 'admin') > 1",
            "changed('status') and saved.status not in ('done', 'todo')",
            "can_read(record.project)",
            "days(record.due, today()) <= 14",
        ):
            parse(accepted, types)
        for refused in (
            "__import__('os').system('true')", "open('/etc/passwd')", "record.owner.__class__", "(lambda: 1)()",
            "actor.role == 'admin' if True else False", "record.amount + 1 > 2", "[p for p in Person]", "any(p for p in Secret)",
            "getattr(actor, 'role')", "changed(record.status)", "f'{actor.role}'", "unknown_name == 1", "1.5 > 1", "",
            "days(record.due)", "today(1)", "days(*record.due)", "days(record.due, today=1)", "days(record.due, open('x'))",
        ):
            with self.subTest(refused), self.assertRaises((RuleError, AttributeError)):
                tree = parse(refused, types)
                # A dunder name parses as a field path and is refused when it is followed.
                store = WorldStore(":memory:")
                self.addCleanup(store.close)
                with store._transaction() as connection:
                    world = store._rules(connection)
                    Evaluation(world, world.node(store._raw(connection, "Person", "ben")),
                               world.node(store._raw(connection, "Task", "homepage")), None).run(refused)
                del tree

    def test_missing_values_and_unknown_records_do_not_raise(self):
        store = WorldStore(":memory:", worlds.load("crm"))
        self.addCleanup(store.close)
        with store._transaction() as connection:
            world = store._rules(connection)
            priya, nina = (world.node(store._raw(connection, "Person", name)) for name in ("priya", "nina"))
            note = world.node(store._raw(connection, "Interaction", "pine-check-in"))
            run = lambda expression, actor=priya: Evaluation(world, actor, note, note).run(expression)
            self.assertIsNone(run("record.opportunity.owner.role"))
            self.assertFalse(run("record.opportunity.amount > 0"))
            self.assertTrue(run("record.opportunity is None"))
            self.assertEqual(run("record.organization.owner.role"), "rep")
            self.assertEqual(run("count(o for o in Opportunity if o.owner == actor.id)"), 3)
            # With no record chosen, only the actor's part of a rule can decide it.
            create = store.types["Opportunity"]["rules"]["create"]
            self.assertIs(Evaluation(world, priya, UNKNOWN, UNKNOWN).run(create), UNKNOWN)
            self.assertTrue(possible(world, priya, create))
            self.assertFalse(possible(world, nina, create))

    def test_days_counts_whole_days_and_today_is_the_date_now(self):
        store = WorldStore(":memory:")
        self.addCleanup(store.close)
        with store._transaction() as connection:
            world = store._rules(connection)
            ben = world.node(store._raw(connection, "Person", "ben"))
            run = lambda expression, record=None: Evaluation(world, ben, record, None).run(expression)
            self.assertEqual(run("days('2026-10-17', '2026-10-31')"), 14)
            self.assertEqual(run("days('2026-10-31', '2026-10-17')"), -14)
            self.assertEqual(run("days('2024-02-28', '2024-03-01')"), 2)  # a leap year
            self.assertEqual(run("days('2026-10-17T23:59:00.000Z', '2026-10-18')"), 1)  # a timestamp's date
            self.assertIsNone(run("days(None, '2026-10-18')"))
            self.assertIsNone(run("days('soon', '2026-10-18')"))
            self.assertFalse(run("days(None, '2026-10-18') <= 14"))  # ordering against None is false
            self.assertEqual(run("days(today(), today())"), 0)
            self.assertIs(Evaluation(world, ben, UNKNOWN, UNKNOWN).run("days(record.due, today()) > 0"), UNKNOWN)
            self.assertTrue(possible(world, ben, "days(record.due, today()) > 0"))

    def test_a_definition_with_a_bad_rule_fails_when_it_loads(self):
        class Broken(Definition):
            name, title, version = "broken", "Broken", "0"
            types = {"Person": {"description": "", "fields": {"name": {"type": "text"}},
                                "rules": {"read": "True", "create": "actor.role = 'admin'", "update": "True", "delete": "True"}}}

        with self.assertRaisesRegex(RuleError, "Person: Cannot parse rule"):
            WorldStore(":memory:", Broken())

    def test_the_description_shows_the_rules_the_engine_enforces(self):
        store = WorldStore(":memory:", worlds.load("lab"))
        self.addCleanup(store.close)
        described = store.describe("kofi")["types"]["Deviation"]
        self.assertEqual(described["capabilities"]["rules"], store.types["Deviation"]["rules"])
        self.assertEqual(described["constraints"], [constraint["check"] for constraint in store.types["Deviation"]["constraints"]])
        self.assertEqual(described["capabilities"]["create"], True)
        self.assertEqual(store.describe("ines")["types"]["Deviation"]["capabilities"], {
            "read": True, "create": False, "update": False, "delete": False, "rules": store.types["Deviation"]["rules"]})
        # Changing only the stated rule changes what the engine does.
        original = store.types["Deviation"]["rules"]["update"]
        self.addCleanup(store.types["Deviation"]["rules"].__setitem__, "update", original)
        store.types["Deviation"]["rules"]["update"] = "actor.role == 'manager'"
        with self.assertRaises(WorldError) as refused:
            store.update("kofi", "Deviation", "pip-007-ejector", {"summary": "Edited."}, 1)
        self.assertEqual(refused.exception.details["rule"], "actor.role == 'manager'")


if __name__ == "__main__":
    unittest.main()
