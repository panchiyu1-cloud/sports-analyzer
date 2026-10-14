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

    def entry(self, t10, t10v=None):
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
            "t10v": t10v,
        }
        snaps = [{"generated_at": "2026-10-14T12:30:00+00:00", "games": [mlb_card]}]
        return review.collect_prematch(snaps, lambda s: s["games"], "game_pk")["9001"]

    @staticmethod
    def value_block(status="value"):
        pick = {
            "key": "dog_rl",
            "label": "客隊 +1.5",
            "side": "away",
            "line": 1.5,
            "price": 1.80,
            "fair_odds": 1.72,
            "ev": 0.0465,
        }
        return {
            "favourite": "home",
            "g_intl": 0.072,
            "g_tw": 0.101,
            "status": status,
            "pick": pick if status == "value" else None,
            "best": {**pick, "ev": 0.0465 if status == "value" else -0.021},
            "tw_at": "2026-10-15T07:50+08:00",
            "fair_at": "2026-10-15T07:51+08:00",
            "minutes_before": 18,
        }

    def test_parse_box(self):
        self.assertEqual(self.box.innings_home[-1], None)  # the "x"
        self.assertEqual(self.box.innings_home[6], 4)
        self.assertEqual(self.box.starter_away.name, "Away Starter")
        self.assertEqual(PitcherLine("x", "6.1").outs, 19)
        self.assertEqual((self.box.hits_home, self.box.errors_away), (9, 1))

    def test_t10_value_bet_is_graded_on_its_own_handicap(self):
        t10 = {"favourite": "home", "g": 0.072, "shape": "強隊大勝型"}
        row = review.mlb_row(self.entry(t10, self.value_block()), self.game, self.box, NOW)
        self.assertEqual((row["winner"], row["fav_won"], row["fav_cover"]), ("home", "1", "1"))
        # 客隊 +1.5 after a 5-2 home win: -3 + 1.5 < 0 -> lost one unit.
        self.assertEqual((row["t10_result"], row["t10_units"]), ("loss", -1.0))
        self.assertEqual(row["verdict"], "t10_value_loss")
        self.assertAlmostEqual(row["market_ll"], round(-math.log(0.58), 4))
        self.assertEqual(row["innings_h"], "0 0 1 0 0 0 4 0 x")
        text = " ".join(review.explain_mlb(row)["bullets"])
        self.assertIn(
            "T-10 價值版（開賽前 18 分比價）：可下 客隊 +1.5 @ 1.8（公允 1.72，EV +4.7%） → 未命中 ✗",
            text,
        )
        self.assertIn("G 值：國際 +7.2pp、運彩 +10.1pp", text)
        # The away starter went 6.1 innings: he was still in when the 7th began.
        self.assertIn("Home Club 第 7 局攻下 4 分（對方先發在這局退場）", text)
        self.assertIn("賽前預告的是 Someone Else，臨時換人", text)

        one_run = MlbGame(
            9001, self.game.game_date, "Final", "Home Club", "Away Club", None, None, None, 3, 2
        )
        row = review.mlb_row(self.entry(t10, self.value_block()), one_run, None, NOW)
        # Lost by one with +1.5: -1 + 1.5 > 0 -> won 0.80 at 1.80.
        self.assertEqual((row["t10_result"], row["t10_units"]), ("win", 0.8))
        self.assertIn("一分差比賽", " ".join(review.explain_mlb(row)["bullets"]))
        s = review.summarize_mlb([row])
        self.assertEqual((s["t10_bets"], s["t10_wins"], s["t10_units"]), (1, 1, 0.8))
        self.assertEqual(s["t10_judged"], 1)

    def test_no_bet_is_not_graded_on_the_score(self):
        t10 = {"favourite": "home", "g": 0.072}
        row = review.mlb_row(self.entry(t10, self.value_block("pass")), self.game, self.box, NOW)
        self.assertEqual((row["t10_result"], row["verdict"]), ("", "t10_pass"))
        self.assertIn(
            "不下，最接近的是 客隊 +1.5（EV -2.1%）", " ".join(review.explain_mlb(row)["bullets"])
        )
        stale = review.mlb_row(self.entry(t10, self.value_block("stale")), self.game, None, NOW)
        self.assertEqual(stale["verdict"], "t10_stale")
        # Snapshots from before the value version have no block at all.
        old = review.mlb_row(
            self.entry({"favourite": "home", "g": 0.072, "signal": "FAV_ML"}), self.game, None, NOW
        )
        self.assertEqual(old["verdict"], "no_t10")
        self.assertEqual(review.summarize_mlb([row, stale, old])["t10_judged"], 1)


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


