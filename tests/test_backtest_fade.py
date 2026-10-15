"""scripts/backtest_fade.py: MLB form (streaks, series) must follow each game's own teams."""

import importlib.util
import unittest
from pathlib import Path

from tests import conftest  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "backtest_fade", ROOT / "scripts" / "backtest_fade.py"
)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)  # type: ignore[union-attr]


def game(start: str, home: str, away: str, hs: int, as_: int) -> dict:
    return {"start": start, "season": start[:4], "home": home, "away": away, "scores": (hs, as_)}


DAY1, DAY2, DAY3 = "2021-04-01T23:05:00Z", "2021-04-02T23:05:00Z", "2021-04-03T23:05:00Z"


class MlbFormTests(unittest.TestCase):
    def test_games_at_the_same_start_keep_their_own_teams(self):
        # Regression: form was keyed by start time only, so the second game of a time
        # slot overwrote the first and the Phillies read the Yankees' streak.
        games = [
            game(DAY1, "Philadelphia Phillies", "Atlanta Braves", 5, 1),
            game(DAY1, "New York Yankees", "Toronto Blue Jays", 1, 3),
            game(DAY2, "Philadelphia Phillies", "Atlanta Braves", 4, 2),
            game(DAY2, "New York Yankees", "Toronto Blue Jays", 0, 2),
            game(DAY3, "Philadelphia Phillies", "Atlanta Braves", 3, 1),
            game(DAY3, "New York Yankees", "Toronto Blue Jays", 6, 5),
        ]
        form = bf.mlb_form(games)
        phi = form[(DAY3, "Philadelphia Phillies", "Atlanta Braves")]
        nyy = form[(DAY3, "New York Yankees", "Toronto Blue Jays")]
        self.assertEqual((phi["home"]["win_run"], phi["away"]["loss_run"]), (2, 2))
        self.assertEqual((nyy["home"]["loss_run"], nyy["away"]["win_run"]), (2, 2))
        self.assertEqual((phi["home"]["last_rf"], nyy["home"]["last_rf"]), (4, 0))
        self.assertTrue(phi["home"]["series_won"])
        self.assertFalse(nyy["home"]["series_won"])
        self.assertTrue(nyy["away"]["series_won"])

    def test_streaks_count_games_whose_odds_were_dropped(self):
        results = [
            game(DAY1, "Philadelphia Phillies", "Atlanta Braves", 5, 1),
            game(DAY2, "Philadelphia Phillies", "Atlanta Braves", 4, 2),
            game(DAY3, "Philadelphia Phillies", "Atlanta Braves", 3, 1),
        ]
        priced = [results[0], results[2]]  # day 2's odds could not be verified
        key = (DAY3, "Philadelphia Phillies", "Atlanta Braves")
        self.assertEqual(bf.mlb_form(priced, results)[key]["home"]["win_run"], 2)
        self.assertEqual(bf.mlb_form(priced)[key]["home"]["win_run"], 1)


class FootballTableTests(unittest.TestCase):
    def test_level_on_points_goal_difference_decides_the_leader(self):
        # Regression: teams level on points were ranked in a set's order, which changes
        # from run to run (押榜首／押墊底 picks moved between runs).
        def match(home: str, away: str, hg: int, ag: int) -> dict:
            return {"div": "E0", "season": "2122", "home": home, "away": away, "hg": hg, "ag": ag}

        matches = []
        for _ in range(5):
            matches += [match("Arsenal", "Burnley", 3, 0), match("Chelsea", "Fulham", 1, 0)]
        matches.append(match("Chelsea", "Arsenal", 1, 1))
        state = bf.football_form(matches)[-1]
        # 15 points each: Arsenal +15, Chelsea +5
        self.assertEqual((state["A"]["rank"], state["H"]["rank"]), (1, 2))
        burnley = bf.football_form([*matches[:-1], match("Burnley", "Fulham", 0, 0)])[-1]
        self.assertEqual((burnley["H"]["rank"], burnley["A"]["rank"]), (4, 3))


if __name__ == "__main__":
    unittest.main()
