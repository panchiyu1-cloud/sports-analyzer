import unittest
from datetime import UTC, datetime

from sports_analyzer.analysis import mlb as analysis
from sports_analyzer.config import ModelConfig
from sports_analyzer.fetch.mlb import MlbGame, parse_schedule
from sports_analyzer.fetch.odds import BookOdds, OddsEvent, SpreadLine, TotalLine
from tests import conftest  # noqa: F401


def _event(home_ml: float, away_ml: float, home_rl: float, away_rl: float) -> OddsEvent:
    books = (
        BookOdds(
            "pinnacle",
            {"home": home_ml, "away": away_ml},
            (SpreadLine(-1.5, home_rl, away_rl),),
            (TotalLine(7.5, 1.95, 1.87),),
        ),
        BookOdds(
            "betfair_ex_eu",
            {"home": home_ml + 0.02, "away": away_ml - 0.05},
            (SpreadLine(-1.5, home_rl - 0.03, away_rl + 0.02),),
            (TotalLine(7.5, 1.9, 1.9),),
        ),
    )
    return OddsEvent(
        "e1",
        "baseball_mlb",
        datetime(2026, 10, 14, 0, 8, tzinfo=UTC),
        "Los Angeles Dodgers",
        "Philadelphia Phillies",
        books,
    )


class MlbCardTests(unittest.TestCase):
    def setUp(self):
        self.game = MlbGame(
            1,
            datetime(2026, 10, 14, 0, 8, tzinfo=UTC),
            "Scheduled",
            "Los Angeles Dodgers",
            "Philadelphia Phillies",
            "Yoshinobu Yamamoto",
            "Zack Wheeler",
            "Dodger Stadium",
        )
        self.cfg = ModelConfig()

    def test_parse_schedule_fixture_shape(self):
        payload = {
            "dates": [
                {
                    "games": [
                        {
                            "gamePk": 7,
                            "gameDate": "2026-10-14T00:08:00Z",
                            "status": {"detailedState": "Scheduled"},
                            "teams": {
                                "home": {
                                    "team": {"name": "Los Angeles Dodgers"},
                                    "probablePitcher": {"fullName": "Y. Yamamoto"},
                                },
                                "away": {"team": {"name": "Philadelphia Phillies"}},
                            },
                            "venue": {"name": "Dodger Stadium"},
                        },
                        {"gamePk": "bad"},
                    ]
                }
            ]
        }
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
        self.assertIn(t10["shape"], analysis.G_SHAPES.values())
        self.assertNotIn("signal", t10)  # G no longer triggers a bet (2026-10-14 backtest)
        self.assertGreater(h2h["home"]["fair_odds"], 1.0)

    def test_strong_favourite_is_a_blowout_shape(self):
        # Heavy favourite whose run line is also likely: G well above +6pp
        cards = analysis.build_cards([self.game], [_event(1.30, 3.80, 1.55, 2.50)], self.cfg)
        self.assertEqual(cards[0]["t10"]["shape"], "強隊大勝型")

    def test_no_event_gives_note(self):
        cards = analysis.build_cards([self.game], [], self.cfg)
        self.assertIsNone(cards[0]["market"])
        self.assertIsNone(cards[0]["t10"])
        self.assertTrue(cards[0]["notes"])


if __name__ == "__main__":
    unittest.main()


def _dodgers_card(**over):
    """Brewers @ Dodgers, 2026-10-15: the international consensus of 23 books at 08:50
    and 運彩's 08:00 upload, as they were on the page (fair probabilities rounded)."""
    card = {
        "game_pk": 813024,
        "kickoff_utc": "2026-10-15T00:00:00+00:00",
        "home": "Los Angeles Dodgers",
        "away": "Milwaukee Brewers",
        "market": {
            "books": 23,
            "h2h": {
                "home": {"fair": 0.6203, "fair_odds": 1.61},
                "away": {"fair": 0.3797, "fair_odds": 2.63},
                "odds_at": "2026-10-14T00:50+00:00",
            },
            "spread": {
                "line": -1.5,
                "home": {"fair": 0.4201, "fair_odds": 2.38},
                "away": {"fair": 0.5799, "fair_odds": 1.72},
                "odds_at": "2026-10-14T00:50+00:00",
            },
            "totals": None,
        },
        "tw": {
            "home_zh": "洛杉磯道奇",
            "away_zh": "密爾瓦基釀酒人",
            "captured_at": "2026-10-14T08:00+08:00",
            "h2h": {"home": {"price": 1.44}, "away": {"price": 2.19}},
            "spread": {"line": -1.5, "home": {"price": 1.90}, "away": {"price": 1.60}},
        },
    }
    card.update(over)
    return card


