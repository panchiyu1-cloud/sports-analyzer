import math
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sports_analyzer import review_store
from sports_analyzer.analysis import review
from sports_analyzer.analysis import taiwan as tw_compare
from sports_analyzer.config import ModelConfig
from sports_analyzer.fetch.football import Match, parse_matches
from sports_analyzer.fetch.mlb import MlbGame, PitcherLine, parse_box
from sports_analyzer.history import find_stats, read_stats
from sports_analyzer.render.cards import render_index, render_reviews_page
from tests import conftest  # noqa: F401

NOW = datetime(2026, 10, 15, 0, 30, tzinfo=UTC)
KICKOFF = "2026-10-14T19:00:00+00:00"


def h2h(home, draw, away, stale=False):
    block = {
        "home": {
            "fair": home,
            "fair_odds": round(1 / home, 2),
            "best": round(1 / home * 1.02, 2),
            "book": "x",
        },
        "draw": {
            "fair": draw,
            "fair_odds": round(1 / draw, 2),
            "best": round(1 / draw * 1.02, 2),
            "book": "x",
        },
        "away": {
            "fair": away,
            "fair_odds": round(1 / away, 2),
            "best": round(1 / away * 1.02, 2),
            "book": "x",
        },
    }
    if stale:
        block["stale"] = True
    return block


def card(model=(0.55, 0.25, 0.20), market=None, data_ok=True, notes=(), tw=None, comp="PL"):
    return {
        "sport": "football",
        "competition": comp,
        "competition_name": "英超" if comp == "PL" else "歐冠",
        "match_id": 7,
        "kickoff_utc": KICKOFF,
        "kickoff_local": "10/15 (週四) 03:00",
        "home": "Arsenal FC",
        "away": "Lille OSC",
        "home_key": "arsenal",
        "away_key": "lille",
        "model": {
            "fitted": True,
            "data_ok": data_ok,
            "p_home": model[0],
            "p_draw": model[1],
            "p_away": model[2],
            "lambda_home": 1.6,
            "mu_away": 0.9,
            "over25": 0.5,
            "most_likely": "1-0（12%）",
        },
        "market": market,
        "edges": {},
        "recommendation": None,
        "form": {"home": "", "away": ""},
        "table": {"home": None, "away": None},
        "h2h": [],
        "notes": list(notes),
        "tw": tw,
    }


def snapshot(generated_at, cards):
    return {"generated_at": generated_at, "competitions": [{"cards": cards}]}


def finished(hg, ag, ht=(None, None)):
    return Match(
        7,
        "PL",
        datetime.fromisoformat(KICKOFF),
        "FINISHED",
        8,
        "Arsenal FC",
        "Lille OSC",
        hg,
        ag,
        ht[0],
        ht[1],
    )


def entry_for(open_mkt, close_mkt, **card_kw):
    snaps = [
        snapshot("2026-10-13T01:00:00+00:00", [card(market=open_mkt, **card_kw)]),
        snapshot("2026-10-14T12:30:00+00:00", [card(market=close_mkt, **card_kw)]),
    ]
    pre = review.collect_prematch(snaps, lambda s: s["competitions"][0]["cards"], "match_id")
    return pre["7"]


class CollectTests(unittest.TestCase):
    def test_first_and_last_snapshot_before_kickoff(self):
        tw_early = {"captured_at": "2026-10-13T00:05:00+00:00", "h2h": {"home": {"price": 1.5}}}
        tw_late = {"captured_at": "2026-10-14T12:00:00+00:00", "h2h": {"home": {"price": 1.6}}}
        snaps = [
            snapshot(
                "2026-10-14T12:30:00+00:00",
                [card(market={"h2h": h2h(0.5, 0.27, 0.23)}, tw=tw_late)],
            ),
            snapshot(
                "2026-10-13T01:00:00+00:00",
                [card(market={"h2h": h2h(0.45, 0.28, 0.27)}, tw=tw_early)],
            ),
            # written after kickoff: never a pre-match number
            snapshot("2026-10-15T00:15:00+00:00", [card(market={"h2h": h2h(0.9, 0.05, 0.05)})]),
        ]
        e = review.collect_prematch(snaps, lambda s: s["competitions"][0]["cards"], "match_id")["7"]
        self.assertEqual(e["open"]["h2h"]["home"]["fair"], 0.45)
        self.assertEqual(e["close"]["h2h"]["home"]["fair"], 0.5)
        self.assertEqual(e["open_at"], "2026-10-13T01:00:00+00:00")
        self.assertEqual(e["tw"]["h2h"]["home"]["price"], 1.6)

    def test_stale_blocks_do_not_count_as_closing_prices(self):
        mv = {
            "h2h": h2h(0.5, 0.27, 0.23, stale=True),
            "totals": {"line": 2.5, "stale": True, "over": {"fair": 0.6}, "under": {"fair": 0.4}},
        }
        self.assertIsNone(review.side_prob(mv, "home", None, fresh_only=True))
        self.assertEqual(review.side_prob(mv, "home", None), 0.5)
        self.assertIsNone(review.side_prob(mv, "over", 2.5, fresh_only=True))
        self.assertIsNone(review.side_prob(mv, "over", 3.0))


