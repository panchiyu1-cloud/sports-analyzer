"""Line-move tape: the international fair prices and 運彩's prices we saw, per match and time.

data/moves/YYYY-MM.csv (by observation month), one row per observed price:

  observed_at   UTC, to the minute: when the consensus was bought / the phone uploaded 運彩
  source        intl | tw
  sport         football | mlb
  match_id      football-data.org match id / MLB game_pk
  kickoff_utc, home, away
  market        h2h | totals | spread
  side          home | draw | away | over | under
  line          totals line / home handicap (blank for h2h)
  prob          de-vigged probability: the international consensus, or 運彩's own prices
                de-vigged the same way, so that both move on one scale
  price         運彩's decimal price (blank for intl)

Read back it answers three questions (the "大手" study of 2026-10-14 found that money
moving a price is usually right, and that the only profit is in getting the price
before it has moved):
  1. `track`: how far the international price moved since we first saw the match
     (a 大單 when a side moved >= big_move_threshold);
  2. `track`: whether 運彩 followed over the same time;
  3. `tw_divergences`: 運彩's own moves that the market did not make (運彩's big
     bettors), settled against results by `settle` / `divergence_summary`.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import ModelConfig
from .models.market import devig

COLUMNS = [
    "observed_at",
    "source",
    "sport",
    "match_id",
    "kickoff_utc",
    "home",
    "away",
    "market",
    "side",
    "line",
    "prob",
    "price",
]
MARKET_SIDES = {
    "h2h": ("home", "draw", "away"),
    "totals": ("over", "under"),
    "spread": ("home", "away"),
}


# ------------------------------------------------------------------- the tape
def parse_time(value: Any) -> datetime | None:
    """Any ISO time with an offset (or Z) -> aware UTC datetime; None when unknown."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="minutes")


def _line(value: Any) -> str:
    return "" if value is None or value == "" else f"{float(value):g}"


def _key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row[k])
        for k in ("observed_at", "source", "sport", "match_id", "market", "side", "line")
    )


