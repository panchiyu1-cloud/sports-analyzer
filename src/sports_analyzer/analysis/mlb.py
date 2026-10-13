"""MLB cards: schedule + probable pitchers + market consensus + the owner's T-10 G value.

No model yet (Phase 3). The card's job today is the fair price and the G value,
so the owner can compare against the price he can actually get.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..config import LOCAL_TZ, ModelConfig
from ..fetch.mlb import MlbGame
from ..fetch.odds import OddsEvent
from ..models import market
from ..teams import best_match
from .football import market_view


def match_event(game: MlbGame, events: list[OddsEvent], tolerance_hours: float = 6) -> OddsEvent | None:
    window = timedelta(hours=tolerance_hours)
    candidates = [e for e in events if abs(e.commence_time - game.game_date) <= window]
    from ..teams import canonical

    hk, ak = canonical(game.home), canonical(game.away)
    for e in candidates:
        if e.home_key == hk and e.away_key == ak:
            return e
    h = best_match(game.home, [e.home_key for e in candidates])
    a = best_match(game.away, [e.away_key for e in candidates])
    for e in candidates:
        if e.home_key == h and e.away_key == a:
            return e
    return None


def g_value_block(view: dict[str, Any], cfg: ModelConfig) -> dict[str, Any] | None:
    """T-10: G = p + c - 1 from the consensus moneyline and the -1.5 run line."""
    h2h, spread = view.get("h2h"), view.get("spread")
    if not h2h or not spread or abs(abs(spread["line"]) - 1.5) > 1e-9:
        return None
    fav = "home" if h2h["home"]["fair"] >= h2h["away"]["fair"] else "away"
    # spread["line"] is from the home view: -1.5 means home gives 1.5 runs.
    fav_gives = (spread["line"] < 0 and fav == "home") or (spread["line"] > 0 and fav == "away")
    if not fav_gives:
        return None
    p = h2h[fav]["fair"]
    c = spread[fav]["fair"]
    g = market.g_value(p, c)
    return {
        "favourite": fav,
        "p_ml": round(p, 4),
        "c_cover": round(c, 4),
        "g": round(g, 4),
        "signal": market.t10_signal(g, cfg.g_threshold),
        "threshold": cfg.g_threshold,
    }


def build_cards(games: list[MlbGame], events: list[OddsEvent], cfg: ModelConfig) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for g in sorted(games, key=lambda x: x.game_date):
        from ..teams import canonical

        event = match_event(g, events)
        view = market_view(event, cfg.devig_method, totals_prefer=None) if event else None
        cards.append(
            {
                "sport": "mlb",
                "game_pk": g.game_pk,
                "kickoff_utc": g.game_date.isoformat(),
                "local": g.game_date.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M"),
                "status": g.status,
                "home": g.home,
                "away": g.away,
                "home_key": canonical(g.home),
                "away_key": canonical(g.away),
                "home_probable": g.home_probable,
                "away_probable": g.away_probable,
                "venue": g.venue,
                "market": view,
                "t10": g_value_block(view, cfg) if view else None,
                "notes": [] if event else ["找不到對應賠率（可能尚未開盤）"],
            }
        )
    return cards
