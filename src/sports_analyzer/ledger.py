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


def grade(selection_key: str, line: float | None, hg: int, ag: int) -> str:
    """Win/loss/push for one selection given the final score."""
    if selection_key == "home":
        return WIN if hg > ag else LOSS
    if selection_key == "away":
        return WIN if ag > hg else LOSS
    if selection_key == "draw":
        return WIN if hg == ag else LOSS
    if selection_key in ("over", "under") and line is not None:
        total = hg + ag
        if total == line:
            return PUSH
        return WIN if (total > line) == (selection_key == "over") else LOSS
    if selection_key in ("spread_home", "spread_away") and line is not None:
        margin = hg - ag + line  # home view
        if selection_key == "spread_away":
            margin = -margin
        # Quarter lines settle half/half; report the net sign as win/loss/push for simplicity.
        if abs(margin) < 1e-9:
            return PUSH
        return WIN if margin > 0 else LOSS
    return VOID


def profit(outcome: str, price: float, stake: float = 1.0) -> float:
    if outcome == WIN:
        return stake * (price - 1.0)
    if outcome == LOSS:
        return -stake
    return 0.0


def summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Headline numbers for the page: settled count, hit rate, units, ROI."""
    settled = [r for r in rows if r["status"] in (WIN, LOSS, PUSH)]
    wins = sum(1 for r in settled if r["status"] == WIN)
    decided = [r for r in settled if r["status"] in (WIN, LOSS)]
    units = sum(_float(r["profit"]) or 0.0 for r in settled)
    return {
        "pending": sum(1 for r in rows if r["status"] == PENDING),
        "settled": len(settled),
        "wins": wins,
        "losses": len(decided) - wins,
        "hit_rate": round(wins / len(decided), 3) if decided else None,
        "units": round(units, 2),
        "roi": round(units / len(decided), 3) if decided else None,
    }


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    return "" if value is None else f"{value:g}"
