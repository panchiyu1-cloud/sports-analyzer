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

Every move caught (3, and `intl_big_moves` for 1) is also kept for good in
data/moves/events.csv (`record_events`), whether or not it ever became a bet;
`event_history` joins it with the prices up to kickoff and the result (docs/bighand.html).
"""

from __future__ import annotations

import csv
import hashlib
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import ModelConfig
from .models.market import devig, g_value

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


TAPE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9].csv"  # YYYY-MM.csv; events.csv is not tape
EVENTS_FILE = "events.csv"  # the 大戶 record, next to the tape


def read(folder: Path, months: int | None = 2) -> list[dict[str, str]]:
    """The newest `months` month files (all with None), oldest first."""
    rows: list[dict[str, str]] = []
    paths = sorted(folder.glob(TAPE_GLOB))
    for path in paths[-months:] if months else paths:
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
        # Only uploads with an international price nearby can say "the market did not
        # move"; an upload without one is skipped rather than breaking the chain (a
        # mid-day manual upload must not hide the 08:00 -> 20:00 comparison).
        times = sorted(
            t
            for t in states
            if (kickoff is None or t < kickoff)
            and any(
                _near(series.get((sport, mid, market, line, side, "intl"), []), t, within)
                is not None
                for side in states[t]
            )
        )
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
                        "type": "tw_only",
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


def intl_big_moves(rows: Iterable[Mapping[str, str]], cfg: ModelConfig) -> list[dict[str, Any]]:
    """The first time a side's international probability stood `big_move_threshold` above
    where we first saw it (before kickoff): big money came in on that side. One event
    per side; the drop on the other sides is the same move."""
    rows = list(rows)
    series = _series(rows)
    meta = {(r["sport"], str(r["match_id"])): r for r in rows}
    within = timedelta(minutes=cfg.max_price_gap_minutes)
    events = []
    for (sport, mid, market, line, side, source), points in series.items():
        if source != "intl":
            continue
        info = meta.get((sport, mid), {})
        kickoff = parse_time(info.get("kickoff_utc"))
        pts = [p for p in points if kickoff is None or p[0] < kickoff]
        if len(pts) < 2:
            continue
        for at, prob, _ in pts[1:]:
            if prob - pts[0][1] < cfg.big_move_threshold:
                continue
            tw = series.get((sport, mid, market, line, side, "tw"), [])
            near_tw = min(tw, key=lambda x: abs(x[0] - at), default=None)
            if near_tw is not None and abs(near_tw[0] - at) > within:
                near_tw = None
            events.append(
                {
                    "type": "intl_big",
                    "sport": sport,
                    "match_id": mid,
                    "home": info.get("home", ""),
                    "away": info.get("away", ""),
                    "kickoff_utc": info.get("kickoff_utc", ""),
                    "market": market,
                    "line": line,
                    "side": side,
                    "from_at": _iso(pts[0][0]),
                    "to_at": _iso(at),
                    "tw_move": "",
                    "intl_move": round(prob - pts[0][1], 4),
                    "divergence": "",
                    "expected": round(prob, 4),
                    "tw_price": near_tw[2] if near_tw else "",
                }
            )
            break
    events.sort(key=lambda e: e["to_at"])
    return events


# ------------------------------------------------- the permanent 大戶 record
EVENT_COLUMNS = [
    "id",
    "type",  # tw_only (運彩大戶) | intl_big (國際大單)
    "to_at",  # when the move was seen: the later 運彩 upload / the international price
    "from_at",  # what it was measured from: the earlier upload / our first price
    "sport",
    "competition",
    "match_id",
    "kickoff_utc",
    "home",
    "away",
    "home_zh",  # 運彩's names, when the match was on 運彩 at the time
    "away_zh",
    "market",
    "line",
    "side",
    "tw_move",
    "intl_move",
    "divergence",
    "expected",  # the international probability of the side when the move was seen
    "tw_price",  # 運彩's price for the side then
]
EVENT_NUMBERS = ("tw_move", "intl_move", "divergence", "expected", "tw_price")


def event_id(e: Mapping[str, Any]) -> str:
    """One record per detection: a 運彩-only move per match, market and upload (on the
    side it was first seen on, even if a later price would pick another), an
    international 大單 per side."""
    parts = [str(e[k]) for k in ("type", "sport", "match_id", "market", "line", "to_at")]
    if e["type"] == "intl_big":
        parts.append(str(e["side"]))
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def record_events(
    path: Path,
    events: Iterable[Mapping[str, Any]],
    names: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
) -> int:
    """Keep every detected 大戶 move (data/moves/events.csv), whatever happened next:
    the record is the point, not only the moves that became a bet. `names` maps
    (sport, match_id) to what is known at detection time and lost later (competition,
    運彩's Chinese team names). Returns how many events were new."""
    existing = read_events(path)
    have = {r["id"] for r in existing}
    fresh = []
    for e in events:
        eid = event_id(e)
        if eid in have:
            continue
        have.add(eid)
        extra = (names or {}).get((str(e["sport"]), str(e["match_id"])), {})
        fresh.append({**extra, **e, "id": eid})
    if not fresh:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    # A file written with older columns is rewritten whole: appending to it would
    # put the new fields under the wrong headings.
    same_header = bool(existing) and list(existing[0].keys()) == EVENT_COLUMNS
    rows = fresh if same_header else existing + fresh
    with path.open("a" if same_header else "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EVENT_COLUMNS)
        if not same_header:
            writer.writeheader()
        for e in rows:
            writer.writerow({c: _cell(e.get(c)) for c in EVENT_COLUMNS})
    return len(fresh)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return f"{value:g}" if isinstance(value, float) else str(value)


def read_events(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def trajectory(
    rows: Iterable[Mapping[str, str]],
    sport: str,
    match_id: str,
    market: str,
    line: str,
    before: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every time we saw one market of one match (before `before`, the kickoff), oldest
    first: the international probability and 運彩's price per side at that time."""
    points: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: {"intl": {}, "tw": {}})
    for r in rows:
        if (
            r["sport"] != sport
            or str(r["match_id"]) != str(match_id)
            or r["market"] != market
            or r.get("line", "") != line
        ):
            continue
        at = parse_time(r["observed_at"])
        if before is not None and (at is None or at >= before):
            continue
        value = float(r["price"]) if r["source"] == "tw" and r.get("price") else float(r["prob"])
        points[r["observed_at"]][r["source"]][r["side"]] = value
    return [{"at": at, **points[at]} for at in sorted(points)]


def g_points(rows: Iterable[Mapping[str, str]], before: datetime | None = None) -> dict[str, float]:
    """MLB: the owner's T-10 G (favourite moneyline + favourite -1.5 cover - 1, both
    international) at each time we bought the moneyline and the +/-1.5 run line
    together, keyed by observed_at. Rows of one game."""
    seen: dict[str, dict[str, Any]] = defaultdict(lambda: {"h2h": {}, "spread": {}, "line": None})
    for r in rows:
        if r["source"] != "intl" or r["market"] not in ("h2h", "spread"):
            continue
        at = parse_time(r["observed_at"])
        if at is None or (before is not None and at >= before):
            continue
        obs = seen[r["observed_at"]]
        if r["market"] == "spread":
            line = _number(r.get("line"))
            if line is None or abs(abs(line) - 1.5) > 1e-9:
                continue
            obs["line"] = line  # the home handicap: -1.5 = home gives 1.5 runs
        obs[r["market"]][r["side"]] = float(r["prob"])
    out = {}
    for at, obs in seen.items():
        ml, rl, line = obs["h2h"], obs["spread"], obs["line"]
        if "home" not in ml or "away" not in ml or line is None:
            continue
        fav = "home" if ml["home"] >= ml["away"] else "away"
        if (line < 0) != (fav == "home") or fav not in rl:
            continue  # the run line has the moneyline underdog giving the runs
        out[at] = round(g_value(ml[fav], rl[fav]), 4)
    return out


# tw_ledger selection keys of each market side (a 大戶 side that became a value bet)
SELECTION_KEYS = {
    ("h2h", "home"): "home",
    ("h2h", "draw"): "draw",
    ("h2h", "away"): "away",
    ("totals", "over"): "over",
    ("totals", "under"): "under",
    ("spread", "home"): "spread_home",
    ("spread", "away"): "spread_away",
}
MARKET_ORDER = {"h2h": 0, "spread": 1, "totals": 2}
# After the move was seen, the market "kept going" / "came back" past this much.
FOLLOW_MIN = 0.005


def _before(
    points: list[tuple[datetime, float, float | None]], kickoff: datetime | None
) -> list[tuple[datetime, float, float | None]]:
    return [p for p in points if kickoff is None or p[0] < kickoff]


def _first_filled(evs: list[Mapping[str, Any]], key: str) -> str:
    return next((str(e[key]) for e in evs if e.get(key)), "")


def event_history(
    events: Iterable[Mapping[str, Any]],
    rows: Iterable[Mapping[str, str]],
    scores: Mapping[tuple[str, str], tuple[int, int]],
    ledger_rows: Iterable[Mapping[str, str]] = (),
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every match with a recorded 大戶 move, newest kickoff first: its events, where the
    prices went afterwards (the side's last international probability and 運彩 price
    before kickoff), whether that side became a 運彩 value bet, the result, the match's
    value bets, and the whole price trajectory of each market involved (MLB: the
    moneyline always, with G)."""
    now = now or datetime.now(UTC)
    rows_by_match: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
    for r in rows:
        rows_by_match[(r["sport"], str(r["match_id"]))].append(r)
    bets_by_match: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
    for b in ledger_rows:
        bets_by_match[(b["sport"], str(b["match_id"]))].append(b)
    by_match: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        numbers = {k: _number(e.get(k)) for k in EVENT_NUMBERS}
        by_match[(str(e["sport"]), str(e["match_id"]))].append({**e, **numbers})
    out = []
    for (sport, mid), evs in by_match.items():
        evs.sort(key=lambda x: str(x["to_at"]))
        kickoff = parse_time(evs[0].get("kickoff_utc"))
        closed = kickoff is not None and kickoff <= now
        match_rows = rows_by_match.get((sport, mid), [])
        series = _series(match_rows)
        bets = sorted(bets_by_match.get((sport, mid), []), key=lambda b: b.get("captured_at", ""))
        bet_keys = {(b["selection_key"], b.get("line") or "") for b in bets}
        score = scores.get((sport, mid))
        views = []
        for e in evs:
            line = str(e.get("line") or "")
            key = (sport, mid, e["market"], line, e["side"])
            intl = _before(series.get((*key, "intl"), []), kickoff)
            tw = _before(series.get((*key, "tw"), []), kickoff)
            close = intl[-1][1] if intl else None
            expected, price = e["expected"], e["tw_price"]
            views.append(
                {
                    **e,
                    "line": line,
                    "closed": closed,
                    # EV of taking 運彩's price for the side when the move was seen
                    "ev_at": round(expected * price - 1, 4)
                    if expected is not None and price
                    else None,
                    "close_intl": close,
                    "close_tw_price": tw[-1][2] if tw else None,
                    # > 0: the market kept moving toward the side after the move was seen
                    "after": round(close - expected, 4)
                    if close is not None and expected is not None
                    else None,
                    "value_bet": (SELECTION_KEYS.get((e["market"], e["side"])), line) in bet_keys,
                    "result": outcome(e["market"], e["side"], line, *score) if score else "",
                    "score": f"{score[0]}-{score[1]}" if score else "",
                }
            )
        markets = {(e["market"], str(e.get("line") or "")) for e in evs}
        if sport == "mlb":
            markets.add(("h2h", ""))
        trajectories = [
            {
                "market": m,
                "line": ln,
                "points": trajectory(match_rows, sport, mid, m, ln, kickoff),
            }
            for m, ln in sorted(markets, key=lambda k: (MARKET_ORDER.get(k[0], 9), k[1]))
        ]
        g = g_points(match_rows, kickoff) if sport == "mlb" else {}
        for t in trajectories:
            if t["market"] == "h2h" and g:
                for p in t["points"]:
                    p["g"] = g.get(p["at"])
        g_seq = [g[at] for at in sorted(g)]
        out.append(
            {
                "sport": sport,
                "match_id": mid,
                "competition": _first_filled(evs, "competition"),
                "home": _first_filled(evs, "home"),
                "away": _first_filled(evs, "away"),
                "home_zh": _first_filled(evs, "home_zh"),
                "away_zh": _first_filled(evs, "away_zh"),
                "kickoff_utc": _first_filled(evs, "kickoff_utc"),
                "closed": closed,
                "score": f"{score[0]}-{score[1]}" if score else "",
                "events": views,
                "markets": [t for t in trajectories if t["points"]],
                "value_bets": bets,
                "g_last": g_seq[-1] if g_seq else None,
                "g_peak": max(g_seq, key=abs) if g_seq else None,
            }
        )
    out.sort(key=lambda m: str(m["kickoff_utc"]), reverse=True)
    return out


def history_summary(history: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per event type: results against the market's expectation, how often the
    international market kept going toward the side or came back by kickoff, and how
    many of the sides became a 運彩 value bet."""
    by_type: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    matches: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for match in history:
        for e in match["events"]:
            by_type[e["type"]].append(e)
            matches[e["type"]].add((match["sport"], match["match_id"]))
    out = {}
    for kind, evs in by_type.items():
        s = divergence_summary(evs)
        afters = [e["after"] for e in evs if e.get("closed") and e.get("after") is not None]
        s["matches"] = len(matches[kind])
        s["with_close"] = len(afters)
        s["followed"] = sum(1 for a in afters if a >= FOLLOW_MIN)
        s["reversed"] = sum(1 for a in afters if a <= -FOLLOW_MIN)
        s["value_bets"] = sum(1 for e in evs if e.get("value_bet"))
        out[kind] = s
    return out


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
    done = [
        e
        for e in events
        if e.get("result") in ("win", "loss") and _number(e.get("expected")) is not None
    ]
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
