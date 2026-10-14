"""Market-implied goal model: the scoring rates that reproduce the market's own prices.

The backtest showed the market is more accurate than our Dixon-Coles fit, so instead
of predicting, this module *translates*: it finds the home and away goal rates
(lambda, mu) and the Dixon-Coles low-score correction rho whose score distribution
has exactly the market's fair 1X2 probabilities and, when known, its fair
probability of over 2.5 goals. That distribution then prices the markets 運彩 sells
but the international books are not asked for: totals at other lines, team totals,
both teams to score, odd/even, three-way handicaps and first-half markets.

Accuracy is measured, not assumed: scripts/validate_implied.py checks the derived
prices against closing Asian handicap prices and against results over ~4,000 matches,
and `config.ModelConfig` sets per-market value thresholds from that error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

from .dixon_coles import score_matrix

# Share of a match's goals scored before half-time (football-data.co.uk 2024-2026,
# five leagues, 3,689 matches: 0.440; measured in scripts/validate_implied.py).
FIRST_HALF_SHARE = 0.44
# Low-score correction used when the market gives no totals price to pin it down.
DEFAULT_RHO = -0.05
MAX_GOALS = 10


@dataclass(frozen=True)
class ImpliedGoals:
    """A full score distribution consistent with the market's prices."""

    lam: float  # expected home goals
    mu: float  # expected away goals
    rho: float
    fit_error: float  # largest gap between fitted and market probabilities
    matrix: np.ndarray  # P(home goals = i, away goals = j)

    # ---- full time
    def p_1x2(self) -> tuple[float, float, float]:
        m = self.matrix
        return float(np.tril(m, -1).sum()), float(np.trace(m)), float(np.triu(m, 1).sum())

    def total_over(self, line: float) -> float:
        """P(over | no push) on total goals; quarter lines settle as two half stakes."""
        return _two_way(self._total_split(line))

    def _total_split(self, line: float) -> tuple[float, float, float]:
        if _is_quarter(line):
            return _average(self._total_split(line - 0.25), self._total_split(line + 0.25))
        n = self.matrix.shape[0]
        total = np.add.outer(np.arange(n), np.arange(n))
        return (
            float(self.matrix[total > line + 1e-9].sum()),
            float(self.matrix[total < line - 1e-9].sum()),
            float(self.matrix[np.abs(total - line) <= 1e-9].sum()),
        )

    def team_over(self, side: str, line: float) -> float:
        """P(one team's goals > line | no push), side = "home" or "away"; quarter lines
        settle as two half stakes, like every other line."""
        return _two_way(self._team_split(side, line))

    def _team_split(self, side: str, line: float) -> tuple[float, float, float]:
        if _is_quarter(line):
            return _average(
                self._team_split(side, line - 0.25), self._team_split(side, line + 0.25)
            )
        marginal = self.matrix.sum(axis=1) if side == "home" else self.matrix.sum(axis=0)
        goals = np.arange(marginal.shape[0])
        return (
            float(marginal[goals > line + 1e-9].sum()),
            float(marginal[goals < line - 1e-9].sum()),
            float(marginal[np.abs(goals - line) <= 1e-9].sum()),
        )

    def both_score(self) -> float:
        return float(self.matrix[1:, 1:].sum())

    def odd_total(self) -> float:
        n = self.matrix.shape[0]
        total = np.add.outer(np.arange(n), np.arange(n))
        return float(self.matrix[total % 2 == 1].sum())

    def handicap3(self, home_handicap: int) -> tuple[float, float, float]:
        """Three-way handicap (運彩 讓分 1:0 style): the result after adding
        `home_handicap` goals to the home score -> P(home, draw, away)."""
        n = self.matrix.shape[0]
        margin = np.subtract.outer(np.arange(n), np.arange(n)) + home_handicap
        return (
            float(self.matrix[margin > 0].sum()),
            float(self.matrix[margin == 0].sum()),
            float(self.matrix[margin < 0].sum()),
        )

    def asian_home(self, home_line: float) -> float:
        """P(home covers | no push) on an Asian handicap line (for validation)."""
        return _two_way(self._ah_split(home_line))

    def _ah_split(self, home_line: float) -> tuple[float, float, float]:
        if _is_quarter(home_line):
            return _average(self._ah_split(home_line - 0.25), self._ah_split(home_line + 0.25))
        n = self.matrix.shape[0]
        margin = np.subtract.outer(np.arange(n), np.arange(n)) + home_line
        return (
            float(self.matrix[margin > 1e-9].sum()),
            float(self.matrix[margin < -1e-9].sum()),
            float(self.matrix[np.abs(margin) <= 1e-9].sum()),
        )

    # ---- first half
    def first_half(self, share: float = FIRST_HALF_SHARE) -> ImpliedGoals:
        """Goals before half-time: the same teams at `share` of the full-match rates.

        No low-score correction: it describes full-match scorelines.
        """
        lam, mu = self.lam * share, self.mu * share
        return ImpliedGoals(lam, mu, 0.0, self.fit_error, score_matrix(lam, mu, 0.0, MAX_GOALS))


