"""data/reviews/*.csv: one row per finished match, kept forever.

The columns are fixed and flat so the files open cleanly in a spreadsheet: this is
the raw material for deciding, months from now, which kinds of model leans to trust.
Rows of the last two weeks are rebuilt on every run (late statistics fill in), older
rows are left as they are.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

FOOTBALL_COLUMNS = [
    "match_id",
    "competition",
    "competition_name",
    "kickoff_utc",
    "kickoff_local",
    "home",
    "away",
    # pre-match: model (last card before kickoff)
    "model_ok",
    "pooled",
    "model_note",
    "model_h",
    "model_d",
    "model_a",
    "model_xg_h",
    "model_xg_a",
    "model_over25",
    "most_likely",
    # pre-match: market fair probabilities, first and last snapshot before kickoff
    "open_h",
    "open_d",
    "open_a",
    "open_at",
    "close_h",
    "close_d",
    "close_a",
    "close_at",
    "mkt_over25",
    # 運彩 1X2 prices
    "tw_h",
    "tw_d",
    "tw_a",
    "tw_at",
    # the lean (模型分歧 signal, or the model's 1X2 lean)
    "lean_source",
    "lean_key",
    "lean_line",
    "lean_label",
    "lean_model_p",
    "lean_open_p",
    "lean_close_p",
    "lean_price",
    # result and statistics
    "ft_h",
    "ft_a",
    "ht_h",
    "ht_a",
    "outcome",
    "xg_h",
    "xg_a",
    "shots_h",
    "shots_a",
    "sot_h",
    "sot_a",
    "corners_h",
    "corners_a",
    "yellow_h",
    "yellow_a",
    "red_h",
    "red_a",
    "stats_source",
    # assessment (recomputed from the columns above)
    "model_ll",
    "market_ll",
    "lean_result",
    "lean_profit",
    "move_pp",
    "move_sign",
    "stats_sign",
    "stats_basis",
    "verdict",
    "reviewed_at",
]

MLB_COLUMNS = [
    "game_pk",
    "kickoff_utc",
    "local",
    "home",
    "away",
    "home_probable",
    "away_probable",
    "open_home",
    "open_away",
    "open_at",
    "close_home",
    "close_away",
    "close_at",
    "rl_line",
    "rl_home",
    "rl_away",
    "total_line",
    "over_p",
    "t10_g",
    "t10_fav",
    "t10_signal",
    "t10_threshold",
    "tw_home",
    "tw_away",
    "tw_at",
    "final_h",
    "final_a",
    "hits_h",
    "hits_a",
    "errors_h",
    "errors_a",
    "innings_h",
    "innings_a",
    "sp_h",
    "sp_h_ip",
    "sp_h_r",
    "sp_h_er",
    "sp_h_h",
    "sp_h_bb",
    "sp_h_k",
    "sp_a",
    "sp_a_ip",
    "sp_a_r",
    "sp_a_er",
    "sp_a_h",
    "sp_a_bb",
    "sp_a_k",
    "winner",
    "market_ll",
    "fav",
    "fav_won",
    "fav_cover",
    "t10_result",
    "verdict",
    "reviewed_at",
]


def read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write(path: Path, rows: Iterable[Mapping[str, Any]], columns: list[str], key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: (str(r.get("kickoff_utc") or ""), str(r.get(key))))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in ordered:
            writer.writerow({c: _cell(row.get(c)) for c in columns})


def upsert(
    path: Path, rows: Iterable[Mapping[str, Any]], columns: list[str], key: str
) -> tuple[int, int]:
    """Insert new rows and replace existing ones with the same key. Returns (added, updated)."""
    existing = {r[key]: r for r in read(path)}
    added = updated = 0
    for row in rows:
        k = str(row[key])
        if k in existing:
            updated += 1
        else:
            added += 1
        existing[k] = {c: _cell(row.get(c)) for c in columns}
    write(path, existing.values(), columns, key)
    return added, updated


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value == value else ""
    return str(value)
