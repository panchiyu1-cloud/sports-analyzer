"""Flatten the SportsBookReview MLB odds dataset (JSON, ~76 MB) into a small CSV.

Source: github.com/ArnavSaraogi/mlb-odds-scraper, release "dataset" (2021-03-20 to
2025-08-16): per game and sportsbook, the opening and closing ("current") moneyline,
run line and totals, in American odds. Used by scripts/backtest_t10.py.

One row per (game, sportsbook). Odds stay American; the backtest converts them.

Usage: python scripts/mlb_odds_to_csv.py <dataset.json> <out.csv.gz>
"""

from __future__ import annotations

import csv
import gzip
import json
import sys
from typing import Any

COLUMNS = [
    "date",
    "start_utc",
    "game_type",
    "status",
    "away",
    "home",
    "away_score",
    "home_score",
    "book",
    "ml_open_home",
    "ml_open_away",
    "ml_close_home",
    "ml_close_away",
    "rl_open_home_spread",
    "rl_open_home",
    "rl_open_away",
    "rl_close_home_spread",
    "rl_close_home",
    "rl_close_away",
    "tot_open_line",
    "tot_open_over",
    "tot_open_under",
    "tot_close_line",
    "tot_close_over",
    "tot_close_under",
]


def _by_book(entries: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {str(e.get("sportsbook")): e for e in entries or [] if isinstance(e, dict)}


def _get(entry: dict[str, Any] | None, line: str, key: str) -> Any:
    return ((entry or {}).get(line) or {}).get(key, "")


def rows(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for date, games in sorted(data.items()):
        for game in games or []:
            view = game.get("gameView") or {}
            odds = game.get("odds") or {}
            ml, rl, tot = (_by_book(odds.get(k)) for k in ("moneyline", "pointspread", "totals"))
            for book in sorted(set(ml) | set(rl) | set(tot)):
                m, r, t = ml.get(book), rl.get(book), tot.get(book)
                out.append(
                    {
                        "date": date,
                        "start_utc": view.get("startDate", ""),
                        "game_type": view.get("gameType", ""),
                        "status": view.get("gameStatusText", ""),
                        "away": (view.get("awayTeam") or {}).get("fullName", ""),
                        "home": (view.get("homeTeam") or {}).get("fullName", ""),
                        "away_score": view.get("awayTeamScore", ""),
                        "home_score": view.get("homeTeamScore", ""),
                        "book": book,
                        "ml_open_home": _get(m, "openingLine", "homeOdds"),
                        "ml_open_away": _get(m, "openingLine", "awayOdds"),
                        "ml_close_home": _get(m, "currentLine", "homeOdds"),
                        "ml_close_away": _get(m, "currentLine", "awayOdds"),
                        "rl_open_home_spread": _get(r, "openingLine", "homeSpread"),
                        "rl_open_home": _get(r, "openingLine", "homeOdds"),
                        "rl_open_away": _get(r, "openingLine", "awayOdds"),
                        "rl_close_home_spread": _get(r, "currentLine", "homeSpread"),
                        "rl_close_home": _get(r, "currentLine", "homeOdds"),
                        "rl_close_away": _get(r, "currentLine", "awayOdds"),
                        "tot_open_line": _get(t, "openingLine", "total"),
                        "tot_open_over": _get(t, "openingLine", "overOdds"),
                        "tot_open_under": _get(t, "openingLine", "underOdds"),
                        "tot_close_line": _get(t, "currentLine", "total"),
                        "tot_close_over": _get(t, "currentLine", "overOdds"),
                        "tot_close_under": _get(t, "currentLine", "underOdds"),
                    }
                )
    return out


def main(src: str, dst: str) -> None:
    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)
    flat = rows(data)
    with gzip.open(dst, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(flat)
    games = {(r["date"], r["away"], r["home"], r["start_utc"]) for r in flat}
    print(f"{len(games)} games, {len(flat)} game-book rows -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
