import math
import unittest
from datetime import UTC, datetime

from sports_analyzer.analysis import fade
from sports_analyzer.config import ModelConfig
from sports_analyzer.render.site import Site, render_page
from tests import conftest  # noqa: F401


class ScoringTests(unittest.TestCase):
    def test_hit_rate_against_the_market_and_roi_by_price(self):
        # Two bets at a fair 50% and 25%: one wins at 2.10, the other loses.
        bets = [
            {"hit": 1.0, "prob": 0.5, "intl": 2.10},
            {"hit": 0.0, "prob": 0.25, "intl": 3.80},
        ]
        s = fade.score(bets, ("intl",))
        self.assertEqual((s["n"], s["hit"], s["fair"]), (2, 50.0, 37.5))
        self.assertEqual(s["gap_pp"], 12.5)
        # z = (0.5 - 0.375) * 2 / sqrt(0.25 + 0.1875)
        self.assertAlmostEqual(s["z"], round(0.25 / math.sqrt(0.4375), 2))
        self.assertEqual(s["roi_fair"], 0.0)  # (2 - 1 - 1) / 2
        self.assertEqual(s["roi_intl"], 5.0)  # (1.10 - 1) / 2
        # break even needs (1/2.10 + 1/3.80)/2 = 0.3697 against a fair 0.375: -0.5pp
        self.assertEqual(s["need_intl"], -0.5)
        self.assertEqual(fade.score([]), {"n": 0})

    def test_dutch_and_tw_prices(self):
        self.assertAlmostEqual(fade.dutch([2.0, 4.0]), 4 / 3)
        probs = [0.75, 0.18, 0.07]
        prices = fade.tw_prices(probs, 0.20)
        self.assertAlmostEqual(sum(1 / p for p in prices), 1.20, places=6)
        # 運彩's margin weighs on long odds: the favourite pays far less than the outsider.
        fav_margin = (1 / probs[0]) / prices[0] - 1
        long_margin = (1 / probs[2]) / prices[2] - 1
        self.assertLess(fav_margin, 0.12)
        self.assertGreater(long_margin, 0.35)
        even = fade.tw_prices([0.6, 0.4], 0.16, even=True)
        self.assertAlmostEqual(even[0], 1 / (0.6 * 1.16))


def mlb_card(home_fair: float, g: float, dog_price: float, ev: float, fresh: bool = True) -> dict:
    return {
        "game_pk": 1,
        "kickoff_utc": "2026-10-16T00:00:00+00:00",
        "home": "Los Angeles Dodgers",
        "away": "Milwaukee Brewers",
        "market": {"h2h": {"home": {"fair": home_fair}, "away": {"fair": round(1 - home_fair, 4)}}},
        "t10": {"g": g},
        "t10v": {
            "bets": [
                {"key": "fav_ml", "price": 1.4, "ev": -0.12, "fresh": fresh},
                {"key": "dog_ml", "price": dog_price, "ev": ev, "fresh": fresh},
            ]
        },
        "tw": {"home_zh": "道奇", "away_zh": "釀酒人"},
    }


def football_card(p: tuple[float, float, float], tw: tuple[float, float, float] | None) -> dict:
    card = {
        "match_id": 7,
        "kickoff_utc": "2026-10-17T14:00:00+00:00",
        "home": "Ipswich",
        "away": "Man City",
        "market": {
            "h2h": {k: {"fair": v} for k, v in zip(("home", "draw", "away"), p, strict=True)},
            "totals": {"line": 3.5, "over": {"fair": 0.45}, "under": {"fair": 0.55}},
        },
        "tw": None,
    }
    if tw:
        card["tw"] = {
            "home_zh": "伊普斯維奇",
            "away_zh": "曼城",
            "fair_fresh": True,
            "h2h": {k: {"price": v} for k, v in zip(("home", "draw", "away"), tw, strict=True)},
            "totals": {"line": 3.5, "over": {"price": 2.05}, "under": {"price": 1.62}},
        }
    return card


