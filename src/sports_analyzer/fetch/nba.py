"""NBA schedule and results from ESPN's public scoreboard (no key, no quota headers).

    https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD

One call per US date gives every game of that date with its tip-off time, status and,
once played, the score and the points per quarter. It is the same role the MLB Stats
API plays for baseball: fixtures for the cards, finals for the ledgers and the review.
The feed is public but not a documented contract, so every field is read defensively
and a malformed game is skipped, never fatal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from ..http import Session

log = logging.getLogger(__name__)

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
# ESPN's status states: pre (not started), in (live), post (finished).
FINAL_STATE = "post"


@dataclass(frozen=True)
class NbaGame:
    game_id: str
    tipoff: datetime  # aware, UTC
    status: str  # ESPN's description: Scheduled / In Progress / Final ...
    state: str  # pre | in | post
    home: str
    away: str
    home_abbr: str = ""
    away_abbr: str = ""
    venue: str | None = None
    home_score: int | None = None
    away_score: int | None = None
    # Points per quarter (overtime periods appended), empty until the game is played.
    home_quarters: tuple[int, ...] = ()
    away_quarters: tuple[int, ...] = ()
    neutral_site: bool = False

    @property
    def is_final(self) -> bool:
        return (
            self.state == FINAL_STATE
            and self.home_score is not None
            and self.away_score is not None
            and self.home_score != self.away_score  # no ties in basketball
        )


def parse_scoreboard(payload: Any) -> list[NbaGame]:
    """The `events` of one scoreboard answer as NbaGames (malformed ones are skipped)."""
    games: list[NbaGame] = []
    events = payload.get("events") if isinstance(payload, dict) else None
    for raw in events or []:
        try:
            game = _parse_event(raw)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            log.warning("Skipping malformed NBA game: %s", type(exc).__name__)
            continue
        if game is not None:
            games.append(game)
    return sorted(games, key=lambda g: g.tipoff)


def _parse_event(raw: dict[str, Any]) -> NbaGame | None:
    competition = (raw.get("competitions") or [{}])[0]
    sides = {}
    for c in competition.get("competitors") or []:
        if c.get("homeAway") in ("home", "away"):
            sides[c["homeAway"]] = c
    if "home" not in sides or "away" not in sides:
        return None
    status = (competition.get("status") or raw.get("status") or {}).get("type") or {}
    state = str(status.get("state") or "")
    home, away = sides["home"], sides["away"]
    return NbaGame(
        game_id=str(raw["id"]),
        tipoff=_parse_iso(str(competition.get("date") or raw["date"])),
        status=str(status.get("description") or status.get("detail") or ""),
        state=state,
        home=str(home["team"]["displayName"]),
        away=str(away["team"]["displayName"]),
        home_abbr=str(home["team"].get("abbreviation") or ""),
        away_abbr=str(away["team"].get("abbreviation") or ""),
        venue=(competition.get("venue") or {}).get("fullName"),
        home_score=_score(home) if state != "pre" else None,
        away_score=_score(away) if state != "pre" else None,
        home_quarters=_quarters(home),
        away_quarters=_quarters(away),
        neutral_site=bool(competition.get("neutralSite")),
    )


def _score(side: dict[str, Any]) -> int | None:
    value = side.get("score")
    if isinstance(value, dict):  # some ESPN answers nest it: {"value": 112.0, ...}
        value = value.get("value")
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _quarters(side: dict[str, Any]) -> tuple[int, ...]:
    out = []
    for q in side.get("linescores") or []:
        try:
            out.append(int(float(q.get("value"))))
        except (TypeError, ValueError):
            return ()
    return tuple(out)


def _parse_iso(value: str) -> datetime:
    # ESPN writes "2026-10-26T23:30Z": minutes only, a literal Z.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class NbaClient:
    def __init__(self, session: Session | None = None) -> None:
        self.session = session or Session(min_interval=0.5)

    def scoreboard(self, day: date) -> list[NbaGame]:
        """Every game of one US date (ESPN's day, US Eastern), played or not."""
        payload = self.session.get_json(
            f"{BASE_URL}/scoreboard", params={"dates": day.strftime("%Y%m%d")}
        )
        return parse_scoreboard(payload)
