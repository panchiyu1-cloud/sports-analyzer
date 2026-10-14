import json
import tempfile
import unittest
from datetime import UTC
from pathlib import Path

from sports_analyzer.fetch import taiwan
from tests import conftest  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"


class TaiwanFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = taiwan.ParseReport()
        cls.games = taiwan.parse_games(
            json.loads((FIXTURES / "tw_games.json").read_text(encoding="utf-8")), cls.report
        )

    def test_counts_and_skips(self):
        self.assertEqual(self.report.games, 3)
        self.assertEqual(self.report.skipped_games, 1)
        self.assertIn("角球大小", self.report.unknown_markets)
        self.assertEqual(self.report.unknown_teams, {"神秘隊", "未知聯"})

    def test_baseball_game_and_prices(self):
        g = self.games[0]
        self.assertEqual(g.home_key, "los angeles dodgers")
        self.assertEqual(g.away_key, "philadelphia phillies")
        self.assertEqual(g.kickoff.tzinfo, UTC)
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
        self.assertIsNone(g.market("spread"))  # "讓分 1:0" is a three-way handicap...
        hcp = g.market("hcp3")  # ...priced from the market-implied model instead
        assert hcp is not None
        self.assertEqual(hcp.line, -1)  # one goal taken off the home score
        self.assertEqual([s.side for s in hcp.selections], ["home", "draw", "away"])

    def test_team_totals_are_not_match_totals(self):
        g = self.games[0]
        self.assertEqual([m.line for m in g.markets if m.kind == "totals"], [7.5])
        team = g.market("team_totals")
        assert team is not None
        self.assertEqual((team.subject, team.line), ("洛杉磯道奇", 4.5))
        self.assertEqual([s.side for s in team.selections], ["over", "under"])
        self.assertIsNone(g.market("totals", line=8.5))

    def test_chinese_name_resolution_rules(self):
        self.assertEqual(taiwan.tw_canonical("PSV恩荷芬"), "psv")
        self.assertEqual(taiwan.tw_canonical("LASK林茨"), "lask")
        self.assertEqual(taiwan.tw_canonical("維京足球會"), "viking")
        self.assertEqual(taiwan.tw_canonical("薩巴赫.馬薩齊爾"), "sabah")
        self.assertEqual(taiwan.tw_canonical("頓涅茨克礦工"), "shakhtar")
        self.assertEqual(taiwan.tw_canonical("AEK雅典"), "aek athens")

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
            (folder / "2026-10-14-0800.json").write_text(
                (FIXTURES / "tw_games.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            games, path, report = taiwan.load_latest(Path(tmp), "football")
            self.assertEqual(path.name, "2026-10-14-0800.json")
            self.assertEqual(len(games), 3)
            stamp = taiwan.stamp_from_name(path)
            self.assertEqual((stamp.year, stamp.month, stamp.day, stamp.hour), (2026, 10, 14, 8))
            games, path, _ = taiwan.load_latest(Path(tmp), "baseball")
            self.assertEqual(games, [])
            self.assertIsNone(path)

    def test_normalize_uploads_renames_to_arrival_time(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "football" / "2026" / "10").mkdir(parents=True)
            odd = root / "football" / "2026" / "10" / "21"  # what the phone produced once
            odd.write_text("[]", encoding="utf-8")
            wrong_day = root / "football" / "20261021-0800.json"
            wrong_day.write_text("[1]", encoding="utf-8")
            done = root / "football" / "2026-10-13-2000.json"
            done.write_text("[2]", encoding="utf-8")
            # 2026-10-14 00:00 UTC = 08:00 Taipei; 2026-10-13 16:48 UTC = 00:48 Taipei
            os.utime(wrong_day, (1791936000, 1791936000))
            os.utime(odd, (1791910080, 1791910080))
            moves = taiwan.normalize_uploads(root)
            names = sorted(p.name for p in (root / "football").iterdir())
            self.assertEqual(len(moves), 2)
            self.assertEqual(
                names, ["2026-10-13-2000.json", "2026-10-14-0048.json", "2026-10-14-0800.json"]
            )
            self.assertFalse((root / "football" / "2026").exists())  # empty folders removed
            # The phone can reuse its name now; a second copy of the same upload is dropped.
            again = root / "football" / "20261021-0800.json"
            again.write_text("[1]", encoding="utf-8")
            os.utime(again, (1791936000, 1791936000))
            taiwan.normalize_uploads(root)
            self.assertEqual(len(list((root / "football").iterdir())), 3)
            stamp = taiwan.captured_at(root / "football" / "2026-10-14-0800.json")
            self.assertEqual((stamp.day, stamp.hour, stamp.minute), (14, 0, 0))  # UTC

    def test_non_list_payload(self):
        self.assertEqual(taiwan.parse_games({"oops": 1}), [])
        self.assertEqual(taiwan.parse_games("nope"), [])

    def test_parse_text_accepts_shortcut_reserialisations(self):
        raw = (FIXTURES / "tw_games.json").read_text(encoding="utf-8")
        games = json.loads(raw)
        # 1. the feed as-is
        self.assertEqual(len(taiwan.parse_text(raw)), 4)
        # 2. one object per line (what Shortcuts' Combine Text produces)
        ndjson = "\n".join(json.dumps(g, ensure_ascii=False) for g in games)
        self.assertEqual(len(taiwan.parse_text(ndjson)), 4)
        # 3. pretty-printed objects joined by newlines, no brackets, with a BOM
        pretty = "﻿" + "\n".join(json.dumps(g, ensure_ascii=False, indent=2) for g in games)
        self.assertEqual(len(taiwan.parse_text(pretty)), 4)
        # 4. comma-separated objects without brackets
        commas = ",".join(json.dumps(g, ensure_ascii=False) for g in games)
        self.assertEqual(len(taiwan.parse_text(commas)), 4)
        # 5. a single object
        self.assertEqual(
            len(taiwan.parse_games(taiwan.parse_text(json.dumps(games[0], ensure_ascii=False)))), 1
        )
        # garbage lines are skipped, valid ones kept
        mixed = "not json at all\n" + json.dumps(games[0], ensure_ascii=False) + "\n???\n"
        self.assertEqual(len(taiwan.parse_text(mixed)), 1)


if __name__ == "__main__":
    unittest.main()