class FootballReviewTests(unittest.TestCase):
    def test_log_loss_and_quarter_line_signal(self):
        spread = {
            "line": -0.25,
            "home": {"fair": 0.52, "best": 1.95, "book": "x"},
            "away": {"fair": 0.48, "best": 2.06, "book": "x"},
        }
        e = entry_for(
            {"h2h": h2h(0.45, 0.28, 0.27), "spread": spread},
            {"h2h": h2h(0.44, 0.27, 0.29), "spread": spread},
        )
        signal = {
            "selection_key": "spread_away",
            "line": "-0.25",
            "selection": "Lille OSC +0.25",
            "model_p": "0.59",
            "market_p": "0.48",
            "price": "2.06",
        }
        row = review.football_row(e, finished(1, 1, (0, 1)), None, signal, NOW)
        # Draw: model 0.25 -> -ln 0.25 = 1.3863; market close 0.27 -> -ln 0.27 = 1.3093
        self.assertAlmostEqual(row["model_ll"], 1.3863, places=4)
        self.assertAlmostEqual(row["market_ll"], 1.3093, places=4)
        self.assertEqual(row["outcome"], "D")
        self.assertEqual(row["lean_result"], "half_win")
        self.assertAlmostEqual(row["lean_profit"], 0.53)
        self.assertEqual(row["move_pp"], 0.0)  # same spread price at open and close
        ex = review.explain_football(row)
        text = " ".join(ex["bullets"])
        self.assertIn("贏一半", text)
        self.assertIn("（打平）", text)
        self.assertIn("半場 0–1 由 Lille OSC 領先，下半場被扳平", text)
        self.assertTrue(
            ex["conclusion"].startswith("這個方向市場只給 48%、模型給 59%，結果站在模型這邊。")
        )
        self.assertIn("這場市場比模型準", text)

    def test_unlucky_when_market_and_xg_back_the_lean_but_it_loses(self):
        e = entry_for({"h2h": h2h(0.45, 0.28, 0.27)}, {"h2h": h2h(0.50, 0.27, 0.23)})
        row = review.football_row(e, finished(0, 1), None, None, NOW)
        row.update({"xg_h": 2.1, "xg_a": 0.6})
        row.update(review.assess_football(row))
        self.assertEqual(row["lean_key"], "home")  # 0.55 vs 0.45 at the open
        self.assertEqual(row["lean_source"], "model")
        self.assertEqual(row["move_pp"], 5.0)
        self.assertEqual((row["move_sign"], row["stats_sign"]), (1, 1))
        self.assertEqual(row["verdict"], "unlucky")
        text = " ".join(review.explain_football(row)["bullets"])
        self.assertIn("開賽前市場往模型的方向移動", text)
        self.assertIn("把握度", text)  # 0-1 against xG 2.1-0.6 = 2.5 goals of finishing luck

    def test_wrong_when_evidence_goes_against_the_lean(self):
        e = entry_for({"h2h": h2h(0.45, 0.28, 0.27)}, {"h2h": h2h(0.40, 0.28, 0.32)})
        row = review.football_row(e, finished(0, 2), None, None, NOW)
        row.update({"xg_h": 0.5, "xg_a": 1.9})
        row.update(review.assess_football(row))
        self.assertEqual(row["verdict"], "wrong")
        self.assertIn(
            "結果站在市場那邊：模型高估了 Arsenal FC。", review.explain_football(row)["conclusion"]
        )
        self.assertIn("判讀", "".join(review.explain_football(row)["bullets"]) + "判讀")

    def test_hit_lucky_agree_and_no_model(self):
        e = entry_for({"h2h": h2h(0.45, 0.28, 0.27)}, {"h2h": h2h(0.45, 0.28, 0.27)})
        row = review.football_row(e, finished(2, 0), None, None, NOW)
        self.assertEqual(row["verdict"], "hit")
        row.update({"sot_h": 1, "sot_a": 6})
        row.update(review.assess_football(row))
        self.assertEqual(row["verdict"], "lucky")
        # The model agrees with the market: no lean, nothing to grade.
        e = entry_for({"h2h": h2h(0.54, 0.25, 0.21)}, {"h2h": h2h(0.54, 0.25, 0.21)})
        self.assertEqual(
            review.football_row(e, finished(2, 0), None, None, NOW)["verdict"], "agree"
        )
        e = entry_for(
            {"h2h": h2h(0.30, 0.28, 0.42)},
            None,
            data_ok=False,
            notes=["資料不足：Lille 僅 1 場（建議 ≥ 6）"],
        )
        row = review.football_row(e, finished(2, 0), None, None, NOW)
        self.assertEqual(row["verdict"], "no_model")
        text = " ".join(review.explain_football(row)["bullets"])
        self.assertNotIn("模型預期進球", text)  # unreliable model numbers are not compared
        self.assertIn("不算模型判斷", text)

    def test_red_card_and_stats_join(self):
        csv_text = (
            "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,HxG,AxG,HS,AS,HST,AST,HF,AF,HC,AC,HY,AY,HR,AR\n"
            "E0,14/10/2026,20:00,Arsenal,Lille,2,0,H,1,0,H,1.85,0.42,17,6,7,1,9,12,8,2,1,3,0,1\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "E0_2627.csv"
            path.write_text(csv_text, encoding="utf-8")
            stats = read_stats(path)
        st = find_stats(stats, "arsenal", "lille", datetime.fromisoformat(KICKOFF).date())
        assert st is not None
        self.assertEqual((st.xg_home, st.xg_away, st.sot_home, st.red_away), (1.85, 0.42, 7, 1))
        self.assertIsNone(
            find_stats(stats, "lille", "arsenal", datetime.fromisoformat(KICKOFF).date())
        )
        e = entry_for({"h2h": h2h(0.45, 0.28, 0.27)}, {"h2h": h2h(0.45, 0.28, 0.27)})
        row = review.football_row(e, finished(2, 0), st, None, NOW)
        self.assertEqual(row["stats_basis"], "xG")
        self.assertEqual(row["verdict"], "right")  # xG 1.85-0.42 backs the home lean
        text = " ".join(review.explain_football(row)["bullets"])
        self.assertIn("Lille OSC 吃到 1 張紅牌", text)
        self.assertIn("實際 xG（機會品質）1.9 : 0.4", text)

    def test_cl_without_stats_says_so_and_flags_the_pooled_model(self):
        e = entry_for(
            {"h2h": h2h(0.45, 0.28, 0.27)},
            {"h2h": h2h(0.45, 0.28, 0.27)},
            comp="CL",
            notes=["跨聯賽合併模型：可靠度低於聯賽內比賽，回測尚未涵蓋"],
        )
        row = review.football_row(e, finished(1, 2), None, None, NOW)
        text = " ".join(review.explain_football(row)["bullets"])
        self.assertIn("歐冠沒有免費的比賽數據", text)
        self.assertIn("跨聯賽合併模型", text)

    def test_summary_numbers(self):
        e = entry_for({"h2h": h2h(0.45, 0.28, 0.27)}, {"h2h": h2h(0.45, 0.28, 0.27)})
        win = review.football_row(e, finished(2, 0), None, None, NOW)  # lean home wins
        loss = review.football_row(e, finished(0, 1), None, None, NOW)
        loss["match_id"] = "8"
        s = review.summarize_football([win, loss])
        # model: -ln .55 = .5978, -ln .20 = 1.6094 -> 1.104; market: -ln .45 = .7985, -ln .27 = 1.3093 -> 1.054
        self.assertEqual(s["n"], 2)
        self.assertAlmostEqual(s["model_ll"], 1.104, places=3)
        self.assertAlmostEqual(s["market_ll"], 1.054, places=3)
        self.assertEqual((s["model_closer"], s["market_closer"]), (1, 1))
        self.assertEqual(s["leans"]["hits"], 1.0)
        self.assertEqual(s["leans"]["decided"], 2.0)
        self.assertEqual(s["leans"]["expected"], 0.9)  # market gave the home lean 45% twice
        self.assertAlmostEqual(s["goal_bias_h"], 0.6)  # (1.6-2 + 1.6-0) / 2
        self.assertIn("樣本還少", s["takeaways"][0])


class StoreTests(unittest.TestCase):
    def test_upsert_round_trip_and_columns(self):
        e = entry_for({"h2h": h2h(0.45, 0.28, 0.27)}, {"h2h": h2h(0.45, 0.28, 0.27)})
        row = review.football_row(e, finished(2, 0), None, None, NOW)
        self.assertLessEqual(set(row), set(review_store.FOOTBALL_COLUMNS))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews" / "football.csv"
            self.assertEqual(
                review_store.upsert(path, [row], review_store.FOOTBALL_COLUMNS, "match_id"), (1, 0)
            )
            row["xg_h"] = 1.2
            self.assertEqual(
                review_store.upsert(path, [row], review_store.FOOTBALL_COLUMNS, "match_id"), (0, 1)
            )
            back = review_store.read(path)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["xg_h"], "1.2")
        self.assertEqual(back[0]["model_h"], "0.55")
        # Re-assessing the CSV strings gives the same verdict as the numbers did.
        self.assertEqual(review.assess_football(back[0])["verdict"], row["verdict"])


