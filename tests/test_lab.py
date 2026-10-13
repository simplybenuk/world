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
        self.assertEqual(error.details["open_deviations"], ["gau-102-high"])
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


if __name__ == "__main__":
    unittest.main()
