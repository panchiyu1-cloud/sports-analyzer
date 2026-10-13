import random
import unittest

import numpy as np

from sports_analyzer.models import blend
from tests import conftest  # noqa: F401


def _softmax(v):
    v = np.asarray(v, dtype=float)
    v = v - v.max()
    e = np.exp(v)
    return e / e.sum()


def synthetic(n: int, true_a: float, true_b: float, seed: int = 11):
    """Outcomes drawn from a known blend of two noisy forecasts."""
    rng = random.Random(seed)
    model, market, outcomes = [], [], []
    for _ in range(n):
        truth = _softmax([rng.gauss(0, 0.8), rng.gauss(-0.3, 0.4), rng.gauss(0, 0.8)])
        m = _softmax(np.log(truth) + np.array([rng.gauss(0, 0.5) for _ in range(3)]))
        k = _softmax(np.log(truth) + np.array([rng.gauss(0, 0.25) for _ in range(3)]))
        p = _softmax(true_a * np.log(m) + true_b * np.log(k))
        u = rng.random()
        outcome = 0 if u < p[0] else (1 if u < p[0] + p[1] else 2)
        model.append(list(m))
        market.append(list(k))
        outcomes.append(outcome)
    return model, market, outcomes


class BlendTests(unittest.TestCase):
    def test_apply_sums_to_one_and_market_only_reproduces_market(self):
        w = blend.BlendWeights(0.0, 1.0, (0.0, 0.0, 0.0), 0)
        p = w.apply([0.5, 0.3, 0.2], [0.4, 0.3, 0.3])
        self.assertAlmostEqual(sum(p), 1.0, places=12)
        for x, y in zip(p, (0.4, 0.3, 0.3), strict=True):
            self.assertAlmostEqual(x, y, places=9)

    def test_fit_recovers_weights_roughly(self):
        model, market, outcomes = synthetic(4000, true_a=0.4, true_b=0.7)
        w = blend.fit_blend(model, market, outcomes, ridge=0.1)
        self.assertAlmostEqual(w.a_model, 0.4, delta=0.15)
        self.assertAlmostEqual(w.b_market, 0.7, delta=0.15)
        self.assertEqual(w.n_samples, 4000)

    def test_blend_beats_both_inputs_when_both_informative(self):
        model, market, outcomes = synthetic(3000, true_a=0.5, true_b=0.6, seed=5)
        train = slice(0, 2000)
        test = slice(2000, 3000)
        w = blend.fit_blend(model[train], market[train], outcomes[train], ridge=0.1)
        blended = [w.apply(m, k) for m, k in zip(model[test], market[test], strict=True)]
        ll_blend = blend.log_loss_3(blended, outcomes[test])
        ll_market = blend.log_loss_3(market[test], outcomes[test])
        ll_model = blend.log_loss_3(model[test], outcomes[test])
        self.assertLess(ll_blend, ll_market)
        self.assertLess(ll_blend, ll_model)

    def test_useless_model_gets_near_zero_weight(self):
        model, market, outcomes = synthetic(3000, true_a=0.0, true_b=1.0, seed=9)
        w = blend.fit_blend(model, market, outcomes)
        self.assertAlmostEqual(w.a_model, 0.0, delta=0.1)
        self.assertAlmostEqual(w.b_market, 1.0, delta=0.15)

    def test_binary_blend(self):
        rng = random.Random(3)
        m, k, y = [], [], []
        for _ in range(3000):
            truth = min(max(rng.gauss(0.5, 0.15), 0.05), 0.95)
            mp = min(max(truth + rng.gauss(0, 0.12), 0.02), 0.98)
            kp = min(max(truth + rng.gauss(0, 0.05), 0.02), 0.98)
            m.append(mp)
            k.append(kp)
            y.append(1 if rng.random() < truth else 0)
        w = blend.fit_binary_blend(m, k, y, ridge=0.1)
        self.assertGreater(w.b_market, w.a_model)  # market is the sharper input here
        p = w.apply(0.6, 0.55)
        self.assertTrue(0 < p < 1)
        self.assertLess(
            blend.log_loss_2([w.apply(a, b) for a, b in zip(m, k, strict=True)], y),
            blend.log_loss_2(m, y),
        )

    def test_refuses_tiny_samples(self):
        with self.assertRaises(ValueError):
            blend.fit_blend([[0.5, 0.3, 0.2]] * 10, [[0.5, 0.3, 0.2]] * 10, [0] * 10)


if __name__ == "__main__":
    unittest.main()
