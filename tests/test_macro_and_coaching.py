import unittest

import coaching
import telemetry as T


class MacroMechanics(unittest.TestCase):
    # Chrono / MULE: 50 energy at 0.7875/s -> one use every 63.49s, plus one on completion.

    def test_protoss_two_nexuses(self):
        # nexus 1 ready at 0: 1 + floor(600/63.49) = 10 ; nexus 2 ready at 300: 1 + floor(300/63.49) = 5
        r = T.mechanic_stats("Protoss", [(0, None), (300, None)], uses=[10] * 12)
        self.assertEqual(r["mech_possible"], 15)
        self.assertEqual(r["mech_uses"], 12)
        self.assertEqual(r["mech_efficiency"], 0.8)

    def test_uses_after_the_window_dont_count(self):
        r = T.mechanic_stats("Protoss", [(0, None)], uses=[10, 20, 700, 800], window=600)
        self.assertEqual(r["mech_uses"], 2)

    def test_a_base_that_died_stops_earning(self):
        # ready 0, destroyed at 120: 1 + floor(120/63.49) = 2
        self.assertEqual(T.mechanic_stats("Protoss", [(0, 120)], uses=[])["mech_possible"], 2)

    def test_a_base_finished_after_the_window_earns_nothing(self):
        self.assertEqual(T.mechanic_stats("Terran", [(0, None), (700, None)], uses=[], window=600)["mech_possible"], 10)

    def test_efficiency_is_capped_at_one(self):
        self.assertEqual(T.mechanic_stats("Terran", [(0, None)], uses=list(range(40)))["mech_efficiency"], 1.0)

    def test_zerg_inject_waits_for_pool_and_a_queen(self):
        # inject every 25/0.7875 = 31.75s; first possible at max(hatch, pool) + 36 queen build
        r = T.mechanic_stats("Zerg", [(0, None)], uses=[], pool_done=100)
        self.assertEqual(r["mech_possible"], 1 + int((600 - 136) // 31.746))      # 15
        self.assertEqual(r["mech_possible"], 15)

    def test_zerg_without_a_pool_has_no_baseline(self):
        r = T.mechanic_stats("Zerg", [(0, None)], uses=[50], pool_done=None)
        self.assertIsNone(r["mech_efficiency"])
        self.assertEqual(r["mech_possible"], 0)

    def test_game_shorter_than_window(self):
        # a 300s game only had 300s of energy: 1 + floor(300/63.49) = 5
        self.assertEqual(T.mechanic_stats("Protoss", [(0, None)], uses=[], window=300)["mech_possible"], 5)


class Workers(unittest.TestCase):
    SAMPLES = [(0, 0, 0, 12, 0, 0, 0), (100, 0, 0, 20, 0, 0, 0), (230, 0, 0, 28, 0, 0, 0), (250, 0, 0, 30, 0, 0, 0)]

    def test_uses_the_last_sample_at_or_before_the_mark(self):
        self.assertEqual(T.worker_at(self.SAMPLES, 240, length=500), 28)
        self.assertEqual(T.worker_at(self.SAMPLES, 250, length=500), 30)

    def test_none_if_the_game_ended_first(self):
        self.assertIsNone(T.worker_at(self.SAMPLES, 480, length=300))


def g(win, **kw):
    return {"win": win, **kw}


class FocusRanking(unittest.TestCase):
    def test_a_timing_that_separates_wins_from_losses_ranks_first(self):
        wins = [g(True, second_base_time=300 + d) for d in (-5, 0, 5, 0)]
        losses = [g(False, second_base_time=360 + d) for d in (-5, 0, 5, 0)]
        out = coaching.rank_focus(wins, losses, [], "Protoss")
        self.assertEqual(out[0]["key"], "second_base_time")
        self.assertIn("losses 6:00 vs wins 5:00", out[0]["evidence"])
        self.assertEqual(out[0]["confidence"], "ok")

    def test_tiny_gaps_are_ignored_however_consistent(self):
        # workers at 4:00 differ by one: below the 3-worker floor, so not a finding
        wins = [g(True, workers_4m=25) for _ in range(5)]
        losses = [g(False, workers_4m=24) for _ in range(5)]
        self.assertEqual(coaching.rank_focus(wins, losses, [], "Protoss"), [])

    def test_higher_is_better_metrics_flag_the_low_side(self):
        wins = [g(True, mech_efficiency=0.8 + d) for d in (-.02, 0, .02, 0)]
        losses = [g(False, mech_efficiency=0.4 + d) for d in (-.02, 0, .02, 0)]
        out = coaching.rank_focus(wins, losses, [], "Zerg")
        self.assertEqual(out[0]["key"], "mech_efficiency")
        self.assertEqual(out[0]["label"], "Inject uptime")
        # the opposite pattern (better in losses) must NOT be reported as a problem
        self.assertEqual(coaching.rank_focus(losses, wins, [], "Zerg"), [])

    def test_pro_comparison_alone_can_surface_a_gap(self):
        mine = [g(True, workers_6m=34 + d) for d in (-2, 0, 2)] + [g(False, workers_6m=34 + d) for d in (-2, 0, 2)]
        pros = [g(True, workers_6m=48 + d) for d in (-3, 0, 3, 0)]
        out = coaching.rank_focus(mine[:3], mine[3:], pros, "Terran")
        self.assertEqual(out[0]["key"], "workers_6m")
        self.assertIn("you 34 vs pros 48", out[0]["evidence"])

    def test_the_three_worker_checkpoints_are_one_finding(self):
        def game(win):
            return g(win, workers_4m=22, workers_6m=34, workers_8m=44)
        pros = [g(True, workers_4m=30 + d, workers_6m=48 + d, workers_8m=62 + d) for d in (-1, 0, 1, 0)]
        out = coaching.rank_focus([game(True)] * 3, [game(False)] * 3, pros, "Protoss")
        self.assertEqual(sum(1 for i in out if i["key"].startswith("workers")), 1)

    def test_confidence_drops_with_few_games(self):
        wins = [g(True, second_base_time=300 + d) for d in (0, 5)]
        losses = [g(False, second_base_time=380 + d) for d in (0, 5)]
        self.assertEqual(coaching.rank_focus(wins, losses, [], "Protoss")[0]["confidence"], "low")
        # only a pro comparison and a handful of games: also low
        pros = [g(True, second_base_time=280 + d) for d in (-5, 0, 5, 0)]
        few = coaching.rank_focus([g(True, second_base_time=400)] * 3, [g(False, second_base_time=410)], pros, "Protoss")
        self.assertEqual(few[0]["confidence"], "low")

    def test_more_games_outrank_the_same_gap_on_fewer(self):
        mk = lambda n: ([g(True, second_base_time=300 + (i % 3) * 5) for i in range(n)],
                        [g(False, second_base_time=360 + (i % 3) * 5) for i in range(n)])
        small = coaching.rank_focus(*mk(2), [], "Protoss")[0]["score"]
        large = coaching.rank_focus(*mk(10), [], "Protoss")[0]["score"]
        self.assertGreater(large, small)

    def test_needs_at_least_two_games_and_returns_at_most_three(self):
        self.assertEqual(coaching.rank_focus([g(True, second_base_time=300)], [], [], "Protoss"), [])
        bad = dict(second_base_time=600, third_base_time=900, supply_block_count=30, float_penalty=2000,
                   apm_retention=0.3, macro_retention=0.2, workers_6m=20, mech_efficiency=0.1)
        good = dict(second_base_time=300, third_base_time=500, supply_block_count=3, float_penalty=50,
                    apm_retention=1.0, macro_retention=0.9, workers_6m=45, mech_efficiency=0.8)
        out = coaching.rank_focus([g(True, **good)] * 4, [g(False, **bad)] * 4, [], "Terran")
        self.assertEqual(len(out), 3)
        self.assertEqual([i["score"] for i in out], sorted((i["score"] for i in out), reverse=True))

    def test_items_carry_what_the_ui_needs(self):
        wins = [g(True, float_penalty=50 + d) for d in (0, 10, 20)]
        losses = [g(False, float_penalty=900 + d) for d in (0, 10, 20)]
        item = coaching.rank_focus(wins, losses, [], "Protoss")[0]
        self.assertEqual(set(item), {"key", "label", "score", "confidence", "evidence", "sample", "tip"})
        self.assertEqual(item["sample"], "3 wins, 3 losses")
        self.assertTrue(item["tip"])


if __name__ == "__main__":
    unittest.main()
