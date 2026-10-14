"""Compare 台灣運彩 prices with the fair odds from the international consensus.

For every match card that has a market consensus, find the 運彩 game, put its
prices next to the fair odds, and flag a *value bet* when

    EV = fair_prob x 運彩_price - 1  >=  value_threshold

This is the one approach the backtest supports: the model does not beat the
market, but a soft book's price above the sharp market's fair price is +EV by
construction — as long as the fair price is right at kickoff, which is what
closing-line value (CLV) checks later.

Markets the international books are not asked for (totals at other lines, team
totals, both teams to score, odd/even, three-way handicaps, first half) are priced
from the market-implied goal model (`models.implied`), validated against closing
prices and results, and need a larger EV (`derived_value_threshold`) to count.

A value bet also needs a fair price bought close to the 運彩 capture (`is_fresh`):
a consensus from the night before misses the news 運彩 may have priced in since.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from ..config import LOCAL_TZ, ModelConfig
from ..fetch.taiwan import TwGame, TwMarket
from ..models.implied import IDENTITY, Calibration, ImpliedGoals, fit_implied

log = logging.getLogger(__name__)

SIDE_LABELS = {
    "home": "主勝",
    "draw": "和局",
    "away": "客勝",
    "over": "大",
    "under": "小",
    "spread_home": "主讓",
    "spread_away": "客受讓",
}


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
    if len(exact) == 1 and (
        exact[0].home_key == card["home_key"] or exact[0].away_key == card["away_key"]
    ):
        return exact[0]
    return None


def fair_time(block: Mapping[str, Any] | None) -> datetime | None:
    """When a consensus block's prices were bought (None if the block has no stamp)."""
    if not block or not block.get("odds_at"):
        return None
    try:
        return datetime.fromisoformat(str(block["odds_at"]))
    except ValueError:
        return None


def is_fresh(
    block: Mapping[str, Any] | None, captured_at: datetime | None, cfg: ModelConfig
) -> bool:
    """Can a 運彩 price captured at `captured_at` be judged against this consensus block?

    Only when the block was bought within `max_price_gap_minutes` of the 運彩 capture.
    A block carried over from an earlier run ("stale") keeps its own purchase time, so
    the time decides (an evening run that could not afford MLB odds carries prices from
    an hour ago, which are fine). A carried block without a time is never fresh; a fresh
    one without a time (snapshots made before the stamps existed) is judged as before.
    """
    if not block:
        return False
    bought = fair_time(block)
    if bought is None:
        return not block.get("stale")
    if captured_at is None:
        return True
    return abs((captured_at - bought).total_seconds()) <= cfg.max_price_gap_minutes * 60