class T10ValueTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ModelConfig()

    def test_hand_checked_dodgers_brewers(self):
        t = analysis.t10_value_block(_dodgers_card(), self.cfg)
        assert t is not None
        # International G = 0.6203 + 0.4201 - 1 = +4.04pp. 運彩's own prices, power de-vig:
        # p = 0.6298, c = 0.4462 -> G = +7.60pp; the gap is mostly the run line (+2.61pp).
        self.assertAlmostEqual(t["g_intl"], 0.0404, places=4)
        self.assertAlmostEqual(t["g_tw"], 0.0760, places=4)
        self.assertAlmostEqual(t["dc"], 0.0261, places=4)
        self.assertEqual(t["shape"], "中性")
        self.assertEqual(
            t["hints"],
            ["運彩把洛杉磯道奇贏 2 分以上算得比國際高 2.6pp → 密爾瓦基釀酒人 +1.5 相對划算"],
        )
        # EV = fair x 運彩 price - 1: 0.6203 x 1.44, 0.3797 x 2.19, 0.4201 x 1.90, 0.5799 x 1.60.
        evs = {b["key"]: b["ev"] for b in t["bets"]}
        self.assertEqual(
            evs, {"fav_ml": -0.1068, "dog_ml": -0.1685, "fav_rl": -0.2018, "dog_rl": -0.0722}
        )
        self.assertEqual(
            [b["label"] for b in t["bets"]][2:], ["洛杉磯道奇 -1.5", "密爾瓦基釀酒人 +1.5"]
        )
        self.assertEqual((t["status"], t["pick"], t["best"]["key"]), ("pass", None, "dog_rl"))
        # 08:00 upload vs the 08:50 consensus: judged, but a day before first pitch.
        self.assertEqual((t["minutes_before"], t["timing"]), (1440, "early"))

    def test_value_before_first_pitch(self):
        card = _dodgers_card()
        card["tw"]["captured_at"] = "2026-10-15T07:45+08:00"
        card["tw"]["spread"]["away"]["price"] = 1.85  # 0.5799 x 1.85 - 1 = +7.3%
        for key in ("h2h", "spread"):
            card["market"][key]["odds_at"] = "2026-10-14T23:46+00:00"
        t = analysis.t10_value_block(card, self.cfg)
        self.assertEqual(t["status"], "value")
        self.assertEqual(
            (t["pick"]["key"], t["pick"]["side"], t["pick"]["line"]), ("dog_rl", "away", 1.5)
        )
        self.assertAlmostEqual(t["pick"]["ev"], 0.0728, places=4)
        self.assertEqual((t["minutes_before"], t["timing"]), (15, "pregame"))

    def test_old_fair_price_is_not_judged(self):
        card = _dodgers_card()
        card["tw"]["captured_at"] = "2026-10-14T20:00+08:00"  # 11 hours after the consensus
        card["tw"]["spread"]["away"]["price"] = 1.85
        t = analysis.t10_value_block(card, self.cfg)
        self.assertEqual((t["status"], t["pick"]), ("stale", None))
        self.assertTrue(all(not b["fresh"] for b in t["bets"]))

    def test_away_favourite_labels_each_teams_own_handicap(self):
        card = _dodgers_card()
        card["market"]["h2h"]["home"]["fair"], card["market"]["h2h"]["away"]["fair"] = 0.40, 0.60
        card["market"]["spread"] = {
            "line": 1.5,  # home receives 1.5: the away favourite gives it
            "home": {"fair": 0.62},
            "away": {"fair": 0.38},
            "odds_at": "2026-10-14T00:50+00:00",
        }
        card["tw"]["spread"]["line"] = 1.5
        t = analysis.t10_value_block(card, self.cfg)
        self.assertEqual(t["favourite"], "away")
        labels = {b["key"]: b["label"] for b in t["bets"]}
        self.assertEqual(labels["fav_rl"], "密爾瓦基釀酒人 -1.5")
        self.assertEqual(labels["dog_rl"], "洛杉磯道奇 +1.5")
        self.assertAlmostEqual(t["g_intl"], 0.60 + 0.38 - 1, places=4)

    def test_no_block_without_tw_prices(self):
        self.assertIsNone(analysis.t10_value_block(_dodgers_card(tw=None), self.cfg))
