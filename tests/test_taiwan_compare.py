import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sports_analyzer import tw_ledger
from sports_analyzer.analysis import taiwan as compare
from sports_analyzer.config import LOCAL_TZ, ModelConfig
from sports_analyzer.fetch import taiwan
from tests import conftest  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"


def football_card(**over):
    card = {
        "sport": "football",
        "competition": "PL",
        "competition_name": "英超",
        "match_id": 500001,
        "kickoff_utc": "2026-10-17T14:00:00+00:00",
        "kickoff_local": "10/17 (週六) 22:00",
        "home": "Arsenal FC",
        "away": "Chelsea FC",
        "home_key": "arsenal",
        "away_key": "chelsea",
        "market": {
            "books": 3,
            "h2h": {
                "home": {"fair": 0.60, "fair_odds": 1.67, "best": 1.80, "book": "x"},
                "draw": {"fair": 0.22, "fair_odds": 4.55, "best": 3.9, "book": "x"},
                "away": {"fair": 0.18, "fair_odds": 5.56, "best": 4.6, "book": "x"},
            },
            "totals": {
                "line": 2.5,
                "over": {"fair": 0.52, "fair_odds": 1.92, "best": 2.0, "book": "x"},
                "under": {"fair": 0.48, "fair_odds": 2.08, "best": 1.9, "book": "x"},
            },
            "spread": None,
        },
    }
    card.update(over)
    return card


class CompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.games = taiwan.parse_games(
            json.loads((FIXTURES / "tw_games.json").read_text(encoding="utf-8"))
        )
        cls.cfg = ModelConfig()

    def test_match_by_names_and_time(self):
        card = football_card()
        g = compare.match_game(card, self.games, compare.timedelta(minutes=45))
        assert g is not None
        self.assertEqual(g.home_zh, "兵工廠")

    def test_no_match_when_time_differs(self):
        card = football_card(kickoff_utc="2026-10-18T14:00:00+00:00")
        self.assertIsNone(compare.match_game(card, self.games, compare.timedelta(minutes=45)))

    def test_compare_flags_value(self):
        # 運彩: home 1.70 (fair 1.67 -> EV +2%), draw 3.60 (fair 4.55 -> negative), away 4.30 (fair 5.56 -> negative)
        # totals: over 1.80 (fair 1.92 -> negative), under 1.90 (fair 2.08 -> negative)
        card = football_card()
        tw = compare.compare(
            card, self.games[1], datetime(2026, 10, 17, 8, 0, tzinfo=LOCAL_TZ), self.cfg
        )
        self.assertAlmostEqual(tw["h2h"]["home"]["ev"], 0.60 * 1.70 - 1, places=4)
        self.assertLess(tw["h2h"]["draw"]["ev"], 0)
        self.assertEqual(tw["totals"]["line"], 2.5)
        self.assertEqual([v["selection_key"] for v in tw["values"]], ["home"])
        self.assertEqual(tw["values"][0]["selection"], "主勝")
        self.assertEqual(tw["captured_at"], "2026-10-17T08:00+08:00")

    def test_annotate_report(self):
        cards = [
            football_card(),
            football_card(
                match_id=2,
                kickoff_utc="2026-10-20T19:00:00+00:00",
                home="Everton FC",
                away="Fulham FC",
                home_key="everton",
                away_key="fulham",
            ),
        ]
        rep = compare.annotate(cards, self.games, None, self.cfg)
        self.assertEqual(rep["matched"], 1)
        self.assertEqual(rep["unmatched_cards"], ["Everton FC vs Fulham FC"])
        self.assertIsNotNone(cards[0]["tw"])
        self.assertIsNone(cards[1]["tw"])
        self.assertEqual(rep["tw_games"], 3)
        # 1/1.7 + 1/3.6 + 1/4.3 - 1 = 0.0986 (hand-checked)
        self.assertAlmostEqual(rep["avg_overround"], 0.0986, places=4)

    def test_totals_skipped_when_lines_differ(self):
        card = football_card()
        card["market"]["totals"]["line"] = 3.0
        tw = compare.compare(card, self.games[1], None, self.cfg)
        self.assertIsNone(tw["totals"])