def fit_implied(
    p_home: float,
    p_draw: float,
    p_away: float,
    p_over: float | None = None,
    line: float = 2.5,
) -> ImpliedGoals:
    """The (lambda, mu, rho) whose score distribution has the given fair probabilities.

    `p_over` is the fair probability of over `line` total goals (the market's main
    line, usually 2.5); without it rho is fixed and only the 1X2 is matched.
    """
    total = p_home + p_draw + p_away
    target = np.array([p_home, p_draw, p_away]) / total
    use_totals = p_over is not None and 0.02 < p_over < 0.98

    def unpack(x: np.ndarray) -> tuple[float, float, float]:
        rho = float(x[2]) if use_totals else DEFAULT_RHO
        return math.exp(x[0]), math.exp(x[1]), rho

    def residuals(x: np.ndarray) -> np.ndarray:
        lam, mu, rho = unpack(x)
        m = score_matrix(lam, mu, rho, MAX_GOALS)
        got = [np.tril(m, -1).sum(), np.trace(m), np.triu(m, 1).sum()]
        res = list(np.array(got) - target)
        if use_totals:
            res.append(ImpliedGoals(lam, mu, rho, 0.0, m).total_over(line) - p_over)
        return np.array(res)

    # Start from a sensible split: total goals ~2.6, supremacy from the 1X2 gap.
    sup = (target[0] - target[2]) * 1.6
    x0 = [math.log(max(0.2, 1.3 + sup / 2)), math.log(max(0.2, 1.3 - sup / 2))]
    bounds = [(math.log(0.05), math.log(6.0))] * 2
    if use_totals:
        x0.append(DEFAULT_RHO)
        bounds.append((-0.3, 0.2))
    result = minimize(
        lambda x: float((residuals(x) ** 2).sum()),
        np.array(x0),
        method="L-BFGS-B",
        bounds=bounds,
        # The residuals are probabilities: stop at ~1e-7, not at scipy's looser default.
        options={"ftol": 1e-15, "gtol": 1e-10},
    )
    lam, mu, rho = unpack(result.x)
    error = float(np.abs(residuals(result.x)).max())
    return ImpliedGoals(lam, mu, rho, error, score_matrix(lam, mu, rho, MAX_GOALS))


# -------------------------------------------------------------- calibration
@dataclass(frozen=True)
class Calibration:
    """Per-market correction learned from results: logit(p) -> a + b * logit(p).

    The Dixon-Coles shape reproduces the market's 1X2 and totals exactly, but over
    ~3,700 matches it under-stated both-teams-score, odd totals and away scoring
    and over-stated two-goal home wins (scripts/validate_implied.py). Each derived
    market (kind@line, e.g. "hcp3_draw@-1") gets its own (a, b), kept only where it
    beat the raw price out of sample. Unknown markets pass through unchanged.
    """

    params: dict[str, tuple[float, float]]

    @classmethod
    def from_json(cls, text: str) -> Calibration:
        import json

        raw = json.loads(text)
        return cls({k: (float(v[0]), float(v[1])) for k, v in raw.get("params", {}).items()})

    def apply(self, kind: str, p: float) -> float:
        if kind not in self.params or not 0.0 < p < 1.0:
            return p
        a, b = self.params[kind]
        z = a + b * math.log(p / (1 - p))
        return 1 / (1 + math.exp(-z))

    def apply3(
        self, prefix: str, probs: tuple[float, float, float], suffix: str = ""
    ) -> tuple[float, float, float]:
        """Calibrate a three-way market outcome by outcome, then renormalise.

        Keys look like "hcp3_draw@-1" (prefix "hcp3", suffix "@-1") or "ht_home".
        """
        outcomes = ("home", "draw", "away")
        adj = [self.apply(f"{prefix}_{k}{suffix}", p) for k, p in zip(outcomes, probs, strict=True)]
        total = sum(adj)
        return adj[0] / total, adj[1] / total, adj[2] / total


IDENTITY = Calibration({})


# ----------------------------------------------------------------------- helpers
def _is_quarter(line: float) -> bool:
    return abs(line * 2 - round(line * 2)) > 1e-9


def _average(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2


def _two_way(split: tuple[float, float, float]) -> float:
    win, lose, _ = split
    return win / (win + lose) if win + lose > 0 else 0.0
