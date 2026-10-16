"""反者道之動: betting against the analysis strategies that do worst against the market.

A strategy is "bad" only when its picks win LESS often than the market's own fair
probability says (a high failure rate alone means nothing: long odds already pay for
it). Even then the opposite bet pays the bookmaker's margin, so it only makes money
where the strategy is wrong by more than that margin: 運彩 takes about 16% (MLB) and
21% (football 1X2) against 2-6% at international books.

This module holds the pure parts:
- `score`: hit rate against the market's probability, z, and ROI at several price levels
  (used by scripts/backtest_fade.py on 2019-2027 data);
- `live_candidates`: today's games where a consistently bad strategy points one way,
  with the opposite bet at 運彩's price and its EV;
- `fade_record`: the same opposite bets on finished games, at the 運彩 price captured
  before kickoff, so the idea is tracked forward with real prices.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..config import ModelConfig
from ..ledger import HALF_LOSS, HALF_WIN, LOSS, PUSH, WIN

# 運彩's margins (overround), measured on its 2026-10 uploads: MLB moneyline / run
# line / totals ~16%, football 1X2 ~20% (on the matches the international market
# also prices), football totals ~17%. How it spreads them: `tw_prices`.
TW_MARGIN = {"mlb": 0.16, "1x2": 0.20, "totals": 0.171}

# The live strategies: the ones the 2019-2027 backtest found consistently below the
# market (both halves of the data) that today's cards can evaluate.
G_FADE = 0.06  # T-10: G >= +6pp backed the favourite's moneyline
HOME_FAV_MIN = 0.55  # MLB home favourites at 55% or more
LONGSHOT_PROB = 1 / 8  # football outcomes at fair odds of 8 or more

STRATEGIES: dict[str, dict[str, str]] = {
    "t10_g6": {
        "sport": "MLB",
        "name": "T-10：G≥+6 押強隊不讓分",
        "fade": "押弱隊不讓分",
    },
    "home_fav55": {
        "sport": "MLB",
        "name": "押主場熱門（勝率≥55%）",
        "fade": "押客隊不讓分",
    },
    "longshot8": {
        "sport": "足球",
        "name": "押大冷門（賠率≥8）",
        "fade": "押另外兩個結果",
    },
    "home_dog": {
        "sport": "足球",
        "name": "押主場冷門",
        "fade": "押和局＋客勝",
    },
    "model_signal": {
        "sport": "足球",
        "name": "系統的模型分歧",
        "fade": "押反方向",
    },
}

OUTCOMES = ("home", "draw", "away")
OUTCOME_ZH = {"home": "主勝", "draw": "和局", "away": "客勝"}
OPPOSITE_RESULT = {WIN: LOSS, LOSS: WIN, HALF_WIN: HALF_LOSS, HALF_LOSS: HALF_WIN, PUSH: PUSH}


# --------------------------------------------------------------------- scoring
def score(bets: Iterable[Mapping[str, Any]], levels: Iterable[str] = ()) -> dict[str, Any]:
    """Bets carry `hit` (0/1), `prob` (the market's fair probability of the hit) and one
    decimal price per name in `levels`. Returns the hit rate against the market's
    expectation, its z-score, and the ROI (with standard error) at fair prices
    (1/prob, no margin) and at every level."""
    rows = list(bets)
    n = len(rows)
    if n == 0:
        return {"n": 0}
    hit = sum(r["hit"] for r in rows) / n
    fair = sum(r["prob"] for r in rows) / n
    var = sum(r["prob"] * (1 - r["prob"]) for r in rows)
    out: dict[str, Any] = {
        "n": n,
        "hit": round(hit * 100, 1),
        "fair": round(fair * 100, 1),
        "gap_pp": round((hit - fair) * 100, 2),
        "z": round((hit - fair) * n / math.sqrt(var), 2) if var > 0 else None,
    }
    for level in ("fair", *levels):
        rets = [r["hit"] * (1 / r["prob"] if level == "fair" else r[level]) - 1 for r in rows]
        roi = sum(rets) / n
        sd = math.sqrt(sum((x - roi) ** 2 for x in rets) / (n - 1)) if n > 1 else 0.0
        out["roi_" + level] = round(roi * 100, 1)
        out["se_" + level] = round(sd / math.sqrt(n) * 100, 1)
        if level != "fair":
            # How many points above the market's probability the hit rate must be to
            # break even at these prices (the margin the bets pay, on average).
            out["need_" + level] = round((sum(1 / r[level] for r in rows) / n - fair) * 100, 1)
    return out


def dutch(prices: Iterable[float]) -> float:
    """One price for backing several outcomes at once, stakes split so that any of them
    pays the same: 1 / sum(1/price). Backing the two other 1X2 outcomes this way is the
    exact opposite of backing one."""
    return 1 / sum(1 / p for p in prices)


def tw_prices(probs: Sequence[float], margin: float, even: bool = False) -> list[float]:
    """運彩's prices for a market with these fair probabilities. In football its margin is
    not spread evenly: it weighs on long odds (about 13% on 1X2 favourites, 40% on the
    longest outcome). A power model reproduces that - implied probability p^e, with e
    such that the implied probabilities add up to 1 + margin: on 191 1X2 markets of the
    2026-10 uploads it prices within 4%, where an even spread is off by 21%. MLB's
    two-way prices fit an even spread as well (10 games: 13% on favourites, 18% on
    underdogs), so `even` spreads the margin in proportion instead."""
    if even:
        return [1 / (p * (1 + margin)) for p in probs]
    target = 1 + margin
    lo, hi = 0.2, 1.0
    for _ in range(60):
        e = (lo + hi) / 2
        if sum(p**e for p in probs) > target:
            lo = e
        else:
            hi = e
    e = (lo + hi) / 2
    return [1 / p**e for p in probs]


# ---------------------------------------------------------------- live picks
def live_candidates(
    football_cards: Iterable[Mapping[str, Any]],
    football_signals: Iterable[Mapping[str, Any]],
    mlb_cards: Iterable[Mapping[str, Any]],
    cfg: ModelConfig,
) -> list[dict[str, Any]]:
    """Upcoming games where a live strategy points one way, with the opposite bet at
    運彩's price. One row per opposite bet (strategies pointing at the same bet are
    listed together), best 運彩 EV first."""
    rows: dict[tuple, dict[str, Any]] = {}

    def add(key: tuple, strategy: str, base: dict[str, Any]) -> None:
        row = rows.setdefault(key, {**base, "strategies": []})
        if strategy not in row["strategies"]:
            row["strategies"].append(strategy)

    for card in mlb_cards:
        h2h = (card.get("market") or {}).get("h2h") or {}
        if "home" not in h2h or "away" not in h2h:
            continue
        fav = "home" if h2h["home"]["fair"] >= h2h["away"]["fair"] else "away"
        dog = "away" if fav == "home" else "home"
        t10v = card.get("t10v") or {}
        g = (card.get("t10") or {}).get("g")
        if g is None:
            g = t10v.get("g_intl")
        bet = next((b for b in t10v.get("bets", []) if b.get("key") == "dog_ml"), None)
        names = _names(card)
        base = {
            "sport": "MLB",
            "sport_key": "mlb",
            "competition": "MLB",
            "match_id": str(card.get("game_pk")),
            "kickoff_utc": card.get("kickoff_utc", ""),
            "match": f"{names['away']} @ {names['home']}",
            "fade_label": f"{names[dog]} 不讓分",
            # The bet's market and sides, for its price history on the page.
            "market": "h2h",
            "line": "",
            "bet_sides": [dog],
            "fair_prob": h2h[dog]["fair"],
            "tw_price": bet["price"] if bet else None,
            "ev": bet["ev"] if bet else None,
            "fresh": bool(bet and bet.get("fresh")),
            "threshold": cfg.mlb_value_threshold,
        }
        key = ("mlb", str(card.get("game_pk")), dog)
        if g is not None and g >= G_FADE:
            add(key, "t10_g6", base)
        if fav == "home" and h2h["home"]["fair"] >= HOME_FAV_MIN:
            add(key, "home_fav55", base)

    for card in football_cards:
        h2h = (card.get("market") or {}).get("h2h") or {}
        if not all(s in h2h for s in OUTCOMES):
            continue
        p = {s: h2h[s]["fair"] for s in OUTCOMES}
        names = _names(card)
        longest = min(OUTCOMES, key=lambda o: p[o])
        picks = [("longshot8", longest)] if p[longest] <= LONGSHOT_PROB else []
        if p["home"] < p["away"]:
            picks.append(("home_dog", "home"))
        for strategy, side in picks:
            add(
                ("football", str(card.get("match_id")), side),
                strategy,
                _against_1x2(card, side, p, names, cfg),
            )

    cards_by_id = {str(c.get("match_id")): c for c in football_cards}
    for s in football_signals:
        card = cards_by_id.get(str(s.get("match_id")))
        if card is None:
            continue
        h2h = (card.get("market") or {}).get("h2h") or {}
        key_ = s.get("selection_key")
        if key_ in OUTCOMES and all(o in h2h for o in OUTCOMES):
            p = {o: h2h[o]["fair"] for o in OUTCOMES}
            add(
                ("football", str(card.get("match_id")), key_),
                "model_signal",
                _against_1x2(card, key_, p, _names(card), cfg),
            )
        elif key_ in ("over", "under"):
            row = _against_total(card, key_, s.get("line"), cfg)
            if row:
                add(
                    ("football", str(card.get("match_id")), key_, s.get("line")),
                    "model_signal",
                    row,
                )

    out = list(rows.values())
    for r in out:
        r["status"] = (
            "none"
            if r["ev"] is None
            else "stale"
            if not r["fresh"]
            else "value"
            if r["ev"] >= r["threshold"]
            else "pass"
        )
    out.sort(key=lambda r: (r["ev"] is None, -(r["ev"] or 0), r["kickoff_utc"]))
    return out


def _competition(row: Mapping[str, Any]) -> str:
    """The league's name ('英超') when the card or review row has it, else its code."""
    return str(row.get("competition_name") or row.get("competition") or "")


