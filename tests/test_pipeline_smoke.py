"""Offline end-to-end runs of the football pipeline with fake API clients.

They exercise the code that only runs on GitHub Actions (odds purchase under the
credit budget, snapshot writing, carrying prices over when nothing is bought).
"""

import dataclasses
import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

from sports_analyzer import budget, pipeline
from sports_analyzer.config import Settings
from sports_analyzer.fetch import football, odds
from tests import conftest  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"


class FakeFootballData:
    def __init__(self, token: str, session=None) -> None:
        payload = json.loads((FIXTURES / "fd_matches.json").read_text(encoding="utf-8"))
        self.matches = football.parse_matches(payload)
        self.table = football.parse_standings(
            json.loads((FIXTURES / "fd_standings.json").read_text(encoding="utf-8"))
        )

    def upcoming(self, code, start, days):
        return (
            [m for m in self.matches if m.status in football.UPCOMING_STATUSES]
            if code == "PL"
            else []
        )

    def results(self, code, season=None):
        return [m for m in self.matches if m.is_finished] if code == "PL" else []

    def standings(self, code):
        return self.table if code == "PL" else []


class FakeOdds:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def __init__(self, api_key: str, regions: str = "eu", session=None) -> None:
        # Like the real client: no counters until a response has been seen.
        self._used: int | None = None
        self._remaining: int | None = None

    def odds(self, sport_key, *, commence_from=None, commence_to=None, markets=()):
        FakeOdds.calls.append((sport_key, tuple(markets)))
        self._used = (self._used or 100) + len(markets)
        self._remaining = (self._remaining or 400) - len(markets)
        payload = json.loads((FIXTURES / "odds_events.json").read_text(encoding="utf-8"))
        for event in payload:  # the API only returns the markets asked for
            for book in event.get("bookmakers", []):
                book["markets"] = [m for m in book.get("markets", []) if m["key"] in markets]
        return odds.parse_events(payload)

    def credits_remaining(self):
        return self._remaining

    def credits_used(self):
        return self._used


class FootballPipelineSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "history").mkdir()
        self.paths = {
            "SNAPSHOT_DIR": root / "snapshots",
            "HISTORY_DIR": root / "history",
            "LEDGER_PATH": root / "ledger.csv",
            "TW_LEDGER_PATH": root / "tw_ledger.csv",
            "ODDS_BUDGET_PATH": root / "odds_budget.json",
            "IMPLIED_CALIBRATION_PATH": root / "implied_calibration.json",
            "MOVES_DIR": root / "moves",
        }
        self.patches = [mock.patch.object(pipeline, k, v) for k, v in self.paths.items()]
        self.patches += [
            mock.patch.object(pipeline, "FootballDataClient", FakeFootballData),
            mock.patch.object(pipeline, "OddsClient", FakeOdds),
        ]
        for p in self.patches:
            p.start()
        FakeOdds.calls = []
        self.settings = Settings("token", "key", fixture_window_days=2)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_buys_h2h_and_totals_and_records_the_budget(self):
        snap = pipeline.run_football(self.settings, date(2026, 10, 16))
        self.assertEqual(FakeOdds.calls, [("soccer_epl", ("h2h", "totals"))])
        taped = pipeline.moves_mod.read(self.paths["MOVES_DIR"])
        self.assertTrue(taped and all(r["source"] == "intl" for r in taped))
        self.assertEqual(snap["odds_markets"], ["h2h", "totals"])
        self.assertEqual(snap["credits_remaining"], 398)
        state = budget.load(self.paths["ODDS_BUDGET_PATH"], date(2026, 10, 16))
        self.assertEqual((state.used, state.remaining), (102, 398))
        written = self.paths["SNAPSHOT_DIR"] / "2026-10-16" / "football.json"
        self.assertTrue(written.exists())
        cards = [c for comp in snap["competitions"] for c in comp["cards"]]
        self.assertTrue(any(c["market"] and c["market"]["h2h"] for c in cards))
        self.assertTrue(all(c["market"] is None or c["market"]["spread"] is None for c in cards))

    def test_out_of_budget_keeps_the_last_prices_as_stale(self):
        pipeline.run_football(self.settings, date(2026, 10, 16))
        # Next day, almost nothing left: the run buys nothing and carries yesterday's prices.
        budget.save(
            self.paths["ODDS_BUDGET_PATH"],
            budget.BudgetState(
                "2026-10-12", used=474, remaining=26, day="2026-10-16", day_start_used=100
            ),
        )
        FakeOdds.calls = []
        snap = pipeline.run_football(self.settings, date(2026, 10, 17))
        self.assertEqual(FakeOdds.calls, [])
        self.assertEqual(snap["odds_markets"], [])
        cards = [c for comp in snap["competitions"] for c in comp["cards"] if c["market"]]
        self.assertTrue(cards)
        self.assertTrue(all(c["market"]["h2h"].get("stale") for c in cards if c["market"]["h2h"]))
        self.assertEqual(snap["credits_remaining"], 26)


