"""Compare 台灣運彩 prices with the fair odds from the international consensus.

For every match card that has a market consensus, find the 運彩 game, put its
prices next to the fair odds, and flag a *value bet* when

    EV = fair_prob x 運彩_price - 1  >=  value_threshold

This is the one approach the backtest supports: the model does not beat the
market, but a soft book's price above the sharp market's fair price is +EV by
construction — as long as the fair price is right at kickoff, which is what
closing-line value (CLV) checks later.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ..config import ModelConfig
from ..fetch.taiwan import TwGame, TwMarket

log = logging.getLogger(__name__)

SIDE_LABELS = {"home": "主勝", "draw": "和局", "away": "客勝", "over": "大", "under": "小", "spread_home": "主讓", "spread_away": "客受讓"}


def match_game(card: dict[str, Any], games: list[TwGame], tolerance: timedelta) -> TwGame | None:
    """Find the 運彩 game for a card: same canonical teams, kickoff within tolerance.

    Unknown Chinese names (not in the alias table) can still match when the
    kickoff time is unique on both sides, which also tells us which alias to add.
    """
    kickoff = datetime.fromisoformat(card["kickoff_utc"])
    near = [g for g in games if abs(g.kickoff - kickoff) <= tolerance]
    for g in near:
        if g.home_key == card["home_key"] and g.away_key == card["away_key"]:
            return g
    # Neutral-venue or swapped home/away listings: accept the reversed pair but remember it.
    for g in near:
        if g.home_key == card["away_key"] and g.away_key == card["home_key"]:
            log.info("tw: home/away swapped for %s vs %s", card["home"], card["away"])
            return g
    exact = [g for g in near if abs(g.kickoff - kickoff) <= timedelta(minutes=1)]
    if len(exact) == 1 and (exact[0].home_key == card["home_key"] or exact[0].away_key == card["away_key"]):
        return exact[0]
    return None


def compare(card: dict[str, Any], game: TwGame, captured_at: datetime | None, cfg: ModelConfig) -> dict[str, Any]:
    """Build the card's `tw` block: prices, EV per selection, and the value bets."""
    mv = card.get("market") or {}
    block: dict[str, Any] = {
        "game_no": game.no,
        "home_zh": game.home_zh,
        "away_zh": game.away_zh,
        "captured_at": captured_at.isoformat(timespec="minutes") if captured_at else None,
        "h2h": None,
        "totals": None,
        "spread": None,
        "values": [],
    }
    h2h = game.market("h2h")
    if h2h and mv.get("h2h"):
        block["h2h"] = _compare_sides(h2h, mv["h2h"], ("home", "draw", "away"), None, cfg, block["values"], "1X2" if "draw" in mv["h2h"] else "不讓分")
    tot = game.market("totals")
    if tot and mv.get("totals") and tot.line is not None and abs(tot.line - mv["totals"]["line"]) < 1e-9:
        block["totals"] = _compare_sides(tot, mv["totals"], ("over", "under"), tot.line, cfg, block["values"], "大小")
    sp = game.market("spread")
    if sp and mv.get("spread"):
        home_sel = next((s for s in sp.selections if s.side == "home"), None)
        if home_sel and home_sel.line is not None and abs(home_sel.line - mv["spread"]["line"]) < 1e-9:
            block["spread"] = _compare_sides(sp, mv["spread"], ("home", "away"), home_sel.line, cfg, block["values"], "讓分", key_prefix="spread_")
    return block


def _compare_sides(
    tw: TwMarket,
    fair: dict[str, Any],
    sides: tuple[str, ...],
    line: float | None,
    cfg: ModelConfig,
    values: list[dict[str, Any]],
    market_name: str,
    key_prefix: str = "",
) -> dict[str, Any]:
    out: dict[str, Any] = {"line": line}
    for side in sides:
        price = tw.price(side)
        if price is None or side not in fair:
            continue
        fair_prob = fair[side]["fair"]
        ev = fair_prob * price - 1.0
        entry = {"price": price, "fair_odds": fair[side]["fair_odds"], "fair_prob": fair_prob, "ev": round(ev, 4)}
        out[side] = entry
        if ev >= cfg.value_threshold:
            key = f"{key_prefix}{side}"
            values.append(
                {
                    "market": market_name,
                    "selection_key": key,
                    "selection": SIDE_LABELS.get(key, key) + (f" {line:+g}" if key.startswith("spread_") and line is not None else (f" {line}" if line is not None and side in ("over", "under") else "")),
                    "line": line,
                    "tw_price": price,
                    "fair_odds": fair[side]["fair_odds"],
                    "fair_prob": fair_prob,
                    "ev": round(ev, 4),
                }
            )
    return out


def annotate(cards: list[dict[str, Any]], games: list[TwGame], captured_at: datetime | None, cfg: ModelConfig, tolerance_minutes: int = 45) -> dict[str, Any]:
    """Attach `tw` blocks to cards in place. Returns a small report for the snapshot."""
    tol = timedelta(minutes=tolerance_minutes)
    matched = 0
    unmatched_cards: list[str] = []
    used: set[str] = set()
    for card in cards:
        if not card.get("market"):
            card["tw"] = None
            continue
        game = match_game(card, games, tol)
        if game is None:
            card["tw"] = None
            unmatched_cards.append(f"{card['home']} vs {card['away']}")
            continue
        used.add(game.id)
        card["tw"] = compare(card, game, captured_at, cfg)
        matched += 1
    unmatched_games = [f"{g.away_zh} @ {g.home_zh} ({g.kickoff.isoformat(timespec='minutes')})" for g in games if g.id not in used]
    return {"matched": matched, "unmatched_cards": unmatched_cards, "unmatched_games": unmatched_games[:40], "tw_games": len(games)}