def _names(card: Mapping[str, Any]) -> dict[str, str]:
    tw = card.get("tw") or {}
    return {
        "home": str(tw.get("home_zh") or card.get("home", "")),
        "away": str(tw.get("away_zh") or card.get("away", "")),
    }


def _against_1x2(
    card: Mapping[str, Any],
    side: str,
    p: Mapping[str, float],
    names: Mapping[str, str],
    cfg: ModelConfig,
) -> dict[str, Any]:
    """Back the two 1X2 outcomes other than `side` at 運彩 (dutched)."""
    tw = card.get("tw") or {}
    tw_h2h = tw.get("h2h") or {}
    others = [o for o in OUTCOMES if o != side]
    prices = [(tw_h2h.get(o) or {}).get("price") for o in others]
    price = dutch(prices) if all(prices) else None
    q = 1 - p[side]
    label = "＋".join(OUTCOME_ZH[o] + (f" {names[o]}" if o != "draw" else "") for o in others)
    return {
        "sport": "足球",
        "sport_key": "football",
        "competition": _competition(card),
        "match_id": str(card.get("match_id")),
        "kickoff_utc": card.get("kickoff_utc", ""),
        "match": f"{names['home']} vs {names['away']}",
        "fade_label": label,
        "market": "h2h",
        "line": "",
        "bet_sides": others,
        "fair_prob": q,
        "tw_price": round(price, 3) if price else None,
        "ev": round(q * price - 1, 4) if price else None,
        "fresh": bool(price and tw.get("fair_fresh", True)),
        "threshold": cfg.value_threshold,
    }


