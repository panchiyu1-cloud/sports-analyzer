"""The Odds API v4 client.

Free tier: 500 credits/month. One request costs (number of markets) x (number of
regions), so `h2h,spreads,totals` in one region = 3 credits. The pipeline only
calls this for competitions that actually have fixtures in the window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..http import Session
from ..teams import canonical

log = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
DEFAULT_MARKETS = ("h2h", "spreads", "totals")


@dataclass(frozen=True)
class SpreadLine:
    """A handicap line quoted by one bookmaker. `point` is from the HOME team's view."""

    point: float
    home_price: float
    away_price: float


@dataclass(frozen=True)
class TotalLine:
    point: float
    over_price: float
    under_price: float


@dataclass(frozen=True)
class BookOdds:
    book: str
    # h2h decimal prices keyed "home" / "away" / "draw" (draw absent for MLB)
    h2h: dict[str, float] = field(default_factory=dict)
    spreads: tuple[SpreadLine, ...] = ()
    totals: tuple[TotalLine, ...] = ()


@dataclass(frozen=True)
class OddsEvent:
    id: str
    sport_key: str
    commence_time: datetime
    home: str
    away: str
    books: tuple[BookOdds, ...] = ()

    @property
    def home_key(self) -> str:
        return canonical(self.home)

    @property
    def away_key(self) -> str:
        return canonical(self.away)


# ----------------------------------------------------------------------- parsing
def parse_events(payload: list[dict[str, Any]]) -> list[OddsEvent]:
    """Convert the raw `/sports/{key}/odds` array into OddsEvent records."""
    events: list[OddsEvent] = []
    for raw in payload:
        try:
            home = str(raw["home_team"])
            away = str(raw["away_team"])
            books = tuple(
                _parse_book(book, home, away) for book in raw.get("bookmakers", [])
            )
            events.append(
                OddsEvent(
                    id=str(raw["id"]),
                    sport_key=str(raw.get("sport_key") or ""),
                    commence_time=_parse_iso(str(raw["commence_time"])),
                    home=home,
                    away=away,
                    books=tuple(b for b in books if b.h2h or b.spreads or b.totals),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed odds event: %s", exc)
    return events


def _parse_book(raw: dict[str, Any], home: str, away: str) -> BookOdds:
    h2h: dict[str, float] = {}
    spreads: list[SpreadLine] = []
    totals: list[TotalLine] = []
    for market in raw.get("markets", []):
        key = market.get("key")
        outcomes = market.get("outcomes", [])
        if key == "h2h":
            for o in outcomes:
                side = _side(str(o["name"]), home, away)
                if side:
                    h2h[side] = float(o["price"])
        elif key == "spreads":
            # Outcomes come in pairs (home point p, away point -p). Group by |point|.
            by_point: dict[float, dict[str, float]] = {}
            for o in outcomes:
                side = _side(str(o["name"]), home, away)
                if not side or "point" not in o:
                    continue
                point = float(o["point"])
                home_point = point if side == "home" else -point
                by_point.setdefault(home_point, {})[side] = float(o["price"])
            for home_point, prices in by_point.items():
                if "home" in prices and "away" in prices:
                    spreads.append(SpreadLine(home_point, prices["home"], prices["away"]))
        elif key == "totals":
            by_point_t: dict[float, dict[str, float]] = {}
            for o in outcomes:
                if "point" not in o:
                    continue
                name = str(o["name"]).lower()
                if name in ("over", "under"):
                    by_point_t.setdefault(float(o["point"]), {})[name] = float(o["price"])
            for point, prices in by_point_t.items():
                if "over" in prices and "under" in prices:
                    totals.append(TotalLine(point, prices["over"], prices["under"]))
    return BookOdds(
        book=str(raw.get("key") or raw.get("title") or "?"),
        h2h=h2h,
        spreads=tuple(sorted(spreads, key=lambda s: s.point)),
        totals=tuple(sorted(totals, key=lambda t: t.point)),
    )


def _side(name: str, home: str, away: str) -> str | None:
    if name == home:
        return "home"
    if name == away:
        return "away"
    if name.lower() == "draw":
        return "draw"
    return None


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------- fetching
class OddsClient:
    def __init__(self, api_key: str, regions: str = "eu", session: Session | None = None) -> None:
        if not api_key:
            raise ValueError("The Odds API key is required")
        self._api_key = api_key
        self.regions = regions
        self.session = session or Session(min_interval=1.0)

    def credits_remaining(self) -> int | None:
        return self.session.quota.remaining

    def odds(
        self,
        sport_key: str,
        *,
        commence_from: datetime | None = None,
        commence_to: datetime | None = None,
        markets: tuple[str, ...] = DEFAULT_MARKETS,
    ) -> list[OddsEvent]:
        """Current odds for one sport. Costs len(markets) x regions credits."""
        params: dict[str, Any] = {
            "apiKey": self._api_key,
            "regions": self.regions,
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if commence_from:
            params["commenceTimeFrom"] = _iso_z(commence_from)
        if commence_to:
            params["commenceTimeTo"] = _iso_z(commence_to)
        payload = self.session.get_json(f"{BASE_URL}/sports/{sport_key}/odds", params=params)
        events = parse_events(payload if isinstance(payload, list) else [])
        log.info(
            "odds[%s]: %d events, credits remaining=%s (last cost=%s)",
            sport_key,
            len(events),
            self.session.quota.remaining,
            self.session.quota.last_cost,
        )
        return events


def _iso_z(value: datetime) -> str:
    # The API wants second precision and a literal Z.
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