class MergePreviousMarketsTests(unittest.TestCase):
    def test_evening_h2h_run_keeps_morning_totals_as_stale(self):
        from sports_analyzer.pipeline import _merge_previous_markets

        morning = {"date": "2026-10-17", "competitions": [{"cards": [football_card()]}]}
        evening_card = football_card()
        evening_card["market"] = {
            "books": 2,
            "h2h": {"home": {"fair": 0.62, "fair_odds": 1.61, "best": 1.7, "book": "y"}},
            "totals": None,
            "spread": None,
        }
        evening = {"date": "2026-10-17", "competitions": [{"cards": [evening_card]}]}
        _merge_previous_markets(evening, morning)
        self.assertEqual(evening_card["market"]["h2h"]["home"]["fair"], 0.62)  # fresh h2h kept
        self.assertTrue(evening_card["market"]["totals"]["stale"])
        self.assertEqual(evening_card["market"]["totals"]["line"], 2.5)
        # Stale blocks never feed CLV.
        self.assertIsNone(tw_ledger._fair_prob_for(evening_card["market"], "over", "2.5"))
        self.assertEqual(tw_ledger._fair_prob_for(evening_card["market"], "home", ""), 0.62)

    def test_different_date_is_not_merged(self):
        from sports_analyzer.pipeline import _merge_previous_markets

        card = football_card()
        card["market"]["totals"] = None
        snap = {"date": "2026-10-18", "competitions": [{"cards": [card]}]}
        _merge_previous_markets(
            snap, {"date": "2026-10-17", "competitions": [{"cards": [football_card()]}]}
        )
        self.assertIsNone(card["market"]["totals"])

    def test_mlb_evening_run_keeps_started_games_market_and_tw(self):
        from sports_analyzer.pipeline import _merge_previous_mlb

        h2h = {"home": {"fair": 0.55, "fair_odds": 1.82, "best": 1.9, "book": "y"}}
        morning = {
            "date": "2026-10-17",
            "games": [
                {
                    "game_pk": 1,
                    "market": {"books": 3, "h2h": h2h, "totals": None, "spread": None},
                    "t10": {"g": 0.07},
                    "tw": {"game_no": "103", "values": []},
                }
            ],
        }
        started = {"game_pk": 1, "market": None, "t10": None, "tw": None, "notes": ["x"]}
        fresh = {"game_pk": 2, "market": {"books": 3, "h2h": h2h}, "t10": None, "tw": None}
        evening = {"date": "2026-10-17", "games": [started, fresh]}
        _merge_previous_mlb(evening, morning)
        self.assertTrue(started["market"]["h2h"]["stale"])
        self.assertEqual(started["t10"], {"g": 0.07})
        self.assertEqual(started["tw"]["game_no"], "103")
        # The carried-over price is never a closing price.
        self.assertIsNone(tw_ledger._fair_prob_for(started["market"], "home", ""))
        self.assertEqual(tw_ledger._fair_prob_for(fresh["market"], "home", ""), 0.55)
        # A different date is left alone.
        other = {"game_pk": 1, "market": None, "tw": None}
        _merge_previous_mlb({"date": "2026-10-18", "games": [other]}, morning)
        self.assertIsNone(other["market"])


class TwLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tw_ledger.csv"
        self.card = football_card()
        self.card["tw"] = {
            "captured_at": "2026-10-17T08:00+08:00",
            "values": [
                {
                    "market": "1X2",
                    "selection_key": "home",
                    "selection": "主勝",
                    "line": None,
                    "tw_price": 1.70,
                    "fair_odds": 1.67,
                    "fair_prob": 0.60,
                    "ev": 0.02,
                },
            ],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_clv_settle_summary(self):
        self.assertEqual(tw_ledger.append_values(self.path, [self.card], "football"), 1)
        self.assertEqual(tw_ledger.append_values(self.path, [self.card], "football"), 0)
        # Later consensus says home is now 0.65: CLV = 0.65 * 1.70 - 1 = +10.5%
        later = football_card()
        later["market"]["h2h"]["home"]["fair"] = 0.65
        self.assertEqual(tw_ledger.update_clv(self.path, [later], "football"), 1)
        rows = tw_ledger.read(self.path)
        self.assertAlmostEqual(float(rows[0]["clv"]), 0.105, places=3)
        self.assertEqual(tw_ledger.settle(self.path, {"500001": (2, 1)}, "football"), 1)
        rows = tw_ledger.read(self.path)
        self.assertEqual(rows[0]["status"], "win")
        self.assertAlmostEqual(float(rows[0]["profit"]), 0.70)
        s = tw_ledger.summary(rows)
        self.assertEqual(s["wins"], 1)
        self.assertAlmostEqual(s["avg_clv"], 0.105, places=3)
        self.assertEqual(s["n_clv"], 1)
        # Other sport's rows are untouched by football settlement.
        self.assertEqual(tw_ledger.settle(self.path, {"500001": (0, 0)}, "mlb"), 0)


if __name__ == "__main__":
    unittest.main()
