"""NBA: ESPN parsing, the normal margin model, cards, the 運彩 decision, the review and
the pages, plus an offline run of the pipeline with fake clients."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

from sports_analyzer import pipeline, review_store, tw_ledger
from sports_analyzer.analysis import nba as analysis
from sports_analyzer.analysis import review
from sports_analyzer.analysis import taiwan as compare
from sports_analyzer.config import ModelConfig, Settings
from sports_analyzer.fetch import taiwan
from sports_analyzer.fetch.nba import NbaGame, parse_scoreboard
from sports_analyzer.fetch.odds import BookOdds, OddsEvent, SpreadLine, TotalLine
from sports_analyzer.models import margin
from sports_analyzer.render import site
from tests import conftest  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"
CFG = ModelConfig()
FETCHED = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
TIP_KNICKS = datetime(2026, 10, 27, 23, 30, tzinfo=UTC)
TIP_CLIPPERS = datetime(2026, 10, 28, 2, 0, tzinfo=UTC)


def _event(
    event_id: str,
    home: str,
    away: str,
    tipoff: datetime,
    home_ml: float,
    away_ml: float,
    line: float,
    total: float,
    fetched_at: datetime = FETCHED,
) -> OddsEvent:
    """Two books quoting the same symmetric handicap and totals prices (fair 50/50)."""
    books = tuple(
        BookOdds(
            name,
            {"home": home_ml, "away": away_ml},
            (SpreadLine(line, 1.91, 1.91),),
            (TotalLine(total, 1.91, 1.91),),
        )
        for name in ("pinnacle", "betfair_ex_eu")
    )
    return OddsEvent(event_id, "basketball_nba", tipoff, home, away, books, fetched_at=fetched_at)


def _games() -> list[NbaGame]:
    return parse_scoreboard(
        json.loads((FIXTURES / "espn_scoreboard.json").read_text(encoding="utf-8"))
    )


def _events() -> list[OddsEvent]:
    return [
        _event("e1", "New York Knicks", "Boston Celtics", TIP_KNICKS, 1.62, 2.40, -3.5, 224.5),
        _event(
            "e2", "Los Angeles Clippers", "Denver Nuggets", TIP_CLIPPERS, 2.30, 1.65, 4.5, 228.5
        ),
    ]


def _tw_games() -> list[taiwan.TwGame]:
    return taiwan.parse_games(
        json.loads((FIXTURES / "tw_basketball.json").read_text(encoding="utf-8"))
    )


def _compared_cards(captured: datetime = datetime(2026, 10, 27, 12, 15, tzinfo=UTC)):
    cards = analysis.build_cards(_games(), _events(), CFG)
    compare.annotate(
        cards, _tw_games(), captured, replace(CFG, value_threshold=CFG.nba_value_threshold)
    )
    return cards


class ScoreboardTests(unittest.TestCase):
    def test_parse_fixture(self):
        games = _games()
        self.assertEqual([g.game_id for g in games], ["401810001", "401810002"])  # broken skipped
        knicks, clippers = games
        self.assertEqual(
            (knicks.home, knicks.away, knicks.state), ("New York Knicks", "Boston Celtics", "pre")
        )
        self.assertEqual(knicks.tipoff, TIP_KNICKS)
        self.assertIsNone(knicks.home_score)
        self.assertFalse(knicks.is_final)
        self.assertTrue(clippers.is_final)
        self.assertEqual((clippers.home_score, clippers.away_score), (104, 112))
        self.assertEqual(clippers.home_quarters, (28, 22, 30, 24))
        self.assertEqual(clippers.venue, "Intuit Dome")

    def test_espn_short_names_match_the_odds_api(self):
        cards = analysis.build_cards(_games(), _events(), CFG)
        self.assertEqual(cards[1]["home_key"], "los angeles clippers")
        self.assertIsNotNone(cards[1]["market"])


class MarginModelTests(unittest.TestCase):
    def test_hand_checked_numbers(self):
        # A 50/50 line at -5.5 means an expected home margin of 5.5.
        mu = margin.mean_from_cover(-5.5, 0.5, 12.5)
        self.assertAlmostEqual(mu, 5.5, places=6)
        # Moving the line to -6.5: home covers when M >= 7, 1 - Phi((6.5 - 5.5) / 12.5).
        home, push, away = margin.cover_probs(5.5, 12.5, -6.5)
        self.assertAlmostEqual(home, 0.4681, places=4)
        self.assertEqual(push, 0.0)
        # A whole-number line can push: P(M == 6) = Phi(0.08) - Phi(0).
        home, push, away = margin.cover_probs(5.5, 12.5, -6.0)
        self.assertAlmostEqual(push, 0.0319, places=4)
        self.assertAlmostEqual(home + push + away, 1.0, places=9)
        self.assertAlmostEqual(margin.conditional(home, push), 0.4681 / (1 - 0.0319), places=3)
        # No ties: P(home wins) = P(M >= 1).
        self.assertAlmostEqual(margin.win_prob(5.5, 12.5), 0.6554, places=4)
        # The mean solved from a whole-number line (bisection) is exact too.
        self.assertAlmostEqual(margin.mean_from_cover(-6.0, 0.5, 12.5), 6.0, places=4)
        # Totals: the same maths on the total.
        mu_t = margin.mean_from_over(224.5, 0.5, 18.0)
        self.assertAlmostEqual(mu_t, 224.5, places=6)
        over, _, under = margin.over_probs(mu_t, 18.0, 225.5)
        self.assertAlmostEqual(over, 0.4778, places=4)
        self.assertEqual(margin.expected_score(5.5, 224.5), (115.0, 109.5))


class CardTests(unittest.TestCase):
    def test_two_way_market_and_shape(self):
        cards = analysis.build_cards(_games(), _events(), CFG)
        c = cards[0]
        self.assertEqual(c["sport"], "nba")
        h2h = c["market"]["h2h"]
        self.assertNotIn("draw", h2h)
        self.assertAlmostEqual(h2h["home"]["fair"] + h2h["away"]["fair"], 1.0, places=3)
        self.assertEqual(c["market"]["spread"]["line"], -3.5)
        self.assertEqual(c["market"]["totals"]["line"], 224.5)
        shape = c["shape"]
        # 50/50 at -3.5 and 224.5: margin 3.5, total 224.5 -> 114.0 : 110.5.
        self.assertEqual(shape["favourite"], "home")
        self.assertEqual((shape["line"], shape["total"]), (-3.5, 224.5))
        self.assertEqual((shape["home_pts"], shape["away_pts"]), (114.0, 110.5))
        self.assertEqual(
            site.nba_shape_text(shape, "尼克", "塞爾提克")[:14], "國際盤：尼克 讓 3.5 分"
        )

    def test_no_event_gives_note(self):
        cards = analysis.build_cards(_games(), [], CFG)
        self.assertIsNone(cards[0]["market"])
        self.assertIsNone(cards[0]["shape"])
        self.assertTrue(cards[0]["notes"])


class DecisionTests(unittest.TestCase):
    def test_direct_markets_and_translated_lines(self):
        cards = _compared_cards()
        tw = cards[0]["tw"]
        self.assertEqual((tw["home_zh"], tw["away_zh"]), ("紐約尼克", "波士頓塞爾提克"))
        # 運彩 1.78 on the Knicks against a fair ~0.60: EV about +7%, the value bet.
        home = tw["h2h"]["home"]
        self.assertAlmostEqual(home["ev"], home["fair_prob"] * 1.78 - 1, places=3)
        self.assertGreater(home["ev"], CFG.nba_value_threshold)
        self.assertEqual([v["selection"] for v in tw["values"]], ["主勝（紐約尼克）"])
        # The handicap is at the international line (-3.5): compared directly, labelled
        # with each team's own handicap.
        self.assertEqual(tw["spread"]["line"], -3.5)
        # 運彩's totals line 225.5 differs from the market's 224.5: translated by the
        # normal model (P(T > 225.5) = 0.4778 when the market's total is 224.5).
        self.assertIsNone(tw["totals"])
        derived = {d["selection_key"]: d for d in tw["derived"]}
        self.assertEqual(set(derived), {"over", "under"})
        self.assertAlmostEqual(derived["over"]["fair_prob"], 0.4778, places=3)
        self.assertEqual(derived["under"]["selection"], "小 225.5")
        self.assertTrue(all(d["derived"] for d in tw["derived"]))

        d = analysis.decision(cards[0], CFG)
        self.assertEqual(
            (d["status"], d["pick"]["key"], d["pick"]["label"]),
            ("value", "home", "紐約尼克 不讓分"),
        )
        self.assertEqual(
            [b["label"] for b in d["bets"]][:4],
            ["紐約尼克 不讓分", "波士頓塞爾提克 不讓分", "紐約尼克 -3.5", "波士頓塞爾提克 +3.5"],
        )
        self.assertEqual(d["timing"], "early")  # 11 hours before tip-off
        # Clippers: nothing pays; the away side is the favourite (home line +4.5).
        d2 = analysis.decision(cards[1], CFG)
        self.assertEqual(
            (d2["status"], d2["pick"], d2["best"]["label"]), ("pass", None, "丹佛金塊 不讓分")
        )
        self.assertEqual(
            [b["label"] for b in d2["bets"] if b["market"] == "spread"],
            ["洛杉磯快艇 +4.5", "丹佛金塊 -4.5"],
        )

    def test_old_fair_price_is_not_judged(self):
        cards = _compared_cards(captured=datetime(2026, 10, 27, 20, 0, tzinfo=UTC))  # 8 h later
        d = analysis.decision(cards[0], CFG)
        self.assertEqual((d["status"], d["pick"]), ("stale", None))
        self.assertFalse(cards[0]["tw"]["values"])

    def test_translated_handicap_line(self):
        # 運彩 posts -4.5 where the market's 50/50 line is -3.5: home covers 46.8%.
        card = analysis.build_cards(_games(), _events(), CFG)[0]
        game = _tw_games()[0]
        shifted = replace(
            game,
            markets=tuple(
                replace(
                    m,
                    name="讓分 -4.5",
                    line=-4.5,
                    selections=tuple(replace(s, line=-4.5) for s in m.selections),
                )
                if m.kind == "spread"
                else m
                for m in game.markets
            ),
        )
        block = compare.compare(card, shifted, datetime(2026, 10, 27, 12, 15, tzinfo=UTC), CFG)
        self.assertIsNone(block["spread"])
        derived = {d["selection_key"]: d for d in block["derived"]}
        self.assertAlmostEqual(derived["spread_home"]["fair_prob"], 0.4681, places=3)
        self.assertEqual(derived["spread_home"]["selection"], "紐約尼克 -4.5")
        self.assertEqual(derived["spread_away"]["selection"], "波士頓塞爾提克 +4.5")
        # The same translation gives such a bet its closing-line value later.
        self.assertAlmostEqual(
            tw_ledger._fair_prob_for(card["market"], "spread_away", "-4.5", sport="nba"),
            1 - 0.4681,
            places=3,
        )
        self.assertIsNone(tw_ledger._fair_prob_for(card["market"], "spread_away", "-4.5"))


class ReviewTests(unittest.TestCase):
    def _entry(self, cards):
        snap = {"generated_at": "2026-10-27T12:00:00+00:00", "date": "2026-10-27", "games": cards}
        return review.collect_prematch([snap], lambda s: s.get("games", []), "game_id")

    def test_passed_game(self):
        cards = _compared_cards()
        entry = self._entry(cards)["401810002"]
        row = review.nba_row(entry, _games()[1], datetime(2026, 10, 28, 5, 0, tzinfo=UTC))
        self.assertEqual(
            (row["value_status"], row["pick_key"], row["verdict"]), ("pass", "", "value_pass")
        )
        self.assertEqual(
            (row["winner"], row["fav"], row["fav_cover"], row["total_result"]),
            ("away", "away", "1", "under"),
        )
        self.assertEqual(row["quarters_a"], "31 25 27 29")
        x = review.explain_nba(row)
        self.assertEqual(x["label"], "沒有價值，不下")
        self.assertIn("讓分：Denver Nuggets -4.5，實際贏 8 分 → 過盤。", x["bullets"])
        self.assertIn("大小：盤口 228.5，實際總分 216 → 小分。", x["bullets"])
        # The CSV round-trips every column.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nba.csv"
            review_store.upsert(path, [row], review_store.NBA_COLUMNS, "game_id")
            back = review_store.read(path)
        self.assertEqual(back[0]["verdict"], "value_pass")
        self.assertEqual(review.assess_nba(back[0])["fav_cover"], "1")

    def test_value_bet_is_graded(self):
        cards = _compared_cards()
        entry = self._entry(cards)["401810001"]
        final = replace(_games()[0], state="post", status="Final", home_score=110, away_score=105)
        row = review.nba_row(entry, final, datetime(2026, 10, 28, 3, 0, tzinfo=UTC))
        self.assertEqual(
            (row["value_status"], row["pick_key"], row["pick_price"]), ("value", "home", 1.78)
        )
        self.assertEqual((row["pick_result"], row["verdict"]), ("win", "value_win"))
        pick = review.pick_from_value(
            {"selection": "主勝（紐約尼克）", "selection_key": "home", "line": "", "tw_price": "1.78",
             "fair_prob": "0.5986", "ev": "0.0655", "status": "pending", "captured_at": "x"}
        )  # fmt: skip
        x = review.explain_nba(row, [pick])
        self.assertEqual(x["picks"][0]["source_label"], "NBA 可下")
        self.assertEqual(x["picks"][0]["result"], "win")
        s = review.summarize_nba([row])
        self.assertEqual((s["bets"], s["wins"], s["units"]), (1, 1, 0.78))


class PageTests(unittest.TestCase):
    def test_nba_pages_render(self):
        cards = _compared_cards()
        snap = {
            "generated_at": "2026-10-27T12:00:00+00:00",
            "date": "2026-10-27",
            "games": cards,
            "tw_report": {"file": "2026-10-27-2015.json", "tw_games": 2, "matched": 2, "avg_overround": 0.076, "unknown_teams": []},
        }  # fmt: skip
        entry = review.collect_prematch([snap], lambda s: s.get("games", []), "game_id")[
            "401810002"
        ]
        row = review.nba_row(entry, _games()[1], datetime(2026, 10, 28, 5, 0, tzinfo=UTC))
        row = {k: ("" if v is None else str(v)) for k, v in row.items()}
        tape = pipeline.moves_mod.intl_rows(cards, "nba") + pipeline.moves_mod.tw_rows(cards, "nba")
        self.assertEqual({r["match_id"] for r in tape}, {"401810001", "401810002"})
        s = site.Site(
            now=datetime(2026, 10, 27, 13, 0, tzinfo=UTC),
            football=None,
            mlb=None,
            nba=snap,
            tape=tape,
            nba_reviews=[row],
        )
        html = site.render_page(s, "nba")
        for text in (
            "可下",
            "紐約尼克 不讓分",
            "換算",
            "國際盤：",
            "Denver Nuggets",
            "沒有價值，不下",
        ):
            self.assertIn(text, html)
        index = site.render_page(s, "index")
        self.assertIn("波士頓塞爾提克 @ 紐約尼克", index)
        self.assertIn('href="value.html#m-nba-401810001"', index)
        reviews = site.render_page(s, "reviews")
        self.assertIn('id="m-nba-401810002"', reviews)
        # The value page lists the NBA comparison too; a 可下 card renders (regression:
        # `c.values` in the template was the dict method, not the list).
        value = site.render_page(s, "value")
        self.assertIn("主勝（紐約尼克）", value)


class FakeNba:
    """ESPN stand-in: the fixture's games for any date (the Clippers game is final)."""

    def __init__(self, session=None) -> None:
        pass

    def scoreboard(self, day: date) -> list[NbaGame]:
        return _games()