LINESCORE = {
    "innings": [
        {"num": 1, "home": {"runs": 0}, "away": {"runs": 1}},
        {"num": 2, "home": {"runs": 0}, "away": {"runs": 0}},
        {"num": 3, "home": {"runs": 1}, "away": {"runs": 0}},
        {"num": 4, "home": {"runs": 0}, "away": {"runs": 0}},
        {"num": 5, "home": {"runs": 0}, "away": {"runs": 0}},
        {"num": 6, "home": {"runs": 0}, "away": {"runs": 0}},
        {"num": 7, "home": {"runs": 4}, "away": {"runs": 0}},
        {"num": 8, "home": {"runs": 0}, "away": {"runs": 1}},
        {"num": 9, "home": {}, "away": {"runs": 0}},
    ],
    "teams": {
        "home": {"runs": 5, "hits": 9, "errors": 0},
        "away": {"runs": 2, "hits": 6, "errors": 1},
    },
}
BOXSCORE = {
    "teams": {
        "home": {
            "pitchers": [101, 102],
            "players": {
                "ID101": {
                    "person": {"fullName": "Home Ace"},
                    "stats": {
                        "pitching": {
                            "inningsPitched": "6.0",
                            "hits": 4,
                            "runs": 1,
                            "earnedRuns": 1,
                            "baseOnBalls": 1,
                            "strikeOuts": 8,
                        }
                    },
                },
            },
        },
        "away": {
            "pitchers": [201, 202],
            "players": {
                "ID201": {
                    "person": {"fullName": "Away Starter"},
                    "stats": {
                        "pitching": {
                            "inningsPitched": "6.1",
                            "hits": 6,
                            "runs": 2,
                            "earnedRuns": 2,
                            "baseOnBalls": 2,
                            "strikeOuts": 5,
                        }
                    },
                },
            },
        },
    }
}


