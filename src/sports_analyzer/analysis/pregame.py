"""Pre-game refresh: fresh international prices for the games about to start.

T-10's one sound idea is timing: the market is most accurate just before first
pitch, and 運彩 is most likely to lag behind it then (a scratched starter, a late
lineup). So when the phone uploads 運彩's prices, the games starting within
`pregame_window_hours` get freshly bought international prices before the
comparison is made, if their last ones are older than `pregame_min_age_minutes`.

Pure functions; the purchase itself (budget, API) lives in pipeline.py.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from ..config import ModelConfig
from ..fetch.odds import OddsEvent
from ..teams import best_match
from .football import market_view
from .taiwan import fair_time

log = logging.getLogger(__name__)

# Only an upload this recent triggers a purchase: a fresh fair price next to an old
# 運彩 price cannot be judged anyway (analysis/taiwan.is_fresh).
UPLOAD_MAX_AGE = timedelta(minutes=90)


def kickoff(card: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(card["kickoff_utc"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def upload_is_recent(captured_at: datetime | None, now: datetime) -> bool:
    return captured_at is not None and timedelta(0) <= now - captured_at <= UPLOAD_MAX_AGE


def due_cards(
    cards: Iterable[dict[str, Any]], now: datetime, cfg: ModelConfig
) -> list[dict[str, Any]]:
    """Cards starting within the window whose match-winner price is missing, has no
    purchase time, or is older than the minimum age."""
    window_end = now + timedelta(hours=cfg.pregame_window_hours)
    min_age = timedelta(minutes=cfg.pregame_min_age_minutes)
    out = []
    for card in cards:
        start = kickoff(card)
        if start is None or not now < start <= window_end:
            continue
        h2h = (card.get("market") or {}).get("h2h")
        bought = fair_time(h2h)
        # A carried-over block keeps its purchase time, so its age is known either way.
        if h2h and bought is not None and now - bought < min_age:
            continue
        out.append(card)
    return out


def match_card_event(
    card: dict[str, Any], events: list[OddsEvent], tolerance_hours: float = 6.0
) -> OddsEvent | None:
    """The odds event for a card: same teams (canonical keys, then the closest fuzzy
    match), and of a doubleheader the game nearest in time."""
    start = kickoff(card)
    if start is None:
        return None
    near = [e for e in events if abs(e.commence_time - start) <= timedelta(hours=tolerance_hours)]
    exact = [
        e for e in near if e.home_key == card.get("home_key") and e.away_key == card.get("away_key")
    ]
    if not exact:
        h = best_match(str(card.get("home", "")), [e.home_key for e in near])
        a = best_match(str(card.get("away", "")), [e.away_key for e in near])
        exact = [e for e in near if e.home_key == h and e.away_key == a]
    return min(exact, key=lambda e: abs(e.commence_time - start)) if exact else None


def refresh_cards(
    cards: list[dict[str, Any]],
    events: list[OddsEvent],
    cfg: ModelConfig,
    totals_prefer: float | None,
) -> list[dict[str, Any]]:
    """Replace the consensus of every card with a matching event, in place. A block the
    purchase did not include (h2h only, when the budget was short) keeps its old
    prices, marked stale. Returns the refreshed cards."""
    done = []
    for card in cards:
        event = match_card_event(card, events)
        if event is None:
            continue
        view = market_view(event, cfg.devig_method, totals_prefer=totals_prefer)
        if not view.get("h2h"):
            continue
        old = card.get("market") or {}
        for key in ("h2h", "totals", "spread"):
            if view.get(key) is None and old.get(key):
                view[key] = {**old[key], "stale": True}
        card["market"] = view
        done.append(card)
    log.info("pre-game refresh: %d of %d due cards repriced", len(done), len(cards))
    return done
