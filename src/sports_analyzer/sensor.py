"""盯盤: the owner's free odds-api.io key watches the matches with a 運彩 price every ten
minutes; when one moves, The Odds API's consensus is bought for that competition (one
credit; MLB two, its run line being half of G) and the last 運彩 upload is judged
against it (延遲雷達). The fixed radar runs are gone: credits go where something moved.

Why two sources. The Odds API's many-bookmaker consensus is what the fair price comes
from, but 500 credits a month buy about one full look a day. odds-api.io (FanDuel and
Bet365, 100 requests an hour, about 500 a day) can look every ten minutes, but two books
are not a consensus: their de-vigged average sat 0.6pp (median) from it, 1.6pp at
worst, on 36 matches checked 2026-10-16. So the two books only say WHEN; the consensus
still says HOW MUCH. A move is measured on the two books' own prices (now against
their reading when the consensus was last bought), never against the consensus, which
would read the gap between the sources as a move.

Quota (free key: 100 an hour, 500 a day): no hour and no 24 hours may go over
HOURLY_CAP / DAILY_CAP (the API does not say when its day starts, so any 24 hours
count); when a run cannot afford every due match, the ones kicking off soonest go
first. What the tiers leave unused is not wasted: a request carries ten matches, so its
free places go to the matches not due yet, and the quota the tiers will not need (a
forecast of the next 24 hours) buys extra requests for them at an even pace, never
more than leaves every 24 hours ahead room for the forecast. A quiet Thursday then
reads everything every ten minutes; a full Saturday keeps to the tiers.

The state (event lists, readings, usage, triggers) lives in .sensor/, kept between runs
in the Actions cache: odds-api.io data stays out of the public repo (its terms do not
allow redistribution).
"""

from __future__ import annotations

import difflib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from .analysis.pregame import kickoff
from .analysis.taiwan import fair_time
from .fetch.oddsio import LEAGUES, MAX_IDS, IoEvent, IoPrices
from .models.market import devig
from .teams import canonical

# How often a watched match is read, by hours to kickoff: (up to hours, every minutes).
# A whole Saturday of the five leagues comes to about 475 requests (scratch simulation,
# 2026-10-16).
TIERS = ((24.0, 10), (48.0, 30))
WINDOW_HOURS = TIERS[-1][0]
# The planning step: the quota is shared out as if a round came every ten minutes. A
# run does rounds back to back until the next reading falls due (`next_due`, sensor.yml),
# because GitHub starts scheduled runs 25-45 minutes apart; readings go by the time since
# the last one, so an extra round reads nothing early.
RUN_MINUTES = 10
RUNS_PER_DAY = 24 * 60 // RUN_MINUTES
DAY = timedelta(hours=24)
# The forecast of the day's needs also counts the matches that enter the window later
# today, with or without a 運彩 price yet (the next upload may add one).
FORECAST_HOURS = WINDOW_HOURS + 24
# A side's two-book probability moved this much since the consensus was last bought.
MOVE = 0.03
# No new purchase this soon after the last one (a book flickering between two prices).
MIN_GAP = timedelta(minutes=20)
# A reading older than this says nothing about now (the last runs failed or were skipped).
FRESH_READING = timedelta(minutes=30)
# odds-api.io's free key: 100 an hour and 500 a day; a margin for manual checks. The
# day is counted as any 24 hours (the API does not say when its day starts).
DAILY_CAP = 480
HOURLY_CAP = 90
# Stop when odds-api.io itself says this few are left this hour (its count, not ours).
HOUR_MARGIN = 10
# Kept back from the day's spare: cards that appear after the forecast (a new upload).
SPARE_MARGIN = 20
# A run may spend this multiple of an even share of a day (10 requests), so the hours
# with matches can take more than the quiet ones.
BURST = 3.0
EVENTS_TTL = timedelta(hours=6)
# The selected bookmakers are asked again this often (the owner may change them).
BOOKS_TTL = timedelta(days=1)
# A watched match missing from its competition's list: look again at most this often.
UNMAPPED_RETRY = timedelta(hours=1)
MATCH_TOLERANCE = timedelta(hours=2)
MIN_NAME_SCORE = 1.2  # home + away name similarity (2.0 = both identical)
# A match is read only in the WINDOW_HOURS before kickoff, so nothing older is needed.
KEEP_READINGS = timedelta(hours=WINDOW_HOURS + 1)


