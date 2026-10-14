import unittest
from datetime import UTC, datetime

from sports_analyzer.analysis import pregame
from sports_analyzer.config import ModelConfig
from sports_analyzer.fetch.odds import BookOdds, OddsEvent, TotalLine
from tests import conftest  # noqa: F401

NOW = datetime(2026, 10, 18, 11, 0, tzinfo=UTC)


def card(kickoff: str, odds_at: str | None = "2026-10-18T00:20+00:00", **over) -> dict:
    h2h = {"home": {"fair": 0.5}, "away": {"fair": 0.3}, "draw": {"fair": 0.2}}
    if odds_at:
        h2h["odds_at"] = odds_at
    c = {
        "kickoff_utc": kickoff,
        "home": "Arsenal FC",
        "away": "Chelsea FC",
        "home_key": "arsenal",
        "away_key": "chelsea",
        "market": {"h2h": h2h, "totals": {"line": 2.5, "over": {"fair": 0.5}}, "spread": None},
    }
    c.update(over)
    return c


def event(start: datetime, totals: bool = False) -> OddsEvent:
    book = BookOdds(
        "pinnacle",
        {"home": 1.9, "draw": 3.6, "away": 4.2},
        (),
        (TotalLine(2.5, 1.95, 1.9),) if totals else (),
    )
    return OddsEvent("x", "soccer_epl", start, "Arsenal", "Chelsea", (book,), fetched_at=NOW)


class PregameTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ModelConfig()  # 3-hour window, prices older than 20 minutes

    def test_due_cards_window_and_age(self):
        cards = [
            card("2026-10-18T13:30:00+00:00"),  # 2h30m away, morning prices: due
            card("2026-10-18T14:30:00+00:00"),  # 3h30m away: outside the window
            card("2026-10-18T10:30:00+00:00"),  # already started
            card("2026-10-18T12:00:00+00:00", odds_at="2026-10-18T10:50+00:00"),  # 10 min old
            card("2026-10-18T12:00:00+00:00", odds_at=None),  # no time: due
            card("2026-10-18T12:00:00+00:00", market=None),  # no prices yet: due
        ]
        due = pregame.due_cards(cards, NOW, self.cfg)
        self.assertEqual([cards.index(c) for c in due], [0, 4, 5])

    def test_upload_must_be_recent(self):
        self.assertTrue(pregame.upload_is_recent(datetime(2026, 10, 18, 9, 31, tzinfo=UTC), NOW))
        self.assertFalse(pregame.upload_is_recent(datetime(2026, 10, 18, 9, 29, tzinfo=UTC), NOW))
        self.assertFalse(pregame.upload_is_recent(None, NOW))

    def test_doubleheader_takes_the_nearest_game(self):
        first, second = (
            event(datetime(2026, 10, 18, 17, 0, tzinfo=UTC)),
            event(datetime(2026, 10, 18, 21, 0, tzinfo=UTC)),
        )
        c = card("2026-10-18T20:45:00+00:00")
        self.assertIs(pregame.match_card_event(c, [first, second]), second)
        self.assertIsNone(pregame.match_card_event(card("2026-10-19T12:00:00+00:00"), [first]))

    def test_h2h_only_purchase_keeps_the_old_totals_as_stale(self):
        c = card("2026-10-18T13:30:00+00:00")
        done = pregame.refresh_cards(
            [c], [event(datetime(2026, 10, 18, 13, 30, tzinfo=UTC))], self.cfg, 2.5
        )
        self.assertEqual(done, [c])
        self.assertEqual(c["market"]["h2h"]["odds_at"], "2026-10-18T11:00+00:00")
        self.assertNotIn("stale", c["market"]["h2h"])
        self.assertTrue(c["market"]["totals"]["stale"])
        self.assertAlmostEqual(
            sum(c["market"]["h2h"][k]["fair"] for k in ("home", "draw", "away")), 1.0, places=3
        )


if __name__ == "__main__":
    unittest.main()