def _against_total(
    card: Mapping[str, Any], side: str, line: Any, cfg: ModelConfig
) -> dict[str, Any] | None:
    """The other side of a 模型分歧 on totals, when 運彩 offers the same line."""
    totals = (card.get("market") or {}).get("totals") or {}
    tw = card.get("tw") or {}
    tw_tot = tw.get("totals") or {}
    other = "under" if side == "over" else "over"
    try:
        same = (
            abs(float(totals.get("line")) - float(line)) < 1e-9
            and abs(float(tw_tot.get("line")) - float(line)) < 1e-9
        )
    except (TypeError, ValueError):
        return None
    if not same or other not in totals:
        return None
    q = totals[other]["fair"]
    price = (tw_tot.get(other) or {}).get("price")
    names = _names(card)
    return {
        "sport": "足球",
        "sport_key": "football",
        "competition": _competition(card),
        "match_id": str(card.get("match_id")),
        "kickoff_utc": card.get("kickoff_utc", ""),
        "match": f"{names['home']} vs {names['away']}",
        "fade_label": f"{'小' if other == 'under' else '大'} {float(line):g}",
        "market": "totals",
        "line": f"{float(line):g}",
        "bet_sides": [other],
        "fair_prob": q,
        "tw_price": price,
        "ev": round(q * price - 1, 4) if price else None,
        "fresh": bool(price and tw.get("fair_fresh", True)),
        "threshold": cfg.value_threshold,
    }


