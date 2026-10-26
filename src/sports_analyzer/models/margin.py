"""Normal model of a basketball game's margin and total, anchored on the market.

The international consensus quotes one handicap line and one totals line. 運彩 often
posts a different one (half a point to a few points away, or updated later). To compare
those, the home margin M and the total T are taken as normal around the market's own
view, with the spread (sigma) measured on history:

    M ~ Normal(mu, sigma)        mu from the market's fair cover probability at its line
    T ~ Normal(mu_t, sigma_t)    mu_t from the market's fair over probability at its line

Scores are whole numbers, so every probability uses the continuity correction
(P(M >= k) = 1 - Phi((k - 0.5 - mu) / sigma)); an integer line can push.

Checked on 22,677 NBA regular-season games (2007-2025, pre-game lines taken as 50/50):
with sigma 12.5 the frequency of covering a line moved by 1-6 points was within 1pp of
the model (2019-2025), with sigma_t 18.0 totals moved by 1-8 points within 0.5pp
(2015-2025). The model translates the market; it never replaces it (an international
line at the same point is always used instead).
"""

from __future__ import annotations

import math
from statistics import NormalDist

_N = NormalDist()


def _cdf(z: float) -> float:
    return _N.cdf(z)


def at_least(k: int, mu: float, sigma: float) -> float:
    """P(X >= k) for a whole-number X ~ Normal(mu, sigma) (continuity corrected)."""
    return 1.0 - _cdf((k - 0.5 - mu) / sigma)


def exactly(k: int, mu: float, sigma: float) -> float:
    """P(X == k) for a whole-number X ~ Normal(mu, sigma)."""
    return _cdf((k + 0.5 - mu) / sigma) - _cdf((k - 0.5 - mu) / sigma)


def over_probs(mu: float, sigma: float, line: float) -> tuple[float, float, float]:
    """(P(X > line), P(X == line), P(X < line)) for a whole-number X; the middle one is
    zero unless the line is a whole number."""
    if abs(line - round(line)) < 1e-9:
        k = int(round(line))
        push = exactly(k, mu, sigma)
        over = at_least(k + 1, mu, sigma)
    else:
        push = 0.0
        over = at_least(math.ceil(line), mu, sigma)
    return over, push, max(0.0, 1.0 - over - push)


def cover_probs(mu: float, sigma: float, line: float) -> tuple[float, float, float]:
    """(home covers, push, away covers) for a home handicap `line` (negative when the
    home side gives points): the home side covers when M + line > 0."""
    return over_probs(mu, sigma, -line)


def win_prob(mu: float, sigma: float) -> float:
    """P(home wins): no ties in basketball, so P(M >= 1)."""
    return at_least(1, mu, sigma)


def conditional(win: float, push: float) -> float:
    """The probability a bet wins given it is not refunded (what a price compares with)."""
    rest = 1.0 - push
    return win / rest if rest > 0 else 0.0


def mean_from_over(line: float, p_over: float, sigma: float) -> float:
    """The mean that gives the market's fair probability of X > line (push excluded).
    Monotone in the mean, so a bisection is enough and exact for integer lines too."""
    p_over = min(max(p_over, 1e-6), 1 - 1e-6)
    lo, hi = line - 8 * sigma, line + 8 * sigma
    for _ in range(100):
        mid = (lo + hi) / 2
        over, push, _ = over_probs(mid, sigma, line)
        if conditional(over, push) < p_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def mean_from_cover(line: float, p_home_cover: float, sigma: float) -> float:
    """The expected home margin behind the market's fair probability that the home side
    covers its handicap `line`."""
    return mean_from_over(-line, p_home_cover, sigma)


def expected_score(mu: float, mu_t: float) -> tuple[float, float]:
    """(home points, away points) implied by the margin and the total."""
    return (mu_t + mu) / 2, (mu_t - mu) / 2