if __name__ == "__main__":
    unittest.main()


class FakeMlbOdds:
    """The Odds API for one MLB game; `fetched_at` is set by the test (the run's clock)."""

    calls: list[tuple[str, tuple[str, ...]]] = []
    fetched_at = None

    def __init__(self, api_key: str, regions: str = "eu", session=None) -> None:
        self._used: int | None = None
        self._remaining: int | None = None

    def odds(self, sport_key, *, commence_from=None, commence_to=None, markets=()):
        FakeMlbOdds.calls.append((sport_key, tuple(markets)))
        self._used = (self._used or 100) + len(markets)
        self._remaining = (self._remaining or 400) - len(markets)
        books = tuple(
            odds.BookOdds(
                name,
                {"home": 1.55, "away": 2.55},
                (odds.SpreadLine(-1.5, 2.20, 1.72),),
                (odds.TotalLine(7.5, 1.90, 1.92),),
            )
            for name in ("pinnacle", "betfair_ex_eu")
        )
        return [
            odds.OddsEvent(
                "e1",
                "baseball_mlb",
                datetime(2026, 10, 14, 0, 0, tzinfo=UTC),
                "Los Angeles Dodgers",
                "Philadelphia Phillies",
                books,
                fetched_at=FakeMlbOdds.fetched_at,
            )
        ]

    def credits_remaining(self):
        return self._remaining

    def credits_used(self):
        return self._used


