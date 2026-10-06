import unittest
from types import SimpleNamespace

import telemetry as T


class FightClustering(unittest.TestCase):
    def test_groups_nearby_deaths_and_drops_skirmishes(self):
        deaths = [(100, 1, 5), (102, 2, 5), (105, 1, 5),          # a 3-death fight
                  (200, 1, 9), (203, 2, 9),                        # only 2 -> skirmish
                  (300, 1, 7), (301, 1, 7), (304, 2, 8), (309, 2, 7)]
        fights = T.cluster_fights(deaths, length=1000)
        self.assertEqual(len(fights), 2)
        self.assertEqual(fights[0]["count"], 3)
        self.assertEqual(fights[0]["by_pid"], {1: 2, 2: 1})
        self.assertEqual(fights[0]["start"], 100 - T.FIGHT_PAD)
        self.assertEqual(fights[1]["cell"], 7)                     # modal cell

    def test_gap_splits_fights(self):
        deaths = [(0, 1, 0), (1, 1, 0), (2, 1, 0), (50, 1, 0), (51, 1, 0), (52, 1, 0)]
        self.assertEqual(len(T.cluster_fights(deaths, length=100)), 2)

    def test_end_is_clamped_to_game_length(self):
        deaths = [(98, 1, 0), (99, 1, 0), (99.5, 2, 0)]
        self.assertEqual(T.cluster_fights(deaths, length=100)[0]["end"], 100)

    def test_overlapping_windows_merge(self):
        fights = [{"start": 0, "end": 12}, {"start": 10, "end": 20}, {"start": 40, "end": 50}]
        self.assertEqual(T.merged_windows(fights), [[0, 20], [40, 50]])


class ActionRates(unittest.TestCase):
    def test_split_rates(self):
        windows = [[60, 120]]                                      # one 60s fight in a 300s game
        actions = [(10, False)] * 30 + [(70, False)] * 90 + [(80, True)] * 30   # 150 in fight? no:
        # 30 calm, 120 in the fight (30 of them macro)
        r = T.split_rates(actions, windows, 300)
        self.assertEqual(r["apm_combat"], 120.0)                   # 120 actions / 1 min
        self.assertEqual(r["apm_macro"], round(30 / 4, 1))         # 30 actions / 4 calm minutes
        self.assertEqual(r["macro_rate_combat"], 30.0)
        self.assertEqual(r["apm_retention"], round(120.0 / 7.5, 3))
        self.assertEqual(r["apm_total"], 30.0)

    def test_too_little_time_in_a_phase_gives_none(self):
        r = T.split_rates([(1, False)], [[0, 10]], 300)             # fights last 10s < 20s minimum
        self.assertIsNone(r["apm_combat"])
        self.assertIsNone(r["apm_retention"])

    def test_no_fights_means_no_combat_rate(self):
        r = T.split_rates([(5, False)] * 50, [], 120)
        self.assertIsNone(r["apm_combat"])
        self.assertEqual(r["apm_macro"], 25.0)

    def test_macro_command_classification(self):
        ev = lambda name, build=False: SimpleNamespace(ability_name=name, ability=SimpleNamespace(is_build=build))
        for name in ("TrainSCV", "BuildSupplyDepot", "Effect_InjectLarva", "ChronoBoostEnergyCost", "ResearchStimpack", "WarpGateTrain"):
            self.assertTrue(T.is_macro_command(ev(name)), name)
        self.assertTrue(T.is_macro_command(ev("Weird", build=True)))      # is_build alone is enough
        for name in ("Attack", "RightClick", "SiegeMode", "Gather", "Stop"):
            self.assertFalse(T.is_macro_command(ev(name)), name)


class Floating(unittest.TestCase):
    def test_ignores_early_game_and_maxed_supply(self):
        samples = [(60, 3000, 20),        # before 4:00 -> ignored
                   (300, 500, 100),       # 100 over threshold
                   (320, 900, 120),       # 500 over, and "heavy"
                   (500, 5000, 200)]      # maxed -> ignored
        r = T.floating_stats(samples)
        self.assertEqual(r["float_penalty"], 300)                   # (100 + 500) / 2
        self.assertEqual(r["float_pct"], 50.0)
        self.assertEqual(r["mineral_floating_avg"], 700)

    def test_nothing_countable(self):
        self.assertIsNone(T.floating_stats([(10, 100, 12)])["float_penalty"])


class Geometry(unittest.TestCase):
    def test_cell_roundtrip_and_clamping(self):
        self.assertEqual(T.cell_of(0, 0), 0)
        self.assertEqual(T.cell_of(4, 0), 1)
        self.assertEqual(T.cell_of(0, 4), T.GRID)
        self.assertEqual(T.cell_of(9999, 9999), T.GRID * T.GRID - 1)

    def test_bounds_ignore_outliers_and_fall_back_when_degenerate(self):
        xs = list(range(40, 140)) * 3 + [250]
        b = T.trimmed_bounds(xs, xs)
        self.assertLess(b[2], 250)
        self.assertEqual(T.trimmed_bounds([], []), [0, 0, T.COORD_MAX, T.COORD_MAX])
        self.assertEqual(T.trimmed_bounds([50, 50], [50, 50]), [0, 0, T.COORD_MAX, T.COORD_MAX])

    def test_normalize_flips_to_a_common_corner(self):
        bounds = [0, 0, 128, 128]
        low = T.normalize_cell(T.cell_of(10, 10), bounds, flip=False, out_grid=16)
        high_flipped = T.normalize_cell(T.cell_of(118, 118), bounds, flip=True, out_grid=16)
        self.assertEqual(low, high_flipped)                         # point symmetry maps both to one cell
        self.assertTrue(T.needs_flip((118, 118), bounds))
        self.assertFalse(T.needs_flip((10, 10), bounds))
        self.assertFalse(T.needs_flip(None, bounds))


class Interpolation(unittest.TestCase):
    def test_linear_between_samples_and_held_at_the_ends(self):
        pts = [(0, 0, 0), (10, 100, 50)]
        out = T.interpolate_track(pts, [0, 5, 10, 20])
        self.assertEqual(out[0], (0, 0))
        self.assertEqual(out[1], (50, 25))
        self.assertEqual(out[2], (100, 50))
        self.assertEqual(out[3], (100, 50))                         # held after last sample

    def test_snapshots_cover_exactly_a_units_lifetime(self):
        # alive from t=0 to t=30 -> ticks 0,1,2,3 (every 10s); one unit, player 1
        tracks = [[1, [(0, 8, 8), (30, 8, 8)], 30]]
        snaps = T.build_snapshots(tracks, [1, 2], length=60)
        self.assertEqual([sum(c.values()) for c in snaps[1]], [1, 1, 1, 1, 0, 0, 0])
        self.assertEqual(sum(sum(c.values()) for c in snaps[2]), 0)

    def test_unit_born_late_starts_at_the_next_tick(self):
        snaps = T.build_snapshots([[1, [(15, 4, 4)], None]], [1], length=40)
        self.assertEqual([sum(c.values()) for c in snaps[1]], [0, 0, 1, 1, 1])


if __name__ == "__main__":
    unittest.main()