def read(folder: Path, months: int = 2) -> list[dict[str, str]]:
    """The newest `months` month files, oldest first."""
    rows: list[dict[str, str]] = []
    for path in sorted(folder.glob("*.csv"))[-months:]:
        with path.open(newline="", encoding="utf-8") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def record(folder: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    """Append the rows the tape does not have yet (same time, source, match, market,
    side, line). The same upload is compared several times a day; it is kept once."""
    by_file: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        at = parse_time(row["observed_at"])
        if at is None:
            continue
        by_file[folder / f"{at:%Y-%m}.csv"].append({**row, "observed_at": _iso(at)})
    added = 0
    for path, new in by_file.items():
        seen: set[tuple[str, ...]] = set()
        if path.exists():
            with path.open(newline="", encoding="utf-8") as fh:
                seen = {_key(r) for r in csv.DictReader(fh)}
        fresh = []
        for row in new:
            k = _key(row)
            if k not in seen:
                seen.add(k)
                fresh.append(row)
        if not fresh:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            if write_header:
                writer.writeheader()
            for row in fresh:
                writer.writerow({c: row.get(c, "") for c in COLUMNS})
        added += len(fresh)
    return added


def _card_base(card: Mapping[str, Any], sport: str, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "sport": sport,
        "match_id": str(card.get("match_id") or card.get("game_pk")),
        "kickoff_utc": card.get("kickoff_utc", ""),
        "home": card.get("home", ""),
        "away": card.get("away", ""),
    }


def intl_rows(
    cards: Iterable[Mapping[str, Any]], sport: str, default_at: Any = None
) -> list[dict[str, Any]]:
    """The fresh consensus blocks of the cards (carried-over "stale" ones are skipped:
    their prices are an older observation, already on the tape). `default_at` stands in
    for blocks made before they carried their own time (backfill of old snapshots)."""
    out = []
    for card in cards:
        base = _card_base(card, sport, "intl")
        for market, sides in MARKET_SIDES.items():
            block = (card.get("market") or {}).get(market)
            if not isinstance(block, dict) or block.get("stale"):
                continue
            at = parse_time(block.get("odds_at")) or parse_time(default_at)
            if at is None:
                continue
            for side in sides:
                entry = block.get(side)
                if isinstance(entry, dict) and entry.get("fair") is not None:
                    out.append(
                        {
                            **base,
                            "observed_at": _iso(at),
                            "market": market,
                            "side": side,
                            "line": _line(block.get("line")) if market != "h2h" else "",
                            "prob": f"{float(entry['fair']):.4f}",
                            "price": "",
                        }
                    )
    return out


def tw_rows(
    cards: Iterable[Mapping[str, Any]], sport: str, method: str = "power"
) -> list[dict[str, Any]]:
    """運彩's prices on the cards, with the probability its own prices imply."""
    out = []
    for card in cards:
        tw = card.get("tw") or {}
        at = parse_time(tw.get("captured_at"))
        if at is None:
            continue
        base = _card_base(card, sport, "tw")
        for market, sides in MARKET_SIDES.items():
            block = tw.get(market)
            if not isinstance(block, dict):
                continue
            present = [s for s in sides if isinstance(block.get(s), dict) and block[s].get("price")]
            if len(present) < 2:
                continue
            probs = devig([float(block[s]["price"]) for s in present], method)
            for side, prob in zip(present, probs, strict=True):
                out.append(
                    {
                        **base,
                        "observed_at": _iso(at),
                        "market": market,
                        "side": side,
                        "line": _line(block.get("line")) if market != "h2h" else "",
                        "prob": f"{prob:.4f}",
                        "price": f"{float(block[side]['price']):g}",
                    }
                )
    return out


# ------------------------------------------------------------- 1 + 2: tracking
def _series(
    rows: Iterable[Mapping[str, str]],
) -> dict[tuple[str, ...], list[tuple[datetime, float, float | None]]]:
    """(sport, match, market, line, side, source) -> time-ordered (time, prob, price)."""
    out: dict[tuple[str, ...], list[tuple[datetime, float, float | None]]] = defaultdict(list)
    for r in rows:
        at = parse_time(r.get("observed_at"))
        try:
            prob = float(r["prob"])
        except (KeyError, TypeError, ValueError):
            continue
        if at is None:
            continue
        price = float(r["price"]) if r.get("price") else None
        key = (
            r["sport"],
            str(r["match_id"]),
            r["market"],
            r.get("line", ""),
            r["side"],
            r["source"],
        )
        out[key].append((at, prob, price))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def _near(
    points: list[tuple[datetime, float, float | None]], t: datetime, within: timedelta
) -> float | None:
    """The observation closest in time to `t`, if one lies within `within`."""
    best = None
    for at, prob, _ in points:
        gap = abs(at - t)
        if gap <= within and (best is None or gap < best[0]):
            best = (gap, prob)
    return best[1] if best else None


def track(
    rows: Iterable[Mapping[str, str]], cfg: ModelConfig
) -> dict[tuple[str, str], dict[str, Any]]:
    """Per (sport, match_id): for each market side, the international probability when we
    first and last saw it, and 運彩's first and last price over its own uploads, with
    the international move over the same span (how much of it 運彩 followed)."""
    series = _series(rows)
    within = timedelta(minutes=cfg.max_price_gap_minutes)
    matches: dict[tuple[str, str], dict[str, Any]] = {}
    for (sport, mid, market, line, side, source), intl in series.items():
        if source != "intl":
            continue
        tw = series.get((sport, mid, market, line, side, "tw"), [])
        entry: dict[str, Any] = {
            "market": market,
            "line": line,
            "side": side,
            "first": intl[0][1],
            "first_at": _iso(intl[0][0]),
            "last": intl[-1][1],
            "last_at": _iso(intl[-1][0]),
            "move": round(intl[-1][1] - intl[0][1], 4),
            "tw_first_price": tw[0][2] if tw else None,
            "tw_last_price": tw[-1][2] if tw else None,
            "tw_first_at": _iso(tw[0][0]) if tw else None,
            "tw_last_at": _iso(tw[-1][0]) if tw else None,
            "tw_move": round(tw[-1][1] - tw[0][1], 4) if len(tw) >= 2 else None,
            "intl_span_move": None,
            "follow": None,
        }
        if len(tw) >= 2:
            a, b = _near(intl, tw[0][0], within), _near(intl, tw[-1][0], within)
            if a is not None and b is not None:
                entry["intl_span_move"] = round(b - a, 4)
                if abs(b - a) >= 0.01:
                    entry["follow"] = round(entry["tw_move"] / (b - a), 2)
        m = matches.setdefault((sport, mid), {"sides": [], "big": False})
        entry["big"] = abs(entry["move"]) >= cfg.big_move_threshold
        m["big"] = m["big"] or entry["big"]
        m["sides"].append(entry)
    for m in matches.values():
        order = {"h2h": 0, "spread": 1, "totals": 2}
        sides = {"home": 0, "draw": 1, "away": 2, "over": 3, "under": 4}
        m["sides"].sort(key=lambda e: (order[e["market"]], e["line"], sides[e["side"]]))
        m["first_at"] = min(e["first_at"] for e in m["sides"])
        m["last_at"] = max(e["last_at"] for e in m["sides"])
    return matches


# --------------------------------------------------------- 3: 運彩-only moves
def tw_divergences(rows: Iterable[Mapping[str, str]], cfg: ModelConfig) -> list[dict[str, Any]]:
    """Between two consecutive 運彩 uploads, 運彩's move on a side minus the international
    move over the same times. Where 運彩 moved toward a side by at least
    `tw_divergence_threshold` more than the market did, money at 運彩 (its own big
    bettors) pushed it there: one event per match, market and upload pair, on the side
    the money moved toward. Needs an international price within the freshness window of
    both uploads, so that "the market did not move" is an observation, not a guess."""
    rows = list(rows)
    series = _series(rows)
    meta = {(r["sport"], str(r["match_id"])): r for r in rows}
    within = timedelta(minutes=cfg.max_price_gap_minutes)
    # 運彩 state per market at each upload: side -> (prob, price)
    tw_states: dict[tuple[str, ...], dict[datetime, dict[str, tuple[float, float | None]]]] = (
        defaultdict(dict)
    )
    for (sport, mid, market, line, side, source), points in series.items():
        if source != "tw":
            continue
        for at, prob, price in points:
            tw_states[(sport, mid, market, line)].setdefault(at, {})[side] = (prob, price)
    events = []
    for (sport, mid, market, line), states in tw_states.items():
        info = meta.get((sport, mid), {})
        kickoff = parse_time(info.get("kickoff_utc"))
        times = sorted(t for t in states if kickoff is None or t < kickoff)
        for t1, t2 in zip(times, times[1:], strict=False):
            best = None
            for side, (p2, price2) in states[t2].items():
                if side not in states[t1]:
                    continue
                intl = series.get((sport, mid, market, line, side, "intl"), [])
                i1, i2 = _near(intl, t1, within), _near(intl, t2, within)
                if i1 is None or i2 is None:
                    continue
                dtw = p2 - states[t1][side][0]
                div = dtw - (i2 - i1)
                if best is None or div > best["divergence"]:
                    best = {
                        "sport": sport,
                        "match_id": mid,
                        "home": info.get("home", ""),
                        "away": info.get("away", ""),
                        "kickoff_utc": info.get("kickoff_utc", ""),
                        "market": market,
                        "line": line,
                        "side": side,
                        "from_at": _iso(t1),
                        "to_at": _iso(t2),
                        "tw_move": round(dtw, 4),
                        "intl_move": round(i2 - i1, 4),
                        "divergence": round(div, 4),
                        "expected": round(i2, 4),  # the market's own chance for that side
                        "tw_price": price2,
                    }
            if best and best["divergence"] >= cfg.tw_divergence_threshold:
                events.append(best)
    events.sort(key=lambda e: e["to_at"])
    return events


def outcome(market: str, side: str, line: str, home: int, away: int) -> str:
    """win / loss / push for one side, from the final score."""
    if market == "h2h":
        actual = "home" if home > away else ("away" if away > home else "draw")
        return "win" if actual == side else "loss"
    if market == "totals":
        total, ln = home + away, float(line)
        if total == ln:
            return "push"
        return "win" if (total > ln) == (side == "over") else "loss"
    margin = home - away + float(line)  # spread: line is the home handicap
    if margin == 0:
        return "push"
    return "win" if (margin > 0) == (side == "home") else "loss"


def settle(
    events: Iterable[Mapping[str, Any]], scores: Mapping[tuple[str, str], tuple[int, int]]
) -> list[dict[str, Any]]:
    out = []
    for e in events:
        score = scores.get((e["sport"], str(e["match_id"])))
        result = outcome(e["market"], e["side"], e["line"], *score) if score else ""
        out.append({**e, "result": result, "score": f"{score[0]}-{score[1]}" if score else ""})
    return out


def divergence_summary(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Did the side 運彩's money moved toward win more often than the market expected?"""
    events = list(events)
    done = [e for e in events if e.get("result") in ("win", "loss")]
    hits = sum(1 for e in done if e["result"] == "win")
    expected = sum(float(e["expected"]) for e in done)
    var = sum(float(e["expected"]) * (1 - float(e["expected"])) for e in done)
    return {
        "events": len(events),
        "settled": len(done),
        "hits": hits,
        "expected": round(expected, 1),
        "z": round((hits - expected) / math.sqrt(var), 2) if var > 0 else None,
    }
