import json
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests import conftest  # noqa: F401

from sports_analyzer import ledger
from sports_analyzer.analysis import football as analysis
from sports_analyzer.config import COMPETITION_BY_CODE, ModelConfig
from sports_analyzer.fetch import football, odds
from sports_analyzer.models import dixon_coles as dc
from sports_analyzer.render.cards import render_index

FIXTURES = Path(__file__).parent / "fixtures"
PL_TEAMS = [
    "arsenal", "chelsea", "wolves", "brighton", "liverpool", "manchester city", "manchester united",
    "tottenham", "newcastle", "aston villa", "everton", "fulham", "brentford", "crystal palace",
    "west ham", "nottingham forest", "bournemouth", "leeds", "burnley", "sunderland",
]


def synthetic_pl_results(seed: int = 3) -> list[dc.MatchResult]:
    """Two full rounds of a 20-team league with Arsenal strong and Wolves weak."""
    rng = random.Random(seed)
    strength = {t: rng.uniform(-0.3, 0.3) for t in PL_TEAMS}
    strength["arsenal"] = 0.6
    strength["wolves"] = -0.6
    results = []
    day = date(2026, 3, 1)
    for h in PL_TEAMS:
        for a in PL_TEAMS:
            if h == a:
                continue
            lam = 1.4 * (2.718 ** (strength[h] - strength[a] * 0.5))
            mu = 1.1 * (2.718 ** (strength[a] - strength[h] * 0.5))
            results.append(dc.MatchResult(day, h, a, _pois(rng, lam), _pois(rng, mu)))
            day += timedelta(hours=12)
    return results


