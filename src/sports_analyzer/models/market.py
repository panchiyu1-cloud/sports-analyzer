"""Market maths: de-vigging, edge, consensus across books, and the T-10 G value.

Decimal odds `o` imply a probability `1/o`. Bookmakers price every outcome a
little high so the implied probabilities sum to more than 1 (the "vig" or
"overround"). De-vigging removes that so model and market can be compared.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


# ------------------------------------------------------------------ de-vigging
def implied(prices: Sequence[float]) -> list[float]:
    """Raw implied probabilities 1/odds (these sum to > 1)."""
    return [1.0 / p for p in prices]


def overround(prices: Sequence[float]) -> float:
    """How much the book is charging: sum(1/odds) - 1. 0.05 means a 5% margin."""
    return sum(implied(prices)) - 1.0


def devig(prices: Sequence[float], method: str = "power") -> list[float]:
    """Fair probabilities from a complete set of mutually exclusive prices.

    - "multiplicative": scale all implied probabilities by the same factor.
    - "power": raise each implied probability to the power k with sum == 1.
      This pushes more of the margin onto long shots, which matches how books
      actually price (the favourite-longshot bias), so it is the default.
    """
    if any(p <= 1.0 for p in prices):
        raise ValueError("decimal odds must be > 1.0")
    raw = implied(prices)
    if method == "multiplicative":
        total = sum(raw)
        return [r / total for r in raw]
    if method == "power":
        return _power_devig(raw)
    raise ValueError(f"unknown de-vig method: {method}")


def _power_devig(raw: list[float], tol: float = 1e-10) -> list[float]:
    """Find k such that sum(r ** k) == 1 by bisection; k is in (0, large)."""
    lo, hi = 0.5, 5.0
    # sum(r**k) is decreasing in k. Expand hi until the sum drops below 1.
    while sum(r**hi for r in raw) > 1.0:
        hi *= 2
        if hi > 1e6:  # pragma: no cover - would need absurd prices
            break
    for _ in range(200):
        mid = (lo + hi) / 2
        if sum(r**mid for r in raw) > 1.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    k = (lo + hi) / 2
    probs = [r**k for r in raw]
    total = sum(probs)
    return [p / total for p in probs]  # tiny renormalisation for floating error


# ------------------------------------------------------------------- value maths
def edge(model_p: float, market_p: float) -> float:
    """Model probability minus fair market probability, in probability units."""
    return model_p - market_p


def expected_value(model_p: float, price: float) -> float:
    """Expected profit per unit staked at decimal `price` if the model is right."""
    return model_p * price - 1.0


def kelly_fraction(model_p: float, price: float, fraction: float = 0.25) -> float:
    """Fractional Kelly stake as a share of bankroll; 0 when there is no edge."""
    b = price - 1.0
    if b <= 0:
        return 0.0
    full = (model_p * b - (1.0 - model_p)) / b
    return max(0.0, full * fraction)


def confidence(edge_value: float, low: float, mid: float, high: float) -> str | None:
    """Map an edge to the three confidence labels used on the cards."""
    if edge_value >= high:
        return "高"
    if edge_value >= mid:
        return "中"
    if edge_value >= low:
        return "低"
    return None


# --------------------------------------------------------------------- consensus
@dataclass(frozen=True)
class Consensus:
    """Market view of one outcome set, aggregated across books."""

    outcomes: tuple[str, ...]
    fair_probs: tuple[float, ...]  # median de-vigged probability per outcome
    best_prices: tuple[float, ...]  # best decimal price per outcome
    best_books: tuple[str, ...]
    books_used: int

    def prob(self, outcome: str) -> float:
        return self.fair_probs[self.outcomes.index(outcome)]

    def price(self, outcome: str) -> float:
        return self.best_prices[self.outcomes.index(outcome)]

    def book(self, outcome: str) -> str:
        return self.best_books[self.outcomes.index(outcome)]


def consensus(
    outcomes: Sequence[str],
    quotes: Iterable[tuple[str, Sequence[float]]],
    method: str = "power",
) -> Consensus | None:
    """Aggregate `(book, prices)` quotes for the same outcome set.

    Fair probability = median across books of each book's de-vigged probability.
    Best price = max across books. Returns None when no book quotes all outcomes.
    """
    per_outcome_probs: list[list[float]] = [[] for _ in outcomes]
    best_price = [0.0] * len(outcomes)
    best_book = [""] * len(outcomes)
    used = 0
    for book, prices in quotes:
        if len(prices) != len(outcomes) or any(p <= 1.0 for p in prices):
            continue
        fair = devig(prices, method)
        used += 1
        for i, (p, price) in enumerate(zip(fair, prices, strict=True)):
            per_outcome_probs[i].append(p)
            if price > best_price[i]:
                best_price[i] = price
                best_book[i] = book
    if used == 0:
        return None
    medians = [statistics.median(ps) for ps in per_outcome_probs]
    total = sum(medians)
    return Consensus(
        outcomes=tuple(outcomes),
        fair_probs=tuple(m / total for m in medians),
        best_prices=tuple(best_price),
        best_books=tuple(best_book),
        books_used=used,
    )


# ------------------------------------------------------------------- T-10 G value
def g_value(fav_ml_prob: float, fav_cover_prob: float) -> float:
    """Owner's T-10 structural signal: G = p + c - 1.

    p: de-vigged probability the favourite wins outright (moneyline).
    c: de-vigged probability the favourite covers -1.5 (run line).
    Both come from the same book at the same moment; never average two sources.
    """
    return fav_ml_prob + fav_cover_prob - 1.0


def t10_signal(g: float, threshold: float = 0.06) -> str | None:
    """The original T-10 rule: +threshold or more -> favourite ML; -threshold or less ->
    underdog +1.5; else nothing. The 2021-2025 backtest found no edge in it, so it now
    only names the game's shape (analysis/mlb.G_SHAPES); bets come from EV."""
    if g >= threshold:
        return "FAV_ML"
    if g <= -threshold:
        return "DOG_PLUS_1_5"
    return None