class LiveTests(unittest.TestCase):
    cfg = ModelConfig()

    def test_mlb_strategies_pointing_at_the_same_bet_share_a_row(self):
        rows = fade.live_candidates([], [], [mlb_card(0.62, 0.071, 2.26, -0.1412)], self.cfg)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["strategies"], ["t10_g6", "home_fav55"])
        self.assertEqual((r["fade_label"], r["match"]), ("釀酒人 不讓分", "釀酒人 @ 道奇"))
        self.assertEqual((r["tw_price"], r["ev"], r["status"]), (2.26, -0.1412, "pass"))
        stale = fade.live_candidates([], [], [mlb_card(0.62, 0.0, 2.6, 0.04, False)], self.cfg)
        self.assertEqual((stale[0]["strategies"], stale[0]["status"]), (["home_fav55"], "stale"))
        value = fade.live_candidates([], [], [mlb_card(0.62, 0.0, 2.75, 0.045)], self.cfg)
        self.assertEqual(value[0]["status"], "value")
        none = fade.live_candidates([], [], [mlb_card(0.52, 0.03, 2.0, -0.1)], self.cfg)
        self.assertEqual(none, [])

    def test_football_longshot_home_underdog_and_a_signal(self):
        card = football_card((0.07, 0.18, 0.75), (21.0, 7.0, 1.15))
        signals = [
            {"match_id": 7, "selection_key": "away", "line": None},
            {"match_id": 7, "selection_key": "over", "line": 3.5},
        ]
        rows = fade.live_candidates([card], signals, [], self.cfg)
        by = {tuple(r["strategies"]): r for r in rows}
        # The home side is both the longest price and the home underdog: one bet,
        # draw + away dutched at 運彩: 1 / (1/7 + 1/1.15) = 0.987, a sure loss.
        r = by[("longshot8", "home_dog")]
        self.assertEqual(r["fade_label"], "和局＋客勝 曼城")
        self.assertAlmostEqual(r["tw_price"], round(1 / (1 / 7 + 1 / 1.15), 3))
        self.assertAlmostEqual(r["ev"], round(0.93 * (1 / (1 / 7 + 1 / 1.15)) - 1, 4))
        # 模型分歧 on the away win: back home + draw; on over 3.5: back under 3.5.
        labels = sorted(x["fade_label"] for x in rows if x["strategies"] == ["model_signal"])
        self.assertEqual(labels, ["主勝 伊普斯維奇＋和局", "小 3.5"])
        under = next(x for x in rows if x["fade_label"] == "小 3.5")
        self.assertEqual((under["tw_price"], under["ev"]), (1.62, round(0.55 * 1.62 - 1, 4)))
        self.assertEqual(len(rows), 3)

    def test_no_tw_price_is_shown_without_ev(self):
        rows = fade.live_candidates([football_card((0.30, 0.25, 0.45), None)], [], [], self.cfg)
        self.assertEqual([(r["strategies"], r["tw_price"], r["status"]) for r in rows],
                         [(["home_dog"], None, "none")])  # fmt: skip


