"""The site's view builders (render/site.py): charts, readings and history verdicts."""

import re
import unittest
from datetime import UTC, datetime

from sports_analyzer.config import ModelConfig
from sports_analyzer.render import site
from tests import conftest  # noqa: F401

CFG = ModelConfig()
NOW = datetime(2026, 10, 16, 0, 15, tzinfo=UTC)


def tape_row(at: str, source: str, market: str, side: str, prob: float, price=None, line=""):
    return {
        "observed_at": at,
        "source": source,
        "sport": "football",
        "match_id": "1",
        "kickoff_utc": "2026-10-17T14:00:00+00:00",
        "home": "Arsenal FC",
        "away": "Chelsea FC",
        "market": market,
        "side": side,
        "line": line,
        "prob": f"{prob:.4f}",
        "price": f"{price:g}" if price else "",
    }


def card(**kw) -> dict:
    base = {
        "match_id": 1,
        "kickoff_utc": "2026-10-17T14:00:00+00:00",
        "competition_name": "英超",
        "home": "Arsenal FC",
        "away": "Chelsea FC",
        "model": {"fitted": True, "p_home": 0.50, "p_draw": 0.25, "p_away": 0.25},
        "market": {
            "h2h": {
                "home": {"fair": 0.48, "fair_odds": 2.08},
                "draw": {"fair": 0.27, "fair_odds": 3.7},
                "away": {"fair": 0.25, "fair_odds": 4.0},
            }
        },
    }
    return {**base, **kw}


class EmptySiteTests(unittest.TestCase):
    def test_every_page_renders_before_any_data_exists(self):
        empty = site.Site(now=NOW, football=None, mlb=None, tape=[])
        for key, _, _ in site.PAGES:
            html = site.render_page(empty, key)
            self.assertIn("</html>", html, key)
            self.assertNotIn("{{", html, key)


class SparkTests(unittest.TestCase):
    def test_geometry_hand_checked(self):
        t0 = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
        t1 = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
        g = site.spark(
            [{"key": "intl", "label": "國際", "points": [(t0, 0.40, ""), (t1, 0.50, "")]}]
        )
        # 0.40..0.50, padded by 12% of the span: 0.388..0.512 over 78 units from y=10;
        # time over 240 units from x=34. y(0.40) = 10 + 0.112 / 0.124 * 78 = 80.45.
        self.assertEqual(g["lines"][0]["d"], "M34.0,80.5 L274.0,17.5")
        self.assertEqual(g["ends"], [{"x": 280.0, "y": 21.5, "text": "50%"}])
        # Steps of 5 points: 0.124 / 0.05 = 2.5 lines; y(0.45) = 10 + 0.062 / 0.124 * 78 = 49.
        self.assertEqual(
            [(t["y"], t["label"]) for t in g["ticks"]], [(80.5, "40"), (49.0, "45"), (17.5, "50")]
        )
        self.assertEqual(
            g["tips"][1], {"x": 274.0, "t": "10/16 10:00", "v": [["intl", "國際", "50%"]]}
        )
        self.assertEqual((g["start"], g["end"]), ("10/16 08:00", "10/16 10:00"))

    def test_g_axis_reads_in_whole_points(self):
        t0 = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
        t1 = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
        g = site.spark(
            [{"key": "g", "label": "G", "points": [(t0, -0.04, ""), (t1, 0.05, "")]}],
            refs=[{"value": 0.06, "label": "+6"}, {"value": -0.06, "label": "-6"}],
            kind="g",
            min_span=0.12,
        )
        # -0.06..+0.06 (the reference lines) padded by 12%: -0.0744..+0.0744, steps of 4
        # points; y(0) = 10 + 0.0744 / 0.1488 * 78 = 49.
        self.assertEqual(
            [(t["y"], t["label"]) for t in g["ticks"]], [(70.0, "-4"), (49.0, "0"), (28.0, "+4")]
        )
        self.assertEqual(g["ends"][0]["text"], "+5.0")

    def test_one_point_is_no_chart(self):
        t0 = datetime(2026, 10, 16, tzinfo=UTC)
        self.assertIsNone(site.spark([{"key": "g", "label": "G", "points": [(t0, 0.05, "")]}]))


