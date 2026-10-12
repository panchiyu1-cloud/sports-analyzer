import unittest

from tests import conftest  # noqa: F401  (adds src/ to sys.path for unittest)

from sports_analyzer.models import market


class DevigTests(unittest.TestCase):
    def test_overround_of_symmetric_two_way(self):
        # 1.91 / 1.91 is the classic -110/-110 line: 4.7% margin.
        self.assertAlmostEqual(market.overround([1.91, 1.91]), 0.0471, places=3)

    def test_multiplicative_sums_to_one_and_keeps_order(self):
        probs = market.devig([1.5, 4.5, 6.0], "multiplicative")
        self.assertAlmostEqual(sum(probs), 1.0, places=12)
        self.assertGreater(probs[0], probs[1])
        self.assertGreater(probs[1], probs[2])

    def test_power_sums_to_one(self):
        probs = market.devig([1.5, 4.5, 6.0], "power")
        self.assertAlmostEqual(sum(probs), 1.0, places=9)

    def test_power_moves_more_margin_onto_longshots(self):
        prices = [1.3, 5.0, 9.0]
        mult = market.devig(prices, "multiplicative")
        power = market.devig(prices, "power")
        # Favourite ends up with a higher fair probability under the power method.
        self.assertGreater(power[0], mult[0])
        self.assertLess(power[2], mult[2])

    def test_fair_prices_unchanged(self):
        # 2.0 / 2.0 already sums to 1: both methods must return 0.5 / 0.5.
        for method in ("multiplicative", "power"):
            self.assertEqual([round(p, 9) for p in market.devig([2.0, 2.0], method)], [0.5, 0.5])

    def test_rejects_invalid_prices(self):
        with self.assertRaises(ValueError):
            market.devig([1.0, 3.0])
        with self.assertRaises(ValueError):
            market.devig([2.0, 2.0], "nope")


class ValueTests(unittest.TestCase):
    def test_expected_value(self):
        self.assertAlmostEqual(market.expected_value(0.5, 2.2), 0.1)
        self.assertAlmostEqual(market.expected_value(0.4, 2.2), -0.12)

    def test_kelly_zero_without_edge(self):
        self.assertEqual(market.kelly_fraction(0.45, 2.0), 0.0)

    def test_kelly_quarter(self):
        # p=0.55 at 2.0: full Kelly = (0.55*1 - 0.45)/1 = 0.10; quarter = 0.025
        self.assertAlmostEqual(market.kelly_fraction(0.55, 2.0, 0.25), 0.025)

    def test_confidence_tiers(self):
        self.assertIsNone(market.confidence(0.03, 0.04, 0.07, 0.10))
        self.assertEqual(market.confidence(0.04, 0.04, 0.07, 0.10), "低")
        self.assertEqual(market.confidence(0.08, 0.04, 0.07, 0.10), "中")
        self.assertEqual(market.confidence(0.12, 0.04, 0.07, 0.10), "高")


class ConsensusTests(unittest.TestCase):
    def test_median_and_best_price(self):
        quotes = [
            ("bookA", [1.9, 3.5, 4.2]),
            ("bookB", [1.95, 3.4, 4.0]),
            ("bookC", [1.85, 3.6, 4.5]),
        ]
        c = market.consensus(("home", "draw", "away"), quotes)
        assert c is not None
        self.assertEqual(c.books_used, 3)
        self.assertAlmostEqual(sum(c.fair_probs), 1.0, places=9)
        self.assertEqual(c.price("home"), 1.95)
        self.assertEqual(c.book("home"), "bookB")
        self.assertEqual(c.price("away"), 4.5)
        self.assertEqual(c.book("away"), "bookC")

    def test_incomplete_quotes_are_skipped(self):
        quotes = [("bookA", [1.9, 3.5]), ("bookB", [1.95, 3.4, 4.0])]
        c = market.consensus(("home", "draw", "away"), quotes)
        assert c is not None
        self.assertEqual(c.books_used, 1)

    def test_none_when_nothing_usable(self):
        self.assertIsNone(market.consensus(("home", "away"), [("x", [1.0, 2.0])]))


class T10Tests(unittest.TestCase):
    def test_g_value_hand_checked(self):
        # Favourite ML 1.60 vs dog 2.45 -> p ~ 0.60; favourite -1.5 at 2.10 vs dog +1.5 at 1.75 -> c ~ 0.455
        p = market.devig([1.60, 2.45], "multiplicative")[0]
        c = market.devig([2.10, 1.75], "multiplicative")[0]
        g = market.g_value(p, c)
        self.assertAlmostEqual(p, 0.6049, places=3)
        self.assertAlmostEqual(c, 0.4545, places=3)
        self.assertAlmostEqual(g, 0.0595, places=3)
        self.assertIsNone(market.t10_signal(g, 0.06))  # just under the +6% line
        self.assertEqual(market.t10_signal(0.061), "FAV_ML")
        self.assertEqual(market.t10_signal(-0.07), "DOG_PLUS_1_5")


if __name__ == "__main__":
    unittest.main()
