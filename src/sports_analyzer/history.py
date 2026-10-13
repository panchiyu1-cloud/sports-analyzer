"""Historical results from football-data.co.uk CSV files (data/history/*.csv).

Those files are free, cover many seasons of the top leagues, and include closing
odds from several bookmakers, which makes them ideal for (a) seeding the model
with last season and (b) backtesting. Column names follow the site's notes:
Date, HomeTeam, AwayTeam, FTHG, FTAG, B365H/D/A, PSH/PSD/PSA (Pinnacle),
B365>2.5, B365<2.5, AHh (Asian handicap line, home view), B365AHH, B365AHA.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .models.dixon_coles import MatchResult
from .teams import canonical

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoricalMatch:
    day: date
    home: str
    away: str
    home_goals: int
    away_goals: int
    # Pre-match odds, collected Friday/Tuesday afternoon (None when the column is missing)
    h2h: tuple[float, float, float] | None  # home, draw, away (Pinnacle, else Bet365)
    over25: tuple[float, float] | None  # over, under at 2.5
    ah_line: float | None
    ah: tuple[float, float] | None  # home, away
    # Closing odds (same bookmaker order) and best pre-match price across books
    h2h_close: tuple[float, float, float] | None = None
    over25_close: tuple[float, float] | None = None
    h2h_max: tuple[float, float, float] | None = None

    @property
    def result(self) -> MatchResult:
        return MatchResult(self.day, self.home, self.away, self.home_goals, self.away_goals)


def read_csv(path: Path) -> list[HistoricalMatch]:
    """Parse one football-data.co.uk CSV. Rows without a full-time score are skipped."""
    matches: list[HistoricalMatch] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                day = _parse_date(row["Date"])
                h, a = row["FTHG"], row["FTAG"]
                if h == "" or a == "":
                    continue
                matches.append(
                    HistoricalMatch(
                        day=day,
                        home=canonical(row["HomeTeam"]),
                        away=canonical(row["AwayTeam"]),
                        home_goals=int(float(h)),
                        away_goals=int(float(a)),
                        h2h=_triple(row, ("PSH", "PSD", "PSA"))
                        or _triple(row, ("B365H", "B365D", "B365A")),
                        over25=_pair(row, ("P>2.5", "P<2.5"))
                        or _pair(row, ("B365>2.5", "B365<2.5")),
                        ah_line=_float(row.get("AHh")),
                        ah=_pair(row, ("PAHH", "PAHA")) or _pair(row, ("B365AHH", "B365AHA")),
                        h2h_close=_triple(row, ("PSCH", "PSCD", "PSCA"))
                        or _triple(row, ("B365CH", "B365CD", "B365CA")),
                        over25_close=_pair(row, ("PC>2.5", "PC<2.5"))
                        or _pair(row, ("B365C>2.5", "B365C<2.5")),
                        h2h_max=_triple(row, ("MaxH", "MaxD", "MaxA")),
                    )
                )
            except (KeyError, ValueError) as exc:
                log.debug("skipping row in %s: %s", path.name, exc)
    return matches


def load_history(history_dir: Path, fdcouk_code: str) -> list[HistoricalMatch]:
    """All CSVs for one league code, e.g. E0_2425.csv and E0_2526.csv, oldest first."""
    matches: list[HistoricalMatch] = []
    for path in sorted(history_dir.glob(f"{fdcouk_code}_*.csv")):
        matches.extend(read_csv(path))
    matches.sort(key=lambda m: m.day)
    return matches


# ----------------------------------------------------------------------- helpers
def _parse_date(value: str) -> date:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {value!r}")


def _float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _pair(row: dict[str, str], cols: tuple[str, str]) -> tuple[float, float] | None:
    a, b = _float(row.get(cols[0])), _float(row.get(cols[1]))
    if a is None or b is None or a <= 1 or b <= 1:
        return None
    return a, b


def _triple(row: dict[str, str], cols: tuple[str, str, str]) -> tuple[float, float, float] | None:
    vals = [_float(row.get(c)) for c in cols]
    if any(v is None or v <= 1 for v in vals):
        return None
    return vals[0], vals[1], vals[2]  # type: ignore[return-value]
