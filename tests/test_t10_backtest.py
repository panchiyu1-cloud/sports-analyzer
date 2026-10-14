import unittest

from sports_analyzer.analysis.t10_backtest import (
    american_to_decimal,
    game_g,
    outcome,
    summarise,
    trusted_books,
)
from tests import conftest  # noqa: F401


class T10BacktestTests(unittest.TestCase):
    def test_american_odds(self):
        self.assertAlmostEqual(american_to_decimal(125), 2.25)
        self.assertAlmostEqual(american_to_decimal(-145), 1.689655, places=6)
        self.assertAlmostEqual(american_to_decimal("-165"), 1.606061, places=6)
        self.assertIsNone(american_to_decimal(""))
        self.assertIsNone(american_to_decimal(50))  # not a valid American price

    def test_g_hand_checked(self):
        # Diamondbacks @ Reds 2021-04-22, bet365 close: ML Reds -145 / D-backs +125,
        # run line Reds -1.5 +145 / D-backs +1.5 -165. Multiplicative de-vig:
        # p = 0.591837 / (0.591837 + 0.444444) = 0.571116
        # c = 0.408163 / (0.408163 + 0.622642) = 0.395966   -> G = p + c - 1 = -0.032918
        g = game_g(-145, 125, -1.5, 145, -165, method="multiplicative")
        assert g is not None
        self.assertEqual(g.favourite, "home")
        self.assertAlmostEqual(g.p, 0.571116, places=6)
        self.assertAlmostEqual(g.c, 0.395966, places=6)
        self.assertAlmostEqual(g.g, -0.032918, places=6)
        self.assertAlmostEqual(g.rl_dog, 1.606061, places=6)
        # The underdog listed at -1.5 (a reversed run line) is not a T-10 game.
        self.assertIsNone(game_g(-145, 125, 1.5, 145, -165))
        # Away favourite giving the runs: the spread is +1.5 from the home side.
        g = game_g(125, -145, 1.5, -165, 145, method="multiplicative")
        assert g is not None
        self.assertEqual(g.favourite, "away")
        self.assertAlmostEqual(g.g, -0.032918, places=6)

    def test_outcomes(self):
        lost = outcome(11, 14, "home")
        self.assertEqual((lost.fav_win, lost.fav_cover, lost.dog_cover), (False, False, True))
        by_one = outcome(5, 4, "home")
        self.assertEqual((by_one.fav_win, by_one.fav_cover, by_one.dog_cover), (True, False, True))
        by_two = outcome(4, 6, "away")
        self.assertEqual((by_two.fav_win, by_two.fav_cover, by_two.dog_cover), (True, True, False))

    def test_trusted_books_drops_in_play_closes(self):
        # Regression: ~3% of the dataset's closing lines are in-play prices that lean
        # toward the eventual winner. Five books agree near 0.60 (median 0.605); one
        # closed at 0.80 -> dropped.
        probs = {"a": 0.60, "b": 0.61, "c": 0.59, "d": 0.605, "e": 0.80}
        self.assertEqual(trusted_books(probs, anchor=0.58), {"a", "b", "c", "d"})
        # Every book sits 20pp from the opening consensus: the whole close is in-play.
        self.assertEqual(trusted_books({"a": 0.80, "b": 0.81, "c": 0.79}, anchor=0.60), set())
        # Two books cannot vouch for each other; three with one outlier leave two.
        self.assertEqual(trusted_books({"a": 0.60, "b": 0.61}), set())
        self.assertEqual(trusted_books({"a": 0.60, "b": 0.60, "c": 0.641}), set())
        self.assertEqual(trusted_books({"a": 0.60, "b": 0.60, "c": 0.639}), {"a", "b", "c"})

    def test_summarise_hand_checked(self):
        s = summarise(
            [{"hit": 1, "prob": 0.6, "price": 1.6}, {"hit": 0, "prob": 0.6, "price": 1.6}]
        )
        # z = (0.5 - 0.6) * 2 / sqrt(2 * 0.24) = -0.29; returns +0.6 and -1 -> ROI -0.2, se 0.8
        self.assertEqual((s["n"], s["hit"], s["fair"], s["gap_pp"]), (2, 0.5, 0.6, -10.0))
        self.assertEqual(s["z"], -0.29)
        self.assertEqual((s["roi"], s["roi_se"]), (-0.2, 0.8))
        self.assertEqual(summarise([])["n"], 0)


if __name__ == "__main__":
    unittest.main()