def _pois(rng, lam):
    import math
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class BuildCardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = dc.fit(synthetic_pl_results(), as_of=date(2026, 10, 16))
        cls.matches = football.parse_matches(load("fd_matches.json"))
        cls.fixtures = [m for m in cls.matches if m.status in football.UPCOMING_STATUSES]
        cls.results = [m for m in cls.matches if m.is_finished]
        cls.standings = football.parse_standings(load("fd_standings.json"))
        cls.events = odds.parse_events(load("odds_events.json"))
        cls.cfg = ModelConfig()

    def build(self, cfg=None):
        return analysis.build_cards(
            COMPETITION_BY_CODE["PL"], self.fixtures, self.model, self.standings,
            self.results, [], self.events, cfg or self.cfg,
        )

    def test_match_odds_by_canonical_key(self):
        fx = next(f for f in self.fixtures if f.id == 500002)
        ev = analysis.match_odds(fx, self.events)
        assert ev is not None
        self.assertEqual(ev.id, "def456")

    def test_cards_have_model_market_and_edges(self):
        cards, signals = self.build()
        self.assertEqual(len(cards), 2)
        ars = next(c for c in cards if c["home_key"] == "arsenal")
        self.assertTrue(ars["model"]["fitted"])
        self.assertTrue(ars["model"]["data_ok"])
        self.assertAlmostEqual(ars["model"]["p_home"] + ars["model"]["p_draw"] + ars["model"]["p_away"], 1.0, places=3)
        self.assertEqual(ars["market"]["books"], 3)
        self.assertEqual(ars["market"]["totals"]["line"], 2.5)
        self.assertEqual(ars["market"]["spread"]["line"], -0.75)
        self.assertEqual(ars["market"]["h2h"]["home"]["best"], 1.84)
        self.assertEqual(ars["market"]["h2h"]["home"]["book"], "betfair_ex_eu")
        for key in ("home", "draw", "away", "over", "under", "spread_home", "spread_away"):
            self.assertIn(key, ars["edges"])
        self.assertEqual(ars["form"]["home"], "W,W,D,W,W")
        self.assertEqual(ars["table"]["away"]["position"], 4)
        self.assertEqual(ars["h2h"][0]["score"], "1-1")
        self.assertEqual(ars["kickoff_local"], "10/17 (週六) 22:00")

    def test_arsenal_is_strong_favourite_in_model(self):
        cards, _ = self.build()
        ars = next(c for c in cards if c["home_key"] == "arsenal")
        self.assertGreater(ars["model"]["p_home"], 0.55)

    def test_recommendation_respects_threshold(self):
        # With an impossible threshold nothing is recommended.
        cards, signals = self.build(ModelConfig(edge_threshold=0.9, edge_mid=0.95, edge_high=0.99))
        self.assertTrue(all(c["recommendation"] is None for c in cards))
        self.assertEqual(signals, [])
        # With a zero threshold every card with a market gets one.
        cards, signals = self.build(ModelConfig(edge_threshold=0.0, edge_mid=0.07, edge_high=0.10, min_model_prob=0.0))
        with_market = [c for c in cards if c["market"]]
        self.assertEqual(len(signals), len(with_market))
        s = signals[0]
        for key in ("selection", "model_p", "market_p", "price", "book", "edge", "confidence", "kelly", "kickoff_local", "competition_name"):
            self.assertIn(key, s)

    def test_missing_odds_adds_note(self):
        cards, _ = analysis.build_cards(
            COMPETITION_BY_CODE["PL"], self.fixtures, self.model, self.standings, self.results, [], [], self.cfg
        )
        self.assertTrue(all(c["market"] is None for c in cards))
        self.assertTrue(any("賠率" in n for n in cards[0]["notes"]))

    def test_insufficient_data_team_gets_no_signal(self):
        # Regression for the first live run: a club with one fitted match produced a 97% "edge".
        thin = synthetic_pl_results() + [dc.MatchResult(date(2026, 10, 1), "club brugge", "arsenal", 0, 3)]
        model = dc.fit(thin, as_of=date(2026, 10, 16))
        fx = football.Match(9, "CL", datetime(2026, 10, 17, 14, tzinfo=timezone.utc), "TIMED", 3, "Arsenal FC", "Club Brugge KV")
        ev = odds.OddsEvent("x", "soccer_uefa_champs_league", fx.utc_date, "Arsenal", "Club Brugge",
                            (odds.BookOdds("pinnacle", {"home": 1.5, "draw": 4.5, "away": 6.5}),))
        cards, signals = analysis.build_cards(
            COMPETITION_BY_CODE["CL"], [fx], model, [], [], [], [ev], ModelConfig(edge_threshold=0.0, min_model_prob=0.0)
        )
        self.assertTrue(cards[0]["model"]["fitted"])
        self.assertFalse(cards[0]["model"]["data_ok"])
        self.assertIsNotNone(cards[0]["market"])  # odds matched via aliases
        self.assertIsNone(cards[0]["recommendation"])
        self.assertEqual(signals, [])
        self.assertTrue(any("資料不足" in n for n in cards[0]["notes"]))

    def test_unknown_team_adds_note_without_crash(self):
        stranger = football.Match(1, "PL", datetime(2026, 10, 17, 14, tzinfo=timezone.utc), "TIMED", 8, "Fictional Town FC", "Arsenal FC")
        cards, _ = analysis.build_cards(COMPETITION_BY_CODE["PL"], [stranger], self.model, [], [], [], [], self.cfg)
        self.assertFalse(cards[0]["model"]["fitted"])
        self.assertIsNone(cards[0]["recommendation"])

    def test_snapshot_is_json_serialisable_and_renders(self):
        cards, signals = self.build(ModelConfig(edge_threshold=0.0, min_model_prob=0.0))
        snapshot = {
            "generated_at": "2026-10-16T00:15:00+00:00",
            "credits_remaining": 480,
            "competitions": [{"code": "PL", "name": "英超", "model": {"n_matches": self.model.n_matches, "home_advantage": 0.25}, "cards": cards, "signals": signals}],
        }
        text = json.dumps(snapshot, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as tmp:
            out = render_index(json.loads(text), None, {"pending": 1, "settled": 0, "wins": 0, "losses": 0, "hit_rate": None, "units": 0.0, "roi": None}, datetime.now(timezone.utc), Path(tmp) / "index.html")
            html = out.read_text(encoding="utf-8")
        self.assertIn("Arsenal FC", html)
        self.assertIn("英超", html)
        self.assertIn("模型分歧", html)  # signals are labelled as disagreement until validated
        self.assertIn("不是下注建議", html)
        self.assertIn("The Odds API 剩餘點數：480", html)
        self.assertNotIn("{{", html)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.csv"
        self.signal = {
            "sport": "football", "competition": "PL", "competition_name": "英超", "match_id": 500001,
            "kickoff_utc": "2026-10-17T14:00:00+00:00", "kickoff_local": "10/17 (週六) 22:00",
            "home": "Arsenal FC", "away": "Chelsea FC", "market": "1X2", "selection_key": "home",
            "selection": "主勝 Arsenal FC", "line": None, "model_p": 0.6, "market_p": 0.52,
            "price": 1.84, "book": "betfair_ex_eu", "edge": 0.08, "confidence": "中", "kelly": 0.03,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_is_idempotent(self):
        self.assertEqual(ledger.append_signals(self.path, [self.signal]), 1)
        self.assertEqual(ledger.append_signals(self.path, [self.signal]), 0)
        rows = ledger.read(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["line"], "")

    def test_settle_win_and_summary(self):
        ledger.append_signals(self.path, [self.signal])
        result = football.Match(500001, "PL", datetime(2026, 10, 17, 14, tzinfo=timezone.utc), "FINISHED", 8, "Arsenal FC", "Chelsea FC", 2, 1)
        self.assertEqual(ledger.settle_football(self.path, [result]), 1)
        rows = ledger.read(self.path)
        self.assertEqual(rows[0]["status"], "win")
        self.assertEqual(rows[0]["result"], "2-1")
        self.assertAlmostEqual(float(rows[0]["profit"]), 0.84)
        s = ledger.summary(rows)
        self.assertEqual(s["wins"], 1)
        self.assertEqual(s["hit_rate"], 1.0)
        self.assertAlmostEqual(s["units"], 0.84)

    def test_grading_rules(self):
        self.assertEqual(ledger.grade("home", None, 2, 1), "win")
        self.assertEqual(ledger.grade("draw", None, 1, 1), "win")
        self.assertEqual(ledger.grade("away", None, 1, 1), "loss")
        self.assertEqual(ledger.grade("over", 2.5, 2, 1), "win")
        self.assertEqual(ledger.grade("under", 3.0, 2, 1), "push")
        self.assertEqual(ledger.grade("spread_home", -0.5, 2, 1), "win")
        self.assertEqual(ledger.grade("spread_home", -1.0, 2, 1), "push")
        self.assertEqual(ledger.grade("spread_away", -1.0, 2, 1), "push")
        self.assertEqual(ledger.grade("spread_away", 0.5, 1, 1), "loss")  # away gives -0.5, draw loses
        self.assertEqual(ledger.grade("spread_away", -0.5, 1, 1), "win")  # away gets +0.5, draw wins
        self.assertEqual(ledger.grade("spread_home", -0.75, 2, 1), "win")  # +0.25 net


if __name__ == "__main__":
    unittest.main()
