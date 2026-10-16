"""The site's view builders (render/site.py): charts, readings and history verdicts."""

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


if __name__ == "__main__":
    unittest.main()
