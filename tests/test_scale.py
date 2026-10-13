"""What keeps a large world usable: indexes, pages, counts, and a description that is an index first."""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from bench.harness import running
from world import WorldError, WorldStore
import worlds


class ScaleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "world.sqlite3"
        self.store = WorldStore(self.path, worlds.load("enterprise"))
        self.addCleanup(self.store.close)

    def test_a_large_world_describes_itself_as_an_index_then_in_detail(self):
        index = self.store.describe("kofi")
        self.assertNotIn("types", index)
        self.assertEqual(len(index["type_index"]), 201)
        self.assertIn("Procurement", index["type_index"]["PurchaseOrder"])
        detail = self.store.describe("kofi", ["Instrument", "PurchaseOrder"])
        self.assertEqual(list(detail["types"]), ["Instrument", "PurchaseOrder"])
        self.assertIn("update", detail["types"]["PurchaseOrder"]["capabilities"]["rules"])
        with self.assertRaises(WorldError):
            self.store.describe("kofi", ["Nope"])
        # A small world still answers in full.
        small = WorldStore(":memory:", worlds.load("lab"))
        self.addCleanup(small.close)
        self.assertEqual(len(small.describe("kofi")["types"]), 4)

    def test_the_index_and_detail_are_served_over_http(self):
        with running("enterprise") as world:
            client = world.client("kofi")
            index = client.get("/api/world")
            self.assertIn("GET /api/world?types=A,B", index["types"])
            self.assertLess(world.requests[-1][3], 20000)
            self.assertEqual(list(client.get("/api/world?types=Deviation")["types"]), ["Deviation"])
            self.assertEqual(client.get("/api/types/Deviation")["name"], "Deviation")
            self.assertEqual(client.call("GET", "/api/world?types=Nope")[0], 400)

    def test_queries_come_back_in_pages_and_can_be_counted(self):
        for index in range(25):
            self.store.create("ada", "Supplier", {"code": f"SUP-{index:02d}", "title": f"Supplier {index}", "site": "cork" if index % 5 else "lyon"})
        first = self.store.find("kofi", "Supplier", where="record.site == 'cork'", limit=8)
        self.assertEqual((len(first["records"]), first["more"]), (8, True))
        seen = [record["id"] for record in first["records"]]
        page = first
        while page["more"]:
            page = self.store.find("kofi", "Supplier", where="record.site == 'cork'", limit=8, after=page["next"])
            seen += [record["id"] for record in page["records"]]
        self.assertEqual(len(seen), 20)
        self.assertEqual(len(set(seen)), 20)
        self.assertEqual(self.store.find("kofi", "Supplier", where="record.site == 'cork'", count=True), {"count": 20})
        self.assertEqual(self.store.find("kofi", "Supplier", where="'supplier 1' in record.title", count=True), {"count": 11})
        # A count shows no record, so it writes no view lines.
        before = len(self.store.audit("ada", limit=10000))
        self.store.find("kofi", "Supplier", count=True)
        self.assertEqual(len(self.store.audit("ada", limit=10000)), before)
        for refused in ({"limit": 0}, {"limit": 1001}, {"limit": "5"}, {"after": 5}):
            with self.subTest(refused), self.assertRaises(WorldError):
                self.store.find("kofi", "Supplier", **refused)
        with running("enterprise") as world:
            client = world.client("kofi")
            _, listed = client.call("GET", "/api/records?type=Instrument&limit=4")
            self.assertEqual((len(listed["records"]), listed["more"]), (4, True))
            _, rest = client.call("GET", f"/api/records?type=Instrument&limit=100&after={listed['next']}")
            self.assertEqual(len(rest["records"]), 5)
            self.assertNotIn("more", rest)
            self.assertEqual(client.call("POST", "/api/query", {"type": "Instrument", "where": "record.state == 'quarantined'", "count": True})[1], {"count": 4})

    def test_an_exact_match_is_answered_from_an_index(self):
        with closing(sqlite3.connect(self.path)) as connection:
            plans = lambda sql, values: " ".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, values))
            with self.store._transaction() as inner:
                narrowed, values = self.store._narrow(inner, "Deviation", [(("instrument", "tag"), "eq", "PIP-007"), (("disposition",), "eq", "open")])
            self.assertIn("USING INDEX field_", plans(f"SELECT * FROM records WHERE type = 'Deviation' AND deleted_at IS NULL{narrowed}", values))
            self.assertEqual(values, ["pip-007", "open"])

    def test_the_reference_index_follows_every_change(self):
        referrers = lambda: {(item["type"], item["id"]) for item in self.store._inbound_for("Instrument", "bal-014")}
        self.assertEqual(referrers(), {("Calibration", "bal-014-last")})
        calibration = self.store.create("kofi", "Calibration", {"instrument": "bal-014", "performed_on": "2026-10-13", "result": "pass"})
        self.assertIn(("Calibration", calibration["id"]), referrers())
        self.store.update("kofi", "Calibration", calibration["id"], {"instrument": "th-221"}, 1)
        self.assertNotIn(("Calibration", calibration["id"]), referrers())
        self.store.update("kofi", "Calibration", calibration["id"], {"instrument": "bal-014"}, 2)
        self.store.delete("ada", "Calibration", calibration["id"], 3)
        self.assertEqual(referrers(), {("Calibration", "bal-014-last")})
        # A database from before the index existed is indexed when it is next opened.
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM refs")
            connection.execute("DELETE FROM world_meta WHERE key = 'refs'")
            connection.commit()
        self.store = WorldStore(self.path, worlds.load("enterprise"))
        self.assertEqual(referrers(), {("Calibration", "bal-014-last")})
        with self.assertRaises(WorldError) as blocked:
            self.store.delete("ada", "Instrument", "bal-014", 1)
        self.assertEqual(blocked.exception.code, "record_referenced")


if __name__ == "__main__":
    unittest.main()
