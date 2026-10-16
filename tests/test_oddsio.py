"""odds-api.io client: parsing the answers and the requests it sends (no network).

The payloads copy the shape of the real answers (2026-10-16 probe); the prices are
made up, never real odds-api.io data.
"""

import unittest
from datetime import UTC, datetime

from sports_analyzer.fetch import oddsio
from sports_analyzer.http import QuotaState
from tests import conftest  # noqa: F401

EVENTS = [
    {
        "id": 61301,
        "home": "Everton",
        "away": "Chelsea",
        "date": "2026-10-17T11:30:00Z",
        "status": "pending",
    },
    {"id": 61302, "home": "Leeds", "away": "Fulham", "date": "not a date"},
    {"id": 61303, "home": "Burnley", "away": "Wolves", "date": "2026-10-17T14:00:00"},  # no zone
    {"home": "Arsenal", "away": "Brentford", "date": "2026-10-18T13:00:00Z"},  # no id
    "junk",
]

ODDS = [
    {
        "id": 61301,
        "bookmakers": {
            "FanDuel": [
                {"name": "ML", "updatedAt": "2026-10-16T10:00:00Z", "odds": [
                    {"home": "2.10", "draw": "3.40", "away": "3.60"}
                ]},
                {"name": "Totals", "odds": [{"hdp": 2.5, "over": "1.90", "under": "1.90"}]},
            ],
            "Bet365": [
                {"name": "ML", "odds": [{"home": "2.05", "draw": "3.50", "away": "3.70"}]},
            ],
            # A variant of a selected book: it would weigh Bet365 twice.
            "Bet365 (no latency)": [
                {"name": "ML", "odds": [{"home": "9.00", "draw": "9.00", "away": "1.01"}]},
            ],
        },
    },
    {
        "id": 849817,
        "bookmakers": {
            "FanDuel": [{"name": "ML", "odds": [{"home": "1.80", "away": "2.05"}]}],
            "Bet365": [{"name": "ML", "odds": [{"home": "1.00", "away": "abc"}]}],  # unusable
        },
    },
    {"bookmakers": {}},  # no id
]  # fmt: skip


class ParseTests(unittest.TestCase):
    def test_events_keep_only_complete_entries_with_a_time_zone(self):
        events = oddsio.parse_events(EVENTS, "england-premier-league")
        self.assertEqual([e.id for e in events], ["61301"])
        self.assertEqual(events[0].start, datetime(2026, 10, 17, 11, 30, tzinfo=UTC))
        self.assertEqual((events[0].home, events[0].away), ("Everton", "Chelsea"))
        self.assertEqual(oddsio.parse_events({"error": "x"}, "x"), [])

    def test_odds_read_the_selected_books_moneyline_only(self):
        prices = oddsio.parse_odds(ODDS, ["FanDuel", "Bet365"])
        self.assertEqual([p.event_id for p in prices], ["61301", "849817"])
        self.assertEqual(
            prices[0].books,
            {
                "FanDuel": {"home": 2.10, "draw": 3.40, "away": 3.60},
                "Bet365": {"home": 2.05, "draw": 3.50, "away": 3.70},
            },
        )
        # MLB: two sides; a book with no usable price is left out.
        self.assertEqual(prices[1].books, {"FanDuel": {"home": 1.80, "away": 2.05}})
        self.assertEqual(oddsio.parse_odds(None, ["FanDuel"]), [])


class FakeSession:
    """Stands in for http.Session: records what was asked, answers from a script."""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, dict]] = []
        self.calls = 0
        self.quota = QuotaState(remaining=87)

    def get_json(self, url: str, *, params=None, headers=None):
        self.calls += 1
        self.asked.append((url, dict(params or {})))
        return self.answers.pop(0)


class ClientTests(unittest.TestCase):
    def test_events_ask_for_the_window_not_a_count(self):
        session = FakeSession([EVENTS])
        client = oddsio.OddsIoClient("k3y", session=session)
        start = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
        end = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)
        events = client.events("football", "england-premier-league", start, end)
        self.assertEqual(len(events), 1)
        url, params = session.asked[0]
        self.assertEqual(url, "https://api.odds-api.io/v3/events")
        self.assertEqual(params["apiKey"], "k3y")
        self.assertEqual(params["from"], "2026-10-16T08:00:00Z")
        self.assertEqual(params["to"], "2026-10-18T16:00:00Z")
        self.assertEqual(params["status"], "pending")
        self.assertEqual((client.requests, client.remaining_this_hour), (1, 87))

    def test_odds_take_one_to_ten_events(self):
        session = FakeSession([ODDS])
        client = oddsio.OddsIoClient("k3y", session=session)
        client.odds(["61301", "849817"], ["FanDuel", "Bet365"])
        params = session.asked[0][1]
        self.assertEqual(params["eventIds"], "61301,849817")
        self.assertEqual(params["bookmakers"], "FanDuel,Bet365")
        with self.assertRaises(ValueError):
            client.odds([], ["FanDuel"])
        with self.assertRaises(ValueError):
            client.odds([str(i) for i in range(11)], ["FanDuel"])
        self.assertEqual(client.requests, 1)  # the refused ones were never sent

    def test_selected_books_in_either_shape(self):
        for answer in (
            [{"name": "FanDuel"}, {"name": "Bet365"}],
            {"bookmakers": ["FanDuel", "Bet365"]},
        ):
            client = oddsio.OddsIoClient("k3y", session=FakeSession([answer]))
            self.assertEqual(client.selected_books(), ["FanDuel", "Bet365"])


if __name__ == "__main__":
    unittest.main()
