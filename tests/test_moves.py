import tempfile
import unittest
from pathlib import Path

from sports_analyzer import moves
from sports_analyzer.config import ModelConfig
from sports_analyzer.models.market import devig
from tests import conftest  # noqa: F401


def dodgers(intl_home: float, odds_at: str, tw_home: float, tw_away: float, captured: str) -> dict:
    """Brewers @ Dodgers as it looked at one moment: consensus and 運彩 prices."""
    return {
        "game_pk": 813024,
        "kickoff_utc": "2026-10-15T00:00:00+00:00",
        "home": "Los Angeles Dodgers",
        "away": "Milwaukee Brewers",
        "market": {
            "books": 23,
            "h2h": {
                "home": {"fair": intl_home},
                "away": {"fair": round(1 - intl_home, 4)},
                "odds_at": odds_at,
            },
            "totals": {"line": 7.5, "over": {"fair": 0.49}, "under": {"fair": 0.51}, "stale": True},
            "spread": None,
        },
        "tw": {
            "captured_at": captured,
            "h2h": {"home": {"price": tw_home}, "away": {"price": tw_away}},
        },
    }


class TapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = ModelConfig()
        # 08:00 upload vs the 08:50 consensus, 20:00 upload vs the 20:53 consensus (2026-10-14).
        self.morning = dodgers(
            0.6203, "2026-10-14T00:50+00:00", 1.44, 2.19, "2026-10-14T08:00+08:00"
        )
        self.evening = dodgers(
            0.6213, "2026-10-14T12:53+00:00", 1.40, 2.31, "2026-10-14T20:00+08:00"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def tape(self):
        for card in (self.morning, self.evening):
            moves.record(self.dir, moves.intl_rows([card], "mlb"))
            moves.record(self.dir, moves.tw_rows([card], "mlb"))
        return moves.read(self.dir)

    def test_rows_skip_stale_blocks_and_keep_tw_probabilities(self):
        intl = moves.intl_rows([self.morning], "mlb")
        self.assertEqual(
            [(r["market"], r["side"], r["prob"]) for r in intl],
            [("h2h", "home", "0.6203"), ("h2h", "away", "0.3797")],
        )
        tw = moves.tw_rows([self.morning], "mlb")
        self.assertEqual(tw[0]["observed_at"], "2026-10-14T00:00+00:00")  # 08:00 Taipei
        self.assertEqual(tw[0]["prob"], f"{devig([1.44, 2.19], 'power')[0]:.4f}")  # 0.6298
        self.assertEqual(tw[0]["price"], "1.44")

    def test_record_keeps_each_observation_once(self):
        rows = moves.intl_rows([self.morning], "mlb")
        self.assertEqual(moves.record(self.dir, rows), 2)
        self.assertEqual(moves.record(self.dir, rows), 0)  # the same upload compared again
        self.assertTrue((self.dir / "2026-10.csv").exists())

    def test_track_and_the_dodgers_divergence(self):
        rows = self.tape()
        t = moves.track(rows, self.cfg)[("mlb", "813024")]
        home = next(e for e in t["sides"] if e["side"] == "home")
        self.assertEqual((home["first"], home["last"]), (0.6203, 0.6213))
        self.assertFalse(home["big"])  # +0.1pp is no 大單
        self.assertEqual((home["tw_first_price"], home["tw_last_price"]), (1.44, 1.4))
        # 運彩's own probability for the Dodgers: 1.44/2.19 -> 0.6298, 1.40/2.31 -> 0.6533
        # (power de-vig, k = 1.265), so 運彩 moved +2.35pp while the market moved +0.10pp.
        self.assertAlmostEqual(home["tw_move"], 0.0235, places=3)
        self.assertIsNone(home["follow"])  # the market barely moved: no ratio to speak of
        events = moves.tw_divergences(rows, self.cfg)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e["side"], e["market"]), ("home", "h2h"))
        self.assertAlmostEqual(e["divergence"], 0.0225, places=3)
        self.assertEqual((e["expected"], e["tw_price"]), (0.6213, 1.4))

    def test_no_event_without_a_market_price_near_both_uploads(self):
        self.evening["market"]["h2h"]["odds_at"] = "2026-10-14T20:00+00:00"  # 8h after the upload
        self.assertEqual(moves.tw_divergences(self.tape(), self.cfg), [])

    def test_big_move_and_follow_ratio(self):
        (
            self.morning["market"]["h2h"]["home"]["fair"],
            self.morning["market"]["h2h"]["away"]["fair"],
        ) = 0.52, 0.48
        rows = self.tape()  # the market went 52% -> 62.1%, 運彩 only 62.98% -> 65.33%
        home = next(
            e
            for e in moves.track(rows, self.cfg)[("mlb", "813024")]["sides"]
            if e["side"] == "home"
        )
        self.assertTrue(home["big"])
        self.assertAlmostEqual(home["move"], 0.1013, places=4)
        self.assertAlmostEqual(home["follow"], round(home["tw_move"] / home["intl_span_move"], 2))
        self.assertLess(home["follow"], 0.3)

    def test_settlement_and_summary(self):
        self.assertEqual(moves.outcome("h2h", "home", "", 3, 2), "win")
        self.assertEqual(moves.outcome("h2h", "draw", "", 1, 1), "win")
        self.assertEqual(moves.outcome("totals", "over", "7", 4, 3), "push")
        self.assertEqual(moves.outcome("totals", "under", "7.5", 4, 3), "win")
        self.assertEqual(
            moves.outcome("spread", "away", "-1.5", 5, 4), "win"
        )  # +1.5 dog lost by one
        events = moves.settle(
            moves.tw_divergences(self.tape(), self.cfg), {("mlb", "813024"): (5, 2)}
        )
        self.assertEqual((events[0]["result"], events[0]["score"]), ("win", "5-2"))
        s = moves.divergence_summary(events + [{**events[0], "result": "loss", "expected": 0.5}])
        # hits 1 vs expected 0.6213 + 0.5 = 1.12; z = (1 - 1.1213) / sqrt(0.2353 + 0.25)
        self.assertEqual((s["settled"], s["hits"], s["expected"]), (2, 1, 1.1))
        self.assertAlmostEqual(s["z"], -0.17, places=2)