class ReadingTests(unittest.TestCase):
    def test_value_reading_uses_the_nearest_fresh_international_price(self):
        rows = [
            tape_row("2026-10-16T12:00+00:00", "tw", "h2h", "home", 0.535, 1.80),
            tape_row("2026-10-16T12:00+00:00", "tw", "h2h", "away", 0.465, 2.10),
            tape_row("2026-10-16T12:30+00:00", "intl", "h2h", "home", 0.52),
            tape_row("2026-10-16T12:30+00:00", "intl", "h2h", "away", 0.48),
            tape_row("2026-10-16T21:40+00:00", "intl", "h2h", "home", 0.60),
        ]
        obs = site.observations(rows, "h2h", "", None)
        read = site.ev_reader("mlb", CFG, {"home": "道奇 不讓分", "away": "釀酒人 不讓分"})
        # 0.48 x 2.10 - 1 = +0.8%, below MLB's 3%: the closest, not a bet.
        self.assertEqual(read(obs, 0), ("最接近：釀酒人 不讓分 EV +0.8%", "neutral"))
        self.assertEqual(read(obs, 2), ("國際更新", "neutral"))
        rows_table = site.timeline_rows(obs, ["home", "away"], read)
        self.assertEqual(rows_table[-1]["tw"], ["1.80", "2.10"])  # newest first
        self.assertEqual(rows_table[0]["intl"], ["60", ""])


class ShapeCheckTests(unittest.TestCase):
    def check(self, g, home, away):
        row = {"t10_g": str(g), "t10_fav": "home", "final_h": str(home), "final_a": str(away)}
        return site.shape_check(row)

    def test_shapes(self):
        self.assertEqual(
            self.check(0.07, 5, 1),
            {"text": "G +7.0 說是強隊大勝型，實際熱門贏 2 分以上：型態說對了。", "tone": "good"},
        )
        self.assertEqual(self.check(-0.08, 4, 3)["tone"], "good")  # one-run game, as said
        self.assertEqual(
            self.check(-0.08, 6, 1)["text"],
            "G -8.0 說是一分差型，實際熱門贏 2 分以上：型態說錯了。",
        )
        self.assertEqual(self.check(0.03, 6, 1)["tone"], "neutral")
        self.assertIsNone(site.shape_check({"final_h": "3", "final_a": "1"}))


class PassedTests(unittest.TestCase):
    def test_passing_is_judged_at_the_closing_price(self):
        s = site.Site(now=NOW, football=None, mlb=None, tape=[])
        row = {"match_id": "1", "competition": "PL", "kickoff_utc": "2026-10-14T19:00:00+00:00",
               "home": "Arsenal FC", "away": "Chelsea FC", "ft_h": "2", "ft_a": "0",
               "tw_h": "2.10", "tw_d": "3.40", "tw_a": "3.60",
               "close_h": "0.45", "close_d": "0.28", "close_a": "0.27"}  # fmt: skip
        sides = [("主", "tw_h", "close_h", "H"), ("和", "tw_d", "close_d", "D"), ("客", "tw_a", "close_a", "A")]  # fmt: skip
        p = site._passed_row(row, "football", sides, "H", s)
        # Best at the close: away 0.27 x 3.60 - 1 = -2.8%, under 2%: passing was right.
        self.assertEqual((p["best"]["label"], round(p["best"]["ev"], 3)), ("客", -0.028))
        self.assertTrue(p["right"])
        self.assertEqual((p["competition"], p["score"]), ("英超", "2–0"))
        p = site._passed_row({**row, "tw_a": "3.90"}, "football", sides, "H", s)
        self.assertAlmostEqual(p["best"]["ev"], 0.053)  # 0.27 x 3.90 - 1: value at the close
        self.assertFalse(p["right"])