class PregameRefreshSmokeTests(unittest.TestCase):
    """A 運彩 upload 40 minutes before first pitch buys fresh MLB prices first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {
            "SNAPSHOT_DIR": root / "snapshots",
            "TW_DIR": root / "tw",
            "TW_LEDGER_PATH": root / "tw_ledger.csv",
            "ODDS_BUDGET_PATH": root / "odds_budget.json",
            "IMPLIED_CALIBRATION_PATH": root / "implied_calibration.json",
            "MOVES_DIR": root / "moves",
        }
        self.patches = [mock.patch.object(pipeline, k, v) for k, v in self.paths.items()]
        self.patches.append(mock.patch.object(pipeline, "OddsClient", FakeMlbOdds))
        self.sent: list[tuple[str, str, str]] = []
        self.patches.append(
            mock.patch.object(
                pipeline.notify,
                "send",
                lambda topic, title, body, session=None: (
                    self.sent.append((topic, title, body)) or True
                ),
            )
        )
        for p in self.patches:
            p.start()
        FakeMlbOdds.calls = []
        # Last evening's snapshot holds the game (international prices from 20:25 Taipei);
        # this morning's run already wrote tomorrow's games into a newer one.
        card = {
            "sport": "mlb",
            "game_pk": 1,
            "kickoff_utc": "2026-10-14T00:00:00+00:00",
            "local": "10/14 08:00",
            "status": "Scheduled",
            "home": "Los Angeles Dodgers",
            "away": "Philadelphia Phillies",
            "home_key": "los angeles dodgers",
            "away_key": "philadelphia phillies",
            "market": {
                "books": 2,
                "h2h": {
                    "home": {"fair": 0.62, "fair_odds": 1.61},
                    "away": {"fair": 0.38, "fair_odds": 2.63},
                    "odds_at": "2026-10-13T12:25+00:00",
                },
                "spread": None,
                "totals": None,
            },
            "t10": None,
            "notes": ["賠率為稍早快照（比賽可能已開打）"],
        }
        self._snapshot("2026-10-13", "2026-10-13T12:26:00+00:00", [card])
        self._snapshot("2026-10-14", "2026-10-13T23:00:00+00:00", [])
        upload = self.paths["TW_DIR"] / "baseball" / "2026-10-14-0720.json"  # 23:20 UTC
        upload.parent.mkdir(parents=True)
        upload.write_text(
            (FIXTURES / "tw_games.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.settings = Settings("token", "key", ntfy_topic="test-topic")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _snapshot(self, day, made, games):
        folder = self.paths["SNAPSHOT_DIR"] / day
        folder.mkdir(parents=True)
        payload = {"generated_at": made, "date": day, "games": games}
        (folder / "mlb.json").write_text(json.dumps(payload), encoding="utf-8")

    def _card(self):
        snap = json.loads(
            (self.paths["SNAPSHOT_DIR"] / "2026-10-13" / "mlb.json").read_text(encoding="utf-8")
        )
        return snap["games"][0]

    def test_upload_before_first_pitch_reprices_and_judges(self):
        now = datetime(2026, 10, 13, 23, 21, tzinfo=UTC)
        FakeMlbOdds.fetched_at = now
        rep = pipeline.run_tw(settings=self.settings, now=now)["mlb"]
        self.assertEqual(FakeMlbOdds.calls, [("baseball_mlb", ("h2h", "spreads", "totals"))])
        self.assertEqual(
            rep["pregame"], {"due": 1, "refreshed": 1, "markets": ["h2h", "spreads", "totals"]}
        )
        card = self._card()
        self.assertEqual(card["market"]["h2h"]["odds_at"], "2026-10-13T23:21+00:00")
        self.assertIsNotNone(card["t10"])
        self.assertEqual(card["notes"], [])
        # 運彩 Dodgers -1.5 at 2.50 against a fair cover chance of ~0.44: a value bet,
        # judged 40 minutes before first pitch.
        t = card["t10v"]
        self.assertEqual(
            (t["status"], t["pick"]["key"], t["timing"]), ("value", "fav_rl", "pregame")
        )
        self.assertEqual(t["minutes_before"], 40)
        rows = pipeline.tw_ledger.read(self.paths["TW_LEDGER_PATH"])
        self.assertEqual([(r["sport"], r["selection_key"]) for r in rows], [("mlb", "spread_home")])
        self.assertEqual(rows[0]["clv"], "")
        # The new value bet went to the phone, once.
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "test-topic")
        self.assertIn("運彩價值注", self.sent[0][1])
        # Both price sets are on the tape: the evening consensus, the fresh one, 運彩.
        taped = pipeline.moves_mod.read(self.paths["MOVES_DIR"])
        self.assertEqual({r["source"] for r in taped}, {"intl", "tw"})
        self.assertIn("2026-10-13T23:21+00:00", {r["observed_at"] for r in taped})
        state = budget.load(self.paths["ODDS_BUDGET_PATH"], now.date())
        self.assertEqual((state.used, state.remaining), (103, 397))

        # 25 minutes later the prices are old enough to buy again: the earlier bet gets
        # its closing-line value from them, and is not recorded twice.
        later = datetime(2026, 10, 13, 23, 46, tzinfo=UTC)
        FakeMlbOdds.fetched_at = later
        pipeline.run_tw(settings=self.settings, now=later)
        self.assertEqual(len(FakeMlbOdds.calls), 2)
        rows = pipeline.tw_ledger.read(self.paths["TW_LEDGER_PATH"])
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["clv"], "")
        self.assertEqual(len(self.sent), 1)  # the same bet is not announced twice

        # Five minutes after that the prices are fresh: nothing is bought.
        pipeline.run_tw(settings=self.settings, now=datetime(2026, 10, 13, 23, 51, tzinfo=UTC))
        self.assertEqual(len(FakeMlbOdds.calls), 2)

    def test_an_old_upload_buys_nothing(self):
        now = datetime(2026, 10, 13, 23, 59, tzinfo=UTC)
        upload = self.paths["TW_DIR"] / "baseball" / "2026-10-14-0720.json"
        upload.rename(upload.with_name("2026-10-14-0510.json"))  # 21:10 UTC: 2h49m old
        rep = pipeline.run_tw(settings=self.settings, now=now)["mlb"]
        self.assertEqual(FakeMlbOdds.calls, [])
        self.assertEqual(rep["pregame"]["skipped"], "upload not recent")
        # The comparison still runs, but against prices 9 hours from the 運彩 capture.
        self.assertEqual(self._card()["t10v"]["status"], "stale")


class BigHandRecordSmokeTests(unittest.TestCase):
    """Every render records the 大戶 moves on the tape and writes docs/bighand.html."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {
            "DATA_DIR": root,
            "DOCS_DIR": root / "docs",
            "SNAPSHOT_DIR": root / "snapshots",
            "MOVES_DIR": root / "moves",
            "LEDGER_PATH": root / "ledger.csv",
            "TW_LEDGER_PATH": root / "tw_ledger.csv",
            "ODDS_BUDGET_PATH": root / "odds_budget.json",
            "FOOTBALL_REVIEWS_PATH": root / "reviews" / "football.csv",
            "MLB_REVIEWS_PATH": root / "reviews" / "mlb.csv",
        }
        self.patches = [mock.patch.object(pipeline, k, v) for k, v in self.paths.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    @staticmethod
    def card(home: float, odds_at: str, tw_home: float, tw_away: float, captured: str) -> dict:
        return {
            "game_pk": 813024,
            "kickoff_utc": "2099-10-15T00:00:00+00:00",
            "local": "10/15 08:00",
            "home": "Los Angeles Dodgers",
            "away": "Milwaukee Brewers",
            "market": {
                "h2h": {
                    "home": {"fair": home, "fair_odds": round(1 / home, 2)},
                    "away": {"fair": round(1 - home, 4), "fair_odds": round(1 / (1 - home), 2)},
                    "odds_at": odds_at,
                },
                "spread": None,
                "totals": None,
            },
            "tw": {
                "captured_at": captured,
                "home_zh": "洛杉磯道奇",
                "away_zh": "密爾瓦基釀酒人",
                "h2h": {"home": {"price": tw_home}, "away": {"price": tw_away}},
            },
        }

    def test_render_records_the_move_with_its_chinese_names_and_lists_it(self):
        morning = self.card(0.6203, "2026-10-14T00:50+00:00", 1.44, 2.19, "2026-10-14T08:00+08:00")
        evening = self.card(0.6213, "2026-10-14T12:53+00:00", 1.40, 2.31, "2026-10-14T20:00+08:00")
        for c in (morning, evening):
            pipeline.moves_mod.record(
                self.paths["MOVES_DIR"], pipeline.moves_mod.intl_rows([c], "mlb")
            )
            pipeline.moves_mod.record(
                self.paths["MOVES_DIR"], pipeline.moves_mod.tw_rows([c], "mlb")
            )
        folder = self.paths["SNAPSHOT_DIR"] / "2026-10-14"
        folder.mkdir(parents=True)
        (folder / "mlb.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-10-14T12:53:00+00:00",
                    "date": "2026-10-14",
                    "games": [evening],
                }
            ),
            encoding="utf-8",
        )
        pipeline.render_latest()
        pipeline.render_latest()  # a second run records nothing new
        saved = pipeline.moves_mod.read_events(self.paths["MOVES_DIR"] / "events.csv")
        self.assertEqual(
            [(e["type"], e["home_zh"], e["competition"]) for e in saved],
            [("tw_only", "洛杉磯道奇", "MLB")],
        )
        docs = self.paths["DOCS_DIR"]
        # Still to play: an analysis card on 盤口移動 with the 運彩-only move, in 運彩's names,
        # counted on the front page's tile.
        moves_page = (docs / "moves.html").read_text(encoding="utf-8")
        self.assertIn('id="m-mlb-813024"', moves_page)
        self.assertIn("<span>密爾瓦基釀酒人</span><i>@</i><span>洛杉磯道奇</span>", moves_page)
        self.assertIn("<dt>運彩大戶</dt><dd>洛杉磯道奇 不讓分", moves_page)
        index = (docs / "index.html").read_text(encoding="utf-8")
        self.assertIn("有在動的比賽 1 場", index)
        # The old 大戶紀錄 address sends the owner's bookmark to the new history.
        self.assertIn("moves.html#history", (docs / "bighand.html").read_text(encoding="utf-8"))
        for name in ("value", "t10", "radar", "model", "fade", "reviews"):
            self.assertTrue((docs / f"{name}.html").exists(), name)


