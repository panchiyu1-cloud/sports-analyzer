import json
import tempfile
import unittest
from datetime import timezone
from pathlib import Path

from tests import conftest  # noqa: F401

from sports_analyzer.fetch import taiwan

FIXTURES = Path(__file__).parent / "fixtures"


class TaiwanFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = taiwan.ParseReport()
        cls.games = taiwan.parse_games(json.loads((FIXTURES / "tw_games.json").read_text(encoding="utf-8")), cls.report)

    def test_counts_and_skips(self):
        self.assertEqual(self.report.games, 3)
        self.assertEqual(self.report.skipped_games, 1)
        self.assertIn("角球大小", self.report.unknown_markets)
        self.assertEqual(self.report.unknown_teams, {"神秘隊", "未知聯"})

    def test_baseball_game_and_prices(self):
        g = self.games[0]
        self.assertEqual(g.home_key, "los angeles dodgers")
        self.assertEqual(g.away_key, "philadelphia phillies")
        self.assertEqual(g.kickoff.tzinfo, timezone.utc)
        self.assertEqual(g.kickoff.hour, 0)  # 08:00 Taipei == 00:00 UTC
        ml = g.market("h2h")
        assert ml is not None
        self.assertEqual(ml.type_id, "354")
        self.assertEqual(ml.price("home"), 1.5)  # pd=2, pu=1
        self.assertEqual(ml.price("away"), 2.07)  # pd=100, pu=107
        self.assertEqual(ml.updated.hour, 4)  # 12:14 Taipei == 04:14 UTC

    def test_spread_and_totals(self):
        g = self.games[0]
        sp = g.market("spread")
        assert sp is not None
        home = next(s for s in sp.selections if s.side == "home")
        self.assertEqual(home.line, -1.5)
        self.assertEqual(home.price, 2.5)
        tot = g.market("totals")
        assert tot is not None
        self.assertEqual(tot.line, 7.5)
        self.assertEqual(tot.price("over"), 1.85)
        self.assertEqual(tot.price("under"), 1.85)
        self.assertIsNone(g.market("nope"))

    def test_football_1x2_with_draw(self):
        g = self.games[1]
        self.assertEqual(g.home_key, "arsenal")
        self.assertEqual(g.away_key, "chelsea")
        ml = g.market("h2h")
        assert ml is not None
        self.assertEqual(ml.price("home"), 1.7)
        self.assertEqual(ml.price("draw"), 3.6)
        self.assertEqual(ml.price("away"), 4.3)
        self.assertEqual(g.market("totals").line, 2.5)

    def test_unknown_team_keeps_raw_name(self):
        self.assertEqual(taiwan.tw_canonical("神秘隊"), "神秘隊")
        self.assertEqual(taiwan.tw_canonical("曼聯"), "manchester united")
        self.assertEqual(taiwan.tw_canonical("辛辛那堤紅人"), "cincinnati reds")
        self.assertEqual(taiwan.tw_canonical("奧克蘭運動家"), "athletics")

    def test_load_latest_picks_newest_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "football"
            folder.mkdir()
            (folder / "2026-10-13-0800.json").write_text("[]", encoding="utf-8")
            (folder / "2026-10-14-0800.json").write_text((FIXTURES / "tw_games.json").read_text(encoding="utf-8"), encoding="utf-8")
            games, path, report = taiwan.load_latest(Path(tmp), "football")
            self.assertEqual(path.name, "2026-10-14-0800.json")
            self.assertEqual(len(games), 3)
            stamp = taiwan.stamp_from_name(path)
            self.assertEqual((stamp.year, stamp.month, stamp.day, stamp.hour), (2026, 10, 14, 8))
            games, path, _ = taiwan.load_latest(Path(tmp), "baseball")
            self.assertEqual(games, [])
            self.assertIsNone(path)

    def test_non_list_payload(self):
        self.assertEqual(taiwan.parse_games({"oops": 1}), [])


if __name__ == "__main__":
    unittest.main()