# ------------------------------------------------------------- forward record
def fade_record(
    football_reviews: Iterable[Mapping[str, Any]],
    mlb_reviews: Iterable[Mapping[str, Any]],
    signal_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Every finished game where a live strategy pointed one way before kickoff, and
    how the opposite bet did: at the 運彩 price captured before kickoff when there is
    one (units), always against the market's closing probability (hits vs expected)."""
    out: list[dict[str, Any]] = []
    for r in mlb_reviews:
        hg, ag = _int(r.get("final_h")), _int(r.get("final_a"))
        if hg is None or ag is None or hg == ag:
            continue
        p_home = _num(r.get("close_home")) or _num(r.get("open_home"))
        if p_home is None:
            continue
        fav = str(r.get("t10_fav") or ("home" if p_home >= 0.5 else "away"))
        dog = "away" if fav == "home" else "home"
        p_dog = 1 - p_home if dog == "away" else p_home
        price = _num(r.get(f"tw_{dog}"))
        base = {
            "sport": "MLB",
            "competition": "MLB",
            "match_id": str(r.get("game_pk", "")),
            "kickoff_utc": r.get("kickoff_utc", ""),
            "match": f"{r.get('away')} @ {r.get('home')}",
            "fade_label": f"{r.get(dog)} 不讓分",
            "fair_prob": p_dog,
            "tw_price": price,
            "result": WIN if (hg > ag) == (dog == "home") else LOSS,
            "score": f"{ag}-{hg}",
        }
        g = _num(r.get("t10_g"))
        if g is not None and g >= G_FADE:
            out.append({**base, "strategy": "t10_g6"})
        if fav == "home" and p_home >= HOME_FAV_MIN:
            out.append({**base, "strategy": "home_fav55"})

    reviews = {str(r.get("match_id")): r for r in football_reviews}
    for r in reviews.values():
        outcome = {"H": "home", "D": "draw", "A": "away"}.get(str(r.get("outcome") or ""))
        p = {o: _num(r.get(f"close_{o[0]}")) or _num(r.get(f"open_{o[0]}")) for o in OUTCOMES}
        if outcome is None or None in p.values():
            continue
        longest = min(OUTCOMES, key=lambda o: p[o])
        sides = [("longshot8", longest)] if p[longest] <= LONGSHOT_PROB else []
        if p["home"] < p["away"]:
            sides.append(("home_dog", "home"))
        for strategy, side in sides:
            out.append({**_record_1x2(r, side, outcome, p), "strategy": strategy})

    for s in signal_rows:
        if s.get("sport") != "football" or s.get("status") not in OPPOSITE_RESULT:
            continue
        r = reviews.get(str(s.get("match_id")))
        key_ = s.get("selection_key")
        if r is not None and key_ in OUTCOMES:
            outcome = {"H": "home", "D": "draw", "A": "away"}.get(str(r.get("outcome") or ""))
            p = {o: _num(r.get(f"close_{o[0]}")) or _num(r.get(f"open_{o[0]}")) for o in OUTCOMES}
            if outcome and None not in p.values():
                out.append({**_record_1x2(r, key_, outcome, p), "strategy": "model_signal"})
                continue
        close = _num(s.get("close_p"))
        out.append(
            {
                "strategy": "model_signal",
                "sport": "足球",
                "competition": _competition(s),
                "match_id": str(s.get("match_id", "")),
                "kickoff_utc": s.get("kickoff_utc", ""),
                "match": f"{s.get('home')} vs {s.get('away')}",
                "fade_label": f"反押 {s.get('selection')}",
                "fair_prob": 1 - close if close is not None else None,
                "tw_price": None,
                "result": OPPOSITE_RESULT[s["status"]],
                "score": s.get("result", ""),
            }
        )
    for row in out:
        row["units"] = _units(row["result"], row.get("tw_price"))
    out.sort(key=lambda x: str(x["kickoff_utc"]), reverse=True)
    return out


def _record_1x2(
    r: Mapping[str, Any], side: str, outcome: str, p: Mapping[str, float]
) -> dict[str, Any]:
    others = [o for o in OUTCOMES if o != side]
    prices = [_num(r.get(f"tw_{o[0]}")) for o in others]
    names = {"home": str(r.get("home", "")), "away": str(r.get("away", ""))}
    return {
        "sport": "足球",
        "competition": _competition(r),
        "match_id": str(r.get("match_id", "")),
        "kickoff_utc": r.get("kickoff_utc", ""),
        "match": f"{names['home']} vs {names['away']}",
        "fade_label": "＋".join(
            OUTCOME_ZH[o] + (f" {names[o]}" if o != "draw" else "") for o in others
        ),
        "fair_prob": 1 - p[side],
        "tw_price": round(dutch(prices), 3) if all(prices) else None,
        "result": WIN if outcome != side else LOSS,
        "score": f"{r.get('ft_h')}-{r.get('ft_a')}",
    }


def _units(result: str, price: float | None) -> float | None:
    """Profit of one unit at `price` (None without a 運彩 price)."""
    if price is None:
        return None
    return {
        WIN: price - 1,
        HALF_WIN: (price - 1) / 2,
        PUSH: 0.0,
        HALF_LOSS: -0.5,
        LOSS: -1.0,
    }.get(result)


def record_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per strategy: bets, wins against the market's expectation, units at 運彩."""
    rows = list(rows)
    out: dict[str, dict[str, Any]] = {}
    for key in STRATEGIES:
        mine = [r for r in rows if r["strategy"] == key]
        decided = [r for r in mine if r["result"] in (WIN, LOSS, HALF_WIN, HALF_LOSS)]
        wins = sum(
            1.0 if r["result"] == WIN else 0.5 if r["result"] == HALF_WIN else 0.0 for r in decided
        )
        priced = [r for r in mine if r.get("units") is not None]
        units = sum(r["units"] for r in priced)
        expected = sum(r["fair_prob"] for r in decided if r.get("fair_prob") is not None)
        out[key] = {
            "n": len(mine),
            "wins": wins,
            "decided": len(decided),
            "expected": round(expected, 1),
            "priced": len(priced),
            "units": round(units, 2),
            "roi": round(units / len(priced) * 100, 1) if priced else None,
        }
    return out


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _int(value: Any) -> int | None:
    v = _num(value)
    return int(v) if v is not None else None