def compare(
    card: dict[str, Any],
    game: TwGame,
    captured_at: datetime | None,
    cfg: ModelConfig,
    calib: Calibration = IDENTITY,
) -> dict[str, Any]:
    """Build the card's `tw` block: prices, EV per selection, and the value bets."""
    mv = card.get("market") or {}
    # A neutral-venue listing can name the teams the other way round: 運彩's "home"
    # is then our away team. Only the match-winner market is safe to map back.
    swapped = game.home_key == card.get("away_key") and game.away_key == card.get("home_key")
    fresh = {k: is_fresh(mv.get(k), captured_at, cfg) for k in ("h2h", "totals", "spread")}
    bought = fair_time(mv.get("h2h"))
    block: dict[str, Any] = {
        "game_no": game.no,
        "home_zh": game.home_zh,
        "away_zh": game.away_zh,
        # Taipei time: the page slices the clock out of this string.
        "captured_at": captured_at.astimezone(LOCAL_TZ).isoformat(timespec="minutes")
        if captured_at
        else None,
        # The fair price's own time, and whether this comparison may flag value bets.
        "fair_at": bought.astimezone(LOCAL_TZ).isoformat(timespec="minutes") if bought else None,
        "fair_fresh": fresh["h2h"],
        "h2h": None,
        "totals": None,
        "spread": None,
        "values": [],
    }
    h2h = game.market("h2h")
    if h2h and mv.get("h2h"):
        block["h2h"] = _compare_sides(
            h2h,
            mv["h2h"],
            ("home", "draw", "away"),
            None,
            cfg,
            block["values"],
            "1X2" if "draw" in mv["h2h"] else "不讓分",
            tw_side={"home": "away", "away": "home"} if swapped else None,
            names={"home": game.home_zh, "away": game.away_zh},
            record=fresh["h2h"],
        )
    if swapped:
        log.info(
            "tw: %s vs %s listed the other way round; only 1X2 compared", card["home"], card["away"]
        )
        block["derived"] = []
        return block
    tot = game.market("totals", line=mv["totals"]["line"]) if mv.get("totals") else None
    if tot:
        block["totals"] = _compare_sides(
            tot,
            mv["totals"],
            ("over", "under"),
            tot.line,
            cfg,
            block["values"],
            "大小",
            record=fresh["totals"],
        )
    sp = game.market("spread")  # two-way only; the parser demotes three-way handicaps
    if sp and mv.get("spread"):
        home_sel = next((s for s in sp.selections if s.side == "home"), None)
        if (
            home_sel
            and home_sel.line is not None
            and abs(home_sel.line - mv["spread"]["line"]) < 1e-9
        ):
            block["spread"] = _compare_sides(
                sp,
                mv["spread"],
                ("home", "away"),
                home_sel.line,
                cfg,
                block["values"],
                "讓分",
                key_prefix="spread_",
                record=fresh["spread"],
            )
    block["derived"] = derived_compare(
        mv, game, cfg, calib, block["values"], record=fresh["h2h"] and fresh["totals"]
    )
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
    tw_side: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
    record: bool = True,
) -> dict[str, Any]:
    """`tw_side` maps our side to 運彩's when it lists the teams the other way round;
    `names` (運彩's Chinese team names) makes the value label say which team to pick
    on 運彩, in 運彩's own home/away terms. With `record` False (the fair price is too
    far in time from the 運彩 price) EVs are shown but no value bet is flagged."""
    out: dict[str, Any] = {"line": line}
    for side in sides:
        their = (tw_side or {}).get(side, side)
        price = tw.price(their)
        if price is None or side not in fair:
            continue
        fair_prob = fair[side]["fair"]
        ev = fair_prob * price - 1.0
        entry = {
            "price": price,
            "fair_odds": fair[side]["fair_odds"],
            "fair_prob": fair_prob,
            "ev": round(ev, 4),
        }
        out[side] = entry
        if record and ev >= cfg.value_threshold:
            key = f"{key_prefix}{side}"
            label = SIDE_LABELS.get(f"{key_prefix}{their}", key)
            if names and their in names:
                label += f"（{names[their]}）"
            values.append(
                {
                    "market": market_name,
                    "selection_key": key,
                    "selection": label
                    + (
                        f" {line:+g}"
                        if key.startswith("spread_") and line is not None
                        else (f" {line}" if line is not None and side in ("over", "under") else "")
                    ),
                    "line": line,
                    "tw_price": price,
                    "fair_odds": fair[side]["fair_odds"],
                    "fair_prob": fair_prob,
                    "ev": round(ev, 4),
                }
            )
    return out


# ------------------------------------------------------------ derived markets
def implied_from_market(mv: dict[str, Any]) -> ImpliedGoals | None:
    """The market-implied goal model for a card's consensus.

    Needs the football 1X2 AND a totals line: from the 1X2 alone the goal level is
    a guess (off by 4.7pp on P(over 2.5) on average over 3,689 real markets), which
    would invent value on every totals-type 運彩 market.
    """
    h2h = mv.get("h2h") or {}
    tot = mv.get("totals") or {}
    p_over = (tot.get("over") or {}).get("fair")
    if not all(k in h2h for k in ("home", "draw", "away")) or p_over is None:
        return None
    return fit_implied(
        h2h["home"]["fair"],
        h2h["draw"]["fair"],
        h2h["away"]["fair"],
        p_over,
        float(tot.get("line", 2.5)),
    )


# Lines scripts/validate_implied.py checked against results; outside them the
# model's shape is untested (e.g. 讓分 0:3), so those markets are not priced.
VALIDATED_LINES = {
    "totals": (1.5, 4.5),
    "team_totals": (0.5, 2.5),
    "hcp3": (-2.0, 2.0),
    "ht_totals": (0.5, 2.5),
}