class ModelCardTests(unittest.TestCase):
    def test_a_totals_signal_charts_its_own_market_against_the_model(self):
        rows = [
            tape_row("2026-10-15T00:50+00:00", "intl", "totals", "over", 0.50, line="2.5"),
            tape_row("2026-10-15T00:50+00:00", "intl", "totals", "under", 0.50, line="2.5"),
            tape_row("2026-10-16T00:50+00:00", "intl", "totals", "over", 0.53, line="2.5"),
            tape_row("2026-10-16T00:50+00:00", "intl", "totals", "under", 0.47, line="2.5"),
        ]
        signal = {"market": "大小", "selection_key": "over", "line": 2.5, "model_p": 0.62,
                  "market_p": 0.50, "edge": 0.12, "confidence": "中"}  # fmt: skip
        s = site.Site(now=NOW, football=None, mlb=None, tape=rows)
        c = site.model_card(card(), rows, [signal], s)
        self.assertEqual(c["focus_label"], "大 2.5")
        self.assertEqual(c["chart"]["refs"][0]["label"], "模型")
        # Newest first: the market moved toward the model, 12pp -> 9pp apart.
        self.assertEqual(
            [(r["text"], r["tone"]) for r in c["timeline"]],
            [("模型比國際 +9.0pp", "neutral"), ("模型比國際 +12.0pp", "good")],
        )
        spread = {**signal, "market": "讓球", "selection_key": "spread_away", "line": -0.5}
        self.assertEqual(site.model_card(card(), [], [spread], s)["focus_label"], "Chelsea FC +0.5")

    def test_old_signals_obey_todays_rules(self):
        thin = card(model={"fitted": True, "home_matches": 8, "away_matches": 30})
        self.assertFalse(site.signal_allowed({"competition": "PL"}, thin, CFG))
        self.assertFalse(site.signal_allowed({"competition": "CL"}, card(), CFG))
        self.assertTrue(site.signal_allowed({"competition": "PL"}, card(), CFG))


class FrontPageTests(unittest.TestCase):
    def test_only_fades_that_pay_are_flagged(self):
        football = {
            "competitions": [{"code": "PL", "name": "英超", "cards": [card()], "signals": []}]
        }
        fade = {"sport_key": "football", "match_id": "1", "status": "pass",
                "strategies": ["home_dog"], "kickoff_utc": "2026-10-17T14:00:00+00:00"}  # fmt: skip
        quiet = site.build_pages(
            site.Site(now=NOW, football=football, mlb=None, tape=[], fade_live=[fade])
        )
        match = quiet["index"]["board"][0]["matches"][0]
        self.assertEqual(match["flags"], [])
        pages = site.build_pages(
            site.Site(now=NOW, football=football, mlb=None, tape=[],
                      fade_live=[{**fade, "status": "value"}])
        )  # fmt: skip
        flags = pages["index"]["board"][0]["matches"][0]["flags"]
        self.assertEqual(
            flags,
            [
                {
                    "page": "fade",
                    "href": "fade.html#m-football-1",
                    "label": "反指標可下",
                    "tone": "good",
                }
            ],
        )
        self.assertEqual(pages["index"]["headline"], "目前沒有可下的注")

    def test_when(self):
        self.assertEqual(
            site.when("2026-10-17T14:00:00+00:00", NOW),
            {"date": "10/17", "time": "22:00", "weekday": "週六", "eta": "還有 37 小時 45 分"},
        )


def mlb_game(pk: int, kickoff: str, **kw) -> dict:
    return {"game_pk": pk, "kickoff_utc": kickoff, "home": "Los Angeles Dodgers",
            "away": "Milwaukee Brewers", "market": {}, **kw}  # fmt: skip