class MlbReviewTests(unittest.TestCase):
    def setUp(self):
        self.box = parse_box(LINESCORE, BOXSCORE)
        self.game = MlbGame(
            9001,
            datetime(2026, 10, 15, 0, 8, tzinfo=UTC),
            "Final",
            "Home Club",
            "Away Club",
            "Home Ace",
            "Someone Else",
            None,
            5,
            2,
        )

    def entry(self, t10):
        mlb_card = {
            "game_pk": 9001,
            "kickoff_utc": "2026-10-15T00:08:00+00:00",
            "local": "10/15 08:08",
            "home": "Home Club",
            "away": "Away Club",
            "home_probable": "Home Ace",
            "away_probable": "Someone Else",
            "market": {"h2h": {"home": {"fair": 0.58}, "away": {"fair": 0.42}}},
            "t10": t10,
        }
        snaps = [{"generated_at": "2026-10-14T12:30:00+00:00", "games": [mlb_card]}]
        return review.collect_prematch(snaps, lambda s: s["games"], "game_pk")["9001"]

    def test_parse_box(self):
        self.assertEqual(self.box.innings_home[-1], None)  # the "x"
        self.assertEqual(self.box.innings_home[6], 4)
        self.assertEqual(self.box.starter_away.name, "Away Starter")
        self.assertEqual(PitcherLine("x", "6.1").outs, 19)
        self.assertEqual((self.box.hits_home, self.box.errors_away), (9, 1))

    def test_t10_fav_ml_hit_and_wording(self):
        t10 = {"favourite": "home", "g": 0.072, "signal": "FAV_ML", "threshold": 0.06}
        row = review.mlb_row(self.entry(t10), self.game, self.box, NOW)
        self.assertEqual((row["winner"], row["fav_won"], row["fav_cover"]), ("home", "1", "1"))
        self.assertEqual(row["t10_result"], "win")
        self.assertEqual(row["verdict"], "t10_hit")
        self.assertAlmostEqual(row["market_ll"], round(-math.log(0.58), 4))
        self.assertEqual(row["innings_h"], "0 0 1 0 0 0 4 0 x")
        text = " ".join(review.explain_mlb(row)["bullets"])
        self.assertIn("T-10 G 值 +7.2pp → 熱門 ML（Home Club） → 命中 ✓", text)
        # The away starter went 6.1 innings: he was still in when the 7th began.
        self.assertIn("Home Club 第 7 局攻下 4 分（對方先發在這局退場）", text)
        self.assertIn("賽前預告的是 Someone Else，臨時換人", text)

    def test_t10_dog_plus_one_and_a_half(self):
        t10 = {"favourite": "home", "g": -0.07, "signal": "DOG_PLUS_1_5", "threshold": 0.06}
        row = review.mlb_row(self.entry(t10), self.game, self.box, NOW)
        self.assertEqual(row["t10_result"], "loss")  # favourite won by 3
        one_run = MlbGame(
            9001, self.game.game_date, "Final", "Home Club", "Away Club", None, None, None, 3, 2
        )
        row = review.mlb_row(self.entry(t10), one_run, None, NOW)
        self.assertEqual(row["t10_result"], "win")  # lost by one with +1.5
        self.assertIn("一分差比賽", " ".join(review.explain_mlb(row)["bullets"]))
        s = review.summarize_mlb([row])
        self.assertEqual(s["t10"], {"DOG_PLUS_1_5": {"n": 1, "hits": 1}})


