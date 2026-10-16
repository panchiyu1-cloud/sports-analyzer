"""The Odds API credit budget: spend evenly so the page never goes dark mid-month.

The free plan gives 500 credits a month, and The Odds API resets every plan's credits on
the 1st of each month (UTC); what is left does not carry over. Every run that buys odds
asks `allowance_today()` how many credits it may spend today and buys less (h2h only) or
nothing (the previous prices are kept, marked stale) when the allowance is short. The
credits left are spread evenly over the days to the next 1st, so the month's last days
are not starved and nothing expires unused.

Counters saved in an earlier month mean the quota has reset since, but only a purchase
reveals the new counters: that run is let through and `record` sees `x-requests-used`
drop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# Kept back for the evening refresh after a heavy day and for manual runs.
RESERVE = 25
# A day may spend up to this multiple of its even share: weekends need more than
# midweek, and tomorrow's share shrinks by whatever today overspent.
BURST = 1.6
# Pre-game refreshes (analysis/pregame.py) may spend this much more per day than the
# routine runs: a price minutes before first pitch is worth more than a routine one.
# The overspend comes out of the following days' shares; the reserve is never touched.
PREGAME_EXTRA = 6


@dataclass(frozen=True)
class BudgetState:
    period_start: str  # ISO date the current quota period was first seen
    used: int | None = None  # x-requests-used after the last run
    remaining: int | None = None  # x-requests-remaining after the last run
    day: str = ""  # UTC date of `day_start_used`
    day_start_used: int | None = None  # `used` before the first purchase of that day


def load(path: Path, today: date) -> BudgetState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return BudgetState(**{k: raw.get(k) for k in BudgetState.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return BudgetState(period_start=today.isoformat())


def save(path: Path, state: BudgetState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=1) + "\n", encoding="utf-8")


def allowance_today(state: BudgetState, today: date, extra: float = 0.0) -> float | None:
    """Credits this run may still spend today; None = no limit known.

    In a month after the one the counters were saved in, the quota has reset, but only
    a purchase reveals the new counters. So the run is let through (None) and `record`
    then sees `used` drop. Without this, a budget that ended a month near the reserve
    would buy nothing ever again.

    `extra` raises today's ceiling (pre-game refreshes) but never past the reserve.
    """
    if state.remaining is None:
        return None
    if _reset_since(state, today):
        return None
    spent_today = 0
    if (
        state.day == today.isoformat()
        and state.used is not None
        and state.day_start_used is not None
    ):
        spent_today = max(0, state.used - state.day_start_used)
    allowed = BURST * daily_share(state, today, spent_today) + extra - spent_today
    if extra:
        allowed = min(allowed, state.remaining - RESERVE)
    return max(0.0, allowed)


def daily_share(state: BudgetState, today: date, spent_today: int = 0) -> float:
    """An even split of the credits left (above the reserve) over the days left."""
    if state.remaining is None:
        return 0.0
    days_left = max(1, (period_end(today) - today).days)
    # Counted from the start of today, so a run late in the day sees the same share.
    return max(0.0, (state.remaining + spent_today - RESERVE) / days_left)


def period_end(today: date) -> date:
    """The next 1st of a month: when the quota resets."""
    return date(today.year + today.month // 12, today.month % 12 + 1, 1)


def _reset_since(state: BudgetState, today: date) -> bool:
    """The counters were saved in an earlier month: the quota has reset since."""
    seen = date.fromisoformat(state.day or state.period_start)
    return (seen.year, seen.month) < (today.year, today.month)


def record(state: BudgetState, used: int | None, remaining: int | None, today: date) -> BudgetState:
    """Fold a run's final counters into the state (detects a quota reset)."""
    if used is None or remaining is None:
        return state
    period_start = state.period_start
    if state.used is not None and used < state.used:
        period_start = today.isoformat()
        log.info("odds budget: quota reset detected (used %s -> %s)", state.used, used)
    if state.day != today.isoformat():
        # The previous run's counter is where today started.
        day, day_start = today.isoformat(), state.used if state.used is not None else used
        if period_start == today.isoformat() and state.used is not None and used < state.used:
            day_start = 0
    else:
        day, day_start = state.day, state.day_start_used
    return BudgetState(period_start, used, remaining, day, day_start)


def plan_markets(
    wanted: tuple[str, ...], calls: int, allowance: float | None, essential: tuple[str, ...]
) -> tuple[str, ...]:
    """The markets this run can afford: all wanted, the essential ones, or none.

    `calls` is how many odds requests the run will make; each costs len(markets).
    """
    if allowance is None or calls == 0:
        return wanted
    if calls * len(wanted) <= allowance:
        return wanted
    if essential and calls * len(essential) <= allowance:
        log.warning("odds budget: %.1f credits left today; buying %s only", allowance, essential)
        return essential
    log.warning("odds budget: %.1f credits left today; keeping earlier prices", allowance)
    return ()
