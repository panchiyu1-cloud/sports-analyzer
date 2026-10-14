"""Signal ledger: every recommendation is written down, then settled against results.

This is the honesty layer. Three months of rows beat any argument about whether
the model "works". Columns are fixed so the CSV opens cleanly in a spreadsheet.
"""

from __future__ import annotations

import csv
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .fetch.football import Match

COLUMNS = [
    "id",
    "created_at",
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
    "model_p",
    "market_p",
    "price",
    "book",
    "edge",
    "confidence",
    "kelly",
    "status",
    "result",
    "profit",
    "settled_at",
]
PENDING, WIN, LOSS, PUSH, VOID = "pending", "win", "loss", "push", "void"
HALF_WIN, HALF_LOSS = "half_win", "half_loss"
SETTLED = (WIN, LOSS, PUSH, HALF_WIN, HALF_LOSS)


def signal_id(signal: dict[str, Any]) -> str:
    key = f"{signal['sport']}|{signal['match_id']}|{signal['market']}|{signal['selection_key']}|{signal.get('line')}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in COLUMNS})


def append_signals(path: Path, signals: list[dict[str, Any]], now: datetime | None = None) -> int:
    """Add new signals; an existing id (same match/market/selection) is left untouched."""
    rows = read(path)
    existing = {r["id"] for r in rows}
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    added = 0
    for s in signals:
        sid = signal_id(s)
        if sid in existing:
            continue
        rows.append(
            {**s, "id": sid, "created_at": stamp, "status": PENDING, "line": _fmt(s.get("line"))}
        )
        existing.add(sid)
        added += 1
    write(path, rows)
    return added


def settle_football(path: Path, results: list[Match], now: datetime | None = None) -> int:
    """Settle pending football rows whose match has a final score."""
    rows = read(path)
    by_id = {m.id: m for m in results if m.is_finished}
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    settled = 0
    for row in rows:
        if row["status"] != PENDING or row["sport"] != "football":
            continue
        try:
            m = by_id.get(int(row["match_id"]))
        except ValueError:
            continue
        if m is None:
            continue
        outcome = grade(
            row["selection_key"], _float(row["line"]), m.home_goals or 0, m.away_goals or 0
        )
        price = _float(row["price"]) or 1.0
        row["status"] = outcome
        row["result"] = f"{m.home_goals}-{m.away_goals}"
        row["profit"] = f"{profit(outcome, price):.3f}"
        row["settled_at"] = stamp
        settled += 1
    write(path, rows)
    return settled


def grade(
    selection_key: str,
    line: float | None,
    hg: int,
    ag: int,
    ht: tuple[int, int] | None = None,
) -> str:
    """Win/loss/push (or half win/half loss on a quarter line) for one selection.

    `ht` is the half-time score, needed only by first-half selections; without it
    those return "" (cannot be settled yet).
    """
    if selection_key.startswith("ht_"):
        if ht is None:
            return ""
        return grade(selection_key[3:], line, ht[0], ht[1])
    if selection_key in ("btts_yes", "btts_no"):
        both = hg > 0 and ag > 0
        return WIN if both == (selection_key == "btts_yes") else LOSS
    if selection_key in ("odd", "even"):
        return WIN if ((hg + ag) % 2 == 1) == (selection_key == "odd") else LOSS
    if selection_key.startswith("team_") and line is not None:
        # team_home_over / team_away_under ...: that team's goals against the line.
        _, side, direction = selection_key.split("_")
        goals = hg if side == "home" else ag
        return grade(direction, line, goals, 0)
    if selection_key.startswith("hcp3_") and line is not None:
        margin = hg - ag + line
        outcome = "home" if margin > 0 else "away" if margin < 0 else "draw"
        return WIN if selection_key == f"hcp3_{outcome}" else LOSS
    if selection_key == "home":
        return WIN if hg > ag else LOSS
    if selection_key == "away":
        return WIN if ag > hg else LOSS
    if selection_key == "draw":
        return WIN if hg == ag else LOSS
    if line is None:
        return VOID
    if _is_quarter(line) and selection_key in ("over", "under", "spread_home", "spread_away"):
        # A quarter line is two half stakes on the neighbouring lines: -0.25 = 0 and -0.5.
        return _combine(
            grade(selection_key, line - 0.25, hg, ag), grade(selection_key, line + 0.25, hg, ag)
        )
    if selection_key in ("over", "under"):
        total = hg + ag
        if total == line:
            return PUSH
        return WIN if (total > line) == (selection_key == "over") else LOSS
    if selection_key in ("spread_home", "spread_away"):
        margin = hg - ag + line  # home view
        if selection_key == "spread_away":
            margin = -margin
        if abs(margin) < 1e-9:
            return PUSH
        return WIN if margin > 0 else LOSS
    return VOID


def _is_quarter(line: float) -> bool:
    return abs(line * 2 - round(line * 2)) > 1e-9


def _combine(a: str, b: str) -> str:
    """Settle the two half stakes of a quarter line as one outcome."""
    if a == b:
        return a
    pair = {a, b}
    if pair == {WIN, PUSH}:
        return HALF_WIN
    if pair == {LOSS, PUSH}:
        return HALF_LOSS
    return VOID  # win + loss cannot happen on neighbouring half lines


def profit(outcome: str, price: float, stake: float = 1.0) -> float:
    if outcome == WIN:
        return stake * (price - 1.0)
    if outcome == HALF_WIN:
        return stake * (price - 1.0) / 2
    if outcome == LOSS:
        return -stake
    if outcome == HALF_LOSS:
        return -stake / 2
    return 0.0


def summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Headline numbers for the page: settled count, hit rate, units, ROI."""
    settled = [r for r in rows if r["status"] in SETTLED]
    wins, decided = hit_counts(settled)
    units = sum(_float(r["profit"]) or 0.0 for r in settled)
    staked = sum(1 for r in settled if r["status"] != PUSH)
    return {
        "pending": sum(1 for r in rows if r["status"] == PENDING),
        "settled": len(settled),
        "wins": wins,
        "losses": decided - wins,
        "hit_rate": round(wins / decided, 3) if decided else None,
        "units": round(units, 2),
        "roi": round(units / staked, 3) if staked else None,
    }


def hit_counts(rows: list[dict[str, str]]) -> tuple[float, float]:
    """(wins, decided) where a half win/loss counts as half a decided bet."""
    weight = {WIN: (1.0, 1.0), LOSS: (0.0, 1.0), HALF_WIN: (0.5, 0.5), HALF_LOSS: (0.0, 0.5)}
    wins = sum(weight[r["status"]][0] for r in rows if r["status"] in weight)
    decided = sum(weight[r["status"]][1] for r in rows if r["status"] in weight)
    return wins, decided


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    return "" if value is None else f"{value:g}"
