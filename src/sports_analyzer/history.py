"""Historical results from football-data.co.uk CSV files (data/history/*.csv).

Those files are free, cover many seasons of the top leagues, and include closing
odds from several bookmakers, which makes them ideal for (a) seeding the model
with last season and (b) backtesting. Column names follow the site's notes:
Date, HomeTeam, AwayTeam, FTHG, FTAG, B365H/D/A, PSH/PSD/PSA (Pinnacle; gone from
2026/27, where BFEH/BFED/BFEA = Betfair Exchange is the sharpest),
B365>2.5, B365<2.5, AHh (Asian handicap line, home view), B365AHH, B365AHA.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
                        # Sharpest available first. The 2026/27 files dropped Pinnacle;
                        # Betfair Exchange is the next sharpest, Bet365 the last resort.
                        h2h=_triple(row, ("PSH", "PSD", "PSA"))
                        or _triple(row, ("BFEH", "BFED", "BFEA"))
                        or _triple(row, ("B365H", "B365D", "B365A")),
                        over25=_pair(row, ("P>2.5", "P<2.5"))
                        or _pair(row, ("BFE>2.5", "BFE<2.5"))
                        or _pair(row, ("B365>2.5", "B365<2.5")),
                        ah_line=_float(row.get("AHh")),
                        ah=_pair(row, ("PAHH", "PAHA"))
                        or _pair(row, ("BFEAHH", "BFEAHA"))
                        or _pair(row, ("B365AHH", "B365AHA")),
                        h2h_close=_triple(row, ("PSCH", "PSCD", "PSCA"))
                        or _triple(row, ("BFECH", "BFECD", "BFECA"))
                        or _triple(row, ("B365CH", "B365CD", "B365CA")),
                        over25_close=_pair(row, ("PC>2.5", "PC<2.5"))
                        or _pair(row, ("BFEC>2.5", "BFEC<2.5"))
                        or _pair(row, ("B365C>2.5", "B365C<2.5")),
                        h2h_max=_triple(row, ("MaxH", "MaxD", "MaxA")),
                    )
                )
            except (KeyError, ValueError) as exc:
                log.debug("skipping row in %s: %s", path.name, exc)
    return matches


@dataclass(frozen=True)
class MatchStats:
    """What happened on the pitch, from the same CSVs (HxG/AxG appear from 2026/27 on).

    The post-match review uses these to separate "the model misread the match" from
    "the model read it right and the finishing went the other way".
    """

    day: date
    home: str  # canonical key
    away: str
    home_goals: int
    away_goals: int
    ht_home: int | None = None
    ht_away: int | None = None
    xg_home: float | None = None
    xg_away: float | None = None
    shots_home: int | None = None
    shots_away: int | None = None
    sot_home: int | None = None
    sot_away: int | None = None
    corners_home: int | None = None
    corners_away: int | None = None
    yellow_home: int | None = None
    yellow_away: int | None = None
    red_home: int | None = None
    red_away: int | None = None


def read_stats(path: Path) -> list[MatchStats]:
    """Match statistics from one football-data.co.uk CSV; missing columns stay None."""
    out: list[MatchStats] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                h, a = row["FTHG"], row["FTAG"]
                if h == "" or a == "":
                    continue
                out.append(
                    MatchStats(
                        day=_parse_date(row["Date"]),
                        home=canonical(row["HomeTeam"]),
                        away=canonical(row["AwayTeam"]),
                        home_goals=int(float(h)),
                        away_goals=int(float(a)),
                        ht_home=_int(row.get("HTHG")),
                        ht_away=_int(row.get("HTAG")),
                        xg_home=_float(row.get("HxG")),
                        xg_away=_float(row.get("AxG")),
                        shots_home=_int(row.get("HS")),
                        shots_away=_int(row.get("AS")),
                        sot_home=_int(row.get("HST")),
                        sot_away=_int(row.get("AST")),
                        corners_home=_int(row.get("HC")),
                        corners_away=_int(row.get("AC")),
                        yellow_home=_int(row.get("HY")),
                        yellow_away=_int(row.get("AY")),
                        red_home=_int(row.get("HR")),
                        red_away=_int(row.get("AR")),
                    )
                )
            except (KeyError, ValueError) as exc:
                log.debug("skipping stats row in %s: %s", path.name, exc)
    return out


def load_stats(history_dir: Path, fdcouk_codes: list[str]) -> list[MatchStats]:
    out: list[MatchStats] = []
    for code in fdcouk_codes:
        for path in sorted(history_dir.glob(f"{code}_*.csv")):
            out.extend(read_stats(path))
    return out


def find_stats(
    stats: list[MatchStats], home_key: str, away_key: str, day: date, slack_days: int = 1
) -> MatchStats | None:
    """The stats row for one fixture: same teams, date within a day (CSV dates are local)."""
    for st in stats:
        if (
            st.home == home_key
            and st.away == away_key
            and abs(st.day - day) <= timedelta(days=slack_days)
        ):
            return st
    return None


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


def _int(value: str | None) -> int | None:
    f = _float(value)
    return int(f) if f is not None else None


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