@dataclass(frozen=True)
class Watch:
    """A match with a 運彩 price, starting within WINDOW_HOURS."""

    sport: str  # football / mlb / nba
    match_id: str
    league: str  # competition code (PL ...), MLB or NBA
    home: str
    away: str
    kickoff: datetime
    bought_at: datetime | None  # The Odds API's match-winner price, last bought

    @property
    def key(self) -> str:
        return f"{self.sport}:{self.match_id}"

    @property
    def sides(self) -> tuple[str, ...]:
        return ("home", "draw", "away") if self.sport == "football" else ("home", "away")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _time(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


# ------------------------------------------------------------------ what to watch
def watched(
    cards: Iterable[tuple[str, Mapping[str, Any]]],
    now: datetime,
    hours: float = WINDOW_HOURS,
    need_tw: bool = True,
) -> list[Watch]:
    """The matches worth reading: a 運彩 price to set a new fair price against, a
    competition odds-api.io covers, kickoff within `hours`. Soonest first. (The
    forecast also counts the ones without a 運彩 price yet: need_tw False.)"""
    end = now + timedelta(hours=hours)
    out: dict[str, Watch] = {}
    for sport, card in cards:
        start = kickoff(dict(card))
        if start is None or not now < start <= end or (need_tw and not card.get("tw")):
            continue
        league = {"mlb": "MLB", "nba": "NBA"}.get(sport) or str(card.get("competition", ""))
        if league not in LEAGUES:
            continue
        mid = str(card.get("match_id") or card.get("game_pk") or card.get("game_id"))
        bought = fair_time((card.get("market") or {}).get("h2h"))
        if bought is not None and bought.tzinfo is None:
            bought = bought.replace(tzinfo=UTC)
        w = Watch(sport, mid, league, str(card["home"]), str(card["away"]), start, bought)
        out.setdefault(w.key, w)
    return sorted(out.values(), key=lambda w: w.kickoff)


def similarity(a: str, b: str) -> float:
    """1.0 for the same team after normalisation and aliases, else how close the
    spellings are ("Juventus" / "Juventus Turin" is about 0.7)."""
    ca, cb = canonical(a), canonical(b)
    return 1.0 if ca == cb else difflib.SequenceMatcher(None, ca, cb).ratio()


def match_events(watch: Iterable[Watch], events: Mapping[str, list[IoEvent]]) -> dict[str, str]:
    """Our match -> odds-api.io event: same competition, kickoff within MATCH_TOLERANCE,
    the pair of names closest (and close enough). Home and away must agree: a swapped
    pair would turn the readings around."""
    out = {}
    for w in watch:
        best, best_score = None, 0.0
        for e in events.get(w.league, []):
            if abs(e.start - w.kickoff) > MATCH_TOLERANCE:
                continue
            score = similarity(w.home, e.home) + similarity(w.away, e.away)
            if score > best_score:
                best, best_score = e, score
        if best is not None and best_score >= MIN_NAME_SCORE:
            out[w.key] = best.id
    return out


def stale_leagues(
    watch: Iterable[Watch], state: Mapping[str, Any], mapped: Mapping[str, str], now: datetime
) -> list[str]:
    """Competitions whose event list to fetch now: none yet, older than EVENTS_TTL, or
    a watched match missing from it and the list older than UNMAPPED_RETRY."""
    out = []
    for w in watch:
        if w.league in out:
            continue
        cached = (state.get("events") or {}).get(w.league) or {}
        at = _time(cached.get("at"))
        age = now - at if at else None
        if age is None or age >= EVENTS_TTL or (w.key not in mapped and age >= UNMAPPED_RETRY):
            out.append(w.league)
    return out


def cached_events(state: Mapping[str, Any]) -> dict[str, list[IoEvent]]:
    out = {}
    for league, cached in (state.get("events") or {}).items():
        rows = []
        for eid, slug, start, home, away in cached.get("events", []):
            at = _time(start)
            if at is not None:
                rows.append(IoEvent(str(eid), slug, at, home, away))
        out[league] = rows
    return out


def store_events(state: dict[str, Any], league: str, events: list[IoEvent], now: datetime) -> None:
    state.setdefault("events", {})[league] = {
        "at": _iso(now),
        "events": [[e.id, e.league, _iso(e.start), e.home, e.away] for e in events],
    }


# ---------------------------------------------------------------- when to read
def interval_minutes(hours_left: float) -> int | None:
    """How often a match this far from kickoff is read (None: not watched, too far
    away or started)."""
    if hours_left <= 0:
        return None
    for up_to, minutes in TIERS:
        if hours_left <= up_to:
            return minutes
    return None


def last_readings(state: Mapping[str, Any]) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for r in state.get("readings", []):
        at = _time(r.get("at"))
        if at is not None and (r["key"] not in out or at > out[r["key"]]):
            out[r["key"]] = at
    return out


def due(watch: Iterable[Watch], last: Mapping[str, datetime], now: datetime) -> list[Watch]:
    """The watched matches whose next reading is due, soonest kickoff first (they go
    first when the quota is short). A minute of slack: the schedule drifts, and a
    ten-minute tier must not skip every other run."""
    out = []
    for w in watch:
        minutes = interval_minutes((w.kickoff - now).total_seconds() / 3600)
        seen = last.get(w.key)
        if minutes is not None and (seen is None or now - seen >= timedelta(minutes=minutes - 1)):
            out.append(w)
    return sorted(out, key=lambda w: w.kickoff)


def next_due(
    watch: Iterable[Watch], last: Mapping[str, datetime], now: datetime
) -> datetime | None:
    """When the next reading falls due: each match's last reading plus its tier's
    interval (less the minute of slack), the earliest of them, not before now. None when
    nothing is watched. A run keeps going until then (sensor.yml) instead of waiting for
    GitHub's next scheduled start, which here comes 25-45 minutes late."""
    times = []
    for w in watch:
        minutes = interval_minutes((w.kickoff - now).total_seconds() / 3600)
        if minutes is None:
            continue
        seen = last.get(w.key)
        at = now if seen is None else seen + timedelta(minutes=minutes - 1)
        times.append(max(at, now))
    return min(times, default=None)


def reads(
    watch: Iterable[Watch],
    last: Mapping[str, datetime],
    now: datetime,
    budget: int,
    spare: int = 0,
) -> list[list[Watch]]:
    """This run's odds requests, MAX_IDS matches each. First the matches due by their
    tier, soonest kickoff first, as many requests as `budget` allows. The last request's
    free places, and up to `spare` more requests (within the budget), go to the other
    matches not read in the last RUN_MINUTES, soonest first: reading them costs nothing
    or what the day can spare. `watch`: the matches with an odds-api.io event."""
    watch = list(watch)
    must = due(watch, last, now)
    taken = {w.key for w in must}
    recent = timedelta(minutes=RUN_MINUTES - 1)
    extra = [
        w
        for w in watch
        if w.key not in taken and (w.key not in last or now - last[w.key] >= recent)
    ]
    chunks = [must[i : i + MAX_IDS] for i in range(0, len(must), MAX_IDS)][: max(0, budget)]
    if chunks and len(chunks[-1]) < MAX_IDS:
        room = MAX_IDS - len(chunks[-1])
        chunks[-1] = chunks[-1] + extra[:room]
        extra = extra[room:]
    paid = min(max(0, budget - len(chunks)), max(0, spare))
    while paid and extra:
        chunks.append(extra[:MAX_IDS])
        extra = extra[MAX_IDS:]
        paid -= 1
    return chunks


def forecast(population: Iterable[Watch], now: datetime) -> list[tuple[datetime, int]]:
    """The requests the tiers alone will need at each run of the next 24 hours: the
    matches due then, MAX_IDS to a request, plus each competition's event list every
    EVENTS_TTL. Every match counts as read now; `population` (FORECAST_HOURS ahead)
    includes the ones that enter the window later."""
    pop = list(population)
    last = {w.key: now for w in pop}
    lists = sum(len(LEAGUES[league][1]) for league in {w.league for w in pop})
    every = max(1, round(EVENTS_TTL / timedelta(minutes=RUN_MINUTES)))
    plan = []
    for k in range(1, RUNS_PER_DAY + 1):
        at = now + k * timedelta(minutes=RUN_MINUTES)
        now_due = due(pop, last, at)
        for w in now_due:
            last[w.key] = at
        plan.append((at, math.ceil(len(now_due) / MAX_IDS) + (lists if k % every == 0 else 0)))
    return plan


def _past(usage: Mapping[str, Any], now: datetime) -> list[tuple[datetime, int]]:
    """The runs of the last 24 hours and their requests."""
    out = []
    for at, n in usage.get("recent", []):
        t = _time(at)
        if t is not None and now - DAY < t <= now:
            out.append((t, int(n)))
    return out


def used(usage: Mapping[str, Any], now: datetime, span: timedelta = DAY) -> int:
    """Requests sent in the last `span` (24 hours by default)."""
    return sum(n for t, n in _past(usage, now) if t > now - span)


def headroom(usage: Mapping[str, Any], now: datetime, plan: list[tuple[datetime, int]]) -> int:
    """Requests this run can make with every 24-hour window from now on within
    DAILY_CAP, if the later runs make what `plan` (the forecast) says: at each later
    run, what is still inside its window from before, plus the plan up to it."""
    past = _past(usage, now)
    best = DAILY_CAP - sum(n for _, n in past)
    planned = 0
    for at, n in plan:
        planned += n
        still_in = sum(m for t, m in past if t > at - DAY)
        best = min(best, DAILY_CAP - still_in - planned)
    return best


def spare_requests(
    usage: Mapping[str, Any], now: datetime, plan: list[tuple[datetime, int]], spent: int, must: int
) -> int:
    """Extra requests this run may make beyond the tiers. At an even pace, what the
    tiers leave of a day's quota (DAILY_CAP less the forecast and SPARE_MARGIN, over the
    day's runs; rounded up, so it is used to the full), but never more than leaves every
    24-hour window ahead room for the forecast and SPARE_MARGIN. `spent`: this run's
    requests so far; `must`: the ones it still has to make for the due matches."""
    need = sum(n for _, n in plan) + spent + must
    even = math.ceil(max(0, DAILY_CAP - SPARE_MARGIN - need) / RUNS_PER_DAY)
    room = headroom(usage, now, plan) - spent - must - SPARE_MARGIN
    return max(0, min(even, room))


def allowance(usage: Mapping[str, Any], now: datetime) -> int:
    """Requests this run may make: what is left of the last hour's and the last 24
    hours' quota (odds-api.io does not say when its day starts; no 24 hours over the cap
    keeps every calendar day under it, whatever its time zone), and at most BURST times
    an even share of a day."""
    hour = HOURLY_CAP - used(usage, now, timedelta(hours=1))
    day = DAILY_CAP - used(usage, now)
    return max(0, min(hour, day, math.ceil(BURST * DAILY_CAP / RUNS_PER_DAY)))


def record_usage(usage: Mapping[str, Any], spent: int, now: datetime) -> dict[str, Any]:
    """The last 24 hours' runs and their requests, after this one."""
    recent = [[_iso(t), n] for t, n in _past(usage, now)]
    if spent:
        recent.append([_iso(now), spent])
    return {"recent": recent}


# ---------------------------------------------------------------------- reading
def two_book_probs(prices: IoPrices, sides: tuple[str, ...]) -> dict[str, float] | None:
    """Each bookmaker's match-winner prices de-vigged, then averaged over the books that
    quote every side; None when no book does."""
    per_book = [
        devig([p[s] for s in sides]) for p in prices.books.values() if all(s in p for s in sides)
    ]
    if not per_book:
        return None
    return {s: round(mean(b[i] for b in per_book), 4) for i, s in enumerate(sides)}


def moved(
    watch: Iterable[Watch],
    readings: Iterable[Mapping[str, Any]],
    now: datetime,
    triggers: Mapping[str, str] | None = None,
) -> list[Watch]:
    """Watched matches whose two-book probability of some side moved MOVE or more since
    The Odds API's price was last bought for them, or since the sensor last asked for
    one for their competition (`triggers`: a purchase that never came, e.g. a match
    The Odds API does not list, must not be asked for again every ten minutes). The
    baseline is the reading nearest that moment (the market the purchase saw); with no
    purchase at all, the first reading."""
    series: dict[str, list[tuple[datetime, dict[str, float]]]] = defaultdict(list)
    for r in readings:
        at = _time(r.get("at"))
        if at is not None:
            series[r["key"]].append((at, r["p"]))
    out = []
    for w in watch:
        points = sorted(series.get(w.key, []), key=lambda x: x[0])
        if len(points) < 2 or now - points[-1][0] > FRESH_READING:
            continue
        asked = _time((triggers or {}).get(w.league))
        last_buy = max((t for t in (w.bought_at, asked) if t is not None), default=None)
        if last_buy is not None and now - last_buy < MIN_GAP:
            continue
        latest = points[-1][1]
        anchor = last_buy or points[0][0]
        base = min(points[:-1], key=lambda x: abs(x[0] - anchor))[1]
        if any(abs(latest[s] - base[s]) >= MOVE for s in w.sides if s in latest and s in base):
            out.append(w)
    return out


def purchases(moving: Iterable[Watch]) -> list[str]:
    """The competitions to buy for, soonest kickoff first (MLB is one purchase)."""
    out: list[str] = []
    for w in sorted(moving, key=lambda w: w.kickoff):
        if w.league not in out:
            out.append(w.league)
    return out


def purchase_cost(league: str) -> int:
    """Credits of one triggered purchase: h2h; MLB also the run line (half of G), NBA
    also the handicap (運彩's main basketball market)."""
    return 2 if league in ("MLB", "NBA") else 1


def record_triggers(state: dict[str, Any], leagues: Iterable[str], now: datetime) -> None:
    """The competitions a purchase was asked for now: their next baseline."""
    for league in leagues:
        state.setdefault("triggers", {})[league] = _iso(now)


def books_due(state: Mapping[str, Any], now: datetime) -> bool:
    """Ask for the selected bookmakers: none yet, or not since BOOKS_TTL."""
    at = _time(state.get("books_at"))
    return not state.get("books") or at is None or now - at >= BOOKS_TTL


# ------------------------------------------------------------------------- state
def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("events", {})
    state.setdefault("readings", [])
    state.setdefault("usage", {})
    state.setdefault("books", [])
    state.setdefault("triggers", {})
    return state


def prune(state: dict[str, Any], now: datetime) -> None:
    """Readings and triggers older than KEEP_READINGS go; nothing needs them after
    kickoff."""
    keep_from = now - KEEP_READINGS
    state["readings"] = [
        r for r in state["readings"] if (_time(r.get("at")) or keep_from) > keep_from
    ]
    state["triggers"] = {
        k: v for k, v in state.get("triggers", {}).items() if (_time(v) or keep_from) > keep_from
    }


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
