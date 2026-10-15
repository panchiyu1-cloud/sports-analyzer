"""scripts/backtest_combos.py: filter search, relations and scoring (no data needed)."""

import importlib.util
import sys
import unittest
from pathlib import Path

from tests import conftest  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "backtest_combos", ROOT / "scripts" / "backtest_combos.py"
)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)  # type: ignore[union-attr]

from sports_analyzer.analysis.fade import score  # noqa: E402


class ScoringTests(unittest.TestCase):
    def test_summed_rows_score_like_fade_score(self):
        # The two bets of test_fade: a fair 50% that wins at 2.10, a fair 25% that loses.
        bets = [
            {"hit": 1.0, "prob": 0.5, "intl": 2.10, "best": 2.20, "tw": 1.72},
            {"hit": 0.0, "prob": 0.25, "intl": 3.80, "best": 4.00, "tw": 3.45},
        ]
        sums = [sum(col) for col in zip(*(bc.stat_row(b) for b in bets), strict=True)]
        got = bc.as_score(bc.sums_score(sums))
        self.assertEqual(got, score(bets, bc.LEVELS))
        self.assertEqual((got["hit"], got["fair"], got["gap_pp"]), (50.0, 37.5, 12.5))
        self.assertEqual((got["roi_intl"], got["roi_tw"]), (5.0, -14.0))  # (1.10 - 1) / 2
        self.assertEqual(bc.as_score(bc.sums_score([0.0] * bc.NC)), {"n": 0})

    def test_relation_of_a_condition_to_a_bet(self):
        self.assertEqual(bc.relation("mlb", "fav_ml", "dog_ml"), "fade")
        self.assertEqual(bc.relation("mlb", "dog_rl", "dog_ml"), "pick")  # same team
        self.assertEqual(bc.relation("mlb", "over", "fav_ml"), "ctx")
        self.assertEqual(bc.relation("football", "SH", "NH"), "fade")  # draw or away
        self.assertEqual(bc.relation("football", "SA", "SH"), "fade")  # home is not away
        self.assertEqual(bc.relation("football", "SH", "SH"), "pick")
        self.assertEqual(bc.relation("football", "SH", "NA"), "ctx")  # home or draw: half
        self.assertEqual(bc.relation("football", "TO", "TU"), "fade")


def synthetic_board():
    """300 games, the first 150 early. A on 0-199, B on 0-99 (inside A), C on 100-249,
    D on the same games as C, K on every game; only the underdog moneyline is priced."""
    rows = []
    for g in range(300):
        active = {"K": "fav_ml"}
        if g < 200:
            active["A"] = "fav_ml"
        if g < 100:
            active["B"] = "fav_ml"
        if 100 <= g < 250:
            active["C"] = "dog_ml"
            active["D"] = "dog_rl"
        bet = {"hit": float(g % 3 == 0), "prob": 0.4, "intl": 2.4, "best": 2.5, "tw": 2.15}
        bets = dict.fromkeys(bc.MLB_BETS)
        bets["dog_ml"] = bet
        rows.append({"season": "2021" if g < 150 else "2024", "test": g >= 150,
                     "active": active, "bets": bets})  # fmt: skip
    strategies = [(k, "mine" if k == "A" else "public", f"strategy {k}", None) for k in "ABCDK"]
    return bc.Board("mlb", rows, strategies)


class SearchTests(unittest.TestCase):
    def test_filters_skip_conditions_that_change_nothing(self):
        board = synthetic_board()
        # K fires every game (filters nothing); D is C under another name
        self.assertEqual(board.conds, [("A", "fav_ml"), ("B", "fav_ml"), ("C", "dog_ml")])
        self.assertEqual(board.aliases[("C", "dog_ml")], [("C", "dog_ml"), ("D", "dog_rl")])
        found = bc.search(board, min_train=50)
        names = [tuple(board.conds[i][0] for i in c) for c in found["conds"]]
        # A & B is B again, B & C never meet: only A & C is a real pair
        self.assertEqual(sorted(names), [("A",), ("A", "C"), ("B",), ("C",)])
        pair = names.index(("A", "C"))
        train = bc.as_score(bc.sums_score(found["train"][pair]))
        test = bc.as_score(bc.sums_score(found["test"][pair]))
        self.assertEqual((train["n"], test["n"]), (50, 50))  # games 100-149 / 150-199
        self.assertEqual(board.rel(2, "dog_ml"), "pick")
        self.assertEqual(board.rel(0, "dog_ml"), "fade")
        text = board.describe("dog_ml", (0, 2))["text"]
        self.assertEqual(text, "押冷門不讓分｜反：strategy A　＆　跟：strategy C＝strategy D")


if __name__ == "__main__":
    unittest.main()
