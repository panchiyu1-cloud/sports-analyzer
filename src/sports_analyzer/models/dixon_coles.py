"""Dixon-Coles (1997) football model with exponential time decay.

Each team i has an attack strength a_i and a defence weakness d_i. For home team
h against away team a:

    expected home goals  lambda = exp(a_h + d_a + home_advantage)
    expected away goals  mu     = exp(a_a + d_h)

Goals are Poisson, with the Dixon-Coles correction tau(x, y) that fixes the
well-known under-prediction of 0-0 and 1-1 and over-prediction of 1-0 / 0-1.
Older matches count less: weight = exp(-xi * days_ago).

Fitting maximises the weighted log-likelihood with scipy. Attack strengths are
constrained to average zero so the parameters are identifiable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson


@dataclass(frozen=True)
class MatchResult:
    """A finished match, already reduced to canonical team keys."""

    day: date
    home: str
    away: str
    home_goals: int
    away_goals: int


@dataclass(frozen=True)
class Prediction:
    """Everything the cards need for one fixture."""

    home: str
    away: str
    lambda_home: float
    mu_away: float
    matrix: np.ndarray  # P(home goals = i, away goals = j), i rows, j cols

    # ---- 1X2
    @property
    def p_home(self) -> float:
        return float(np.tril(self.matrix, -1).sum())

    @property
    def p_draw(self) -> float:
        return float(np.trace(self.matrix))

    @property
    def p_away(self) -> float:
        return float(np.triu(self.matrix, 1).sum())

    # ---- totals
    def totals(self, line: float) -> tuple[float, float, float]:
        """(P over, P under, P push) for a total-goals line such as 2.5 or 3."""
        n = self.matrix.shape[0]
        idx = np.add.outer(np.arange(n), np.arange(n))
        over = float(self.matrix[idx > line].sum())
        under = float(self.matrix[idx < line].sum())
        push = float(self.matrix[idx == line].sum())
        return over, under, push

    def over_prob(self, line: float) -> float:
        """P(over | no push) — comparable to a de-vigged two-way price."""
        over, under, _ = self.totals(line)
        return over / (over + under) if over + under > 0 else 0.0

    # ---- handicap (Asian style; `home_line` is added to the home score)
    def handicap(self, home_line: float) -> tuple[float, float, float]:
        """(P home covers, P away covers, P push) for a half/integer line.

        Quarter lines (e.g. -0.25) are the average of the two neighbouring lines,
        which is exactly how a quarter-line bet settles (half stake on each).
        """
        if abs(home_line * 4 - round(home_line * 4)) > 1e-9:
            raise ValueError("line must be a multiple of 0.25")
        if abs(home_line * 2 - round(home_line * 2)) > 1e-9:  # quarter line
            lo, hi = home_line - 0.25, home_line + 0.25
            a = self.handicap(lo)
            b = self.handicap(hi)
            return tuple((x + y) / 2 for x, y in zip(a, b, strict=True))  # type: ignore[return-value]
        n = self.matrix.shape[0]
        margin = np.subtract.outer(np.arange(n), np.arange(n)) + home_line
        win = float(self.matrix[margin > 1e-9].sum())
        lose = float(self.matrix[margin < -1e-9].sum())
        push = float(self.matrix[np.abs(margin) <= 1e-9].sum())
        return win, lose, push

    def home_cover_prob(self, home_line: float) -> float:
        """P(home covers | no push)."""
        win, lose, _ = self.handicap(home_line)
        return win / (win + lose) if win + lose > 0 else 0.0

    def most_likely_score(self) -> tuple[int, int, float]:
        i, j = np.unravel_index(int(np.argmax(self.matrix)), self.matrix.shape)
        return int(i), int(j), float(self.matrix[i, j])


@dataclass(frozen=True)
class FittedModel:
    teams: tuple[str, ...]
    attack: dict[str, float]
    defence: dict[str, float]
    home_advantage: float
    rho: float
    matches_per_team: dict[str, int]
    n_matches: int
    as_of: date
    max_goals: int = 10

    def knows(self, team: str) -> bool:
        return team in self.attack

    def predict(self, home: str, away: str) -> Prediction:
        if not (self.knows(home) and self.knows(away)):
            missing = [t for t in (home, away) if not self.knows(t)]
            raise KeyError(f"no fitted data for: {', '.join(missing)}")
        lam = math.exp(self.attack[home] + self.defence[away] + self.home_advantage)
        mu = math.exp(self.attack[away] + self.defence[home])
        matrix = score_matrix(lam, mu, self.rho, self.max_goals)
        return Prediction(home=home, away=away, lambda_home=lam, mu_away=mu, matrix=matrix)

    def attack_rank(self, team: str) -> int:
        order = sorted(self.teams, key=lambda t: -self.attack[t])
        return order.index(team) + 1

    def defence_rank(self, team: str) -> int:
        # Lower defence parameter = concedes fewer = better.
        order = sorted(self.teams, key=lambda t: self.defence[t])
        return order.index(team) + 1


# ---------------------------------------------------------------- core formulas
def tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Dixon-Coles low-score correction factor."""
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(lam: float, mu: float, rho: float, max_goals: int = 10) -> np.ndarray:
    """Joint probability matrix of (home goals, away goals), truncated and renormalised."""
    goals = np.arange(max_goals + 1)
    ph = poisson.pmf(goals, lam)
    pa = poisson.pmf(goals, mu)
    matrix = np.outer(ph, pa)
    for x, y in ((0, 0), (0, 1), (1, 0), (1, 1)):
        matrix[x, y] *= max(tau(x, y, lam, mu, rho), 1e-12)
    return matrix / matrix.sum()