class RegressionTests(unittest.TestCase):
    def test_half_time_and_regular_time_are_parsed(self):
        payload = {
            "matches": [
                {
                    "id": 1,
                    "utcDate": "2026-10-14T19:00:00Z",
                    "status": "FINISHED",
                    "matchday": 1,
                    "homeTeam": {"name": "A"},
                    "awayTeam": {"name": "B"},
                    "score": {
                        "duration": "PENALTY_SHOOTOUT",
                        "fullTime": {"home": 5, "away": 4},
                        "halfTime": {"home": 0, "away": 1},
                        "regularTime": {"home": 1, "away": 1},
                    },
                }
            ]
        }
        m = parse_matches(payload)[0]
        self.assertEqual((m.home_goals, m.away_goals), (1, 1))  # 90 minutes settle the markets
        self.assertEqual((m.home_ht, m.away_ht, m.duration), (0, 1, "PENALTY_SHOOTOUT"))

    def test_later_upload_without_the_match_keeps_the_earlier_tw_block(self):
        c = card(
            market={"h2h": h2h(0.45, 0.28, 0.27)},
            tw={"captured_at": "2026-10-14T09:00:00+00:00", "h2h": {"home": {"price": 1.7}}},
        )
        rep = tw_compare.annotate(
            [c], [], datetime(2026, 10, 14, 22, 58, tzinfo=UTC), ModelConfig()
        )
        self.assertEqual(c["tw"]["h2h"]["home"]["price"], 1.7)
        self.assertEqual(rep["unmatched_cards"], [])  # kicked off: not an alias problem
        self.assertIsNone(rep["avg_overround"])  # stale blocks never count toward the margin


class RenderTests(unittest.TestCase):
    def test_page_moves_finished_matches_into_the_review(self):
        e = entry_for({"h2h": h2h(0.45, 0.28, 0.27)}, {"h2h": h2h(0.45, 0.28, 0.27)})
        row = review.football_row(e, finished(2, 0, (1, 0)), None, None, NOW)
        rows = [{k: review_store._cell(v) for k, v in row.items()}]
        upcoming = dict(
            card(market={"h2h": h2h(0.45, 0.28, 0.27)}),
            match_id=99,
            kickoff_utc="2026-10-16T19:00:00+00:00",
            home="Chelsea FC",
            away="Everton FC",
        )
        football = {
            "competitions": [
                {
                    "code": "PL",
                    "name": "英超",
                    "cards": [card(market={"h2h": h2h(0.45, 0.28, 0.27)}), upcoming],
                    "signals": [],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = render_index(
                football,
                None,
                None,
                NOW,
                Path(tmp) / "index.html",
                football_reviews=rows,
                mlb_reviews=[],
            )
            html = out.read_text(encoding="utf-8")
            page = render_reviews_page(rows, [], NOW, Path(tmp) / "reviews.html").read_text(
                encoding="utf-8"
            )
        self.assertIn("賽後檢討", html)
        self.assertIn("半場 1–0", html)
        self.assertIn("Chelsea FC", html)
        # The finished match shows once, as a review card, not again as an upcoming card.
        self.assertEqual(html.count('<article class="card review">'), 1)
        self.assertEqual(html.count('<div class="name">Arsenal FC</div>'), 1)
        self.assertIn("足球累計", page)


if __name__ == "__main__":
    unittest.main()
