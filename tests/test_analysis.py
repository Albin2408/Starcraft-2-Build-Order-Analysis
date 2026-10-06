import json
import sqlite3
import unittest
from types import SimpleNamespace

import analyzer
import db
import heatmaps
import telemetry as T


class PatchCutoff(unittest.TestCase):
    def test_date_cutoff(self):
        self.assertTrue(analyzer.is_pre_patch({"played_at": "2026-06-08T10:00:00+00:00"}, "2026-06-22"))
        self.assertFalse(analyzer.is_pre_patch({"played_at": "2026-06-22T00:00:01+00:00"}, "2026-06-22"))
        self.assertFalse(analyzer.is_pre_patch({"played_at": "2026-07-01T00:00:00+00:00"}, "2026-06-22"))

    def test_version_cutoff(self):
        self.assertTrue(analyzer.is_pre_patch({"game_version": "5.0.15.96883"}, "5.0.16"))
        self.assertFalse(analyzer.is_pre_patch({"game_version": "5.0.16.97563"}, "5.0.16"))
        self.assertFalse(analyzer.is_pre_patch({"game_version": "5.1.0.1"}, "5.0.16"))
        self.assertTrue(analyzer.is_pre_patch({"game_version": "4.12.1.1"}, "5.0"))

    def test_unknown_or_blank_is_never_excluded(self):
        self.assertFalse(analyzer.is_pre_patch({}, "2026-06-22"))
        self.assertFalse(analyzer.is_pre_patch({}, "5.0.16"))
        self.assertFalse(analyzer.is_pre_patch({"played_at": "2020-01-01"}, ""))


class Names(unittest.TestCase):
    def test_battletag_matches_plain_name(self):
        self.assertEqual(analyzer.normalize_names(["Player#1234"]), {"player#1234", "player"})
        replay = SimpleNamespace(players=[SimpleNamespace(name="Player"), SimpleNamespace(name="Other")])
        self.assertEqual(analyzer.find_player_by_name(replay, ["player#1234"]).name, "Player")
        self.assertIsNone(analyzer.find_player_by_name(replay, ["nobody"]))

    def test_pretty_names_and_times(self):
        self.assertEqual(analyzer.prettify_name("CyberneticsCore"), "Cybernetics Core")
        self.assertEqual(analyzer.prettify_name("zerglingmovementspeed"), "Zergling Movement Speed")
        self.assertEqual(analyzer.fmt_time(119.6), "2:00")           # no "1:60"
        self.assertEqual(analyzer.fmt_time(None), "n/a")


def game(*steps, win=True):
    return {"build_order": [{"time": t, "kind": "structure", "name": n} for n, t in steps], "win": win}


class BuildGuide(unittest.TestCase):
    def test_guide_uses_median_time_and_nth_occurrence(self):
        pool = [game(("Pylon", 20), ("Gateway", 60), ("Gateway", 120)),
                game(("Pylon", 24), ("Gateway", 70)),
                game(("Pylon", 22), ("Gateway", 65), ("Gateway", 130)),
                game(("Pylon", 21), ("Stargate", 90))]
        guide = analyzer.aggregate_build_steps(pool, min_frequency=0.5)
        by = {(s["name"], s["occurrence"]): s for s in guide}
        self.assertEqual(by[("Pylon", 1)]["time"], 21.5)
        self.assertIn(("Gateway", 2), by)                            # 2 of 4 games = 50%
        self.assertNotIn(("Stargate", 1), by)                        # 1 of 4 < 50%
        self.assertEqual([s["time"] for s in guide], sorted(s["time"] for s in guide))

    def test_diff_reports_missing_and_late_steps(self):
        guide = [{"name": "Pylon", "label": "Pylon", "occurrence": 1, "time": 20, "frequency": 1.0},
                 {"name": "Gateway", "label": "Gateway", "occurrence": 1, "time": 60, "frequency": 1.0},
                 {"name": "Stargate", "label": "Stargate", "occurrence": 1, "time": 100, "frequency": 0.8}]
        mine = [game(("Pylon", 21), ("Gateway", 100)), game(("Pylon", 22), ("Gateway", 110)),
                game(("Pylon", 20), ("Gateway", 105))]
        diffs = {d["label"]: d for d in analyzer.compare_build_steps(mine, guide)}
        self.assertNotIn("Pylon", diffs)                             # within 15s
        self.assertEqual(diffs["Gateway"]["type"], "timing")
        self.assertEqual(diffs["Gateway"]["diff"], 45)
        self.assertEqual(diffs["Stargate"]["type"], "missing")

    def test_too_few_games_gives_no_diff(self):
        self.assertEqual(analyzer.compare_build_steps([game(("Pylon", 20))], [{"name": "Pylon", "label": "Pylon", "occurrence": 1, "time": 20, "frequency": 1}]), [])


