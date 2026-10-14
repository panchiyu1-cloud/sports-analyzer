import json
import unittest
from datetime import UTC
from pathlib import Path

from sports_analyzer import history, teams
from sports_analyzer.fetch import football, odds
from sports_analyzer.http import Session
from tests import conftest  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FootballDataParsingTests(unittest.TestCase):
    def test_parse_matches_skips_broken_rows(self):
        matches = football.parse_matches(load("fd_matches.json"))
        self.assertEqual(len(matches), 5)  # 6 rows, one unparseable
        ids = [m.id for m in matches]
        self.assertIn(500001, ids)
        self.assertNotIn("broken", ids)

    def test_match_fields_and_keys(self):
        m = next(m for m in football.parse_matches(load("fd_matches.json")) if m.id == 500002)
        self.assertEqual(m.home, "Wolverhampton Wanderers FC")
        self.assertEqual(m.home_key, "wolves")
        self.assertEqual(m.away_key, "brighton")
        self.assertEqual(m.utc_date.tzinfo, UTC)
        self.assertEqual(m.utc_date.hour, 15)
        self.assertFalse(m.is_finished)

    def test_finished_match(self):
        m = next(m for m in football.parse_matches(load("fd_matches.json")) if m.id == 499991)
        self.assertTrue(m.is_finished)
        self.assertEqual((m.home_goals, m.away_goals), (2, 0))

    def test_parse_standings_total_table_only(self):
        rows = football.parse_standings(load("fd_standings.json"))
        self.assertEqual(len(rows), 4)
        arsenal = next(r for r in rows if r.team_key == "arsenal")
        self.assertEqual(arsenal.position, 1)
        self.assertEqual(arsenal.form, "W,W,D,W,W")


class OddsParsingTests(unittest.TestCase):
    def test_parse_events(self):
        events = odds.parse_events(load("odds_events.json"))
        self.assertEqual(len(events), 3)
        ars = events[0]
        self.assertEqual(ars.home_key, "arsenal")
        self.assertEqual(ars.away_key, "chelsea")
        self.assertEqual(len(ars.books), 3)
        pin = ars.books[0]
        self.assertEqual(pin.book, "pinnacle")
        self.assertEqual(pin.h2h, {"home": 1.80, "away": 4.60, "draw": 3.90})
        self.assertEqual(pin.spreads[0].point, -0.75)
        self.assertEqual(pin.spreads[0].home_price, 1.96)
        self.assertEqual(pin.totals[0].point, 2.5)

    def test_spreads_grouped_from_home_view(self):
        events = odds.parse_events(load("odds_events.json"))
        unibet = events[0].books[2]
        self.assertEqual(len(unibet.spreads), 1)
        self.assertEqual(unibet.spreads[0], odds.SpreadLine(-0.75, 1.90, 1.98))

    def test_multiple_total_lines_sorted(self):
        events = odds.parse_events(load("odds_events.json"))
        betfair = events[0].books[1]
        self.assertEqual([t.point for t in betfair.totals], [2.0, 2.5])

    def test_event_without_books(self):
        events = odds.parse_events(load("odds_events.json"))
        self.assertEqual(events[2].books, ())


class TeamNameTests(unittest.TestCase):
    def test_three_sources_agree(self):
        self.assertEqual(teams.canonical("Wolverhampton Wanderers FC"), "wolves")
        self.assertEqual(teams.canonical("Wolverhampton Wanderers"), "wolves")
        self.assertEqual(teams.canonical("Wolves"), "wolves")
        self.assertEqual(teams.canonical("Brighton & Hove Albion FC"), "brighton")
        self.assertEqual(teams.canonical("Brighton and Hove Albion"), "brighton")
        self.assertEqual(teams.canonical("Nott'm Forest"), "nottingham forest")
        self.assertEqual(teams.canonical("FC Bayern München"), "bayern munich")
        self.assertEqual(teams.canonical("Borussia Mönchengladbach"), "monchengladbach")
        self.assertEqual(teams.canonical("M'gladbach"), "monchengladbach")
        self.assertEqual(teams.canonical("FC Internazionale Milano"), "inter")
        self.assertEqual(teams.canonical("Inter Milan"), "inter")
        self.assertEqual(teams.canonical("Paris Saint-Germain FC"), "psg")
        self.assertEqual(teams.canonical("Paris SG"), "psg")
        self.assertEqual(teams.canonical("Club Atlético de Madrid"), "atletico madrid")
        self.assertEqual(teams.canonical("Ath Madrid"), "atletico madrid")

    def test_champions_league_and_long_form_names(self):
        pairs = [
            ("Racing Club de Lens", "Lens"),
            ("Sporting Clube de Portugal", "Sporting Lisbon"),
            ("PSV", "PSV Eindhoven"),
            ("FK Shakhtar Donetsk", "Shakhtar Donetsk"),
            ("PAE AEK", "AEK Athens"),
            ("Club Brugge KV", "Club Brugge"),
            ("ŠK Slovan Bratislava", "Slovan Bratislava"),
            ("LASK Linz", "LASK"),
            ("FK Bodø/Glimt", "Bodo/Glimt"),
            ("SK Slavia Praha", "Slavia Prague"),
            ("Racing Club de Strasbourg Alsace", "Strasbourg"),
            ("SC Paderborn 07", "Paderborn"),
            ("RC Deportivo de La Coruña", "La Coruna"),
            ("Coventry City FC", "Coventry"),
            ("ES Troyes AC", "Troyes"),
        ]
        for a, b in pairs:
            self.assertEqual(teams.canonical(a), teams.canonical(b), f"{a!r} vs {b!r}")

    def test_unknown_name_falls_back_to_normalised(self):
        self.assertEqual(teams.canonical("Fictional Town FC"), "fictional town")

    def test_best_match(self):
        self.assertEqual(
            teams.best_match("Manchester Utd", ["manchester united", "manchester city"]),
            "manchester united",
        )
        self.assertIsNone(teams.best_match("Zebra", ["arsenal", "chelsea"]))