class RecordTests(unittest.TestCase):
    def test_finished_games_at_the_tw_price_captured(self):
        mlb = [
            {  # Brewers 5-2 at Dodgers (62% at home): the opposite bet won at 2.26
                "kickoff_utc": "2026-10-15T00:00:00+00:00",
                "home": "Los Angeles Dodgers",
                "away": "Milwaukee Brewers",
                "close_home": "0.62",
                "t10_g": "0.071",
                "t10_fav": "home",
                "tw_home": "1.4",
                "tw_away": "2.26",
                "final_h": "2",
                "final_a": "5",
            }
        ]
        football = [
            {  # home longshot at 7%: draw + away at 運彩 7.0 / 1.15; the away side won 0-2
                "match_id": "7",
                "kickoff_utc": "2026-10-17T14:00:00+00:00",
                "home": "Ipswich",
                "away": "Man City",
                "close_h": "0.07",
                "close_d": "0.18",
                "close_a": "0.75",
                "tw_h": "21",
                "tw_d": "7",
                "tw_a": "1.15",
                "outcome": "A",
                "ft_h": "0",
                "ft_a": "2",
            }
        ]
        signals = [
            {  # 模型分歧 on over 3.5 lost (2 goals): the opposite (under) won, no 運彩 price
                "sport": "football",
                "match_id": "7",
                "kickoff_utc": "2026-10-17T14:00:00+00:00",
                "home": "Ipswich",
                "away": "Man City",
                "selection": "大分 3.5",
                "selection_key": "over",
                "line": "3.5",
                "close_p": "0.45",
                "status": "loss",
                "result": "0-2",
            }
        ]
        rows = fade.fade_record(football, mlb, signals)
        by = {(r["strategy"], r["sport"]): r for r in rows}
        mlb_row = by[("t10_g6", "MLB")]
        self.assertEqual((mlb_row["result"], mlb_row["score"]), ("win", "5-2"))
        self.assertAlmostEqual(mlb_row["units"], 1.26)
        self.assertIn(("home_fav55", "MLB"), by)
        long_row = by[("longshot8", "足球")]
        self.assertEqual(long_row["result"], "win")
        self.assertAlmostEqual(long_row["units"], round(1 / (1 / 7 + 1 / 1.15), 3) - 1)
        self.assertLess(long_row["units"], 0)  # won, and still lost money
        sig = by[("model_signal", "足球")]
        self.assertEqual((sig["result"], sig["units"], sig["fair_prob"]), ("win", None, 0.55))
        s = fade.record_summary(rows)
        self.assertEqual(
            (s["t10_g6"]["n"], s["t10_g6"]["wins"], s["t10_g6"]["units"]), (1, 1.0, 1.26)
        )
        self.assertEqual(s["model_signal"]["priced"], 0)
        self.assertIsNone(s["model_signal"]["roi"])
        self.assertEqual(s["longshot8"]["roi"], round(long_row["units"] * 100, 1))


class PageTests(unittest.TestCase):
    def test_page(self):
        row = {
            "sport": "MLB",
            "group": "mine",
            "name": "T-10：G≥+6 押強隊不讓分（你的原始規則）",
            "self": {"n": 3554, "hit": 64.2, "fair": 66.0, "gap_pp": -1.8, "z": -2.28},
            "fade": {
                "n": 3554, "hit": 35.8, "fair": 34.0, "gap_pp": 1.8, "z": 2.28,
                "roi_fair": 5.3, "roi_intl": -2.4, "roi_best": 0.5, "roi_tw": -9.2,
                "need_intl": 2.6, "need_best": 1.5, "need_tw": 5.4,
            },
            "early": {"n": 2411, "gap_pp": -1.91}, "late": {"n": 1143, "gap_pp": -1.57},
            "verdict": "consistent",
        }  # fmt: skip
        longshot = {
            **row,
            "sport": "足球",
            "group": "public",
            "name": "押大冷門（賠率≥8）",
            "fade": {**row["fade"], "fair": 92.8, "need_tw": 13.4, "roi_tw": -11.5},
        }
        summary = {
            "generated": "2026-10-15",
            "data": {
                "mlb_games": 10522, "mlb_seasons": "2021–2025", "football_matches": 54880,
                "football_seasons": "2019/20–2026/27", "football_leagues": 22,
                "halves": {"mlb": ["2021–2023", "2024–2025"], "football": ["a", "b"]},
            },
            "margins": {"mlb": 0.16, "1x2": 0.20, "totals": 0.171},
            "strategies": {"t10_g6": row, "longshot8": longshot},
        }  # fmt: skip
        site = Site(
            now=datetime(2026, 10, 15, 9, tzinfo=UTC),
            football=None,
            mlb=None,
            tape=[],
            fade_summary=summary,
        )
        html = render_page(site, "fade")
        self.assertIn("<title>反者道之動｜比賽分析</title>", html)
        self.assertIn("反者道之動，弱者道之用。", html)
        self.assertIn("沒有一種賺錢", html)
        self.assertIn("+1.8</b> ／ 需 2.6・5.4", html)
        self.assertIn("不可能", html)  # 92.8% + 13.4 points is more than certain
        self.assertIn("目前沒有符合的比賽。", html)


if __name__ == "__main__":
    unittest.main()
