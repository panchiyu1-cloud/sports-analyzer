"""MLB cards: schedule + probable pitchers + market consensus + T-10 (價值版).

No model yet (Phase 3). The card's job is the fair price and, once a 運彩 upload is
in, the T-10 value block: what each of 運彩's moneyline and run-line bets is worth
against that fair price.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from ..config import LOCAL_TZ, ModelConfig
from ..fetch.mlb import MlbGame
from ..fetch.odds import OddsEvent
from ..models import market
from ..teams import best_match
from .football import market_view
from .taiwan import fair_time, is_fresh

# What G says about the game (it is the market's own view, not an edge: see
# CLAUDE.md "T-10 backtest"). +/-6pp are the owner's original T-10 boundaries.
G_SHAPES = {"FAV_ML": "強隊大勝型", "DOG_PLUS_1_5": "一分差型", None: "中性"}
# A 運彩-vs-international gap below this is not worth a hint.
HINT_MIN = 0.01


def match_event(
    game: MlbGame, events: list[OddsEvent], tolerance_hours: float = 6
) -> OddsEvent | None:
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
    """G = p + c - 1 from the consensus moneyline and the -1.5 run line.

    Since the 2026-10-14 backtest this describes the game ("shape") and no longer
    triggers a bet: the T-10 decision is `t10_value_block`.
    """
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
        "shape": G_SHAPES[market.t10_signal(g, cfg.g_threshold)],
        "threshold": cfg.g_threshold,
    }


def t10_value_block(card: Mapping[str, Any], cfg: ModelConfig) -> dict[str, Any] | None:
    """T-10 價值版: each of 運彩's four bets (favourite / underdog x moneyline / run line)
    against the international fair price, and G from both sets of prices.

    The decision is EV = fair probability x 運彩 price - 1 >= `mlb_value_threshold`,
    judged only when the fair price was bought close to the 運彩 capture (`is_fresh`).
    G (international) describes the game; G from 運彩's own prices minus it points at
    the side 運彩 prices loosely. None until both a consensus and a 運彩 block exist.
    """
    mv = card.get("market") or {}
    tw = card.get("tw") or {}
    h2h, spread = mv.get("h2h"), mv.get("spread")
    tw_h2h, tw_spread = tw.get("h2h") or {}, tw.get("spread") or {}
    if not h2h or not tw_h2h or "home" not in h2h or "away" not in h2h:
        return None
    captured = _parse_time(tw.get("captured_at"))
    fav = "home" if h2h["home"]["fair"] >= h2h["away"]["fair"] else "away"
    dog = "away" if fav == "home" else "home"
    zh = {
        "home": tw.get("home_zh") or card.get("home"),
        "away": tw.get("away_zh") or card.get("away"),
    }
    role = {fav: "fav", dog: "dog"}

    bets: list[dict[str, Any]] = []
    ml_fresh = is_fresh(h2h, captured, cfg)
    for side in (fav, dog):
        if tw_h2h.get(side) and side in h2h:
            bets.append(
                _bet(
                    f"{role[side]}_ml",
                    f"{zh[side]} 不讓分",
                    tw_h2h[side]["price"],
                    h2h[side]["fair"],
                    ml_fresh,
                    side,
                    None,
                )
            )
    rl_ok = bool(spread and tw_spread and abs(abs(spread["line"]) - 1.5) < 1e-9)
    if rl_ok:
        rl_fresh = is_fresh(spread, captured, cfg)
        for side in (fav, dog):
            own = spread["line"] if side == "home" else -spread["line"]
            if tw_spread.get(side) and side in spread:
                bets.append(
                    _bet(
                        f"{role[side]}_rl",
                        f"{zh[side]} {own:+g}",
                        tw_spread[side]["price"],
                        spread[side]["fair"],
                        rl_fresh,
                        side,
                        own,
                    )
                )
    if not bets:
        return None

    # G from the international fair prices and from 運彩's own (de-vigged the same way).
    g_intl = g_tw = dp = dc = None
    fav_gives = rl_ok and (spread["line"] < 0) == (fav == "home")
    if fav_gives:
        p, c = h2h[fav]["fair"], spread[fav]["fair"]
        g_intl = market.g_value(p, c)
        if all(tw_h2h.get(s) for s in (fav, dog)) and all(tw_spread.get(s) for s in (fav, dog)):
            p_tw = market.devig([tw_h2h[fav]["price"], tw_h2h[dog]["price"]], cfg.devig_method)[0]
            c_tw = market.devig(
                [tw_spread[fav]["price"], tw_spread[dog]["price"]], cfg.devig_method
            )[0]
            g_tw = market.g_value(p_tw, c_tw)
            dp, dc = p_tw - p, c_tw - c
    hints = _gap_hints(zh[fav], zh[dog], dp, dc)

    fresh_bets = [b for b in bets if b["fresh"]]
    values = [b for b in fresh_bets if b["ev"] >= cfg.mlb_value_threshold]
    best = max(bets, key=lambda b: b["ev"])
    pick = max(values, key=lambda b: b["ev"]) if values else None
    status = "value" if pick else ("pass" if fresh_bets else "stale")

    kickoff = _parse_time(card.get("kickoff_utc"))
    bought = fair_time(h2h)
    minutes_before = (
        round((kickoff - captured).total_seconds() / 60) if kickoff and captured else None
    )
    pregame = (
        minutes_before is not None
        and 0 < minutes_before <= cfg.pregame_window_hours * 60
        and bool(fresh_bets)
    )
    return {
        "favourite": fav,
        "fav_zh": zh[fav],
        "dog_zh": zh[dog],
        "g_intl": _r(g_intl),
        "g_tw": _r(g_tw),
        "g_gap": _r(g_tw - g_intl) if g_tw is not None and g_intl is not None else None,
        "dp": _r(dp),
        "dc": _r(dc),
        "shape": G_SHAPES[market.t10_signal(g_intl, cfg.g_threshold)]
        if g_intl is not None
        else None,
        "hints": hints,
        "bets": bets,
        "best": best,
        "pick": pick,
        "status": status,
        "threshold": cfg.mlb_value_threshold,
        "tw_at": tw.get("captured_at"),
        "fair_at": bought.astimezone(LOCAL_TZ).isoformat(timespec="minutes") if bought else None,
        "minutes_before": minutes_before,
        "timing": "pregame" if pregame else "early",
    }


def _bet(
    key: str,
    label: str,
    price: float,
    fair: float,
    fresh: bool,
    side: str,
    line: float | None,
) -> dict[str, Any]:
    """One 運彩 bet: `side` is the team backed, `line` its own handicap (None = moneyline)."""
    return {
        "key": key,
        "label": label,
        "side": side,
        "line": line,
        "price": price,
        "fair_prob": round(fair, 4),
        "fair_odds": round(1 / fair, 2) if fair > 0 else None,
        "ev": round(fair * price - 1, 4),
        "fresh": fresh,
    }


def _gap_hints(fav: str, dog: str, dp: float | None, dc: float | None) -> list[str]:
    """Where 運彩's own prices disagree with the international market. Both sides of a
    運彩 market carry the same margin, so the side 運彩 underrates is the cheaper one."""
    out = []
    if dc is not None and abs(dc) >= HINT_MIN:
        if dc > 0:
            out.append(f"運彩把{fav}贏 2 分以上算得比國際高 {dc * 100:.1f}pp → {dog} +1.5 相對划算")
        else:
            out.append(
                f"運彩把{fav}贏 2 分以上算得比國際低 {-dc * 100:.1f}pp → {fav} −1.5 相對划算"
            )
    if dp is not None and abs(dp) >= HINT_MIN:
        if dp > 0:
            out.append(f"運彩把{fav}贏球算得比國際高 {dp * 100:.1f}pp → {dog} 不讓分相對划算")
        else:
            out.append(f"運彩把{fav}贏球算得比國際低 {-dp * 100:.1f}pp → {fav} 不讓分相對划算")
    return out


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _r(x: float | None) -> float | None:
    return round(x, 4) if x is not None else None


def build_cards(
    games: list[MlbGame], events: list[OddsEvent], cfg: ModelConfig
) -> list[dict[str, Any]]:
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
