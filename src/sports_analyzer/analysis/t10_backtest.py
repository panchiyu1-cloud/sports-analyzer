"""T-10 backtest maths: G value per game from historical closing odds, and how the
owner's ±6pp rule would have done against results and against the market itself.

T-10 (the owner's method): about ten minutes before first pitch, de-vig the
moneyline (favourite's probability p) and the ±1.5 run line (favourite covers -1.5
with probability c) from the same book, G = p + c - 1;
G >= +6pp -> favourite moneyline, G <= -6pp -> underdog +1.5.

Since c = p - P(favourite wins by exactly one run), G = 2p - 1 - P(one-run win):
large for strong favourites, negative in coin-flip games. The backtest asks whether
those two groups win more often than the closing market's own probabilities say;
if not, betting them only pays the bookmaker's margin.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..models.market import devig, g_value


def american_to_decimal(odds: Any) -> float | None:
    """+150 -> 2.50, -200 -> 1.50; None for blanks and nonsense."""
    try:
        a = float(odds)
    except (TypeError, ValueError):
        return None
    if math.isnan(a) or -100 < a < 100:
        return None
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


@dataclass(frozen=True)
class GameG:
    """One game priced by one book at one moment."""

    favourite: str  # "home" | "away"
    p: float  # de-vigged favourite moneyline probability
    c: float  # de-vigged favourite -1.5 cover probability
    g: float
    ml_fav: float  # decimal prices from the same book
    ml_dog: float
    rl_fav: float
    rl_dog: float


def game_g(
    ml_home: Any,
    ml_away: Any,
    rl_home_spread: Any,
    rl_home: Any,
    rl_away: Any,
    method: str = "power",
) -> GameG | None:
    """G from one book's American odds; None when the run line is not favourite -1.5."""
    prices = [american_to_decimal(x) for x in (ml_home, ml_away, rl_home, rl_away)]
    if any(p is None or p <= 1.0 for p in prices):
        return None
    mh, ma, rh, ra = prices  # type: ignore[misc]
    try:
        spread = float(rl_home_spread)
    except (TypeError, ValueError):
        return None
    ph, pa = devig([mh, ma], method)
    if abs(ph - pa) < 1e-12:
        return None
    favourite = "home" if ph > pa else "away"
    # T-10 needs the moneyline favourite to be the side giving the 1.5 runs.
    if not ((favourite == "home" and spread == -1.5) or (favourite == "away" and spread == 1.5)):
        return None
    ch, ca = devig([rh, ra], method)
    if favourite == "home":
        return GameG("home", ph, ch, g_value(ph, ch), mh, ma, rh, ra)
    return GameG("away", pa, ca, g_value(pa, ca), ma, mh, ra, rh)


@dataclass(frozen=True)
class Outcome:
    fav_win: bool
    fav_cover: bool  # favourite won by 2+ runs
    dog_cover: bool  # underdog lost by one run or won


def outcome(home_score: int, away_score: int, favourite: str) -> Outcome:
    margin = (home_score - away_score) * (1 if favourite == "home" else -1)
    return Outcome(margin > 0, margin >= 2, margin <= 1)


def summarise(bets: Iterable[Mapping[str, float]]) -> dict[str, Any]:
    """Bets carry `hit` (0/1), `prob` (market's fair probability of the hit) and
    `price` (decimal odds taken). Returns hit rate vs the market's expectation, the
    z-score of the gap, and ROI with its standard error."""
    rows = list(bets)
    n = len(rows)
    if n == 0:
        return {
            "n": 0,
            "hit": None,
            "fair": None,
            "gap_pp": None,
            "z": None,
            "roi": None,
            "roi_se": None,
        }
    hit = sum(r["hit"] for r in rows) / n
    fair = sum(r["prob"] for r in rows) / n
    var = sum(r["prob"] * (1 - r["prob"]) for r in rows)
    z = (hit - fair) * n / math.sqrt(var) if var > 0 else None
    returns = [r["hit"] * r["price"] - 1 for r in rows]
    roi = sum(returns) / n
    sd = math.sqrt(sum((x - roi) ** 2 for x in returns) / (n - 1)) if n > 1 else 0.0
    return {
        "n": n,
        "hit": round(hit, 4),
        "fair": round(fair, 4),
        "gap_pp": round(100 * (hit - fair), 2),
        "z": round(z, 2) if z is not None else None,
        "roi": round(roi, 4),
        "roi_se": round(sd / math.sqrt(n), 4),
    }
