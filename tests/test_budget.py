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
        # 30-day period ends 11-11: 28 days left from 10-14. Spent today 18.
        # share = (473 + 18 - 25) / 28 = 16.643; allowance = 1.6 * 16.643 - 18 = 8.63
        self.assertAlmostEqual(budget.daily_share(st, date(2026, 10, 14), 18), 16.643, places=3)
        self.assertAlmostEqual(budget.allowance_today(st, date(2026, 10, 14)), 8.629, places=3)
        # A new day starts with nothing spent: 1.6 * (473 - 25) / 27 = 26.55
        self.assertAlmostEqual(budget.allowance_today(st, date(2026, 10, 15)), 26.548, places=3)
        self.assertIsNone(
            budget.allowance_today(budget.BudgetState("2026-10-12"), date(2026, 10, 14))
        )

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
        )  # past the period: let one run buy
        st = budget.record(st, 6, 494, date(2026, 12, 1))  # ...which sees the counter drop
        self.assertEqual(st.period_start, "2026-12-01")
        self.assertGreater(budget.allowance_today(st, date(2026, 12, 1)), 0)

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
