"""Ledger of 運彩 value bets: price taken, fair price at the time, EV, result, and CLV.

CLV (closing-line value) is filled when a later snapshot captures the market
closer to kickoff: clv = closing_fair_prob x tw_price - 1. A strategy whose
average CLV is positive is beating the market even before results come in.
"""

from __future__ import annotations

import csv
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .analysis.taiwan import derived_prob_for, fair_time, implied_from_market
from .config import MODEL
from .ledger import PENDING, PUSH, SETTLED, grade, hit_counts, profit
from .models import margin as margin_model
from .models.implied import IDENTITY, Calibration

COLUMNS = [
    "id",
    "captured_at",
    "sport",
    "competition",
    "match_id",
    "kickoff_utc",
    "home",
    "away",
    "market",
    "selection",
    "selection_key",
    "line",
    "tw_price",
    "fair_odds",
    "fair_prob",
    "ev",
    "closing_fair_prob",
    "clv",
    "status",
    "result",
    "profit",
    "settled_at",
]


def value_id(sport: str, match_id: Any, selection_key: str, line: Any) -> str:
    key = f"tw|{sport}|{match_id}|{selection_key}|{line}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in COLUMNS})


def append_values(
    path: Path,
    cards: list[dict[str, Any]],
    sport: str,
    competition_of: Any = None,
    added_rows: list[dict[str, Any]] | None = None,
) -> int:
    """Record every value bet on the cards; an existing id keeps its first (earliest) price.

    `added_rows`, when given, receives the rows this call added (for notifications)."""
    rows = read(path)
    existing = {r["id"] for r in rows}
    added = 0
    for card in cards:
        tw = card.get("tw")
        if not tw:
            continue
        match_id = card.get("match_id") or card.get("game_pk") or card.get("game_id")
        for v in tw.get("values", []):
            vid = value_id(sport, match_id, v["selection_key"], v.get("line"))
            if vid in existing:
                continue
            rows.append(
                {
                    "id": vid,
                    "captured_at": tw.get("captured_at")
                    or datetime.now(UTC).isoformat(timespec="minutes"),
                    "sport": sport,
                    "competition": card.get("competition")
                    or (competition_of(card) if competition_of else "MLB"),
                    "match_id": match_id,
                    "kickoff_utc": card["kickoff_utc"],
                    "home": card["home"],
                    "away": card["away"],
                    "market": v["market"],
                    "selection": v["selection"],
                    "selection_key": v["selection_key"],
                    "line": "" if v.get("line") is None else f"{v['line']:g}",
                    "tw_price": v["tw_price"],
                    "fair_odds": v["fair_odds"],
                    "fair_prob": v["fair_prob"],
                    "ev": v["ev"],
                    "status": PENDING,
                }
            )
            if added_rows is not None:
                added_rows.append(rows[-1])
            existing.add(vid)
            added += 1
    write(path, rows)
    return added


def update_clv(
    path: Path,
    cards: list[dict[str, Any]],
    sport: str,
    calib: Calibration = IDENTITY,
    now: datetime | None = None,
) -> int:
    """Fill closing_fair_prob/clv from a later consensus for still-pending rows.

    Prices bought at or after kickoff are in-play prices, never a close (an evening run
    prices the day's matches from 00:00 UTC, some of which are already being played):
    such a card is skipped. Its time is the h2h block's own `odds_at`, else `now`."""
    rows = read(path)
    now = now or datetime.now(UTC)
    fair_by_match: dict[str, dict[str, Any]] = {}
    for card in cards:
        mv = card.get("market")
        if not mv or not _priced_before_kickoff(card, mv, now):
            continue
        fair_by_match[str(card.get("match_id") or card.get("game_pk") or card.get("game_id"))] = mv
    updated = 0
    for row in rows:
        if row["sport"] != sport or row["status"] != PENDING:
            continue
        mv = fair_by_match.get(str(row["match_id"]))
        if not mv:
            continue
        prob = _fair_prob_for(mv, row["selection_key"], row["line"], calib, sport)
        if prob is None:
            continue
        row["closing_fair_prob"] = f"{prob:.4f}"
        row["clv"] = f"{prob * float(row['tw_price']) - 1:.4f}"
        updated += 1
    write(path, rows)
    return updated


def _priced_before_kickoff(card: dict[str, Any], mv: dict[str, Any], now: datetime) -> bool:
    try:
        kickoff = datetime.fromisoformat(str(card.get("kickoff_utc")).replace("Z", "+00:00"))
    except ValueError:
        return True  # no kickoff to compare with: as before
    bought = fair_time(mv.get("h2h")) or now
    return bought < kickoff