class RecommendationReviewTests(unittest.TestCase):
    """Every recommendation on the page is reviewed on its match's card."""

    def football(self, hg, ag, comp="CL", **extra):
        e = entry_for({"h2h": h2h(0.45, 0.28, 0.27)}, {"h2h": h2h(0.45, 0.28, 0.27)}, comp=comp)
        row = review.football_row(e, finished(hg, ag), None, None, NOW)
        row.update(extra)
        return row

    def test_value_bet_lost_and_the_close_went_against_it(self):
        # LASK - Liverpool, 2026-10-14: 讓分 0:2 客 at 3.15 against a fair 34.2%; the
        # consensus closed at 25.8% (CLV -18.9%).
        value = {
            "selection": "讓分0:2 客（利物浦）",
            "selection_key": "hcp3_away",
            "line": "2",
            "tw_price": "3.15",
            "fair_prob": "0.3421",
            "ev": "0.0776",
            "closing_fair_prob": "0.2576",
            "clv": "-0.1885",
            "status": "pending",  # the daily run has not settled it yet
            "captured_at": "2026-10-14T20:00+08:00",
        }
        row = self.football(0, 2)
        ex = review.explain_football(row, [review.pick_from_value(value)])
        p = ex["picks"][0]
        self.assertEqual((p["source_label"], p["result"]), ("運彩價值注", "loss"))
        self.assertEqual(p["margin"], "（Lille OSC 贏 2 球）")  # a two-goal win is the 讓分 draw
        self.assertIn("運彩 @ 3.15，當時公允機率 34%（EV +7.8%）", p["lines"])
        self.assertIn(
            "開賽前國際盤 34% → 26%（-8.5pp），用收盤價重算 CLV -18.9%：市場後來不認同這個價格。",
            p["lines"],
        )
        self.assertEqual(p["verdict"], "wrong")
        self.assertEqual(p["conclusion"], "收盤價值（CLV）不利：這注的判斷有問題。")

    def test_value_bet_that_kept_its_value_and_lost_was_unlucky(self):
        value = {
            "selection": "大 2.5",
            "selection_key": "over",
            "line": "2.5",
            "tw_price": "2.10",
            "fair_prob": "0.50",
            "ev": "0.05",
            "closing_fair_prob": "0.52",
            "clv": "0.092",
            "status": "loss",
        }
        p = review.explain_football(self.football(1, 0), [review.pick_from_value(value)])["picks"][
            0
        ]
        self.assertEqual((p["result_text"], p["margin"]), ("未命中 ✗", "（總進球 1）"))
        self.assertEqual(p["verdict"], "unlucky")
        self.assertIn("價格經得起收盤檢驗", p["lines"][1])

    def test_signal_with_its_closing_line_and_xg(self):
        signal = {
            "sport": "football",
            "selection": "主 +0.5",
            "selection_key": "spread_home",
            "line": "0.5",
            "model_p": "0.7455",
            "market_p": "0.50",
            "price": "2.0",
            "close_p": "0.55",
            "clv": "0.10",
            "status": "pending",
            "created_at": "2026-10-13T00:50:04+00:00",
        }
        row = self.football(1, 1, comp="PL", xg_h=1.2, xg_a=1.0)
        p = review.explain_football(row, [review.pick_from_signal(signal)])["picks"][0]
        self.assertEqual((p["source_label"], p["result"]), ("模型分歧", "win"))
        self.assertEqual(p["lines"][0], "模型 75% vs 市場 50%，分歧 +25pp")
        self.assertIn("比賽xG支持這一邊。", p["lines"])  # xG 1.2-1.0 + 0.5 = +0.7 for home
        self.assertEqual(
            (p["verdict"], p["conclusion"]), ("right", "判斷有賽前市場移動、比賽xG支持，結果也對。")
        )

    def test_the_lean_signal_is_judged_like_the_card_and_not_repeated(self):
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
            "sport": "football",
            "selection_key": "spread_away",
            "line": "-0.25",
            "selection": "Lille OSC +0.25",
            "model_p": "0.59",
            "market_p": "0.48",
            "price": "2.06",
            "close_p": "0.40",  # would read as "against" on its own; the card measured 0pp
            "status": "half_win",
        }
        row = review.football_row(e, finished(1, 1, (0, 1)), None, signal, NOW)
        ex = review.explain_football(row, [review.pick_from_signal(signal)])
        self.assertEqual((ex["verdict"], ex["picks"][0]["verdict"]), ("hit", "hit"))
        self.assertNotIn("模型分歧：", " ".join(ex["bullets"]))  # listed with the picks instead
        self.assertEqual(ex["picks"][0]["result_text"], "贏一半")

    def test_mlb_t10_pick_and_another_value_bet(self):
        t = MlbReviewTests()
        t.setUp()
        row = review.mlb_row(
            t.entry({"favourite": "home", "g": 0.072}, t.value_block()), t.game, t.box, NOW
        )
        t10_bet = {  # the T-10 pick 客隊 +1.5, kept by the ledger as the home handicap
            "selection": "客受讓（客隊） +1.5",
            "selection_key": "spread_away",
            "line": "-1.5",
            "tw_price": "1.80",
            "fair_prob": "0.5814",
            "ev": "0.0465",
            "closing_fair_prob": "0.60",
            "clv": "0.08",
            "status": "loss",
        }
        totals = {
            "selection": "大 7.5",
            "selection_key": "over",
            "line": "7.5",
            "tw_price": "1.95",
            "fair_prob": "0.53",
            "ev": "0.0335",
            "status": "loss",
        }
        picks = [review.pick_from_value(t10_bet), review.pick_from_value(totals)]
        ex = review.explain_mlb(row, picks)
        self.assertEqual(
            [(p["source_label"], p["result"]) for p in ex["picks"]],
            [("T-10 可下", "loss"), ("運彩價值注", "loss")],
        )
        self.assertEqual(ex["picks"][0]["margin"], "（Away Club 輸 3 分）")
        self.assertEqual(ex["picks"][1]["margin"], "（總分 7）")
        self.assertIn("開賽前沒有再買到國際價", ex["picks"][1]["lines"][1])
        # The T-10 decision is reviewed with the picks, not repeated in the bullets.
        self.assertNotIn("可下 客隊 +1.5", " ".join(ex["bullets"]))
        self.assertIn("可下 客隊 +1.5", " ".join(review.explain_mlb(row)["bullets"]))

    def test_recommended_matches_lead_the_front_page_and_are_never_dropped(self):
        from sports_analyzer.render.cards import review_views

        rows = []
        for i in range(14):  # a busy weekend: 14 reviews in the last three days
            r = self.football(1, 0)
            r.update(match_id=str(100 + i), kickoff_utc=f"2026-10-14T{10 + i % 9:02d}:00:00+00:00")
            rows.append({k: review_store._cell(v) for k, v in r.items()})
        value = {
            "sport": "football",
            "match_id": "100",
            "selection": "主勝",
            "selection_key": "home",
            "line": "",
            "tw_price": "2.2",
            "fair_prob": "0.5",
            "ev": "0.1",
            "status": "win",
        }
        views = review_views(rows, [], NOW, value_rows=[value])
        self.assertEqual(len(views["football"]), 12)
        self.assertEqual(views["football"][0]["row"]["match_id"], "100")
        self.assertEqual(views["picks"], {"n": 1, "settled": 1, "wins": 1})


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

    def test_a_started_match_with_a_recommendation_is_flagged_while_waiting(self):
        started = card(market={"h2h": h2h(0.45, 0.28, 0.27)})  # kicked off before NOW
        football = {
            "competitions": [{"code": "PL", "name": "英超", "cards": [started], "signals": []}]
        }
        value = {
            "sport": "football",
            "match_id": "7",
            "selection": "主勝",
            "selection_key": "home",
            "line": "",
            "tw_price": "2.4",
            "fair_prob": "0.45",
            "ev": "0.08",
            "status": "pending",
        }
        with tempfile.TemporaryDirectory() as tmp:
            html = render_index(
                football, None, None, NOW, Path(tmp) / "index.html", value_rows=[value]
            ).read_text(encoding="utf-8")
        self.assertIn("<b>Arsenal FC vs Lille OSC（有推薦）</b>", html)
        self.assertIn("下一次更新（每次上傳運彩，或台北 08:15／20:25）就會產生賽後檢討", html)


if __name__ == "__main__":
    unittest.main()
