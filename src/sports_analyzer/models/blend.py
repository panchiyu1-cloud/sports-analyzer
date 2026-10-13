"""Market-anchored blend: combine model and market probabilities in log-odds space.

The closing market is the best public forecast there is. Rather than betting
the raw model against it, we learn how much the model adds *on top of* the
market from past out-of-sample predictions:

    score_i = a * log(model_i) + b * log(market_i) + c_i        (1X2, softmax)
    logit(p) = a * logit(model) + b * logit(market) + c          (two-way, e.g. over 2.5)

If `a` comes out near 0 the model carries no extra information and the blend
collapses to the market, which is the honest answer. Weights are fitted by
maximum likelihood with a light ridge penalty, always on data strictly before
the matches being predicted (the backtest enforces this).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

EPS = 1e-6


@dataclass(frozen=True)
class BlendWeights:
    """Weights for the three-way (1X2) blend. `intercepts` are (home, draw, away) with home fixed at 0."""

    a_model: float
    b_market: float
    intercepts: tuple[float, float, float]
    n_samples: int

    def apply(
        self, model_p: Sequence[float], market_p: Sequence[float]
    ) -> tuple[float, float, float]:
        m = np.clip(np.asarray(model_p, dtype=float), EPS, 1 - EPS)
        k = np.clip(np.asarray(market_p, dtype=float), EPS, 1 - EPS)
        score = self.a_model * np.log(m) + self.b_market * np.log(k) + np.asarray(self.intercepts)
        score -= score.max()
        p = np.exp(score)
        p /= p.sum()
        return float(p[0]), float(p[1]), float(p[2])


@dataclass(frozen=True)
class BinaryBlendWeights:
    a_model: float
    b_market: float
    intercept: float
    n_samples: int

    def apply(self, model_p: float, market_p: float) -> float:
        z = self.a_model * _logit(model_p) + self.b_market * _logit(market_p) + self.intercept
        return 1.0 / (1.0 + math.exp(-z))


def _logit(p: float) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


# ------------------------------------------------------------------ fitting (1X2)
def fit_blend(
    model_probs: Sequence[Sequence[float]],
    market_probs: Sequence[Sequence[float]],
    outcomes: Sequence[int],
    ridge: float = 1.0,
) -> BlendWeights:
    """Fit the three-way blend. `outcomes` are 0=home, 1=draw, 2=away.

    The ridge penalty pulls (a, b) towards (0, 1) — i.e. towards "just use the
    market" — so with little data the blend stays conservative.
    """
    M = np.log(np.clip(np.asarray(model_probs, dtype=float), EPS, 1 - EPS))
    K = np.log(np.clip(np.asarray(market_probs, dtype=float), EPS, 1 - EPS))
    y = np.asarray(outcomes, dtype=int)
    n = len(y)
    if n < 50:
        raise ValueError(f"need at least 50 samples to fit a blend, got {n}")
    rows = np.arange(n)

    def nll(params: np.ndarray) -> float:
        a, b, c_draw, c_away = params
        score = a * M + b * K + np.array([0.0, c_draw, c_away])
        score -= score.max(axis=1, keepdims=True)
        logp = score - np.log(np.exp(score).sum(axis=1, keepdims=True))
        penalty = ridge * (a**2 + (b - 1.0) ** 2 + c_draw**2 + c_away**2)
        return float(-logp[rows, y].sum() + penalty)

    res = minimize(
        nll,
        x0=np.array([0.0, 1.0, 0.0, 0.0]),
        method="L-BFGS-B",
        bounds=[(-2, 3), (-2, 3), (-2, 2), (-2, 2)],
    )
    a, b, c_draw, c_away = res.x
    return BlendWeights(float(a), float(b), (0.0, float(c_draw), float(c_away)), n)


# ---------------------------------------------------------------- fitting (binary)
def fit_binary_blend(
    model_p: Sequence[float],
    market_p: Sequence[float],
    outcomes: Sequence[int],
    ridge: float = 1.0,
) -> BinaryBlendWeights:
    """Fit the two-way blend (e.g. over 2.5). `outcomes` are 1 = event happened, 0 = not."""
    m = np.array([_logit(p) for p in model_p])
    k = np.array([_logit(p) for p in market_p])
    y = np.asarray(outcomes, dtype=float)
    if len(y) < 50:
        raise ValueError(f"need at least 50 samples to fit a blend, got {len(y)}")

    def nll(params: np.ndarray) -> float:
        a, b, c = params
        z = a * m + b * k + c
        # log-loss of logistic in a numerically safe form
        loss = np.logaddexp(0, -z) * y + np.logaddexp(0, z) * (1 - y)
        return float(loss.sum() + ridge * (a**2 + (b - 1.0) ** 2 + c**2))

    res = minimize(
        nll, x0=np.array([0.0, 1.0, 0.0]), method="L-BFGS-B", bounds=[(-2, 3), (-2, 3), (-2, 2)]
    )
    a, b, c = res.x
    return BinaryBlendWeights(float(a), float(b), float(c), len(y))


# ------------------------------------------------------------------- scoring
def log_loss_3(probs: Sequence[Sequence[float]], outcomes: Sequence[int]) -> float:
    P = np.clip(np.asarray(probs, dtype=float), EPS, 1.0)
    y = np.asarray(outcomes, dtype=int)
    return float(-np.log(P[np.arange(len(y)), y]).mean())


def log_loss_2(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    p = np.clip(np.asarray(probs, dtype=float), EPS, 1 - EPS)
    y = np.asarray(outcomes, dtype=float)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())
