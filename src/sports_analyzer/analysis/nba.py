"""NBA cards: schedule + market consensus (不讓分 / 讓分 / 大小) + the 運彩 decision.

No model of our own: the 2026-10-25 study found nothing to fade in NBA closing lines
(50 strategies, none consistently off the market by more than noise), and the T-10 and
football backtests say the same about price-only overlays. What pays is a 運彩 price
above the international fair price, judged just before tip-off when news (injuries,
rest) has moved the market and 運彩 may not have followed. So the card's job is:

1. the fair prices (de-vigged consensus) for the three markets 運彩 offers,
2. what the market expects (favourite, handicap, total, the score they imply),
3. once a 運彩 upload is in, the EV of every 運彩 bet - the three markets at the
   international line directly, other handicap / totals lines through the normal
   margin model (models/margin.py) - and the decision: 可下 / 不下 / 不判斷.

The dict layout is the contract with the templates, the ledgers and the review:

card = {sport: "nba", game_id, kickoff_utc, local, status, home, away, home_key,
        away_key, home_abbr, away_abbr, venue, market: {books, h2h, totals, spread},
        shape: {favourite, line, total, home_pts, away_pts, p_home}, notes: [str],
        tw: analysis/taiwan.compare's block (after an upload)}
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from ..config import LOCAL_TZ, MODEL, ModelConfig
from ..fetch.nba import NbaGame
from ..fetch.odds import OddsEvent
from ..models import margin as margin_model
from ..teams import best_match, canonical
from .football import market_view
from .taiwan import is_fresh

# A tip-off within this many hours of the odds event's start is the same game.
MATCH_TOLERANCE_HOURS = 6.0


def match_event(game: NbaGame, events: list[OddsEvent]) -> OddsEvent | None:
    """The odds event of a game: same two teams (canonical keys, then the closest
    spelling), tip-off within the tolerance."""
    window = timedelta(hours=MATCH_TOLERANCE_HOURS)
    near = [e for e in events if abs(e.commence_time - game.tipoff) <= window]
    hk, ak = canonical(game.home), canonical(game.away)
    for e in near:
        if e.home_key == hk and e.away_key == ak:
            return e
    h = best_match(game.home, [e.home_key for e in near])
    a = best_match(game.away, [e.away_key for e in near])
    for e in near:
        if e.home_key == h and e.away_key == a:
            return e
    return None


def shape_block(view: Mapping[str, Any] | None, cfg: ModelConfig) -> dict[str, Any] | None:
    """What the market expects: the favourite and its win chance, the handicap and
    totals lines, and the score they imply (margin and total from the normal model
    anchored on the fair cover / over probabilities)."""
    if not view:
        return None
    h2h, spread, totals = view.get("h2h") or {}, view.get("spread") or {}, view.get("totals") or {}
    if "home" not in h2h or "away" not in h2h:
        return None
    p_home = float(h2h["home"]["fair"])
    out: dict[str, Any] = {
        "favourite": "home" if p_home >= 0.5 else "away",
        "p_home": round(p_home, 4),
        "line": None,
        "total": None,
        "home_pts": None,
        "away_pts": None,
    }
    mu = mu_t = None
    if spread.get("line") is not None and spread.get("home"):
        out["line"] = float(spread["line"])
        mu = margin_model.mean_from_cover(out["line"], spread["home"]["fair"], cfg.nba_margin_sigma)
    if totals.get("line") is not None and totals.get("over"):
        out["total"] = float(totals["line"])
        mu_t = margin_model.mean_from_over(
            out["total"], totals["over"]["fair"], cfg.nba_total_sigma
        )
    if mu is not None and mu_t is not None:
        home_pts, away_pts = margin_model.expected_score(mu, mu_t)
        out["home_pts"] = round(home_pts, 1)
        out["away_pts"] = round(away_pts, 1)
        out["margin"] = round(mu, 1)
    return out


def build_cards(
    games: list[NbaGame], events: list[OddsEvent], cfg: ModelConfig
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for g in sorted(games, key=lambda x: x.tipoff):
        event = match_event(g, events)
        # Totals have no natural line to prefer (every game has its own), so the most
        # quoted one wins; the same goes for the handicap.
        view = market_view(event, cfg.devig_method, totals_prefer=None) if event else None
        cards.append(
            {
                "sport": "nba",
                "game_id": g.game_id,
                "kickoff_utc": g.tipoff.isoformat(),
                "local": g.tipoff.astimezone(LOCAL_TZ).strftime("%m/%d %H:%M"),
                "status": g.status,
                "home": g.home,
                "away": g.away,
                "home_key": canonical(g.home),
                "away_key": canonical(g.away),
                "home_abbr": g.home_abbr,
                "away_abbr": g.away_abbr,
                "venue": g.venue,
                "neutral_site": g.neutral_site,
                "market": view,
                "shape": shape_block(view, cfg),
                "notes": [] if event else ["找不到對應賠率（可能尚未開盤）"],
            }
        )
    return cards


# ------------------------------------------------------------------ the decision
def bets(card: Mapping[str, Any], cfg: ModelConfig = MODEL) -> list[dict[str, Any]]:
    """Every 運彩 bet on the card against its fair price, the three direct markets first
    (fresh or not), then the lines the margin model translated (`derived`)."""
    tw = card.get("tw") or {}
    zh = {
        "home": tw.get("home_zh") or card.get("home", ""),
        "away": tw.get("away_zh") or card.get("away", ""),
    }
    mv = card.get("market") or {}
    captured = _parse_time(tw.get("captured_at"))
    out: list[dict[str, Any]] = []
    for market, sides in (
        ("h2h", ("home", "away")),
        ("spread", ("home", "away")),
        ("totals", ("over", "under")),
    ):
        block = tw.get(market)
        if not isinstance(block, dict):
            continue
        line = block.get("line")
        fresh = is_fresh(mv.get(market), captured, cfg)
        for side in sides:
            b = block.get(side)
            if not isinstance(b, dict) or not b.get("price"):
                continue
            if market == "h2h":
                label, key = f"{zh[side]} 不讓分", side
            elif market == "spread":
                own = float(line) if side == "home" else -float(line)
                label, key = f"{zh[side]} {own:+g}", f"spread_{side}"
            else:
                label, key = f"{'大' if side == 'over' else '小'} {float(line):g}", side
            out.append(
                {
                    "key": key,
                    "market": market,
                    "label": label,
                    "side": side,
                    "line": line,
                    "price": b["price"],
                    "fair_prob": b.get("fair_prob"),
                    "fair_odds": b.get("fair_odds"),
                    "ev": b.get("ev"),
                    "fresh": fresh,
                    "derived": False,
                }
            )
    for d in tw.get("derived") or []:
        market = "spread" if str(d.get("selection_key", "")).startswith("spread_") else "totals"
        out.append(
            {
                "key": d.get("selection_key"),
                "market": market,
                "label": d.get("selection"),
                "side": str(d.get("selection_key", "")).removeprefix("spread_"),
                "line": d.get("line"),
                "price": d.get("tw_price"),
                "fair_prob": d.get("fair_prob"),
                "fair_odds": d.get("fair_odds"),
                "ev": d.get("ev"),
                "fresh": is_fresh(mv.get(market), captured, cfg),
                "derived": True,
            }
        )
    return out


def decision(card: Mapping[str, Any], cfg: ModelConfig) -> dict[str, Any] | None:
    """The 運彩 decision for the card: "value" (a bet reaches its threshold on a fresh
    comparison: 3% direct, 4% for a translated line), "pass" (judged, nothing pays),
    "stale" (the fair price is too far in time from the 運彩 price). None until both a
    consensus and a 運彩 block exist. `pick` is the best value bet, `best` the best EV
    of all, `timing` whether the comparison was made inside the pre-game window."""
    tw = card.get("tw") or {}
    all_bets = bets(card, cfg)
    if not tw or not all_bets:
        return None

    def threshold(b: Mapping[str, Any]) -> float:
        return cfg.derived_value_threshold if b["derived"] else cfg.nba_value_threshold

    priced = [b for b in all_bets if b["ev"] is not None]
    fresh = [b for b in priced if b["fresh"]]
    values = [b for b in fresh if b["ev"] >= threshold(b)]
    best = max(priced, key=lambda b: b["ev"], default=None)
    pick = max(values, key=lambda b: b["ev"], default=None)
    status = "value" if pick else ("pass" if fresh else "stale")
    kickoff = _parse_time(card.get("kickoff_utc"))
    captured = _parse_time(tw.get("captured_at"))
    minutes_before = (
        round((kickoff - captured).total_seconds() / 60) if kickoff and captured else None
    )
    pregame = (
        minutes_before is not None
        and 0 < minutes_before <= cfg.pregame_window_hours * 60
        and bool(fresh)
    )
    return {
        "bets": all_bets,
        "best": best,
        "pick": pick,
        "values": values,
        "status": status,
        "threshold": cfg.nba_value_threshold,
        "derived_threshold": cfg.derived_value_threshold,
        "tw_at": tw.get("captured_at"),
        "fair_at": tw.get("fair_at"),
        "minutes_before": minutes_before,
        "timing": "pregame" if pregame else "early",
    }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
