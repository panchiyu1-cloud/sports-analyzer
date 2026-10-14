"""MLB Stats API (statsapi.mlb.com) client — official, free, no key.

Phase 3 builds the baseball model on top of this. For now it fetches the day's
schedule with probable pitchers so the MLB card skeleton can be rendered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from ..http import Session

log = logging.getLogger(__name__)

BASE_URL = "https://statsapi.mlb.com/api/v1"


@dataclass(frozen=True)
class MlbGame:
    game_pk: int
    game_date: datetime
    status: str
    home: str
    away: str
    home_probable: str | None
    away_probable: str | None
    venue: str | None
    home_score: int | None = None
    away_score: int | None = None

    @property
    def is_final(self) -> bool:
        return (
            self.status.startswith("Final")
            and self.home_score is not None
            and self.away_score is not None
        )


def parse_schedule(payload: dict[str, Any]) -> list[MlbGame]:
    games: list[MlbGame] = []
    for day in payload.get("dates", []):
        for raw in day.get("games", []):
            try:
                teams = raw["teams"]
                games.append(
                    MlbGame(
                        game_pk=int(raw["gamePk"]),
                        game_date=datetime.fromisoformat(
                            str(raw["gameDate"]).replace("Z", "+00:00")
                        ).astimezone(UTC),
                        status=str((raw.get("status") or {}).get("detailedState") or ""),
                        home=str(teams["home"]["team"]["name"]),
                        away=str(teams["away"]["team"]["name"]),
                        home_probable=_probable(teams["home"]),
                        away_probable=_probable(teams["away"]),
                        venue=(raw.get("venue") or {}).get("name"),
                        home_score=_score(teams["home"]),
                        away_score=_score(teams["away"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping malformed MLB game: %s", exc)
    return games


def _probable(side: dict[str, Any]) -> str | None:
    pitcher = side.get("probablePitcher") or {}
    return pitcher.get("fullName")


def _score(side: dict[str, Any]) -> int | None:
    value = side.get("score")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PitcherLine:
    """One pitcher's line from the box score. `ip` keeps baseball notation ("5.2" = 5 2/3)."""

    name: str
    ip: str
    hits: int | None = None
    runs: int | None = None
    earned_runs: int | None = None
    walks: int | None = None
    strikeouts: int | None = None
    pitches: int | None = None

    @property
    def outs(self) -> int:
        whole, _, part = self.ip.partition(".")
        try:
            return int(whole or 0) * 3 + int(part or 0)
        except ValueError:
            return 0


@dataclass(frozen=True)
class BoxScore:
    """Inning-by-inning runs, team hits/errors and both starting pitchers' lines."""

    innings_home: tuple[int | None, ...]  # None = the home side did not bat (the "x")
    innings_away: tuple[int | None, ...]
    hits_home: int | None
    hits_away: int | None
    errors_home: int | None
    errors_away: int | None
    starter_home: PitcherLine | None
    starter_away: PitcherLine | None


def parse_box(linescore: dict[str, Any], boxscore: dict[str, Any]) -> BoxScore:
    """Combine `/game/{pk}/linescore` and `/game/{pk}/boxscore` (shapes validated loosely)."""
    innings = linescore.get("innings") or []
    teams = linescore.get("teams") or {}

    def inning_runs(side: str) -> tuple[int | None, ...]:
        return tuple(_int((inn.get(side) or {}).get("runs")) for inn in innings)

    return BoxScore(
        innings_home=inning_runs("home"),
        innings_away=inning_runs("away"),
        hits_home=_int((teams.get("home") or {}).get("hits")),
        hits_away=_int((teams.get("away") or {}).get("hits")),
        errors_home=_int((teams.get("home") or {}).get("errors")),
        errors_away=_int((teams.get("away") or {}).get("errors")),
        starter_home=_starter((boxscore.get("teams") or {}).get("home") or {}),
        starter_away=_starter((boxscore.get("teams") or {}).get("away") or {}),
    )


def _starter(team: dict[str, Any]) -> PitcherLine | None:
    """The first pitcher listed for a team is its starter."""
    pitchers = team.get("pitchers") or []
    if not pitchers:
        return None
    player = (team.get("players") or {}).get(f"ID{pitchers[0]}") or {}
    stats = (player.get("stats") or {}).get("pitching") or {}
    name = (player.get("person") or {}).get("fullName")
    if not name or "inningsPitched" not in stats:
        return None
    return PitcherLine(
        name=str(name),
        ip=str(stats.get("inningsPitched")),
        hits=_int(stats.get("hits")),
        runs=_int(stats.get("runs")),
        earned_runs=_int(stats.get("earnedRuns")),
        walks=_int(stats.get("baseOnBalls")),
        strikeouts=_int(stats.get("strikeOuts")),
        pitches=_int(stats.get("numberOfPitches")),
    )


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


class MlbClient:
    def __init__(self, session: Session | None = None) -> None:
        self.session = session or Session(min_interval=0.5)

    def schedule(self, day: date) -> list[MlbGame]:
        payload = self.session.get_json(
            f"{BASE_URL}/schedule",
            params={
                "sportId": 1,
                "date": day.isoformat(),
                "hydrate": "probablePitcher,team,venue",
            },
        )
        return parse_schedule(payload)

    def box(self, game_pk: int) -> BoxScore | None:
        """Line score + box score of one finished game (two small calls)."""
        try:
            line = self.session.get_json(f"{BASE_URL}/game/{game_pk}/linescore")
            box = self.session.get_json(f"{BASE_URL}/game/{game_pk}/boxscore")
        except Exception as exc:  # a missing box score must not stop the review
            log.warning("mlb: box score for %s unavailable: %s", game_pk, exc)
            return None
        return parse_box(line, box)