def derived_probs(
    m: TwMarket, game: TwGame, g: ImpliedGoals, calib: Calibration
) -> list[tuple[str, str, float | None, float]] | None:
    """(selection key, label, line, fair probability) for each side of a 運彩 market
    the implied model can price, or None when it cannot."""
    line = m.line
    bounds = VALIDATED_LINES.get(m.kind)
    if bounds and (line is None or not bounds[0] <= line <= bounds[1]):
        return None
    if m.kind in ("totals", "ht_totals") and line is not None:
        pre, tag = ("", "") if m.kind == "totals" else ("ht_", "上半場 ")
        sides = ((f"{pre}over", f"{tag}大 {line:g}"), (f"{pre}under", f"{tag}小 {line:g}"))
    elif m.kind == "team_totals" and line is not None and m.subject in (game.home_zh, game.away_zh):
        side = "home" if m.subject == game.home_zh else "away"
        sides = (
            (f"team_{side}_over", f"{m.subject} 大 {line:g}"),
            (f"team_{side}_under", f"{m.subject} 小 {line:g}"),
        )
    elif m.kind == "btts":
        sides = (("btts_yes", "兩隊都進球：是"), ("btts_no", "兩隊都進球：否"))
    elif m.kind == "odd_even":
        sides = (("odd", "總進球：單"), ("even", "總進球：雙"))
    elif m.kind == "hcp3" and line is not None and abs(line - round(line)) < 1e-9:
        tag = m.name.replace("讓分 ", "讓分")
        sides = (
            ("hcp3_home", f"{tag} 主（{game.home_zh}）"),
            ("hcp3_draw", f"{tag} 和"),
            ("hcp3_away", f"{tag} 客（{game.away_zh}）"),
        )
    elif m.kind == "ht_h2h":
        sides = (("ht_home", "上半場 主勝"), ("ht_draw", "上半場 和局"), ("ht_away", "上半場 客勝"))
    else:
        return None
    out = []
    for key, label in sides:
        prob = derived_prob_for(g, key, line, calib)
        if prob is not None:
            out.append((key, label, line, prob))
    return out


def derived_prob_for(
    g: ImpliedGoals, key: str, line: float | None, calib: Calibration = IDENTITY
) -> float | None:
    """Fair probability of one derived selection (the keys the ledger settles)."""
    if key in ("over", "under") and line is not None:
        p = calib.apply(f"total_over@{line:g}", g.total_over(line))
        return p if key == "over" else 1 - p
    if key in ("ht_over", "ht_under") and line is not None:
        p = calib.apply(f"ht_over@{line:g}", g.first_half().total_over(line))
        return p if key == "ht_over" else 1 - p
    if key.startswith("team_") and line is not None:
        _, side, direction = key.split("_")
        p = calib.apply(f"team_over_{side}@{line:g}", g.team_over(side, line))
        return p if direction == "over" else 1 - p
    if key in ("btts_yes", "btts_no"):
        p = calib.apply("btts", g.both_score())
        return p if key == "btts_yes" else 1 - p
    if key in ("odd", "even"):
        p = calib.apply("odd", g.odd_total())
        return p if key == "odd" else 1 - p
    if key.startswith("hcp3_") and line is not None and abs(line - round(line)) < 1e-9:
        h, d, a = _hcp3(g, int(round(line)), calib)
        return {"hcp3_home": h, "hcp3_draw": d, "hcp3_away": a}.get(key)
    if key in ("ht_home", "ht_draw", "ht_away"):
        h, d, a = calib.apply3("ht", g.first_half().p_1x2())
        return {"ht_home": h, "ht_draw": d, "ht_away": a}[key]
    return None


def _hcp3(g: ImpliedGoals, hcp: int, calib: Calibration) -> tuple[float, float, float]:
    raw = g.handicap3(hcp)
    h, d, a = calib.apply3("hcp3", raw, suffix=f"@{hcp:g}")
    # With a one-goal handicap one side is a plain 1X2 outcome the market prices
    # itself (1:0 "客" = draw or away win; 0:1 "主" = home win or draw): keep it
    # exact and let the calibration only re-split the other two.
    if hcp == -1:
        rest = 1 - raw[2]
        return h * rest / (h + d), d * rest / (h + d), raw[2]
    if hcp == 1:
        rest = 1 - raw[0]
        return raw[0], d * rest / (d + a), a * rest / (d + a)
    return h, d, a


