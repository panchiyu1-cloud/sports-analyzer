"""The live page's starting data (docs/live/data.json) and what feeds it."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sports_analyzer.analysis.taiwan import compare
from sports_analyzer.config import MODEL
from sports_analyzer.fetch.taiwan import _min_legs, parse_games
from sports_analyzer.render import live, site

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _card(**over: object) -> dict:
    card = {
        "sport": "football",
        "competition": "PL",
        "match_id": 501,
        "kickoff_utc": "2026-11-21T15:00:00+00:00",
        "home": "Arsenal FC",
        "away": "Chelsea FC",
        "home_key": "arsenal",
        "away_key": "chelsea",
        "market": {
            "books": 20,
            "h2h": {
                "home": {"fair": 0.5, "fair_odds": 2.0, "best": 2.05, "book": "x"},
                "draw": {"fair": 0.26, "fair_odds": 3.85, "best": 3.9, "book": "x"},
                "away": {"fair": 0.24, "fair_odds": 4.17, "best": 4.3, "book": "x"},
                "odds_at": "2026-11-15T11:00+00:00",
            },
            "totals": {
                "line": 2.5,
                "over": {"fair": 0.55, "fair_odds": 1.82, "best": 1.9, "book": "x"},
                "under": {"fair": 0.45, "fair_odds": 2.22, "best": 2.3, "book": "x"},
                "odds_at": "2026-11-15T08:00+00:00",
                "stale": True,
            },
            "spread": None,
        },
        "tw": {
            "game_no": "101",
            "home_zh": "兵工廠",
            "away_zh": "切爾西",
            "captured_at": "2026-11-15T20:00+08:00",
            "id": "3344.1",
            "legs": {"h2h": 1, "totals": 2, "不讓分": 1, "[總分]大小 2.5": 2, "[總分]大小 3.5": 3},
            "h2h": {
                "line": None,
                "home": {"price": 1.9, "fair_odds": 2.0, "fair_prob": 0.5, "ev": -0.05},
                "draw": {"price": 3.3, "fair_odds": 3.85, "fair_prob": 0.26, "ev": -0.142},
                "away": {"price": 3.6, "fair_odds": 4.17, "fair_prob": 0.24, "ev": -0.136},
            },
            "totals": {
                "line": 2.5,
                "over": {"price": 1.7, "fair_odds": 1.82, "fair_prob": 0.55, "ev": -0.065},
                "under": {"price": 1.95, "fair_odds": 2.22, "fair_prob": 0.45, "ev": -0.1225},
            },
            "spread": None,
            "values": [],
            "derived": [
                {
                    "market": "[總分]大小 3.5",
                    "selection_key": "under",
                    "selection": "小 3.5",
                    "line": 3.5,
                    "tw_price": 1.4,
                    "fair_odds": 1.43,
                    "fair_prob": 0.7,
                    "ev": -0.02,
                    "derived": True,
                }
            ],
        },
    }
    card.update(over)
    return card


def _site(cards: list[dict], tape: list[dict] | None = None) -> site.Site:
    football = {"competitions": [{"code": "PL", "name": "英超", "cards": cards}]}
    return site.Site(
        now=NOW,
        football=football,
        mlb=None,
        tape=tape or [],
        budget={"remaining": 288, "used": 212, "period_start": "2026-11-01", "day": "x"},
    )


class MinLegsTest(unittest.TestCase):
    def test_feed_value_becomes_a_number_of_legs(self) -> None:
        self.assertEqual(_min_legs("1"), 1)
        self.assertEqual(_min_legs("3"), 3)
        self.assertEqual(_min_legs(2), 2)

    def test_anything_else_is_unknown(self) -> None:
        for value in (None, "", "x", "0", "99", 1.5):
            self.assertIsNone(_min_legs(value), value)

    def test_parser_reads_it_from_the_fixture(self) -> None:
        payload = json.loads((FIXTURES / "tw_games.json").read_text(encoding="utf-8"))
        games = parse_games(payload)
        legs = {m.min_legs for g in games for m in g.markets}
        self.assertIn(1, legs)
        self.assertLessEqual(legs, {1, None})

    def test_compare_puts_legs_and_the_game_id_in_the_block(self) -> None:
        payload = json.loads((FIXTURES / "tw_games.json").read_text(encoding="utf-8"))
        games = parse_games(payload)
        game = games[0]
        fair = {"fair": 0.4, "fair_odds": 2.5, "best": 2.5, "book": "x"}
        sides = {"home": fair, "away": fair}
        if game.market("h2h").price("draw"):
            sides["draw"] = fair
        card = {
            "sport": "football",
            "home": "A",
            "away": "B",
            "home_key": game.home_key,
            "away_key": game.away_key,
            "market": {"h2h": {**sides, "odds_at": "2026-10-14T00:00+00:00"}},
        }
        block = compare(card, game, datetime(2026, 10, 14, 1, 0, tzinfo=UTC), MODEL)
        self.assertEqual(block["id"], game.id)
        self.assertEqual(block["legs"]["h2h"], 1)
        self.assertEqual(block["legs"][game.market("h2h").name], 1)


class LiveDataTest(unittest.TestCase):
    def test_fair_block_keeps_probabilities_line_and_age(self) -> None:
        fair = live.fair_block(_card()["market"])
        self.assertEqual(fair["books"], 20)
        self.assertEqual(
            fair["h2h"],
            {
                "home": 0.5,
                "draw": 0.26,
                "away": 0.24,
                "line": None,
                "at": "2026-11-15T11:00+00:00",
                "stale": False,
            },
        )
        self.assertEqual(fair["totals"]["line"], 2.5)
        self.assertTrue(fair["totals"]["stale"])
        self.assertIsNone(fair["spread"])
        self.assertIsNone(live.fair_block(None))
        self.assertIsNone(
            live.fair_block({"books": 0, "h2h": None, "totals": None, "spread": None})
        )

    def test_tw_block_keeps_prices_legs_and_translated_lines(self) -> None:
        tw = live.tw_block(_card()["tw"])
        self.assertEqual(tw["h2h"], {"home": 1.9, "draw": 3.3, "away": 3.6, "line": None})
        self.assertEqual(tw["totals"], {"over": 1.7, "under": 1.95, "line": 2.5})
        self.assertIsNone(tw["spread"])
        self.assertEqual(tw["legs"]["[總分]大小 3.5"], 3)
        self.assertEqual(tw["id"], "3344.1")
        self.assertEqual(
            tw["derived"],
            [
                {
                    "market": "[總分]大小 3.5",
                    "label": "小 3.5",
                    "key": "under",
                    "line": 3.5,
                    "price": 1.4,
                    "p": 0.7,
                }
            ],
        )
        self.assertIsNone(live.tw_block(None))

    def test_old_blocks_without_legs_still_load(self) -> None:
        old = {k: v for k, v in _card()["tw"].items() if k not in ("legs", "id")}
        tw = live.tw_block(old)
        self.assertEqual(tw["legs"], {})
        self.assertIsNone(tw["id"])

    def test_tape_points_group_sides_by_time_and_source(self) -> None:
        rows = [
            {
                "observed_at": "t2",
                "source": "intl",
                "market": "h2h",
                "side": "home",
                "prob": "0.52",
            },
            {"observed_at": "t1", "source": "intl", "market": "h2h", "side": "home", "prob": "0.5"},
            {
                "observed_at": "t1",
                "source": "intl",
                "market": "h2h",
                "side": "away",
                "prob": "0.24",
            },
            {"observed_at": "t1", "source": "tw", "market": "h2h", "side": "home", "prob": "0.49"},
            {
                "observed_at": "t1",
                "source": "intl",
                "market": "totals",
                "side": "over",
                "prob": "0.5",
            },
            {"observed_at": "t3", "source": "intl", "market": "h2h", "side": "home", "prob": ""},
        ]
        points = live.tape_points(rows)
        self.assertEqual(
            points["intl"], [["t1", {"home": 0.5, "away": 0.24}], ["t2", {"home": 0.52}]]
        )
        self.assertEqual(points["tw"], [["t1", {"home": 0.49}]])

    def test_live_data_lists_matches_not_started(self) -> None:
        started = _card(match_id=400, kickoff_utc="2026-11-15T10:00:00+00:00")
        tape = [
            {
                "observed_at": "2026-11-15T11:00+00:00",
                "source": "intl",
                "sport": "football",
                "match_id": "501",
                "market": "h2h",
                "side": "home",
                "prob": "0.5",
            }
        ]
        data = live.live_data(_site([started, _card()], tape))
        self.assertEqual([m["key"] for m in data["matches"]], ["football:501"])
        m = data["matches"][0]
        self.assertEqual((m["league"], m["league_name"]), ("PL", "英超"))
        self.assertEqual((m["home_zh"], m["away_zh"]), ("兵工廠", "切爾西"))
        self.assertEqual(m["tape"]["intl"], [["2026-11-15T11:00+00:00", {"home": 0.5}]])
        self.assertEqual(m["page"], "value.html#m-football-501")
        self.assertEqual(data["cfg"]["threshold"], {"football": 0.02, "mlb": 0.03, "nba": 0.03})
        self.assertEqual(
            data["budget"], {"remaining": 288, "used": 212, "period_start": "2026-11-01"}
        )
        self.assertEqual(data["generated_at"], "2026-11-15T12:00:00+00:00")
        codes = {x["code"]: x for x in data["leagues"]}
        self.assertEqual(codes["PL"]["espn"], "soccer/eng.1")
        self.assertEqual(codes["NBA"]["odds_key"], "basketball_nba")
        self.assertEqual(data["zh"]["兵工廠"], "arsenal")

    def test_a_card_without_prices_is_still_listed(self) -> None:
        bare = _card(market=None, tw=None)
        m = live.live_data(_site([bare]))["matches"][0]
        self.assertIsNone(m["fair"])
        self.assertIsNone(m["tw"])
        self.assertEqual(m["page"], "model.html#m-football-501")

    def test_no_secret_shaped_field_is_written(self) -> None:
        text = json.dumps(live.live_data(_site([_card()])), ensure_ascii=False).lower()
        for word in ("apikey", "api_key", "token", "ntfy"):
            self.assertNotIn(word, text)

    def test_write_live_writes_parseable_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = live.write_live(_site([_card()]), Path(tmp))
            self.assertEqual(path, Path(tmp) / "live" / "data.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["matches"]), 1)
            self.assertEqual(live.parse_generated(data["generated_at"]), NOW)


class LiveEngineTest(unittest.TestCase):
    """docs/live/engine.js (the page's maths) has its own tests, run with Node when the
    machine has it (GitHub's runners do)."""

    def test_engine_js(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = ROOT / "tests" / "js" / "engine_test.js"
        done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout[-3000:] + done.stderr[-3000:])


if __name__ == "__main__":
    unittest.main()
