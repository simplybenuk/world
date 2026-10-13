"""The calibration lab is a third world on the same engine, used for the unfamiliar-domain comparison."""

import unittest

from world import WorldError, WorldStore
import worlds


class LabTest(unittest.TestCase):
    def setUp(self):
        self.store = WorldStore(":memory:", worlds.load("lab"))
        self.addCleanup(self.store.close)

    def assert_error(self, status, action, code=None):
        with self.assertRaises(WorldError) as caught:
            action()
        self.assertEqual((caught.exception.status, code or caught.exception.code), (status, caught.exception.code))
        return caught.exception

    def test_an_instrument_cannot_return_to_service_with_an_open_deviation(self):
        error = self.assert_error(422, lambda: self.store.update("kofi", "Instrument", "gau-102", {"state": "in-service"}, 1), "constraint_failed")
        self.assertIn("for d in Deviation if d.instrument == record.id", error.details["constraint"])
        self.store.update("kofi", "Deviation", "gau-102-high", {"disposition": "rework", "closed_on": "2026-10-13"}, 1)
        self.assertEqual(self.store.update("kofi", "Instrument", "gau-102", {"state": "in-service"}, 1)["data"]["state"], "in-service")

    def test_calibrations_and_deviations_need_their_supporting_fields(self):
        calibration = {"instrument": "bal-014", "performed_on": "2026-10-13", "result": "adjusted"}
        self.assertEqual(self.assert_error(422, lambda: self.store.create("kofi", "Calibration", calibration)).details["fields"], ["drift_ppm"])
        self.assertEqual(self.store.create("kofi", "Calibration", {**calibration, "drift_ppm": 180})["data"]["technician"], "kofi")
        close = lambda changes: self.store.update("kofi", "Deviation", "pip-007-ejector", changes, 1)
        self.assertEqual(self.assert_error(422, lambda: close({"disposition": "use-as-is"})).details["fields"], ["closed_on"])
        self.assert_error(422, lambda: self.store.create("kofi", "Deviation", {
            "instrument": "bal-014", "calibration": "gau-102-last", "severity": "minor", "summary": "Wrong instrument."}), "invalid_relationship")

    def test_roles_limit_who_can_change_what(self):
        self.assert_error(403, lambda: self.store.update("kofi", "Instrument", "th-305", {"state": "quarantined"}, 1))
        self.assert_error(403, lambda: self.store.update("kofi", "Instrument", "th-221", {"custodian": "lena"}, 1))
        self.assert_error(403, lambda: self.store.update("lena", "Deviation", "bal-020-dropped", {"disposition": "scrap", "closed_on": "2026-10-13"}, 1))
        self.store.update("ada", "Deviation", "bal-020-dropped", {"disposition": "scrap", "closed_on": "2026-10-13"}, 1)
        self.assert_error(403, lambda: self.store.create("ines", "Deviation", {"instrument": "bal-014", "severity": "minor", "summary": "No."}))
        self.assert_error(403, lambda: self.store.create("kofi", "Instrument", {
            "tag": "X-1", "kind": "gauge", "site": "cork", "custodian": "kofi", "interval_days": 30}))
        self.assertEqual(len(self.store.list_records("ines")), 20)

    def test_a_query_follows_references_and_can_include_the_records_it_names(self):
        found = self.store.find("kofi", "Deviation", where="record.instrument.tag == 'PIP-007' and record.disposition == 'open'",
                                include=["instrument"])
        self.assertEqual([record["id"] for record in found["records"]], ["pip-007-ejector"])
        self.assertEqual([record["data"]["tag"] for record in found["included"]], ["PIP-007"])
        mine = self.store.find("kofi", "Instrument", where="record.custodian == actor.id and record.site == 'leeds'")["records"]
        self.assertEqual(len(mine), 5)
        blocked = self.store.find("kofi", "Instrument", where="any(d.disposition == 'open' for d in Deviation if d.instrument == record.id)")
        self.assertEqual({record["id"] for record in blocked["records"]}, {"pip-007", "gau-102", "bal-020"})
        self.assertTrue(any(line.endswith("  kofi  viewed  Instrument pip-007  Included in a query.")
                            for line in self.store.audit("ada", "Instrument", "pip-007")))
        for refused in ("record.instrument.nope == 1", "__import__('os')", "record.tag ==", " or ".join(["record.tag == 'x'"] * 60)):
            self.assertEqual(self.assert_error(400, lambda: self.store.find("kofi", "Deviation", where=refused)).code, "invalid_query")
        self.assert_error(400, lambda: self.store.find("kofi", "Deviation", include=["severity"]))

    def test_a_query_cannot_see_through_a_record_the_actor_may_not_read(self):
        crm = WorldStore(":memory:", worlds.load("crm"))
        self.addCleanup(crm.close)
        # Priya cannot read Tom's opportunities, so a path through one gives None and matches nothing.
        probe = "record.opportunity.amount == 9000"
        self.assertEqual([record["id"] for record in crm.find("tom", "Interaction", where=probe)["records"]], ["alder-training-note"])
        self.assertEqual(crm.find("priya", "Interaction", where=probe)["records"], [])
        named = crm.find("priya", "Opportunity", where="'HARBOR' in record.name or 'renewal' in record.name")["records"]
        self.assertEqual({record["id"] for record in named}, {"harbor-pilot", "northwind-renewal"})
        self.assertEqual(crm.find("priya", "Opportunity", where="'harbor' not in record.name and record.lost_reason is None and 'x' in record.lost_reason")["records"], [])
        counted = "count(o for o in Opportunity if o.organization == record.id) == 2"
        self.assertEqual({record["id"] for record in crm.find("priya", "Organization", where=counted)["records"]}, {"northwind"})
        self.assertEqual({record["id"] for record in crm.find("ben", "Organization", where=counted)["records"]}, {"northwind", "alder"})

    def test_several_changes_are_saved_together_or_not_at_all(self):
        steps = [
            {"create": "Calibration", "as": "calibration", "data": {"instrument": "th-221", "performed_on": "2026-10-13", "result": "fail", "drift_ppm": 950}},
            {"create": "Deviation", "data": {"instrument": "th-221", "calibration": "$calibration", "severity": "major", "summary": "Failed."}},
            {"update": "Instrument", "id": "th-221", "changes": {"state": "quarantined"}, "expected_version": 1},
        ]
        calibration, deviation, instrument = self.store.apply("kofi", steps, {"reason": "TH-221 failed."})
        self.assertEqual(deviation["data"]["calibration"], calibration["id"])
        self.assertEqual(instrument["data"]["state"], "quarantined")
        self.assertEqual(self.store.history("ada", "Deviation", deviation["id"])[0]["reason"], "TH-221 failed.")

        before = len(self.store.list_records("ada"))
        refused = self.assert_error(422, lambda: self.store.apply("kofi", [
            {"create": "Calibration", "data": {"instrument": "bal-014", "performed_on": "2026-10-13", "result": "pass"}},
            {"update": "Instrument", "id": "gau-102", "changes": {"state": "in-service"}, "expected_version": 1},
        ]), "constraint_failed")
        self.assertEqual((refused.details["step"], refused.details["saved"]), (1, False))
        self.assertIn("nothing was saved", refused.message)
        self.assertEqual(len(self.store.list_records("ada")), before)
        self.assert_error(422, lambda: self.store.apply("kofi", [{"create": "Deviation", "data": {
            "instrument": "bal-014", "calibration": "$missing", "severity": "minor", "summary": "x"}}]), "unknown_name")
        self.assert_error(400, lambda: self.store.apply("kofi", []))
        self.assert_error(400, lambda: self.store.apply("kofi", [{"create": "Deviation", "update": "Deviation"}]))
        self.assert_error(403, lambda: self.store.apply("ines", [steps[0]]))


if __name__ == "__main__":
    unittest.main()
