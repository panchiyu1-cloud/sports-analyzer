"""延遲雷達: the last 運彩 upload against international prices bought hours after it."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sports_analyzer import moves, notify
from sports_analyzer.analysis import pregame
from sports_analyzer.config import ModelConfig
from sports_analyzer.render.cards import render_index
from tests import conftest  # noqa: F401

CFG = ModelConfig()
KICKOFF = "2026-10-17T00:00:00+00:00"


def row(at: str, source: str, side: str, prob: float, price: float | None = None, **kw) -> dict:
    base = {
        "observed_at": at,
        "source": source,
        "sport": "mlb",
        "match_id": "813024",
        "kickoff_utc": KICKOFF,
        "home": "Los Angeles Dodgers",
        "away": "Milwaukee Brewers",
        "market": "h2h",
        "side": side,
        "line": "",
        "prob": f"{prob:.4f}",
        "price": f"{price:g}" if price else "",
    }
    return {**base, **kw}


def tape() -> list[dict]:
    """運彩 uploaded at 20:00 Taipei (Dodgers 1.80, Brewers 2.10); the evening consensus
    half an hour later had the Dodgers at 52%; the 05:40 radar run bought 60%."""
    return [
        row("2026-10-16T12:00+00:00", "tw", "home", 0.535, 1.80),
        row("2026-10-16T12:00+00:00", "tw", "away", 0.465, 2.10),
        row("2026-10-16T12:30+00:00", "intl", "home", 0.52),
        row("2026-10-16T12:30+00:00", "intl", "away", 0.48),
        row("2026-10-16T21:40+00:00", "intl", "home", 0.60),
        row("2026-10-16T21:40+00:00", "intl", "away", 0.40),
    ]


NOW = datetime(2026, 10, 16, 21, 45, tzinfo=UTC)


class PriceTests(unittest.TestCase):
    def test_lowest_price_that_still_pays(self):
        self.assertEqual(moves.price_needed(0.60, 0.03), 1.72)  # 1.03 / 0.60 = 1.7167
        self.assertEqual(moves.price_needed(0.50, 0.02), 2.04)  # exactly 2.04, not 2.05
        self.assertEqual(moves.value_threshold("mlb", CFG), 0.03)
        self.assertEqual(moves.value_threshold("football", CFG), 0.02)


class RadarTests(unittest.TestCase):
    def test_an_unchanged_upload_behind_the_market_is_a_candidate(self):
        found = moves.radar(tape(), NOW, CFG)
        self.assertEqual(len(found["candidates"]), 1)
        c = found["candidates"][0]
        self.assertEqual(
            (c["side"], c["tw_price"], c["prob"], c["prob_then"]), ("home", 1.8, 0.6, 0.52)
        )
        self.assertEqual((c["move"], c["ev"], c["need"]), (0.08, 0.08, 1.72))  # 0.60 x 1.80 - 1
        self.assertEqual(
            (c["tw_at"], c["intl_at"]), ("2026-10-16T12:00+00:00", "2026-10-16T21:40+00:00")
        )
        self.assertEqual(found["watch"], [])  # the Brewers went the other way
        self.assertEqual(
            found["last"]["mlb"], {"tw": "2026-10-16T12:00+00:00", "intl": "2026-10-16T21:40+00:00"}
        )

    def test_nothing_once_started_answered_or_judged_the_usual_way(self):
        self.assertEqual(
            moves.radar(tape(), datetime(2026, 10, 17, tzinfo=UTC), CFG)["candidates"], []
        )
        # A newer upload answered the question (運彩 moved to 1.62): nothing stale after it.
        newer = tape() + [
            row("2026-10-16T22:10+00:00", "tw", "home", 0.60, 1.62),
            row("2026-10-16T22:10+00:00", "tw", "away", 0.40, 2.40),
        ]
        self.assertEqual(moves.radar(newer, NOW, CFG)["candidates"], [])
        # International prices within 3 hours of the upload are the regular comparison's.
        near = [r for r in tape() if r["observed_at"] != "2026-10-16T21:40+00:00"] + [
            row("2026-10-16T14:30+00:00", "intl", "home", 0.60),
        ]
        self.assertEqual(moves.radar(near, NOW, CFG)["candidates"], [])

    def test_a_move_short_of_value_is_watched(self):
        rows = [r for r in tape() if r["observed_at"] != "2026-10-16T21:40+00:00"] + [
            row("2026-10-16T21:40+00:00", "intl", "home", 0.565),  # +4.5pp: 0.565 x 1.80 = 1.017
            row("2026-10-16T21:40+00:00", "intl", "away", 0.435),
        ]
        found = moves.radar(rows, NOW, CFG)
        self.assertEqual(found["candidates"], [])
        self.assertEqual(
            [(w["side"], w["move"], w["need"]) for w in found["watch"]], [("home", 0.045, 1.83)]
        )


class RadarEventTests(unittest.TestCase):
    def test_one_record_per_side_and_upload(self):
        rows = tape() + [
            row("2026-10-16T23:10+00:00", "intl", "home", 0.62),  # says the same, later
            row("2026-10-16T23:10+00:00", "intl", "away", 0.38),
        ]
        events = moves.radar_events(rows, CFG)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e["type"], e["side"], e["from_at"], e["to_at"]),
                         ("radar", "home", "2026-10-16T12:00+00:00", "2026-10-16T21:40+00:00"))  # fmt: skip
        self.assertEqual(
            (e["intl_move"], e["expected"], e["tw_price"], e["current"]), (0.08, 0.6, 1.8, True)
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.csv"
            added: list[dict] = []
            names = {
                ("mlb", "813024"): {"competition": "MLB", "home_zh": "道奇", "away_zh": "釀酒人"}
            }
            self.assertEqual(moves.record_events(path, events, names, added=added), 1)
            self.assertEqual((added[0]["home_zh"], added[0]["current"]), ("道奇", True))
            again: list[dict] = []
            self.assertEqual(
                moves.record_events(path, moves.radar_events(rows, CFG), added=again), 0
            )
            self.assertEqual(again, [])
            saved = moves.read_events(path)
            self.assertEqual((saved[0]["type"], saved[0]["tw_price"]), ("radar", "1.8"))
            self.assertNotIn("current", saved[0])

    def test_history_shows_the_stale_ev_and_whether_tw_followed(self):
        rows = tape() + [
            row("2026-10-16T22:10+00:00", "tw", "home", 0.60, 1.62),
            row("2026-10-16T22:10+00:00", "tw", "away", 0.40, 2.40),
        ]
        events = moves.radar_events(rows, CFG)
        self.assertFalse(events[0]["current"])  # 運彩 answered at 22:10
        history = moves.event_history(events, rows, {("mlb", "813024"): (5, 2)}, now=NOW)
        e = history[0]["events"][0]
        self.assertEqual((e["ev_at"], e["close_tw_price"], e["result"]), (0.08, 1.62, "win"))


def card(kickoff: datetime, odds_at: datetime | None, tw: bool = True) -> dict:
    return {
        "kickoff_utc": kickoff.isoformat(),
        "market": {"h2h": {"odds_at": odds_at.isoformat()}} if odds_at else {},
        "tw": {"captured_at": "x"} if tw else None,
    }


class RadarPurchaseTests(unittest.TestCase):
    def test_due_within_four_hours_with_a_tw_price_and_old_prices(self):
        now = NOW
        cards = [
            card(now + timedelta(hours=3), now - timedelta(hours=9)),  # due
            card(now + timedelta(hours=5), now - timedelta(hours=9)),  # too far ahead
            card(now + timedelta(hours=2), now - timedelta(minutes=50)),  # prices recent
            card(now + timedelta(hours=2), now - timedelta(hours=9), tw=False),  # no 運彩
            card(now - timedelta(minutes=5), now - timedelta(hours=9)),  # started
            card(now + timedelta(hours=1), None),  # no prices at all: due
        ]
        due = pregame.radar_due(cards, now, CFG)
        self.assertEqual([cards.index(c) for c in due], [0, 5])

    def test_upload_plan_covers_every_game_an_hour_ahead(self):
        now = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
        mlb = [datetime(2026, 10, 16, 23, 5, tzinfo=UTC), datetime(2026, 10, 16, 23, 40, tzinfo=UTC),
               datetime(2026, 10, 17, 1, 10, tzinfo=UTC), datetime(2026, 10, 17, 2, 10, tzinfo=UTC)]  # fmt: skip
        plan = pregame.upload_plan({"mlb": mlb + mlb[:1]}, {"mlb": None}, now, CFG)
        # 23:05 - 1h -> 22:00 covers games to 01:00 (two start at 23:05); 01:10 - 1h ->
        # 00:00 covers the rest
        self.assertEqual(
            [(p["at"].strftime("%H:%M"), p["games"], p["done"], p["now"]) for p in plan],
            [("22:00", 3, False, False), ("00:00", 2, False, False)],
        )
        later = datetime(2026, 10, 16, 22, 20, tzinfo=UTC)
        uploaded = {"mlb": datetime(2026, 10, 16, 22, 5, tzinfo=UTC)}
        self.assertTrue(pregame.upload_plan({"mlb": mlb}, uploaded, later, CFG)[0]["done"])
        self.assertTrue(pregame.upload_plan({"mlb": mlb}, {"mlb": None}, later, CFG)[0]["now"])


class RadarNotifyTests(unittest.TestCase):
    def test_message_and_only_open_findings_are_sent(self):
        event = {**moves.radar_events(tape(), CFG)[0], "competition": "MLB",
                 "home_zh": "洛杉磯道奇", "away_zh": "密爾瓦基釀酒人"}  # fmt: skip
        title, body = notify.radar_message(event)
        self.assertEqual(title, "延遲雷達 EV +8.0%")
        self.assertIn("密爾瓦基釀酒人 @ 洛杉磯道奇", body)
        self.assertIn("洛杉磯道奇 不讓分：國際 52% → 60%（你上次上傳後 +8.0pp）", body)
        self.assertIn("運彩上次 1.8（20:00）若還沒變，EV +8.0%", body)
        self.assertIn("賠率 ≥ 1.72 就可下", body)
        sent = []

        class Session:
            def post(self, url, json, timeout):
                sent.append(json)
                return type("R", (), {"ok": True})()

        closed = {**event, "current": False}
        n = notify.notify_radar("topic", [event, closed, {"type": "tw_only"}], NOW, Session())
        self.assertEqual(n, 1)
        self.assertEqual((sent[0]["title"], sent[0]["tags"]), ("延遲雷達 EV +8.0%", ["satellite"]))
        self.assertEqual(
            notify.notify_radar("topic", [event], datetime(2026, 10, 17, tzinfo=UTC), Session()), 0
        )


class RadarPageTests(unittest.TestCase):
    def test_front_page_section(self):
        radar = {
            "found": moves.radar(tape(), NOW, CFG),
            "names": {
                ("mlb", "813024"): {"competition": "MLB", "home_zh": "道奇", "away_zh": "釀酒人"}
            },
            "plan": pregame.upload_plan(
                {"mlb": [datetime(2026, 10, 17, 0, 0, tzinfo=UTC)]},
                {"mlb": datetime(2026, 10, 16, 12, 0, tzinfo=UTC)},
                NOW,
                CFG,
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            html = render_index(
                None, None, None, NOW, Path(tmp) / "index.html", radar=radar
            ).read_text(encoding="utf-8")
        self.assertIn("延遲雷達", html)
        self.assertIn("釀酒人 @ 道奇", html)
        self.assertIn("道奇 不讓分：運彩賠率 ≥ <b>1.72</b> 就可下", html)
        self.assertIn("還有 2 小時 15 分", html)
        self.assertIn("國際 52% → 60%（+8.0pp）", html)
        self.assertIn('沒變的話 EV <span class="pos">+8.0%</span>', html)
        self.assertIn("MLB：運彩 10/16 20:00・國際 10/17 05:40", html)
        self.assertIn("<b>10/17 07:00</b>", html)  # an hour before the 08:00 first pitch
        # 05:45 in Taipei: the slot has not opened, and the last upload (20:00) is not in it.
        self.assertNotIn("現在上傳", html)
        self.assertNotIn("已上傳", html)


if __name__ == "__main__":
    unittest.main()
