"""盯盤: what to read, when, within the free quota, and when a move asks for a purchase.

Every number below is checked by hand in the comment next to it. Prices are made up.
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from sports_analyzer import budget, pipeline, sensor
from sports_analyzer.config import Settings
from sports_analyzer.fetch.oddsio import IoEvent, IoPrices
from tests import conftest  # noqa: F401

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def card(mid, home, away, hours, comp="PL", tw=True, bought=None, sport="football"):
    c = {
        "kickoff_utc": (NOW + timedelta(hours=hours)).isoformat(),
        "home": home,
        "away": away,
        "tw": {"prices": {}} if tw else None,
        "market": {"h2h": {"odds_at": bought.isoformat()}} if bought else {},
    }
    if sport == "mlb":
        c["game_pk"] = mid
    else:
        c.update({"match_id": mid, "competition": comp})
    return (sport, c)


def w(key="football:1", hours=5.0, league="PL", bought=None, sport="football"):
    return sensor.Watch(
        sport, key.split(":")[1], league, "H", "A", NOW + timedelta(hours=hours), bought
    )


def reading(minutes_ago: float, key="football:1", **p):
    at = NOW - timedelta(minutes=minutes_ago)
    return {"at": at.isoformat(timespec="seconds"), "key": key, "p": p}


class WatchTests(unittest.TestCase):
    def test_only_matches_with_a_tw_price_in_covered_competitions_within_48h(self):
        cards = [
            card(1, "Everton FC", "Chelsea FC", 23.5),
            card(2, "Leeds", "Fulham", 1, tw=False),  # no 運彩 price to compare
            card(3, "Ajax", "PSV", 2, comp="DED"),  # odds-api.io slug not mapped
            card(4, "Arsenal", "Brentford", 49),  # too far away
            card(5, "Burnley", "Wolves", -0.5),  # started
            card(849817, "Cleveland Guardians", "Tampa Bay Rays", 6, sport="mlb"),
            card(1, "Everton FC", "Chelsea FC", 23.5),  # an older snapshot's copy
        ]
        watch = sensor.watched(cards, NOW)
        self.assertEqual([x.key for x in watch], ["mlb:849817", "football:1"])  # soonest first
        self.assertEqual(watch[0].sides, ("home", "away"))
        self.assertEqual(watch[1].sides, ("home", "draw", "away"))
        everything = sensor.watched(cards, NOW, hours=72, need_tw=False)
        self.assertEqual(len(everything), 4)  # + Leeds and Arsenal; DED and the started one never

    def test_the_purchase_time_comes_from_the_h2h_block(self):
        bought = NOW - timedelta(hours=3)
        watch = sensor.watched([card(1, "H", "A", 5, bought=bought)], NOW)
        self.assertEqual(watch[0].bought_at, bought)


class MatchingTests(unittest.TestCase):
    def test_names_and_kickoff_pick_the_event(self):
        ours = w(hours=5)
        ours = sensor.Watch("football", "1", "SA", "Juventus FC", "FC Internazionale Milano",
                            ours.kickoff, None)  # fmt: skip
        events = {
            "SA": [
                # 0.727 + 1.0 (Inter is an alias) = 1.73 >= 1.2
                IoEvent("a", "italy-serie-a", ours.kickoff, "Juventus Turin", "Inter"),
                # home and away swapped: the readings would be the wrong way round
                IoEvent("b", "italy-serie-a", ours.kickoff, "Inter", "Juventus"),
                # the same pair three hours later (another competition's replay)
                IoEvent("c", "italy-serie-a", ours.kickoff + timedelta(hours=3), "Juventus",
                        "Inter"),
            ]
        }  # fmt: skip
        self.assertEqual(sensor.match_events([ours], events), {"football:1": "a"})
        self.assertEqual(sensor.match_events([ours], {"SA": events["SA"][1:]}), {})

    def test_event_lists_are_fetched_when_missing_old_or_short_of_a_match(self):
        state = {"events": {"PL": {"at": (NOW - timedelta(hours=2)).isoformat(), "events": []}}}
        watch = [w(), w("football:2", league="SA")]
        # PL: two hours old but a watched match is missing from it (retry after 1 h); SA: none
        self.assertEqual(sensor.stale_leagues(watch, state, {}, NOW), ["PL", "SA"])
        self.assertEqual(sensor.stale_leagues(watch[:1], state, {"football:1": "x"}, NOW), [])


class ScheduleTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(sensor.interval_minutes(0.5), 10)
        self.assertEqual(sensor.interval_minutes(24), 10)
        self.assertEqual(sensor.interval_minutes(24.1), 30)
        self.assertEqual(sensor.interval_minutes(48), 30)
        self.assertIsNone(sensor.interval_minutes(48.1))
        self.assertIsNone(sensor.interval_minutes(0))
        self.assertIsNone(sensor.interval_minutes(-1))

    def test_due_with_a_minute_of_slack(self):
        near, far = w("football:1", hours=3), w("football:2", hours=30)
        last = {"football:1": NOW - timedelta(minutes=9), "football:2": NOW - timedelta(minutes=25)}
        self.assertEqual(sensor.due([near, far], last, NOW), [near])  # 9 >= 10 - 1; 25 < 29
        last = {"football:1": NOW - timedelta(minutes=8), "football:2": NOW - timedelta(minutes=29)}
        self.assertEqual(sensor.due([near, far], last, NOW), [far])

    def test_free_places_and_spare_requests_go_to_the_matches_not_due(self):
        due_now = [w(f"football:{i}", hours=2 + i) for i in range(3)]
        far = [w(f"football:{i}", hours=30 + i) for i in range(10, 22)]
        last = {x.key: NOW - timedelta(minutes=10) for x in far}  # read ten minutes ago
        chunks = sensor.reads(due_now + far, last, NOW, budget=5, spare=1)
        # 3 due + 7 far in the first request (free places), the other 5 far in one spare
        self.assertEqual([len(c) for c in chunks], [10, 5])
        self.assertEqual(chunks[0][:3], due_now)
        self.assertEqual(chunks[0][3:], far[:7])  # soonest first
        self.assertEqual([len(c) for c in sensor.reads(due_now + far, last, NOW, 5)], [10])
        self.assertEqual(sensor.reads(due_now + far, last, NOW, budget=0, spare=3), [])
        # A far match read five minutes ago is left alone, spare or not.
        last[far[0].key] = NOW - timedelta(minutes=5)
        self.assertNotIn(far[0], sensor.reads(due_now + far, last, NOW, 5, 1)[0])

    def test_due_matches_beyond_the_budget_wait_soonest_first(self):
        due_now = [w(f"football:{i}", hours=1 + i / 10) for i in range(25)]
        chunks = sensor.reads(due_now, {}, NOW, budget=2, spare=3)
        self.assertEqual([len(c) for c in chunks], [10, 10])
        self.assertEqual(chunks[1][-1], due_now[19])


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat(timespec="seconds")


class QuotaTests(unittest.TestCase):
    def test_allowance_counts_any_hour_and_any_24_hours(self):
        # nothing used: a run's even share of a day times BURST, 3 x 480 / 144 = 10
        self.assertEqual(sensor.allowance({}, NOW), 10)
        # 85 in the last hour: 90 - 85 = 5
        self.assertEqual(sensor.allowance({"recent": [[ago(minutes=50), 85]]}, NOW), 5)
        # 475 in the last 24 hours: 480 - 475 = 5; 25 hours ago no longer counts
        busy = {"recent": [[ago(hours=23, minutes=50), 475], [ago(hours=25), 400]]}
        self.assertEqual(sensor.allowance(busy, NOW), 5)
        self.assertEqual(sensor.used(busy, NOW), 475)

    def test_usage_keeps_the_last_24_hours(self):
        usage = {"recent": [[ago(hours=25), 4], [ago(hours=2), 6]]}
        after = sensor.record_usage(usage, 3, NOW)
        self.assertEqual(after["recent"], [[ago(hours=2), 6], [ago(), 3]])
        self.assertEqual(sensor.record_usage(after, 0, NOW)["recent"], after["recent"])

    def test_forecast_of_the_tiers_over_24_hours(self):
        def total(watch):
            plan = sensor.forecast(watch, NOW)
            self.assertEqual(len(plan), 144)
            return sum(n for _, n in plan)

        # 5 h away: due at runs 1-29 (kickoff at run 30), plus the list every 6 h (4 times)
        self.assertEqual(total([w(hours=5)]), 33)
        # 30 h away: every third run while more than 24 h away (runs 3 ... 33: 11), then
        # every run from run 36 (24 h left) to 144 (109): 120, plus 4 lists
        self.assertEqual(total([w(hours=30)]), 124)
        # 20 matches 5 h away: two requests a run
        self.assertEqual(total([w(f"football:{i}", hours=5) for i in range(20)]), 62)
        # MLB's list has two slugs (regular season and playoffs)
        self.assertEqual(total([w("mlb:9", hours=5, league="MLB", sport="mlb")]), 37)

    def test_headroom_finds_the_tightest_24_hours_ahead(self):
        plan = [(NOW + timedelta(minutes=10 * k), 1) for k in range(1, 145)]
        self.assertEqual(sensor.headroom({}, NOW, plan), 336)  # 480 - 144 planned
        # 100 used 23 h 50 min ago (gone at the next run), 300 an hour ago (gone at run
        # 138): the window ending at run 137 holds 300 + 137 planned, 43 short of 480.
        usage = {"recent": [[ago(hours=23, minutes=50), 100], [ago(hours=1), 300]]}
        self.assertEqual(sensor.headroom(usage, NOW, plan), 43)

    def test_spare_uses_the_quota_evenly_within_the_headroom(self):
        plan = [(NOW + timedelta(minutes=10 * k), 1) for k in range(1, 145)]
        # need 144 + 2 spent + 3 due = 149; (480 - 20 - 149) / 144 = 2.16 -> 3
        self.assertEqual(sensor.spare_requests({}, NOW, plan, 2, 3), 3)
        # an even pace of 3 still fits: 43 - 2 - 3 - 20 = 18 left in the tightest window
        usage = {"recent": [[ago(hours=23, minutes=50), 100], [ago(hours=1), 300]]}
        self.assertEqual(sensor.spare_requests(usage, NOW, plan, 2, 3), 3)
        # 470 in the last 24 hours: no room at all
        self.assertEqual(
            sensor.spare_requests({"recent": [[ago(hours=3), 470]]}, NOW, plan, 2, 3), 0
        )
        # a forecast that takes the whole day leaves nothing to spare
        heavy = [(at, 4) for at, _ in plan]
        self.assertEqual(sensor.spare_requests({}, NOW, heavy, 0, 0), 0)


class MoveTests(unittest.TestCase):
    def test_a_three_point_move_since_the_purchase(self):
        bought = NOW - timedelta(minutes=60)
        watch = [w(bought=bought)]
        readings = [
            reading(70, home=0.40, draw=0.30, away=0.30),
            reading(58, home=0.41, draw=0.30, away=0.29),  # nearest the purchase: baseline
            reading(10, home=0.43, draw=0.29, away=0.28),
            reading(0, home=0.439, draw=0.29, away=0.271),  # +2.9 home: not yet
        ]
        self.assertEqual(sensor.moved(watch, readings, NOW), [])
        readings[-1] = reading(0, home=0.44, draw=0.29, away=0.27)  # +3.0
        self.assertEqual(sensor.moved(watch, readings, NOW), watch)

    def test_no_purchase_too_soon_or_on_an_old_reading(self):
        readings = [reading(30, home=0.40, away=0.60), reading(0, home=0.50, away=0.50)]
        mlb = dict(key="mlb:1", league="MLB", sport="mlb")
        self.assertEqual(sensor.moved([w(**mlb, bought=NOW - timedelta(minutes=19))],
                                      [dict(r, key="mlb:1") for r in readings], NOW), [])  # fmt: skip
        stale = [reading(70, key="mlb:1", home=0.40, away=0.60),
                 reading(31, key="mlb:1", home=0.50, away=0.50)]  # fmt: skip
        self.assertEqual(sensor.moved([w(**mlb)], stale, NOW), [])
        # never bought: against the first reading
        fresh = [dict(r, key="mlb:1") for r in readings]
        self.assertEqual(len(sensor.moved([w(**mlb)], fresh, NOW)), 1)

    def test_an_unanswered_trigger_becomes_the_baseline(self):
        watch = [w(bought=NOW - timedelta(hours=5))]
        readings = [
            reading(300, home=0.40, draw=0.30, away=0.30),
            reading(30, home=0.45, draw=0.28, away=0.27),  # asked for a purchase then
            reading(0, home=0.46, draw=0.28, away=0.26),
        ]
        asked = {"PL": (NOW - timedelta(minutes=30)).isoformat()}
        self.assertEqual(len(sensor.moved(watch, readings, NOW)), 1)  # +6 since the purchase
        self.assertEqual(sensor.moved(watch, readings, NOW, asked), [])  # +1 since the ask
        recent = {"PL": (NOW - timedelta(minutes=10)).isoformat()}
        self.assertEqual(sensor.moved(watch, readings, NOW, recent), [])  # within MIN_GAP

    def test_two_books_de_vigged_and_averaged(self):
        prices = IoPrices("9", {"FanDuel": {"home": 1.9, "away": 1.9},
                                "Bet365": {"home": 1.8, "away": 2.0},
                                "Other": {"home": 1.5}})  # fmt: skip
        # 1.9/1.9 -> 0.5; 1.8/2.0 power de-vig -> 0.52854 (k = 1.084); mean 0.51427
        self.assertEqual(sensor.two_book_probs(prices, ("home", "away")),
                         {"home": 0.5143, "away": 0.4857})  # fmt: skip
        self.assertIsNone(sensor.two_book_probs(prices, ("home", "draw", "away")))

    def test_purchases_once_per_competition_soonest_first(self):
        moving = [w("football:1", 30, "SA"), w("mlb:2", 3, "MLB", sport="mlb"),
                  w("football:3", 20, "SA")]  # fmt: skip
        self.assertEqual(sensor.purchases(moving), ["MLB", "SA"])
        self.assertEqual([sensor.purchase_cost(x) for x in ("MLB", "SA")], [2, 1])


class StateTests(unittest.TestCase):
    def test_round_trip_and_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".sensor" / "state.json"
            self.assertEqual(sensor.load_state(path)["readings"], [])  # nothing yet
            state = sensor.load_state(path)
            state["readings"] = [reading(50 * 60), reading(10)]
            sensor.record_triggers(state, ["PL"], NOW - timedelta(hours=50))
            sensor.record_triggers(state, ["SA"], NOW)
            sensor.prune(state, NOW)
            sensor.save_state(path, state)
            back = sensor.load_state(path)
            self.assertEqual(len(back["readings"]), 1)  # 50 h old: gone
            self.assertEqual(list(back["triggers"]), ["SA"])

    def test_books_are_asked_again_once_a_day(self):
        self.assertTrue(sensor.books_due({}, NOW))
        state = {"books": ["FanDuel", "Bet365"], "books_at": (NOW - timedelta(hours=5)).isoformat()}
        self.assertFalse(sensor.books_due(state, NOW))
        self.assertTrue(sensor.books_due(state, NOW + timedelta(hours=20)))


# ------------------------------------------------------------------ pipeline
class FakeIoClient:
    """odds-api.io as the sensor sees it: two books, a Premier League list, prices that
    the test moves between rounds."""

    def __init__(self, prices: dict[str, dict[str, float]]) -> None:
        self.prices = prices
        self.requests = 0
        self.remaining_this_hour = 80
        self.asked: list[str] = []

    def selected_books(self):
        self.requests += 1
        self.asked.append("books")
        return ["FanDuel", "Bet365"]

    def events(self, sport, league, start, end):
        self.requests += 1
        self.asked.append(f"events {league}")
        kick = NOW + timedelta(hours=5)
        return (
            [IoEvent("e1", league, kick, "Everton", "Chelsea")] if league.startswith("eng") else []
        )

    def odds(self, ids, books):
        self.requests += 1
        self.asked.append(f"odds {len(ids)}")
        return [IoPrices(i, {b: self.prices[i] for b in books}) for i in ids if i in self.prices]


class SensorRoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {
            "SNAPSHOT_DIR": root / "snapshots",
            "SENSOR_DIR": root / ".sensor",
            "ODDS_BUDGET_PATH": root / "odds_budget.json",
        }
        self.patches = [mock.patch.object(pipeline, k, v) for k, v in self.paths.items()]
        for p in self.patches:
            p.start()
        folder = self.paths["SNAPSHOT_DIR"] / "2026-10-16"
        folder.mkdir(parents=True)
        cards = [card(560603, "Everton FC", "Chelsea FC", 5, bought=NOW - timedelta(hours=11))[1]]
        snap = {"date": "2026-10-16", "competitions": [{"code": "PL", "cards": cards}]}
        (folder / "football.json").write_text(json.dumps(snap), encoding="utf-8")
        self.settings = Settings(None, None, odds_api_io_key="k3y")

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_reads_then_asks_for_the_competition_that_moved(self):
        client = FakeIoClient({"e1": {"home": 2.5, "draw": 3.4, "away": 2.9}})
        first = pipeline.run_sensor(self.settings, NOW, client)
        self.assertEqual(client.asked, ["books", "events england-premier-league", "odds 1"])
        self.assertEqual((first["readings"], first["buy"], first["mapped"]), (1, [], 1))

        client = FakeIoClient({"e1": {"home": 2.1, "draw": 3.5, "away": 3.6}})  # home steamed
        later = NOW + timedelta(minutes=10)
        second = pipeline.run_sensor(self.settings, later, client)
        self.assertEqual(client.asked, ["odds 1"])  # books and the list are kept
        self.assertEqual(second["buy"], ["PL"])
        status = (self.paths["SENSOR_DIR"] / "status.json").read_text(encoding="utf-8")
        self.assertNotIn("2.1", status)  # counts only, never a price
        self.assertNotIn("0.4", status)
        state = json.loads((self.paths["SENSOR_DIR"] / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["triggers"], {"PL": later.isoformat(timespec="seconds")})

        # Asked for, so not again ten minutes later (MIN_GAP), nor after it without a new move.
        third = pipeline.run_sensor(self.settings, later + timedelta(minutes=10), client)
        self.assertEqual(third["buy"], [])
        fourth = pipeline.run_sensor(self.settings, later + timedelta(minutes=30), client)
        self.assertEqual(fourth["buy"], [])

    def test_a_busy_queue_or_an_empty_budget_holds_the_purchase(self):
        client = FakeIoClient({"e1": {"home": 2.5, "draw": 3.4, "away": 2.9}})
        pipeline.run_sensor(self.settings, NOW, client)
        client.prices = {"e1": {"home": 2.1, "draw": 3.5, "away": 3.6}}
        later = NOW + timedelta(minutes=10)
        held = pipeline.run_sensor(self.settings, later, client, hold=True)
        self.assertEqual((held["buy"], held["held_busy"]), ([], ["PL"]))
        # The Odds API: 25 credits left = the reserve, nothing to spend today.
        budget.save(
            self.paths["ODDS_BUDGET_PATH"],
            budget.BudgetState("2026-10-01", 475, 25, "2026-10-16", 470),
        )
        poor = pipeline.run_sensor(self.settings, later + timedelta(minutes=10), client)
        self.assertEqual((poor["buy"], poor["held_budget"]), ([], ["PL"]))
        # Still there once a queue clears or a budget allows: nothing was asked for.
        self.assertNotIn(
            "PL", sensor.load_state(self.paths["SENSOR_DIR"] / "state.json")["triggers"]
        )

    def test_without_the_key_nothing_happens(self):
        report = pipeline.run_sensor(Settings(None, None), NOW, FakeIoClient({}))
        self.assertEqual(report["skipped"], "no ODDS_API_IO_KEY")


class RadarOnlyTests(unittest.TestCase):
    def test_only_the_competitions_that_moved_are_bought_whole(self):
        cards = [
            card(1, "H1", "A1", 5)[1],
            card(2, "H2", "A2", 40)[1],  # far, but the same competition: one request anyway
            card(3, "H3", "A3", 50)[1],  # beyond the sensor's window
            card(4, "H4", "A4", 5, tw=False)[1],
            card(5, "H5", "A5", 5, comp="SA")[1],
        ]
        snap = {"date": "2026-10-16", "competitions": [{"code": "X", "cards": cards}]}
        seen: list = []

        def fake_buy(sport, due, settings, now, cfg, report, essential_only=False):
            seen.append((sport, [c["match_id"] for c in due], essential_only))
            return report, []

        with (
            mock.patch.object(pipeline, "_latest_snapshot", return_value=snap),
            mock.patch.object(pipeline, "_latest_snapshots", return_value=[]),
            mock.patch.object(pipeline, "_buy_fresh", side_effect=fake_buy),
        ):
            pipeline.run_radar(settings=Settings(None, None), now=NOW, only={"PL"})
        self.assertEqual(seen, [("football", [1, 2], True)])


class ReviewDueTests(unittest.TestCase):
    """A sensor round hands just-finished matches to `review --new-only`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {
            "SNAPSHOT_DIR": root / "snapshots",
            "FOOTBALL_REVIEWS_PATH": root / "reviews" / "football.csv",
            "MLB_REVIEWS_PATH": root / "reviews" / "mlb.csv",
        }
        self.patches = [mock.patch.object(pipeline, k, v) for k, v in self.paths.items()]
        for p in self.patches:
            p.start()
        made = (NOW - timedelta(hours=10)).isoformat()  # every card made before kickoff

        def kick(hours_ago: float) -> str:
            return (NOW - timedelta(hours=hours_ago)).isoformat()

        football = [
            {"match_id": 1, "kickoff_utc": kick(2.2)},  # finished: due
            {"match_id": 2, "kickoff_utc": kick(1.5)},  # still playing
            {"match_id": 3, "kickoff_utc": kick(7)},  # left to the routine reviews
            {"match_id": 4, "kickoff_utc": kick(3)},  # already reviewed
        ]
        mlb = [
            {"game_pk": 10, "kickoff_utc": kick(2.6)},  # due (2.5 h)
            {"game_pk": 11, "kickoff_utc": kick(2.2)},  # still playing
            {"game_pk": 12, "kickoff_utc": kick(8.5)},  # rain delays: still asked (9 h)
        ]
        folder = self.paths["SNAPSHOT_DIR"] / NOW.date().isoformat()
        folder.mkdir(parents=True)
        (folder / "football.json").write_text(
            json.dumps({"generated_at": made, "competitions": [{"cards": football}]}),
            encoding="utf-8",
        )
        (folder / "mlb.json").write_text(
            json.dumps({"generated_at": made, "games": mlb}), encoding="utf-8"
        )
        reviewed = self.paths["FOOTBALL_REVIEWS_PATH"]
        reviewed.parent.mkdir(parents=True)
        reviewed.write_text("match_id,kickoff_utc\n4,x\n", encoding="utf-8")

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_finished_and_not_reviewed(self):
        self.assertEqual(pipeline.review_due(NOW), ["football:1", "mlb:10", "mlb:12"])

    def test_the_round_passes_it_on_unless_holding(self):
        out = Path(self.tmp.name) / "github_output"
        with (
            mock.patch.dict("os.environ", {"GITHUB_OUTPUT": str(out)}),
            mock.patch.object(pipeline, "run_sensor", return_value={"buy": ["PL"]}),
            mock.patch.object(pipeline, "review_due", return_value=["mlb:10", "mlb:12"]),
        ):
            pipeline.main(["sensor"])
            self.assertEqual(out.read_text(encoding="utf-8"), "buy=PL\nreview=2\n")
            out.unlink()
            pipeline.main(["sensor", "--hold"])
            self.assertEqual(out.read_text(encoding="utf-8"), "buy=PL\nreview=\n")


if __name__ == "__main__":
    unittest.main()