_SIDE_OF_KEY = {
    "over": "over",
    "under": "under",
    "btts_yes": "yes",
    "btts_no": "no",
    "odd": "odd",
    "even": "even",
    "hcp3_home": "home",
    "hcp3_draw": "draw",
    "hcp3_away": "away",
    "ht_home": "home",
    "ht_draw": "draw",
    "ht_away": "away",
    "ht_over": "over",
    "ht_under": "under",
    "team_home_over": "over",
    "team_home_under": "under",
    "team_away_over": "over",
    "team_away_under": "under",
}


def derived_compare(
    mv: dict[str, Any],
    game: TwGame,
    cfg: ModelConfig,
    calib: Calibration,
    values: list[dict[str, Any]],
    record: bool = True,
) -> list[dict[str, Any]]:
    """EV of every 運彩 selection the implied model prices; value bets go to `values`
    (only with `record`: both the 1X2 and the totals line must be fresh).

    The totals line the market itself quotes is compared directly above, so it is
    skipped here.
    """
    g = implied_from_market(mv)
    if g is None:
        return []
    main_line = (mv.get("totals") or {}).get("line")
    out: list[dict[str, Any]] = []
    for m in game.markets:
        if m.kind == "totals" and main_line is not None and m.line == main_line:
            continue
        priced = derived_probs(m, game, g, calib)
        if not priced:
            continue
        for key, label, line, prob in priced:
            price = m.price(_SIDE_OF_KEY[key])
            if price is None or not 0 < prob < 1:
                continue
            ev = prob * price - 1.0
            entry = {
                "market": m.name,
                "selection_key": key,
                "selection": label,
                "line": line,
                "tw_price": price,
                "fair_odds": round(1 / prob, 2),
                "fair_prob": round(prob, 4),
                "ev": round(ev, 4),
                "derived": True,
            }
            out.append(entry)
            threshold = (
                cfg.ht_value_threshold if key.startswith("ht_") else cfg.derived_value_threshold
            )
            if record and ev >= threshold:
                values.append(entry)
    out.sort(key=lambda e: -e["ev"])
    return out


def annotate(
    cards: list[dict[str, Any]],
    games: list[TwGame],
    captured_at: datetime | None,
    cfg: ModelConfig,
    tolerance_minutes: int = 45,
    calib: Calibration = IDENTITY,
) -> dict[str, Any]:
    """Attach `tw` blocks to cards in place. Returns a small report for the snapshot."""
    tol = timedelta(minutes=tolerance_minutes)
    matched = 0
    unmatched_cards: list[str] = []
    used: set[str] = set()
    fresh: list[dict[str, Any]] = []
    for card in cards:
        # A later upload no longer lists a match once it has kicked off. Keep the block
        # from the earlier upload (it carries its own captured_at) instead of erasing it:
        # the post-match review needs the last 運彩 price before kickoff.
        card.setdefault("tw", None)
        if not card.get("market"):
            continue
        game = match_game(card, games, tol)
        if game is None:
            if not _started(card, captured_at):
                unmatched_cards.append(f"{card['home']} vs {card['away']}")
            continue
        used.add(game.id)
        card["tw"] = compare(card, game, captured_at, cfg, calib)
        fresh.append(card["tw"])
        matched += 1
    unmatched_games = [
        f"{g.away_zh} @ {g.home_zh} ({g.kickoff.isoformat(timespec='minutes')})"
        for g in games
        if g.id not in used
    ]
    # How much 運彩 charges on the match-winner market, averaged over this upload's games:
    # sum(1/price) - 1. Pinnacle is ~2-3%; a soft book 8-12%; 運彩 is known to be higher.
    overrounds = []
    for tw in fresh:
        if tw.get("h2h"):
            prices = [
                v["price"] for k, v in tw["h2h"].items() if k != "line" and isinstance(v, dict)
            ]
            if len(prices) >= 2:
                overrounds.append(sum(1 / p for p in prices) - 1)
    return {
        "matched": matched,
        "unmatched_cards": unmatched_cards,
        "unmatched_games": unmatched_games[:40],
        "tw_games": len(games),
        "avg_overround": round(sum(overrounds) / len(overrounds), 4) if overrounds else None,
    }


def _started(card: dict[str, Any], captured_at: datetime | None) -> bool:
    if captured_at is None:
        return False
    try:
        return datetime.fromisoformat(card["kickoff_utc"]) <= captured_at
    except (KeyError, TypeError, ValueError):
        return False