class FakeRadarOdds(FakeMlbOdds):
    """FakeMlbOdds whose game starts when the test's card does."""

    commence = None

    def odds(self, sport_key, *, commence_from=None, commence_to=None, markets=()):
        events = super().odds(sport_key, markets=markets)
        return [dataclasses.replace(e, commence_time=FakeRadarOdds.commence) for e in events]


class RadarSmokeTests(unittest.TestCase):
    """`pipeline radar` on a schedule: no upload for 7 hours, the game starts in 2."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {
            "DATA_DIR": root,
            "DOCS_DIR": root / "docs",
            "SNAPSHOT_DIR": root / "snapshots",
            "TW_DIR": root / "tw",
            "MOVES_DIR": root / "moves",
            "LEDGER_PATH": root / "ledger.csv",
            "TW_LEDGER_PATH": root / "tw_ledger.csv",
            "ODDS_BUDGET_PATH": root / "odds_budget.json",
            "IMPLIED_CALIBRATION_PATH": root / "implied_calibration.json",
            "FOOTBALL_REVIEWS_PATH": root / "reviews" / "football.csv",
            "MLB_REVIEWS_PATH": root / "reviews" / "mlb.csv",
        }
        self.patches = [mock.patch.object(pipeline, k, v) for k, v in self.paths.items()]
        self.patches.append(mock.patch.object(pipeline, "OddsClient", FakeRadarOdds))
        for p in self.patches:
            p.start()
        FakeMlbOdds.calls = []
        # Real time: the render judges "not started yet" by the clock.
        self.now = datetime.now(UTC).replace(second=0, microsecond=0)
        kickoff = self.now + timedelta(hours=2)
        FakeRadarOdds.commence, FakeMlbOdds.fetched_at = kickoff, self.now
        self.card = {
            "sport": "mlb",
            "game_pk": 1,
            "kickoff_utc": kickoff.isoformat(),
            "home": "Los Angeles Dodgers",
            "away": "Philadelphia Phillies",
            "home_key": "los angeles dodgers",
            "away_key": "philadelphia phillies",
            "market": {
                "h2h": {
                    "home": {"fair": 0.55, "fair_odds": 1.82},
                    "away": {"fair": 0.45, "fair_odds": 2.22},
                    "odds_at": (self.now - timedelta(hours=6)).isoformat(),
                },
                "spread": None,
                "totals": None,
            },
            "t10": None,
            "tw": {
                "captured_at": (self.now - timedelta(hours=7)).isoformat(),
                "home_zh": "洛杉磯道奇",
                "away_zh": "費城費城人",
                "h2h": {"home": {"price": 1.75}, "away": {"price": 2.10}},
            },
        }
        folder = self.paths["SNAPSHOT_DIR"] / self.now.date().isoformat()
        folder.mkdir(parents=True)
        payload = {"generated_at": self.card["market"]["h2h"]["odds_at"],
                   "date": self.now.date().isoformat(), "games": [self.card]}  # fmt: skip
        (folder / "mlb.json").write_text(json.dumps(payload), encoding="utf-8")
        tape = pipeline.moves_mod
        tape.record(self.paths["MOVES_DIR"], tape.intl_rows([self.card], "mlb"))
        tape.record(self.paths["MOVES_DIR"], tape.tw_rows([self.card], "mlb"))
        self.output = Path(self.tmp.name) / "github_output"

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_scheduled_purchase_finds_the_stale_tw_price(self):
        env = {"ODDS_API_KEY": "key", "GITHUB_OUTPUT": str(self.output)}
        with mock.patch.dict("os.environ", env):
            self.assertEqual(pipeline.main(["radar"]), 0)
        # Essential markets only: the moneyline and the run line (the two halves of G).
        self.assertEqual(FakeMlbOdds.calls, [("baseball_mlb", ("h2h", "spreads"))])
        self.assertEqual(self.output.read_text(), "refreshed=1\n")
        # 1.75 against a fresh fair ~62%: the upload 7 hours ago is behind the market.
        radar = (self.paths["DOCS_DIR"] / "radar.html").read_text(encoding="utf-8")
        self.assertIn("費城費城人 @ 洛杉磯道奇", radar)
        self.assertIn('<span class="chip good">候選</span><b>洛杉磯道奇 不讓分</b>', radar)
        saved = pipeline.moves_mod.read_events(self.paths["MOVES_DIR"] / "events.csv")
        self.assertEqual([(e["type"], e["home_zh"]) for e in saved], [("radar", "洛杉磯道奇")])
        index = (self.paths["DOCS_DIR"] / "index.html").read_text(encoding="utf-8")
        self.assertIn("雷達有 1 個候選", index)
        # The next scheduled run finds the prices recent: nothing bought, nothing rendered.
        with mock.patch.dict("os.environ", env):
            pipeline.main(["radar"])
        self.assertEqual(len(FakeMlbOdds.calls), 1)
        self.assertEqual(self.output.read_text(), "refreshed=1\nrefreshed=0\n")


class RenderFailureTests(unittest.TestCase):
    def test_a_broken_page_does_not_fail_the_task(self):
        # Regression guard (2026-10-16): every task ends with a render after its data is
        # written; a render error must not stop the workflow from committing that data.
        def broken():
            raise RuntimeError("template bug")

        with mock.patch.object(pipeline, "render_latest", broken):
            with self.assertLogs("sports_analyzer", "ERROR") as logs:
                self.assertEqual(pipeline.main(["render"]), 0)
        self.assertIn("render failed", logs.output[0])


class FakeResults:
    """football-data.org with one finished CL match (id 7): 2-1, 1-0 at half-time."""

    calls: list[str] = []

    def __init__(self, token: str, session=None) -> None:
        pass

    def results(self, code, season=None):
        FakeResults.calls.append(code)
        return [
            football.Match(
                7,
                "CL",
                datetime(2026, 10, 14, 19, 0, tzinfo=UTC),
                "FINISHED",
                3,
                "AS Roma",
                "Real Madrid CF",
                2,
                1,
                1,
                0,
            )
        ]


class ReviewSmokeTests(unittest.TestCase):
    """The review settles both ledgers and closes each 模型分歧 for every finished match."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "history").mkdir()
        self.paths = {
            "SNAPSHOT_DIR": root / "snapshots",
            "HISTORY_DIR": root / "history",
            "LEDGER_PATH": root / "ledger.csv",
            "TW_LEDGER_PATH": root / "tw_ledger.csv",
            "FOOTBALL_REVIEWS_PATH": root / "reviews" / "football.csv",
            "MLB_REVIEWS_PATH": root / "reviews" / "mlb.csv",
            "IMPLIED_CALIBRATION_PATH": root / "implied_calibration.json",
        }
        self.patches = [mock.patch.object(pipeline, k, v) for k, v in self.paths.items()]
        self.patches.append(mock.patch.object(pipeline, "FootballDataClient", FakeResults))
        for p in self.patches:
            p.start()
        FakeResults.calls = []

        def block(home, draw, away):
            return {
                "home": {"fair": home, "fair_odds": round(1 / home, 2)},
                "draw": {"fair": draw, "fair_odds": round(1 / draw, 2)},
                "away": {"fair": away, "fair_odds": round(1 / away, 2)},
                "odds_at": "2026-10-14T12:25+00:00",
            }

        card = {
            "sport": "football",
            "competition": "CL",
            "competition_name": "歐冠",
            "match_id": 7,
            "kickoff_utc": "2026-10-14T19:00:00+00:00",
            "kickoff_local": "10/15 (週四) 03:00",
            "home": "AS Roma",
            "away": "Real Madrid CF",
            "model": {},
            "market": {
                "h2h": block(0.30, 0.25, 0.45),
                "totals": {"line": 2.5, "over": {"fair": 0.55}, "under": {"fair": 0.45}},
            },
            "notes": [],
        }
        folder = self.paths["SNAPSHOT_DIR"] / "2026-10-14"
        folder.mkdir(parents=True)
        (folder / "football.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-10-14T12:30:00+00:00",
                    "date": "2026-10-14",
                    "competitions": [{"code": "CL", "name": "歐冠", "cards": [card]}],
                }
            ),
            encoding="utf-8",
        )
        pipeline.ledger_mod.write(
            self.paths["LEDGER_PATH"],
            [
                {
                    "id": "s1",
                    "created_at": "2026-10-14T00:50:25+00:00",
                    "sport": "football",
                    "match_id": "7",
                    "market": "大小",
                    "selection": "大分 2.5",
                    "selection_key": "over",
                    "line": "2.5",
                    "model_p": "0.66",
                    "market_p": "0.50",
                    "price": "2.0",
                    "status": "pending",
                }
            ],
        )
        pipeline.tw_ledger.write(
            self.paths["TW_LEDGER_PATH"],
            [
                {
                    "id": "v1",
                    "captured_at": "2026-10-14T20:00+08:00",
                    "sport": "football",
                    "match_id": "7",
                    "selection": "主勝",
                    "selection_key": "home",
                    "line": "",
                    "tw_price": "3.6",
                    "fair_prob": "0.30",
                    "ev": "0.08",
                    "status": "pending",
                }
            ],
        )
        self.settings = Settings("token", None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_review_settles_and_closes_every_recommendation(self):
        now = datetime(2026, 10, 14, 22, 30, tzinfo=UTC)  # 06:30 Taipei, the morning after
        rep = pipeline.run_review(self.settings, now=now)["football"]
        self.assertEqual((rep["added"], rep["settled"], rep["signals_closed"]), (1, 2, 1))
        signal = pipeline.ledger_mod.read(self.paths["LEDGER_PATH"])[0]
        # Over 2.5 after 2-1; closed at 55% against a price of 2.0: CLV +10%.
        self.assertEqual((signal["status"], signal["result"]), ("win", "2-1"))
        self.assertEqual((signal["close_p"], signal["clv"]), ("0.5500", "0.1000"))
        value = pipeline.tw_ledger.read(self.paths["TW_LEDGER_PATH"])[0]
        self.assertEqual((value["status"], value["profit"]), ("win", "2.600"))
        self.assertEqual(FakeResults.calls, ["CL"])

        # After a phone upload only matches without a review are looked up: none left.
        rep = pipeline.run_review(self.settings, now=now, new_only=True)
        self.assertEqual(FakeResults.calls, ["CL"])
        self.assertEqual(rep, {})


class EveningMlbMergeTests(unittest.TestCase):
    def test_the_evening_keeps_the_morning_totals_as_stale(self):
        """The evening buys h2h + run line only (evening.yml): the morning's totals stay."""
        morning = {
            "date": "2026-10-17",
            "games": [
                {
                    "game_pk": 9,
                    "market": {
                        "books": 8,
                        "h2h": {"home": 0.55, "away": 0.45},
                        "totals": {"line": 7.5, "over": 0.5, "under": 0.5},
                        "spread": {"home": 0.4, "away": 0.6},
                    },
                    "tw": {"captured_at": "x"},
                }
            ],
        }
        evening = {
            "date": "2026-10-17",
            "games": [
                {
                    "game_pk": 9,
                    "market": {
                        "books": 7,
                        "h2h": {"home": 0.58, "away": 0.42},
                        "totals": None,
                        "spread": {"home": 0.43, "away": 0.57},
                    },
                }
            ],
        }
        pipeline._merge_previous_mlb(evening, morning)
        market = evening["games"][0]["market"]
        self.assertEqual(market["totals"], {"line": 7.5, "over": 0.5, "under": 0.5, "stale": True})
        self.assertEqual(market["h2h"], {"home": 0.58, "away": 0.42})  # the new prices win
        self.assertNotIn("stale", market["spread"])
        self.assertEqual(evening["games"][0]["tw"], {"captured_at": "x"})
