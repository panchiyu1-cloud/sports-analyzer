"""Offline end-to-end runs of the football pipeline with fake API clients.

They exercise the code that only runs on GitHub Actions (odds purchase under the
credit budget, snapshot writing, carrying prices over when nothing is bought).
"""

import json
import tempfile
import unittest
from datetime import date
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
