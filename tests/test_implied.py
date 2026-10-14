import json
import math
import tempfile
import unittest
from pathlib import Path

from sports_analyzer import ledger, tw_ledger
from sports_analyzer.analysis import taiwan as compare
from sports_analyzer.config import ModelConfig
from sports_analyzer.fetch import taiwan
from sports_analyzer.models.implied import IDENTITY, Calibration, fit_implied
from tests import conftest  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"


class ImpliedModelTests(unittest.TestCase):
    def setUp(self):
        self.g = fit_implied(0.60, 0.22, 0.18, 0.52)

    def test_reproduces_the_market_it_was_fitted_to(self):
        h, d, a = self.g.p_1x2()
        self.assertAlmostEqual(h, 0.60, places=5)
        self.assertAlmostEqual(d, 0.22, places=5)
        self.assertAlmostEqual(a, 0.18, places=5)
        self.assertAlmostEqual(self.g.total_over(2.5), 0.52, places=5)
        self.assertLess(self.g.fit_error, 1e-5)
        # Fitted on another totals line, the same match lands on the same level.
        g3 = fit_implied(0.60, 0.22, 0.18, self.g.total_over(3.5), line=3.5)
        self.assertAlmostEqual(g3.total_over(2.5), 0.52, places=3)

    def test_one_goal_handicaps_contain_plain_1x2_outcomes(self):
        # 讓分 1:0 (home -1): "away" = draw or away win; 讓分 0:1 (home +1): "home" = home win or draw
        self.assertAlmostEqual(self.g.handicap3(-1)[2], 0.22 + 0.18, places=5)
        self.assertAlmostEqual(self.g.handicap3(1)[0], 0.60 + 0.22, places=5)
        self.assertAlmostEqual(sum(self.g.handicap3(-2)), 1.0, places=9)

    def test_quarter_and_whole_lines(self):
        # Whole line 2: P(over | no push); quarter 2.25 = half on 2.0 and half on 2.5.
        m = self.g.matrix
        n = m.shape[0]
        total = [[i + j for j in range(n)] for i in range(n)]
        p = {
            k: sum(m[i][j] for i in range(n) for j in range(n) if total[i][j] == k)
            for k in range(2 * n)
        }
        over2 = sum(v for k, v in p.items() if k > 2)
        under2 = sum(v for k, v in p.items() if k < 2)
        self.assertAlmostEqual(self.g.total_over(2.0), over2 / (over2 + under2), places=9)
        win = over2  # 3+ goals wins both halves
        lose = sum(v for k, v in p.items() if k < 2) + p[2] / 2  # 2 goals: push + loss
        self.assertAlmostEqual(self.g.total_over(2.25), win / (win + lose), places=9)

    def test_first_half_uses_the_measured_share(self):
        half = self.g.first_half()
        self.assertAlmostEqual(half.lam, self.g.lam * 0.44, places=9)
        self.assertAlmostEqual(sum(half.p_1x2()), 1.0, places=9)
        self.assertGreater(half.p_1x2()[1], self.g.p_1x2()[1])  # more half-time draws

    def test_without_totals_rho_is_fixed(self):
        g = fit_implied(0.45, 0.27, 0.28)
        self.assertEqual(g.rho, -0.05)
        self.assertAlmostEqual(g.p_1x2()[0], 0.45, places=5)


class CalibrationTests(unittest.TestCase):
    def test_logistic_correction_hand_checked(self):
        cal = Calibration({"btts": (0.1, 0.8)})
        # logit(0.5) = 0 -> z = 0.1 -> 1 / (1 + e^-0.1) = 0.524979
        self.assertAlmostEqual(cal.apply("btts", 0.5), 0.524979, places=6)
        # p = 0.8: logit = 1.386294 -> z = 0.1 + 0.8 * 1.386294 = 1.209035 -> 0.770128
        self.assertAlmostEqual(cal.apply("btts", 0.8), 0.770128, places=6)
        self.assertEqual(cal.apply("odd", 0.47), 0.47)  # unknown market: unchanged

    def test_three_way_is_renormalised(self):
        cal = Calibration({"ht_draw": (0.2, 1.0)})
        h, d, a = cal.apply3("ht", (0.35, 0.40, 0.25))
        self.assertAlmostEqual(h + d + a, 1.0, places=9)
        self.assertGreater(d, 0.40)

    def test_reads_the_file_the_validation_writes(self):
        cal = Calibration.from_json(json.dumps({"params": {"odd": [0.03, 0.3]}}))
        self.assertAlmostEqual(cal.apply("odd", 0.5), 1 / (1 + math.exp(-0.03)), places=9)
        real = Path(__file__).resolve().parents[1] / "data" / "implied_calibration.json"
        if real.exists():
            loaded = Calibration.from_json(real.read_text(encoding="utf-8"))
            self.assertIn("btts", loaded.params)


