"""Offline end-to-end runs of the football pipeline with fake API clients.

They exercise the code that only runs on GitHub Actions (odds purchase under the
credit budget, snapshot writing, carrying prices over when nothing is bought).
"""

import json
import tempfile
import unittest
from datetime import UTC, date, datetime
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
        }
        self.patches = [mock.patch.object(pipeline, k, v) for k, v in self.paths.items()]
        self.patches.append(mock.patch.object(pipeline, "OddsClient", FakeMlbOdds))
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
        self.settings = Settings("token", "key")

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