def time_weights(days_ago: np.ndarray, xi: float) -> np.ndarray:
    return np.exp(-xi * np.clip(days_ago, 0, None))


# ------------------------------------------------------------------------ fitting
def fit(
    results: list[MatchResult],
    as_of: date,
    xi: float = 0.0065,
    max_goals: int = 10,
) -> FittedModel:
    """Fit attack/defence/home/rho to `results` by weighted maximum likelihood."""
    if len(results) < 10:
        raise ValueError(f"need at least 10 results to fit, got {len(results)}")

    teams = tuple(sorted({r.home for r in results} | {r.away for r in results}))
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)

    h = np.array([index[r.home] for r in results])
    a = np.array([index[r.away] for r in results])
    x = np.array([r.home_goals for r in results], dtype=float)
    y = np.array([r.away_goals for r in results], dtype=float)
    days = np.array([(as_of - r.day).days for r in results], dtype=float)
    w = time_weights(days, xi)

    # Masks for the four tau cells, evaluated once.
    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)

    def unpack(params: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
        att = np.concatenate([params[: n - 1], [-params[: n - 1].sum()]])  # mean-zero attack
        dfc = params[n - 1 : 2 * n - 1]
        home_adv = params[2 * n - 1]
        rho = params[2 * n]
        return att, dfc, home_adv, rho

    def neg_log_lik(params: np.ndarray) -> float:
        att, dfc, home_adv, rho = unpack(params)
        lam = np.exp(att[h] + dfc[a] + home_adv)
        mu = np.exp(att[a] + dfc[h])
        corr = np.ones_like(lam)
        corr[m00] = 1.0 - lam[m00] * mu[m00] * rho
        corr[m01] = 1.0 + lam[m01] * rho
        corr[m10] = 1.0 + mu[m10] * rho
        corr[m11] = 1.0 - rho
        corr = np.clip(corr, 1e-10, None)
        ll = np.log(corr) + x * np.log(lam) - lam + y * np.log(mu) - mu
        return float(-(w * ll).sum())

    x0 = np.concatenate([np.zeros(n - 1), np.zeros(n), [0.25], [-0.05]])
    bounds = [(-3, 3)] * (n - 1) + [(-3, 3)] * n + [(-1, 1), (-0.3, 0.3)]
    res = minimize(neg_log_lik, x0, method="L-BFGS-B", bounds=bounds, options={"maxiter": 500})
    att, dfc, home_adv, rho = unpack(res.x)

    counts: dict[str, int] = dict.fromkeys(teams, 0)
    for r in results:
        counts[r.home] += 1
        counts[r.away] += 1

    return FittedModel(
        teams=teams,
        attack={t: float(att[i]) for t, i in index.items()},
        defence={t: float(dfc[i]) for t, i in index.items()},
        home_advantage=float(home_adv),
        rho=float(rho),
        matches_per_team=counts,
        n_matches=len(results),
        as_of=as_of,
        max_goals=max_goals,
    )