class DerivedCompareTests(unittest.TestCase):
    def setUp(self):
        self.games = taiwan.parse_games(
            json.loads((FIXTURES / "tw_games.json").read_text(encoding="utf-8"))
        )
        self.game = self.games[1]
        self.mv = {
            "h2h": {
                "home": {"fair": 0.60, "fair_odds": 1.67},
                "draw": {"fair": 0.22, "fair_odds": 4.55},
                "away": {"fair": 0.18, "fair_odds": 5.56},
            },
            "totals": {
                "line": 2.5,
                "over": {"fair": 0.52, "fair_odds": 1.92},
                "under": {"fair": 0.48, "fair_odds": 2.08},
            },
        }

    def test_every_supported_market_is_priced(self):
        values: list = []
        rows = compare.derived_compare(self.mv, self.game, ModelConfig(), IDENTITY, values)
        keys = {r["selection_key"] for r in rows}
        for key in (
            "btts_yes",
            "btts_no",
            "odd",
            "even",
            "team_home_over",
            "team_home_under",
            "hcp3_home",
            "hcp3_draw",
            "hcp3_away",
            "ht_home",
            "ht_draw",
            "ht_away",
            "ht_over",
            "ht_under",
        ):
            self.assertIn(key, keys)
        self.assertNotIn("over", keys)  # the market's own 2.5 line is compared directly
        btts = next(r for r in rows if r["selection_key"] == "btts_yes")
        g = fit_implied(0.60, 0.22, 0.18, 0.52)
        self.assertAlmostEqual(btts["fair_prob"], round(g.both_score(), 4), places=4)
        self.assertAlmostEqual(btts["ev"], round(g.both_score() * 1.70 - 1, 4), places=4)
        self.assertEqual(rows, sorted(rows, key=lambda r: -r["ev"]))
        # Only 讓分 1:0 客 clears the +4% bar in this fixture (0.40 x 2.70 - 1 = +8%).
        self.assertEqual([v["selection_key"] for v in values], ["hcp3_away"])

    def test_unvalidated_lines_are_not_priced(self):
        g = fit_implied(0.60, 0.22, 0.18, 0.52)
        hcp = self.game.market("hcp3")
        far = taiwan.TwMarket("讓分 0:3", "hcp3", "14", hcp.selections, None, line=3.0)
        self.assertIsNone(compare.derived_probs(far, self.game, g, IDENTITY))
        self.assertIsNotNone(compare.derived_probs(hcp, self.game, g, IDENTITY))

    def test_team_total_maps_to_the_right_team(self):
        g = fit_implied(0.60, 0.22, 0.18, 0.52)
        team = self.game.market("team_totals")
        priced = compare.derived_probs(team, self.game, g, IDENTITY)
        self.assertEqual(priced[0][0], "team_home_over")  # 兵工廠 is the home side
        self.assertAlmostEqual(priced[0][3], g.team_over("home", 1.5), places=9)

    def test_clv_for_a_derived_bet_needs_a_fresh_1x2(self):
        g = fit_implied(0.60, 0.22, 0.18, 0.52)
        p = tw_ledger._fair_prob_for(self.mv, "btts_yes", "", IDENTITY)
        self.assertAlmostEqual(p, g.both_score(), places=9)
        stale = {**self.mv, "h2h": {**self.mv["h2h"], "stale": True}}
        self.assertIsNone(tw_ledger._fair_prob_for(stale, "btts_yes", "", IDENTITY))
        stale_totals = {**self.mv, "totals": {**self.mv["totals"], "stale": True}}
        self.assertIsNone(tw_ledger._fair_prob_for(stale_totals, "over", "2.5", IDENTITY))
        self.assertIsNotNone(tw_ledger._fair_prob_for(stale_totals, "over", "3.5", IDENTITY))


class DerivedSettlementTests(unittest.TestCase):
    def test_grades(self):
        g = ledger.grade
        self.assertEqual(g("btts_yes", None, 2, 1), "win")
        self.assertEqual(g("btts_no", None, 2, 0), "win")
        self.assertEqual(g("odd", None, 2, 1), "win")
        self.assertEqual(g("even", None, 2, 1), "loss")
        self.assertEqual(g("team_home_over", 1.5, 2, 0), "win")
        self.assertEqual(g("team_away_under", 0.5, 2, 1), "loss")
        self.assertEqual(g("team_home_over", 1.0, 1, 3), "push")
        self.assertEqual(g("hcp3_draw", -1.0, 2, 1), "win")  # won by exactly one
        self.assertEqual(g("hcp3_home", -1.0, 2, 1), "loss")
        self.assertEqual(g("hcp3_away", 1.0, 0, 2), "win")  # lost by two with a goal start
        self.assertEqual(g("ht_draw", None, 2, 1, (0, 0)), "win")
        self.assertEqual(g("ht_over", 1.5, 2, 1, (1, 1)), "win")
        self.assertEqual(g("ht_home", None, 2, 1), "")  # no half-time score yet

    def test_settle_waits_for_the_half_time_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tw.csv"
            base = {c: "" for c in tw_ledger.COLUMNS}
            rows = [
                {
                    **base,
                    "id": "a",
                    "sport": "football",
                    "match_id": "7",
                    "selection_key": "ht_draw",
                    "tw_price": "2.1",
                    "status": "pending",
                },
                {
                    **base,
                    "id": "b",
                    "sport": "football",
                    "match_id": "7",
                    "selection_key": "btts_yes",
                    "tw_price": "1.7",
                    "status": "pending",
                },
            ]
            tw_ledger.write(path, rows)
            self.assertEqual(tw_ledger.settle(path, {"7": (2, 1, None, None)}, "football"), 1)
            back = {r["id"]: r for r in tw_ledger.read(path)}
            self.assertEqual((back["a"]["status"], back["b"]["status"]), ("pending", "win"))
            self.assertEqual(tw_ledger.settle(path, {"7": (2, 1, 0, 0)}, "football"), 1)
            back = {r["id"]: r for r in tw_ledger.read(path)}
            self.assertEqual(back["a"]["status"], "win")
            self.assertAlmostEqual(float(back["a"]["profit"]), 1.1)


if __name__ == "__main__":
    unittest.main()