class TodayTests(unittest.TestCase):
    """The day is Taipei's: 10/16 runs from 10/15 16:00 to 10/16 16:00 UTC."""

    NOW = datetime(2026, 10, 16, 5, 30, tzinfo=UTC)  # 13:30 Taipei

    def test_todays_started_matches_stay_on_the_board(self):
        reviewed = mlb_game(1, "2026-10-15T17:00:00+00:00")  # 10/16 01:00 Taipei
        playing = mlb_game(2, "2026-10-16T00:00:00+00:00")  # 10/16 08:00 Taipei
        yesterday = mlb_game(3, "2026-10-15T15:00:00+00:00")  # 10/15 23:00 Taipei
        later = mlb_game(4, "2026-10-16T22:00:00+00:00")  # 10/17 06:00 Taipei
        s = site.Site(
            now=self.NOW,
            football=None,
            mlb={"games": [later]},
            tape=[],
            mlb_reviews=[{"game_pk": "1", "final_a": "2", "final_h": "3"}],
            started=[reviewed, playing, yesterday],
        )
        pages = site.build_pages(s)
        board = pages["index"]["board"]
        self.assertEqual([d["day"] for d in board], ["10/16 週五", "10/17 週六"])
        today = board[0]["matches"]
        self.assertEqual([m["when"]["time"] for m in today], ["01:00", "08:00"])
        self.assertEqual((today[0]["score"], today[0]["href"]), ("2–3", "reviews.html#m-mlb-1"))
        self.assertEqual((today[1]["score"], today[1]["started"]), ("", True))
        self.assertEqual(pages["index"]["counts"]["matches"], 1)  # only the one to come
        html = site.render_page(s, "index")
        self.assertIn("2–3・看檢討", html)
        self.assertIn("已開賽・等賽果", html)
        # The one still playing waits for its review on the reviews page too.
        self.assertEqual([w["title"] for w in pages["reviews"]["waiting"]][-1],
                         "Milwaukee Brewers @ Los Angeles Dodgers")  # fmt: skip

    def test_start_of_day_is_midnight_in_taipei(self):
        self.assertEqual(
            site.start_of_day(self.NOW).astimezone(UTC),
            datetime(2026, 10, 15, 16, 0, tzinfo=UTC),
        )


class StampTests(unittest.TestCase):
    def test_report_stamps_in_taipei_time(self):
        from sports_analyzer.render.cards import month_label, taipei_stamp

        self.assertEqual(taipei_stamp("2026-10-14T07:43+00:00"), "2026/10/14 15:43")
        self.assertEqual(taipei_stamp("2026-10-15T16:30+00:00"), "2026/10/16 00:30")  # next day
        self.assertEqual(taipei_stamp("2026-10-14"), "2026/10/14")  # a bare date stays one
        self.assertEqual(taipei_stamp("2026-10-14T07:43"), "—")  # no zone: unknown
        self.assertEqual(taipei_stamp(None), "—")
        self.assertEqual(month_label("2025-08-16"), "2025 年 8 月")


class OrderTests(unittest.TestCase):
    """Cards follow their matches' kickoff (owner, 2026-10-16), whatever their status."""

    def test_cards_soonest_first_history_newest_first(self):
        rows = [
            {"kickoff_utc": "2026-10-18T13:00:00+00:00", "id": "c"},
            {"kickoff_utc": "2026-10-17T11:30:00+00:00", "id": "a"},
            {"kickoff_utc": None, "id": "unknown"},
            {"kickoff_utc": "2026-10-17T11:30:00+00:00", "id": "b"},  # same match time
        ]
        self.assertEqual([r["id"] for r in site.by_kickoff(rows)], ["a", "b", "c", "unknown"])
        self.assertEqual(
            [r["id"] for r in site.by_kickoff(rows, newest_first=True)],
            ["unknown", "c", "a", "b"],
        )

    def test_a_signal_no_longer_jumps_the_queue(self):
        later = card(match_id=2, kickoff_utc="2026-10-18T13:00:00+00:00")
        sooner = card(match_id=1, kickoff_utc="2026-10-17T11:30:00+00:00")
        signal = {"match_id": 2, "market": "1X2", "selection_key": "home", "model_p": 0.6,
                  "market_p": 0.48, "edge": 0.12}  # fmt: skip
        football = {"competitions": [{"code": "PL", "name": "英超", "cards": [later, sooner],
                                      "signals": [signal]}]}  # fmt: skip
        pages = site.build_pages(site.Site(now=NOW, football=football, mlb=None, tape=[]))
        self.assertEqual([c["id"] for c in pages["model"]["cards"]],
                         ["m-football-1", "m-football-2"])  # fmt: skip
        self.assertTrue(pages["model"]["cards"][1]["signal"])

    def test_both_radar_sides_of_a_match_share_one_anchor(self):
        cards = [{"id": "m-mlb-9", "kickoff_utc": "2026-10-17T01:00:00+00:00"},
                 {"id": "m-mlb-9", "kickoff_utc": "2026-10-17T01:00:00+00:00"}]  # fmt: skip
        self.assertEqual([c["id"] for c in site.unique_anchors(cards)], ["m-mlb-9", ""])


