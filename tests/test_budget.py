import tempfile
import unittest
from datetime import date
from pathlib import Path

from sports_analyzer import budget
from tests import conftest  # noqa: F401


class BudgetTests(unittest.TestCase):
    def test_allowance_hand_checked(self):
        st = budget.BudgetState(
            "2026-10-12", used=27, remaining=473, day="2026-10-14", day_start_used=9
        )
        # The quota resets on 11-01: 18 days left from 10-14. Spent today 18.
        # share = (473 + 18 - 25) / 18 = 25.889; allowance = 1.6 * 25.889 - 18 = 23.42
        self.assertAlmostEqual(budget.daily_share(st, date(2026, 10, 14), 18), 25.889, places=3)
        self.assertAlmostEqual(budget.allowance_today(st, date(2026, 10, 14)), 23.422, places=3)
        # A new day starts with nothing spent: 1.6 * (473 - 25) / 17 = 42.16
        self.assertAlmostEqual(budget.allowance_today(st, date(2026, 10, 15)), 42.165, places=3)
        self.assertIsNone(
            budget.allowance_today(budget.BudgetState("2026-10-12"), date(2026, 10, 14))
        )

    def test_pregame_extra_raises_the_ceiling_but_never_touches_the_reserve(self):
        st = budget.BudgetState(
            "2026-10-12", used=27, remaining=473, day="2026-10-14", day_start_used=9
        )
        # 23.42 left of today's routine allowance (above) + 6 for pre-game refreshes.
        self.assertAlmostEqual(
            budget.allowance_today(st, date(2026, 10, 14), extra=budget.PREGAME_EXTRA),
            29.422,
            places=3,
        )
        # Near the end: nothing spent today, 30 left. Routine: 1.6 * (30 - 25) / 18 = 0.44;
        # with the extra it would be 6.44, but only 30 - 25 = 5 sit above the reserve.
        low = budget.BudgetState(
            "2026-10-12", used=470, remaining=30, day="2026-10-14", day_start_used=470
        )
        self.assertAlmostEqual(budget.allowance_today(low, date(2026, 10, 14)), 0.444, places=3)
        self.assertEqual(budget.allowance_today(low, date(2026, 10, 14), extra=6), 5)

    def test_plan_degrades_to_essentials_then_nothing(self):
        wanted, essential = ("h2h", "totals"), ("h2h",)
        self.assertEqual(budget.plan_markets(wanted, 5, 10.0, essential), wanted)
        self.assertEqual(budget.plan_markets(wanted, 5, 9.0, essential), essential)
        self.assertEqual(budget.plan_markets(wanted, 5, 4.0, essential), ())
        self.assertEqual(
            budget.plan_markets(wanted, 5, None, essential), wanted
        )  # unknown: no limit
        self.assertEqual(budget.plan_markets(wanted, 0, 0.0, essential), wanted)  # nothing to buy

    def test_record_tracks_the_day_and_detects_a_reset(self):
        st = budget.BudgetState(
            "2026-10-12", used=27, remaining=473, day="2026-10-14", day_start_used=9
        )
        st = budget.record(st, 31, 469, date(2026, 10, 14))  # evening run, same day
        self.assertEqual((st.used, st.day_start_used), (31, 9))
        st = budget.record(st, 45, 455, date(2026, 10, 15))  # next morning
        self.assertEqual((st.day, st.day_start_used), ("2026-10-15", 31))
        st = budget.record(st, 6, 494, date(2026, 11, 1))  # counter went down: new period
        self.assertEqual((st.period_start, st.day_start_used), ("2026-11-01", 0))
        self.assertEqual(budget.record(st, None, None, date(2026, 11, 1)), st)

    def test_never_locks_itself_out_after_the_period(self):
        # Regression (review 2026-10-14): near the reserve at the end of a period the
        # allowance was 0 forever, so no purchase ever saw the quota reset.
        st = budget.BudgetState(
            "2026-11-01", used=476, remaining=24, day="2026-11-30", day_start_used=476
        )
        # (24 - 25 reserve) / 1 day left: nothing to spend on the period's last day...
        self.assertEqual(budget.allowance_today(st, date(2026, 11, 30)), 0.0)
        self.assertIsNone(
            budget.allowance_today(st, date(2026, 12, 1))
        )  # a new month: let one run buy
        st = budget.record(st, 6, 494, date(2026, 12, 1))  # ...which sees the counter drop
        self.assertEqual(st.period_start, "2026-12-01")
        self.assertGreater(budget.allowance_today(st, date(2026, 12, 1)), 0)

    def test_the_quota_resets_on_the_first_of_each_month(self):
        # Regression (2026-10-16): the budget assumed 30 days from the first day it saw,
        # spreading October's credits until 11-11 although they expire on 11-01.
        self.assertEqual(budget.period_end(date(2026, 10, 16)), date(2026, 11, 1))
        self.assertEqual(budget.period_end(date(2026, 12, 5)), date(2027, 1, 1))
        st = budget.BudgetState(
            "2026-10-12", used=82, remaining=418, day="2026-10-16", day_start_used=69
        )
        # 16 days to 11-01, 13 spent today: (418 + 13 - 25) / 16 = 25.375 a day.
        self.assertAlmostEqual(budget.daily_share(st, date(2026, 10, 16), 13), 25.375, places=3)
        # A new month with October's counters: the run may buy and so see the reset.
        self.assertIsNone(budget.allowance_today(st, date(2026, 11, 1)))

    def test_round_trip_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            fresh = budget.load(path, date(2026, 10, 14))
            self.assertEqual(fresh.period_start, "2026-10-14")
            budget.save(path, budget.record(fresh, 3, 497, date(2026, 10, 14)))
            back = budget.load(path, date(2026, 10, 20))
            self.assertEqual((back.used, back.remaining, back.period_start), (3, 497, "2026-10-14"))


if __name__ == "__main__":
    unittest.main()