class HistoryCsvTests(unittest.TestCase):
    def test_reads_sample_csv(self):
        rows = history.read_csv(FIXTURES / "E0_sample.csv")
        self.assertEqual(len(rows), 6)  # blank last row skipped
        first = rows[0]
        self.assertEqual(first.home, "liverpool")
        self.assertEqual(first.away, "bournemouth")
        self.assertEqual((first.home_goals, first.away_goals), (4, 2))
        self.assertEqual(first.h2h, (1.35, 5.60, 9.10))  # Pinnacle preferred over B365
        self.assertEqual(first.over25, (1.60, 2.46))
        self.assertEqual(first.ah_line, -1.5)
        self.assertEqual(first.ah, (2.03, 1.92))
        self.assertEqual(rows[1].home, "manchester united")
        self.assertEqual(rows[2].home, "nottingham forest")
        self.assertIsNone(first.h2h_close)  # sample file has no closing columns
        self.assertIsNone(first.h2h_max)

    def test_reads_closing_and_max_columns(self):
        import tempfile

        csv_text = (
            "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,PSH,PSD,PSA,PSCH,PSCD,PSCA,MaxH,MaxD,MaxA,P>2.5,P<2.5,PC>2.5,PC<2.5\n"
            "E0,20/09/2025,Arsenal,Chelsea,2,1,1.80,3.90,4.60,1.75,4.00,4.90,1.86,4.10,4.80,1.95,1.95,1.90,2.00\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as fh:
            fh.write(csv_text)
            path = Path(fh.name)
        rows = history.read_csv(path)
        self.assertEqual(rows[0].h2h, (1.80, 3.90, 4.60))
        self.assertEqual(rows[0].h2h_close, (1.75, 4.00, 4.90))
        self.assertEqual(rows[0].h2h_max, (1.86, 4.10, 4.80))
        self.assertEqual(rows[0].over25_close, (1.90, 2.00))

    def test_without_pinnacle_the_exchange_beats_bet365(self):
        # Regression: the 2026/27 files have no Pinnacle columns; the backtest's closing
        # line fell back to Bet365 although Betfair Exchange closing prices are there.
        import tempfile

        csv_text = (
            "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,B365CH,B365CD,B365CA,BFECH,BFECD,BFECA,BFEC>2.5,BFEC<2.5,B365C>2.5,B365C<2.5\n"
            "E0,20/09/2026,Arsenal,Chelsea,2,1,1.70,3.80,4.50,1.78,4.05,4.95,1.96,2.02,1.90,1.90\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as fh:
            fh.write(csv_text)
            path = Path(fh.name)
        rows = history.read_csv(path)
        self.assertEqual(rows[0].h2h_close, (1.78, 4.05, 4.95))
        self.assertEqual(rows[0].over25_close, (1.96, 2.02))


class SessionThrottleTests(unittest.TestCase):
    def test_sleeps_when_minute_quota_exhausted(self):
        slept: list[float] = []
        s = Session(sleep=slept.append)
        s._read_quota_headers({"X-Requests-Available-Minute": "0", "X-RequestCounter-Reset": "7"})
        self.assertEqual(slept, [8])
        self.assertEqual(s.quota.remaining, 0)

    def test_reads_odds_api_headers(self):
        s = Session(sleep=lambda _: None)
        s._read_quota_headers({"x-requests-remaining": "412", "x-requests-last": "3"})
        self.assertEqual(s.quota.remaining, 412)
        self.assertEqual(s.quota.last_cost, 3)

    def test_backoff_prefers_retry_after(self):
        s = Session(sleep=lambda _: None)
        self.assertEqual(s._backoff_seconds({"Retry-After": "10"}, 0), 11)
        self.assertEqual(s._backoff_seconds({}, 2), 8)


if __name__ == "__main__":
    unittest.main()