def fade_row(**kw) -> dict:
    base = {"sport_key": "football", "sport": "足球", "match_id": "1", "status": "pass",
            "strategies": ["home_dog"], "kickoff_utc": "2026-10-17T14:00:00+00:00",
            "match": "A vs B", "fade_label": "和局＋客勝", "tw_price": 1.2, "ev": -0.157,
            "threshold": 0.02, "fair_prob": 0.70, "market": "h2h", "line": "",
            "bet_sides": ["draw", "away"]}  # fmt: skip
    return {**base, **kw}


class FadeCurveTests(unittest.TestCase):
    """Every 反指標 card charts its opposite bet: fair probability vs what 運彩 pays."""

    TAPE = [
        tape_row("2026-10-15T00:30:00+00:00", "intl", "h2h", "home", 0.30),
        tape_row("2026-10-15T00:30:00+00:00", "intl", "h2h", "draw", 0.25),
        tape_row("2026-10-15T00:30:00+00:00", "intl", "h2h", "away", 0.45),
        tape_row("2026-10-15T01:00:00+00:00", "tw", "h2h", "draw", 0.24, 3.15),
        tape_row("2026-10-15T01:00:00+00:00", "tw", "h2h", "away", 0.47, 1.95),
    ]

    def test_the_two_lines_and_the_reading(self):
        obs = site.observations(self.TAPE, "h2h", "", None)
        lines = {s["key"]: s["points"] for s in site.fade_series(obs, ["draw", "away"])}
        self.assertAlmostEqual(lines["intl"][0][1], 0.70)  # 0.25 + 0.45
        # 1 / (1/3.15 + 1/1.95) = 1.2044: 運彩 asks for 1 / 1.2044 = 83.0%
        self.assertAlmostEqual(lines["tw"][0][1], 0.8303, places=4)
        self.assertEqual(lines["tw"][0][2], "1.20")
        read = site.fade_reader(["draw", "away"], 0.02, CFG)
        # the upload half an hour after the international price: 0.70 x 1.2044 - 1 = -15.7%
        self.assertEqual(read(obs, 1), ("反著押 EV -15.7%", "neutral"))
        self.assertEqual(read(obs, 0), ("國際更新", "neutral"))

    def test_the_card_carries_the_curve_or_says_why_not(self):
        s = site.Site(now=NOW, football=None, mlb=None, tape=self.TAPE, fade_live=[fade_row()])
        html = site.render_page(s, "fade")
        self.assertIn('class="spark"', html)
        self.assertIn("這一邊的機率：藍線高過橘線才有 EV", html)
        lone = site.Site(now=NOW, football=None, mlb=None, tape=self.TAPE[:3],
                         fade_live=[fade_row()])  # fmt: skip
        html = site.render_page(lone, "fade")
        self.assertNotIn('class="spark"', html)
        self.assertIn("目前只有 1 次報價（10/15 08:30），下一次更新後就會畫出變化", html)


def mlb_tape(at: str, market: str, side: str, prob: float, line: str = "") -> dict:
    return {**tape_row(at, "intl", market, side, prob, line=line), "sport": "mlb",
            "match_id": "7", "kickoff_utc": "2026-10-16T00:00:00+00:00"}  # fmt: skip


