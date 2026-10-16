"""odds-api.io v3 client: the owner's free key (two bookmakers, 100 requests an hour).

It is only a sensor (sensor.py): the two bookmakers' match-winner prices, looked at
every ten minutes, tell WHEN a match moves; the fair price 運彩 is judged against still
comes from The Odds API's many-bookmaker consensus. Its data never goes into the repo
or the public pages (its terms do not allow redistribution); only what it triggered
does. The key travels as the apiKey query parameter, so URLs are never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..http import Session

log = logging.getLogger(__name__)

BASE_URL = "https://api.odds-api.io/v3"
# /odds/multi takes at most this many events per request.
MAX_IDS = 10

# Our competitions by their odds-api.io (sport, league slug). MLB has two slugs there:
# the regular season and the playoffs.
LEAGUES: dict[str, tuple[str, tuple[str, ...]]] = {
    "PL": ("football", ("england-premier-league",)),
    "PD": ("football", ("spain-laliga",)),
    "BL1": ("football", ("germany-bundesliga",)),
    "SA": ("football", ("italy-serie-a",)),
    "FL1": ("football", ("france-ligue-1",)),
    "CL": ("football", ("international-clubs-uefa-champions-league",)),
    "MLB": ("baseball", ("usa-mlb", "usa-mlb-playoffs")),
}
# The match-winner market's name in the odds answer (1X2 for football).
MONEYLINE = "ML"
# More than a busy two days of any league (the API caps an answer at 5000).
EVENTS_LIMIT = 200


@dataclass(frozen=True)
class IoEvent:
    id: str
    league: str  # odds-api.io slug
    start: datetime
    home: str
    away: str


@dataclass(frozen=True)
class IoPrices:
    """One event's match-winner prices per bookmaker: book -> side -> decimal price."""

    event_id: str
    books: dict[str, dict[str, float]] = field(default_factory=dict)


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_events(payload: Any, league: str) -> list[IoEvent]:
    """The /events answer as IoEvents; malformed entries are skipped."""
    out = []
    for item in payload if isinstance(payload, list) else []:
        start = _time(item.get("date")) if isinstance(item, dict) else None
        if start is None or start.tzinfo is None or not item.get("id"):
            continue
        out.append(
            IoEvent(str(item["id"]), league, start, str(item.get("home")), str(item.get("away")))
        )
    return out


def parse_odds(payload: Any, books: Iterable[str]) -> list[IoPrices]:
    """The /odds/multi answer: per event, each wanted bookmaker's match-winner prices.

    Only the exact names asked for count: the answer can also carry variants of a book
    (e.g. "Bet365 (no latency)"), which would weigh one bookmaker twice."""
    wanted = set(books)
    out = []
    for event in payload if isinstance(payload, list) else []:
        if not isinstance(event, dict) or not event.get("id"):
            continue
        prices: dict[str, dict[str, float]] = {}
        for book, markets in (event.get("bookmakers") or {}).items():
            if book not in wanted or not isinstance(markets, list):
                continue
            ml = next(
                (m for m in markets if isinstance(m, dict) and m.get("name") == MONEYLINE), None
            )
            quotes = (ml or {}).get("odds") or []
            if not quotes or not isinstance(quotes[0], dict):
                continue
            sides = {}
            for side in ("home", "draw", "away"):
                try:
                    price = float(quotes[0][side])
                except (KeyError, TypeError, ValueError):
                    continue
                if price > 1.0:
                    sides[side] = price
            if sides:
                prices[book] = sides
        out.append(IoPrices(str(event["id"]), prices))
    return out


class OddsIoClient:
    """The three calls the sensor needs. `requests` counts what was sent (retries too)."""

    def __init__(self, key: str, session: Session | None = None) -> None:
        self._key = key
        # No retries: a 429 can ask for a wait of most of an hour, and the next round is
        # ten minutes away anyway (a failed round stops; nothing is lost but one reading).
        self.session = session or Session(max_retries=0, timeout=30.0)

    @property
    def requests(self) -> int:
        return self.session.calls

    @property
    def remaining_this_hour(self) -> int | None:
        return self.session.quota.remaining

    def _get(self, path: str, **params: Any) -> Any:
        return self.session.get_json(f"{BASE_URL}{path}", params={"apiKey": self._key, **params})

    def selected_books(self) -> list[str]:
        """The bookmakers chosen in the odds-api.io dashboard (two on the free plan)."""
        raw = self._get("/bookmakers/selected")
        if isinstance(raw, dict):
            raw = next((v for v in raw.values() if isinstance(v, list)), [])
        return [str(b.get("name", "")) if isinstance(b, dict) else str(b) for b in raw or []]

    def events(self, sport: str, league: str, start: datetime, end: datetime) -> list[IoEvent]:
        """One league's events not started yet that kick off between `start` and `end`.
        The answer is not in date order, so the window, not the limit, picks them."""
        payload = self._get(
            "/events",
            sport=sport,
            league=league,
            status="pending",
            limit=EVENTS_LIMIT,
            **{"from": _rfc3339(start), "to": _rfc3339(end)},
        )
        return parse_events(payload, league)

    def odds(self, event_ids: list[str], books: list[str]) -> list[IoPrices]:
        """Match-winner prices of up to MAX_IDS events from the given bookmakers."""
        if not event_ids or len(event_ids) > MAX_IDS:
            raise ValueError(f"1 to {MAX_IDS} events per request")
        payload = self._get("/odds/multi", eventIds=",".join(event_ids), bookmakers=",".join(books))
        return parse_odds(payload, books)
