"""football-data.org v4 client: fixtures, results and standings.

Free tier: 12 competitions, 10 calls/minute, delayed scores. We only need
pre-match data, so the delay does not matter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..http import Session
from ..teams import canonical

log = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"
FINISHED = "FINISHED"
UPCOMING_STATUSES = ("SCHEDULED", "TIMED")


@dataclass(frozen=True)
class Match:
    """One fixture or result. Team names are kept raw; `home_key`/`away_key` are canonical."""

    id: int
    competition: str
    utc_date: datetime
    status: str
    matchday: int | None
    home: str
    away: str
    home_goals: int | None = None
    away_goals: int | None = None
    # Half-time score and how the match ended (REGULAR / EXTRA_TIME / PENALTY_SHOOTOUT).
    home_ht: int | None = None
    away_ht: int | None = None
    duration: str = "REGULAR"

    @property
    def home_key(self) -> str:
        return canonical(self.home)

    @property
    def away_key(self) -> str:
        return canonical(self.away)

    @property
    def is_finished(self) -> bool:
        return (
            self.status == FINISHED and self.home_goals is not None and self.away_goals is not None
        )


@dataclass(frozen=True)
class TableRow:
    team: str
    position: int
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    points: int
    form: str  # e.g. "W,W,D,L,W" (most recent last on football-data.org)

    @property
    def team_key(self) -> str:
        return canonical(self.team)


# ----------------------------------------------------------------------- parsing
def parse_matches(payload: dict[str, Any]) -> list[Match]:
    """Turn a `/competitions/{code}/matches` response into Match records.

    Defensive on purpose: a missing field skips the match instead of crashing the run.
    """
    matches: list[Match] = []
    for raw in payload.get("matches", []):
        try:
            score = raw.get("score") or {}
            # Markets settle on 90 minutes. For a cup tie that went to extra time the API
            # adds `regularTime`; `fullTime` then includes extra time (and penalties).
            full_time = score.get("regularTime") or score.get("fullTime") or {}
            half_time = score.get("halfTime") or {}
            matches.append(
                Match(
                    id=int(raw["id"]),
                    competition=str((raw.get("competition") or {}).get("code") or ""),
                    utc_date=_parse_utc(raw["utcDate"]),
                    status=str(raw.get("status") or ""),
                    matchday=raw.get("matchday"),
                    home=str(raw["homeTeam"]["name"]),
                    away=str(raw["awayTeam"]["name"]),
                    home_goals=full_time.get("home"),
                    away_goals=full_time.get("away"),
                    home_ht=half_time.get("home"),
                    away_ht=half_time.get("away"),
                    duration=str(score.get("duration") or "REGULAR"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed match record: %s", exc)
    return matches


def parse_standings(payload: dict[str, Any]) -> list[TableRow]:
    """Extract the TOTAL table from a `/competitions/{code}/standings` response."""
    for block in payload.get("standings", []):
        if block.get("type") != "TOTAL":
            continue
        rows: list[TableRow] = []
        for raw in block.get("table", []):
            try:
                rows.append(
                    TableRow(
                        team=str(raw["team"]["name"]),
                        position=int(raw["position"]),
                        played=int(raw["playedGames"]),
                        won=int(raw["won"]),
                        drawn=int(raw["draw"]),
                        lost=int(raw["lost"]),
                        goals_for=int(raw["goalsFor"]),
                        goals_against=int(raw["goalsAgainst"]),
                        points=int(raw["points"]),
                        form=str(raw.get("form") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping malformed table row: %s", exc)
        return rows
    return []


def _parse_utc(value: str) -> datetime:
    # football-data.org uses "2026-10-17T14:00:00Z"
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


# ---------------------------------------------------------------------- fetching
class FootballDataClient:
    """Network side. Keeps one `Session` so the per-minute throttle is shared."""

    def __init__(self, token: str, session: Session | None = None) -> None:
        if not token:
            raise ValueError("football-data.org token is required")
        self._headers = {"X-Auth-Token": token}
        # 10 calls/minute => a 6s spacing never trips the limiter even without headers.
        self.session = session or Session(min_interval=6.0)

    def upcoming(self, code: str, start: date, days: int) -> list[Match]:
        """Fixtures for competition `code` from `start` for `days` days (inclusive)."""
        payload = self.session.get_json(
            f"{BASE_URL}/competitions/{code}/matches",
            params={
                "dateFrom": start.isoformat(),
                "dateTo": (start + timedelta(days=days)).isoformat(),
            },
            headers=self._headers,
        )
        return [m for m in parse_matches(payload) if m.status in UPCOMING_STATUSES]

    def results(self, code: str, season: int | None = None) -> list[Match]:
        """Finished matches of the current (or given) season."""
        params: dict[str, Any] = {"status": FINISHED}
        if season is not None:
            params["season"] = season
        payload = self.session.get_json(
            f"{BASE_URL}/competitions/{code}/matches", params=params, headers=self._headers
        )
        return [m for m in parse_matches(payload) if m.is_finished]

    def standings(self, code: str) -> list[TableRow]:
        payload = self.session.get_json(
            f"{BASE_URL}/competitions/{code}/standings", headers=self._headers
        )
        return parse_standings(payload)