def _fair_prob_for(
    mv: dict[str, Any],
    key: str,
    line: str,
    calib: Calibration = IDENTITY,
    sport: str = "football",
) -> float | None:
    """Fair probability for a selection from a *fresh* consensus block (stale blocks are carried
    over from an earlier snapshot and would make CLV equal EV, which proves nothing).

    Derived selections (team totals, both teams to score, first half, other totals
    lines...) are re-priced through the market-implied model from a fresh 1X2 and a
    fresh totals line; NBA handicap and totals lines away from the main line through the
    normal margin model from the fresh main block.
    """
    direct = _direct_fair_prob(mv, key, line)
    if direct is not None:
        return direct
    if sport == "nba":
        return _nba_line_prob(mv, key, line)
    if key in ("home", "draw", "away", "spread_home", "spread_away"):
        return None
    tot = mv.get("totals") or {}
    if key in ("over", "under") and line and tot and abs(float(line) - tot["line"]) < 1e-9:
        return None  # the market's own line, but stale: re-deriving it would echo the old price
    h2h = mv.get("h2h")
    # Both inputs of the implied model must be fresh: with the morning's totals the
    # goal level is the morning's, and the "closing" price would echo the bet's EV.
    if not h2h or h2h.get("stale") or not tot or tot.get("stale"):
        return None
    g = implied_from_market(mv)
    return derived_prob_for(g, key, float(line) if line else None, calib) if g else None


def _nba_line_prob(mv: dict[str, Any], key: str, line: str) -> float | None:
    """An NBA handicap or totals line the consensus did not quote: translated from its
    fresh main line with the normal margin model (analysis/taiwan.nba_derived_compare
    priced the bet the same way)."""
    if not line:
        return None
    wanted = float(line)
    if key in ("spread_home", "spread_away"):
        block = mv.get("spread") or {}
        if not block or block.get("stale") or block.get("line") is None:
            return None
        mu = margin_model.mean_from_cover(
            float(block["line"]), block["home"]["fair"], MODEL.nba_margin_sigma
        )
        home, push, away = margin_model.cover_probs(mu, MODEL.nba_margin_sigma, wanted)
        return margin_model.conditional(home if key == "spread_home" else away, push)
    if key in ("over", "under"):
        block = mv.get("totals") or {}
        if not block or block.get("stale") or block.get("line") is None:
            return None
        mu = margin_model.mean_from_over(
            float(block["line"]), block["over"]["fair"], MODEL.nba_total_sigma
        )
        over, push, under = margin_model.over_probs(mu, MODEL.nba_total_sigma, wanted)
        return margin_model.conditional(over if key == "over" else under, push)
    return None


def _direct_fair_prob(mv: dict[str, Any], key: str, line: str) -> float | None:
    if key in ("home", "draw", "away"):
        h = mv.get("h2h")
        if h and not h.get("stale"):
            return h.get(key, {}).get("fair")
        return None
    if key in ("over", "under"):
        t = mv.get("totals")
        if t and not t.get("stale") and line and abs(float(line) - t["line"]) < 1e-9:
            return t[key]["fair"]
        return None
    if key in ("spread_home", "spread_away"):
        s = mv.get("spread")
        side = key.split("_")[1]
        if s and not s.get("stale") and line and abs(float(line) - s["line"]) < 1e-9:
            return s[side]["fair"]
    return None


def settle(
    path: Path,
    results: dict[str, tuple[int, ...] | tuple[int | None, ...]],
    sport: str,
    now: datetime | None = None,
) -> int:
    """Settle pending rows of `sport` with final scores keyed by match_id / game_pk / game_id
    (as strings).

    A score is (home, away) or, for football, (home, away, home at half-time, away at
    half-time); first-half bets wait until the half-time score is known.
    """
    rows = read(path)
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    settled = 0
    for row in rows:
        if row["sport"] != sport or row["status"] != PENDING:
            continue
        score = results.get(str(row["match_id"]))
        if score is None:
            continue
        hg, ag = int(score[0] or 0), int(score[1] or 0)
        ht = (int(score[2]), int(score[3])) if len(score) >= 4 and None not in score[2:4] else None
        outcome = grade(
            row["selection_key"], float(row["line"]) if row["line"] else None, hg, ag, ht
        )
        if not outcome:
            continue
        row["status"] = outcome
        row["result"] = f"{hg}-{ag}"
        row["profit"] = f"{profit(outcome, float(row['tw_price'])):.3f}"
        row["settled_at"] = stamp
        settled += 1
    write(path, rows)
    return settled


def summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    settled = [r for r in rows if r["status"] in SETTLED]
    wins, decided = hit_counts(settled)
    units = sum(float(r["profit"] or 0) for r in settled)
    staked = sum(1 for r in settled if r["status"] != PUSH)
    clvs = [float(r["clv"]) for r in rows if r.get("clv")]
    return {
        "pending": sum(1 for r in rows if r["status"] == PENDING),
        "settled": len(settled),
        "wins": wins,
        "losses": decided - wins,
        "hit_rate": round(wins / decided, 3) if decided else None,
        "units": round(units, 2),
        "roi": round(units / staked, 3) if staked else None,
        "avg_ev": round(sum(float(r["ev"]) for r in rows) / len(rows), 4) if rows else None,
        "avg_clv": round(sum(clvs) / len(clvs), 4) if clvs else None,
        "n_clv": len(clvs),
    }