class NotifyTests(unittest.TestCase):
    row = {
        "sport": "mlb",
        "match_id": "813024",
        "competition": "MLB",
        "kickoff_utc": "2026-10-15T00:00:00+00:00",
        "home": "Los Angeles Dodgers",
        "away": "Milwaukee Brewers",
        "market": "讓分",
        "selection": "客受讓（密爾瓦基釀酒人） +1.5",
        "tw_price": 1.85,
        "fair_odds": 1.72,
        "ev": 0.0728,
    }

    def test_message(self):
        from sports_analyzer import notify

        move = {
            "sides": [{"side": "away", "big": True, "first": 0.30, "last": 0.39, "follow": 0.25}]
        }
        title, body = notify.value_bet_message(self.row, move)
        self.assertEqual(title, "運彩價值注 EV +7.3%")
        self.assertIn("MLB 10/15 08:00 開賽", body)
        self.assertIn("Milwaukee Brewers @ Los Angeles Dodgers", body)
        self.assertIn("讓分：客受讓（密爾瓦基釀酒人） +1.5 @ 1.85（公允 1.72）", body)
        self.assertIn("國際盤大單：客 30% → 39%，運彩跟了 25%", body)

    def test_send_needs_a_topic_and_reports_failures(self):
        from sports_analyzer import notify

        class Session:
            def __init__(self, ok=True):
                self.calls, self.ok = [], ok

            def post(self, url, json, timeout):
                self.calls.append((url, json))
                return type("R", (), {"ok": self.ok, "status_code": 200 if self.ok else 500})()

        s = Session()
        self.assertFalse(notify.send(None, "t", "m", session=s))
        self.assertEqual(s.calls, [])
        self.assertTrue(notify.send("topic-x", "標題", "內容", session=s))
        self.assertEqual(s.calls[0][0], "https://ntfy.sh")
        self.assertEqual((s.calls[0][1]["topic"], s.calls[0][1]["title"]), ("topic-x", "標題"))
        self.assertFalse(notify.send("topic-x", "t", "m", session=Session(ok=False)))
        self.assertEqual(notify.notify_value_bets("topic-x", [self.row], {}, session=s), 1)


if __name__ == "__main__":
    unittest.main()
