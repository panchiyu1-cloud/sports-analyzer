import math
import random
import unittest
from datetime import date, timedelta

import numpy as np

from sports_analyzer.models import dixon_coles as dc
from tests import conftest  # noqa: F401


def synthetic_season(seed: int = 7, n_teams: int = 10, rounds: int = 4):
    """Generate matches from KNOWN parameters so the fit can be checked against truth."""
    rng = random.Random(seed)
    teams = [f"t{i}" for i in range(n_teams)]
    attack = {t: rng.uniform(-0.5, 0.5) for t in teams}
    mean_att = sum(attack.values()) / n_teams
    attack = {t: v - mean_att for t, v in attack.items()}
    defence = {t: rng.uniform(-0.4, 0.4) for t in teams}
    home_adv = 0.3  # simulated without the low-score (rho) correction
    results = []
    day = date(2026, 1, 1)
    for _ in range(rounds):
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                lam = math.exp(attack[h] + defence[a] + home_adv)
                mu = math.exp(attack[a] + defence[h])
                x = _poisson(rng, lam)
                y = _poisson(rng, mu)
                results.append(dc.MatchResult(day, h, a, x, y))
                day += timedelta(days=1)
    return teams, attack, defence, home_adv, results, day


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth's algorithm; fine for small lambda.
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


class TauAndMatrixTests(unittest.TestCase):
    def test_tau_cells(self):
        self.assertAlmostEqual(dc.tau(0, 0, 1.5, 1.2, -0.1), 1 + 1.5 * 1.2 * 0.1)
        self.assertAlmostEqual(dc.tau(1, 1, 1.5, 1.2, -0.1), 1.1)
        self.assertEqual(dc.tau(2, 3, 1.5, 1.2, -0.1), 1.0)

    def test_matrix_sums_to_one_and_1x2_partition(self):
        m = dc.score_matrix(1.6, 1.1, -0.1)
        self.assertAlmostEqual(m.sum(), 1.0, places=12)
        p = dc.Prediction("h", "a", 1.6, 1.1, m)
        self.assertAlmostEqual(p.p_home + p.p_draw + p.p_away, 1.0, places=12)
        self.assertGreater(p.p_home, p.p_away)  # home expected 1.6 vs 1.1 goals

    def test_totals_and_handicap_partitions(self):
        p = dc.Prediction("h", "a", 1.6, 1.1, dc.score_matrix(1.6, 1.1, -0.1))
        over, under, push = p.totals(2.5)
        self.assertAlmostEqual(over + under + push, 1.0, places=12)
        self.assertEqual(push, 0.0)
        over3, under3, push3 = p.totals(3)
        self.assertGreater(push3, 0.0)
        self.assertAlmostEqual(over3 + under3 + push3, 1.0, places=12)
        win, lose, push_h = p.handicap(-1)
        self.assertAlmostEqual(win + lose + push_h, 1.0, places=12)
        # -0.5 handicap on the home side == plain home win probability
        self.assertAlmostEqual(p.handicap(-0.5)[0], p.p_home, places=12)
        # +0.5 on the home side == home win or draw
        self.assertAlmostEqual(p.handicap(0.5)[0], p.p_home + p.p_draw, places=12)

    def test_quarter_line_is_average_of_neighbours(self):
        p = dc.Prediction("h", "a", 1.6, 1.1, dc.score_matrix(1.6, 1.1, -0.1))
        q = p.handicap(-0.25)
        a, b = p.handicap(0.0), p.handicap(-0.5)
        for i in range(3):
            self.assertAlmostEqual(q[i], (a[i] + b[i]) / 2, places=12)
        with self.assertRaises(ValueError):
            p.handicap(-0.3)

    def test_most_likely_score(self):
        p = dc.Prediction("h", "a", 1.6, 1.1, dc.score_matrix(1.6, 1.1, -0.1))
        i, j, prob = p.most_likely_score()
        self.assertEqual(prob, float(np.max(p.matrix)))
        self.assertIn((i, j), {(1, 0), (1, 1), (2, 1)})


class FitTests(unittest.TestCase):
    def test_recovers_known_parameters(self):
        teams, attack, defence, home_adv, results, end = synthetic_season()
        model = dc.fit(results, as_of=end, xi=0.0)  # no decay: every match counts equally
        self.assertEqual(model.n_matches, len(results))
        # Home advantage within a reasonable band of the truth.
        self.assertAlmostEqual(model.home_advantage, home_adv, delta=0.12)
        # Attack ordering should broadly agree with the truth (rank correlation).
        true_order = sorted(teams, key=lambda t: attack[t])
        fit_order = sorted(teams, key=lambda t: model.attack[t])
        agreement = sum(1 for a, b in zip(true_order, fit_order, strict=True) if a == b)
        self.assertGreaterEqual(agreement, 4)  # exact ties are unlikely; ordering mostly right
        corr = np.corrcoef([attack[t] for t in teams], [model.attack[t] for t in teams])[0, 1]
        self.assertGreater(corr, 0.8)
        corr_d = np.corrcoef([defence[t] for t in teams], [model.defence[t] for t in teams])[0, 1]
        self.assertGreater(corr_d, 0.7)
        # Mean-zero attack constraint holds.
        self.assertAlmostEqual(sum(model.attack.values()), 0.0, places=9)

    def test_predict_and_ranks(self):
        teams, _, _, _, results, end = synthetic_season()
        model = dc.fit(results, as_of=end)
        pred = model.predict(teams[0], teams[1])
        self.assertAlmostEqual(pred.p_home + pred.p_draw + pred.p_away, 1.0, places=9)
        self.assertEqual(sorted(model.attack_rank(t) for t in teams), list(range(1, 11)))
        self.assertEqual(model.matches_per_team[teams[0]], 4 * 2 * 9)
        with self.assertRaises(KeyError):
            model.predict(teams[0], "unknown team")

    def test_refuses_tiny_samples(self):
        with self.assertRaises(ValueError):
            dc.fit([dc.MatchResult(date(2026, 1, 1), "a", "b", 1, 0)], as_of=date(2026, 1, 2))

    def test_time_weights(self):
        w = dc.time_weights(np.array([0.0, 100.0, -5.0]), 0.0065)
        self.assertEqual(w[0], 1.0)
        self.assertAlmostEqual(w[1], math.exp(-0.65))
        self.assertEqual(w[2], 1.0)  # future dates are clipped, not boosted


if __name__ == "__main__":
    unittest.main()
