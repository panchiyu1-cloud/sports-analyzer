import unittest
from datetime import datetime, timezone

from tests import conftest  # noqa: F401

from sports_analyzer.analysis import mlb as analysis
from sports_analyzer.config import ModelConfig
from sports_analyzer.fetch.mlb import MlbGame, parse_schedule
from sports_analyzer.fetch.odds import BookOdds, OddsEvent, SpreadLine, TotalLine


def _event(home_ml: float, away_ml: float, home_rl: float, away_rl: float) -> OddsEvent:
    books = (
        BookOdds("pinnacle", {"home": home_ml, "away": away_ml}, (SpreadLine(-1.5, home_rl, away_rl),), (TotalLine(7.5, 1.95, 1.87),)),
        BookOdds("betfair_ex_eu", {"home": home_ml + 0.02, "away": away_ml - 0.05}, (SpreadLine(-1.5, home_rl - 0.03, away_rl + 0.02),), (TotalLine(7.5, 1.9, 1.9),)),
    )
    return OddsEvent("e1", "baseball_mlb", datetime(2026, 10, 14, 0, 8, tzinfo=timezone.utc), "Los Angeles Dodgers", "Philadelphia Phillies", books)


class MlbCardTests(unittest.TestCase):
    def setUp(self):
        self.game = MlbGame(1, datetime(2026, 10, 14, 0, 8, tzinfo=timezone.utc), "Scheduled", "Los Angeles Dodgers", "Philadelphia Phillies", "Yoshinobu Yamamoto", "Zack Wheeler", "Dodger Stadium")
        self.cfg = ModelConfig()

    def test_parse_schedule_fixture_shape(self):
        payload = {"dates": [{"games": [{"gamePk": 7, "gameDate": "2026-10-14T00:08:00Z", "status": {"detailedState": "Scheduled"},
                                          "teams": {"home": {"team": {"name": "Los Angeles Dodgers"}, "probablePitcher": {"fullName": "Y. Yamamoto"}},
                                                    "away": {"team": {"name": "Philadelphia Phillies"}}},
                                          "venue": {"name": "Dodger Stadium"}}, {"gamePk": "bad"}]}]}
        games = parse_schedule(payload)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].home_probable, "Y. Yamamoto")
        self.assertIsNone(games[0].away_probable)

    def test_two_way_market_and_g_value(self):
        # Dodgers 1.60 / Phillies 2.45 ML; run line Dodgers -1.5 at 2.10, Phillies +1.5 at 1.75
        cards = analysis.build_cards([self.game], [_event(1.60, 2.45, 2.10, 1.75)], self.cfg)
        c = cards[0]
        self.assertEqual(c["home_key"], "los angeles dodgers")
        h2h = c["market"]["h2h"]
        self.assertNotIn("draw", h2h)
        self.assertAlmostEqual(h2h["home"]["fair"] + h2h["away"]["fair"], 1.0, places=3)
        self.assertEqual(c["market"]["spread"]["line"], -1.5)
        self.assertEqual(c["market"]["totals"]["line"], 7.5)
        t10 = c["t10"]
        assert t10 is not None
        self.assertEqual(t10["favourite"], "home")
        self.assertAlmostEqual(t10["g"], t10["p_ml"] + t10["c_cover"] - 1, places=6)
        self.assertIn(t10["signal"], (None, "FAV_ML", "DOG_PLUS_1_5"))
        self.assertGreater(h2h["home"]["fair_odds"], 1.0)

    def test_strong_favourite_triggers_fav_ml(self):
        # Heavy favourite whose run line is also likely: G well above +6pp
        cards = analysis.build_cards([self.game], [_event(1.30, 3.80, 1.55, 2.50)], self.cfg)
        self.assertEqual(cards[0]["t10"]["signal"], "FAV_ML")

    def test_no_event_gives_note(self):
        cards = analysis.build_cards([self.game], [], self.cfg)
        self.assertIsNone(cards[0]["market"])
        self.assertIsNone(cards[0]["t10"])
        self.assertTrue(cards[0]["notes"])


if __name__ == "__main__":
    unittest.main()
