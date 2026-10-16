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


class BigHandRecordTests(unittest.TestCase):
    """The permanent record of every 大戶 move (data/moves/events.csv, docs/bighand.html)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = ModelConfig()
        self.morning = dodgers(
            0.6203, "2026-10-14T00:50+00:00", 1.44, 2.19, "2026-10-14T08:00+08:00"
        )
        self.evening = dodgers(
            0.6213, "2026-10-14T12:53+00:00", 1.40, 2.31, "2026-10-14T20:00+08:00"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def tape(self, *cards):
        for card in cards or (self.morning, self.evening):
            moves.record(self.dir, moves.intl_rows([card], "mlb"))
            moves.record(self.dir, moves.tw_rows([card], "mlb"))
        return moves.read(self.dir)

    def test_a_midday_upload_without_a_market_price_does_not_break_the_chain(self):
        # A manual 14:00 upload: the nearest consensus is 5 hours away, so nothing can be
        # said about it; the 08:00 -> 20:00 comparison must still be made.
        noon = dodgers(0.62, "2026-10-14T00:50+00:00", 1.42, 2.25, "2026-10-14T14:00+08:00")
        self.tape()
        moves.record(self.dir, moves.tw_rows([noon], "mlb"))
        events = moves.tw_divergences(moves.read(self.dir), self.cfg)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            (events[0]["from_at"], events[0]["to_at"]),
            ("2026-10-14T00:00+00:00", "2026-10-14T12:00+00:00"),
        )
        self.assertEqual(events[0]["type"], "tw_only")
        self.assertAlmostEqual(events[0]["divergence"], 0.0225, places=3)

    def test_international_big_move_is_caught_once_per_side_before_kickoff(self):
        self.morning["market"]["h2h"]["home"]["fair"] = 0.52
        self.morning["market"]["h2h"]["away"]["fair"] = 0.48
        later = dodgers(0.66, "2026-10-14T14:00+00:00", 1.38, 2.40, "2026-10-14T22:00+08:00")
        after_kickoff = dodgers(0.40, "2026-10-15T01:00+00:00", 2.0, 1.8, "2026-10-15T09:00+08:00")
        rows = self.tape(self.morning, self.evening, later, after_kickoff)
        events = moves.intl_big_moves(rows, self.cfg)
        # Home 52% -> 62.13% at 20:53 Taipei (+10.1pp): one event, at the first crossing;
        # the 22:00 price (66%) is the same move, the in-game 40% is after first pitch.
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e["type"], e["side"], e["market"]), ("intl_big", "home", "h2h"))
        self.assertEqual(
            (e["from_at"], e["to_at"]), ("2026-10-14T00:50+00:00", "2026-10-14T12:53+00:00")
        )
        self.assertAlmostEqual(e["intl_move"], 0.1013, places=4)
        self.assertEqual(e["expected"], 0.6213)
        self.assertEqual(e["tw_price"], 1.4)  # 運彩 at 20:00, 53 minutes from the move

    def test_events_are_recorded_once_with_the_names_known_at_the_time(self):
        path = self.dir / "events.csv"
        events = moves.tw_divergences(self.tape(), self.cfg)
        names = {("mlb", "813024"): {"competition": "MLB", "home_zh": "道奇", "away_zh": "釀酒人"}}
        self.assertEqual(moves.record_events(path, events, names), 1)
        self.assertEqual(moves.record_events(path, events, names), 0)
        # The same upload pair seen later with another side on top is the same detection.
        self.assertEqual(moves.record_events(path, [{**events[0], "side": "away"}]), 0)
        saved = moves.read_events(path)
        self.assertEqual(len(saved), 1)
        self.assertEqual((saved[0]["home_zh"], saved[0]["competition"]), ("道奇", "MLB"))
        self.assertEqual((saved[0]["expected"], saved[0]["tw_price"]), ("0.6213", "1.4"))

    def test_a_record_with_older_columns_is_rewritten_not_misaligned(self):
        path = self.dir / "events.csv"
        old = ["id", "type", "to_at", "sport", "match_id", "market", "line", "side", "expected"]
        path.write_text(
            ",".join(old) + "\nabc,tw_only,2026-10-01T00:00+00:00,mlb,1,h2h,,home,0.5\n",
            encoding="utf-8",
        )
        self.assertEqual(moves.record_events(path, moves.tw_divergences(self.tape(), self.cfg)), 1)
        saved = moves.read_events(path)
        self.assertEqual(list(saved[0].keys()), moves.EVENT_COLUMNS)
        self.assertEqual([(r["id"], r["expected"]) for r in saved][0], ("abc", "0.5"))
        self.assertEqual(saved[1]["expected"], "0.6213")

    def test_g_from_the_moneyline_and_the_run_line_bought_together(self):
        def rows(at, home, line, cover_home):
            base = {"observed_at": at, "source": "intl", "sport": "mlb", "match_id": "1"}
            return [
                {**base, "market": "h2h", "side": "home", "line": "", "prob": str(home)},
                {**base, "market": "h2h", "side": "away", "line": "", "prob": str(1 - home)},
                {**base, "market": "spread", "side": "home", "line": line, "prob": str(cover_home)},
                {
                    **base,
                    "market": "spread",
                    "side": "away",
                    "line": line,
                    "prob": str(1 - cover_home),
                },
            ]

        tape = (
            rows("2026-10-14T00:50+00:00", 0.6213, "-1.5", 0.42)  # home fav gives 1.5
            + rows("2026-10-14T12:53+00:00", 0.40, "1.5", 0.55)  # away fav gives 1.5
            + rows("2026-10-14T13:30+00:00", 0.60, "1.5", 0.70)  # the underdog gives runs
            + rows("2026-10-15T01:00+00:00", 0.70, "-1.5", 0.50)  # after first pitch
        )
        g = moves.g_points(tape, before=moves.parse_time("2026-10-15T00:00+00:00"))
        # 0.6213 + 0.42 - 1 and 0.60 + (1 - 0.55) - 1
        self.assertEqual(g, {"2026-10-14T00:50+00:00": 0.0413, "2026-10-14T12:53+00:00": 0.05})

    def history(self, now="2026-10-15T05:00+00:00"):
        self.evening["market"]["spread"] = {
            "line": -1.5,
            "home": {"fair": 0.42},
            "away": {"fair": 0.58},
            "odds_at": "2026-10-14T12:53+00:00",
        }
        closing = dodgers(0.645, "2026-10-14T23:40+00:00", 1.36, 2.45, "2026-10-15T07:30+08:00")
        rows = self.tape(self.morning, self.evening, closing)
        path = self.dir / "events.csv"
        moves.record_events(path, moves.tw_divergences(rows, self.cfg))
        bet = {
            "sport": "mlb",
            "match_id": "813024",
            "selection_key": "home",
            "line": "",
            "captured_at": "2026-10-15T07:30+08:00",
            "market": "不讓分",
            "selection": "主（道奇）",
            "tw_price": "1.36",
            "ev": "-0.12",
            "status": "win",
        }
        return moves.event_history(
            moves.read_events(path),
            rows,
            {("mlb", "813024"): (5, 2)},
            [bet],
            now=moves.parse_time(now),
        )

    def test_history_joins_the_prices_up_to_kickoff_the_bets_and_the_result(self):
        history = self.history()
        self.assertEqual(len(history), 1)
        m = history[0]
        self.assertEqual((m["score"], m["closed"]), ("5-2", True))
        first = m["events"][0]
        self.assertEqual(
            (first["type"], first["side"], first["result"]), ("tw_only", "home", "win")
        )
        self.assertTrue(first["value_bet"])
        # Seen at 62.13% (20:53); the market closed at 64.5% before first pitch: +2.4pp.
        self.assertAlmostEqual(first["after"], 0.0237, places=4)
        self.assertEqual(first["close_tw_price"], 1.36)
        self.assertAlmostEqual(first["ev_at"], 0.6213 * 1.4 - 1, places=4)
        # The 07:30 upload followed the market (運彩 +2.5pp, market +2.4pp): no new event.
        self.assertEqual(len(m["events"]), 1)
        h2h = m["markets"][0]
        self.assertEqual(h2h["market"], "h2h")
        self.assertEqual(
            [p["at"] for p in h2h["points"]][:2],
            ["2026-10-14T00:00+00:00", "2026-10-14T00:50+00:00"],
        )
        self.assertEqual(h2h["points"][0]["tw"], {"home": 1.44, "away": 2.19})
        self.assertEqual(next(p["g"] for p in h2h["points"] if p.get("g") is not None), 0.0413)
        self.assertEqual(m["g_last"], 0.0413)
        s = moves.history_summary(history)["tw_only"]
        self.assertEqual(
            (s["matches"], s["followed"], s["reversed"], s["value_bets"]), (1, 1, 0, 1)
        )
        self.assertEqual((s["settled"], s["hits"]), (1, 1))

    def test_before_kickoff_the_move_since_is_not_counted_as_followed(self):
        s = moves.history_summary(self.history(now="2026-10-14T13:00+00:00"))["tw_only"]
        self.assertEqual((s["with_close"], s["followed"]), (0, 0))

    def test_history_page(self):
        from sports_analyzer.render.site import Site, render_page

        history = self.history()
        history[0]["home_zh"], history[0]["away_zh"] = "洛杉磯道奇", "密爾瓦基釀酒人"
        site = Site(
            now=moves.parse_time("2026-10-15T05:00+00:00"),
            football=None,
            mlb=None,
            tape=[],
            history=history,
            history_summary=moves.history_summary(history),
        )
        html = render_page(site, "moves")
        self.assertIn("<title>盤口移動｜比賽分析</title>", html)
        self.assertIn("密爾瓦基釀酒人 @ 洛杉磯道奇", html)
        self.assertIn('<span class="score">2–5</span>', html)  # away first, like the title
        self.assertIn("洛杉磯道奇 不讓分", html)
        self.assertIn("・運彩 1.40", html)
        self.assertIn("進了可下 1 注", html)
        self.assertIn("G +4.1（中性）", html)
        self.assertIn("20:00 ●", html)  # the upload the move was caught at
        self.assertIn("之後國際盤續往同一邊 +2.4pp：這個動向是對的方向。", html)
        self.assertIn('這一邊：<span class="pos">贏</span>', html)

    def test_history_page_without_records(self):
        from sports_analyzer.render.site import Site, render_page

        site = Site(
            now=moves.parse_time("2026-10-15T05:00+00:00"), football=None, mlb=None, tape=[]
        )
        self.assertIn("還沒有打完的紀錄", render_page(site, "moves"))


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