def tele(length, me_cells, start_me, bounds=(0, 0, 128, 128)):
    snaps = max(1, length // 10)
    return {"snap": 10, "length": length, "bounds": list(bounds), "start_me": start_me, "start_opp": None,
            "me": [list(me_cells)] * snaps, "opp": [[]] * snaps, "deaths_me": [], "deaths_opp": [[30, me_cells[0]]]}


class HeatmapAggregation(unittest.TestCase):
    def test_games_from_opposite_corners_land_in_the_same_place(self):
        # same 1-unit army: standing at the player's own base, which is lower-left in one game
        # and upper-right in the other
        low, high = T.cell_of(10, 10), T.cell_of(118, 118)
        out = heatmaps.aggregate([tele(120, [low, 1], (10, 10)), tele(120, [high, 1], (118, 118))], out_grid=16)
        cells = {out["layers"]["me"][0][i] for i in range(0, len(out["layers"]["me"][0]), 2)}
        self.assertEqual(len(cells), 1)                              # one shared cell, not two
        self.assertEqual(out["games"], 2)
        self.assertAlmostEqual(out["layers"]["me"][0][1], 1.0, places=3)   # avg of 1 unit per game

    def test_unoriented_keeps_raw_orientation(self):
        low, high = T.cell_of(10, 10), T.cell_of(118, 118)
        out = heatmaps.aggregate([tele(60, [low, 1], (10, 10)), tele(60, [high, 1], (118, 118))], orient=False, out_grid=16)
        self.assertEqual(len(out["layers"]["me"][0]) // 2, 2)

    def test_minutes_average_over_games_still_running(self):
        long_game = tele(180, [T.cell_of(10, 10), 6], (10, 10))
        short_game = tele(60, [T.cell_of(10, 10), 6], (10, 10))
        out = heatmaps.aggregate([long_game, short_game], out_grid=16)
        self.assertEqual(out["games_at_minute"][0], 2)
        self.assertEqual(out["games_at_minute"][2], 1)               # only the long game reaches minute 2
        self.assertAlmostEqual(out["layers"]["me"][2][1], 6.0, places=3)

    def test_empty(self):
        out = heatmaps.aggregate([])
        self.assertEqual(out["games"], 0)
        self.assertEqual(out["layers"]["me"], [])


class Database(unittest.TestCase):
    def setUp(self):
        import glob
        import os
        for path in glob.glob(db.DB_PATH + "*"):          # the db plus its -wal/-shm files
            os.remove(path)

    def conn(self):
        c = db.get_conn()
        self.addCleanup(c.close)                          # Windows won't delete an open file
        return c

    def test_migrates_a_database_from_the_earlier_schema(self):
        legacy = sqlite3.connect(db.DB_PATH)
        legacy.execute("CREATE TABLE games (id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT UNIQUE NOT NULL, map TEXT, "
                       "my_race TEXT, opp_race TEXT, win INTEGER, first_supply_time INTEGER, first_gas_time INTEGER, "
                       "second_base_time INTEGER, third_base_time INTEGER, supply_block_count INTEGER, apm_combat REAL)")
        legacy.execute("INSERT INTO games (file_name, map, win) VALUES ('old.SC2Replay', 'X', 1)")
        legacy.commit()
        legacy.close()
        db.init_db()
        c = self.conn()
        cols = {r["name"] for r in c.execute("PRAGMA table_info(games)")}
        self.assertTrue({"build_order_json", "played_at", "parser_version", "float_penalty", "file_hash"} <= cols)
        self.assertEqual(c.execute("SELECT file_name FROM games").fetchone()[0], "old.SC2Replay")   # data survived
        self.assertEqual(db.stale_counts(c)["games"], 1)                                            # and is flagged stale

    def test_upsert_refreshes_in_place_and_clears_staleness(self):
        db.init_db()
        c = self.conn()
        record = {"map": "M", "my_race": "Protoss", "opp_race": "Zerg", "win": True, "played_at": "2026-07-01",
                  "first_supply_time": 20, "build_order": [{"time": 20, "kind": "structure", "name": "Pylon"}],
                  "telemetry": {"v": 1, "me": []}}
        db.upsert_game(c, record, "a.SC2Replay", source_path="/x/a.SC2Replay", file_hash="h1")
        record["first_supply_time"] = 25
        db.upsert_game(c, record, "a.SC2Replay", file_hash="h1")
        c.commit()
        rows = c.execute("SELECT * FROM games").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["first_supply_time"], 25)
        self.assertEqual(rows[0]["parser_version"], db.PARSER_VERSION)
        self.assertEqual(db.inflate(rows[0])["build_order"][0]["name"], "Pylon")
        self.assertEqual(db.load_telemetry(c, "mine", "a.SC2Replay"), {"v": 1, "me": []})
        self.assertEqual(db.stale_counts(c)["games"], 0)

    def test_pro_rows_are_keyed_by_perspective_and_delete_cascades(self):
        db.init_db()
        c = self.conn()
        rec = {"map": "M", "my_race": "Terran", "opp_race": "Zerg", "win": False, "telemetry": {"v": 1}}
        db.upsert_pro_game(c, rec, "p.SC2Replay", 0)
        db.upsert_pro_game(c, {**rec, "win": True}, "p.SC2Replay", 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pro_games").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0], 2)
        db.delete_game(c, "pro", "p.SC2Replay")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pro_games").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0], 0)


class Matchups(unittest.TestCase):
    def rec(self, win, **kw):
        base = {"my_race": "Protoss", "opp_race": "Zerg", "win": win, "map": "M", "file": "f", "build_order": [],
                "first_supply_time": 30, "first_gas_time": 60, "second_base_time": 300, "third_base_time": None,
                "supply_block_count": 0}
        return {**base, **kw}

    def test_flags_a_loss_that_floated_and_lost_retention(self):
        wins = [self.rec(True, float_penalty=100, apm_retention=0.9, macro_retention=0.8) for _ in range(3)]
        loss = self.rec(False, file="bad", float_penalty=900, apm_retention=0.5, macro_retention=0.78)
        m = analyzer.compute_matchup_data(wins + [loss])[0]
        notes = {n["label"] for f in m["flagged"] for n in f["notes"]}
        self.assertIn("Floating minerals", notes)
        self.assertIn("APM retention in fights", notes)
        self.assertNotIn("Macro retention in fights", notes)         # only 2 points lower, under threshold
        self.assertEqual(m["telemetry"]["float_penalty"]["win"], 100)

    def test_ai_and_coop_games_are_not_ladder(self):
        human = SimpleNamespace(name="A", play_race="Zerg", is_human=True)
        ai = SimpleNamespace(name="A.I. 1 (Very Easy)", play_race="Zerg", is_human=False)
        coop = SimpleNamespace(name="Amon's Forces", play_race="Zerg", is_human=True)
        odd = SimpleNamespace(name="B", play_race="Kerrigan", is_human=True)
        me = SimpleNamespace(name="Me", play_race="Terran", is_human=True)
        self.assertTrue(analyzer._is_ladder_game(me, human))
        for other in (ai, coop, odd):
            self.assertFalse(analyzer._is_ladder_game(me, other))


if __name__ == "__main__":
    unittest.main()
