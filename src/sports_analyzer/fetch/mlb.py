"""MLB Stats API (statsapi.mlb.com) client — official, free, no key.

Phase 3 builds the baseball model on top of this. For now it fetches the day's
schedule with probable pitchers so the MLB card skeleton can be rendered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
                        ).astimezone(timezone.utc),
                        status=str((raw.get("status") or {}).get("detailedState") or ""),
                        home=str(teams["home"]["team"]["name"]),
                        away=str(teams["away"]["team"]["name"]),
                        home_probable=_probable(teams["home"]),
                        away_probable=_probable(teams["away"]),
                        venue=(raw.get("venue") or {}).get("name"),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping malformed MLB game: %s", exc)
    return games


def _probable(side: dict[str, Any]) -> str | None:
    pitcher = side.get("probablePitcher") or {}
    return pitcher.get("fullName")


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
