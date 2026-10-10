"""Integration tests. Each one skips itself when its dependency isn't there
(Redis not running, no replay files on this machine) so the suite still passes
on a fresh clone."""

import glob
import os
import unittest

import events

REDIS_UP = events.ping()


@unittest.skipUnless(REDIS_UP, "Redis isn't running")
class BatchEvents(unittest.TestCase):
    def setUp(self):
        self.ids = []

    def tearDown(self):
        r = events.client()
        for b in self.ids:
            r.delete(f"batch:{b}", f"batch:{b}:events")

    def batch(self, *a, **kw):
        b = events.create_batch(*a, **kw)
        self.ids.append(b)
        return b

    def test_counters_and_finish(self):
        b = self.batch("mine", total=3)
        events.record(b, "added", "a.SC2Replay")
        events.record(b, "skipped", "b.SC2Replay", "already analysed")
        s = events.snapshot(b)
        self.assertEqual((s["done"], s["added"], s["skipped"], s["finished"]), (2, 1, 1, False))
        events.record(b, "failed", "c.SC2Replay", "boom")
        s = events.snapshot(b)
        self.assertTrue(s["finished"])
        self.assertEqual(s["failed"], 1)

    def test_a_scan_isnt_finished_until_its_folder_is_listed(self):
        b = self.batch("mine")                                       # total unknown yet
        self.assertFalse(events.snapshot(b)["finished"])
        events.set_total(b, 0)                                       # empty folder
        self.assertTrue(events.snapshot(b)["finished"])

    def test_stream_delivers_events_in_order(self):
        b = self.batch("pro", total=2)
        events.record(b, "added", "one.SC2Replay")
        events.record(b, "unmatched", "two.SC2Replay", "why")
        got = events.read_events(b, "0", block_ms=100)
        self.assertEqual([f["file"] for _, f in got], ["one.SC2Replay", "two.SC2Replay"])
        last = got[-1][0]
        self.assertEqual(events.read_events(b, last, block_ms=50), [])   # nothing newer

    def test_unknown_outcome_rejected(self):
        b = self.batch("mine", total=1)
        with self.assertRaises(ValueError):
            events.record(b, "exploded")

    def test_unknown_batch(self):
        self.assertIsNone(events.snapshot("does-not-exist"))


def _replays():
    folder = os.environ.get("SC2_TEST_REPLAYS") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pro_uploads")
    return sorted(glob.glob(os.path.join(folder, "*.SC2Replay")))[:5]


@unittest.skipUnless(_replays(), "no .SC2Replay files available (set SC2_TEST_REPLAYS to a folder)")
class RealReplays(unittest.TestCase):
    """Properties that must hold for any real replay - not specific numbers."""

    def test_records_are_internally_consistent(self):
        import analyzer
        import sc2reader
        checked = 0
        for path in _replays():
            records = analyzer.extract_both_perspectives(path)
            if not records:
                continue                                              # e.g. vs-AI practice game
            replay = sc2reader.load_replay(path, load_level=0)
            for rec in records:
                checked += 1
                tel = rec["telemetry"]
                # game length is the real Faster clock, i.e. frames / 22.4, not sc2reader's 16 fps
                self.assertAlmostEqual(rec["game_length"], replay.frames / 22.4, delta=1.5)
                self.assertEqual(len(tel["me"]), len(tel["opp"]))
                self.assertEqual(len(tel["me"]), int(tel["length"] // tel["snap"]) + 1)
                x0, y0, x1, y1 = tel["bounds"]
                self.assertTrue(0 <= x0 < x1 <= 256 and 0 <= y0 < y1 <= 256)
                if rec["first_supply_time"] is not None:
                    self.assertGreater(rec["first_supply_time"], 0)
                    self.assertLess(rec["first_supply_time"], 120)    # a first depot/pylon/overlord, not minute 20
                if rec["apm_retention"] is not None:
                    self.assertGreater(rec["apm_retention"], 0)
                self.assertEqual([s["time"] for s in rec["build_order"]], sorted(s["time"] for s in rec["build_order"]))
            self.assertEqual(records[0]["win"], not records[1]["win"])
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