class HistoryCurveTests(unittest.TestCase):
    """Finished matches keep a curve too: their prices up to kickoff, on every history card."""

    def test_ledger_and_review_keys_map_to_the_tape(self):
        self.assertEqual(site.tape_focus("away", ""), ("h2h", "away", "", False))
        self.assertEqual(site.tape_focus("under", "2.5"), ("totals", "under", "2.5", False))
        # Spread lines are the home handicap in the ledgers and in the tape alike.
        self.assertEqual(site.tape_focus("spread_away", "-1.50"), ("spread", "away", "-1.5", False))
        # A derived bet has no price history of its own: its team's 1X2 price stands in.
        self.assertEqual(site.tape_focus("hcp3_away", "2"), ("h2h", "away", "", True))
        self.assertEqual(site.tape_focus("team_home_over", "1.5"), ("h2h", "home", "", True))
        self.assertEqual(site.tape_focus("btts_yes", ""), ("h2h", "home", "", True))

    def test_the_curve_stops_at_kickoff(self):
        rows = [
            tape_row("2026-10-17T08:00:00+00:00", "intl", "h2h", "home", 0.48),
            tape_row("2026-10-17T12:00:00+00:00", "tw", "h2h", "home", 0.47, 1.95),
            tape_row("2026-10-17T15:00:00+00:00", "intl", "h2h", "home", 0.70),  # in play
        ]
        kickoff = "2026-10-17T14:00:00+00:00"
        home = site.tape_focus("home", "")
        c = site.past_curve(rows, kickoff, "football", home, "兵工廠", "切爾西")
        self.assertEqual(c["seen"], 2)
        self.assertEqual(c["caption"], "主勝 兵工廠：開賽前的隱含機率")
        self.assertEqual(c["chart"]["end"], "10/17 20:00")  # 12:00 UTC, not the in-play price
        lone = site.past_curve(rows[:1], kickoff, "football", home, "兵工廠", "切爾西")
        self.assertIsNone(lone["chart"])
        self.assertEqual(lone["seen"], 1)

    def test_a_review_follows_its_lean_else_the_models_side(self):
        lean = {"lean_key": "spread_away", "lean_line": "-1.5", "lean_model_p": "0.6448"}
        self.assertEqual(site.review_focus(lean), (("spread", "away", "-1.5", False), 0.6448))
        row = {"model_ok": "1", "model_h": "0.36", "model_d": "0.27", "model_a": "0.37",
               "open_h": "0.45", "open_d": "0.26", "open_a": "0.29"}  # fmt: skip
        # Gaps: home -9pp, draw +1pp, away +8pp.
        self.assertEqual(site.review_focus(row), (("h2h", "away", "", False), 0.37))
        # A pooled cup model's numbers mean nothing: home, and no model line.
        self.assertEqual(site.review_focus({**row, "model_ok": "0"}),
                         (("h2h", "home", "", False), None))  # fmt: skip

    def test_mlb_history_charts_g_else_the_home_moneyline(self):
        row = {"game_pk": "7", "kickoff_utc": "2026-10-16T00:00:00+00:00",
               "home": "Los Angeles Dodgers", "away": "Milwaukee Brewers",
               "final_a": "2", "final_h": "3"}  # fmt: skip
        ml = [mlb_tape("2026-10-15T08:00:00+00:00", "h2h", "home", 0.60),
              mlb_tape("2026-10-15T08:00:00+00:00", "h2h", "away", 0.40),
              mlb_tape("2026-10-15T20:00:00+00:00", "h2h", "home", 0.62),
              mlb_tape("2026-10-15T20:00:00+00:00", "h2h", "away", 0.38)]  # fmt: skip
        rl = [mlb_tape("2026-10-15T08:00:00+00:00", "spread", "home", 0.45, "-1.5"),
              mlb_tape("2026-10-15T20:00:00+00:00", "spread", "home", 0.48, "-1.5")]  # fmt: skip
        g = site.mlb_past_curve(ml + rl, row, "道奇", "釀酒人", CFG)
        # G = 0.60 + 0.45 - 1 = +5.0, then 0.62 + 0.48 - 1 = +10.0
        self.assertEqual(g["caption"], "G 值（國際）：開賽前的變化")
        self.assertEqual([e["text"] for e in g["chart"]["ends"]], ["+10.0"])
        self.assertEqual([r["label"] for r in g["chart"]["refs"]], ["+6", "−6"])
        price = site.mlb_past_curve(ml, row, "道奇", "釀酒人", CFG)
        self.assertEqual(price["caption"], "道奇 不讓分：開賽前的隱含機率")
        self.assertIsNotNone(price["chart"])
        # On the page: the review card carries its curve, or says why there is none.
        s = site.Site(now=NOW, football=None, mlb=None, tape=ml, mlb_reviews=[row])
        html = site.render_page(s, "reviews")
        card = re.search(r'<article class="card review".*?</article>', html, flags=re.S).group(0)
        self.assertIn('class="spark"', card)
        self.assertIn("Los Angeles Dodgers 不讓分：開賽前的隱含機率", card)
        bare = site.render_page(site.Site(now=NOW, football=None, mlb=None, tape=ml[:2],
                                          mlb_reviews=[row]), "reviews")  # fmt: skip
        self.assertIn("開賽前只有 1 次報價，畫不成曲線", bare)