class FakeNbaOdds:
    calls: list[tuple[str, tuple[str, ...]]] = []
    fetched_at = FETCHED

    def __init__(self, api_key: str, regions: str = "eu", session=None) -> None:
        self._used: int | None = None
        self._remaining: int | None = None

    def odds(self, sport_key, *, commence_from=None, commence_to=None, markets=()):
        FakeNbaOdds.calls.append((sport_key, tuple(markets)))
        self._used = (self._used or 100) + len(markets)
        self._remaining = (self._remaining or 400) - len(markets)
        return [replace(e, fetched_at=FakeNbaOdds.fetched_at) for e in _events()]

    def credits_remaining(self):
        return self._remaining

    def credits_used(self):
        return self._used


class PipelineSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {
            "SNAPSHOT_DIR": root / "snapshots",
            "TW_DIR": root / "tw",
            "TW_LEDGER_PATH": root / "tw_ledger.csv",
            "NBA_REVIEWS_PATH": root / "reviews" / "nba.csv",
            "FOOTBALL_REVIEWS_PATH": root / "reviews" / "football.csv",
            "MLB_REVIEWS_PATH": root / "reviews" / "mlb.csv",
            "ODDS_BUDGET_PATH": root / "odds_budget.json",
            "IMPLIED_CALIBRATION_PATH": root / "implied_calibration.json",
            "MOVES_DIR": root / "moves",
            "LEDGER_PATH": root / "ledger.csv",
        }
        self.patches = [mock.patch.object(pipeline, k, v) for k, v in self.paths.items()]
        self.patches += [
            mock.patch.object(pipeline, "NbaClient", FakeNba),
            mock.patch.object(pipeline, "OddsClient", FakeNbaOdds),
        ]
        for p in self.patches:
            p.start()
        FakeNbaOdds.calls = []
        self.settings = Settings("token", "key")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_run_compare_and_review(self):
        snap = pipeline.run_nba(date(2026, 10, 27), self.settings)
        self.assertEqual(FakeNbaOdds.calls, [("basketball_nba", ("h2h", "spreads", "totals"))])
        self.assertEqual(snap["odds_key"], "basketball_nba")
        self.assertEqual(len(snap["games"]), 2)
        self.assertTrue((self.paths["SNAPSHOT_DIR"] / "2026-10-27" / "nba.json").exists())
        taped = pipeline.moves_mod.read(self.paths["MOVES_DIR"])
        self.assertTrue(taped and all(r["sport"] == "nba" for r in taped))

        # The phone uploads the basketball feed 15 minutes after the prices were bought.
        upload = self.paths["TW_DIR"] / "basketball" / "2026-10-27-2015.json"
        upload.parent.mkdir(parents=True)
        upload.write_text(
            (FIXTURES / "tw_basketball.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        now = datetime(2026, 10, 27, 12, 20, tzinfo=UTC)
        rep = pipeline.run_tw(settings=self.settings, now=now)["nba"]
        self.assertEqual((rep["matched"], rep["values_added"]), (2, 1))
        self.assertEqual(rep["pregame"]["due"], 0)  # tip-off is 11 hours away
        rows = tw_ledger.read(self.paths["TW_LEDGER_PATH"])
        self.assertEqual(
            [(r["sport"], r["competition"], r["selection_key"], r["tw_price"]) for r in rows],
            [("nba", "NBA", "home", "1.78")],
        )
        written = json.loads(
            (self.paths["SNAPSHOT_DIR"] / "2026-10-27" / "nba.json").read_text(encoding="utf-8")
        )
        self.assertEqual(written["tw_report"]["matched"], 2)
        self.assertTrue(all(g["tw"] for g in written["games"]))
        # The tape now holds 運彩's prices as well.
        self.assertIn("tw", {r["source"] for r in pipeline.moves_mod.read(self.paths["MOVES_DIR"])})

        # Hours later the Clippers game is final: reviewed, and nothing else yet.
        report = pipeline.run_review(self.settings, now=datetime(2026, 10, 28, 5, 0, tzinfo=UTC))
        self.assertEqual(report["nba"], {"due": 2, "added": 1, "settled": 0})
        rows = review_store.read(self.paths["NBA_REVIEWS_PATH"])
        self.assertEqual(
            [(r["game_id"], r["verdict"]) for r in rows], [("401810002", "value_pass")]
        )
        self.assertTrue(pipeline.review_changed(report))

        # Every page comes out of the real data files; the reviewed game is on the NBA
        # page's history and on the reviews page (what is upcoming depends on the clock).
        with mock.patch.object(pipeline, "DOCS_DIR", Path(self.tmp.name) / "docs"):
            out = pipeline.render_latest()
        self.assertTrue(out.exists())
        nba_html = (out.parent / "nba.html").read_text(encoding="utf-8")
        self.assertIn("沒有價值，不下", nba_html)
        self.assertIn("Denver Nuggets", nba_html)
        self.assertIn(
            'id="m-nba-401810002"', (out.parent / "reviews.html").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