class LeagueTests(unittest.TestCase):
    """Every card names its league (owner, 2026-10-16: easier to tell matches apart)."""

    LEAGUES = ("英超", "西甲", "德甲", "義甲", "法甲", "歐冠", "MLB")

    def make_site(self) -> site.Site:
        pl = card(competition="PL", competition_name="英超")
        football = {"competitions": [{"code": "PL", "name": "英超", "cards": [pl], "signals": []}]}
        record = {"sport": "足球", "competition": "FL1", "match_id": "9", "strategy": "home_dog",
                  "kickoff_utc": "2026-10-15T19:00:00+00:00", "match": "C vs D",
                  "fade_label": "和局＋客勝 D", "fair_prob": 0.6, "tw_price": 1.3,
                  "result": "win", "score": "1-2", "units": 0.3}  # fmt: skip
        value = {"sport": "football", "competition": "PD", "match_id": "5",
                 "captured_at": "2026-10-14T20:00+08:00", "kickoff_utc": "2026-10-14T19:00:00+00:00",
                 "home": "Sevilla FC", "away": "Real Betis", "selection": "主勝",
                 "selection_key": "home", "line": "", "tw_price": "2.4", "fair_prob": "0.45",
                 "ev": "0.08", "status": "win"}  # fmt: skip
        mlb_review = {"game_pk": "1", "kickoff_utc": "2026-10-15T00:00:00+00:00",
                      "final_a": "2", "final_h": "3"}  # fmt: skip
        return site.Site(
            now=NOW,
            football=football,
            mlb={"games": [mlb_game(3, "2026-10-17T00:00:00+00:00")]},
            tape=[],
            mlb_reviews=[mlb_review],
            value_rows=[value],
            # One fade row carries its league, one only a match id the snapshots know.
            fade_live=[fade_row(competition="PL"), fade_row(match_id="2", competition="")],
            fade_record=[record],
            names={("football", "2"): {"competition": "德甲", "home_zh": "", "away_zh": ""}},
        )

    def test_every_card_on_every_page_names_its_league(self):
        s = self.make_site()
        for key, _, _ in site.PAGES:
            html = site.render_page(s, key)
            for art in re.findall(r"<article\b.*?</article>", html, flags=re.S):
                head = re.sub(r"<[^>]+>", " ", art)[:120]
                self.assertTrue(any(x in head for x in self.LEAGUES), f"{key}: {head}")

    def test_fade_cards_and_record_say_which_league(self):
        html = site.render_page(self.make_site(), "fade")
        self.assertIn('<span class="comp">英超</span>', html)
        self.assertIn('<span class="comp">德甲</span>', html)  # from the snapshots' names
        self.assertIn('<span class="sub">法甲・押主場冷門', html)  # "FL1" in the record


class AnchorTests(unittest.TestCase):
    def test_no_page_repeats_an_anchor(self):
        """Two strategies pointing at one match give two fade cards, one anchor."""
        football = {
            "competitions": [{"code": "PL", "name": "英超", "cards": [card()], "signals": []}]
        }
        fades = [
            {"sport_key": "football", "sport": "足球", "match_id": "1", "status": "value",
             "strategies": [k], "kickoff_utc": "2026-10-17T14:00:00+00:00", "match": "A vs B",
             "fade_label": label, "tw_price": 2.0, "ev": 0.05, "threshold": 0.02,
             "fair_prob": 0.525}
            for k, label in (("home_dog", "客勝或和局"), ("longshot8", "主勝或和局"))
        ]  # fmt: skip
        s = site.Site(now=NOW, football=football, mlb=None, tape=[], fade_live=fades)
        for key, _, _ in site.PAGES:
            html = site.render_page(s, key)
            ids = re.findall(r'\bid="([^"]+)"', html)
            self.assertEqual(len(ids), len(set(ids)), key)


if __name__ == "__main__":
    unittest.main()
